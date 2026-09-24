"""Registry delivery against an in-process fake OCI Distribution v2 registry."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from piceli.artifacts import cli
from piceli.artifacts.delivery import ArchiveSource, DeliveryGrant, DockerImageSource
from piceli.artifacts.image_manifest import scan_image_stream
from piceli.artifacts.node_transport import RunResult
from piceli.artifacts.process import ProcessLimits, ToolPin
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryTarget,
    StreamedOciRegistryClient,
)
from piceli.artifacts.registry_delivery import (
    SCHEMA,
    RegistryDelivery,
    RegistryForward,
    node_registry_address,
)

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
USER, PASSWORD = "pusher", "s3cret-Pa55word"


def sha(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


# --- image fixtures -------------------------------------------------------------


def add(archive: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    archive.addfile(info, io.BytesIO(body))


def layer(marker: bytes, size: int = 0) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        add(archive, "file.txt", marker + b"x" * size)
    return out.getvalue()


def config_for(layers: list[bytes], arch: str = "arm64") -> bytes:
    return json.dumps(
        {
            "architecture": arch,
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [sha(item) for item in layers]},
        }
    ).encode()


def docker_archive(layers: list[bytes], tags: list[str] | None = None) -> bytes:
    """Classic ``docker save``: uncompressed ``<id>/layer.tar`` members."""
    config = config_for(layers)
    out = io.BytesIO()
    names = []
    with tarfile.open(fileobj=out, mode="w") as archive:
        for body in layers:
            name = f"{hashlib.sha256(b'v1' + body).hexdigest()}/layer.tar"
            names.append(name)
            add(archive, name, body)
        add(archive, f"{sha(config)[7:]}.json", config)
        entry = {
            "Config": f"{sha(config)[7:]}.json",
            "RepoTags": tags or ["example/app:1"],
            "Layers": names,
        }
        add(archive, "manifest.json", json.dumps([entry]).encode())
    return out.getvalue()


def oci_archive(
    layers: list[bytes],
    *,
    compress: bool = False,
    as_index: bool = False,
    config: bytes | None = None,
) -> tuple[bytes, str, str]:
    """An OCI layout tar; returns (tar, config digest, manifest digest)."""
    blobs = [gzip.compress(item, mtime=0) if compress else item for item in layers]
    config = config or config_for(layers)
    media = "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if compress else "")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": sha(config),
                "size": len(config),
            },
            "layers": [
                {"mediaType": media, "digest": sha(blob), "size": len(blob)}
                for blob in blobs
            ],
        },
        indent=1,  # deliberately not canonical: the bytes must be kept as-is
    ).encode()
    descriptor = {
        "mediaType": OCI_MANIFEST,
        "digest": sha(manifest),
        "size": len(manifest),
        "platform": {"os": "linux", "architecture": "arm64"},
    }
    extra: list[bytes] = []
    if as_index:
        # Another platform whose manifest is absent from the archive, and an
        # attestation manifest: both are ignored.
        absent = {
            "mediaType": OCI_MANIFEST,
            "digest": "sha256:" + "e" * 64,
            "size": 10,
            "platform": {"os": "linux", "architecture": "amd64"},
        }
        attestation = {
            "mediaType": OCI_MANIFEST,
            "digest": "sha256:" + "f" * 64,
            "size": 10,
            "platform": {"os": "unknown", "architecture": "unknown"},
        }
        index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": OCI_INDEX,
                "manifests": [descriptor, absent, attestation],
            }
        ).encode()
        extra.append(index)
        top = {"mediaType": OCI_INDEX, "digest": sha(index), "size": len(index)}
    else:
        top = descriptor
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        add(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        for body in [*blobs, config, manifest, *extra]:
            add(archive, "blobs/sha256/" + sha(body)[7:], body)
        add(
            archive,
            "index.json",
            json.dumps({"schemaVersion": 2, "manifests": [top]}).encode(),
        )
    return out.getvalue(), sha(config), sha(manifest)


# --- fake registry --------------------------------------------------------------


class FakeRegistry:
    """Just enough of OCI Distribution v2, with request recording."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.tags: dict[tuple[str, str], str] = {}
        self.uploads: dict[str, bytearray] = {}
        self.requests: list[tuple[str, str]] = []
        self.authorizations: list[str | None] = []
        self.auth: str | None = None  # None, "basic" or "bearer"
        self.chunks = True
        self.foreign_location: str | None = None
        self.lock = threading.Lock()
        registry = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _any(self) -> None:
                registry.handle(self)

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # helpers -----------------------------------------------------------------

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.requests if m in {"POST", "PUT", "PATCH"}]

    @staticmethod
    def _reply(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        handler.send_response(status)
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD" and body:
            handler.wfile.write(body)

    def _authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        header = handler.headers.get("Authorization")
        self.authorizations.append(header)
        if self.auth is None:
            return True
        if self.auth == "basic":
            expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            if header == f"Basic {expected}":
                return True
            challenge = 'Basic realm="fake"'
        else:
            if header == "Bearer issued-token":
                return True
            challenge = (
                f'Bearer realm="http://127.0.0.1:{self.port}/token",service="fake"'
            )
        self._reply(handler, 401, b"{}", {"WWW-Authenticate": challenge})
        return False

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command
        parts = urlsplit(handler.path)
        path, query = parts.path, parse_qs(parts.query)
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        with self.lock:
            self.requests.append((method, handler.path))
        if path == "/token":
            expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            if handler.headers.get("Authorization") != f"Basic {expected}":
                return self._reply(handler, 401)
            assert query["scope"][0].endswith(":pull,push")
            return self._reply(handler, 200, b'{"token":"issued-token"}')
        if not self._authorized(handler):
            return None
        if path == "/v2/":
            return self._reply(handler, 200, b"{}")
        repo, kind, rest = self._split(path)
        if kind == "blobs" and rest.startswith("uploads/"):
            return self._upload(handler, method, repo, rest[8:], query, body)
        if kind == "blobs":
            blob = self.blobs.get((repo, rest))
            if blob is None:
                return self._reply(handler, 404)
            return self._reply(
                handler,
                200,
                blob if method == "GET" else b"",
                {"Docker-Content-Digest": rest}
                | ({"Content-Length": str(len(blob))} if method == "HEAD" else {}),
            )
        if kind == "manifests":
            return self._manifest(handler, method, repo, rest, body)
        return self._reply(handler, 404)

    @staticmethod
    def _split(path: str) -> tuple[str, str, str]:
        tail = path[len("/v2/") :]
        for kind in ("/blobs/", "/manifests/"):
            if kind in tail:
                repo, rest = tail.split(kind, 1)
                return repo, kind.strip("/"), rest
        return "", "", ""

    def _upload(self, handler, method, repo, session, query, body):  # type: ignore[no-untyped-def]
        if method == "POST":
            new = uuid.uuid4().hex
            self.uploads[new] = bytearray()
            location = f"/v2/{repo}/blobs/uploads/{new}"
            if self.foreign_location:
                location = self.foreign_location + location
            return self._reply(handler, 202, b"", {"Location": location})
        if session not in self.uploads:
            return self._reply(handler, 404)
        if method == "DELETE":
            self.uploads.pop(session)
            return self._reply(handler, 204)
        if method == "PATCH":
            if not self.chunks:
                return self._reply(handler, 405)
            start = int(handler.headers["Content-Range"].split("-")[0])
            assert start == len(self.uploads[session])
            self.uploads[session].extend(body)
            return self._reply(
                handler,
                202,
                b"",
                {
                    "Location": f"/v2/{repo}/blobs/uploads/{session}",
                    "Range": f"0-{len(self.uploads[session]) - 1}",
                },
            )
        if method == "PUT":
            data = bytes(self.uploads.pop(session) + body)
            digest = query["digest"][0]
            if sha(data) != digest:
                return self._reply(
                    handler, 400, b'{"errors":[{"code":"DIGEST_INVALID"}]}'
                )
            self.blobs[(repo, digest)] = data
            return self._reply(handler, 201, b"", {"Docker-Content-Digest": digest})
        return self._reply(handler, 405)

    def _manifest(self, handler, method, repo, reference, body):  # type: ignore[no-untyped-def]
        if method == "PUT":
            document = json.loads(body)
            for desc in [document["config"], *document["layers"]]:
                if (repo, desc["digest"]) not in self.blobs:
                    return self._reply(
                        handler, 400, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}'
                    )
            digest = sha(body)
            media = handler.headers["Content-Type"]
            self.manifests[(repo, digest)] = (body, media)
            if not reference.startswith("sha256:"):
                self.tags[(repo, reference)] = digest
            return self._reply(handler, 201, b"", {"Docker-Content-Digest": digest})
        digest = (
            reference
            if reference.startswith("sha256:")
            else self.tags.get((repo, reference))
        )
        stored = self.manifests.get((repo, digest)) if digest else None
        if stored is None:
            return self._reply(handler, 404)
        return self._reply(
            handler,
            200,
            stored[0],
            {"Docker-Content-Digest": digest, "Content-Type": stored[1]}
            | ({"Content-Length": str(len(stored[0]))} if method == "HEAD" else {}),
        )


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


def write_archive(tmp_path: Path, body: bytes, name: str = "image.tar") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def push(
    registry: FakeRegistry,
    archive: Path,
    approved: str,
    target: str | None = None,
    **options: object,
) -> dict:
    url = target or f"oci://127.0.0.1:{registry.port}/team/app:1"
    node_registry = options.pop("node_registry", None)
    delivery = RegistryDelivery(**options)  # type: ignore[arg-type]
    return delivery.deliver(
        ArchiveSource(archive),
        RegistryTarget.parse(url),
        DeliveryGrant(approved, url, time.time() + 60),
        node_registry=node_registry,  # type: ignore[arg-type]
    )


# --- tests ----------------------------------------------------------------------


def test_docker_archive_push_then_idempotent(registry, tmp_path):
    layers = [layer(b"base"), layer(b"app")]
    path = write_archive(tmp_path, docker_archive(layers))
    approved = sha(config_for(layers))
    first = push(registry, path, approved)
    assert first["schema"] == SCHEMA
    assert (first["result"], first["state"], first["pushed"]) == (
        "pushed",
        "succeeded",
        True,
    )
    manifest_digest = first["image"]["manifest_digest"]
    body, media = registry.manifests[("team/app", manifest_digest)]
    assert media == OCI_MANIFEST and sha(body) == manifest_digest
    document = json.loads(body)
    assert document["config"]["digest"] == approved
    assert [item["mediaType"] for item in document["layers"]] == [
        "application/vnd.oci.image.layer.v1.tar"
    ] * 2
    assert registry.tags[("team/app", "1")] == manifest_digest
    assert first["image"]["manifest_origin"] == "synthesized"
    assert first["blobs"]["uploaded"] == 3 and first["blobs"]["skipped"] == 0
    assert first["pull_ref"] == (
        f"127.0.0.1:{registry.port}/team/app@{manifest_digest}"
    )
    registry.requests.clear()
    second = push(registry, path, approved)
    assert second["result"] == "already-present" and second["pushed"] is False
    assert second["image"]["manifest_digest"] == manifest_digest  # deterministic
    assert second["blobs"]["skipped"] == 3 and second["blobs"]["uploaded"] == 0
    assert registry.mutations() == []


def test_existing_blobs_are_skipped_and_tag_moves_are_recorded(registry, tmp_path):
    base = layer(b"shared-base", 50_000)
    one = write_archive(tmp_path, docker_archive([base, layer(b"v1")]), "one.tar")
    two = write_archive(tmp_path, docker_archive([base, layer(b"v2")]), "two.tar")
    first = push(registry, one, sha(config_for([base, layer(b"v1")])))
    registry.requests.clear()
    second = push(registry, two, sha(config_for([base, layer(b"v2")])))
    assert second["result"] == "pushed"
    actions = {item["digest"]: item["action"] for item in second["blobs"]["items"]}
    assert actions[sha(base)] == "skipped"
    assert actions[sha(layer(b"v2"))] == "uploaded"
    assert second["blobs"]["skipped_bytes"] == len(base)
    # One POST per uploaded blob (config + new layer), none for the shared base.
    assert sum(1 for m, _ in registry.requests if m == "POST") == 2
    assert second["previous"] == {"tag_digest": first["image"]["manifest_digest"]}


def test_oci_archive_manifest_is_pushed_byte_for_byte(registry, tmp_path):
    layers = [layer(b"one"), layer(b"two")]
    body, config, manifest = oci_archive(layers, compress=True, as_index=True)
    receipt = push(registry, write_archive(tmp_path, body), config)
    assert receipt["result"] == "pushed", receipt
    assert receipt["image"]["manifest_digest"] == manifest
    assert receipt["image"]["manifest_origin"] == "archive"
    assert receipt["source"]["format"] == "oci"
    stored, _ = registry.manifests[("team/app", manifest)]
    assert sha(stored) == manifest and b"\n" in stored  # original, not re-encoded


def test_digest_mismatch_is_refused_before_contacting_the_registry(registry, tmp_path):
    path = write_archive(tmp_path, docker_archive([layer(b"a")]))
    receipt = push(registry, path, "sha256:" + "1" * 64)
    assert (receipt["result"], receipt["reason"]) == ("rejected", "digest-mismatch")
    assert receipt["pull_ref"] is None and receipt["image"]["config_digest"] is None
    assert registry.requests == []  # not even /v2/: no upload, no manifest PUT


def test_layers_not_matching_the_config_are_rejected(registry, tmp_path):
    # A self-consistent archive whose config names other layer content: the
    # approved config digest must fix every layer, so this is refused.
    wrong = config_for([layer(b"something else")])
    body, config, _ = oci_archive([layer(b"a")], config=wrong)
    receipt = push(registry, write_archive(tmp_path, body), config)
    assert (receipt["result"], receipt["reason"]) == ("rejected", "invalid-archive")
    assert registry.requests == []


def test_manifest_naming_another_config_is_refused_before_put(registry, tmp_path):
    layers = [layer(b"a")]
    path = write_archive(tmp_path, docker_archive(layers))
    approved = sha(config_for(layers))
    import piceli.artifacts.registry_delivery as module

    original = module.manifest_config_digest
    module.manifest_config_digest = lambda body: "sha256:" + "2" * 64  # type: ignore[assignment]
    try:
        receipt = push(registry, path, approved)
    finally:
        module.manifest_config_digest = original  # type: ignore[assignment]
    assert (receipt["result"], receipt["reason"]) == ("rejected", "digest-mismatch")
    assert not any("/manifests/" in p for m, p in registry.requests if m == "PUT")
    assert registry.manifests == {}


def test_chunked_uploads_and_monolithic_fallback(registry, tmp_path):
    big = layer(b"big", 300_000)
    path = write_archive(tmp_path, docker_archive([big]))
    approved = sha(config_for([big]))
    small_chunks = partial(
        StreamedOciRegistryClient, chunk_size=65536, monolithic_max=1024
    )
    receipt = push(registry, path, approved, client_factory=small_chunks)
    modes = {item["digest"]: item["mode"] for item in receipt["blobs"]["items"]}
    assert modes[sha(big)] == "chunked"
    assert sum(1 for m, _ in registry.requests if m == "PATCH") == 5
    assert registry.blobs[("team/app", sha(big))] == big
    registry.chunks = False
    other = f"oci://127.0.0.1:{registry.port}/team/other:1"
    receipt = push(registry, path, approved, other, client_factory=small_chunks)
    modes = {item["digest"]: item["mode"] for item in receipt["blobs"]["items"]}
    assert receipt["result"] == "pushed"
    assert modes[sha(big)] == "monolithic-fallback"


@pytest.mark.parametrize("scheme", ["basic", "bearer"])
def test_credentials_are_used_but_never_leaked(registry, tmp_path, capsys, scheme):
    registry.auth = scheme
    layers = [layer(b"secret-test")]
    path = write_archive(tmp_path, docker_archive(layers))
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"username": USER, "password": PASSWORD}))
    creds.chmod(0o600)
    url = f"oci://127.0.0.1:{registry.port}/team/app:1"
    code = cli.main(
        [
            "deliver",
            "--archive",
            str(path),
            "--to",
            url,
            "--approve-digest",
            sha(config_for(layers)),
            "--credentials",
            str(creds),
            "--receipt",
            str(tmp_path / "r.json"),
            "--journal",
            str(tmp_path / "j.jsonl"),
        ]
    )
    out = capsys.readouterr()
    assert code == 0, out
    receipt = json.loads(out.out)
    assert receipt["result"] == "pushed" and receipt["target"]["auth"] == "basic"
    encoded = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    for text in (
        out.out,
        out.err,
        (tmp_path / "r.json").read_text(),
        (tmp_path / "j.jsonl").read_text(),
        repr(RegistryCredentials.load(creds)),
    ):
        for secret in (USER, PASSWORD, encoded, "issued-token", str(creds)):
            assert secret not in text
    creds.chmod(0o644)
    with pytest.raises(ValueError, match="group/world"):
        RegistryCredentials.load(creds)


def test_upload_location_on_another_origin_is_refused(registry, tmp_path):
    other = FakeRegistry()
    try:
        registry.auth = "basic"
        registry.foreign_location = f"http://127.0.0.1:{other.port}"
        creds = RegistryCredentials(USER, PASSWORD)
        layers = [layer(b"x")]
        path = write_archive(tmp_path, docker_archive(layers))
        receipt = push(registry, path, sha(config_for(layers)), credentials=creds)
        assert (receipt["result"], receipt["reason"]) == (
            "failed",
            "cross-origin-location-refused",
        )
        assert other.requests == []  # neither bytes nor credentials went there
    finally:
        other.close()


def test_target_parsing_and_plain_http_rule():
    remote = RegistryTarget.parse("oci://registry.example/team/app:1.2")
    assert (remote.tls, remote.effective_port, remote.tag) == (True, 443, "1.2")
    local = RegistryTarget.parse("oci://127.0.0.1:5000/team/app")
    assert (local.tls, local.tag, local.registry) == (False, None, "127.0.0.1:5000")
    assert local.identity == "oci://127.0.0.1:5000/team/app?tls=false"
    assert RegistryTarget.parse("oci://localhost:5000/a?tls=true").tls is True
    for bad in (
        "oci://registry.example:5000/app?tls=false",
        "oci://registry.example/app@sha256:" + "a" * 64,
        "oci://user:pw@registry.example/app",
        "oci://registry.example/",
        "oci://registry.example/App",
        "oci://127.0.0.1/app?insecure=1",
        "https://registry.example/app",
    ):
        with pytest.raises(ValueError):
            RegistryTarget.parse(bad)
    with pytest.raises(ValueError, match="loopback"):
        RegistryEndpoint("registry.example", 5000, use_tls=False)
    assert node_registry_address("127.0.0.1:5000") == "127.0.0.1:5000"
    with pytest.raises(ValueError):
        node_registry_address("127.0.0.1:5000/path")


def test_grant_must_name_the_exact_registry_target(registry, tmp_path):
    path = write_archive(tmp_path, docker_archive([layer(b"a")]))
    target = RegistryTarget.parse(f"oci://127.0.0.1:{registry.port}/team/app:1")
    other = f"oci://127.0.0.1:{registry.port}/team/app:2"
    with pytest.raises(ValueError, match="grant"):
        RegistryDelivery().deliver(
            ArchiveSource(path),
            target,
            DeliveryGrant("sha256:" + "1" * 64, other, time.time() + 60),
        )
    assert registry.requests == []


def test_forward_is_held_for_the_push_and_node_registry_is_explicit(registry, tmp_path):
    layers = [layer(b"fwd")]
    path = write_archive(tmp_path, docker_archive(layers))
    kubectl = tmp_path / "kubectl"
    kubectl.write_bytes(b"#!/bin/sh\n")
    forward = RegistryForward(
        namespace="registry",
        target="service/registry",
        remote_port=5000,
        kubeconfig=tmp_path / "kubeconfig",
        kubectl=ToolPin.capture(kubectl),
        context="kind-test",
    )
    events: list[str] = []

    @contextmanager
    def fake_forward(spec: RegistryForward, local_port: int) -> Iterator[None]:
        assert spec is forward and local_port == registry.port
        events.append("start")
        yield
        events.append(f"stop after {len(registry.requests)} requests")

    with pytest.raises(ValueError, match="node registry"):
        push(registry, path, sha(config_for(layers)), forward=forward)
    receipt = push(
        registry,
        path,
        sha(config_for(layers)),
        forward=forward,
        forwarder=fake_forward,
        node_registry="127.0.0.1:5000",
    )
    assert receipt["result"] == "pushed"
    assert events == ["start", f"stop after {len(registry.requests)} requests"]
    assert receipt["pull_ref"].startswith("127.0.0.1:5000/team/app@sha256:")
    assert receipt["target"]["forward"] == {
        "namespace": "registry",
        "target": "service/registry",
        "local_port": registry.port,
        "remote_port": 5000,
        "context": "kind-test",
    }
    assert str(tmp_path) not in json.dumps(receipt)
    assert receipt["tools"] == {"kubectl": forward.kubectl.sha256}
    with pytest.raises(ValueError, match="loopback"):
        push(
            registry,
            path,
            sha(config_for(layers)),
            "oci://registry.example/team/app:1",
            forward=forward,
            node_registry="127.0.0.1:5000",
        )


FAKE_KUBECTL = """#!{python}
import os, socket, sys, threading
args = sys.argv[1:]
assert args[:2] == ["--kubeconfig", os.environ["EXPECT_KUBECONFIG"]], args
local = int(args[args.index("port-forward") + 2].split(":")[0])
assert args[-2:] == ["--address", "127.0.0.1"]
upstream = int(os.environ["FAKE_UPSTREAM_PORT"])
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", local))
server.listen(16)
def pipe(a, b):
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
while True:
    client, _ = server.accept()
    remote = socket.create_connection(("127.0.0.1", upstream))
    threading.Thread(target=pipe, args=(client, remote), daemon=True).start()
    threading.Thread(target=pipe, args=(remote, client), daemon=True).start()
"""


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_supervised_forward_runs_kubectl_and_stops_it(registry, tmp_path, monkeypatch):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL.format(python=sys.executable))
    kubectl.chmod(0o755)
    kubeconfig = tmp_path / "kubeconfig"
    monkeypatch.setenv("FAKE_UPSTREAM_PORT", str(registry.port))
    monkeypatch.setenv("EXPECT_KUBECONFIG", str(kubeconfig))
    port = free_port()
    forward = RegistryForward(
        namespace="registry",
        target="service/registry",
        remote_port=5000,
        kubeconfig=kubeconfig,
        kubectl=ToolPin.capture(kubectl),
        context="kind-test",
        startup_seconds=20,
    )
    layers = [layer(b"through-forward")]
    path = write_archive(tmp_path, docker_archive(layers))
    receipt = push(
        registry,
        path,
        sha(config_for(layers)),
        f"oci://127.0.0.1:{port}/team/app:1",
        forward=forward,
        node_registry="127.0.0.1:5000",
    )
    assert receipt["result"] == "pushed", receipt
    assert registry.tags[("team/app", "1")] == receipt["image"]["manifest_digest"]
    import socket

    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), 1):
        pass  # the forward is gone


class FakeDocker:
    """Runner seam: ``docker image inspect`` and ``docker image save``."""

    def __init__(self, image_id: str, archive: bytes) -> None:
        self.image_id = image_id
        self.archive = archive
        self.calls: list[list[str]] = []

    def capture(self, argv, limits, env):
        self.calls.append(argv)
        if "inspect" in argv:
            return RunResult("succeeded", 0, self.image_id.encode() + b"\n", 0.0)
        if "info" in argv:
            return RunResult("succeeded", 0, b"[]", 0.0)
        raise AssertionError(argv)

    def read(self, argv, reader, limits, env, cancel=None):
        self.calls.append(argv)
        assert argv[-3:] == ["image", "save", self.image_id]
        return RunResult("succeeded", 0, b"", 0.0), reader(io.BytesIO(self.archive))

    def feed(self, *args, **kwargs):  # pragma: no cover - never used here
        raise AssertionError("registry delivery never feeds a process")


def test_docker_image_source_is_saved_by_id(registry, tmp_path):
    layers = [layer(b"img")]
    image_id = sha(config_for(layers))
    docker = FakeDocker(image_id, docker_archive(layers))
    tool = tmp_path / "docker"
    tool.write_bytes(b"#!/bin/sh\n")
    pin = ToolPin.capture(tool)
    url = f"oci://127.0.0.1:{registry.port}/team/app:1"
    delivery = RegistryDelivery(
        docker=pin, docker_socket=Path("/var/run/docker.sock"), runner=docker
    )
    receipt = delivery.deliver(
        DockerImageSource("example/app:1"),
        RegistryTarget.parse(url),
        DeliveryGrant(image_id, url, time.time() + 60),
        limits=ProcessLimits(60),
    )
    assert receipt["result"] == "pushed"
    assert receipt["source"]["local_image_id"] == image_id
    # Pass 1 (plan) and pass 2 (missing layers) both save the immutable ID.
    assert sum(1 for argv in docker.calls if "save" in argv) == 2
    docker.calls.clear()
    mismatch = delivery.deliver(
        DockerImageSource("example/app:1"),
        RegistryTarget.parse(url),
        DeliveryGrant("sha256:" + "3" * 64, url, time.time() + 60),
    )
    assert mismatch["reason"] == "digest-mismatch"
    assert not any("save" in argv for argv in docker.calls)


def reason(capsys) -> str:
    out, err = capsys.readouterr()
    body = json.loads(out)  # exactly one JSON object on stdout
    assert body["state"] == "rejected" and body["message"]
    assert err.startswith("rejected: ") and not err.lstrip().startswith("{")
    return body["reason"]


def test_cli_rejections_are_specific_fixed_codes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    path = write_archive(tmp_path, docker_archive([layer(b"a")]))
    digest = sha(config_for([layer(b"a")]))
    base = ["deliver", "--archive", str(path), "--approve-digest", digest]
    oci = "oci://127.0.0.1:5/a"
    cases = [
        ([*base, "--to", oci, "--ref", "a:1"], "node-options-on-registry-target"),
        ([*base, "--to", oci, "--namespace", "x"], "forward-options-without-forward"),
        (
            [*base, "--to", "docker://n?runtime=containerd", "--node-registry", "a"],
            "registry-options-on-node-target",
        ),
        (
            [*base, "--to", "oci://registry.example/a?tls=false"],
            "plain-http-not-loopback",
        ),
        ([*base, "--to", "oci://127.0.0.1:5/A"], "invalid-target"),
        ([*base, "--to", "ftp://node"], "invalid-target"),
        (
            [*base[:4], "sha256:nope", "--to", oci],
            "invalid-approved-digest",
        ),
        (
            [*base, "--to", oci, "--via-forward", "service/r"],
            "forward-options-incomplete",
        ),
        (
            [
                *base,
                "--to",
                oci,
                "--via-forward",
                "service/r",
                "--namespace",
                "r",
                "--kubeconfig",
                str(tmp_path / "kc"),
                "--context",
                "kind-test",
            ],
            "kubectl-tool-required",
        ),
        (
            ["deliver", "--image", "app:1", "--approve-digest", digest, "--to", oci],
            "docker-tool-required",
        ),
        (
            [*base, "--to", "ssh://node-1?runtime=k3s-containerd"],
            "ssh-tool-required",
        ),
        (
            [*base, "--to", oci, "--credentials", str(tmp_path / "missing.json")],
            "invalid-credentials-file",
        ),
    ]
    for argv, code in cases:
        assert cli.main(argv) == 2, code
        assert reason(capsys) == code
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"token": "t0ken"}))
    creds.chmod(0o644)
    assert cli.main([*base, "--to", oci, "--credentials", str(creds)]) == 2
    assert reason(capsys) == "credentials-file-not-private"
    # A forward whose kubectl is found on PATH still needs the node registry.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text("#!/bin/sh\n")
    (bin_dir / "kubectl").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    forward = [
        "--via-forward",
        "service/r",
        "--namespace",
        "r",
        "--kubeconfig",
        str(tmp_path / "kc"),
        "--context",
        "kind-test",
    ]
    assert cli.main([*base, "--to", oci, *forward]) == 2
    assert reason(capsys) == "node-registry-required"
    assert cli.main([*base, "--to", "oci://registry.example/a", *forward]) == 2
    assert reason(capsys) == "forward-target-not-loopback"
    wrong = ["--kubectl-sha256", "sha256:" + "0" * 64]
    assert cli.main([*base, "--to", oci, *forward, *wrong]) == 2
    assert reason(capsys) == "tool-pin-mismatch"


FAKE_DOCKER = """#!{python}
import sys
args = sys.argv[1:]
if args[:2] == ["context", "inspect"]:
    print("unix:///fake/run/docker.sock")
    raise SystemExit(0)
assert args[:2] == ["--host", "unix:///fake/run/docker.sock"], args
args = args[2:]
if args[:2] == ["image", "inspect"]:
    print("{image_id}")
elif args[:1] == ["info"]:
    print("[]")
elif args[:2] == ["image", "save"] and args[2] == "{image_id}":
    with open("{archive}", "rb") as stream:
        sys.stdout.buffer.write(stream.read())
else:
    raise SystemExit(3)
"""


def test_cli_discovers_and_pins_docker_from_path(
    registry, tmp_path, capsys, monkeypatch
):
    layers = [layer(b"discovered")]
    image_id = sha(config_for(layers))
    archive = write_archive(tmp_path, docker_archive(layers))
    real = tmp_path / "real" / "docker"
    real.parent.mkdir()
    real.write_text(
        FAKE_DOCKER.format(python=sys.executable, image_id=image_id, archive=archive)
    )
    real.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").symlink_to(real)  # like Docker Desktop's PATH entry
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    url = f"oci://127.0.0.1:{registry.port}/team/app:1"
    argv = ["deliver", "--image", "example/app:1", "--approve-digest", image_id]
    assert cli.main([*argv, "--to", url]) == 0, capsys.readouterr().err
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["result"] == "pushed"
    assert receipt["tools"] == {"docker": ToolPin.capture(real).sha256}
    assert str(tmp_path) not in json.dumps(receipt)
    # DOCKER_HOST wins over the context, and only unix sockets are accepted.
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    assert cli.main([*argv, "--to", url]) == 2
    assert reason(capsys) == "docker-socket-required"
    # An explicit pin that does not match is refused before anything runs.
    wrong = ["--docker-sha256", "sha256:" + "0" * 64, "--to", url]
    monkeypatch.delenv("DOCKER_HOST")
    assert cli.main([*argv, *wrong]) == 2
    assert reason(capsys) == "tool-pin-mismatch"


def test_discovery_helpers(tmp_path, monkeypatch):
    from piceli.artifacts.delivery_inputs import (
        DeliveryInputError,
        discover_docker_socket,
        discover_tool,
    )

    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    link = tmp_path / "link"
    link.symlink_to(tool)
    pin = discover_tool("docker", link)
    assert pin.path == tool.resolve() and pin == ToolPin.capture(tool)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(DeliveryInputError) as error:
        discover_tool("docker")
    assert error.value.code == "docker-tool-required"
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/other.sock")
    assert discover_docker_socket(pin) == Path("/var/run/other.sock")
    assert discover_docker_socket(pin, Path("/x.sock")) == Path("/x.sock")
    monkeypatch.setenv("DOCKER_HOST", "ssh://host")
    with pytest.raises(DeliveryInputError, match="unix"):
        discover_docker_socket(pin)


def test_scan_reports_plan_for_gzip_and_uncompressed_layers():
    plain = layer(b"p")
    body, config, manifest = oci_archive([plain], compress=True)
    plan = scan_image_stream(io.BytesIO(body))
    assert plan.config_digest == config and plan.manifest_digest == manifest
    assert plan.layers[0].media_type.endswith("+gzip")
    assert plan.platform == "linux/arm64"
    classic = scan_image_stream(io.BytesIO(docker_archive([plain])))
    assert classic.layers[0].digest == sha(plain)
    assert classic.names == ("example/app:1",)


def test_registry_modules_import_without_side_effects():
    script = r"""
import socket,subprocess,sys
def trap(*a,**kw): raise AssertionError("unexpected side effect")
socket.socket=trap
subprocess.Popen=trap
import piceli.artifacts.registry, piceli.artifacts.registry_delivery
import piceli.artifacts.image_manifest
assert "piceli.k8s.observe" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ},
    )
    assert result.returncode == 0, result.stderr
