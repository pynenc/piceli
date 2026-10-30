"""A server whose image changes: rebuilt in place or replaced (0.26.2).

``Server(on_image_change="rebuild")`` (the default) lets the provider
update the image in place: OpenStack rebuilds the server with Nova, and the
rebuilt host boots with its *original* user data. A plan that does so for a
server with ``UserData`` warns (a spent bootstrap key?) without blocking.
``Server(on_image_change="replace")`` plans ``-replace`` instead: the server
is destroyed and created again on its persistent port, with the current
user data. ``piceli infra replace INFRA SERVER`` plans the same for one
server, on request, through the normal plan, approval and hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import Infrastructure, OpenStack, PrimaryIp, Server, UserData
from piceli.infra.machines import credentials
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import INVENTORY, Workspace, apply, plan
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record
from piceli.infra.machines.tofu import TofuResult
from piceli.testing.infra import FakeProvider

from .machines_support import PASSPHRASE

OS = OpenStack(
    credentials="cloud",
    auth_url="https://identity.example.net/v3",
    region="region-1",
    public_network="public",
    flavors=["small"],
)
SERVER = "openstack_compute_instance_v2.server-web-1"
PORT = "openstack_networking_port_v2.ip-web-1-port"
BOOT = "#cloud-config\nruncmd:\n  - [tailscale, up, --auth-key=tskey-fresh]\n"
OURS = {"piceli.io:managed-by": "piceli", "piceli.io:infra": "web"}
WARNING = (
    "server web-1 is rebuilt in place with its original user data (a spent "
    'bootstrap key?): use Server(on_image_change="replace") or piceli infra replace'
)


def _server(**overrides: Any) -> Server:
    values: dict[str, Any] = {
        "name": "web-1",
        "provider": OS,
        "type": "small",
        "image": "debian-13",
        "ipv4": PrimaryIp("web-1-port"),
        "user_data": UserData("web-1-boot"),
    }
    values.update(overrides)
    return Server(**values)


class ImageTofu:
    """Plans like OpenStack's provider: a new image is an in-place update
    (a Nova rebuild); ``-replace=`` makes it delete and create."""

    version_text = "1.10.0"

    def __init__(self, *, image_change: bool = True) -> None:
        self.image_change = image_change
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, args: Any, *, cwd: Path, env: Any, timeout: float = 0) -> Any:
        args = list(args)
        self.calls.append((args, dict(env)))
        if args[0] == "plan":
            Path(next(a for a in args if a.startswith("-out="))[5:]).write_text("p")
            return TofuResult(0, "", "")
        if args[0] != "show":
            return TofuResult(0, "", "")
        plan_args = next(a for a, _ in reversed(self.calls) if a[0] == "plan")
        replaced = f"-replace={SERVER}" in plan_args
        before = {"name": "web-1", "image_name": "debian-12", "metadata": OURS}
        after = {
            **before,
            "image_name": "debian-13" if self.image_change else "debian-12",
        }
        if replaced:
            actions = ["delete", "create"]
        elif self.image_change:
            actions = ["update"]
        else:
            actions = ["no-op"]
        changes = [
            {
                "address": SERVER, "mode": "managed",
                "type": "openstack_compute_instance_v2",
                "change": {"actions": actions, "before": before, "after": after,
                           "after_unknown": {}},
            },
            {
                "address": PORT, "mode": "managed", "type": "openstack_networking_port_v2",
                "change": {"actions": ["no-op"], "before": {"tags": []},
                           "after": {"tags": []}},
            },
        ]  # fmt: skip
        if replaced:
            changes[0]["action_reason"] = "replace_by_request"
        return TofuResult(0, json.dumps({"resource_changes": changes}), "")


def _workspace(tmp_path: Path, tofu: ImageTofu, **server: Any) -> Workspace:
    infra = Infrastructure("web", servers=[_server(**server)], state_dir=tmp_path)
    rendered = render(infra)
    write_record(tmp_path, INVENTORY, {"created": sorted(rendered.resources)})
    return Workspace(infra, tmp_path, tofu, rendered, {})  # type: ignore[arg-type]


def _plans(tofu: ImageTofu) -> list[tuple[list[str], dict[str, str]]]:
    return [(args, env) for args, env in tofu.calls if args[0] == "plan"]


# ------------------------------------------------------------ the declaration


def test_on_image_change_defaults_to_rebuild_and_is_validated() -> None:
    assert _server().on_image_change == "rebuild"
    assert _server(on_image_change="replace").on_image_change == "replace"
    with pytest.raises(InfraError, match="on_image_change"):
        _server(on_image_change="recreate")
    # The default leaves the description (and the golden hashes) unchanged.
    assert "on_image_change" not in _server().describe()
    assert _server(on_image_change="replace").describe()["on_image_change"] == (
        "replace"
    )


def test_both_modes_render_the_same_server_on_its_persistent_port() -> None:
    rebuild = render(Infrastructure("web", servers=[_server()])).config
    replace = render(
        Infrastructure("web", servers=[_server(on_image_change="replace")])
    ).config
    assert rebuild == replace
    body = replace["resource"]["openstack_compute_instance_v2"]["server-web-1"]
    assert body["network"] == [{"port": f"${{{PORT}.id}}"}]
    assert "create_before_destroy" not in json.dumps(body)


# ------------------------------------------------------------ plans


def test_the_default_rebuilds_in_place_and_warns(homes: Path, tmp_path: Path) -> None:
    tofu = ImageTofu()
    result = plan(_workspace(tmp_path, tofu), prices=False)  # no user data stored
    assert [(c.address, c.action) for c in result.changes] == [(SERVER, "update")]
    assert result.foreign == ()
    assert len(_plans(tofu)) == 1
    assert result.user_data == ()
    assert result.warnings == (
        {"server": "web-1", "kind": "rebuild-reuses-user-data", "message": WARNING},
    )
    assert result.to_dict()["warnings"] == [dict(result.warnings[0])]


def test_no_warning_without_user_data_or_without_an_image_change(
    homes: Path, tmp_path: Path
) -> None:
    plain = plan(_workspace(tmp_path, ImageTofu(), user_data=None), prices=False)
    assert plain.warnings == () and "warnings" not in plain.to_dict()
    same = plan(_workspace(tmp_path, ImageTofu(image_change=False)), prices=False)
    assert same.changes == () and same.warnings == ()


def test_replace_mode_replaces_the_server_with_its_current_user_data(
    homes: Path, tmp_path: Path
) -> None:
    credentials.save_credential("web-1-boot", "user-data", BOOT)
    tofu = ImageTofu()
    ws = _workspace(tmp_path, tofu, on_image_change="replace")
    result = plan(ws, prices=False)
    assert [(c.address, c.action) for c in result.changes] == [(SERVER, "replace")]
    plans = _plans(tofu)
    assert f"-replace={SERVER}" not in plans[0][0]
    assert f"-replace={SERVER}" in plans[-1][0]
    assert plans[-1][1]["TF_VAR_user_data_web_1"] == BOOT
    assert [item["server"] for item in result.user_data] == ["web-1"]
    assert result.warnings == ()
    assert result.replaced == ({"server": "web-1", "why": "its image changed"},)
    assert result.to_dict()["replaced"] == [
        {"server": "web-1", "why": "its image changed"}
    ]


def test_replace_mode_needs_the_user_data(homes: Path, tmp_path: Path) -> None:
    ws = _workspace(tmp_path, ImageTofu(), on_image_change="replace")
    with pytest.raises(InfraError) as caught:
        plan(ws, prices=False)
    assert caught.value.code == "infra-user-data-missing"


def test_a_requested_replace_is_planned_and_hashed(homes: Path, tmp_path: Path) -> None:
    credentials.save_credential("web-1-boot", "user-data", BOOT)
    tofu = ImageTofu(image_change=False)
    ws = _workspace(tmp_path, tofu)
    result = plan(ws, prices=False, replace=["web-1"])
    assert f"-replace={SERVER}" in _plans(tofu)[0][0]
    assert [(c.address, c.action) for c in result.changes] == [(SERVER, "replace")]
    assert result.replaced == ({"server": "web-1", "why": "requested"},)
    assert [item["server"] for item in result.user_data] == ["web-1"]
    # Another plan (no replace) is another hash: approving one never runs the other.
    normal = plan(_workspace(tmp_path, ImageTofu(image_change=False)), prices=False)
    assert normal.plan_hash != result.plan_hash
    # The approval runs the same replace again.
    with pytest.raises(InfraError) as caught:
        apply(_workspace(tmp_path, ImageTofu(image_change=False)), result.plan_hash)
    assert caught.value.code == "infra-plan-changed"


def test_a_requested_replace_without_user_data_is_refused(
    homes: Path, tmp_path: Path
) -> None:
    ws = _workspace(tmp_path, ImageTofu(image_change=False))
    with pytest.raises(InfraError) as caught:
        plan(ws, prices=False, replace=["web-1"])
    assert caught.value.code == "infra-user-data-missing"


def test_replacing_an_unknown_server_is_refused(homes: Path, tmp_path: Path) -> None:
    ws = _workspace(tmp_path, ImageTofu())
    with pytest.raises(InfraError) as caught:
        plan(ws, prices=False, replace=["web-9"])
    assert caught.value.code == "infra-server-unknown"


# ------------------------------------------------------------ real OpenTofu


@pytest.mark.tofu
@pytest.mark.timeout(600)  # about 12 OpenTofu runs; slow under load
def test_real_tofu_rebuilds_or_replaces_on_a_new_image(homes: Path) -> None:
    """terraform_data updates its input in place, as Nova rebuilds a server."""
    from piceli.infra.machines.plan import workspace

    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    credentials.save_credential("web-1-boot", "user-data", BOOT)
    fake = FakeProvider()

    def infra(image: str, mode: str = "rebuild") -> Infrastructure:
        server = Server("web-1", provider=fake, type="t1", image=image,
                        ipv4=PrimaryIp("web-1-v4"), user_data=UserData("web-1-boot"),
                        on_image_change=mode)  # fmt: skip
        return Infrastructure("web", servers=[server], state_dir=homes / "state")

    address = "terraform_data.server-web-1"
    with workspace(infra("img-1")) as ws:
        validated = ws.run("validate", "-json")
        assert validated.code == 0, validated.stdout
        apply(ws, plan(ws, prices=False).plan_hash)
    with workspace(infra("img-2")) as ws:
        rebuilt = plan(ws, prices=False)
    assert [(c.address, c.action) for c in rebuilt.changes] == [(address, "update")]
    assert [w["server"] for w in rebuilt.warnings] == ["web-1"]
    assert rebuilt.user_data == ()
    with workspace(infra("img-2", "replace")) as ws:
        replaced = plan(ws, prices=False)
    assert [(c.address, c.action) for c in replaced.changes] == [(address, "replace")]
    assert replaced.warnings == ()
    assert [u["server"] for u in replaced.user_data] == ["web-1"]
    with workspace(infra("img-1")) as ws:
        requested = plan(ws, prices=False, replace=["web-1"])
        assert [(c.address, c.action) for c in requested.changes] == [
            (address, "replace")
        ]
        apply(ws, requested.plan_hash, replace=["web-1"])
        assert plan(ws, prices=False).changes == ()
        doomed = plan(ws, destroy=True)
        apply(ws, doomed.plan_hash, destroy=True)


# ------------------------------------------------------------ the command

_MODULE = """
from piceli.infra import Infrastructure, OpenStack, PrimaryIp, Server, UserData

cloud = OpenStack(credentials="cloud", auth_url="https://identity.example.net/v3",
                  region="region-1", public_network="public", flavors=["small"])
infra = Infrastructure("web", servers=[
    Server("web-1", provider=cloud, type="small", image="debian-13",
           ipv4=PrimaryIp("web-1-port"), user_data=UserData("web-1-boot")),
])
"""


def test_infra_replace_plans_then_applies_the_approved_replace(
    homes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from typer.testing import CliRunner

    from piceli.infra.machines import plan as infra_plan
    from piceli.k8s.cli import app

    credentials.save_credential("web-1-boot", "user-data", BOOT)
    (tmp_path / "machines.py").write_text(_MODULE)
    ref = f"{tmp_path / 'machines.py'}:infra"
    tofus: list[ImageTofu] = []

    @contextmanager
    def workspace(infra: Any, *, tofu: str | None = None) -> Any:
        tofus.append(ImageTofu(image_change=False))
        rendered = render(infra)
        write_record(tmp_path, INVENTORY, {"created": sorted(rendered.resources)})
        yield Workspace(infra, tmp_path, tofus[-1], rendered, {})  # type: ignore[arg-type]

    monkeypatch.setattr(infra_plan, "workspace", workspace)
    first = CliRunner().invoke(app, ["infra", "replace", ref, "web-1"])
    assert first.exit_code == 3, (first.stdout, first.stderr)
    body = json.loads(first.stdout)
    assert body["state"] == "approval-required"
    assert body["replaced"] == [{"server": "web-1", "why": "requested"}]
    assert body["approve_command"] == (
        f"piceli infra replace {ref} web-1 --approve {body['plan_hash']}"
    )
    assert f"replace  server web-1 ({SERVER})" in first.stderr
    assert "tskey-fresh" not in first.stdout + first.stderr
    done = CliRunner().invoke(
        app, ["infra", "replace", ref, "web-1", "--approve", body["plan_hash"]]
    )
    assert done.exit_code == 0, (done.stdout, done.stderr)
    assert json.loads(done.stdout.splitlines()[-1])["state"] == "applied"
    assert any(args[0] == "apply" for args, _ in tofus[-1].calls)
    unknown = CliRunner().invoke(app, ["infra", "replace", ref, "web-9"])
    assert unknown.exit_code == 2
    assert json.loads(unknown.stdout)["reason"] == "infra-server-unknown"
