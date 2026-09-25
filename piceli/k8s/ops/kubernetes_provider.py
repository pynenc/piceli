"""Explicit-client Kubernetes HTTP provider. Never loads ambient configuration."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import time
from typing import Any
from urllib.parse import quote, urlsplit

from piceli.k8s.ops.bounds import bounded_call, positive, seconds, strict_json, text
from piceli.k8s.ops.discovery import (
    RELEASE_NAMESPACE_ANNOTATION,
    RETAINED_KINDS,
    ApiResource,
    ApplyProbeResult,
    ApplyProbeStatus,
    DiscoveredResource,
    DiscoveryFailureKind,
    DiscoveryPage,
    DiscoveryProvenance,
    EvidenceSource,
    Ownership,
    PlanTarget,
    ReadinessProbeResult,
    ReadinessStatus,
    ResourceIdentity,
    ResourceListRequest,
    ResourceScope,
    ResourceType,
)
from piceli.k8s.ops.exec_credentials import approved_refresh
from piceli.k8s.ops.plan import (
    FieldManagerEntry,
    ResourceIntent,
    field_manager_entries,
    is_transferable,
    manifest_contains,
    transferable_managers,
)

OWNER_ANNOTATION = "piceli.io/owner"
OPERATION_ANNOTATION = "piceli.io/operation"
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


def _entry(value: dict[str, Any]) -> FieldManagerEntry:
    return FieldManagerEntry(
        str(value.get("manager", "")),
        str(value.get("operation", "")),
        str(value.get("subresource", "")),
        json.dumps(value.get("fieldsV1") or {}),
    )


def _merge_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Union of two FieldsV1 trees."""
    for key, child in source.items():
        if isinstance(child, dict) and child:
            node = target.setdefault(key, {})
            if isinstance(node, dict):
                _merge_fields(node, child)
        else:
            target.setdefault(key, {})


def _without_ownership(manifest: dict[str, Any]) -> dict[str, Any]:
    """Object content minus what a metadata-only adoption may change."""
    value = json.loads(json.dumps(manifest))
    value.pop("status", None)
    metadata = value.get("metadata", {})
    for key in ("resourceVersion", "managedFields"):
        metadata.pop(key, None)
    annotations = metadata.get("annotations", {})
    for key in (OWNER_ANNOTATION, OPERATION_ANNOTATION):
        annotations.pop(key, None)
    if not annotations:
        metadata.pop("annotations", None)
    return dict(value)


class ProviderError(Exception):
    """Allowlisted failure only; server bodies and credentials are never exposed."""

    def __init__(self, category: str, *, status: int = 0, ambiguous: bool = False):
        super().__init__(category)
        self.category = category
        self.status = status
        self.ambiguous = ambiguous


class KubernetesProvider:
    """Caller-owned ApiClient transport, one target, manager and ownership domain.

    Uses the supplied client's pool directly to bound error bodies too: the SDK's
    ApiException path otherwise eagerly reads arbitrary server error payloads.
    Redirects and automatic retries are disabled for every request.
    """

    provider_id = "piceli.kubernetes/v2"

    def __init__(
        self,
        api_client: Any,
        *,
        target: PlanTarget,
        field_manager: str,
        owner_id: str,
        source: EvidenceSource,
        cluster_uid: str,
        namespace_uid: str,
        request_seconds: float = 5,
        max_response_bytes: int = 1_000_000,
        inherited_owner_ids: tuple[str, ...] = (),
    ) -> None:
        self.client = api_client
        self.target = target
        self.field_manager = text(field_manager, "field manager")
        self.owner_id = text(owner_id, "owner")
        # Explicit, opt-in predecessor owners (e.g. a previous release's owner
        # id) whose objects this provider may treat as managed. Exact ids only.
        if isinstance(inherited_owner_ids, str):
            raise ValueError("inherited owner ids must be a tuple of ids")
        self.inherited_owner_ids = frozenset(
            text(value, "inherited owner") for value in inherited_owner_ids
        )
        self.request_seconds = seconds(request_seconds, "request", 60)
        self.max_response_bytes = positive(
            max_response_bytes, "response bytes", 10_000_000
        )
        self.host = api_client.configuration.host.rstrip("/")
        parsed = urlsplit(self.host)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("client requires an explicit API origin")
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if source is EvidenceSource.LOOPBACK:
            if not loopback or parsed.scheme != "http":
                raise ValueError(
                    "loopback provider requires a literal loopback HTTP origin"
                )
        elif source is EvidenceSource.LIVE:
            if parsed.scheme != "https" or not api_client.configuration.verify_ssl:
                raise ValueError("live provider requires verified HTTPS")
        else:
            raise ValueError("provider source must be explicit loopback or live")
        # A refresh hook is accepted only when Piceli's exec credential source
        # installed it on this very client (explicit allow_exec, pinned plugin);
        # hooks from the SDK's loaders or any other callable are refused.
        if api_client.configuration.proxy or (
            api_client.configuration.refresh_api_key_hook is not None
            and not approved_refresh(api_client)
        ):
            raise ValueError(
                "provider requires explicit credentials and direct transport"
            )
        self.provenance = DiscoveryProvenance(
            source,
            hashlib.sha256(self.host.encode()).hexdigest(),
            cluster_uid,
            namespace_uid,
        )
        self._apis: dict[ResourceType, ApiResource] = {}

    def _target(self, target: PlanTarget) -> None:
        if (
            target != self.target
            or self.client.configuration.host.rstrip("/") != self.host
        ):
            raise ProviderError("target-mismatch")

    def verify_target(self, *, deadline: float | None = None) -> None:
        """Cluster identity is the kube-system Namespace UID, never a context name."""
        for name, uid in (
            ("kube-system", self.provenance.cluster_uid),
            (self.target.namespace, self.provenance.namespace_uid),
        ):
            manifest = self._request(
                "GET", "/api/v1/namespaces/" + quote(name, safe=""), deadline=deadline
            )
            metadata = manifest.get("metadata", {})
            if (
                manifest.get("apiVersion") != "v1"
                or manifest.get("kind") != "Namespace"
                or metadata.get("name") != name
                or metadata.get("uid") != uid
                or not isinstance(metadata.get("resourceVersion"), str)
                or not metadata["resourceVersion"]
                or metadata.get("deletionTimestamp")
            ):
                raise ProviderError("server-target-identity-mismatch")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        deadline: float | None = None,
        apply: bool = False,
        merge: bool = False,
    ) -> dict[str, Any]:
        self._target(self.target)
        end = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + self.request_seconds,
        )
        mutating = method != "GET" and (query or {}).get("dryRun") != "All"
        if time.monotonic() >= end:
            raise ProviderError("deadline-exceeded")
        if mutating:
            self.verify_target(deadline=end)
        headers = {
            "Accept": "application/json",
            "Content-Type": (
                "application/apply-patch+yaml"
                if apply
                else "application/merge-patch+json"
                if merge
                else "application/json"
            ),
        }
        self.client.update_params_for_auth(headers, [], ["BearerToken"])
        if (
            body is not None
            and len(json.dumps(body, allow_nan=False).encode())
            > self.max_response_bytes
        ):
            raise ProviderError("request-byte-limit")

        def send() -> dict[str, Any]:
            from urllib.parse import urlencode

            from urllib3 import Timeout
            from urllib3.exceptions import TimeoutError as TransportTimeout

            url = self.host + path
            if query:
                url += "?" + urlencode(query)
            response = None
            try:
                if time.monotonic() >= end:
                    raise ProviderError("deadline-exceeded")
                remaining = max(0.001, end - time.monotonic())
                response = self.client.rest_client.pool_manager.request(
                    method,
                    url,
                    body=None
                    if body is None
                    else json.dumps(body, allow_nan=False).encode(),
                    headers=headers,
                    preload_content=False,
                    retries=False,
                    redirect=False,
                    timeout=Timeout(total=remaining, connect=remaining, read=remaining),
                )
                # Error bodies are intentionally never loaded or included in errors.
                if not 200 <= response.status < 300:
                    category = {
                        401: "rbac-denied",
                        403: "rbac-denied",
                        404: "not-found",
                        409: "conflict",
                        422: "invalid-request",
                        429: "api-unavailable",
                    }.get(response.status, "api-unavailable")
                    raise ProviderError(
                        category,
                        status=response.status,
                        ambiguous=mutating and response.status >= 500,
                    )
                raw = response.read(self.max_response_bytes + 1, decode_content=True)
                if len(raw) > self.max_response_bytes:
                    raise ProviderError("response-byte-limit", ambiguous=mutating)
                decoded = strict_json(raw.decode(), self.max_response_bytes)
                if not isinstance(decoded, dict):
                    raise ValueError("expected object")
                return decoded
            except ProviderError:
                raise
            except TransportTimeout:
                raise ProviderError("deadline-exceeded", ambiguous=mutating) from None
            except Exception:
                raise ProviderError("transport-error", ambiguous=mutating) from None
            finally:
                if response is not None:
                    response.close()

        try:
            return bounded_call(send, end)
        except TimeoutError:
            raise ProviderError("deadline-exceeded", ambiguous=mutating) from None

    @staticmethod
    def _root(resource_type: ResourceType) -> str:
        return (
            "/apis/" if "/" in resource_type.api_version else "/api/"
        ) + resource_type.api_version

    def discover_api_resource(
        self,
        target: PlanTarget,
        resource_type: ResourceType,
        *,
        deadline: float | None = None,
    ) -> ApiResource | DiscoveryFailureKind:
        self._target(target)
        try:
            data = self._request("GET", self._root(resource_type), deadline=deadline)
            if data.get("groupVersion") != resource_type.api_version or not isinstance(
                data.get("resources"), list
            ):
                return DiscoveryFailureKind.INVALID_PAGE
            entries = [
                entry
                for entry in data["resources"]
                if entry.get("kind") == resource_type.kind
                and "/" not in entry.get("name", "")
            ]
            if (
                len(entries) != 1
                or not isinstance(entries[0].get("namespaced"), bool)
                or not {"get", "list"} <= set(entries[0].get("verbs", []))
            ):
                return DiscoveryFailureKind.API_UNAVAILABLE
            entry = entries[0]
            api = ApiResource(
                resource_type,
                ResourceScope.NAMESPACED
                if entry["namespaced"]
                else ResourceScope.CLUSTER,
                entry["name"],
            )
            previous = self._apis.get(resource_type)
            if previous is not None and previous != api:
                return DiscoveryFailureKind.INVALID_PAGE
            self._apis[resource_type] = api
            return api
        except ProviderError as error:
            return self._failure(error)
        except (ValueError, TypeError, KeyError, AttributeError):
            return DiscoveryFailureKind.INVALID_PAGE

    @staticmethod
    def _failure(error: ProviderError) -> DiscoveryFailureKind:
        return {
            "rbac-denied": DiscoveryFailureKind.RBAC_DENIED,
            "deadline-exceeded": DiscoveryFailureKind.DEADLINE_EXCEEDED,
            "response-byte-limit": DiscoveryFailureKind.LIMIT_EXCEEDED,
        }.get(error.category, DiscoveryFailureKind.API_UNAVAILABLE)

    def api_for(
        self, identity: ResourceIdentity, *, deadline: float | None = None
    ) -> ApiResource:
        key = ResourceType(identity.api_version, identity.kind)
        api = self._apis.get(key)
        if api is None:
            discovered = self.discover_api_resource(self.target, key, deadline=deadline)
            if not isinstance(discovered, ApiResource):
                raise ProviderError(discovered.value)
            api = discovered
        expected = (
            self.target.namespace if api.scope is ResourceScope.NAMESPACED else ""
        )
        if identity.namespace != expected:
            raise ProviderError("scope-mismatch")
        return api

    def _path(self, api: ApiResource, name: str | None = None) -> str:
        root = self._root(api.resource_type)
        if api.scope is ResourceScope.NAMESPACED:
            root += "/namespaces/" + quote(self.target.namespace, safe="")
        return root + "/" + api.plural + ("/" + quote(name, safe="") if name else "")

    def _is_managed(
        self, manifest: dict[str, Any], scope: ResourceScope = ResourceScope.NAMESPACED
    ) -> bool:
        """Ownership is an exact owner-id match, never inferred from id shape.

        A cluster-scoped object is shared by every namespace, so it is managed
        only when it also names this target's namespace
        (``piceli.io/namespace``): one owner's releases in two namespaces
        never change or prune each other's cluster-scoped objects.
        """
        ann = manifest.get("metadata", {}).get("annotations") or {}
        owner = ann.get(OWNER_ANNOTATION)
        if not isinstance(owner, str) or not owner:
            return False
        if (
            scope is ResourceScope.CLUSTER
            and ann.get(RELEASE_NAMESPACE_ANNOTATION) != self.target.namespace
        ):
            return False
        return owner == self.owner_id or owner in self.inherited_owner_ids

    def _resource(
        self, manifest: dict[str, Any], api: ApiResource
    ) -> DiscoveredResource:
        resource = DiscoveredResource.from_manifest(
            manifest,
            scope=api.scope,
            ownership=Ownership.MANAGED
            if self._is_managed(manifest, api.scope)
            else Ownership.UNMANAGED,
        )
        if (
            ResourceType(resource.identity.api_version, resource.identity.kind)
            != api.resource_type
        ):
            raise ProviderError("identity-mismatch")
        self.api_for(resource.identity)
        return resource

    def list_resources(self, request: ResourceListRequest) -> DiscoveryPage:
        self._target(request.target)
        api = self._apis.get(request.api_resource.resource_type)
        if api != request.api_resource:
            raise ProviderError("undiscovered-api")
        positive(request.page_size, "page", 1000)
        query: dict[str, Any] = {"limit": request.page_size}
        if request.continuation is not None:
            query["continue"] = text(request.continuation, "continuation")
        try:
            data = self._request("GET", self._path(api), query=query)
            if (
                data.get("apiVersion") != api.resource_type.api_version
                or data.get("kind") != api.resource_type.kind + "List"
                or not isinstance(data.get("items"), list)
            ):
                raise ValueError("invalid list")
            if len(data["items"]) > request.page_size:
                raise ValueError("oversized page")
            metadata = data.get("metadata", {})
            text(metadata.get("resourceVersion"), "list resourceVersion")
            continuation = metadata.get("continue", "")
            if not isinstance(continuation, str):
                raise ValueError("invalid continuation")
            continuation = continuation or None
            if continuation is not None:
                text(continuation, "continuation")
            # Kubernetes List responses establish the item type.  Some
            # conforming API servers omit redundant apiVersion/kind fields from
            # individual list items, so restore only those omitted values before
            # applying the normal identity validation below.  A supplied but
            # conflicting value still reaches _resource and is rejected.
            items = []
            for item in data["items"]:
                if not isinstance(item, dict):
                    raise ValueError("list item must be an object")
                normalized = dict(item)
                normalized.setdefault("apiVersion", api.resource_type.api_version)
                normalized.setdefault("kind", api.resource_type.kind)
                items.append(normalized)
            return DiscoveryPage(
                request.target,
                api,
                tuple(self._resource(item, api) for item in items),
                continuation=continuation,
                list_resource_version=metadata["resourceVersion"],
            )
        except ProviderError as error:
            return DiscoveryPage(request.target, api, failure=self._failure(error))
        except (ValueError, TypeError, KeyError, AttributeError):
            return DiscoveryPage(
                request.target, api, failure=DiscoveryFailureKind.INVALID_PAGE
            )

    def get(
        self, identity: ResourceIdentity, *, deadline: float | None = None
    ) -> DiscoveredResource | None:
        api = self.api_for(identity, deadline=deadline)
        try:
            result = self._resource(
                self._request("GET", self._path(api, identity.name), deadline=deadline),
                api,
            )
            if result.identity != identity:
                raise ProviderError("identity-mismatch")
            return result
        except ProviderError as error:
            if error.status == 404:
                return None
            raise

    def read_secret_key(
        self, name: str, key: str, *, deadline: float | None = None
    ) -> bytes:
        """Read one data key of a Secret in the target namespace (an explicit import).

        The value is returned to the caller only; it is never logged, and a
        failure carries an allowlisted category, never the server's body.
        """
        manifest = self._request(
            "GET",
            "/api/v1/namespaces/"
            + quote(self.target.namespace, safe="")
            + "/secrets/"
            + quote(text(name, "Secret name"), safe=""),
            deadline=deadline,
        )
        metadata = manifest.get("metadata") or {}
        if (
            manifest.get("apiVersion") != "v1"
            or manifest.get("kind") != "Secret"
            or metadata.get("name") != name
            or metadata.get("namespace") != self.target.namespace
        ):
            raise ProviderError("identity-mismatch")
        data = manifest.get("data") or {}
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, str):
            raise ProviderError("secret-key-missing")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError:
            raise ProviderError("invalid-secret-data") from None

    def write(
        self,
        identity: ResourceIdentity,
        manifest: dict[str, Any],
        *,
        create: bool,
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> DiscoveredResource | None:
        """Create, or server-side apply with ``force=false`` (conflicts surface)."""
        return self._write(
            identity,
            manifest,
            create=create,
            dry_run=dry_run,
            deadline=deadline,
            force=False,
        )

    def _write(
        self,
        identity: ResourceIdentity,
        manifest: dict[str, Any],
        *,
        create: bool,
        dry_run: bool,
        deadline: float | None,
        force: bool,
    ) -> DiscoveredResource | None:
        if force and create:
            raise ProviderError("invalid-force")
        api = self.api_for(identity, deadline=deadline)
        if (
            manifest.get("apiVersion"),
            manifest.get("kind"),
            manifest.get("metadata", {}).get("name"),
            manifest.get("metadata", {}).get("namespace", ""),
        ) != (identity.api_version, identity.kind, identity.name, identity.namespace):
            raise ProviderError("identity-mismatch")
        metadata = manifest["metadata"]
        if not create and (
            not metadata.get("uid") or not metadata.get("resourceVersion")
        ):
            raise ProviderError("missing-precondition")
        query = {"fieldManager": self.field_manager}
        if not create:
            # ``force=true`` is reachable only through ``take_over``.
            query["force"] = "true" if force else "false"
        if dry_run:
            query["dryRun"] = "All"
        raw = self._request(
            "POST" if create else "PATCH",
            self._path(api, None if create else identity.name),
            body=manifest,
            query=query,
            deadline=deadline,
            apply=not create,
        )
        try:
            if dry_run:
                # Admission responses are not persisted Kubernetes objects.
                # K3s validly omits UID/resourceVersion from a dry-run create,
                # so validate only the response identity and requested Piceli
                # ownership marker.  Callers use this branch solely as an
                # admission gate and must not treat it as observed state.
                returned = raw.get("metadata", {})
                if (
                    raw.get("apiVersion"),
                    raw.get("kind"),
                    returned.get("name"),
                    returned.get("namespace", ""),
                ) != (
                    identity.api_version,
                    identity.kind,
                    identity.name,
                    identity.namespace,
                ):
                    raise ValueError("dry-run identity")
                annotations = metadata.get("annotations", {})
                if any(
                    returned.get("annotations", {}).get(key) != annotations[key]
                    for key in (OWNER_ANNOTATION, OPERATION_ANNOTATION)
                    if key in annotations
                ):
                    raise ValueError("dry-run ownership")
                return None
            result = self._resource(raw, api)
            if result.identity != identity:
                raise ValueError("identity")
            if not create and result.manifest["metadata"]["uid"] != metadata["uid"]:
                raise ValueError("UID")
            annotations = metadata.get("annotations", {})
            if any(
                result.manifest["metadata"].get("annotations", {}).get(key)
                != annotations[key]
                for key in (OWNER_ANNOTATION, OPERATION_ANNOTATION)
                if key in annotations
            ):
                raise ValueError("operation ownership")
            return result
        except (ValueError, ProviderError):
            raise ProviderError(
                "invalid-write-response", ambiguous=not dry_run
            ) from None

    def update_owned(
        self,
        current: DiscoveredResource,
        manifest: dict[str, Any],
        *,
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> DiscoveredResource | None:
        """Optimistically update an existing Piceli-owned resource without force.

        Kubernetes records ordinary creates as ``Update`` field ownership, so a
        later server-side apply can conflict with Piceli's own prior manager.
        This path is restricted to the same owner and exact UID/resourceVersion;
        content-based executor reconciliation retains crash safety.
        """
        identity = current.identity
        if current.ownership is not Ownership.MANAGED:
            raise ProviderError("ownership-precondition-failed")
        metadata = manifest.get("metadata", {})
        if metadata.get("uid") != current.manifest["metadata"].get(
            "uid"
        ) or metadata.get("resourceVersion") != current.manifest["metadata"].get(
            "resourceVersion"
        ):
            raise ProviderError("uid-version-precondition-failed")
        query: dict[str, Any] = {"fieldManager": self.field_manager}
        if dry_run:
            query["dryRun"] = "All"
        raw = self._request(
            "PATCH",
            self._path(self.api_for(identity, deadline=deadline), identity.name),
            body=manifest,
            query=query,
            deadline=deadline,
            merge=True,
        )
        if dry_run:
            returned = raw.get("metadata", {})
            if returned.get("name") != identity.name:
                raise ProviderError("invalid-write-response")
            return None
        result = self._resource(raw, self.api_for(identity, deadline=deadline))
        if (
            result.identity != identity
            or result.manifest["metadata"].get("uid")
            != current.manifest["metadata"].get("uid")
            or result.ownership is not Ownership.MANAGED
        ):
            raise ProviderError("invalid-write-response", ambiguous=True)
        return result

    def preview_update(
        self,
        current: DiscoveredResource,
        manifest: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """The object the API server would store for :meth:`update_owned`.

        Sends the same merge patch (same field manager, the observed UID and
        resourceVersion in the body as preconditions) with ``dryRun=All``.
        Patch options are query parameters, which the API server honours for
        PATCH (unlike DELETE, whose body options override the query), so
        nothing is persisted. The response is returned only after its identity
        and UID are checked; server messages are never exposed.
        """
        identity = current.identity
        if current.ownership is not Ownership.MANAGED:
            raise ProviderError("ownership-precondition-failed")
        observed = current.manifest["metadata"]
        metadata = manifest.get("metadata", {})
        if metadata.get("uid") != observed.get("uid") or metadata.get(
            "resourceVersion"
        ) != observed.get("resourceVersion"):
            raise ProviderError("uid-version-precondition-failed")
        raw = self._request(
            "PATCH",
            self._path(self.api_for(identity, deadline=deadline), identity.name),
            body=manifest,
            query={"fieldManager": self.field_manager, "dryRun": "All"},
            deadline=deadline,
            merge=True,
        )
        returned = raw.get("metadata")
        if (
            not isinstance(returned, dict)
            or (
                raw.get("apiVersion"),
                raw.get("kind"),
                returned.get("name"),
                returned.get("namespace", ""),
            )
            != (identity.api_version, identity.kind, identity.name, identity.namespace)
            or returned.get("uid", observed.get("uid")) != observed.get("uid")
        ):
            raise ProviderError("invalid-dry-run-response")
        return raw

    def take_over(
        self,
        current: DiscoveredResource,
        manifest: dict[str, Any],
        *,
        transferred_managers: tuple[str, ...],
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> tuple[DiscoveredResource | None, tuple[str, ...]]:
        """Adopt a non-retained object by transferring field ownership.

        The executor calls this solely for an ADOPT action that the plan marked
        ``takeover`` and the caller's grant authorizes. This method re-checks
        what it can see: never a retained kind or a ``piceli.io/retained``
        object, the exact UID/resourceVersion observed, and a manifest that
        claims this provider's owner id.

        ``dry_run`` sends the admission check: a ``dryRun=All`` server-side
        apply with ``force=true``. It is the only request Piceli ever sends
        with ``force=true``, and it persists nothing; a forced apply cannot
        be used for the real write because it leaves foreign fields in place.
        The real write is :meth:`converge_takeover`.
        """
        identity = current.identity
        if identity.kind in RETAINED_KINDS or current.retained:
            raise ProviderError("retained-resource")
        metadata = manifest.get("metadata", {})
        observed = current.manifest["metadata"]
        if metadata.get("uid") != observed.get("uid") or metadata.get(
            "resourceVersion"
        ) != observed.get("resourceVersion"):
            raise ProviderError("uid-version-precondition-failed")
        annotations = metadata.get("annotations") or {}
        operation_id = annotations.get(OPERATION_ANNOTATION)
        if annotations.get(OWNER_ANNOTATION) != self.owner_id or not operation_id:
            raise ProviderError("ownership-precondition-failed")
        if dry_run:
            self._write(
                identity,
                manifest,
                create=False,
                dry_run=True,
                deadline=deadline,
                force=True,
            )
            return None, ()
        return self.converge_takeover(
            current,
            manifest,
            transferred_managers=transferred_managers,
            deadline=deadline,
        )

    def converge_takeover(
        self,
        current: DiscoveredResource,
        manifest: dict[str, Any],
        *,
        transferred_managers: tuple[str, ...],
        deadline: float | None = None,
        attempts: int = 5,
    ) -> tuple[DiscoveredResource, tuple[str, ...]]:
        """Make ``manifest`` the object's full desired state (idempotent).

        1. **Transfer.** A merge patch replaces ``managedFields``: the entries
           of the planned transferable managers (and this manager's own
           Update entries) become one Apply entry of this field manager whose
           field set is their union. Subresource and control-plane entries
           are kept unchanged. The patch carries the observed resourceVersion.
        2. **Apply** the manifest with ``force=false``. This manager now owns
           every client-written field, so the server removes each one the
           manifest does not declare (a container another tool added, the
           ``kubectl.kubernetes.io/last-applied-configuration`` annotation,
           ...). A remaining conflict is with a kept manager (for example an
           autoscaler's ``scale`` entry) and fails as a real conflict.
        3. **Verify** the object contains the manifest and no transferable
           foreign manager is left. An unowned leftover
           ``last-applied-configuration`` annotation is removed.

        A concurrent write makes a step fail its resourceVersion precondition;
        the object is then re-read and the steps repeat. A transferable
        manager that was not planned (someone else wrote in between) stops
        the takeover. Resuming an interrupted takeover calls this again.
        """
        identity = current.identity
        uid = current.manifest["metadata"].get("uid")
        if identity.kind in RETAINED_KINDS or current.retained:
            raise ProviderError("retained-resource")
        if manifest.get("metadata", {}).get("uid") != uid:
            raise ProviderError("uid-version-precondition-failed")
        planned = set(transferred_managers) - {self.field_manager}
        desired = ResourceIntent.from_manifest(manifest).manifest
        declares_last_applied = LAST_APPLIED_ANNOTATION in (
            manifest.get("metadata", {}).get("annotations") or {}
        )
        transferred: set[str] = set()
        wrote = False
        path = self._path(self.api_for(identity, deadline=deadline), identity.name)

        def refresh() -> DiscoveredResource:
            value = self.get(identity, deadline=deadline)
            if value is None or value.manifest["metadata"].get("uid") != uid:
                raise ProviderError("recreated-object", ambiguous=True)
            return value

        for _ in range(attempts):
            metadata = current.manifest["metadata"]
            entries = metadata.get("managedFields") or []
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict) for entry in entries
            ):
                raise ProviderError("invalid-field-ownership-evidence", ambiguous=wrote)
            parsed = field_manager_entries(current.manifest)
            unplanned = {
                entry.manager
                for entry in parsed
                if is_transferable(entry)
                and entry.manager not in planned | {self.field_manager}
            }
            if unplanned:
                raise ProviderError("field-owner-precondition-failed", ambiguous=wrote)
            fold = [
                entry
                for entry in entries
                if is_transferable(_entry(entry))
                and (
                    entry.get("manager") in planned
                    or entry.get("manager") == self.field_manager
                )
            ]
            if not (
                len(fold) == 1
                and fold[0].get("manager") == self.field_manager
                and fold[0].get("operation") == "Apply"
            ):
                union: dict[str, Any] = {}
                for entry in fold:
                    _merge_fields(union, entry.get("fieldsV1") or {})
                kept = [entry for entry in entries if entry not in fold]
                kept.append(
                    {
                        "manager": self.field_manager,
                        "operation": "Apply",
                        "apiVersion": manifest["apiVersion"],
                        "fieldsType": "FieldsV1",
                        "fieldsV1": union,
                    }
                )
                try:
                    raw = self._request(
                        "PATCH",
                        path,
                        body={
                            "metadata": {
                                "uid": uid,
                                "resourceVersion": metadata["resourceVersion"],
                                "managedFields": kept,
                            }
                        },
                        query={"fieldManager": self.field_manager},
                        deadline=deadline,
                        merge=True,
                    )
                except ProviderError as error:
                    if error.status != 409 or error.ambiguous:
                        raise ProviderError(
                            error.category, status=error.status, ambiguous=wrote
                        ) from None
                    current = refresh()
                    continue
                wrote = True
                transferred |= {
                    str(entry.get("manager"))
                    for entry in fold
                    if entry.get("manager") != self.field_manager
                }
                current = self._checked(raw, identity, uid)
                metadata = current.manifest["metadata"]
            body = json.loads(json.dumps(manifest))
            body["metadata"]["resourceVersion"] = metadata["resourceVersion"]
            try:
                applied = self._write(
                    identity,
                    body,
                    create=False,
                    dry_run=False,
                    deadline=deadline,
                    force=False,
                )
            except ProviderError as error:
                if error.status != 409:
                    raise ProviderError(
                        error.category,
                        status=error.status,
                        ambiguous=error.ambiguous or wrote,
                    ) from None
                refreshed = refresh()
                if (
                    refreshed.manifest["metadata"].get("resourceVersion")
                    == metadata["resourceVersion"]
                ):
                    # Unchanged object: a real field conflict with a kept
                    # manager (for example an autoscaler), not a race.
                    raise ProviderError(
                        "conflict", status=409, ambiguous=wrote
                    ) from None
                current = refreshed
                continue
            wrote = True
            assert applied is not None
            current = applied
            annotations = current.manifest["metadata"].get("annotations") or {}
            if LAST_APPLIED_ANNOTATION in annotations and not declares_last_applied:
                try:
                    raw = self._request(
                        "PATCH",
                        path,
                        body={
                            "metadata": {
                                "uid": uid,
                                "resourceVersion": current.manifest["metadata"][
                                    "resourceVersion"
                                ],
                                "annotations": {LAST_APPLIED_ANNOTATION: None},
                            }
                        },
                        query={"fieldManager": self.field_manager},
                        deadline=deadline,
                        merge=True,
                    )
                except ProviderError as error:
                    if error.status != 409 or error.ambiguous:
                        raise ProviderError(
                            error.category, status=error.status, ambiguous=True
                        ) from None
                    current = refresh()
                    continue
                current = self._checked(raw, identity, uid)
            leftover = transferable_managers(
                field_manager_entries(current.manifest), exclude=(self.field_manager,)
            )
            if leftover:
                current = refresh()
                continue
            if current.ownership is not Ownership.MANAGED or not manifest_contains(
                ResourceIntent.from_manifest(current.manifest).manifest, desired
            ):
                raise ProviderError(
                    "resource-content-precondition-failed", ambiguous=True
                )
            # Every planned manager's fields now belong to this manager
            # (including those transferred by an interrupted earlier attempt).
            return current, tuple(sorted(planned | transferred))
        raise ProviderError("takeover-conflict", ambiguous=True)

    def _checked(
        self, raw: dict[str, Any], identity: ResourceIdentity, uid: Any
    ) -> DiscoveredResource:
        try:
            result = self._resource(raw, self.api_for(identity))
        except (ValueError, ProviderError):
            raise ProviderError("invalid-write-response", ambiguous=True) from None
        if result.identity != identity or result.manifest["metadata"].get("uid") != uid:
            raise ProviderError("invalid-write-response", ambiguous=True)
        return result

    def adopt_metadata(
        self,
        current: DiscoveredResource,
        *,
        operation_id: str,
        dry_run: bool = False,
        deadline: float | None = None,
        labels: dict[str, Any] | None = None,
        annotations: dict[str, Any] | None = None,
    ) -> DiscoveredResource | None:
        """Metadata-only write of a retained object (ownership and metadata).

        The merge patch carries the owner and operation annotations, the
        given ``labels``/``annotations`` (set, never removed) and the observed
        UID and resourceVersion as preconditions, so spec, data and every
        other field are never part of the request. The response is checked to
        differ from the observed object in exactly those keys.
        """
        identity = current.identity
        if not current.retained:
            raise ProviderError("not-retained")
        observed = current.manifest["metadata"]
        text(operation_id, "operation id")
        labels = dict(labels or {})
        annotations = dict(annotations or {})
        if (
            any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in (*labels.items(), *annotations.items())
            )
            or {OWNER_ANNOTATION, OPERATION_ANNOTATION} & annotations.keys()
        ):
            raise ProviderError("invalid-metadata-change")
        patch: dict[str, Any] = {
            "uid": observed["uid"],
            "resourceVersion": observed["resourceVersion"],
            "annotations": annotations
            | {OWNER_ANNOTATION: self.owner_id, OPERATION_ANNOTATION: operation_id},
        }
        if labels:
            patch["labels"] = labels
        expected = json.loads(json.dumps(current.manifest))
        for section, values in (("labels", labels), ("annotations", annotations)):
            if values:
                expected["metadata"].setdefault(section, {}).update(values)
        raw = self._request(
            "PATCH",
            self._path(self.api_for(identity, deadline=deadline), identity.name),
            body={"metadata": patch},
            query={"fieldManager": self.field_manager}
            | ({"dryRun": "All"} if dry_run else {}),
            deadline=deadline,
            merge=True,
        )
        if dry_run:
            if raw.get("metadata", {}).get("name") != identity.name:
                raise ProviderError("invalid-write-response")
            return None
        try:
            result = self._resource(raw, self.api_for(identity, deadline=deadline))
        except (ValueError, ProviderError):
            raise ProviderError("invalid-write-response", ambiguous=True) from None
        returned = result.manifest["metadata"].get("annotations", {})
        if (
            result.identity != identity
            or result.manifest["metadata"].get("uid") != observed["uid"]
            or result.ownership is not Ownership.MANAGED
            or returned.get(OWNER_ANNOTATION) != self.owner_id
            or returned.get(OPERATION_ANNOTATION) != operation_id
            or _without_ownership(result.manifest) != _without_ownership(expected)
        ):
            raise ProviderError("invalid-write-response", ambiguous=True)
        return result

    def delete(
        self,
        identity: ResourceIdentity,
        *,
        uid: str,
        resource_version: str,
        deadline: float | None = None,
        propagation: str = "Orphan",
        dry_run: bool = False,
    ) -> None:
        """Delete with UID and resourceVersion preconditions (never a retained kind).

        ``propagation`` is ``Orphan`` (dependents are kept) unless a replace
        chooses ``Background`` for a workload controller (see
        :func:`piceli.k8s.ops.plan.replace_propagation`).
        """
        if identity.kind in RETAINED_KINDS:
            raise ProviderError("retained-resource")
        if propagation not in {"Orphan", "Background"}:
            raise ProviderError("invalid-propagation")
        text(uid, "uid")
        text(resource_version, "resource version")
        body: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": propagation,
            "preconditions": {"uid": uid, "resourceVersion": resource_version},
        }
        if dry_run:
            # The API server reads DeleteOptions from the body and ignores
            # the query string when a body is sent: dryRun must be in both
            # (the query marks the request as non-mutating here).
            body["dryRun"] = ["All"]
        self._request(
            "DELETE",
            self._path(self.api_for(identity, deadline=deadline), identity.name),
            body=body,
            query={"dryRun": "All"} if dry_run else None,
            deadline=deadline,
        )

    def probe_server_side_apply(
        self, target: PlanTarget, resource: DiscoveredResource
    ) -> ApplyProbeResult:
        self._target(target)
        try:
            self.write(resource.identity, resource.manifest, create=False, dry_run=True)
            status = ApplyProbeStatus.ACCEPTED
        except ProviderError as error:
            status = {
                409: ApplyProbeStatus.CONFLICT,
                401: ApplyProbeStatus.DENIED,
                403: ApplyProbeStatus.DENIED,
            }.get(error.status, ApplyProbeStatus.UNSUPPORTED)
        return ApplyProbeResult(resource.identity, status)

    def readiness(self, resource: DiscoveredResource) -> ReadinessProbeResult:
        manifest = resource.manifest
        kind = resource.identity.kind
        metadata = manifest["metadata"]
        status = manifest.get("status", {})
        generation = metadata.get("generation", 0)
        observed = status.get("observedGeneration")
        ready = False
        if metadata.get("deletionTimestamp"):
            ready = False
        elif kind in {
            "ConfigMap",
            "Secret",
            "ServiceAccount",
            "Role",
            "RoleBinding",
            "ClusterRole",
            "ClusterRoleBinding",
            "NetworkPolicy",
            "CronJob",
        }:
            ready = True
        elif kind in {"Deployment", "StatefulSet", "DaemonSet"}:
            desired = manifest.get("spec", {}).get("replicas", 1)
            fresh = (
                isinstance(observed, int)
                and not isinstance(observed, bool)
                and isinstance(generation, int)
                and not isinstance(generation, bool)
                and observed >= generation
            )
            if kind == "DaemonSet":
                desired = status.get("desiredNumberScheduled", 0)
                ready = (
                    fresh
                    and desired > 0
                    and status.get("numberReady", 0) >= desired
                    and status.get("updatedNumberScheduled", 0) >= desired
                )
            else:
                ready = (
                    fresh
                    and status.get("readyReplicas", 0) >= desired
                    and status.get("updatedReplicas", 0) >= desired
                    and status.get("replicas", 0) == desired
                )
        elif kind == "Job":
            ready = any(
                c.get("type") == "Complete" and c.get("status") == "True"
                for c in status.get("conditions", [])
            )
        elif kind == "Pod":
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", [])
            )
        elif kind == "PersistentVolume":
            ready = status.get("phase") == "Bound"
        elif kind == "PersistentVolumeClaim":
            # The executor may defer this check while it submits a declared
            # first consumer, but a PVC itself is never ready until bound.
            ready = status.get("phase") == "Bound"
        elif kind == "Namespace":
            ready = status.get("phase") == "Active"
        elif kind == "CustomResourceDefinition":
            ready = any(
                c.get("type") == "Established" and c.get("status") == "True"
                for c in status.get("conditions", [])
            )
        elif kind == "Service":
            spec = manifest.get("spec", {})
            ready = (
                bool(spec.get("externalName"))
                if spec.get("type") == "ExternalName"
                else bool(spec.get("clusterIP"))
            )
            if spec.get("type") == "LoadBalancer":
                ready = ready and bool(status.get("loadBalancer", {}).get("ingress"))
        else:
            return ReadinessProbeResult(resource.identity, ReadinessStatus.UNSUPPORTED)
        return ReadinessProbeResult(
            resource.identity,
            ReadinessStatus.READY if ready else ReadinessStatus.NOT_READY,
            observed,
        )

    def probe_readiness(
        self, target: PlanTarget, resource: ResourceIdentity
    ) -> ReadinessProbeResult:
        self._target(target)
        try:
            current = self.get(resource)
            return (
                self.readiness(current)
                if current
                else ReadinessProbeResult(resource, ReadinessStatus.NOT_READY)
            )
        except ProviderError as error:
            return ReadinessProbeResult(
                resource,
                ReadinessStatus.DENIED
                if error.status in {401, 403}
                else ReadinessStatus.UNSUPPORTED,
            )
