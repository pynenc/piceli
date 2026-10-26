"""Files as one OCI artifact (``artifacts publish-files``, ``fetch-file``; 0.32.0)."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from piceli.artifacts.cli import main
from piceli.artifacts.image_manifest import OCI_MANIFEST
from piceli.artifacts.publish import PublishError, PublishGrant
from piceli.artifacts.publish_files import FilePublisher, FilesPlan, fetch_file
from piceli.artifacts.registry import RegistryCredentials, RegistryError
from tests.unit.test_registry_delivery import PASSWORD, USER, FakeRegistry

TYPE = "application/vnd.example.downloads.v1"
EMPTY = "sha256:" + hashlib.sha256(b"{}").hexdigest()


class PublicRegistry(FakeRegistry):
    """A registry like a public package: anyone pulls (anonymous token), only
    the pusher pushes."""

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(handler.path)
        if parts.path == "/token" and "Authorization" not in handler.headers:
            with self.lock:
                self.requests.append((handler.command, handler.path))
            scope = parse_qs(parts.query)["scope"][0]
            if not scope.endswith(":pull"):
                return self._reply(handler, 401)
            return self._reply(handler, 200, b'{"token":"anonymous"}')
        return super().handle(handler)

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if handler.headers.get("Authorization") == "Bearer anonymous" and (
            handler.command in {"GET", "HEAD"}
        ):
            self.authorizations.append("Bearer anonymous")
            return True
        return super()._authorized(handler)


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def public() -> Iterator[PublicRegistry]:
    fake = PublicRegistry()
    fake.auth = "bearer"
    yield fake
    fake.close()


@pytest.fixture
def dist(tmp_path: Path) -> list[Path]:
    """Release downloads: two archives, the checksums and a signature."""
    directory = tmp_path / "dist"
    directory.mkdir()
    files = {
        "tool-linux-x86_64.tar.gz": bytes(range(256)) * 300,
        "tool-macos-universal.tar.gz": b"\x1f\x8b" + b"m" * 5000,
        "SHA256SUMS": b"",
        "SHA256SUMS.sig": b"-----BEGIN SSH SIGNATURE-----\n",
    }
    sums = "".join(
        f"{hashlib.sha256(body).hexdigest()}  {name}\n"
        for name, body in files.items()
        if name.endswith(".tar.gz")
    )
    files["SHA256SUMS"] = sums.encode()
    for name, body in files.items():
        (directory / name).write_bytes(body)
    return sorted(directory.iterdir())


def to(registry: FakeRegistry, repository: str = "example/tool") -> str:
    return f"oci://127.0.0.1:{registry.port}/{repository}"


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def publish(plan: FilesPlan, publisher: FilePublisher | None = None) -> dict[str, Any]:
    publisher = publisher or FilePublisher()
    return publisher.publish(plan, PublishGrant(plan.digest, time.time() + 600))


def test_each_file_is_one_layer_whose_digest_is_its_sha256(
    registry: FakeRegistry, dist: list[Path]
) -> None:
    plan = FilesPlan.from_paths(dist, to(registry), "0.2.0-downloads", TYPE)
    registry.requests.clear()
    result = publish(plan)
    assert result["state"] == "published", result
    assert result["result"] == "pushed"
    repository = "example/tool"
    for path in dist:
        # Stored as it is: no compression, no tar.
        assert registry.blobs[(repository, sha(path))] == path.read_bytes()
    manifest_digest = registry.tags[(repository, "0.2.0-downloads")]
    assert manifest_digest == result["manifest_digest"] == plan.manifest_digest
    body, media = registry.manifests[(repository, manifest_digest)]
    assert media == OCI_MANIFEST
    document = json.loads(body)
    assert document["artifactType"] == TYPE
    assert document["config"] == {
        "mediaType": "application/vnd.oci.empty.v1+json",
        "digest": EMPTY,
        "size": 2,
        "data": "e30=",
    }
    assert registry.blobs[(repository, EMPTY)] == b"{}"
    assert [
        (layer["annotations"]["org.opencontainers.image.title"], layer["digest"])
        for layer in document["layers"]
    ] == [(path.name, sha(path)) for path in dist]
    assert {layer["mediaType"] for layer in document["layers"]} == {
        "application/octet-stream"
    }
    # The receipt: the manifest and every file's digest, size and URL.
    assert [(f["name"], f["digest"], f["size"]) for f in result["files"]] == [
        (path.name, sha(path), path.stat().st_size) for path in dist
    ]
    first = result["files"][0]
    assert first["url"] == (
        f"http://127.0.0.1:{registry.port}/v2/{repository}/blobs/{first['digest']}"
    )
    # The tag moves last.
    puts = [path for method, path in registry.mutations() if method == "PUT"]
    assert puts[-1].endswith("/manifests/0.2.0-downloads")


def test_publishing_again_pushes_nothing(
    registry: FakeRegistry, dist: list[Path]
) -> None:
    plan = FilesPlan.from_paths(dist, to(registry), "0.2.0-downloads", TYPE)
    assert publish(plan)["state"] == "published"
    registry.requests.clear()
    again = publish(FilesPlan.from_paths(dist, to(registry), "0.2.0-downloads", TYPE))
    assert again["state"] == "published" and again["result"] == "already-present"
    assert again["manifest_digest"] == plan.manifest_digest
    assert registry.mutations() == []
    assert {f["result"] for f in again["files"]} == {"already-present"}


def test_a_tag_is_moved_only_with_move_tag(
    registry: FakeRegistry, dist: list[Path]
) -> None:
    first = FilesPlan.from_paths(dist[:2], to(registry), "downloads", TYPE)
    assert publish(first)["state"] == "published"
    registry.requests.clear()
    other = FilesPlan.from_paths(dist, to(registry), "downloads", TYPE)
    refused = publish(other)
    assert refused["state"] == "failed" and refused["reason"] == "publish-tag-exists"
    assert registry.mutations() == []  # refused before anything was pushed
    moved = FilesPlan.from_paths(dist, to(registry), "downloads", TYPE, move_tag=True)
    assert moved.digest != other.digest
    result = publish(moved)
    assert result["state"] == "published"
    assert result["previous"] == first.manifest_digest
    assert registry.tags[("example/tool", "downloads")] == moved.manifest_digest


def test_the_grant_binds_the_plan_and_changed_files_are_refused(
    registry: FakeRegistry, dist: list[Path]
) -> None:
    plan = FilesPlan.from_paths(dist, to(registry), "downloads", TYPE)
    with pytest.raises(PublishError) as raised:
        FilePublisher().publish(
            plan, PublishGrant("sha256:" + "0" * 64, time.time() + 60)
        )
    assert raised.value.code == "publish-not-approved"
    dist[0].write_bytes(b"changed after the plan")
    result = publish(plan)
    assert result["state"] == "failed" and result["reason"] == "publish-input-changed"
    assert ("example/tool", "downloads") not in registry.tags


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"tag": "bad tag"}, "invalid tag"),
        ({"artifact_type": "not a type"}, "media type"),
        ({"to": "oci://127.0.0.1:5000/example/tool:1.0"}, "--tag"),
        ({"twice": True}, "same name"),
    ],
)
def test_the_plan_refuses_bad_inputs_without_network(
    dist: list[Path], tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    paths = list(dist)
    if change.get("twice"):
        other = tmp_path / "other"
        other.mkdir()
        (other / dist[0].name).write_bytes(b"x")
        paths.append(other / dist[0].name)
    with pytest.raises(PublishError, match=message):
        FilesPlan.from_paths(
            paths,
            change.get("to", "oci://127.0.0.1:9/example/tool"),
            change.get("tag", "1.0"),
            change.get("artifact_type", TYPE),
        )


def test_a_directory_or_link_is_not_a_file(dist: list[Path], tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(dist[0])
    for path in (tmp_path, link):
        with pytest.raises(PublishError, match="regular file"):
            FilesPlan.from_paths([path], "oci://127.0.0.1:9/x", "1.0", TYPE)


def test_cli_plans_then_pushes_and_never_prints_the_credential(
    public: PublicRegistry,
    dist: list[Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = tmp_path / "credentials.json"
    secret.write_text(json.dumps({"username": USER, "password": PASSWORD}))
    secret.chmod(0o600)
    base = [
        "publish-files", *map(str, dist), "--to", to(public), "--tag",
        "0.2.0-downloads", "--artifact-type", TYPE,
    ]  # fmt: skip
    public.requests.clear()
    assert main(base) == 3
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan["state"] == "approval-required"
    assert [f["name"] for f in plan["files"]] == [p.name for p in dist]
    assert public.requests == []  # planning contacts nothing
    assert dist[0].name in captured.err and plan["digest"] in captured.err
    # Anonymous: pulling is allowed, pushing is not.
    assert main([*base, "--approve", plan["digest"]]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "registry-unauthorized"
    receipt_path = tmp_path / "receipt.json"
    code = main(
        [*base, "--approve", plan["digest"], "--credentials", str(secret),
         "--out", str(receipt_path)]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0, captured
    receipt = json.loads(receipt_path.read_text())
    assert receipt["revision"] == "piceli.publish-files-receipt.v1"
    assert receipt["digest"] == plan["digest"]
    assert receipt["manifest_digest"] == plan["manifest_digest"]
    auth = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    for text in (captured.out, captured.err, receipt_path.read_text()):
        assert PASSWORD not in text and auth not in text
    # Anyone fetches one file by its digest from the public repository.
    wanted = receipt["files"][-1]
    out = tmp_path / "fetched" / wanted["name"]
    fetched = main(
        ["fetch-file", "--from", to(public), "--digest", wanted["digest"],
         "--out", str(out)]
    )  # fmt: skip
    body = json.loads(capsys.readouterr().out)
    assert fetched == 0, body
    assert body["state"] == "fetched" and body["digest"] == wanted["digest"]
    assert sha(out) == wanted["digest"]
    assert "Bearer anonymous" in public.authorizations


def test_a_blob_that_does_not_match_its_digest_is_not_written(
    registry: FakeRegistry, dist: list[Path], tmp_path: Path
) -> None:
    assert publish(FilesPlan.from_paths(dist, to(registry), "d", TYPE))["state"]
    wanted = sha(dist[0])
    registry.blobs[("example/tool", wanted)] = b"tampered"
    out = tmp_path / "out.bin"
    with pytest.raises(RegistryError) as raised:
        fetch_file(to(registry), wanted, out)
    assert raised.value.reason == "blob-digest-mismatch"
    assert not out.exists() and list(tmp_path.glob(".out.bin.*")) == []
    with pytest.raises(RegistryError) as raised:
        fetch_file(to(registry), "sha256:" + "1" * 64, out)
    assert raised.value.reason == "blob-not-found"
    assert (
        main(
            [
                "fetch-file",
                "--from",
                to(registry),
                "--digest",
                wanted,
                "--out",
                str(out),
            ]
        )
        == 1
    )


def test_publish_with_docker_config_credentials(
    public: PublicRegistry, dist: list[Path]
) -> None:
    plan = FilesPlan.from_paths(dist[:1], to(public), "d", TYPE)
    creds = RegistryCredentials(username=USER, password=PASSWORD)
    result = publish(plan, FilePublisher(credentials=creds))
    assert result["state"] == "published", result
    assert PASSWORD not in json.dumps(result) and PASSWORD not in repr(creds)
