"""Mirroring third-party images by digest between two in-process OCI registries."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from piceli.artifacts.mirror import (
    MirrorError,
    MirrorSource,
    Platform,
    mirror_image,
    same_image,
)
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryTarget,
    StreamedOciRegistryClient,
)
from tests.oci_registry import PASSWORD, USER, OciRegistry, oci_registry

DIGEST = "sha256:" + "d" * 64


@pytest.fixture
def source() -> Iterator[OciRegistry]:
    with oci_registry(auth="bearer") as registry:
        yield registry


@pytest.fixture
def target() -> Iterator[OciRegistry]:
    with oci_registry() as registry:
        yield registry


def _copy(
    source: OciRegistry,
    target: OciRegistry,
    reference: str,
    *,
    platform: str | None = "linux/arm64",
    credentials: RegistryCredentials | None = None,
) -> dict:
    parsed = MirrorSource.parse(reference)
    source_client = StreamedOciRegistryClient(
        RegistryTarget.parse(parsed.url).endpoint(credentials=credentials),
        actions="pull",
    )
    repository = parsed.mirrored_repository("mirror")
    target_client = StreamedOciRegistryClient(
        RegistryTarget.parse(f"oci://{target.host}/{repository}").endpoint()
    )
    return mirror_image(
        parsed,
        source_client,
        target_client,
        repository,
        platform=Platform.parse(platform) if platform else None,
        target_registry=target.host,
        node_registry="127.0.0.1:5000",
    )


# ------------------------------------------------------------------ references


def test_references_are_normalized_like_docker_pull() -> None:
    assert (
        MirrorSource.parse(f"redis@{DIGEST}").key == f"docker.io/library/redis@{DIGEST}"
    )
    assert (
        MirrorSource.parse(f"docker.io/library/redis:7.2@{DIGEST}").key
        == f"docker.io/library/redis@{DIGEST}"
    )
    assert MirrorSource.parse(f"index.docker.io/bitnami/redis@{DIGEST}").key == (
        f"docker.io/bitnami/redis@{DIGEST}"
    )
    ghcr = MirrorSource.parse(f"ghcr.io/example/cache:1@{DIGEST}")
    assert (ghcr.domain, ghcr.repository) == ("ghcr.io", "example/cache")
    assert ghcr.url == "oci://ghcr.io/example/cache?tls=true"
    hub = MirrorSource.parse(f"redis@{DIGEST}")
    assert hub.endpoint_host == "registry-1.docker.io"
    assert hub.mirrored_repository("mirror") == "mirror/docker.io/library/redis"
    local = MirrorSource.parse(f"localhost:5001/team/app@{DIGEST}")
    assert local.url == "oci://localhost:5001/team/app?tls=false"
    assert local.mirrored_repository("shop/mirror") == (
        "shop/mirror/localhost-5001/team/app"
    )
    assert same_image(f"redis:7@{DIGEST}", hub)
    assert not same_image("redis:7", hub)


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("docker.io/library/redis:7.2", "pipeline-mirror-not-pinned"),
        ("redis", "pipeline-mirror-not-pinned"),
        ("redis@sha256:abc", "pipeline-mirror-not-pinned"),
        (f"Bad Name@{DIGEST}", "pipeline-invalid"),
        (f"docker.io/UPPER/case@{DIGEST}", "pipeline-invalid"),
        ("", "pipeline-invalid"),
    ],
)
def test_only_digest_pinned_references_are_accepted(reference: str, code: str) -> None:
    with pytest.raises(MirrorError) as raised:
        MirrorSource.parse(reference)
    assert raised.value.code == code


def test_platforms() -> None:
    node = Platform.parse("linux/arm64")
    assert node.accepts({"os": "linux", "architecture": "arm64", "variant": "v8"})
    assert node.accepts({"os": "linux", "architecture": "arm64"})
    assert not node.accepts({"os": "linux", "architecture": "amd64"})
    assert not node.accepts({"os": "unknown", "architecture": "unknown"})
    assert str(Platform.parse("linux/arm/v7")) == "linux/arm/v7"
    with pytest.raises(MirrorError):
        Platform.parse("arm64")


# ---------------------------------------------------------------------- copies


def test_single_image_is_copied_anonymously_then_found_present(source, target) -> None:
    digest, _ = source.add_image("library/cache")
    reference = f"{source.host}/library/cache:7@{digest}"
    receipt = _copy(source, target, reference)
    assert receipt["state"] == "succeeded", receipt
    assert receipt["result"] == "mirrored"
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert receipt["pull_ref"] == f"127.0.0.1:5000/{repository}@{digest}"
    assert receipt["source"]["auth"] == "none"
    assert receipt["blobs"]["uploaded"] == 2
    assert (repository, digest) in target.manifests
    # Anonymous pull token: the source was asked for pull scope only.
    assert any(
        method == "GET" and path.startswith("/token") and path.endswith("%3Apull")
        for method, path in source.requests
    )
    assert not source.mutations()

    before = len(target.mutations())
    again = _copy(source, target, reference)
    assert again["result"] == "already-present"
    assert len(target.mutations()) == before


def test_index_keeps_its_digest_and_copies_only_the_node_platform(
    source, target
) -> None:
    index, children = source.add_index("library/cache")
    target.index_platforms = {("linux", "arm64")}  # like the registry's config
    receipt = _copy(source, target, f"{source.host}/library/cache@{index}")
    assert receipt["state"] == "succeeded", receipt
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert (repository, index) in target.manifests  # same digest as pinned
    assert (repository, children["arm64"]) in target.manifests
    assert (repository, children["amd64"]) not in target.manifests
    assert receipt["image"]["manifests"] == [
        {"digest": children["arm64"], "platform": "linux/arm64/v8"}
    ]
    assert receipt["platform"] == "linux/arm64"


def test_index_is_copied_completely_without_a_platform(source, target) -> None:
    index, children = source.add_index("library/cache")
    receipt = _copy(
        source, target, f"{source.host}/library/cache@{index}", platform=None
    )
    assert receipt["state"] == "succeeded", receipt
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert all((repository, child) in target.manifests for child in children.values())
    assert receipt["platform"] == "all"


def test_missing_platform_is_refused_before_anything_is_pushed(source, target) -> None:
    index, _ = source.add_index("library/cache", arches=("amd64",))
    receipt = _copy(source, target, f"{source.host}/library/cache@{index}")
    assert (receipt["state"], receipt["reason"]) == (
        "failed",
        "mirror-platform-unavailable",
    )
    assert not target.mutations()
    single, _ = source.add_image("library/other", arch="amd64")
    receipt = _copy(source, target, f"{source.host}/library/other@{single}")
    assert receipt["reason"] == "mirror-platform-unavailable"
    assert not target.mutations()


def test_bytes_that_do_not_hash_to_the_digest_are_rejected(source, target) -> None:
    digest, _ = source.add_image("library/cache")
    source.tamper.add(digest)
    receipt = _copy(source, target, f"{source.host}/library/cache@{digest}")
    assert (receipt["state"], receipt["reason"]) == (
        "rejected",
        "mirror-digest-mismatch",
    )
    assert not target.mutations()


def test_unknown_digest_fails_with_a_registered_code(source, target) -> None:
    receipt = _copy(source, target, f"{source.host}/library/cache@{DIGEST}")
    assert (receipt["state"], receipt["reason"]) == ("failed", "manifest-not-found")


def test_credentials_are_used_but_never_follow_a_redirect_or_leak(
    source, target, capsys
) -> None:
    source.anonymous_token = False
    digest, _ = source.add_image("team/private")
    with oci_registry() as cdn:
        source.redirect_to = cdn
        credentials = RegistryCredentials(USER, PASSWORD)
        reference = f"{source.host}/team/private@{digest}"
        receipt = _copy(source, target, reference, credentials=credentials)
        assert receipt["state"] == "succeeded", receipt
        assert receipt["source"]["auth"] == "basic"
        # Blobs came from the second origin, which never saw a credential.
        assert any(path.startswith("/cdn/") for _, path in cdn.requests)
        assert not any(cdn.authorizations)
    text = json.dumps(receipt) + capsys.readouterr().out
    assert PASSWORD not in text and USER not in text

    target_copy = OciRegistry()
    try:
        failed = _copy(source, target_copy, reference)  # no credentials now
    finally:
        target_copy.close()
    assert (failed["state"], failed["reason"]) == ("failed", "registry-unauthorized")
