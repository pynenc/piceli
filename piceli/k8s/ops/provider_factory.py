"""Build a :class:`KubernetesProvider` from an explicit kubeconfig file and context.

This is the seed of the kubeconfig-to-provider factory. The authority model
stays explicit:

* the kubeconfig is a named file; ``KUBECONFIG``, ``~/.kube/config`` and
  in-cluster configuration are never consulted;
* the context is named; the file's ``current-context`` is never used;
* the cluster is identified by the ``kube-system`` Namespace UID and the
  target Namespace UID, read from the server and optionally compared with
  expected values;
* credentials are static (client certificate or bearer token) or, only
  with an explicit :class:`~piceli.k8s.ops.exec_credentials.ExecPolicy`
  (``allow_exec``), an exec credential plugin that is pinned, run with a
  minimal environment and refreshed by a Piceli-owned hook (see
  :mod:`piceli.k8s.ops.exec_credentials`). Legacy ``auth-provider`` users
  (``gcp``, ``oidc``, ``azure``) are always refused: their exec replacements
  (``gke-gcloud-auth-plugin``, ``kubelogin``) cover the same clusters;
* proxies, ``insecure-skip-tls-verify`` and non-https servers are refused.

Importing this module has no side effects; the Kubernetes SDK is imported
lazily when a client is built.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from piceli.k8s.ops.discovery import EvidenceSource, PlanTarget
from piceli.k8s.ops.exec_credentials import (
    ClusterInfo,
    ExecAuthError,
    ExecCredentialSource,
    ExecPlugin,
    ExecPolicy,
    ProviderFactoryError,
    TlsMaterialError,
    resolve_plugin,
    tls_context,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider

__all__ = [
    "ClusterIdentity",
    "ExecAuthError",
    "ExecPolicy",
    "KubeconfigTarget",
    "NodeExpectation",
    "ProviderBinding",
    "ProviderFactoryError",
    "api_client_from_kubeconfig",
    "build_provider",
    "credential_plugin",
    "read_cluster_identity",
    "verify_exec_user",
    "verify_kubeconfig_context",
]

logger = logging.getLogger(__name__)
_NO_EXEC = ExecPolicy()
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
    allow_exec: bool = False
    exec_sha256: str | None = None
    exec_pass_env: tuple[str, ...] = ()
    exec_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        self.exec_policy  # noqa: B018 (validates the exec fields)
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

    @property
    def exec_policy(self) -> ExecPolicy:
        """The explicit authority to run this context's exec plugin, if any."""
        try:
            return ExecPolicy(
                self.allow_exec,
                self.exec_sha256,
                tuple(self.exec_pass_env),
                self.exec_timeout_seconds,
            )
        except ValueError as error:
            raise ProviderFactoryError(str(error)) from None


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
    #: The pinned exec plugin (path, sha256, apiVersion) when one authenticates.
    credential_plugin: Mapping[str, str] | None = None

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


@dataclass(frozen=True)
class _Entries:
    cluster: dict[str, Any]
    user: dict[str, Any]


def _check_entries(
    document: Mapping[str, Any],
    context: str,
    transport: str,
    policy: ExecPolicy = _NO_EXEC,
) -> _Entries:
    """Refuse credentials and transports the provider cannot use safely."""
    ctx = _named(document, "contexts", context)
    cluster = _named(document, "clusters", str(ctx.get("cluster", "")))
    user = _named(document, "users", str(ctx.get("user", "")))
    if "auth-provider" in user:
        raise ExecAuthError(
            "auth-provider-refused",
            "legacy auth-provider credentials are not supported; use the "
            "provider's exec plugin (gke-gcloud-auth-plugin, kubelogin) with "
            "allow_exec = true",
        )
    if "exec" in user:
        if not policy.allow:
            raise ExecAuthError(
                "exec-auth-not-allowed",
                "the context's user runs an exec credential plugin; review it "
                "and set allow_exec = true in [target] to permit it",
            )
        unsupported = set(user) - {"exec"}
    else:
        if policy.sha256 is not None:
            raise ExecAuthError(
                "exec-config-invalid",
                "exec_sha256 is set but the context's user has no exec plugin",
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
    return _Entries(cluster, user)


def api_client_from_kubeconfig(
    kubeconfig: Path,
    context: str,
    *,
    transport: Transport = "https",
    exec_policy: ExecPolicy | None = None,
) -> Any:
    """Return an ``ApiClient`` for exactly ``context`` in ``kubeconfig``.

    Never reads ``KUBECONFIG``, the default kubeconfig or in-cluster files, and
    never falls back to the file's ``current-context``. ``transport`` is
    ``https`` or, for a literal loopback test API, ``loopback-http``. An exec
    plugin runs only when ``exec_policy`` allows it.
    """
    return _client(kubeconfig, context, transport, exec_policy or _NO_EXEC)


def verify_kubeconfig_context(
    kubeconfig: Path, context: str, *, exec_policy: ExecPolicy | None = None
) -> dict[str, str] | None:
    """Check ``context`` in ``kubeconfig`` without contacting the cluster.

    Applies every refusal of :func:`api_client_from_kubeconfig` (explicit
    context, no auth-provider, no proxy or insecure TLS, https, exec only with
    ``exec_policy``) and, for an exec user, resolves and pins the plugin
    without running it. Use it before handing the kubeconfig to ``kubectl``.
    Returns the pinned plugin summary for an exec user, else ``None``.
    """
    policy = exec_policy or _NO_EXEC
    path, entries = _entries_for(kubeconfig, context, "https", policy)
    if "exec" in entries.user:
        plugin = resolve_plugin(
            entries.user["exec"], kubeconfig_dir=path.resolve().parent, policy=policy
        )
        return plugin.summary()
    return None


def verify_exec_user(
    kubeconfig: Path, context: str, *, exec_policy: ExecPolicy | None = None
) -> dict[str, str] | None:
    """Refuse or pin the context's exec plugin only; never runs it.

    For commands that hand the kubeconfig to ``kubectl`` and keep kubectl's
    own transport rules: a user with an ``exec`` block is refused
    (``exec-auth-not-allowed``) unless ``exec_policy`` allows it; an allowed
    plugin is resolved and pinned (``exec_sha256``). Returns the pinned
    plugin summary, else ``None``.
    """
    policy = exec_policy or _NO_EXEC
    path = Path(kubeconfig).expanduser()
    document = _read_kubeconfig(path)
    ctx = _named(document, "contexts", context)
    user = _named(document, "users", str(ctx.get("user", "")))
    if "exec" not in user:
        return None
    if not policy.allow:
        raise ExecAuthError(
            "exec-auth-not-allowed",
            "the context's user runs an exec credential plugin; review it and "
            "allow it on the target (allow_exec) to permit it",
        )
    plugin = resolve_plugin(
        user["exec"], kubeconfig_dir=path.resolve().parent, policy=policy
    )
    return plugin.summary()


def _entries_for(
    kubeconfig: Path, context: str, transport: str, policy: ExecPolicy
) -> tuple[Path, _Entries]:
    if not context:
        raise ProviderFactoryError("an explicit kubeconfig context is required")
    path = Path(kubeconfig).expanduser()
    return path, _check_entries(_read_kubeconfig(path), context, transport, policy)


def _client(
    kubeconfig: Path,
    context: str,
    transport: str,
    policy: ExecPolicy = _NO_EXEC,
) -> Any:
    path, entries = _entries_for(kubeconfig, context, transport, policy)
    if "exec" in entries.user:
        return _exec_client(path, entries, policy)
    return _static_client(path, entries)


def _ca(cluster: Mapping[str, Any], base: Path) -> tuple[str | None, Path | None]:
    """The cluster CA as in-memory PEM text, or the file the kubeconfig names."""
    import base64
    import binascii

    if cluster.get("certificate-authority-data"):
        try:
            data = base64.b64decode(
                str(cluster["certificate-authority-data"]), validate=True
            ).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ProviderFactoryError(
                "certificate-authority-data is not base64 PEM"
            ) from None
        return data, None
    if cluster.get("certificate-authority"):
        path = base / str(cluster["certificate-authority"])
        if not path.is_file():
            raise ProviderFactoryError("certificate-authority file not found")
        return None, path
    return None, None


def _configuration(cluster: Mapping[str, Any]) -> Any:
    from kubernetes.client import Configuration

    configuration = Configuration()
    configuration.host = str(cluster.get("server", "")).rstrip("/")
    configuration.verify_ssl = True
    configuration.proxy = None
    configuration.retries = 0
    configuration.refresh_api_key_hook = None
    if cluster.get("tls-server-name"):
        configuration.tls_server_name = str(cluster["tls-server-name"])
    return configuration


def _pem_data(user: Mapping[str, Any], key: str) -> bytes | None:
    import base64
    import binascii

    value = user.get(key)
    if not value:
        return None
    try:
        return base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        raise ProviderFactoryError(f"kubeconfig {key} is not base64") from None


def _static_client(path: Path, entries: _Entries) -> Any:
    """An ``ApiClient`` for a static token or client certificate.

    The SDK's kubeconfig loader is bypassed: it writes certificate data to
    temporary files. Here ``*-data`` fields go to OpenSSL through pipes, the
    CA is read in memory, and files the kubeconfig names are used in place.
    No refresh hook is installed (a static token cannot be refreshed).
    """
    from kubernetes.client import ApiClient

    base = path.resolve().parent
    user = entries.user
    configuration = _configuration(entries.cluster)
    token = user.get("token")
    if token is not None:
        if not isinstance(token, str) or not token or "\n" in token:
            raise ProviderFactoryError("kubeconfig token is invalid")
        configuration.api_key["BearerToken"] = "Bearer " + token
    cert = _pem_data(user, "client-certificate-data")
    key = _pem_data(user, "client-key-data")
    cert_file = key_file = None
    if cert is None and user.get("client-certificate"):
        cert_file = base / str(user["client-certificate"])
    if key is None and user.get("client-key"):
        key_file = base / str(user["client-key"])
    ca_data, ca_file = _ca(entries.cluster, base)
    try:
        context = tls_context(
            ca_data,
            ca_file,
            cert=cert,
            key=key,
            cert_file=cert_file,
            key_file=key_file,
        )
    except TlsMaterialError as error:
        raise ProviderFactoryError(str(error)) from None
    client = ApiClient(configuration)
    client.rest_client.pool_manager = _direct_pool(context, configuration)
    return client


def _direct_pool(context: Any, configuration: Any) -> Any:
    import urllib3

    return urllib3.PoolManager(
        num_pools=4,
        maxsize=4,
        retries=False,
        ssl_context=context,
        **(
            {"server_hostname": configuration.tls_server_name}
            if configuration.tls_server_name
            else {}
        ),
    )


def _exec_client(path: Path, entries: _Entries, policy: ExecPolicy) -> Any:
    """An ``ApiClient`` whose credential comes from a pinned exec plugin.

    The SDK's own exec loader is bypassed: it inherits the whole environment
    and writes client certificates to temporary files. The cluster's CA is
    read in memory as well.
    """
    from kubernetes.client import ApiClient

    base = path.resolve().parent
    plugin: ExecPlugin = resolve_plugin(
        entries.user["exec"], kubeconfig_dir=base, policy=policy
    )
    cluster = entries.cluster
    ca_data, ca_file = _ca(cluster, base)
    configuration = _configuration(cluster)
    client = ApiClient(configuration)
    try:
        ExecCredentialSource.attach(
            client,
            plugin,
            policy,
            cluster=ClusterInfo(
                configuration.host,
                cluster.get("certificate-authority-data") or None,
                configuration.tls_server_name or None,
            ),
            ca_data=ca_data,
            ca_file=ca_file,
        )
    except BaseException:
        client.close()
        raise
    logger.info("exec credential plugin %s (%s)", plugin.command, plugin.sha256)
    return client


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
            f"cluster identity read failed with HTTP {error.status}",
            code="cluster-identity-unreadable",
        ) from None
    except Exception as error:  # transport details may contain credentials
        raise ProviderFactoryError(
            f"cluster identity read failed: {type(error).__name__}",
            code="cluster-identity-unreadable",
        ) from None
    if (
        not isinstance(data, dict)
        or data.get("kind") != kind
        or not isinstance(data.get("metadata"), dict)
        or data["metadata"].get("name") != name
    ):
        raise ProviderFactoryError(
            f"unexpected {kind} identity response", code="cluster-identity-unreadable"
        )
    uid = data["metadata"].get("uid")
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise ProviderFactoryError(
            f"{kind} identity has no uid", code="cluster-identity-unreadable"
        )
    return uid


def read_cluster_identity(api_client: Any, target: KubeconfigTarget) -> ClusterIdentity:
    """Read and verify the kube-system, namespace and pinned node UIDs."""
    seconds = target.request_seconds
    cluster = _read_uid(api_client, "Namespace", "kube-system", seconds)
    if cluster is None:
        raise ProviderFactoryError(
            "kube-system namespace is not readable", code="cluster-identity-unreadable"
        )
    namespace = _read_uid(api_client, "Namespace", target.namespace, seconds)
    if namespace is None:
        raise ProviderFactoryError(
            f"namespace {target.namespace!r} does not exist; create it explicitly",
            code="namespace-not-found",
        )
    if target.cluster_uid is not None and cluster != target.cluster_uid:
        raise ProviderFactoryError(
            "cluster identity mismatch: kube-system UID differs from the spec",
            code="server-target-identity-mismatch",
        )
    if target.namespace_uid is not None and namespace != target.namespace_uid:
        raise ProviderFactoryError(
            "namespace identity mismatch: namespace UID differs from the spec",
            code="server-target-identity-mismatch",
        )
    nodes: dict[str, NodeExpectation] = {}
    for alias, expected in target.nodes.items():
        uid = _read_uid(api_client, "Node", expected.name, seconds)
        if uid is None:
            raise ProviderFactoryError(
                f"node {expected.name!r} ({alias}) not found", code="node-not-found"
            )
        if expected.uid is not None and uid != expected.uid:
            raise ProviderFactoryError(
                f"node identity mismatch for {alias!r}: UID differs from the spec",
                code="node-identity-mismatch",
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
    client = api_client or _client(
        target.kubeconfig, target.context, target.transport, target.exec_policy
    )
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
    return ProviderBinding(provider, identity, plan_target, credential_plugin(client))


def credential_plugin(api_client: Any) -> dict[str, str] | None:
    """The pinned exec plugin behind ``api_client`` (path, sha256), if any."""
    hook = getattr(api_client.configuration, "refresh_api_key_hook", None)
    source = getattr(hook, "__self__", None)
    if isinstance(source, ExecCredentialSource):
        return source.plugin.summary()
    return None
