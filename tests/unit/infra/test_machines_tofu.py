"""``piceli infra`` with a real OpenTofu and the fake provider (``terraform_data``).

Nothing is created anywhere and no provider is downloaded: the fake
provider renders OpenTofu's built-in resource. The state is real and
encrypted. Skipped without OpenTofu (``PICELI_TOFU`` or ``tofu`` on PATH).
"""

from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from piceli.infra.machines import credentials
from piceli.infra.machines.state import encryption_env
from piceli.infra.machines.tofu import locate
from piceli.k8s.cli import app as cli

from .machines_support import (
    PASSPHRASE,
    last_json,
    needs_tofu,
    tofu_binary,
    write_module,
)

pytestmark = [needs_tofu, pytest.mark.timeout(240)]
runner = CliRunner()
OURS = [
    "terraform_data.firewall-edge-1",
    "terraform_data.ip-edge-1-v4",
    "terraform_data.record-example-com-www-a",
    "terraform_data.server-edge-1",
]


def piceli(*args: str) -> tuple[Result, dict[str, Any]]:
    result = runner.invoke(cli, list(args))
    return result, last_json(result.stdout)


@pytest.fixture
def module(homes: Path) -> str:
    credentials.save_credential("edge-state", "state-key", PASSPHRASE)
    return f"{write_module(homes)}:infra"


def _apply(ref: str) -> dict[str, Any]:
    result, body = piceli("infra", "plan", ref)
    assert result.exit_code == 3, result.output
    result, done = piceli("infra", "apply", ref, "--approve", body["plan_hash"])
    assert result.exit_code == 0, result.output
    return done


def _state_text(homes: Path) -> str:
    return (homes / "state" / "terraform.tfstate").read_text()


def _no_secret_anywhere(homes: Path, *outputs: str) -> None:
    for text in outputs:
        assert PASSPHRASE not in text
    for path in (homes / "state").rglob("*"):
        if path.is_file() and ".terraform" not in path.parts:
            assert PASSPHRASE.encode() not in path.read_bytes(), path


def test_plan_approve_apply_status_destroy(homes: Path, module: str) -> None:
    result, plan = piceli("infra", "plan", module)
    assert result.exit_code == 3, result.output
    assert plan["state"] == "approval-required"
    assert [c["address"] for c in plan["changes"]] == OURS
    assert plan["summary"]["create"] == 4
    assert plan["estimate"]["monthly_net"] == pytest.approx(3.79)
    assert plan["approve_command"].endswith(f"--approve {plan['plan_hash']}")

    wrong, refused = piceli("infra", "apply", module, "--approve", "sha256:" + "0" * 64)
    assert wrong.exit_code == 2 and refused["reason"] == "infra-plan-changed"
    assert not (homes / "state" / "terraform.tfstate").exists()

    applied_result, applied = piceli(
        "infra", "apply", module, "--approve", plan["plan_hash"]
    )
    assert applied_result.exit_code == 0, applied_result.output
    assert applied["state"] == "applied"
    assert applied["servers"]["edge-1"]["ipv4"] == "127.0.0.1"
    # Encrypted at rest: no address, no label, no name in the clear.
    text = _state_text(homes)
    assert "encrypted_data" in text
    assert (
        "127.0.0.1" not in text
        and "piceli.io/infra" not in text
        and "edge-1" not in text
    )

    again, unchanged = piceli("infra", "plan", module)
    assert again.exit_code == 0 and unchanged["state"] == "unchanged"

    shown, status = piceli("infra", "status", module, "--json")
    assert shown.exit_code == 0
    assert status["state"] == "applied"
    server = status["servers"][0]
    assert (
        server["ipv4"] == "127.0.0.1"
        and server["state"] == "created"
        and server["fixed_ipv4"]
    )
    assert server["cluster"]["registered"] is False
    assert status["records"][0]["state"] == "created"
    assert status["estimate"]["monthly_net"] == pytest.approx(3.79)

    plan_d_result, plan_d = piceli("infra", "destroy", module)
    assert plan_d_result.exit_code == 3
    assert [c["address"] for c in plan_d["changes"]] == OURS
    assert {
        c["action"] if "action" in c else c["actions"][0] for c in plan_d["changes"]
    } == {"delete"}
    assert plan_d["plan_hash"] != plan["plan_hash"]
    gone_result, gone = piceli(
        "infra", "destroy", module, "--approve", plan_d["plan_hash"]
    )
    assert gone_result.exit_code == 0, gone_result.output
    assert gone["state"] == "destroyed" and gone["servers"] == {}
    _, after = piceli("infra", "status", module, "--json")
    assert after["state"] == "not-applied"
    _no_secret_anywhere(
        homes,
        *(
            r.output
            for r in (
                result,
                wrong,
                applied_result,
                again,
                shown,
                plan_d_result,
                gone_result,
            )
        ),
    )


def test_a_wrong_passphrase_cannot_read_the_state(homes: Path, module: str) -> None:
    _apply(module)
    credentials.save_credential(
        "edge-state", "state-key", "another-passphrase-of-length"
    )
    result, body = piceli("infra", "plan", module)
    assert result.exit_code == 2
    assert body["reason"] == "infra-tofu-failed"
    assert "another-passphrase-of-length" not in result.output


def _intrude(homes: Path) -> None:
    """Put a resource Piceli never declared into the state, the way a person could."""
    directory = homes / "state"
    tofu = locate(tofu_binary())
    main = directory / "main.tf.json"
    saved = main.read_text()
    config = json.loads(saved)
    config["resource"]["terraform_data"]["intruder"] = {"input": {"labels": {}}}
    main.write_text(json.dumps(config))
    try:
        done = tofu.run(
            ["apply", "-auto-approve", "-input=false", "-json"],
            cwd=directory,
            env=encryption_env(PASSPHRASE),
        )
        assert done.code == 0, done.stderr
    finally:
        main.write_text(saved)


def _addresses_in_state(homes: Path) -> set[str]:
    tofu = locate(tofu_binary())
    shown = tofu.run(
        ["show", "-json"], cwd=homes / "state", env=encryption_env(PASSPHRASE)
    )
    values = json.loads(shown.stdout).get("values") or {}
    return {
        r["address"] for r in (values.get("root_module") or {}).get("resources") or ()
    }


def test_destroy_never_touches_what_piceli_did_not_create(
    homes: Path, module: str
) -> None:
    _apply(module)
    _intrude(homes)
    assert "terraform_data.intruder" in _addresses_in_state(homes)

    planned, body = piceli("infra", "destroy", module)
    assert planned.exit_code == 2, planned.output
    assert body["reason"] == "infra-foreign-resource"
    assert body["foreign"] == ["terraform_data.intruder"]

    # An apply plan (which would delete it as undeclared) is refused too.
    applying, refused = piceli("infra", "apply", module)
    assert applying.exit_code == 2 and refused["reason"] == "infra-foreign-resource"

    # Nothing ran: ours and the intruder are all still there.
    assert _addresses_in_state(homes) == {*OURS, "terraform_data.intruder"}


def test_stray_configuration_in_the_state_directory_is_refused(
    homes: Path, module: str
) -> None:
    _apply(module)
    (homes / "state" / "extra.tf.json").write_text("{}")
    result, body = piceli("infra", "plan", module)
    assert result.exit_code == 2 and body["reason"] == "infra-state-unexpected"


def test_another_command_holds_the_lock(homes: Path, module: str) -> None:
    from piceli.infra.machines.state import locked

    (homes / "state").mkdir(mode=0o700)
    with locked(homes / "state"):
        result, body = piceli("infra", "plan", module)
    assert result.exit_code == 2 and body["reason"] == "infra-state-locked"


# ------------------------------------------------------------ remote state


class _StateStore:
    def __init__(self, password: str) -> None:
        self.password = password
        self.body: bytes | None = None
        self.locked = False
        self.auth: list[bool] = []
        self.methods: list[str] = []


@contextmanager
def _state_server(store: _StateStore) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            expected = (
                "Basic "
                + base64.b64encode(f"piceli:{store.password}".encode()).decode()
            )
            ok = self.headers.get("Authorization") == expected
            store.auth.append(ok)
            store.methods.append(self.command)
            if not ok:
                self._reply(401)
            return ok

        def _reply(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self) -> None:
            if self._authorized():
                self._reply(200, store.body) if store.body else self._reply(404)

        def do_POST(self) -> None:
            if self._authorized():
                store.body = self._read()
                self._reply(200)

        def do_DELETE(self) -> None:
            if self._authorized():
                store.body = None
                self._reply(200)

        def do_LOCK(self) -> None:
            if self._authorized():
                self._read()
                if store.locked:
                    self._reply(423)
                else:
                    store.locked = True
                    self._reply(200)

        def do_UNLOCK(self) -> None:
            if self._authorized():
                self._read()
                store.locked = False
                self._reply(200)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/state/edge"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_http_state_receives_ciphertext_only(homes: Path) -> None:
    password = "state-store-password-for-tests"
    credentials.save_credential("edge-state", "state-key", PASSPHRASE)
    credentials.save_credential("state-store", "provider-token", password)
    store = _StateStore(password)
    with _state_server(store) as url:
        extra = (
            "from piceli.infra import HttpState\n"
            f"BACKEND = HttpState({url!r}, lock_address={url!r}, credentials='state-store')\n"
        )
        path = write_module(homes, extra=extra)
        path.write_text(
            path.read_text().replace(
                'state_dir=HERE / "state",',
                'state_dir=HERE / "state", backend=BACKEND,',
            )
        )
        ref = f"{path}:infra"
        result, plan = piceli("infra", "plan", ref)
        assert result.exit_code == 3, result.output
        applied, body = piceli("infra", "apply", ref, "--approve", plan["plan_hash"])
        assert applied.exit_code == 0, applied.output
    assert store.body is not None and b"encrypted_data" in store.body
    assert b"127.0.0.1" not in store.body
    assert store.auth and all(store.auth)
    assert "LOCK" in store.methods and not store.locked
    assert not (homes / "state" / "terraform.tfstate").exists()
    assert password not in result.output + applied.output


def test_a_changed_backend_is_refused(homes: Path, module: str) -> None:
    _apply(module)
    path = Path(module.rsplit(":", 1)[0])
    text = path.read_text().replace(
        'state_dir=HERE / "state",',
        'state_dir=HERE / "state", backend=__import__("piceli.infra", fromlist=["HttpState"]).HttpState("http://127.0.0.1:9/x"),',
    )
    other = path.with_name("machines_http.py")
    other.write_text(text)
    result, body = piceli("infra", "plan", f"{other}:infra")
    assert result.exit_code == 2 and body["reason"] == "infra-backend-changed"


# ------------------------------------------------------------ hcloud provider

needs_network = pytest.mark.skipif(
    os.environ.get("PICELI_TOFU_NETWORK") != "1",
    reason="downloads the pinned hcloud provider (set PICELI_TOFU_NETWORK=1)",
)


@needs_network
def test_hetzner_rendering_plans_with_the_pinned_provider(homes: Path) -> None:
    """The real hcloud provider (pinned, hashes verified) accepts what Piceli renders.

    The API is a loopback mock: ``tofu plan`` of new resources needs no
    call, and the prices come from the mock's ``/pricing``.
    """
    from piceli.infra import DnsRecord, Hetzner, Infrastructure, PrimaryIp, Rule, Server
    from piceli.infra.machines.plan import plan, workspace
    from piceli.testing.infra import mock_hetzner_api

    from .machines_support import TOKEN

    credentials.save_credential("edge-state", "state-key", PASSPHRASE)
    credentials.save_credential("hcloud", "provider-token", TOKEN)
    with mock_hetzner_api(TOKEN) as api:
        hcloud = Hetzner(credentials="hcloud", location="fsn1", endpoint=api.url)
        server = Server(
            "edge-1", provider=hcloud, type="cax11", image="debian-12",
            ipv4=PrimaryIp("edge-1-v4"), firewall=[Rule.tcp(443), Rule.udp((3478, 3479)), Rule.icmp()],
            ssh_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests owner@example"],
        )  # fmt: skip
        infra = Infrastructure(
            "edge",
            servers=[server],
            records=[
                DnsRecord("example.com", "www", "A", server=server),
                DnsRecord(
                    "example.com", "@", "TXT", value="v=spf1 -all", provider=hcloud
                ),
            ],
            state_dir=homes / "state",
        )
        with workspace(infra) as ws:
            result = plan(ws)
            validated = ws.run("validate", "-json")
    assert validated.code == 0, validated.stdout
    assert json.loads(validated.stdout)["warning_count"] == 0
    assert {c.address for c in result.changes} == {
        "hcloud_firewall.firewall-edge-1",
        "hcloud_primary_ip.ip-edge-1-v4",
        "hcloud_server.server-edge-1",
        "hcloud_ssh_key.key-" + next(c.name for c in result.changes if c.kind == "key"),
        "hcloud_zone_rrset.record-example-com-www-a",
        "hcloud_zone_rrset.record-example-com-apex-txt",
    }
    assert result.estimate is not None and result.estimate[
        "monthly_net"
    ] == pytest.approx(4.29)
    assert ("GET", "/v1/pricing", True) in api.requests
