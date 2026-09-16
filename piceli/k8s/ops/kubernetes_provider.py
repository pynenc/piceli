"""Explicit-client Kubernetes HTTP provider. Never loads ambient configuration."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from typing import Any
from urllib.parse import quote, urlsplit

from piceli.k8s.ops.bounds import bounded_call, positive, seconds, strict_json, text
from piceli.k8s.ops.discovery import (
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

OWNER_ANNOTATION = "piceli.io/owner"
OPERATION_ANNOTATION = "piceli.io/operation"


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
    ) -> None:
        self.client = api_client
        self.target = target
        self.field_manager = text(field_manager, "field manager")
        self.owner_id = text(owner_id, "owner")
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
        if (
            api_client.configuration.refresh_api_key_hook is not None
            or api_client.configuration.proxy
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

    def _is_managed(self, manifest: dict[str, Any]) -> bool:
        ann = manifest.get("metadata", {}).get("annotations", {})
        owner = ann.get(OWNER_ANNOTATION)
        if not owner or not self.owner_id:
            return False
        if owner == self.owner_id:
            return True
        # Allow release/version variations belonging to the same app deployment root (e.g. ih-v18 vs ih-r05)
        base_current = self.owner_id.split("-", 1)[0]
        base_observed = owner.split("-", 1)[0]
        if base_current and base_current == base_observed:
            return True
        return False

    def _resource(
        self, manifest: dict[str, Any], api: ApiResource
    ) -> DiscoveredResource:
        resource = DiscoveredResource.from_manifest(
            manifest,
            scope=api.scope,
            ownership=Ownership.MANAGED
            if self._is_managed(manifest)
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

    def write(
        self,
        identity: ResourceIdentity,
        manifest: dict[str, Any],
        *,
        create: bool,
        dry_run: bool = False,
        deadline: float | None = None,
    ) -> DiscoveredResource | None:
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
            query["force"] = "false"
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

    def delete(
        self,
        identity: ResourceIdentity,
        *,
        uid: str,
        resource_version: str,
        deadline: float | None = None,
    ) -> None:
        from piceli.k8s.ops.discovery import RETAINED_KINDS

        if identity.kind in RETAINED_KINDS:
            raise ProviderError("retained-resource")
        text(uid, "uid")
        text(resource_version, "resource version")
        self._request(
            "DELETE",
            self._path(self.api_for(identity, deadline=deadline), identity.name),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "propagationPolicy": "Orphan",
                "preconditions": {"uid": uid, "resourceVersion": resource_version},
            },
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
