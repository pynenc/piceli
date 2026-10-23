"""Piceli's own image upload to Glance (0.25.3): retries, progress, clean-up, checks.

Against a fake Keystone and Glance (:mod:`.fake_glance`) and, for the build
host, a fake container CLI that runs the sender here as the host would.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import Image, ImageHost, Infrastructure, OpenStack, PrimaryIp, Server
from piceli.infra.machines import glance as glance_module
from piceli.infra.machines import image_upload
from piceli.infra.machines.glance import (
    Glance,
    HostSender,
    LocalSender,
    ProgressLine,
    host_argv,
    upload_image,
)
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.openstack import ID_ENV, IMAGE_SHA_TAG, SECRET_ENV
from piceli.infra.machines.plan import INVENTORY, Workspace, apply, plan
from piceli.infra.machines.render import render
from piceli.infra.machines.state import read_record, write_record
from piceli.infra.machines.tofu import TofuResult

from .fake_glance import CRED_ID, CRED_SECRET, REGION, FakeCloud, fake_cloud

DATA = bytes(range(256)) * (12 * 1024)  # 3 MiB: several chunks
SHA = hashlib.sha256(DATA).hexdigest()
STORE_FILE = "/nix/store/" + "a" * 32 + "-nixos-image/nixos.qcow2"
RUNNER = "registry.example.com/builder@sha256:" + "b" * 64
TAGS = [
    "piceli.io:infra=web",
    "piceli.io:managed-by=piceli",
    "piceli.io:resource=image-base",
]


@pytest.fixture
def cloud() -> Any:
    with fake_cloud() as found:
        yield found


@pytest.fixture
def disk(tmp_path: Path) -> Path:
    path = tmp_path / "disk.qcow2"
    path.write_bytes(DATA)
    return path


def _glance(cloud: FakeCloud) -> Glance:
    return Glance(cloud.auth_url, REGION, CRED_ID, CRED_SECRET)


def _body() -> dict[str, Any]:
    return {"name": "base", "container_format": "bare", "disk_format": "qcow2",
            "visibility": "private", "tags": [*TAGS, IMAGE_SHA_TAG + SHA]}  # fmt: skip


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.lines: list[str] = []

    def progress(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    def log(self, line: str) -> None:
        self.lines.append(line)


def _upload(cloud: FakeCloud, sender: Any, record: Recorder, **extra: Any) -> Any:
    return upload_image(
        _glance(cloud), name="base", sha256=SHA, body=_body(), sender=sender,
        tags=TAGS, progress=record.progress, log=record.log,
        sleep=lambda _s: None, **extra,
    )  # fmt: skip


def _no_secret(cloud: FakeCloud, *texts: str) -> None:
    for text in texts:
        assert CRED_SECRET not in text
        for token in cloud.issued:
            assert token not in text


def test_the_happy_path_uploads_verifies_and_revokes_its_token(
    cloud: FakeCloud, disk: Path
) -> None:
    record = Recorder()
    done = _upload(cloud, LocalSender(disk), record)
    image = cloud.images[done.id]
    assert image["status"] == "active" and image["size"] == len(DATA)
    assert set(TAGS) <= set(image["tags"])
    assert done.attempts == 1 and done.bytes == len(DATA)
    assert record.events[-1]["sent"] == record.events[-1]["total"] == len(DATA)
    # The upload's token is its own and revoked; Piceli's calls used another.
    upload_tokens = [t for t in cloud.revoked if t]
    assert len(upload_tokens) == 1
    _no_secret(cloud, "\n".join(record.lines), json.dumps(done.to_dict()))


def test_a_dropped_connection_is_retried_on_a_new_record(
    cloud: FakeCloud, disk: Path
) -> None:
    """The report: the connection dropped and Glance left the image in
    ``saving``. That record is deleted and the upload starts over."""
    cloud.drops, cloud.drop_after, cloud.after_drop = 1, 1 << 20, "saving"
    record = Recorder()
    done = _upload(cloud, LocalSender(disk), record)
    assert done.attempts == 2
    assert list(cloud.images) == [done.id]  # the half-made one is gone
    assert len(cloud.deleted) == 1
    assert any("saving; deleted" in line for line in record.lines)
    assert [e["attempt"] for e in record.events][-1] == 2


def test_a_record_glance_put_back_to_queued_is_reused(
    cloud: FakeCloud, disk: Path
) -> None:
    cloud.drops, cloud.drop_after, cloud.after_drop = 1, 1 << 20, "queued"
    done = _upload(cloud, LocalSender(disk), Recorder())
    assert done.attempts == 2 and cloud.deleted == []
    assert list(cloud.images) == [done.id]


def test_retries_exhausted_delete_the_image(cloud: FakeCloud, disk: Path) -> None:
    cloud.drops, cloud.drop_after, cloud.after_drop = 5, 1 << 20, "saving"
    record = Recorder()
    with pytest.raises(InfraError) as caught:
        _upload(cloud, LocalSender(disk), record)
    assert caught.value.code == "infra-image-upload-failed"
    assert cloud.images == {}
    assert len(cloud.deleted) == 3
    _no_secret(cloud, str(caught.value), "\n".join(record.lines))


def test_an_interrupt_deletes_the_record_it_made(cloud: FakeCloud, disk: Path) -> None:
    def interrupt(event: Mapping[str, Any]) -> None:
        if event["sent"] >= 1 << 20:
            raise KeyboardInterrupt

    def send(
        request: Mapping[str, Any], progress: Any = None, clock: Any = None
    ) -> Any:
        return original(request, progress, clock=iter(range(10**6)).__next__)

    original = glance_module.image_sender.send
    sender = LocalSender(disk)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(glance_module.image_sender, "send", send)
        with pytest.raises(KeyboardInterrupt):
            upload_image(
                _glance(cloud), name="base", sha256=SHA, body=_body(), sender=sender,
                tags=TAGS, progress=interrupt, sleep=lambda _s: None,
            )  # fmt: skip
    assert cloud.images == {}
    assert len(cloud.deleted) == 1


def test_a_checksum_mismatch_deletes_the_image(cloud: FakeCloud, disk: Path) -> None:
    cloud.corrupt = True
    with pytest.raises(InfraError) as caught:
        _upload(cloud, LocalSender(disk), Recorder())
    assert caught.value.code == "infra-image-checksum"
    assert cloud.images == {}


def test_a_wrong_file_is_refused_before_anything_is_created(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    other = tmp_path / "other.qcow2"
    other.write_bytes(b"something else")
    with pytest.raises(InfraError) as caught:
        _upload(cloud, LocalSender(other), Recorder())
    assert caught.value.code == "infra-image-checksum"
    with pytest.raises(InfraError) as caught:
        _upload(cloud, LocalSender(tmp_path / "missing"), Recorder())
    assert caught.value.code == "infra-image-unavailable"
    assert not any(r.startswith("POST /image") for r in cloud.requests)


def test_an_image_on_someone_elses_tags_is_never_deleted(cloud: FakeCloud) -> None:
    glance = _glance(cloud)
    theirs = cloud.add(name="base", tags=["piceli.io:infra=shop"], status="saving")
    mine = cloud.add(name="base", tags=list(TAGS), status="saving")
    removed = glance_module.remove_stale(glance, TAGS, keep=[])
    assert [item["id"] for item in removed] == [mine]
    assert theirs in cloud.images


# ------------------------------------------------------------------ build host

#: Runs the sender as the build host would, with the store file mapped here.
PRELUDE = """
import io, json, sys
request = json.loads(sys.stdin.read())
request["file"] = {mapping!r}.get(request["file"], request["file"])
sys.stdin = io.StringIO(json.dumps(request))
"""


class FakeSsh:
    """A container CLI over SSH: records argv, runs the sender here."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.argvs: list[list[str]] = []
        self.stdin = io.BytesIO()

    def __call__(self, argv: list[str], **options: Any) -> subprocess.Popen[bytes]:
        self.argvs.append(list(argv))
        assert argv[argv.index("-c") - 2] == RUNNER  # the run image, then python
        sender = f"exec(compile({argv[-1]!r}, '<sender>', 'exec'), {{'__name__': '__main__'}})"
        source = PRELUDE.format(mapping=self.mapping) + sender
        return subprocess.Popen([sys.executable, "-I", "-c", source], **options)


def _host() -> ImageHost:
    return ImageHost("build-1", context="ssh://builder@build-1", image=RUNNER,
                     path=STORE_FILE)  # fmt: skip


def test_the_build_host_sends_it_with_the_token_on_stdin_only(
    cloud: FakeCloud, disk: Path
) -> None:
    ssh = FakeSsh({STORE_FILE: str(disk)})
    sender = HostSender(_host(), popen=ssh)
    record = Recorder()
    done = _upload(cloud, sender, record)
    assert done.where == "build host build-1"
    assert cloud.images[done.id]["status"] == "active"
    assert len(ssh.argvs) == 2  # check, then send
    argv = ssh.argvs[1]
    assert argv[:3] == ["docker", "--host", "ssh://builder@build-1"]
    assert "/nix:/nix:ro" in argv and "--cap-drop" in argv
    assert record.events and record.events[-1]["sent"] == len(DATA)
    _no_secret(cloud, json.dumps(ssh.argvs), "\n".join(record.lines))


def test_a_host_that_lost_the_file_creates_nothing(cloud: FakeCloud) -> None:
    sender = HostSender(_host(), popen=FakeSsh({}))
    with pytest.raises(InfraError) as caught:
        _upload(cloud, sender, Recorder())
    assert caught.value.code == "infra-image-unavailable"
    assert "upload='local'" in str(caught.value)
    assert cloud.images == {}


def test_the_host_argv_carries_no_secret() -> None:
    argv = host_argv(_host())
    assert "run" in argv and "--rm" in argv and "-i" in argv
    assert not any("token" in item.lower() for item in argv[:-1])


# ------------------------------------------------------------------ progress


def test_progress_is_printed_at_most_once_a_second() -> None:
    now = [0.0]
    lines: list[tuple[str, bool]] = []
    line = ProgressLine(lambda text, final: lines.append((text, final)),
                        clock=lambda: now[0])  # fmt: skip
    total = 100 << 20
    for step in range(1, 41):
        now[0] = step * 0.25
        line({"image": "base", "sent": step * (1 << 20), "total": total, "attempt": 1})
    assert len(lines) == 10  # 10 seconds, at most once each
    text = lines[-1][0]
    assert "image base: 40.0 MB of 100.0 MB (40%)" in text
    assert "MB/s" in text and "ETA" in text
    line({"image": "base", "sent": total, "total": total, "attempt": 1})
    assert lines[-1][1] is True  # the last one ends the line


# ------------------------------------------------------------------ plan and apply


def _provider(cloud: FakeCloud) -> OpenStack:
    return OpenStack(credentials="cloud", auth_url=cloud.auth_url, region=REGION,
                     public_network="public", flavors=["small"])  # fmt: skip


def _infra(cloud: FakeCloud, disk: Path, state: Path, **image: Any) -> Infrastructure:
    declared = Image("base", file=disk, sha256=SHA, **image)
    server = Server("host-1", provider=_provider(cloud), type="small", image=declared,
                    ipv4=PrimaryIp("host-1-ip"))  # fmt: skip
    return Infrastructure("web", servers=[server], state_dir=state)


IMAGE = "openstack_images_image_v2.image-base"
SERVER = "openstack_compute_instance_v2.server-host-1"
PORT = "openstack_networking_port_v2.ip-host-1-ip"


def _document(*changes: tuple[str, list[str], Any]) -> dict[str, Any]:
    return {"format_version": "1.2", "resource_changes": [
        {"address": a, "mode": "managed", "type": a.split(".")[0],
         "change": {"actions": actions, "before": before, "after": {}}}
        for a, actions, before in changes
    ]}  # fmt: skip


class Tofu:
    """Plans that change once the image is imported; records every call."""

    version_text = "1.12.5"

    def __init__(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        self.before, self.after = before, after
        self.imported: str | None = None
        self.calls: list[list[str]] = []
        self.planned: list[list[str]] = []

    def run(
        self, args: Any, *, cwd: Path, env: Any, timeout: float = 0, **_: Any
    ) -> Any:
        args = list(args)
        self.calls.append(args)
        if args[0] == "plan":
            self.planned.append(args)
            Path(next(a for a in args if a.startswith("-out="))[5:]).write_text("plan")
        if args[0] == "import":
            self.imported = args[-1]
        if args[0] == "show":
            if len(args) == 3:
                return TofuResult(
                    0, json.dumps(self.after if self.imported else self.before), ""
                )
            return TofuResult(0, json.dumps({"values": {"root_module": {"resources": [
                {"address": SERVER, "mode": "managed", "type": SERVER.split(".")[0],
                 "values": {"image_id": self.imported}},
                {"address": IMAGE, "mode": "managed", "type": IMAGE.split(".")[0],
                 "values": {"id": self.imported, "image_id": self.imported,
                            "tags": [*TAGS, IMAGE_SHA_TAG + SHA]}},
            ]}}}), "")  # fmt: skip
        return TofuResult(0, "", "")


def _workspace(infra: Infrastructure, tofu: Tofu, directory: Path) -> Workspace:
    directory.mkdir(parents=True, exist_ok=True)
    env = {ID_ENV: CRED_ID, SECRET_ENV: CRED_SECRET}
    return Workspace(infra, directory, tofu, render(infra), env)  # type: ignore[arg-type]


def test_the_plan_shows_the_upload_and_the_apply_adopts_it(
    cloud: FakeCloud, disk: Path, tmp_path: Path
) -> None:
    infra = _infra(cloud, disk, tmp_path / "state")
    tofu = Tofu(
        _document((IMAGE, ["create"], None), (PORT, ["create"], None),
                  (SERVER, ["create"], None)),
        _document((PORT, ["create"], None), (SERVER, ["create"], None)),
    )  # fmt: skip
    ws = _workspace(infra, tofu, tmp_path / "state")
    planned = plan(ws, prices=False)
    entry = planned.to_dict()["images"][0]
    assert entry == {"name": "base", "address": IMAGE, "action": "upload",
                     "from": "this machine", "bytes": len(DATA), "sha256": SHA}  # fmt: skip
    said: list[str] = []
    record = Recorder()
    done, inventory = apply(
        ws, planned.plan_hash, say=said.append, progress=record.progress
    )
    assert tofu.imported in cloud.images
    assert ["import", "-input=false", "-no-color", IMAGE, tofu.imported] in tofu.calls
    assert inventory["uploaded_images"][0]["id"] == tofu.imported
    log = tmp_path / "state" / "image-upload-base.log"
    assert any(str(log) in line for line in said)
    assert IMAGE in read_record(tmp_path / "state", INVENTORY)["created"]
    _no_secret(
        cloud,
        "\n".join(said),
        log.read_text(),
        json.dumps(inventory),
        json.dumps(planned.to_dict()),
    )


def test_a_stale_saving_image_is_replaced(
    cloud: FakeCloud, disk: Path, tmp_path: Path
) -> None:
    """An earlier run's upload left the image in ``saving``: the plan
    replaces it (``-replace``), the apply deletes it before the upload."""
    old = cloud.add(name="base", tags=[*TAGS, IMAGE_SHA_TAG + SHA], status="saving")
    leftover = cloud.add(name="base", tags=list(TAGS), status="queued")
    before = {"id": old, "status": "saving", "tags": [*TAGS, IMAGE_SHA_TAG + SHA]}
    infra = _infra(cloud, disk, tmp_path / "state")
    tofu = Tofu(
        _document((IMAGE, ["no-op"], before), (SERVER, ["create"], None)),
        _document((SERVER, ["create"], None)),
    )
    ws = _workspace(infra, tofu, tmp_path / "state")
    write_record(ws.directory, INVENTORY, {"created": [IMAGE]})
    # OpenTofu replaces it when asked to.
    replaced = _document(
        (IMAGE, ["delete", "create"], before), (SERVER, ["create"], None)
    )
    original = tofu.run

    def run(args: Any, **options: Any) -> Any:
        if list(args)[0] == "plan" and f"-replace={IMAGE}" in args:
            tofu.before = replaced
        return original(args, **options)

    tofu.run = run  # type: ignore[method-assign]
    planned = plan(ws, prices=False)
    assert any(f"-replace={IMAGE}" in call for call in tofu.planned)
    entry = planned.to_dict()["images"][0]
    assert entry["action"] == "replace"
    assert entry["reason"] == "a failed upload left it in saving"
    assert planned.to_dict()["stale_images"] == [
        {"image": "base", "id": leftover, "status": "queued"}
    ]
    said: list[str] = []
    apply(ws, planned.plan_hash, say=said.append)
    assert old not in cloud.images and leftover not in cloud.images
    assert ["state", "rm", IMAGE] in tofu.calls
    assert list(cloud.images) == [tofu.imported]


def test_a_new_sha256_replaces_an_active_image_and_retires_it(
    cloud: FakeCloud, disk: Path, tmp_path: Path
) -> None:
    old_sha = "c" * 64
    old = cloud.add(name="base", tags=[*TAGS, IMAGE_SHA_TAG + old_sha], status="active")
    before = {"id": old, "status": "active", "tags": [*TAGS, IMAGE_SHA_TAG + old_sha]}
    infra = _infra(cloud, disk, tmp_path / "state")
    tofu = Tofu(
        _document((IMAGE, ["update"], before)),
        _document((SERVER, ["delete", "create"], {"image_id": old})),
    )
    ws = _workspace(infra, tofu, tmp_path / "state")
    write_record(ws.directory, INVENTORY, {"created": [IMAGE, SERVER]})
    replaced = _document((IMAGE, ["delete", "create"], before),
                         (SERVER, ["delete", "create"], {"image_id": old}))  # fmt: skip
    original = tofu.run

    def run(args: Any, **options: Any) -> Any:
        if list(args)[0] == "plan" and f"-replace={IMAGE}" in args:
            tofu.before = replaced
        return original(args, **options)

    tofu.run = run  # type: ignore[method-assign]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("piceli.infra.machines.plan.owned", lambda *_a, **_k: True)
        planned = plan(ws, prices=False)
        assert planned.to_dict()["images"][0]["reason"] == "its sha256 changed"
        said: list[str] = []
        apply(ws, planned.plan_hash, say=said.append)
    assert old not in cloud.images  # deleted once the apply succeeded
    assert f"deleted the replaced image {old}" in said
    assert image_upload.retired(ws) is False


def test_plan_and_upload_never_contact_a_cloud_for_a_named_image(
    tmp_path: Path,
) -> None:
    provider = OpenStack(credentials="cloud", auth_url="http://127.0.0.1:9/v3",
                         region=REGION, public_network="public", flavors=["small"])  # fmt: skip
    server = Server("host-1", provider=provider, type="small", image="debian-12",
                    ipv4=PrimaryIp("host-1-ip"))  # fmt: skip
    infra = Infrastructure("web", servers=[server], state_dir=tmp_path)
    ws = Workspace(infra, tmp_path, None, render(infra), {})  # type: ignore[arg-type]
    document = _document((SERVER, ["create"], None))
    assert image_upload.image_entries(ws, document) == ()
    assert image_upload.replacements(ws, document) == []


# ------------------------------------------------------------------ declaration


def _sidecar(directory: Path, **extra: Any) -> Path:
    path = directory / "base.image.json"
    body = {"schema": "piceli.nix-image.v1", "file": "run/piceli-nix-out/nixos.qcow2",
            "sha256": SHA, "format": "qcow2", "bytes": len(DATA),
            "store_path": STORE_FILE.rsplit("/", 1)[0], **extra}  # fmt: skip
    path.write_text(json.dumps(body))
    return path


def test_from_build_uploads_from_the_host_that_built_it(tmp_path: Path) -> None:
    built_on = {"host": "build-1", "kind": "docker", "context": "ssh://builder@build-1",
                "cli": "docker", "image": RUNNER}  # fmt: skip
    sidecar = _sidecar(tmp_path, built_on=built_on, store_file=STORE_FILE)
    image = Image.from_build("base", sidecar)
    assert image.host == _host() and image.size == len(DATA)
    assert image.describe()["upload_from"] == "build-1"
    assert Image.from_build("base", sidecar, upload="local").host is None
    # A sidecar before 0.25.3 (or of a local host): the local copy.
    assert Image.from_build("base", _sidecar(tmp_path)).host is None
    broken = _sidecar(tmp_path, built_on={**built_on, "image": "unpinned"},
                      store_file=STORE_FILE)  # fmt: skip
    with pytest.raises(InfraError) as caught:
        Image.from_build("base", broken)
    assert caught.value.code == "infra-image-build-invalid"


def test_the_sidecar_names_the_docker_host_and_the_store_file(tmp_path: Path) -> None:
    from piceli.dev.model import BuildHost
    from piceli.k8s.cli.dev_nix import _built_on, _image_sidecar

    host = BuildHost.docker("build-1", context="ssh://builder@build-1")

    class Profile:
        image = RUNNER

    local = tmp_path / "artifacts" / "run-1" / "piceli-nix-out" / "nixos.qcow2"
    found = {"file": str(local), "sha256": SHA, "format": "qcow2", "bytes": len(DATA)}
    store = STORE_FILE.rsplit("/", 1)[0]
    path = _image_sidecar(tmp_path / "artifacts", "base", found, store, "a" * 40,
                          ".#x", built_on=_built_on(host, Profile()))  # fmt: skip
    body = json.loads(path.read_text())
    assert body["store_file"] == STORE_FILE
    assert body["built_on"]["context"] == "ssh://builder@build-1"
    assert Image.from_build("base", path).host == _host()
    # A local docker engine or a native host: the local copy is the image.
    assert _built_on(BuildHost.docker("here"), Profile()) is None


def test_the_rendered_image_carries_its_sha256_and_ignores_its_file(
    cloud: FakeCloud, disk: Path, tmp_path: Path
) -> None:
    rendered = render(_infra(cloud, disk, tmp_path))
    body = rendered.config["resource"]["openstack_images_image_v2"]["image-base"]
    assert IMAGE_SHA_TAG + SHA in body["tags"]
    assert body["lifecycle"] == {
        "ignore_changes": ["local_file_path", "image_cache_path"]
    }
    record = _provider(cloud).image_record(body)
    assert record["tags"] == body["tags"] and record["visibility"] == "private"
    assert "lifecycle" not in record and "local_file_path" not in record


def test_a_failed_apply_after_the_upload_reports_the_adopted_image(
    cloud: FakeCloud, disk: Path, tmp_path: Path
) -> None:
    infra = _infra(cloud, disk, tmp_path / "state")
    tofu = Tofu(
        _document((IMAGE, ["create"], None), (SERVER, ["create"], None)),
        _document((SERVER, ["create"], None)),
    )
    original = tofu.run

    def run(args: Any, **options: Any) -> Any:
        if list(args)[0] == "apply":
            return TofuResult(1, "", "quota exceeded")
        return original(args, **options)

    tofu.run = run  # type: ignore[method-assign]
    ws = _workspace(infra, tofu, tmp_path / "state")
    planned = plan(ws, prices=False)
    with pytest.raises(InfraError) as caught:
        apply(ws, planned.plan_hash)
    fields = caught.value.fields
    assert fields["partially_applied"] is True
    assert fields["applied"] == [
        {"address": IMAGE, "action": "upload", "id": tofu.imported}
    ]
    assert fields["not_applied"] == [{"address": SERVER, "action": "create"}]
    assert tofu.imported in cloud.images  # adopted: the next plan keeps it
