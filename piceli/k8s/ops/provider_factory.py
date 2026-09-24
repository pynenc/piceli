"""Build a :class:`KubernetesProvider` from an explicit kubeconfig file and context.

This is the seed of the kubeconfig-to-provider factory. The authority model
stays explicit:

* the kubeconfig is a named file; ``KUBECONFIG``, ``~/.kube/config`` and
  in-cluster configuration are never consulted;
* the context is named; the file's ``current-context`` is never used;
* the cluster is identified by the ``kube-system`` Namespace UID and the
  target Namespace UID, read from the server and optionally compared with
  expected values;
* credentials must be static (client certificate or bearer token). Exec
  plugins and auth providers need token refresh, which
  :class:`KubernetesProvider` rejects, so they are refused with a clear error
  instead of being silently loaded.

Importing this module has no side effects; the Kubernetes SDK is imported
lazily when a client is built.
"""

from __future__ import annotations

import ipaddress
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from piceli.k8s.ops.discovery import EvidenceSource, PlanTarget
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider

Transport = Literal["https", "loopback-http"]
_UID = re.compile(r"[0-9A-Za-z-]{1,128}")
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_MAX_KUBECONFIG_BYTES = 1_000_000
_STATIC_USER_KEYS = frozenset(
    {
        "client-certificate",
        "client-certificate-data",
        "client-key",
        "client-key-data",
        "token",
    }
)


class ProviderFactoryError(ValueError):
    """The kubeconfig, context or observed cluster identity is not acceptable."""


@dataclass(frozen=True)
class NodeExpectation:
    """A node the caller relies on, optionally pinned to its UID."""

    name: str
    uid: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ProviderFactoryError(f"invalid node name: {self.name!r}")
        if self.uid is not None and not _UID.fullmatch(self.uid):
            raise ProviderFactoryError(f"invalid node uid for {self.name!r}")


@dataclass(frozen=True)
class KubeconfigTarget:
    """Everything needed to reach exactly one namespace of one cluster."""

    kubeconfig: Path
    context: str
    namespace: str
    cluster_uid: str | None = None
    namespace_uid: str | None = None
    transport: Transport = "https"
    request_seconds: float = 10.0
    nodes: Mapping[str, NodeExpectation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.context:
            raise ProviderFactoryError("an explicit kubeconfig context is required")
        PlanTarget("placeholder", self.namespace)  # namespace syntax
        for value in (self.cluster_uid, self.namespace_uid):
            if value is not None and not _UID.fullmatch(value):
                raise ProviderFactoryError("invalid expected cluster identity")
        if self.transport not in ("https", "loopback-http"):
            raise ProviderFactoryError("transport must be https or loopback-http")
        if not 0 < self.request_seconds <= 60:
            raise ProviderFactoryError("request_seconds must be in (0, 60]")


@dataclass(frozen=True)
class ClusterIdentity:
    """Server-observed identity; ``cluster_uid`` is the kube-system Namespace UID."""

    cluster_uid: str
    namespace_uid: str
    nodes: Mapping[str, NodeExpectation] = field(default_factory=dict)

    def plan_target(self, namespace: str) -> PlanTarget:
        return PlanTarget(self.cluster_uid, namespace)


@dataclass
class ProviderBinding:
    """A ready provider plus the identity it was verified against."""

    provider: KubernetesProvider
    identity: ClusterIdentity
    target: PlanTarget

    def close(self) -> None:
        close = getattr(self.provider.client, "close", None)
        if close is not None:
            close()


def _read_kubeconfig(path: Path) -> dict[str, Any]:
    import yaml

    if path.is_symlink():
        path = path.resolve(strict=True)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ProviderFactoryError(f"kubeconfig not found: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ProviderFactoryError("kubeconfig must be a regular file")
    if info.st_size > _MAX_KUBECONFIG_BYTES:
        raise ProviderFactoryError("kubeconfig exceeds 1 MB")
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        raise ProviderFactoryError("kubeconfig is not valid YAML") from None
    if not isinstance(document, dict):
        raise ProviderFactoryError("kubeconfig must be a mapping")
    return document


def _named(document: Mapping[str, Any], section: str, name: str) -> dict[str, Any]:
    entries = document.get(section) or []
    matches = [
        item for item in entries if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ProviderFactoryError(
            f"kubeconfig must define exactly one {section[:-1]} named {name!r}"
        )
    value = matches[0].get(section[:-1])
    if not isinstance(value, dict):
        raise ProviderFactoryError(f"kubeconfig {section[:-1]} {name!r} is invalid")
    return value


def _check_entries(document: Mapping[str, Any], context: str, transport: str) -> None:
    """Refuse credentials and transports the provider cannot use safely."""
    ctx = _named(document, "contexts", context)
    cluster = _named(document, "clusters", str(ctx.get("cluster", "")))
    user = _named(document, "users", str(ctx.get("user", "")))
    if "exec" in user or "auth-provider" in user:
        raise ProviderFactoryError(
            "exec/auth-provider credentials need token refresh, which is not "
            "supported yet; use a static client certificate or token context"
        )
    unsupported = set(user) - _STATIC_USER_KEYS
    if unsupported:
        raise ProviderFactoryError(
            f"unsupported kubeconfig user fields: {sorted(unsupported)}"
        )
    if cluster.get("proxy-url"):
        raise ProviderFactoryError("proxied API transport is not supported")
    if cluster.get("insecure-skip-tls-verify"):
        raise ProviderFactoryError("insecure-skip-tls-verify is refused")
    server = urlsplit(str(cluster.get("server", "")))
    if transport == "https" and server.scheme != "https":
        raise ProviderFactoryError("the context's server must use https")
    if transport == "loopback-http":
        try:
            loopback = ipaddress.ip_address(server.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if server.scheme != "http" or not loopback:
            raise ProviderFactoryError(
                "loopback-http is only for a literal loopback http:// test API"
            )


def api_client_from_kubeconfig(kubeconfig: Path, context: str) -> Any:
    """Return an ``ApiClient`` for exactly ``context`` in ``kubeconfig``.

    Never reads ``KUBECONFIG``, the default kubeconfig or in-cluster files, and
    never falls back to the file's ``current-context``.
    """
    return _client(kubeconfig, context, "https")


def _client(kubeconfig: Path, context: str, transport: str) -> Any:
    from kubernetes.client import ApiClient, Configuration
    from kubernetes.config.config_exception import ConfigException
    from kubernetes.config.kube_config import KubeConfigLoader

    if not context:
        raise ProviderFactoryError("an explicit kubeconfig context is required")
    path = Path(kubeconfig).expanduser()
    document = _read_kubeconfig(path)
    _check_entries(document, context, transport)
    configuration = Configuration()
    try:
        loader = KubeConfigLoader(
            config_dict=document,
            active_context=context,
            config_base_path=str(path.resolve().parent),
        )
        loader.load_and_set(configuration)
    except ConfigException as error:
        raise ProviderFactoryError(f"kubeconfig context rejected: {error}") from None
    # The SDK installs a refresh hook for every token, including static ones.
    # Exec/auth-provider users were refused above, so the hook could only
    # re-read the same static token; dropping it keeps credentials explicit.
    configuration.refresh_api_key_hook = None
    configuration.proxy = None
    configuration.retries = 0
    return ApiClient(configuration)


def _read_uid(api_client: Any, kind: str, name: str, timeout: float) -> str | None:
    import json

    from kubernetes.client import CoreV1Api
    from kubernetes.client.exceptions import ApiException

    api = CoreV1Api(api_client)
    read = api.read_namespace if kind == "Namespace" else api.read_node
    try:
        response = read(name, _preload_content=False, _request_timeout=timeout)
        data = json.loads(response.data)
    except ApiException as error:
        if error.status == 404:
            return None
        raise ProviderFactoryError(
            f"cluster identity read failed with HTTP {error.status}"
        ) from None
    except Exception as error:  # transport details may contain credentials
        raise ProviderFactoryError(
            f"cluster identity read failed: {type(error).__name__}"
        ) from None
    if (
        not isinstance(data, dict)
        or data.get("kind") != kind
        or not isinstance(data.get("metadata"), dict)
        or data["metadata"].get("name") != name
    ):
        raise ProviderFactoryError(f"unexpected {kind} identity response")
    uid = data["metadata"].get("uid")
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise ProviderFactoryError(f"{kind} identity has no uid")
    return uid


def read_cluster_identity(api_client: Any, target: KubeconfigTarget) -> ClusterIdentity:
    """Read and verify the kube-system, namespace and pinned node UIDs."""
    seconds = target.request_seconds
    cluster = _read_uid(api_client, "Namespace", "kube-system", seconds)
    if cluster is None:
        raise ProviderFactoryError("kube-system namespace is not readable")
    namespace = _read_uid(api_client, "Namespace", target.namespace, seconds)
    if namespace is None:
        raise ProviderFactoryError(
            f"namespace {target.namespace!r} does not exist; create it explicitly"
        )
    if target.cluster_uid is not None and cluster != target.cluster_uid:
        raise ProviderFactoryError(
            "cluster identity mismatch: kube-system UID differs from the spec"
        )
    if target.namespace_uid is not None and namespace != target.namespace_uid:
        raise ProviderFactoryError(
            "namespace identity mismatch: namespace UID differs from the spec"
        )
    nodes: dict[str, NodeExpectation] = {}
    for alias, expected in target.nodes.items():
        uid = _read_uid(api_client, "Node", expected.name, seconds)
        if uid is None:
            raise ProviderFactoryError(f"node {expected.name!r} ({alias}) not found")
        if expected.uid is not None and uid != expected.uid:
            raise ProviderFactoryError(
                f"node identity mismatch for {alias!r}: UID differs from the spec"
            )
        nodes[alias] = NodeExpectation(expected.name, uid)
    return ClusterIdentity(cluster, namespace, nodes)


def build_provider(
    target: KubeconfigTarget,
    *,
    field_manager: str,
    owner_id: str,
    inherited_owner_ids: tuple[str, ...] = (),
    api_client: Any | None = None,
) -> ProviderBinding:
    """Build a verified provider for one explicit kubeconfig context.

    ``api_client`` may be injected (tests); it is still identity-checked.
    """
    client = api_client or _client(target.kubeconfig, target.context, target.transport)
    try:
        identity = read_cluster_identity(client, target)
        plan_target = identity.plan_target(target.namespace)
        provider = KubernetesProvider(
            client,
            target=plan_target,
            field_manager=field_manager,
            owner_id=owner_id,
            source=EvidenceSource.LIVE
            if target.transport == "https"
            else EvidenceSource.LOOPBACK,
            cluster_uid=identity.cluster_uid,
            namespace_uid=identity.namespace_uid,
            request_seconds=target.request_seconds,
            inherited_owner_ids=inherited_owner_ids,
        )
    except BaseException:
        client.close()
        raise
    return ProviderBinding(provider, identity, plan_target)
