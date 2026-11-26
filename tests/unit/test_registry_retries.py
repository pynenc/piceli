"""0.78.0: one flaky request no longer fails a copy (ops#100).

The registry client retries transient failures with a bounded backoff,
resumes a chunked upload from the session's ``Range``, starts a blob again
when its session is gone, and, when it still fails, says where and why in
the receipt and the CLI line (never a credential). Sleeps are injected, so
nothing here waits.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.cli import build_parser, run_copy_command
from piceli.artifacts.image_copy import Copier, CopyGrant, CopyPlan
from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    RetryPolicy,
    StreamedOciRegistryClient,
    describe,
)
from tests.oci_registry import PASSWORD, USER, Fault, OciRegistry, oci_registry, sha

REPO = "shop/api"
UPLOADS = "/blobs/uploads/"


@pytest.fixture
def source() -> Iterator[OciRegistry]:
    with oci_registry() as registry:
        yield registry


@pytest.fixture
def target() -> Iterator[OciRegistry]:
    with oci_registry() as registry:
        yield registry


def _factory(sleeps: list[float] | None = None) -> Any:
    """Clients with small chunks (so a layer is chunked) and no real sleep."""
    return partial(
        StreamedOciRegistryClient,
        chunk_size=4096,
        monolithic_max=1024,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


def _image(registry: OciRegistry) -> tuple[str, str, bytes]:
    """A single-platform image whose 11 kB layer goes up in three chunks."""
    digest, body = registry.add_image(REPO, arch="amd64", marker=b"linux/amd64")
    layer = json.loads(body)["layers"][0]["digest"]
    return digest, layer, registry.blobs[(REPO, layer)]


def _copy(source: OciRegistry, target: OciRegistry, **copier: Any) -> dict[str, Any]:
    digest, _, _ = _image(source)
    plan = CopyPlan.parse(
        f"oci://{source.host}/{REPO}@{digest}",
        f"oci://{target.host}/example/api:1.0",
        referrers=False,
    )
    copier.setdefault("client_factory", _factory())
    return Copier(**copier).copy(plan, CopyGrant(plan.digest, 4e9))


def _patches(target: OciRegistry) -> list[str]:
    return [path for method, path in target.requests if method == "PATCH"]


@pytest.mark.parametrize("action", ["drop", "drop-applied"])
def test_a_connection_dropped_mid_upload_resumes_from_the_range(
    source: OciRegistry, target: OciRegistry, action: str
) -> None:
    """The second chunk's connection drops once, before or after the registry
    stored it: the session's Range says where to go on, and the copy passes."""
    target.faults.append(Fault("PATCH", UPLOADS, action, skip=1))
    receipt = _copy(source, target)
    assert receipt["state"] == "copied", receipt
    _, layer, data = _image(source)
    assert target.blobs[("example/api", layer)] == data
    [retry] = receipt["retries"]
    assert retry["side"] == "target" and retry["method"] == "PATCH"
    assert retry["action"] == "resume" and retry["attempt"] == 1
    assert retry["digest"] == layer and retry["offset"] == 4096
    assert retry["cause"] == "RemoteDisconnected"
    assert retry["path"].startswith("/v2/example/api/blobs/uploads/")
    assert ("GET", retry["path"]) in target.requests  # the session was asked
    # Nothing was sent twice when the registry already had the chunk.
    assert len(_patches(target)) == (4 if action == "drop" else 3)


def test_a_session_that_is_gone_restarts_the_blob(
    source: OciRegistry, target: OciRegistry
) -> None:
    target.faults.append(Fault("PATCH", UPLOADS, "forget", skip=1))
    receipt = _copy(source, target)
    assert receipt["state"] == "copied", receipt
    _, layer, data = _image(source)
    assert target.blobs[("example/api", layer)] == data
    assert [r["action"] for r in receipt["retries"]] == ["resume", "restart"]
    restart = receipt["retries"][1]
    assert restart["digest"] == layer and restart["offset"] == 4096
    assert restart["cause"] == "upload session gone"


def test_a_source_stream_cut_mid_blob_starts_the_blob_again(
    source: OciRegistry, target: OciRegistry
) -> None:
    _, layer, _ = _image(source)
    source.faults.append(Fault("GET", f"/blobs/{layer}", "cut"))
    receipt = _copy(source, target)
    assert receipt["state"] == "copied", receipt
    [retry] = receipt["retries"]
    assert (retry["side"], retry["method"], retry["action"]) == (
        "source",
        "GET",
        "restart",
    )
    assert retry["cause"] == "IncompleteRead"


def test_a_get_that_times_out_once_is_retried(target: OciRegistry) -> None:
    digest, body = target.add_image(REPO)
    target.faults.append(Fault("GET", "/manifests/", "stall", seconds=0.6))
    endpoint = RegistryEndpoint("127.0.0.1", target.port, timeout=0.2)
    client = StreamedOciRegistryClient(endpoint, sleep=lambda _: None)
    assert client.get_manifest(REPO, digest)[0] == body
    [retry] = client.retries
    assert (retry["method"], retry["cause"]) == ("GET", "TimeoutError")


def test_retry_after_is_honoured_within_bounds(target: OciRegistry) -> None:
    digest, _ = target.add_image(REPO)
    target.faults.append(Fault("HEAD", "/manifests/", "status", retry_after="3"))
    # Faults count only the requests that reach them: this one breaks the 2nd.
    target.faults.append(Fault("HEAD", "/manifests/", "status", retry_after="900"))
    sleeps: list[float] = []
    client = StreamedOciRegistryClient(
        RegistryEndpoint("127.0.0.1", target.port),
        retry=RetryPolicy(base=0.0, retry_after_max=60.0),
        sleep=sleeps.append,
    )
    assert client.manifest_digest(REPO, digest) == digest
    assert sleeps == [3.0, 60.0]
    assert [r["cause"] for r in client.retries] == ["HTTP 503", "HTTP 503"]


def test_retries_stop_at_the_attempts_and_the_deadline(target: OciRegistry) -> None:
    digest, _ = target.add_image(REPO)
    target.faults.append(Fault("GET", "/manifests/", "status", times=99))
    client = StreamedOciRegistryClient(
        RegistryEndpoint("127.0.0.1", target.port), sleep=lambda _: None
    )
    with pytest.raises(RegistryError) as raised:
        client.get_manifest(REPO, digest)
    assert raised.value.status == 503 and len(client.retries) == 4
    late = StreamedOciRegistryClient(
        RegistryEndpoint("127.0.0.1", target.port), deadline=0.0
    )
    with pytest.raises(RegistryError):
        late.get_manifest(REPO, digest)
    assert late.retries == []


def test_a_seekable_source_restarts_its_blob_in_place(target: OciRegistry) -> None:
    """``artifacts publish`` uploads files and bytes: a lost session past the
    first chunk starts the blob again from the start of the file."""
    data = os.urandom(10_000)
    target.faults.append(Fault("PATCH", UPLOADS, "forget", skip=1))
    client = _factory()(RegistryEndpoint("127.0.0.1", target.port))
    upload = client.push_blob(REPO, sha(data), len(data), io.BytesIO(data))
    assert upload.mode == "chunked"
    assert target.blobs[(REPO, sha(data))] == data
    assert [r["action"] for r in client.retries] == ["resume", "restart"]


def test_a_failure_that_stays_names_side_host_request_and_cause(
    source: OciRegistry,
    target: OciRegistry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target.auth = "bearer"
    target.faults.append(Fault("PATCH", UPLOADS, "drop", skip=1, times=999))
    digest, layer, _ = _image(source)
    credentials = tmp_path / "push.json"
    credentials.write_text(json.dumps({"username": USER, "password": PASSWORD}))
    credentials.chmod(0o600)
    origin = f"oci://{source.host}/{REPO}@{digest}"
    to = f"oci://{target.host}/example/api:1.0"
    common = ["copy", "--from", origin, "--to", to, "--no-referrers"]
    common += ["--credentials", str(credentials), "--attempts", "1"]
    plan = CopyPlan.parse(origin, to, referrers=False)
    args = build_parser().parse_args([*common, "--approve", plan.digest])
    code = run_copy_command(args, client_factory=_factory())
    out, err = capsys.readouterr()
    receipt = json.loads(out)
    assert code == 1 and receipt["reason"] == "registry-unreachable"
    error = receipt["error"]
    assert error["side"] == "target" and error["host"] == target.host
    assert error["method"] == "PATCH" and error["digest"] == layer
    assert error["path"].startswith("/v2/example/api/blobs/uploads/")
    assert error["exception"] == "RemoteDisconnected"
    # 4 retries of the chunk, then the blob starts again (twice), each time
    # failing at its first chunk.
    assert error["offset"] == 0
    assert [r["action"] for r in receipt["retries"]].count("restart") == 2
    assert len(receipt["retries"]) == 14
    line = f"copy: failed [registry-unreachable] {describe(error)}"
    assert line in err
    assert f"target {target.host} PATCH blob {layer[:15]}… at 0" in err
    assert err.rstrip().endswith(": RemoteDisconnected")
    for secret in (PASSWORD, "issued-token", "Basic ", "Bearer "):
        assert secret not in out and secret not in err


def test_describe_matches_the_issue_example() -> None:
    detail = {
        "side": "target",
        "host": "registry.example.org",
        "method": "PATCH",
        "path": "/v2/shop/api/blobs/uploads/1",
        "digest": "sha256:9072a3f9" + "0" * 56,
        "offset": 33554432,
        "exception": "ConnectionResetError",
        "errno": 54,
    }
    assert describe(detail) == (
        "target registry.example.org PATCH blob sha256:9072a3f9… at 33554432: "
        "ConnectionResetError(54)"
    )
    assert describe({}) == ""
    assert describe({"side": "source", "status": 503}) == "source: HTTP 503"


def test_recorded_paths_keep_no_query() -> None:
    error = RegistryError("registry-unreachable")
    client = StreamedOciRegistryClient(
        RegistryTarget.parse("oci://registry.example.org/shop/api").endpoint()
    )
    client._describe(error, "PATCH", "/v2/shop/api/blobs/uploads/1?_state=secret")
    assert error.public()["path"] == "/v2/shop/api/blobs/uploads/1"
    assert error.public()["host"] == "registry.example.org"
