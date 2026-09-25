"""Direct node delivery with fake runners: no ssh, Docker or containerd needed."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from piceli.artifacts import cli
from piceli.artifacts.delivery import (
    ArchiveSource,
    DeliveryGrant,
    DeliveryRejected,
    DockerImageSource,
    NodeDelivery,
    append_journal,
    normalize_reference,
    relay_image_stream,
    write_receipt,
)
from piceli.artifacts.node_transport import (
    NodeTarget,
    RunResult,
    SubprocessRunner,
    Transport,
)
from piceli.artifacts.process import ProcessLimits, ToolPin

DOCKER_TARGET = "docker://node-1?runtime=containerd"
SSH_TARGET = "ssh://ops@node-1.example:2222?runtime=k3s-containerd"
MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def add(archive: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    archive.addfile(info, io.BytesIO(body))


def layer_and_config(marker: bytes, architecture: str = "arm64") -> tuple[bytes, bytes]:
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        add(archive, "hello.txt", marker)
    config = json.dumps(
        {
            "architecture": architecture,
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [sha(layer.getvalue())]},
        }
    ).encode()
    return layer.getvalue(), config


def docker_archive(
    marker: bytes = b"hi",
    tags: list[str] | None = None,
    architecture: str = "arm64",
) -> tuple[bytes, str]:
    """A classic ``docker save`` tar: index members last, like Docker writes it."""
    layer, config = layer_and_config(marker, architecture)
    digest = sha(config)
    layer_dir = hashlib.sha256(layer).hexdigest()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        add(archive, f"{layer_dir}/layer.tar", layer)
        add(archive, f"{digest[7:]}.json", config)
        entry = {
            "Config": f"{digest[7:]}.json",
            "RepoTags": ["example/app:1"] if tags is None else tags,
            "Layers": [f"{layer_dir}/layer.tar"],
        }
        add(archive, "manifest.json", json.dumps([entry]).encode())
        add(archive, "repositories", b"{}")
    return out.getvalue(), digest


def oci_archive(
    marker: bytes = b"oci", architecture: str = "arm64"
) -> tuple[bytes, str]:
    layer, config = layer_and_config(marker, architecture)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": sha(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": sha(layer),
                    "size": len(layer),
                }
            ],
        }
    ).encode()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": sha(manifest),
                "size": len(manifest),
            }
        ],
    }
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for body in (layer, config, manifest):
            add(archive, "blobs/sha256/" + sha(body)[7:], body)
        add(archive, "index.json", json.dumps(index).encode())
        add(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
    return out.getvalue(), sha(config)


class FakeNode:
    """Emulates the local Docker CLI and a node's ``ctr`` behind any transport."""

    def __init__(self, images: dict[str, tuple[str, bytes]] | None = None) -> None:
        self.local = images or {}
        self.containerd_store = False
        self.names: dict[str, str] = {}
        self.content: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.imports: list[bytes] = []
        self.import_state = "succeeded"
        self.tamper: bytes | None = None

    # Runner protocol --------------------------------------------------------
    def capture(self, argv, limits, env):
        self.calls.append(argv)
        self.envs.append(dict(env))
        words = argv[-1].split() if argv[0].endswith("ssh") else argv
        if "inspect" in words:
            known = self.local.get(argv[-1])
            if known is None:
                return RunResult("failed", 1, b"", 0.0)
            return RunResult("succeeded", 0, known[0].encode() + b"\n", 0.0)
        if "info" in words:
            store = b'[["driver-type","io.containerd.snapshotter.v1"]]'
            return RunResult(
                "succeeded", 0, store if self.containerd_store else b"[]", 0.0
            )
        if words[-2:] == ["images", "ls"]:
            rows = ["REF TYPE DIGEST SIZE PLATFORMS LABELS"]
            rows += [
                f"{name} {MANIFEST} {digest} 4.1 MiB linux/arm64 -"
                for name, digest in self.names.items()
            ]
            return RunResult("succeeded", 0, "\n".join(rows).encode(), 0.0)
        if "content" in words:
            body = self.content.get(words[-1].strip("'"))
            if body is None:
                return RunResult("failed", 1, b"", 0.0)
            return RunResult("succeeded", 0, body, 0.0)
        raise AssertionError(argv)

    def read(self, argv, reader, limits, env, cancel=None):
        self.calls.append(argv)
        self.envs.append(dict(env))
        assert argv[-2] == "save"
        archive = next(body for ident, body in self.local.values() if ident == argv[-1])
        return RunResult("succeeded", 0, b"", 0.0), reader(io.BytesIO(archive))

    def feed(self, argv, writer, limits, env, cancel=None):
        self.calls.append(argv)
        self.envs.append(dict(env))
        sink = io.BytesIO()
        writer(sink)  # exceptions propagate exactly like the real runner
        received = sink.getvalue()
        self.imports.append(received)
        self._import(self.tamper or received)
        return RunResult(self.import_state, 0, b"", 0.0)

    # ctr import emulation ---------------------------------------------------
    def _import(self, received: bytes) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(received)) as archive:
                files = {
                    m.name: archive.extractfile(m).read() for m in archive if m.isfile()
                }
        except tarfile.TarError:
            return  # a truncated stream fails to import
        if "manifest.json" not in files:
            return  # "unrecognized image format": nothing is created
        (entry,) = json.loads(files["manifest.json"])
        config = files[entry["Config"]]
        manifest = json.dumps(
            {"mediaType": MANIFEST, "config": {"digest": sha(config)}}
        ).encode()
        self.content[sha(manifest)] = manifest
        for tag in entry["RepoTags"]:
            self.names[normalize_reference(tag)] = sha(manifest)


@pytest.fixture
def docker_pin(tmp_path: Path) -> ToolPin:
    tool = tmp_path / "docker"
    tool.write_bytes(b"#!/bin/sh\n")
    return ToolPin.capture(tool)


@pytest.fixture
def ssh_pin(tmp_path: Path) -> ToolPin:
    tool = tmp_path / "ssh"
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    return ToolPin.capture(tool)


def delivery(node: FakeNode, docker_pin: ToolPin, **kwargs) -> NodeDelivery:
    return NodeDelivery(
        docker=docker_pin,
        docker_socket=Path("/run/docker.sock"),
        runner=node,
        **kwargs,
    )


def grant(digest: str, target: str = DOCKER_TARGET) -> DeliveryGrant:
    return DeliveryGrant(digest, target, time.time() + 60)


def test_reference_normalization_matches_containerd_names():
    assert normalize_reference("app") == "docker.io/library/app:latest"
    assert normalize_reference("team/app:1") == "docker.io/team/app:1"
    assert normalize_reference("registry.test:5000/a/b:v2") == (
        "registry.test:5000/a/b:v2"
    )
    assert normalize_reference("localhost/app:x") == "localhost/app:x"
    for bad in ("app@sha256:" + "0" * 64, "-oProxy", "UPPER/app:1", "a b"):
        with pytest.raises(ValueError):
            normalize_reference(bad)


def test_target_parsing_and_runtime_argv():
    ssh = NodeTarget.parse(SSH_TARGET)
    assert (ssh.address, ssh.user, ssh.port, ssh.sudo) == (
        "node-1.example",
        "ops",
        2222,
        True,
    )
    assert ssh.runtime_argv("images", "import", "-") == [
        "sudo",
        "-n",
        "k3s",
        "ctr",
        "-n",
        "k8s.io",
        "images",
        "import",
        "-",
    ]
    assert "ops" not in json.dumps(ssh.public()) and "ops" not in ssh.identity
    kind = NodeTarget.parse("docker://kind-node?runtime=containerd&namespace=k8s.io")
    assert kind.runtime_argv("images", "ls") == ["ctr", "-n", "k8s.io", "images", "ls"]
    plain = NodeTarget.parse("ssh://node?runtime=containerd&sudo=false")
    assert plain.runtime_argv("images", "ls")[0] == "ctr"
    for bad in (
        "ssh://node",
        "ssh://node?runtime=docker",
        "ssh://node?runtime=containerd&runtime=containerd",
        "ssh://node?runtime=containerd&shell=1",
        "ssh://user:secret@node?runtime=containerd",
        "ssh://-oProxyCommand=x?runtime=containerd",
        "docker://a/b?runtime=containerd",
        "registry://registry.test:5000/app",
        "http://node?runtime=containerd",
        "ssh://node?runtime=containerd&namespace=a;b",
    ):
        with pytest.raises(ValueError):
            NodeTarget.parse(bad)


def test_ssh_transport_is_batch_mode_and_quotes_one_remote_command(ssh_pin):
    target = NodeTarget.parse(SSH_TARGET)
    argv = Transport(target, ssh=ssh_pin).argv(target.runtime_argv("images", "ls"))
    assert argv[:4] == [str(ssh_pin.path), "-o", "BatchMode=yes", "-T"]
    assert argv[-3:] == [
        "--",
        "node-1.example",
        "sudo -n k3s ctr -n k8s.io images ls",
    ]
    assert argv[argv.index("-p") : argv.index("-p") + 2] == ["-p", "2222"]
    assert argv[argv.index("-l") : argv.index("-l") + 2] == ["-l", "ops"]
    with pytest.raises(ValueError):
        target.runtime_argv("images", "ls", "$(reboot)")
    with pytest.raises(ValueError):
        Transport(target)  # ssh must be a pinned tool


def test_image_delivery_imports_then_is_idempotent(docker_pin, tmp_path):
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": (digest, archive)})
    deliver = delivery(node, docker_pin)
    target = NodeTarget.parse(DOCKER_TARGET)
    first = deliver.deliver(DockerImageSource("example/app:1"), target, grant(digest))
    assert first["result"] == "imported" and first["state"] == "succeeded"
    assert first["image"]["reference"] == "docker.io/example/app:1"
    assert first["image"]["config_digest"] == digest
    assert first["transfer"] == {
        "streamed": True,
        "bytes": len(archive),
        "intermediate_registry": False,
        "temporary_archive": False,
    }
    assert first["source"]["local_image_id"] == digest
    assert first["tools"] == {"docker": docker_pin.sha256}
    assert first["pushed"] is False and first["previous"] is None
    assert first["started_at"].endswith("Z") and first["finished_at"].endswith("Z")
    save = next(call for call in node.calls if "save" in call)
    assert save[-1] == digest  # saved by immutable ID, not a movable tag
    importer = next(call for call in node.calls if "import" in call)
    assert importer[3:6] == ["exec", "-i", "node-1"]
    assert importer[-3:] == ["images", "import", "-"]
    # The re-emitted archive names exactly the approved reference.
    with tarfile.open(fileobj=io.BytesIO(node.imports[0])) as sent:
        (entry,) = json.loads(sent.extractfile("manifest.json").read())
        assert entry["RepoTags"] == ["docker.io/example/app:1"]
        assert "repositories" not in sent.getnames()
        assert sent.getnames()[-1] == "manifest.json"

    second = deliver.deliver(DockerImageSource("example/app:1"), target, grant(digest))
    assert second["result"] == "already-present"
    assert second["transfer"]["streamed"] is False
    assert len(node.imports) == 1


def test_local_mismatch_aborts_before_any_transfer(docker_pin):
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": (digest, archive)})
    other = "sha256:" + "1" * 64
    receipt = delivery(node, docker_pin).deliver(
        DockerImageSource("example/app:1"),
        NodeTarget.parse(DOCKER_TARGET),
        grant(other),
    )
    assert (receipt["result"], receipt["reason"]) == ("rejected", "digest-mismatch")
    assert receipt["state"] == "rejected"
    assert not node.imports and not any("save" in call for call in node.calls)


def test_stream_mismatch_withholds_the_index_so_nothing_is_imported(docker_pin):
    # With Docker's containerd store the local ID is not the config digest, so
    # the authoritative check is the one recomputed from the streamed bytes.
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": ("sha256:" + "2" * 64, archive)})
    node.containerd_store = True
    other = "sha256:" + "1" * 64
    sent: list[bytes] = []
    original = node.feed

    def recording_feed(argv, writer, limits, env, cancel=None):
        sink = io.BytesIO()
        try:
            writer(sink)
        finally:
            sent.append(sink.getvalue())
            node._import(sink.getvalue())  # the runtime sees the truncated stream
        return original(argv, writer, limits, env, cancel)

    node.feed = recording_feed  # type: ignore[method-assign]
    receipt = delivery(node, docker_pin).deliver(
        DockerImageSource("example/app:1"),
        NodeTarget.parse(DOCKER_TARGET),
        grant(other),
    )
    assert (receipt["result"], receipt["reason"]) == ("rejected", "digest-mismatch")
    assert b"manifest.json" not in sent[0] and b"oci-layout" not in sent[0]
    assert node.names == {}


def test_relay_withholds_index_members_and_checks_config_name():
    archive, digest = docker_archive()
    sink = io.BytesIO()
    with pytest.raises(DeliveryRejected):
        relay_image_stream(
            io.BytesIO(archive),
            sink,
            approved_digest="sha256:" + "3" * 64,
            reference="docker.io/example/app:1",
        )
    assert b"manifest.json" not in sink.getvalue()
    scanned = relay_image_stream(io.BytesIO(archive), None)
    assert (scanned.format, scanned.config_digest) == ("docker", digest)
    assert scanned.stream_sha256 == sha(archive)
    # A config whose file name is not its own digest is refused.
    renamed = archive.replace(digest[7:].encode(), b"f" * 64)
    with pytest.raises(ValueError):
        relay_image_stream(io.BytesIO(renamed), None)
    with pytest.raises(ValueError):
        relay_image_stream(io.BytesIO(archive), None, max_bytes=1024)


def test_archive_delivery_uses_the_archive_name_or_explicit_reference(
    docker_pin, tmp_path
):
    archive, digest = docker_archive(tags=["example/app:2"])
    path = tmp_path / "image.tar"
    path.write_bytes(archive)
    node = FakeNode()
    deliver = delivery(node, docker_pin)
    target = NodeTarget.parse(DOCKER_TARGET)
    receipt = deliver.deliver(ArchiveSource(path), target, grant(digest))
    assert receipt["result"] == "imported"
    assert receipt["image"]["reference"] == "docker.io/example/app:2"
    assert receipt["source"]["archive_sha256"] == sha(archive)
    assert receipt["source"]["format"] == "docker"
    assert str(tmp_path) not in json.dumps(receipt)
    renamed = deliver.deliver(
        ArchiveSource(path), target, grant(digest), reference="registry.test/app:x"
    )
    assert renamed["result"] == "imported"
    assert node.names.keys() == {"docker.io/example/app:2", "registry.test/app:x"}

    untagged = tmp_path / "untagged.tar"
    untagged.write_bytes(docker_archive(tags=[])[0])
    missing = deliver.deliver(ArchiveSource(untagged), target, grant(digest))
    assert (missing["result"], missing["reason"]) == ("rejected", "reference-required")
    wrong = deliver.deliver(ArchiveSource(path), target, grant("sha256:" + "4" * 64))
    assert (wrong["result"], wrong["reason"]) == ("rejected", "digest-mismatch")
    garbage = tmp_path / "garbage.tar"
    garbage.write_bytes(b"not a tar")
    invalid = deliver.deliver(ArchiveSource(garbage), target, grant(digest))
    assert (invalid["result"], invalid["reason"]) == ("rejected", "invalid-archive")


def test_oci_archive_is_renamed_through_index_annotations(docker_pin):
    archive, digest = oci_archive()
    sink = io.BytesIO()
    identity = relay_image_stream(
        io.BytesIO(archive),
        sink,
        approved_digest=digest,
        reference="registry.test/app:oci",
    )
    assert (identity.format, identity.config_digest) == ("oci", digest)
    with tarfile.open(fileobj=io.BytesIO(sink.getvalue())) as sent:
        index = json.loads(sent.extractfile("index.json").read())
        assert sent.getnames()[-2:] == ["oci-layout", "index.json"]
    assert index["manifests"][0]["annotations"] == {
        "io.containerd.image.name": "registry.test/app:oci",
        "org.opencontainers.image.ref.name": "oci",
    }


def test_replacement_is_recorded_and_bad_imports_fail_verification(docker_pin):
    old_archive, old = docker_archive(b"old")
    archive, digest = docker_archive(b"new")
    node = FakeNode({"example/app:1": (digest, archive)})
    node._import(old_archive)
    deliver = delivery(node, docker_pin)
    target = NodeTarget.parse(DOCKER_TARGET)
    receipt = deliver.deliver(DockerImageSource("example/app:1"), target, grant(digest))
    assert receipt["result"] == "imported"
    assert receipt["previous"]["config_digests"] == [old]

    node.tamper = old_archive  # the runtime ends up with something else
    node.names.clear()
    bad = deliver.deliver(DockerImageSource("example/app:1"), target, grant(digest))
    assert (bad["result"], bad["reason"]) == ("failed", "verification-failed")
    node.tamper = None
    node.names.clear()
    node.import_state = "failed"
    failed = deliver.deliver(DockerImageSource("example/app:1"), target, grant(digest))
    assert (failed["result"], failed["reason"]) == ("failed", "import-failed")


def test_ssh_delivery_forwards_only_an_explicit_agent_socket(docker_pin, ssh_pin):
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": (digest, archive)})
    deliver = delivery(
        node, docker_pin, ssh=ssh_pin, ssh_agent_socket=Path("/run/agent.sock")
    )
    target = NodeTarget.parse(SSH_TARGET)
    receipt = deliver.deliver(
        DockerImageSource("example/app:1"), target, grant(digest, SSH_TARGET)
    )
    assert receipt["result"] == "imported"
    assert receipt["tools"] == {"docker": docker_pin.sha256, "ssh": ssh_pin.sha256}
    assert receipt["target"] == {
        "transport": "ssh",
        "host": "node-1.example",
        "port": 2222,
        "runtime": "k3s-containerd",
        "namespace": "k8s.io",
        "sudo": True,
    }
    ssh_calls = [
        (call, env)
        for call, env in zip(node.calls, node.envs, strict=True)
        if call[0] == str(ssh_pin.path)
    ]
    assert len(ssh_calls) >= 3
    assert all(env == {"SSH_AUTH_SOCK": "/run/agent.sock"} for _, env in ssh_calls)
    assert all(
        env == {}
        for call, env in zip(node.calls, node.envs, strict=True)
        if call[0] == str(docker_pin.path)
    )
    assert any(
        call[-1] == "sudo -n k3s ctr -n k8s.io images import -" for call, _ in ssh_calls
    )


def test_grants_and_pins_are_checked_before_anything_runs(docker_pin, tmp_path):
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": (digest, archive)})
    deliver = delivery(node, docker_pin)
    target = NodeTarget.parse(DOCKER_TARGET)
    source = DockerImageSource("example/app:1")
    with pytest.raises(ValueError):
        deliver.deliver(source, target, DeliveryGrant(digest, DOCKER_TARGET, 1.0))
    with pytest.raises(ValueError):
        deliver.deliver(
            source, target, grant(digest, "docker://other?runtime=containerd")
        )
    with pytest.raises(ValueError):
        deliver.deliver(DockerImageSource("sha256:" + "a" * 64), target, grant(digest))
    docker_pin.path.write_bytes(b"#!/bin/sh\n# changed\n")
    with pytest.raises(ValueError):
        deliver.deliver(source, target, grant(digest))
    assert node.calls == []


def test_receipt_file_and_journal(tmp_path):
    receipt = {"schema": "piceli.node-delivery.v1", "result": "imported"}
    write_receipt(tmp_path / "receipt.json", receipt)
    assert json.loads((tmp_path / "receipt.json").read_text()) == receipt
    assert (tmp_path / "receipt.json").stat().st_mode & 0o777 == 0o600
    append_journal(tmp_path / "journal.jsonl", receipt)
    append_journal(tmp_path / "journal.jsonl", {**receipt, "result": "x"})
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert [json.loads(line)["result"] for line in lines] == ["imported", "x"]


def test_cli_deliver_writes_receipt_and_journal(
    docker_pin, tmp_path, monkeypatch, capsys
):
    archive, digest = docker_archive()
    node = FakeNode({"example/app:1": (digest, archive)})
    monkeypatch.setattr(
        cli, "NodeDelivery", lambda **kwargs: NodeDelivery(**kwargs, runner=node)
    )
    base = [
        "deliver",
        "--image",
        "example/app:1",
        "--to",
        DOCKER_TARGET,
        "--docker",
        str(docker_pin.path),
        "--docker-sha256",
        docker_pin.sha256,
        "--docker-socket",
        "/run/docker.sock",
        "--receipt",
        str(tmp_path / "r.json"),
        "--journal",
        str(tmp_path / "j.jsonl"),
    ]
    assert cli.main([*base, "--approve-digest", digest]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "imported"
    assert json.loads((tmp_path / "r.json").read_text())["result"] == "imported"
    assert cli.main([*base, "--approve-digest", "sha256:" + "5" * 64]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "digest-mismatch"
    assert len((tmp_path / "j.jsonl").read_text().splitlines()) == 2
    assert cli.main([*base[:3], "--to", "ssh://node", "--approve-digest", digest]) == 2
    assert "invalid" in capsys.readouterr().err


def test_subprocess_runner_streams_and_fences(tmp_path):
    runner = SubprocessRunner()
    limits = ProcessLimits(10, 1024)
    fed = runner.feed(["/bin/cat"], lambda sink: sink.write(b"payload"), limits, {})
    assert (fed.state, fed.stdout) == ("succeeded", b"payload")
    result, value = runner.read(
        ["/bin/echo", "streamed"], lambda stream: stream.read(), limits, {}
    )
    assert (result.state, value) == ("succeeded", b"streamed\n")

    def explode(sink):
        sink.write(b"partial")
        raise DeliveryRejected("stop")

    started = time.monotonic()
    with pytest.raises(DeliveryRejected):
        runner.feed(["/bin/cat"], explode, limits, {})
    assert time.monotonic() - started < 5
    slow = runner.capture(["/bin/sleep", "5"], ProcessLimits(0.2, 1024), {})
    assert slow.state == "timed-out"


def test_delivery_import_has_no_side_effects():
    script = r"""
import socket,subprocess
def trap(*a,**kw): raise AssertionError("unexpected side effect")
socket.socket=trap
subprocess.Popen=trap
import piceli.artifacts.delivery, piceli.artifacts.node_transport
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
