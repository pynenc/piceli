"""Client-owned laptop port forwarding for an authenticated cluster UI ticket."""

from __future__ import annotations

import getpass
import ipaddress
import shutil
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer

from piceli.artifacts.process import ToolPin
from piceli.cli_contract import emit_json, reject
from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.owned_processes import OwnedProcessRegistry
from piceli.k8s.ui_config import UiShortcut
from piceli.k8s.ui_state import private_ui_state_dir
from piceli.services.contracts import (
    AccessSession,
    RemoteAccessLease,
    ResourceIdentity,
)
from piceli.services.query import KubernetesReader, _resource
from piceli.services.registration import Registration


class RemoteClientError(ValueError):
    """A bounded public refusal; no HTTP response body or secret escapes."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _server_url(value: str, *, allow_insecure_loopback_test: bool) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (
            parsed.path.strip("/")
            and not all(
                part.replace("-", "").replace("_", "").isalnum()
                for part in parsed.path.strip("/").split("/")
            )
        )
    ):
        raise RemoteClientError("ui-invalid-request")
    if parsed.scheme != "https":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if not (allow_insecure_loopback_test and parsed.scheme == "http" and local):
            raise RemoteClientError("ui-invalid-request")
    return value.rstrip("/") + "/"


class RemoteAccessClient:
    """Claim one ticket, own one supervised forward, and stop on lost lease."""

    def __init__(
        self,
        *,
        server: str,
        kubeconfig: Path,
        context: str,
        local_port: int,
        kubectl: Path,
        ca_file: Path | None = None,
        allow_insecure_loopback_test: bool = False,
        http_client: Any = None,
        verify_resource: Callable[[ResourceIdentity, KubeconfigTarget, int], None]
        | None = None,
        supervisor_factory: Callable[..., ForwardSupervisor] = ForwardSupervisor,
        probe: Callable[[int], bool] | None = None,
        poll_seconds: float = 2.0,
        registry: OwnedProcessRegistry | None = None,
    ) -> None:
        self.server = _server_url(
            server, allow_insecure_loopback_test=allow_insecure_loopback_test
        )
        if (
            not kubeconfig.is_absolute()
            or not kubeconfig.is_file()
            or not context
            or not 1 <= local_port <= 65535
            or (
                ca_file is not None
                and (not ca_file.is_absolute() or not ca_file.is_file())
            )
            or not 0 < poll_seconds <= 10
        ):
            raise RemoteClientError("ui-invalid-request")
        self.kubeconfig = kubeconfig
        self.context = context
        self.local_port = local_port
        self.kubectl = ToolPin.capture(kubectl)
        self.verify_resource = verify_resource or self._verify_resource
        self.supervisor_factory = supervisor_factory
        self.probe = probe or self._probe
        self.poll_seconds = poll_seconds
        self.registry = registry
        self._owns_http = http_client is None
        if http_client is None:
            try:
                import httpx2
            except ImportError:
                raise RemoteClientError("ui-assets-unavailable") from None
            self.http = httpx2.Client(
                base_url=self.server,
                timeout=10,
                trust_env=False,
                verify=str(ca_file) if ca_file else True,
            )
        else:
            self.http = http_client

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    @staticmethod
    def _probe(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            return False

    @staticmethod
    def _verify_resource(
        resource: ResourceIdentity, target: KubeconfigTarget, remote_port: int
    ) -> None:
        registration = Registration("remote-client", "Remote client", target)
        reader = KubernetesReader(registration)
        try:
            raw = next(
                (
                    item
                    for item in reader.list(
                        resource.api_version, resource.kind, resource.namespace
                    )
                    if item.get("metadata", {}).get("name") == resource.name
                ),
                None,
            )
            if raw is None or raw.get("metadata", {}).get("uid") != resource.uid:
                raise RemoteClientError("ui-observation-unavailable")
            live = _resource(registration, raw)
            if remote_port not in live.ports:
                raise RemoteClientError("ui-observation-unavailable")
        finally:
            reader.close()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(path, json=body)
        except Exception:
            raise RemoteClientError("ui-access-failed") from None
        if response.status_code == 404:
            raise RemoteClientError("ui-not-found")
        if response.status_code == 403:
            raise RemoteClientError("ui-request-rejected")
        if response.status_code >= 400:
            raise RemoteClientError("ui-access-failed")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid response")
            return data
        except (ValueError, TypeError):
            raise RemoteClientError("ui-access-failed") from None

    def _target(self, lease: RemoteAccessLease) -> KubeconfigTarget:
        target = lease.target
        if not target.cluster_uid or not target.namespace_uid:
            raise RemoteClientError("ui-observation-unavailable")
        if lease.session.resource.namespace != target.namespace:
            raise RemoteClientError("ui-observation-unavailable")
        return KubeconfigTarget(
            self.kubeconfig,
            self.context,
            target.namespace,
            cluster_uid=target.cluster_uid,
            namespace_uid=target.namespace_uid,
        )

    def run(
        self,
        ticket_id: str,
        pairing_secret: str,
        *,
        stop: threading.Event | None = None,
        on_state: Callable[[AccessSession], None] | None = None,
    ) -> AccessSession:
        if len(ticket_id) != 32 or any(c not in "0123456789abcdef" for c in ticket_id):
            raise RemoteClientError("ui-invalid-request")
        if not 40 <= len(pairing_secret) <= 128:
            raise RemoteClientError("ui-invalid-request")
        self.kubectl.verify()
        lease = RemoteAccessLease.model_validate(
            self._post(
                f"api/v1/remote-access/{ticket_id}/claim",
                {"pairing_secret": pairing_secret},
            )
        )
        supervisor: ForwardSupervisor | None = None
        last = lease.session
        try:
            target = self._target(lease)
            resource = lease.session.resource
            remote_port = lease.session.remote_port
            if remote_port is None:
                raise RemoteClientError("ui-access-failed")
            self.verify_resource(resource, target, remote_port)
            shortcut = UiShortcut(
                id="remote-" + ticket_id,
                label="Remote Piceli access",
                target=f"{resource.kind.lower()}/{resource.name}",
                namespace=resource.namespace,
                local_port=self.local_port,
                remote_port=remote_port,
            )
            supervisor = self.supervisor_factory(
                kubeconfig=target.kubeconfig,
                context=target.context,
                kubectl=str(self.kubectl.path),
                shortcuts=(shortcut,),
                namespace=target.namespace,
                registry=self.registry,
            )
            supervisor.quick_start(shortcut.id, target.namespace)
            stop = stop or threading.Event()
            until = datetime.fromisoformat(lease.session.expires_at)
            while datetime.now(UTC) < until and not stop.is_set():
                self.kubectl.verify()
                statuses = supervisor.statuses()
                status = statuses[0] if statuses else None
                if status is not None and status.state == "failed":
                    raise RemoteClientError("ui-access-failed")
                ready = bool(
                    status is not None
                    and status.state == "running"
                    and status.reachable
                    and self.probe(self.local_port)
                )
                if ready:
                    self.verify_resource(resource, target, remote_port)
                last = AccessSession.model_validate(
                    self._post(
                        f"api/v1/remote-access/{ticket_id}/heartbeat",
                        {
                            "lease_secret": lease.lease_secret,
                            "state": "ready" if ready else "connecting",
                            "local_port": self.local_port if ready else None,
                        },
                    )
                )
                if on_state is not None:
                    on_state(last)
                if last.state not in {"connecting", "ready"}:
                    raise RemoteClientError("ui-access-failed")
                stop.wait(self.poll_seconds)
            return last
        finally:
            try:
                if supervisor is not None:
                    supervisor.close()
            finally:
                try:
                    self._post(
                        f"api/v1/remote-access/{ticket_id}/release",
                        {"lease_secret": lease.lease_secret},
                    )
                except RemoteClientError:
                    pass


def connect(
    server: Annotated[str, typer.Option(help="Cluster UI HTTPS origin and prefix")],
    ticket: Annotated[str, typer.Option(help="Pending remote-access ticket ID")],
    kubeconfig: Annotated[Path, typer.Option(help="Explicit local kubeconfig file")],
    context: Annotated[str, typer.Option(help="Explicit local kubeconfig context")],
    local_port: Annotated[int, typer.Option(min=1, max=65535)],
    ca_file: Annotated[
        Path | None, typer.Option(help="Optional trusted UI server CA file")
    ] = None,
    kubectl: Annotated[
        Path | None, typer.Option(help="Pinned kubectl executable")
    ] = None,
) -> None:
    """Bind a laptop port for a cluster UI ticket, then supervise it until stopped."""
    executable = kubectl or (
        Path(found) if (found := shutil.which("kubectl")) else None
    )
    if executable is None:
        reject("ui-operation-unavailable")
    try:
        forwards = OwnedProcessRegistry(private_ui_state_dir() / "forwards")
        forwards.reap_orphans()
        client = RemoteAccessClient(
            server=server,
            kubeconfig=kubeconfig.resolve(strict=True),
            context=context,
            local_port=local_port,
            kubectl=executable.resolve(strict=True),
            ca_file=ca_file.resolve(strict=True) if ca_file else None,
            registry=forwards,
        )
        secret = getpass.getpass("One-time pairing secret: ")
        try:
            previous: str | None = None

            def report(session: AccessSession) -> None:
                nonlocal previous
                if session.state != previous:
                    emit_json(
                        {
                            "state": session.state,
                            "binding_location": "local_client",
                            "endpoint": session.endpoint,
                            "ticket": session.id,
                        }
                    )
                    previous = session.state

            client.run(ticket, secret, on_state=report)
        finally:
            client.close()
        emit_json({"state": "stopped", "ticket": ticket})
    except KeyboardInterrupt:
        emit_json({"state": "stopped", "ticket": ticket})
    except RemoteClientError as error:
        reject(error.code)
    except (OSError, ValueError):
        reject("ui-invalid-request")
