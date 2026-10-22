"""Secret user data at creation, and the OpenStack flavor allow-list.

``Server(user_data=UserData(NAME))`` renders a sensitive OpenTofu variable
(default ``null``) that only a plan creating the server fills, from the
credential NAME through ``TF_VAR_*``; later plans never load it and never
replace the server for it. The plan names it with a short digest.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import (
    Hetzner,
    Infomaniak,
    Infrastructure,
    OpenStack,
    PrimaryIp,
    Server,
    UserData,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import INVENTORY, Workspace, plan, user_data_digest
from piceli.infra.machines.provider import Resource
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record
from piceli.infra.machines.tofu import TofuResult, redact
from piceli.testing.infra import FakeProvider

from .machines_support import PASSPHRASE, last_json, needs_tofu, planned

KEY = "tskey-auth-kExample-" + "q" * 40
CLOUD_CONFIG = f'#cloud-config\nruncmd:\n  - [tailscale, up, "--auth-key={KEY}"]\n'
OS = OpenStack(
    credentials="cloud",
    auth_url="https://identity.example.net/v3",
    region="region-1",
    public_network="public",
    flavors=["small"],
)
SERVER = "openstack_compute_instance_v2.server-web-1"


def _server(**overrides: Any) -> Server:
    values: dict[str, Any] = {
        "name": "web-1",
        "provider": OS,
        "type": "small",
        "image": "debian-12",
        "ipv4": PrimaryIp("web-1-port"),
        "user_data": UserData("web-1-boot"),
    }
    values.update(overrides)
    return Server(**values)


def test_user_data_is_a_sensitive_variable_ignored_after_creation() -> None:
    rendered = render(Infrastructure("web", servers=[_server()]))
    config = rendered.config
    assert config["variable"] == {
        "user_data_web_1": {
            "type": "string",
            "default": None,
            "sensitive": True,
            "description": "user data of web-1 (creation only)",
        }
    }
    body = config["resource"]["openstack_compute_instance_v2"]["server-web-1"]
    assert body["user_data"] == "${var.user_data_web_1}"
    assert body["lifecycle"] == {"ignore_changes": ["user_data"]}
    assert rendered.user_data == {SERVER: ("web-1", "web-1-boot")}
    assert (
        Infrastructure("web", servers=[_server()]).describe()["servers"][0]["user_data"]
        == "web-1-boot"
    )
    plain = render(Infrastructure("web", servers=[_server(user_data=None)])).config
    assert "variable" not in plain


def test_user_data_keeps_a_protected_lifecycle() -> None:
    server = Resource("openstack_compute_instance_v2", "server-web-1",
                      {"name": "web-1", "lifecycle": {"prevent_destroy": True}})  # fmt: skip
    resource = OS.render_user_data(server, "${var.x}")
    assert resource.body["lifecycle"] == {
        "prevent_destroy": True,
        "ignore_changes": ["user_data"],
    }
    assert resource.body["user_data"] == "${var.x}"


def test_user_data_is_refused_where_it_cannot_go() -> None:
    hetzner = Hetzner(credentials="h", location="fsn1")
    with pytest.raises(InfraError, match="no user data"):
        Server("web-1", provider=hetzner, type="cax11", image="debian-12",
               user_data=UserData("web-1-boot"))  # fmt: skip
    with pytest.raises(InfraError, match="credential name"):
        UserData("not a name/")
    with pytest.raises(InfraError, match="UserData"):
        _server(user_data="web-1-boot")


# ------------------------------------------------------------ flavors


def test_flavors_are_an_allow_list() -> None:
    with pytest.raises(InfraError, match=r"'big' is not one of small"):
        _server(type="big")
    with pytest.raises(InfraError, match="flavors"):
        OpenStack(credentials="c", auth_url="https://x.example/v3", region="r",
                  public_network="p")  # fmt: skip
    with pytest.raises(InfraError, match="flavors"):
        OpenStack(credentials="c", auth_url="https://x.example/v3", region="r",
                  public_network="p", flavors="small")  # fmt: skip
    with pytest.raises(InfraError, match="flavors"):
        OpenStack(credentials="c", auth_url="https://x.example/v3", region="r",
                  public_network="p", flavors=["has space"])  # fmt: skip
    cloud = Infomaniak(credentials="cloud")
    assert cloud.flavors == ("a1-ram2-disk20-perf1",)
    Server("web-1", provider=cloud, type="a1-ram2-disk20-perf1", image="debian-12")
    with pytest.raises(InfraError, match=r"Infomaniak\(flavors=...\) adds one"):
        Server("web-1", provider=cloud, type="a2-ram4-disk50-perf1", image="debian-12")
    bigger = Infomaniak(
        credentials="cloud", flavors=["a2-ram4-disk50-perf1", "a1-ram2-disk20-perf1"]
    )
    assert bigger.flavors == ("a1-ram2-disk20-perf1", "a2-ram4-disk50-perf1")
    Server("web-1", provider=bigger, type="a2-ram4-disk50-perf1", image="debian-12")


# ------------------------------------------------------------ plans


class TwoPassTofu:
    """Plans like OpenTofu would: ``after.user_data`` is the value's SHA-1 when set."""

    version_text = "1.10.0"

    def __init__(self, action: str) -> None:
        self.action = action
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, args: Any, *, cwd: Path, env: Any, timeout: float = 0) -> Any:
        import hashlib

        args = list(args)
        self.calls.append((args, dict(env)))
        if args[0] == "plan":
            Path(next(a for a in args if a.startswith("-out="))[5:]).write_text("p")
        if args[0] == "show":
            value = env.get("TF_VAR_user_data_web_1")
            document = planned((SERVER, self.action, None), infra="web")
            after: dict[str, Any] = {"user_data": None}
            if value is not None:
                after["user_data"] = hashlib.sha1(value.encode()).hexdigest()
            document["resource_changes"][0]["change"]["after"] = after
            document["variables"] = {"user_data_web_1": {"value": value}}
            text = json.dumps(document)
            secrets = [v for k, v in env.items() if k.startswith("TF_VAR_")]
            return TofuResult(0, redact(text, secrets), "")
        return TofuResult(0, "", "")


def _workspace(tmp_path: Path, action: str) -> tuple[Workspace, TwoPassTofu]:
    infra = Infrastructure("web", servers=[_server()], state_dir=tmp_path)
    rendered = render(infra)
    write_record(tmp_path, INVENTORY, {"created": sorted(rendered.resources)})
    tofu = TwoPassTofu(action)
    return Workspace(infra, tmp_path, tofu, rendered, {}), tofu  # type: ignore[arg-type]


def _plans(tofu: TwoPassTofu) -> list[dict[str, str]]:
    return [env for args, env in tofu.calls if args[0] == "plan"]


def test_a_plan_that_keeps_the_server_never_loads_the_user_data(
    homes: Path, tmp_path: Path
) -> None:
    ws, tofu = _workspace(tmp_path, "no-op")
    result = plan(ws, prices=False)  # no credential stored: not needed
    assert len(_plans(tofu)) == 1
    assert result.user_data == ()
    assert result.to_dict()["user_data"] == []


def test_a_plan_that_creates_the_server_needs_it(homes: Path, tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, "create")
    with pytest.raises(InfraError) as caught:
        plan(ws, prices=False)
    assert caught.value.code == "infra-user-data-missing"
    assert "piceli secrets user-data web-1-boot" in str(caught.value)


@pytest.mark.parametrize("action", ["create", "replace"])
def test_a_creating_plan_passes_it_in_the_environment_only(
    homes: Path, tmp_path: Path, action: str
) -> None:
    credentials.save_credential("web-1-boot", "user-data", CLOUD_CONFIG)
    ws, tofu = _workspace(tmp_path, action)
    result = plan(ws, prices=False)
    first, second = _plans(tofu)
    assert "TF_VAR_user_data_web_1" not in first
    assert second["TF_VAR_user_data_web_1"] == CLOUD_CONFIG
    assert result.user_data == (
        {"server": "web-1", "secret": "web-1-boot",
         "digest": user_data_digest(CLOUD_CONFIG)},
    )  # fmt: skip
    text = json.dumps(result.to_dict()) + ws.rendered.config_text
    assert KEY not in text and "tskey" not in text
    assert all(KEY not in arg for args, _ in tofu.calls for arg in args)
    # The approval covers the user data: another value is another hash.
    again = plan(ws, prices=False)
    assert again.plan_hash == result.plan_hash
    credentials.save_credential("web-1-boot", "user-data", CLOUD_CONFIG + "#\n")
    other, _ = _workspace(tmp_path, action)
    assert plan(other, prices=False).plan_hash != result.plan_hash


def test_redaction_covers_json_escaped_secrets() -> None:
    text = json.dumps({"value": CLOUD_CONFIG}) + "\n" + CLOUD_CONFIG
    redacted = redact(text, [CLOUD_CONFIG])
    assert KEY not in redacted
    assert redacted.count("<redacted>") == 2


# ------------------------------------------------------------ the secret


def test_user_data_credentials_hold_whole_documents(homes: Path) -> None:
    path = credentials.save_credential("web-1-boot", "user-data", CLOUD_CONFIG)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.load_credential("web-1-boot", "user-data") == CLOUD_CONFIG
    for bad in ("", " \n", "x" * (credentials.MAX_USER_DATA + 1), "a\x00b"):
        with pytest.raises(InfraError, match="user data"):
            credentials.save_credential("web-1-boot", "user-data", bad)
    with pytest.raises(InfraError) as caught:
        credentials.load_credential("missing", "user-data")
    assert "piceli secrets user-data missing --file PATH" in str(caught.value)


def _cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "piceli", "secrets", "user-data", *args],
        input=stdin, capture_output=True, text=True, env=os.environ.copy(),
        timeout=120,
    )  # fmt: skip


def test_secrets_user_data_command(homes: Path, tmp_path: Path) -> None:
    source = tmp_path / "boot.yaml"
    source.write_text(CLOUD_CONFIG)
    done = _cli("web-1-boot", "--file", str(source))
    assert done.returncode == 0, done.stderr
    body = last_json(done.stdout)
    assert body["kind"] == "user-data"
    assert body["digest"] == user_data_digest(CLOUD_CONFIG)
    assert KEY not in done.stdout + done.stderr
    assert credentials.load_credential("web-1-boot", "user-data") == CLOUD_CONFIG
    piped = _cli("web-1-key", "--prompt", stdin=KEY + "\n")
    assert piped.returncode == 0, piped.stderr
    assert KEY not in piped.stdout + piped.stderr
    assert credentials.load_credential("web-1-key", "user-data") == KEY + "\n"
    refused = _cli("web-1-key", KEY)
    assert refused.returncode == 2
    assert KEY not in refused.stdout + refused.stderr
    assert _cli("web-1-key").returncode == 2


# ------------------------------------------------------------ real OpenTofu


@needs_tofu
@pytest.mark.timeout(240)
def test_real_tofu_loads_user_data_only_to_create(homes: Path) -> None:
    """Created with the value; then planned without it, nothing changes."""
    from piceli.infra.machines.plan import apply, workspace

    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    credentials.save_credential("web-1-boot", "user-data", CLOUD_CONFIG)
    server = Server("web-1", provider=FakeProvider(), type="t1", image="img",
                    user_data=UserData("web-1-boot"))  # fmt: skip
    infra = Infrastructure("web", servers=[server], state_dir=homes / "state")
    with workspace(infra) as ws:
        first = plan(ws, prices=False)
        assert [c.action for c in first.changes] == ["create"]
        assert first.user_data[0]["digest"] == user_data_digest(CLOUD_CONFIG)
        shown = ws.run("show", "-json")
        apply(ws, first.plan_hash)
    assert KEY not in shown.stdout
    (homes / "credentials" / "web-1-boot.json").unlink()
    with workspace(infra) as ws:
        later = plan(ws, prices=False)
        assert later.changes == () and later.user_data == ()
        assert "TF_VAR_user_data_web_1" not in ws.env
        doomed = plan(ws, destroy=True)
        apply(ws, doomed.plan_hash, destroy=True)
    # terraform_data keeps the value in the (encrypted) state; OpenStack's
    # provider keeps only its SHA-1. Piceli's own records never hold it.
    for path in (homes / "state").rglob("*"):
        if path.is_file() and ".terraform" not in path.parts:
            assert KEY.encode() not in path.read_bytes(), path
