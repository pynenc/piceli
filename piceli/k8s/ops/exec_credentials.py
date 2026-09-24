"""Exec credential plugins for explicitly opted-in kubeconfig users.

Managed clusters (GKE ``gke-gcloud-auth-plugin``, EKS ``aws eks get-token``,
AKS/OIDC ``kubelogin``) authenticate through a kubeconfig ``exec`` plugin: a
local program that prints a short-lived ``ExecCredential``. Running it is code
execution, so Piceli only does it under an explicit policy:

* **Opt-in.** Without :attr:`ExecPolicy.allow` an exec user is refused and the
  plugin never runs.
* **Pinned.** The command is resolved once to an absolute real path (a bare
  name is looked up in ``PATH``, a relative path from the kubeconfig's
  directory) and its sha256 is recorded. An optional expected digest
  (``exec_sha256``) must match. The file is hashed again before every run, so
  a binary replaced mid-run is refused.
* **Minimal environment.** The plugin gets ``PATH`` and ``HOME`` from Piceli's
  environment, the variables named in :attr:`ExecPolicy.pass_env`, the
  kubeconfig's ``env`` entries, ``LANG=C`` and ``KUBERNETES_EXEC_INFO``;
  nothing else (no cloud credentials, no ``KUBECONFIG``) is inherited.
* **Non-interactive, bounded.** stdin is closed, the run has a timeout and a
  1 MB output limit; ``interactiveMode: Always`` plugins are refused. The
  plugin's stderr is forwarded to Piceli's stderr (as ``kubectl`` does); its
  stdout, which carries the credential, is never logged.
* **In memory only.** Tokens live in the client configuration; client
  certificates are handed to OpenSSL through pipes (``/dev/fd``), never files.
* **Controlled refresh.** :class:`ExecCredentialSource` owns the client's
  ``refresh_api_key_hook`` and connection pool. It re-runs the pinned plugin
  when ``expirationTimestamp`` is near, and :func:`approved_refresh` lets
  :class:`~piceli.k8s.ops.kubernetes_provider.KubernetesProvider` accept only
  clients bound by this module; any other refresh hook is still refused.

Importing this module has no side effects.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import re
import shutil
import stat
import sys
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

EXEC_API_VERSIONS = frozenset(
    {"client.authentication.k8s.io/v1", "client.authentication.k8s.io/v1beta1"}
)
#: Variables every plugin receives from Piceli's own environment (when set).
BASE_ENV = ("PATH", "HOME")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_RESERVED_ENV = frozenset({"KUBERNETES_EXEC_INFO"})
_MAX_OUTPUT = 1_000_000
_MAX_PEM = 12 * 1024  # fits one pipe buffer on every supported platform
_MAX_SKEW = timedelta(seconds=30)
_EXEC_KEYS = frozenset(
    {
        "apiVersion",
        "command",
        "args",
        "env",
        "provideClusterInfo",
        "interactiveMode",
        "installHint",
    }
)


class ProviderFactoryError(ValueError):
    """The kubeconfig, context or observed cluster identity is not acceptable.

    Defined here so :class:`ExecAuthError` can extend it; import it from
    :mod:`piceli.k8s.ops.provider_factory`.
    """


class ExecAuthError(ProviderFactoryError):
    """An exec credential was refused or failed; ``code`` is a registered error code.

    Messages never contain tokens, certificates, keys or plugin output.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecPolicy:
    """The caller's explicit authority to run a kubeconfig exec plugin.

    :param allow: Run exec plugins at all. ``False`` refuses every exec user.
    :param sha256: Optional expected ``sha256:<hex>`` of the resolved command.
    :param pass_env: Names of extra variables copied from Piceli's environment
        (for example ``AWS_PROFILE``); ``PATH`` and ``HOME`` are always passed.
    :param timeout_seconds: Limit for one plugin run.
    """

    allow: bool = False
    sha256: str | None = None
    pass_env: tuple[str, ...] = ()
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not isinstance(self.allow, bool):
            raise ValueError("allow_exec must be a boolean")
        if self.sha256 is not None and not _DIGEST.fullmatch(self.sha256):
            raise ValueError("exec_sha256 must be sha256:<64 hex>")
        if isinstance(self.pass_env, str) or not all(
            isinstance(name, str)
            and _ENV_NAME.fullmatch(name)
            and name not in _RESERVED_ENV
            for name in self.pass_env
        ):
            raise ValueError("exec_pass_env must be a list of variable names")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("exec_timeout_seconds must be in (0, 300]")
        if not self.allow and (self.sha256 is not None or self.pass_env):
            raise ValueError("exec_sha256/exec_pass_env require allow_exec = true")


@dataclass(frozen=True)
class ExecPlugin:
    """A kubeconfig exec block, resolved to one pinned executable file."""

    command: Path
    sha256: str
    api_version: str
    args: tuple[str, ...] = field(default=(), repr=False)
    env: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    provide_cluster_info: bool = False
    cwd: Path = Path("/")

    def summary(self) -> dict[str, str]:
        """What may be recorded: the resolved path and digest, never args or env."""
        return {
            "command": str(self.command),
            "sha256": self.sha256,
            "api_version": self.api_version,
        }


@dataclass(frozen=True, repr=False)
class ExecCredential:
    """One ``ExecCredential`` status. ``repr`` never shows the secret material."""

    token: str | None
    client_certificate: bytes | None
    client_key: bytes | None
    expires_at: datetime | None

    def __repr__(self) -> str:
        kinds = [
            name
            for name, value in (
                ("token", self.token),
                ("client-certificate", self.client_certificate),
            )
            if value
        ]
        return f"ExecCredential({'+'.join(kinds)}, expires_at={self.expires_at})"


@dataclass(frozen=True)
class ClusterInfo:
    """Cluster fields a plugin may receive with ``provideClusterInfo``."""

    server: str
    certificate_authority_data: str | None = None
    tls_server_name: str | None = None

    def to_exec_info(self) -> dict[str, Any]:
        value: dict[str, Any] = {"server": self.server}
        if self.certificate_authority_data:
            value["certificate-authority-data"] = self.certificate_authority_data
        if self.tls_server_name:
            value["tls-server-name"] = self.tls_server_name
        return value


def _digest(path: Path) -> str:
    from piceli.artifacts.process import ToolPin

    try:
        return ToolPin._digest(path)
    except (OSError, ValueError):
        raise ExecAuthError(
            "exec-command-unsafe", "exec command is not a bounded regular file"
        ) from None


def _check_file(path: Path) -> None:
    try:
        info = path.stat()
    except OSError:
        raise ExecAuthError(
            "exec-command-not-found", f"exec command {path} is not readable"
        ) from None
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ExecAuthError(
            "exec-command-unsafe", f"exec command {path} is not an executable file"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ExecAuthError(
            "exec-command-unsafe",
            f"exec command {path} is writable by group or others",
        )


def _resolve_command(command: str, base: Path, search_path: str | None) -> Path:
    if "/" in command or os.sep in command:
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = base / candidate
        found: str | None = str(candidate)
    else:
        found = shutil.which(command, path=search_path or "")
    if not found:
        raise ExecAuthError(
            "exec-command-not-found",
            f"exec command {command!r} was not found in PATH",
        )
    try:
        return Path(os.path.realpath(found, strict=True))
    except OSError:
        raise ExecAuthError(
            "exec-command-not-found", f"exec command {command!r} does not exist"
        ) from None


def resolve_plugin(
    config: Any,
    *,
    kubeconfig_dir: Path,
    policy: ExecPolicy,
    environ: Mapping[str, str] | None = None,
) -> ExecPlugin:
    """Validate a kubeconfig ``exec`` block and pin its command.

    Refuses without :attr:`ExecPolicy.allow`, before anything is resolved.
    """
    if not policy.allow:
        raise ExecAuthError(
            "exec-auth-not-allowed",
            "the context's user runs an exec credential plugin; review it and "
            "set allow_exec = true in [target] to permit it",
        )
    if not isinstance(config, Mapping):
        raise ExecAuthError("exec-config-invalid", "kubeconfig exec must be a mapping")
    unknown = set(config) - _EXEC_KEYS
    if unknown:
        raise ExecAuthError(
            "exec-config-invalid", f"unsupported exec fields: {sorted(unknown)}"
        )
    api_version = config.get("apiVersion")
    if api_version not in EXEC_API_VERSIONS:
        raise ExecAuthError(
            "exec-config-invalid",
            "exec apiVersion must be client.authentication.k8s.io/v1 or v1beta1",
        )
    command = config.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ExecAuthError("exec-config-invalid", "exec command is required")
    args = config.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ExecAuthError("exec-config-invalid", "exec args must be strings")
    env: list[tuple[str, str]] = []
    for item in config.get("env") or []:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"name", "value"}
            or not isinstance(item["name"], str)
            or not _ENV_NAME.fullmatch(item["name"])
            or item["name"] in _RESERVED_ENV
            or not isinstance(item["value"], str)
        ):
            raise ExecAuthError(
                "exec-config-invalid", "exec env entries must be {name, value} strings"
            )
        env.append((item["name"], item["value"]))
    if config.get("interactiveMode", "IfAvailable") not in ("Never", "IfAvailable"):
        raise ExecAuthError(
            "exec-interactive-required",
            "the exec plugin requires an interactive terminal (interactiveMode: "
            "Always); log in with the plugin first or use a non-interactive mode",
        )
    provide = config.get("provideClusterInfo", False)
    if not isinstance(provide, bool):
        raise ExecAuthError("exec-config-invalid", "provideClusterInfo must be bool")
    source = os.environ if environ is None else environ
    path = _resolve_command(command, kubeconfig_dir, source.get("PATH"))
    _check_file(path)
    digest = _digest(path)
    if policy.sha256 is not None and digest != policy.sha256:
        raise ExecAuthError(
            "exec-pin-mismatch",
            f"exec command {path} has {digest}, the spec pins {policy.sha256}",
        )
    return ExecPlugin(
        command=path,
        sha256=digest,
        api_version=str(api_version),
        args=tuple(args),
        env=tuple(env),
        provide_cluster_info=provide,
        cwd=kubeconfig_dir,
    )


def plugin_environment(
    plugin: ExecPlugin,
    policy: ExecPolicy,
    environ: Mapping[str, str],
    cluster: ClusterInfo | None,
) -> dict[str, str]:
    """The complete environment for one plugin run (see the module docstring)."""
    env = {
        name: environ[name]
        for name in (*BASE_ENV, *policy.pass_env)
        if name in environ and "\0" not in environ[name]
    }
    env.update(plugin.env)
    spec: dict[str, Any] = {"interactive": False}
    if plugin.provide_cluster_info and cluster is not None:
        spec["cluster"] = cluster.to_exec_info()
    env["KUBERNETES_EXEC_INFO"] = json.dumps(
        {"apiVersion": plugin.api_version, "kind": "ExecCredential", "spec": spec},
        sort_keys=True,
    )
    return env


def _pem(value: Any, what: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ExecAuthError("exec-credential-invalid", f"ExecCredential {what} empty")
    data = value.encode()
    if len(data) > _MAX_PEM or b"-----BEGIN " not in data:
        raise ExecAuthError(
            "exec-credential-invalid", f"ExecCredential {what} is not a PEM block"
        )
    return data


def parse_credential(
    output: bytes, plugin: ExecPlugin, now: datetime
) -> ExecCredential:
    """Validate a plugin's stdout. Errors never quote the output."""
    try:
        document = json.loads(output)
    except (UnicodeDecodeError, ValueError):
        raise ExecAuthError(
            "exec-credential-invalid", "exec plugin output is not JSON"
        ) from None
    if (
        not isinstance(document, dict)
        or document.get("kind") != "ExecCredential"
        or document.get("apiVersion") != plugin.api_version
        or not isinstance(document.get("status"), dict)
    ):
        raise ExecAuthError(
            "exec-credential-invalid",
            "exec plugin output is not an ExecCredential of the configured apiVersion",
        )
    status = document["status"]
    token = status.get("token")
    if token is not None and (
        not isinstance(token, str) or not token or any(c in token for c in "\r\n\0")
    ):
        raise ExecAuthError("exec-credential-invalid", "ExecCredential token invalid")
    cert = key = None
    if "clientCertificateData" in status or "clientKeyData" in status:
        cert = _pem(status.get("clientCertificateData"), "clientCertificateData")
        key = _pem(status.get("clientKeyData"), "clientKeyData")
    if not token and cert is None:
        raise ExecAuthError(
            "exec-credential-invalid",
            "ExecCredential has neither a token nor a client certificate",
        )
    expires_at = None
    raw = status.get("expirationTimestamp")
    if raw is not None:
        try:
            expires_at = datetime.fromisoformat(str(raw))
        except ValueError:
            expires_at = None
        if expires_at is None or expires_at.tzinfo is None:
            raise ExecAuthError(
                "exec-credential-invalid",
                "ExecCredential expirationTimestamp is not RFC 3339",
            )
        if expires_at <= now:
            raise ExecAuthError(
                "exec-credential-invalid", "exec plugin returned an expired credential"
            )
    return ExecCredential(token or None, cert, key, expires_at)


def _forward_stderr(stream: str, block: bytes) -> None:
    if stream == "stderr":
        try:
            sys.stderr.buffer.write(block)
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            pass


def run_plugin(
    plugin: ExecPlugin,
    policy: ExecPolicy,
    *,
    environ: Mapping[str, str],
    cluster: ClusterInfo | None,
    now: datetime,
) -> ExecCredential:
    """Re-verify the pin, run the plugin once and parse its credential."""
    from piceli.artifacts.process import ProcessLimits, _run_process

    _check_file(plugin.command)
    if _digest(plugin.command) != plugin.sha256:
        raise ExecAuthError(
            "exec-pin-mismatch",
            f"exec command {plugin.command} changed since it was pinned",
        )
    try:
        receipt, stdout, _ = _run_process(
            [str(plugin.command), *plugin.args],
            plugin.cwd,
            ProcessLimits(policy.timeout_seconds, _MAX_OUTPUT),
            environment=plugin_environment(plugin, policy, environ, cluster),
            on_output=_forward_stderr,
        )
    except (OSError, ValueError):
        raise ExecAuthError(
            "exec-plugin-failed", f"exec command {plugin.command} could not start"
        ) from None
    state = receipt["state"]
    if state == "timed-out":
        raise ExecAuthError(
            "exec-plugin-timed-out",
            f"exec command {plugin.command} did not finish in "
            f"{policy.timeout_seconds:g}s",
        )
    if state == "output-limit":
        raise ExecAuthError(
            "exec-credential-invalid", "exec plugin output exceeds 1 MB"
        )
    if state != "succeeded":
        raise ExecAuthError(
            "exec-plugin-failed",
            f"exec command {plugin.command} failed (exit code "
            f"{receipt['exit_code']}); see its message above",
        )
    return parse_credential(stdout, plugin, now)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ssl_context(
    ca_data: str | None, ca_file: Path | None, credential: ExecCredential | None
) -> ssl.SSLContext:
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_data is not None:
        context.load_verify_locations(cadata=ca_data)
    elif ca_file is not None:
        context.load_verify_locations(cafile=str(ca_file))
    else:
        context.load_default_certs()
    if credential is not None and credential.client_certificate is not None:
        assert credential.client_key is not None
        _load_client_certificate(
            context, credential.client_certificate, credential.client_key
        )
    return context


def _load_client_certificate(context: ssl.SSLContext, cert: bytes, key: bytes) -> None:
    """Hand PEM data to OpenSSL through pipes, so it never touches a file."""
    import ssl

    if not os.path.isdir("/dev/fd"):
        raise ExecAuthError(
            "exec-credential-invalid",
            "client-certificate exec credentials need /dev/fd on this platform",
        )
    descriptors: list[int] = []
    try:
        paths = []
        for data in (cert, key):
            read, write = os.pipe()
            descriptors += [read, write]
            view = memoryview(data)
            while view:
                view = view[os.write(write, view) :]
            os.close(write)
            descriptors.remove(write)
            paths.append(f"/dev/fd/{read}")
        context.load_cert_chain(paths[0], paths[1])
    except (ssl.SSLError, OSError):  # OpenSSL may try to seek the pipe on errors
        raise ExecAuthError(
            "exec-credential-invalid",
            "the exec plugin's client certificate or key is not valid",
        ) from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


_ISSUED: weakref.WeakSet[ExecCredentialSource] = weakref.WeakSet()


class ExecCredentialSource:
    """Piceli-owned credential refresh for one ``ApiClient``.

    Use :meth:`attach`; a source built any other way is never approved. The
    source replaces the client's connection pool (so client certificates can
    rotate) and installs its own ``refresh_api_key_hook`` (so tokens can).
    Both call :meth:`ensure_fresh`, which re-runs the pinned plugin when the
    credential is within ``min(30s, lifetime / 2)`` of ``expirationTimestamp``.
    A credential without an expiry is reused for the client's lifetime.
    """

    def __init__(
        self,
        plugin: ExecPlugin,
        policy: ExecPolicy,
        *,
        cluster: ClusterInfo,
        ca_data: str | None,
        ca_file: Path | None,
        environ: Mapping[str, str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.plugin = plugin
        self.policy = policy
        self._cluster = cluster
        self._ca_data = ca_data
        self._ca_file = ca_file
        self._environ = dict(environ)
        # Looked up per call, so the default clock follows the module's.
        self._clock = clock or (lambda: _utc_now())
        self._lock = threading.RLock()
        self._credential: ExecCredential | None = None
        self._fetched_at: datetime | None = None
        self.runs = 0
        self.configuration: Any = None
        self.pool_manager: Any = None

    @classmethod
    def attach(
        cls,
        api_client: Any,
        plugin: ExecPlugin,
        policy: ExecPolicy,
        *,
        cluster: ClusterInfo,
        ca_data: str | None = None,
        ca_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ExecCredentialSource:
        """Run the plugin once and bind the credential to ``api_client``."""
        source = cls(
            plugin,
            policy,
            cluster=cluster,
            ca_data=ca_data,
            ca_file=ca_file,
            environ=os.environ if environ is None else environ,
            clock=clock,
        )
        credential = source._fetch()
        configuration = api_client.configuration
        source.configuration = configuration
        source.pool_manager = _pool_manager_class()(
            source,
            num_pools=4,
            maxsize=4,
            retries=False,
            ssl_context=_ssl_context(ca_data, ca_file, credential),
            **(
                {"server_hostname": configuration.tls_server_name}
                if configuration.tls_server_name
                else {}
            ),
        )
        api_client.rest_client.pool_manager = source.pool_manager
        source._apply_token(credential)
        configuration.refresh_api_key_hook = source._refresh_hook
        _ISSUED.add(source)
        return source

    def _fetch(self) -> ExecCredential:
        now = self._clock()
        credential = run_plugin(
            self.plugin,
            self.policy,
            environ=self._environ,
            cluster=self._cluster,
            now=now,
        )
        self.runs += 1
        self._credential = credential
        self._fetched_at = now
        return credential

    def _stale(self) -> bool:
        credential = self._credential
        if credential is None:
            return True
        if credential.expires_at is None or self._fetched_at is None:
            return False
        lifetime = credential.expires_at - self._fetched_at
        skew = min(_MAX_SKEW, lifetime / 2)
        return self._clock() >= credential.expires_at - skew

    def _apply_token(self, credential: ExecCredential) -> None:
        if credential.token:
            self.configuration.api_key["BearerToken"] = "Bearer " + credential.token
        else:
            self.configuration.api_key.pop("BearerToken", None)

    def ensure_fresh(self) -> None:
        """Re-run the pinned plugin when the credential is (nearly) expired."""
        with self._lock:
            if not self._stale():
                return
            previous = self._credential
            credential = self._fetch()
            self._apply_token(credential)
            if (
                previous is None
                or previous.client_certificate != credential.client_certificate
                or previous.client_key != credential.client_key
            ):
                self.pool_manager.connection_pool_kw["ssl_context"] = _ssl_context(
                    self._ca_data, self._ca_file, credential
                )
                self.pool_manager.clear()

    def _refresh_hook(self, configuration: Any) -> None:
        if configuration is not self.configuration:
            raise ExecAuthError(
                "exec-config-invalid", "exec credential hook bound to another client"
            )
        self.ensure_fresh()


@functools.cache
def _pool_manager_class() -> type:
    """The urllib3 pool subclass, defined lazily so importing stays cheap."""
    import urllib3

    class _ExecPoolManager(urllib3.PoolManager):
        """Refreshes the exec credential before every request it sends."""

        def __init__(self, source: ExecCredentialSource, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._source = source

        def urlopen(  # type: ignore[override]
            self, method: str, url: str, redirect: bool = True, **kw: Any
        ) -> Any:
            self._source.ensure_fresh()
            return super().urlopen(method, url, redirect=redirect, **kw)

    return _ExecPoolManager


def approved_refresh(api_client: Any) -> bool:
    """``True`` only for a client whose refresh hook was installed by :meth:`attach`.

    The hook must belong to an issued :class:`ExecCredentialSource` bound to
    this client's configuration and connection pool. Refresh hooks from the
    Kubernetes SDK's own loaders, or any other callable, are not approved.
    """
    configuration = getattr(api_client, "configuration", None)
    hook = getattr(configuration, "refresh_api_key_hook", None)
    source = getattr(hook, "__self__", None)
    rest_client = getattr(api_client, "rest_client", None)
    return (
        isinstance(source, ExecCredentialSource)
        and source in _ISSUED
        and getattr(hook, "__func__", None) is ExecCredentialSource._refresh_hook
        and source.configuration is configuration
        and source.pool_manager is getattr(rest_client, "pool_manager", None)
    )


def decode_ca_data(value: Any) -> str:
    """``certificate-authority-data`` (base64 PEM) as PEM text for OpenSSL."""
    try:
        return base64.b64decode(str(value), validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ExecAuthError(
            "exec-config-invalid", "certificate-authority-data is not base64 PEM"
        ) from None
