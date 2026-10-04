"""Several clusters from one GitOps controller (0.15): credentials, reach, images.

A composition declares several :class:`~piceli.infra.Cluster` s; the one with
``Controller(...)`` runs the controller (the *home* cluster) and the others
are reached through their API servers, which may be private-network
addresses. This module holds what the controller and the CLI need for them:

- **Credentials.** The controller reads each other cluster's kubeconfig from
  the Secret ``piceli-cluster-<name>`` (key ``kubeconfig``) in its own
  namespace: one context, certificates and token inlined, never an exec
  plugin, the server the controller uses. :func:`credential_kubeconfig`
  builds it from a kubeconfig file (or :func:`token_kubeconfig` from a bearer
  token); ``piceli secrets cluster`` and :func:`register_cluster` write it
  (:func:`write_cluster_secret`). It is never printed and never in Git.
- **Reach.** :class:`RemoteClusters` writes each Secret's kubeconfig into the
  controller's private directory (mode ``0600``, removed when it exits) and
  probes the API (``GET /version``, short timeout): an unreachable cluster is
  reported (``cluster-unreachable``, the last contact) and retried, never
  blocking the others.
- **Images.** Builds stay on the home cluster. The controller copies each
  image a cluster runs, by digest, from the home registry into that
  cluster's own in-cluster registry (``Registry.in_cluster`` on it), through
  that cluster's API server (the Service proxy, TLS and its credentials:
  :class:`ProxiedRegistryClient`), so no registry is exposed and a remote
  cluster never pulls from the home one.
- **Removal.** :func:`remove_placement` deletes what Piceli created for an
  environment in a cluster it no longer runs on (see its docstring).

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryError,
    Response,
    StreamedOciRegistryClient,
)
from piceli.infra import Cluster, CompositionError

#: The Secret of a cluster's credentials (in the controller's namespace).
SECRET_PREFIX = "piceli-cluster-"
SECRET_KEY = "kubeconfig"
#: The context name inside a credential kubeconfig.
CONTEXT = "piceli-remote"
#: Seconds a probe of a cluster's API waits.
PROBE_SECONDS = 5.0
#: Label on the credential Secrets.
COMPONENT = "cluster-credentials"
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_REF = re.compile(r"(?P<host>[^/]+)/(?P<repo>[^@]+)@(?P<digest>sha256:[0-9a-f]{64})")


def secret_name(cluster: str) -> str:
    """``piceli-cluster-<name>``: the Secret holding ``cluster``'s kubeconfig."""
    return f"{SECRET_PREFIX}{cluster}"[:253]


def _refused(message: str) -> CompositionError:
    return CompositionError("cluster-credentials-unsupported", message)


# ---------------------------------------------------------------- credentials


def _named(document: Mapping[str, Any], kind: str, name: str) -> dict[str, Any]:
    for item in document.get(kind) or ():
        if isinstance(item, Mapping) and item.get("name") == name:
            key = kind[:-1]
            found = item.get(key)
            return dict(found) if isinstance(found, Mapping) else {}
    raise _refused(f"the kubeconfig has no {kind[:-1]} named {name!r}")


def _inline(entry: dict[str, Any], key: str, base: Path | None) -> None:
    """``key`` (a file path) becomes ``key-data`` (its base64 contents)."""
    path = entry.pop(key, None)
    if path is None or f"{key}-data" in entry:
        return
    file = Path(str(path))
    if not file.is_absolute() and base is not None:
        file = base / file
    try:
        data = file.read_bytes()
    except OSError:
        raise _refused(f"the kubeconfig's {key} file is unreadable") from None
    entry[f"{key}-data"] = base64.b64encode(data).decode()


def credential_kubeconfig(
    document: Mapping[str, Any],
    context: str,
    *,
    server: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """A one-context kubeconfig for the controller, from ``context`` of ``document``.

    The user's certificates and token are inlined (files read relative to
    ``base_dir``); ``server`` replaces the cluster's server (the address the
    controller uses). An exec plugin, an auth provider, a token file or a
    user name and password are refused (``cluster-credentials-unsupported``):
    the controller runs no plugin and holds no file of its own.
    """
    ctx = _named(document, "contexts", context)
    cluster = _named(document, "clusters", str(ctx.get("cluster")))
    user = _named(document, "users", str(ctx.get("user")))
    for key in ("exec", "auth-provider", "tokenFile", "username", "password"):
        if key in user:
            raise _refused(
                f"the kubeconfig user uses {key}; the controller needs a token or a "
                "client certificate"
            )
    if cluster.get("insecure-skip-tls-verify"):
        raise _refused("the kubeconfig skips TLS verification")
    _inline(cluster, "certificate-authority", base_dir)
    _inline(user, "client-certificate", base_dir)
    _inline(user, "client-key", base_dir)
    if not (user.get("token") or user.get("client-certificate-data")):
        raise _refused("the kubeconfig user has no token and no client certificate")
    allowed_cluster = {"certificate-authority-data", "tls-server-name"}
    allowed_user = {"token", "client-certificate-data", "client-key-data"}
    return _document(
        {k: v for k, v in cluster.items() if k in allowed_cluster} | {"server": server},
        {k: v for k, v in user.items() if k in allowed_user},
    )


def token_kubeconfig(server: str, token: str, ca_data: str | None) -> dict[str, Any]:
    """A one-context kubeconfig of a bearer token (``secrets cluster --prompt``)."""
    if not token or any(c in token for c in "\r\n\0 "):
        raise _refused("no usable token was read")
    cluster: dict[str, Any] = {"server": server}
    if ca_data:
        cluster["certificate-authority-data"] = ca_data
    return _document(cluster, {"token": token})


def _document(cluster: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": CONTEXT, "cluster": cluster}],
        "users": [{"name": CONTEXT, "user": user}],
        "contexts": [
            {"name": CONTEXT, "context": {"cluster": CONTEXT, "user": CONTEXT}}
        ],
        "current-context": CONTEXT,
    }


def profile_ca(
    document: Mapping[str, Any], context: str, base: Path | None
) -> str | None:
    """The CA (base64) of ``context``'s cluster in a profile's kubeconfig."""
    try:
        ctx = _named(document, "contexts", context)
        cluster = _named(document, "clusters", str(ctx.get("cluster")))
    except CompositionError:
        return None
    _inline(cluster, "certificate-authority", base)
    found = cluster.get("certificate-authority-data")
    return found if isinstance(found, str) else None


def write_cluster_secret(
    api: Any, cluster: str, document: Mapping[str, Any], *, namespace: str
) -> str:
    """Create or update ``piceli-cluster-<cluster>``; ``created`` or ``updated``."""
    import json

    name = secret_name(cluster)
    data = {
        SECRET_KEY: base64.b64encode(json.dumps(document).encode()).decode(),
    }
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        "piceli.io/component": COMPONENT,
        "piceli.io/cluster": cluster,
    }
    path = f"/api/v1/namespaces/{namespace}/secrets"
    live = api.call(f"{path}/{name}", "GET")
    if live is None:
        api.call(
            path,
            "POST",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "type": "Opaque",
                "data": data,
            },
            missing_ok=False,
        )
        return "created"
    api.call(
        f"{path}/{name}",
        "PATCH",
        {
            "metadata": {
                "uid": live["metadata"]["uid"],
                "resourceVersion": live["metadata"]["resourceVersion"],
                "labels": labels,
            },
            "data": data,
        },
        "application/merge-patch+json",
        missing_ok=False,
    )
    return "updated"


def register_cluster(
    cluster: Cluster,
    *,
    kubeconfig: Path,
    context: str,
    home: Cluster,
    server: str | None = None,
    transport: str = "https",
    namespace: str = "piceli-system",
) -> dict[str, Any]:
    """Register a (newly provisioned) cluster with the composition's controller.

    Saves the local credential profile ``cluster.credentials`` (like
    ``piceli login``: a reference to ``kubeconfig`` and ``context``, never a
    copy) and writes the controller's Secret ``piceli-cluster-<name>`` on the
    ``home`` cluster (reached through its own profile, checked against its
    ``api``), with the server ``server`` (default ``cluster.api``). Prints
    nothing; returns names only. ``piceli cluster init`` of the new cluster
    (a plan and an approval) stays the owner's next step.

    :raises CompositionError: ``cluster-credentials-unsupported``.
    :raises piceli.infra.cluster.ClusterError: ``cluster-api-mismatch`` and
        the profile codes.
    """
    from piceli.infra.cluster import ClusterError, api_matches, kubeconfig_server
    from piceli.k8s.ops.provider_factory import _read_kubeconfig
    from piceli.profiles import save_profile

    if not isinstance(cluster, Cluster) or not isinstance(home, Cluster):
        raise CompositionError("cluster-invalid", "cluster and home are Clusters")
    if cluster.name == home.name:
        raise CompositionError(
            "cluster-invalid", "the home cluster needs no credential Secret"
        )
    document = _read_kubeconfig(kubeconfig.absolute())
    found = kubeconfig_server(document, context)
    if found is None or not api_matches(cluster.api, found):
        raise ClusterError(
            "cluster-api-mismatch",
            f"the kubeconfig's context does not point at Cluster(api={cluster.api!r})",
        )
    secret = credential_kubeconfig(
        document,
        context,
        server=server or cluster.api,
        base_dir=kubeconfig.absolute().parent,
    )
    save_profile(cluster.credentials, kubeconfig.absolute(), context)
    with home_api(home, transport) as api:
        state = write_cluster_secret(api, cluster.name, secret, namespace=namespace)
    return {
        "state": state,
        "cluster": cluster.name,
        "profile": cluster.credentials,
        "secret": {
            "namespace": namespace,
            "name": secret_name(cluster.name),
            "keys": [SECRET_KEY],
        },
        "next": [f"piceli cluster init MODULE:ATTR  # the {cluster.name} Cluster"],
    }


def home_api(home: Cluster, transport: str) -> Any:
    """An :class:`~piceli.gitops.install.Api` on ``home`` through its profile."""
    from contextlib import contextmanager

    from piceli.gitops.install import Api
    from piceli.infra.cluster import ClusterError, api_matches, kubeconfig_server
    from piceli.k8s.ops.provider_factory import (
        _read_kubeconfig,
        api_client_from_kubeconfig,
    )
    from piceli.profiles import resolve

    @contextmanager
    def opened() -> Iterator[Any]:
        found = resolve(home.credentials)
        if not found.in_cluster:
            server = kubeconfig_server(
                _read_kubeconfig(found.kubeconfig), found.context
            )
            if server is None or not api_matches(home.api, server):
                raise ClusterError(
                    "cluster-api-mismatch",
                    f"profile {home.credentials!r} does not point at "
                    f"Cluster(api={home.api!r})",
                )
        client = api_client_from_kubeconfig(
            found.kubeconfig.absolute(),
            found.context,
            transport=transport,  # type: ignore[arg-type]
        )
        api = Api(client, field_manager="piceli-cluster")
        try:
            yield api
        finally:
            api.close()

    return opened()


# ---------------------------------------------------------------- the controller


@dataclass
class Contact:
    """What the controller knows of reaching one cluster."""

    reachable: bool
    reason: str | None = None
    checked_at: float = 0.0
    last_contact: float | None = None


class RemoteClusters:
    """The other clusters as the controller reaches them (credentials, probes).

    :param api: An :class:`~piceli.gitops.install.Api` on the home cluster
        (reads the credential Secrets).
    :param namespace: The controller's namespace (where the Secrets are).
    :param private_dir: The controller's private directory: each cluster's
        kubeconfig is written there (``0600``) and goes with it.
    """

    def __init__(
        self,
        api: Any,
        *,
        namespace: str,
        private_dir: Path,
        transport: str = "https",
        probe_seconds: float = PROBE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self.namespace = namespace
        self.directory = private_dir / "clusters"
        self.transport = transport
        self.probe_seconds = probe_seconds
        self.clock = clock
        self.contacts: dict[str, Contact] = {}

    def kubeconfig(self, cluster: str) -> tuple[Path, str]:
        """``(file, context)`` of ``cluster`` from its Secret (read each time).

        :raises CompositionError: ``cluster-credentials-missing``.
        """
        import json

        if not _NAME.fullmatch(cluster):
            raise CompositionError("cluster-invalid", "invalid cluster name")
        try:
            found = self.api.call(
                f"/api/v1/namespaces/{self.namespace}/secrets/{secret_name(cluster)}",
                "GET",
            )
        except Exception:
            raise CompositionError(
                "cluster-credentials-missing",
                f"the credentials of cluster {cluster!r} could not be read",
            ) from None
        encoded = ((found or {}).get("data") or {}).get(SECRET_KEY)
        if not isinstance(encoded, str):
            raise CompositionError(
                "cluster-credentials-missing",
                f"no Secret {secret_name(cluster)} with a kubeconfig: run piceli "
                "secrets cluster --cluster MODULE:ATTR",
            )
        try:
            document = json.loads(base64.b64decode(encoded))
        except ValueError:
            raise CompositionError(
                "cluster-credentials-missing",
                f"the Secret {secret_name(cluster)} holds no kubeconfig",
            ) from None
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{cluster}.kubeconfig"
        descriptor = os.open(
            path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle)
        os.replace(path.with_suffix(".tmp"), path)
        return path, CONTEXT

    def client(self, cluster: str) -> Any:
        from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

        path, context = self.kubeconfig(cluster)
        return api_client_from_kubeconfig(path, context, transport=self.transport)  # type: ignore[arg-type]

    def probe(self, cluster: str) -> Contact:
        """``GET /version`` of ``cluster``'s API (short timeout); remembered."""
        before = self.contacts.get(cluster)
        last = before.last_contact if before is not None else None
        now = self.clock()
        try:
            client = self.client(cluster)
        except CompositionError as error:
            contact = Contact(False, error.code, now, last)
        except Exception:
            contact = Contact(False, "cluster-credentials-unsupported", now, last)
        else:
            try:
                client.call_api(
                    "/version",
                    "GET",
                    header_params={"Accept": "application/json"},
                    auth_settings=["BearerToken"],
                    _preload_content=False,
                    _request_timeout=self.probe_seconds,
                )
                contact = Contact(True, None, now, now)
            except Exception as error:
                status = getattr(error, "status", None)
                if status in {401, 403}:
                    refused = CompositionError(
                        "cluster-credentials-refused", "the API refused them"
                    )
                    contact = Contact(False, refused.code, now, last)
                elif isinstance(status, int) and status > 0:
                    contact = Contact(True, None, now, now)  # it answered
                else:
                    contact = Contact(False, "cluster-unreachable", now, last)
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        self.contacts[cluster] = contact
        return contact


# ---------------------------------------------------------------- images


class ProxiedRegistryClient(StreamedOciRegistryClient):
    """An OCI registry client reaching an in-cluster registry through its API server.

    Every request goes to ``/api/v1/namespaces/<ns>/services/<name>:<port>/proxy``
    of the cluster's API server, with the cluster's credentials (TLS, token
    or client certificate): the registry is never exposed. A ``Location`` a
    registry returns (any origin) is resolved as a path behind the proxy.
    """

    def __init__(self, client: Any, registry: Any, *, timeout: float = 300.0) -> None:
        super().__init__(
            RegistryEndpoint(
                host=f"{registry.name}.{registry.namespace}.svc",
                port=int(registry.port),
                use_tls=False,
                timeout=timeout,
            )
        )
        self.client = client
        self.prefix = (
            f"/api/v1/namespaces/{registry.namespace}/services/"
            f"{registry.name}:{int(registry.port)}/proxy"
        )

    def _target(self, path: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(path)
        found = parts.path or "/"
        if found.startswith(self.prefix):
            found = found[len(self.prefix) :] or "/"
        return found + ("?" + parts.query if parts.query else "")

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | Iterator[bytes] | None = None,
        length: int | None = None,
        headers: Mapping[str, str] | None = None,
        authorize: bool = True,
        max_body: int = 64 * 1024 * 1024,
    ) -> Response:
        import urllib3

        data = body if body is None or isinstance(body, bytes) else b"".join(body)
        sent = {
            k: v for k, v in (headers or {}).items() if k.lower() != "authorization"
        }
        if data is not None or method in {"PUT", "PATCH", "POST"}:
            sent["Content-Length"] = str(len(data or b""))
        self.client.update_params_for_auth(sent, [], ["BearerToken"])
        url = (
            self.client.configuration.host.rstrip("/")
            + self.prefix
            + self._target(path)
        )
        try:
            response = self.client.rest_client.pool_manager.request(
                method,
                url,
                body=data,
                headers=sent,
                redirect=False,
                retries=False,
                preload_content=False,
                timeout=urllib3.Timeout(connect=10.0, read=self.endpoint.timeout),
            )
        except (OSError, urllib3.exceptions.HTTPError) as error:
            raise RegistryError("registry-unreachable") from error
        try:
            payload = b"" if method == "HEAD" else response.read(max_body + 1)
        finally:
            response.release_conn()
        if len(payload) > max_body:
            raise RegistryError("registry-response-too-large")
        return Response(
            int(response.status),
            {key.lower(): value for key, value in response.headers.items()},
            payload,
        )


def parse_ref(reference: str) -> tuple[str, str, str] | None:
    """``(host, repository, digest)`` of ``host/repo@sha256:…``, else ``None``."""
    found = _REF.fullmatch(reference or "")
    if found is None:
        return None
    return found.group("host"), found.group("repo"), found.group("digest")


def copy_images(
    references: Mapping[str, str],
    *,
    home_host: str,
    target_host: str,
    source: StreamedOciRegistryClient,
    target: StreamedOciRegistryClient,
    repositories: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy every reference in the home registry to the target registry by digest.

    Returns the references the target cluster's nodes pull
    (``<target_host>/<repository>@<digest>``); a reference outside the home
    registry is returned unchanged. ``repositories`` maps a key to another
    target repository (a mirrored image under the target registry's prefix).

    :raises CompositionError: ``cluster-registry-copy-failed``.
    """
    from piceli.artifacts.mirror import MirrorError, MirrorSource, mirror_image

    copied: dict[str, str] = {}
    for key, reference in sorted(references.items()):
        parsed = parse_ref(reference)
        if parsed is None or parsed[0] != home_host:
            copied[key] = reference
            continue
        _, repository, digest = parsed
        wanted = (repositories or {}).get(key) or repository
        try:
            image = MirrorSource.parse(reference)
        except MirrorError:
            raise CompositionError(
                "cluster-registry-copy-failed", f"cannot read reference of {key!r}"
            ) from None
        receipt = mirror_image(
            image,
            source,
            target,
            wanted,
            platform=None,
            target_registry=target_host,
            node_registry=target_host,
        )
        if receipt.get("state") != "succeeded":
            raise CompositionError(
                "cluster-registry-copy-failed",
                f"copying {key!r} into the cluster's registry failed "
                f"({receipt.get('reason')})",
            )
        copied[key] = f"{target_host}/{wanted}@{digest}"
    return copied


# ---------------------------------------------------------------- removal

#: Namespaced kinds an environment's app creates (path, kind), deleted on removal.
REMOVED_KINDS: tuple[tuple[str, str], ...] = (
    ("/apis/apps/v1/namespaces/{ns}/deployments", "Deployment"),
    ("/apis/apps/v1/namespaces/{ns}/statefulsets", "StatefulSet"),
    ("/apis/apps/v1/namespaces/{ns}/daemonsets", "DaemonSet"),
    ("/apis/batch/v1/namespaces/{ns}/cronjobs", "CronJob"),
    ("/apis/batch/v1/namespaces/{ns}/jobs", "Job"),
    ("/api/v1/namespaces/{ns}/services", "Service"),
    ("/api/v1/namespaces/{ns}/configmaps", "ConfigMap"),
    ("/api/v1/namespaces/{ns}/serviceaccounts", "ServiceAccount"),
    ("/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies", "NetworkPolicy"),
    ("/apis/networking.k8s.io/v1/namespaces/{ns}/ingresses", "Ingress"),
    ("/apis/policy/v1/namespaces/{ns}/poddisruptionbudgets", "PodDisruptionBudget"),
    ("/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers", "HorizontalPodAutoscaler"),
    ("/apis/rbac.authorization.k8s.io/v1/namespaces/{ns}/rolebindings", "RoleBinding"),
    ("/apis/rbac.authorization.k8s.io/v1/namespaces/{ns}/roles", "Role"),
)  # fmt: skip
#: Kept on removal (data and credentials), listed with their delete command.
KEPT_KINDS: tuple[tuple[str, str], ...] = (
    ("/api/v1/namespaces/{ns}/persistentvolumeclaims", "PersistentVolumeClaim"),
    ("/api/v1/namespaces/{ns}/secrets", "Secret"),
)


def _items(api: Any, path: str, selector: str) -> list[dict[str, Any]]:
    from urllib.parse import quote

    query = f"?labelSelector={quote(selector)}" if selector else ""
    found = api.call(f"{path}{query}", "GET")
    return [item for item in (found or {}).get("items") or [] if isinstance(item, dict)]


def _delete_options(item: Mapping[str, Any]) -> dict[str, Any]:
    """Delete exactly what was read (uid and resourceVersion preconditions)."""
    metadata = item.get("metadata") or {}
    return {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Background",
        "preconditions": {
            "uid": metadata.get("uid"),
            "resourceVersion": metadata.get("resourceVersion"),
        },
    }


def remove_placement(api: Any, *, namespace: str, app: str, env: str) -> dict[str, Any]:
    """Delete what Piceli created for ``env`` (app ``app``) in one cluster.

    Only objects labelled as the app's (``app.kubernetes.io/part-of=<app>``)
    in ``namespace`` are deleted. Claims and Secrets are kept and listed with
    the command deleting each; the namespace is deleted only when Piceli
    created it for this environment (``piceli.io/env-name=<env>``,
    ``app.kubernetes.io/managed-by=piceli``) and nothing is kept in it.
    Returns ``{"deleted": [...], "kept": [...], "namespace": "deleted"|"kept"|"absent"}``.
    """
    from piceli.envs.model import ENV_NAME_LABEL

    live = api.call(f"/api/v1/namespaces/{namespace}", "GET")
    if live is None:
        return {"deleted": [], "kept": [], "namespace": "absent"}
    selector = f"app.kubernetes.io/part-of={app}"
    deleted: list[dict[str, str]] = []
    for path, kind in REMOVED_KINDS:
        base = path.format(ns=namespace)
        for item in _items(api, base, selector):
            name = str((item.get("metadata") or {}).get("name"))
            api.call(f"{base}/{name}", "DELETE", _delete_options(item))
            deleted.append({"kind": kind, "name": name})
    kept: list[dict[str, str]] = []
    for path, kind in KEPT_KINDS:
        base = path.format(ns=namespace)
        # Every claim is kept (data), not only the app's labelled ones.
        found = (
            _items(api, base, "")
            if kind == "PersistentVolumeClaim"
            else _items(api, base, selector)
        )
        for item in found:
            name = str((item.get("metadata") or {}).get("name"))
            kept.append(
                {
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "command": f"kubectl -n {namespace} delete {kind.lower()} {name}",
                }
            )
    labels = (live.get("metadata") or {}).get("labels") or {}
    created = (
        labels.get(ENV_NAME_LABEL) == env
        and labels.get("app.kubernetes.io/managed-by") == "piceli"
    )
    state = "kept"
    if created and not kept:
        api.call(f"/api/v1/namespaces/{namespace}", "DELETE", _delete_options(live))
        state = "deleted"
    return {"deleted": deleted, "kept": kept, "namespace": state}


def cluster_secret_names(clusters: Sequence[str]) -> list[str]:
    """The Secret names the controller may read (one per other cluster)."""
    return sorted(secret_name(name) for name in clusters)
