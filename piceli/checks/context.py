"""What a check runs against: :class:`CheckContext`.

A context names exactly one namespace of one cluster through an explicit
kubeconfig file and context (never ``KUBECONFIG``, ``~/.kube/config`` or the
file's ``current-context``), the release being checked and its images. It
gives checks three ways to reach the application:

* :meth:`CheckContext.forward` — a temporary loopback port forward, supervised
  by :class:`~piceli.k8s.observe.ForwardSupervisor` and stopped afterwards;
* :meth:`CheckContext.http_get` — one GET through such a forward;
* :meth:`CheckContext.exec` — a command in a ready pod, through the Kubernetes
  API (``pods/exec``).

Nothing happens when a context is built: the API client, forwards and exec
streams are created on first use and released by :meth:`CheckContext.close`
(or leaving a ``with`` block).
"""

from __future__ import annotations

import http.client
import json
import socket
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from piceli.checks.model import EXEC_KINDS, HTTP_KINDS, split_target

_MAX_BODY = 1 << 20
_MAX_OUTPUT = 64 * 1024

#: ``forwarder(context, target, remote_port)`` → a context manager yielding
#: the base URL (``http://127.0.0.1:PORT``) of a live forward.
Forwarder = Callable[["CheckContext", str, int], AbstractContextManager[str]]
#: ``executor(context, target, command, container, timeout)`` → result.
Executor = Callable[
    ["CheckContext", str, Sequence[str], "str | None", float], "ExecResult"
]


class CheckError(Exception):
    """A check could not pass. ``code`` is one fixed word; the message is safe.

    Codes: ``check-failed`` (the condition was not met),
    ``check-api-unavailable``, ``check-target-not-found``, ``check-port-unknown``,
    ``check-forward-unavailable``, ``check-timed-out``,
    ``check-exec-unavailable``, ``check-metric-invalid`` and ``check-raised``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.detail = message


class CheckFailed(Exception):
    """Raise from a ``python`` check to fail it with ``detail``.

    The detail is printed and stored in the release history: never put a
    secret value in it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class HttpResponse:
    """Status and (at most 1 MiB of) body of one GET."""

    status: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ExecResult:
    """Exit code and (at most 64 KiB each of) output of one exec."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _image_reference(value: Any) -> str:
    """An image as a string: ``ImageRef``, a stored image dict, or a string."""
    if isinstance(value, str):
        return value
    reference = getattr(value, "reference", None)
    if reference is not None:
        try:
            return str(reference)
        except Exception:  # an ImageRef without an immutable reference
            return str(getattr(value, "identity", ""))
    if isinstance(value, Mapping):
        if value.get("repository") and value.get("digest"):
            return f"{value['repository']}@{value['digest']}"
        return str(value.get("ref") or value.get("identity") or "")
    identity = getattr(value, "identity", None)
    return str(identity) if identity is not None else str(value)


class CheckContext:
    """Everything a check may use; built from explicit inputs only.

    :param kubeconfig: Path of the kubeconfig file (read only on first use).
    :param context: The context name in that file; required, never
        ``current-context``.
    :param namespace: The namespace the release lives in; every target is in it.
    :param release: The release name being checked (for example
        ``shop-3f9a1c2e4b5d``).
    :param images: ``{name: image}``; values may be pull references
        (``repo@sha256:…``), :class:`~piceli.k8s.release_spec.ImageRef` objects or
        stored image dicts. Exposed as ``{name: reference}``.
    :param values: Free-form values (the release's ``[values]``), read only.
    :param base: Directory that relative ``python`` check files resolve from.
    :param transport: ``https`` (default) or ``loopback-http`` (test APIs).
    :param request_seconds: Timeout of each Kubernetes API request.
    :param kubectl: The ``kubectl`` used for port forwards.
    :param forward_seconds: How long a forward may take to become healthy.
    :param forwarder: Replaces the supervised ``kubectl port-forward``
        (tests, or another transport).
    :param executor: Replaces the Kubernetes API exec (tests).
    :param api_client: An existing ``kubernetes.client.ApiClient`` to use
        instead of building one; the caller keeps ownership.
    :param exec_policy: The explicit authority to run the context's exec
        credential plugin (``[target] allow_exec``); none by default.

    Example::

        with CheckContext(Path("kubeconfig"), "kind-shop", "shop", "shop-1",
                          {"web": "registry.test/web@sha256:…"}) as ctx:
            report = run_checks(Checks.http("service/web", "/login"), ctx)
    """

    def __init__(
        self,
        kubeconfig: Path | str,
        context: str,
        namespace: str,
        release: str,
        images: Mapping[str, Any] | None = None,
        *,
        values: Mapping[str, Any] | None = None,
        base: Path | None = None,
        transport: Literal["https", "loopback-http"] = "https",
        request_seconds: float = 10.0,
        kubectl: str = "kubectl",
        forward_seconds: float = 20.0,
        forwarder: Forwarder | None = None,
        executor: Executor | None = None,
        api_client: Any | None = None,
        exec_policy: Any | None = None,
    ) -> None:
        if not context:
            raise ValueError("an explicit kubeconfig context is required")
        if not namespace or not release:
            raise ValueError("namespace and release are required")
        self.kubeconfig = Path(kubeconfig).expanduser()
        self.context = context
        self.namespace = namespace
        self.release = release
        self.images: Mapping[str, str] = MappingProxyType(
            {name: _image_reference(value) for name, value in (images or {}).items()}
        )
        self.values: Mapping[str, Any] = MappingProxyType(dict(values or {}))
        self.base = base
        self.transport = transport
        self.request_seconds = request_seconds
        self.kubectl = kubectl
        self.forward_seconds = forward_seconds
        self._forwarder = forwarder or supervised_forward
        self._executor = executor or api_exec
        self._client = api_client
        self._owns_client = api_client is None
        self.exec_policy = exec_policy

    # ----------------------------------------------------------- lifecycle
    def __enter__(self) -> CheckContext:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the API client this context built (if any)."""
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            close = getattr(client, "close", None)
            if close is not None:
                close()
        self._owns_client = True

    def api_client(self) -> Any:
        """A ``kubernetes.client.ApiClient`` for exactly this kubeconfig context."""
        if self._client is None:
            from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

            try:
                self._client = api_client_from_kubeconfig(
                    self.kubeconfig,
                    self.context,
                    transport=self.transport,
                    exec_policy=self.exec_policy,
                )
            except ValueError as error:
                raise CheckError(
                    "check-api-unavailable",
                    f"cannot build an API client from the kubeconfig: {error}",
                ) from None
            self._owns_client = True
        return self._client

    # ------------------------------------------------------------ reading
    def _read(
        self, path: str, query: Sequence[tuple[str, str]] = ()
    ) -> dict[str, Any] | None:
        """GET one API path as JSON; ``None`` on 404."""
        from kubernetes.client.exceptions import ApiException

        client = self.api_client()
        try:
            response = client.call_api(
                path,
                "GET",
                query_params=list(query),
                header_params={"Accept": "application/json"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise CheckError(
                "check-api-unavailable", f"API read failed with HTTP {error.status}"
            ) from None
        except Exception as error:  # transport details may hold credentials
            raise CheckError(
                "check-api-unavailable", f"API read failed: {type(error).__name__}"
            ) from None
        raw = response[0] if isinstance(response, tuple) else response
        value = json.loads(raw.data)
        return value if isinstance(value, dict) else None

    def _object(self, kind: str, name: str) -> dict[str, Any]:
        base = f"/api/v1/namespaces/{self.namespace}"
        apps = f"/apis/apps/v1/namespaces/{self.namespace}"
        path = {
            "service": f"{base}/services/{name}",
            "pod": f"{base}/pods/{name}",
            "deployment": f"{apps}/deployments/{name}",
            "statefulset": f"{apps}/statefulsets/{name}",
            "daemonset": f"{apps}/daemonsets/{name}",
        }[kind]
        value = self._read(path)
        if value is None:
            raise CheckError("check-target-not-found", f"{kind}/{name} not found")
        return value

    def resolve_port(self, target: str) -> int:
        """The first declared port of a live Service, workload or pod."""
        kind, name = split_target(target, HTTP_KINDS)
        value = self._object(kind, name)
        spec = value.get("spec") or {}
        if kind == "service":
            ports = [item.get("port") for item in spec.get("ports") or ()]
        else:
            pod = spec if kind == "pod" else (spec.get("template") or {}).get("spec")
            ports = [
                port.get("containerPort")
                for container in (pod or {}).get("containers") or ()
                for port in container.get("ports") or ()
            ]
        for port in ports:
            if isinstance(port, int):
                return port
        raise CheckError(
            "check-port-unknown", f"{target} declares no port; set port = …"
        )

    def ready_pod(self, target: str) -> str:
        """The pod a command runs in: the pod itself, or a workload's newest ready pod."""
        kind, name = split_target(target, EXEC_KINDS)
        if kind == "pod":
            self._object(kind, name)
            return name
        workload = self._object(kind, name)
        labels = ((workload.get("spec") or {}).get("selector") or {}).get(
            "matchLabels"
        ) or {}
        if not labels:
            raise CheckError(
                "check-target-not-found", f"{target} has no matchLabels selector"
            )
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        listing = self._read(
            f"/api/v1/namespaces/{self.namespace}/pods", [("labelSelector", selector)]
        )
        candidates = []
        for pod in (listing or {}).get("items") or ():
            metadata = pod.get("metadata") or {}
            status = pod.get("status") or {}
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions") or ()
            )
            if (
                ready
                and status.get("phase") == "Running"
                and not metadata.get("deletionTimestamp")
            ):
                candidates.append(
                    (str(metadata.get("creationTimestamp", "")), metadata["name"])
                )
        if not candidates:
            raise CheckError("check-target-not-found", f"{target} has no ready pod")
        return max(candidates)[1]

    # ------------------------------------------------------------ reaching
    def forward(
        self, target: str, port: int | None = None
    ) -> AbstractContextManager[str]:
        """A temporary forward to ``target``; yields ``http://127.0.0.1:PORT``.

        ``port`` defaults to the target's first declared port. The forward is
        stopped when the ``with`` block ends.
        """
        kind, name = split_target(target, HTTP_KINDS)
        remote = port if port is not None else self.resolve_port(f"{kind}/{name}")
        return self._forwarder(self, f"{kind}/{name}", remote)

    def http_get(
        self,
        target: str,
        path: str = "/",
        *,
        port: int | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse:
        """One ``GET path`` to ``target`` through a temporary forward."""
        with self.forward(target, port) as url:
            return http_get(url, path, timeout=timeout)

    def exec(
        self,
        target: str,
        command: Sequence[str],
        *,
        container: str | None = None,
        timeout: float = 30.0,
    ) -> ExecResult:
        """Run ``command`` (an argv) in a ready pod of ``target``."""
        if isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty argv list")
        return self._executor(self, target, list(command), container, timeout)


def http_get(base_url: str, path: str, *, timeout: float) -> HttpResponse:
    """GET ``base_url + path`` (a loopback ``http://host:port``)."""
    host, _, port = base_url.removeprefix("http://").partition(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(_MAX_BODY)
        return HttpResponse(response.status, body)
    except TimeoutError:
        raise CheckError("check-timed-out", f"GET {path} timed out") from None
    except (OSError, http.client.HTTPException) as error:
        raise CheckError(
            "check-forward-unavailable", f"GET {path} failed: {type(error).__name__}"
        ) from None
    finally:
        connection.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def supervised_forward(context: CheckContext, target: str, port: int) -> Iterator[str]:
    """The default forwarder: a supervised ``kubectl port-forward`` on loopback.

    Uses the context's explicit kubeconfig and context; waits until the
    supervisor's TCP probe finds the forward healthy, then yields its URL and
    stops the process group afterwards.
    """
    from piceli.k8s.observe import ForwardSupervisor, PortForward

    local = _free_port()
    name = "piceli-check"
    supervisor = ForwardSupervisor(
        kubeconfig=context.kubeconfig, context=context.context, kubectl=context.kubectl
    )
    try:
        supervisor.add_or_update(
            PortForward(name, context.namespace, target, local, port), persist=False
        )
        supervisor.start(name)
        deadline = time.monotonic() + context.forward_seconds
        while True:
            status = supervisor.statuses()[0]
            if status.health == "healthy":
                break
            if status.state == "failed" or time.monotonic() >= deadline:
                raise CheckError(
                    "check-forward-unavailable",
                    f"port forward to {target}:{port} did not become healthy"
                    + (f" ({status.last_error})" if status.last_error else ""),
                )
            time.sleep(0.1)
        yield f"http://127.0.0.1:{local}"
    finally:
        supervisor.close()


def _limit(data: str) -> str:
    return data[:_MAX_OUTPUT]


_EXEC_PROTOCOL = "v4.channel.k8s.io"
_STDOUT, _STDERR, _STATUS = 1, 2, 3


def open_exec_socket(
    client: Any,
    namespace: str,
    pod: str,
    command: Sequence[str],
    container: str | None,
    timeout: float,
) -> Any:
    """Open a ``pods/exec`` websocket with the client's own TLS and credentials.

    ``kubernetes.stream`` builds its websocket from ``Configuration`` file
    paths and ignores the in-memory TLS context of clients built by
    :func:`~piceli.k8s.ops.provider_factory.api_client_from_kubeconfig`, so
    it cannot verify the cluster. This connects through ``websocket-client``
    with the connection pool's verifying ``ssl_context`` instead, and the
    ``Authorization`` header from the client's (refreshing) auth settings.
    """
    import ssl
    from urllib.parse import quote, urlencode

    import websocket  # websocket-client, a kubernetes dependency

    configuration = client.configuration
    headers: dict[str, str] = {}
    # Refreshes an exec-plugin credential (and its TLS context) when stale.
    client.update_params_for_auth(headers, [], ["BearerToken"])
    query = [("command", argument) for argument in command]
    query += [("stdout", "true"), ("stderr", "true")]
    if container is not None:
        query.append(("container", container))
    host = str(configuration.host).rstrip("/")
    scheme, _, rest = host.partition("://")
    if scheme not in {"https", "http"}:
        raise ValueError("unsupported API server scheme")
    url = (
        ("wss" if scheme == "https" else "ws")
        + "://"
        + rest
        + f"/api/v1/namespaces/{quote(namespace, safe='')}"
        + f"/pods/{quote(pod, safe='')}/exec?"
        + urlencode(query)
    )
    header = [
        f"{key}: {value}"
        for key, value in headers.items()
        if key.lower() == "authorization"
    ]
    sslopt: dict[str, Any] = {}
    if scheme == "https":
        pool = getattr(client.rest_client, "pool_manager", None)
        context = getattr(pool, "connection_pool_kw", {}).get("ssl_context")
        if context is None:
            context = ssl.create_default_context(
                cafile=configuration.ssl_ca_cert or None
            )
            if configuration.cert_file:
                context.load_cert_chain(
                    configuration.cert_file, configuration.key_file or None
                )
        sslopt["context"] = context
        if getattr(configuration, "tls_server_name", None):
            sslopt["server_hostname"] = configuration.tls_server_name
    return websocket.create_connection(
        url,
        timeout=timeout,
        header=header,
        subprotocols=[_EXEC_PROTOCOL],
        sslopt=sslopt,
        enable_multithread=False,
    )


def read_exec(socket_: Any, target: str, timeout: float) -> ExecResult:
    """Collect stdout, stderr and the exit status from a ``v4`` exec stream."""
    import websocket

    stdout: list[bytes] = []
    stderr: list[bytes] = []
    status_raw = b""
    sizes = [0, 0]
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(
                    "check-timed-out", f"exec in {target} ran longer than {timeout}s"
                )
            # One timeout per frame: a timed-out read may have consumed part
            # of a frame, so it ends the command instead of being retried.
            socket_.settimeout(remaining)
            try:
                opcode, data = socket_.recv_data()
            except websocket.WebSocketTimeoutException:
                raise CheckError(
                    "check-timed-out", f"exec in {target} ran longer than {timeout}s"
                ) from None
            except websocket.WebSocketConnectionClosedException:
                break
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode not in (websocket.ABNF.OPCODE_BINARY, websocket.ABNF.OPCODE_TEXT):
                continue
            if isinstance(data, str):
                data = data.encode()
            if not data:
                continue
            channel, payload = data[0], data[1:]
            if channel in (_STDOUT, _STDERR):
                index = channel - 1
                if sizes[index] < _MAX_OUTPUT:
                    (stdout if channel == _STDOUT else stderr).append(payload)
                    sizes[index] += len(payload)
            elif channel == _STATUS and len(status_raw) < _MAX_OUTPUT:
                status_raw += payload
    finally:
        socket_.close()
    try:
        status = json.loads(status_raw or b"{}") or {}
    except ValueError:
        status = {}
    if not isinstance(status, dict):
        status = {}
    if status.get("status") == "Success":
        code = 0
    else:
        causes = (status.get("details") or {}).get("causes") or []
        exit_codes = [
            cause.get("message")
            for cause in causes
            if isinstance(cause, Mapping) and cause.get("reason") == "ExitCode"
        ]
        if not exit_codes:
            raise CheckError(
                "check-exec-unavailable",
                f"exec did not run: {status.get('reason') or 'unknown reason'}",
            )
        try:
            code = int(str(exit_codes[0]))
        except ValueError:
            raise CheckError(
                "check-exec-unavailable", "exec reported an unreadable exit code"
            ) from None
    return ExecResult(
        code,
        _limit(b"".join(stdout).decode(errors="replace")),
        _limit(b"".join(stderr).decode(errors="replace")),
    )


def api_exec(
    context: CheckContext,
    target: str,
    command: Sequence[str],
    container: str | None,
    timeout: float,
) -> ExecResult:
    """The default executor: ``pods/exec`` through the Kubernetes API."""
    import websocket

    pod = context.ready_pod(target)
    try:
        socket_ = open_exec_socket(
            context.api_client(),
            context.namespace,
            pod,
            command,
            container,
            min(timeout, context.request_seconds),
        )
    except websocket.WebSocketBadStatusException as error:
        raise CheckError(
            "check-exec-unavailable",
            f"exec refused with HTTP {error.status_code}",
        ) from None
    except Exception as error:  # transport details may contain credentials
        raise CheckError(
            "check-exec-unavailable", f"exec failed: {type(error).__name__}"
        ) from None
    return read_exec(socket_, target, timeout)
