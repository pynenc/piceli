"""0.89.0: derived images, a built image plus one layer of files, no build.

The spec rules (``from``, the keys a derived image takes, its contexts), the
deterministic layer, and the append against an in-process OCI registry: the
derived manifest keeps every base layer below its new one, a base index
gets one derived manifest per platform, and base blobs are mounted, not
downloaded.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.derived_image import (
    HISTORY_COMMENT,
    DerivedFile,
    append,
    write_layer_of,
)
from piceli.artifacts.host_build import HostBuildSpec
from piceli.artifacts.oci_layers import BlobStore, LayerError
from piceli.artifacts.registry import RegistryTarget, StreamedOciRegistryClient
from piceli.infra import provenance
from tests.oci_registry import (
    OCI_CONFIG,
    OCI_INDEX,
    OCI_LAYER,
    OCI_MANIFEST,
    OciRegistry,
    oci_registry,
    sha,
)

RELEASE = DerivedFile("/usr/local/share/release.json", b'{"release": "1.4.0"}\n')


def _document(**derived: Any) -> dict[str, Any]:
    image = {
        "name": "web",
        "repository": "shop/web",
        "from": "web-base",
        "files": {"release/release.json": "/usr/local/share/release.json"},
        "contexts": ["release"],
        **derived,
    }
    return {
        "revision": "piceli.host-build.v1",
        "name": "shop",
        "build": {"tools": ["true"], "commands": [["true"]]},
        "context": {
            "app": {"include": ["site/**"]},
            "release": {"include": ["release/**"]},
        },
        "output": {
            "image": [
                {
                    "name": "web-base",
                    "repository": "shop/web-base",
                    "contexts": ["app"],
                    "files": {"app/site": "/srv/site"},
                },
                {k: v for k, v in image.items() if v is not None},
            ]
        },
    }


def _spec(document: dict[str, Any]) -> HostBuildSpec:
    return HostBuildSpec.from_dict(document, Path("/"))


# ------------------------------------------------------------------ the spec


def test_an_image_with_from_is_a_derived_image() -> None:
    spec = _spec(_document(labels={"org.opencontainers.image.version": "1.4.0"}))
    assert [item.name for item in spec.images] == ["web-base"]
    (derived,) = spec.derived
    assert derived.base == "web-base" and derived.contexts == ("release",)
    assert derived.files == (("release/release.json", "/usr/local/share/release.json"),)
    assert derived.labels == (("org.opencontainers.image.version", "1.4.0"),)
    assert spec.to_dict()["derived"][0]["from"] == "web-base"


def test_a_spec_without_derived_images_keeps_its_digest() -> None:
    document = _document()
    document["output"]["image"] = document["output"]["image"][:1]
    assert "derived" not in _spec(document).to_dict()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"base": "scratch"}, "cannot set 'base'"),
        ({"entrypoint": ["/bin/web"]}, "cannot set 'entrypoint'"),
        ({"target_files": {"web": "/usr/bin/web"}}, "cannot set 'target_files'"),
        ({"user": "1:1"}, "cannot set 'user'"),
        ({"from": "web"}, "names itself"),
        ({"from": "nowhere"}, "is not an [[output.image]]"),
        ({"contexts": ["app"]}, "not in a context it reads"),
        ({"files": {}}, "files must map"),
    ],
)
def test_a_derived_image_is_refused_when_the_spec_is_loaded(
    change: dict[str, Any], message: str
) -> None:
    with pytest.raises(BuildSpecError) as refused:
        _spec(_document(**change))
    assert refused.value.code == "invalid-spec"
    assert message in str(refused.value)


def test_a_derived_image_cannot_derive_from_a_derived_image() -> None:
    document = _document()
    document["output"]["image"].append(
        {
            "name": "web-2",
            "repository": "shop/web-2",
            "from": "web",
            "files": {"release/release.json": "/release.json"},
            "contexts": ["release"],
        }
    )
    with pytest.raises(BuildSpecError, match="is a derived image"):
        _spec(document)


def test_no_command_runs_for_a_derived_image() -> None:
    document = _document()
    document["build"]["commands"] = [{"run": ["true"], "images": ["web"]}]
    with pytest.raises(BuildSpecError, match="no command runs for a derived"):
        _spec(document)


def test_a_local_build_spec_run_refuses_a_derived_image(tmp_path: Path) -> None:
    import argparse

    from piceli.artifacts.build_spec import _run_host_build_command

    text = """revision = "piceli.host-build.v1"
name = "shop"
[build]
tools = ["true"]
commands = [["true"]]
[context.app]
include = ["site/**"]
[context.release]
include = ["release/**"]
[[output.image]]
name = "web-base"
repository = "shop/web-base"
contexts = ["app"]
files = { "app/site" = "/srv/site" }
[[output.image]]
name = "web"
repository = "shop/web"
from = "web-base"
contexts = ["release"]
files = { "release/release.json" = "/release.json" }
"""
    path = tmp_path / "host-build.toml"
    path.write_text(text)
    with pytest.raises(BuildSpecError) as refused:
        _run_host_build_command(
            argparse.Namespace(spec=path, cache_dir=None), runner=None
        )
    assert refused.value.code == "derived-image-local-unsupported"


# ------------------------------------------------------------------ the layer


def _layer_digest(root: Path, files: list[DerivedFile], epoch: int = 0) -> str:
    return write_layer_of(BlobStore(root), files, epoch=epoch).descriptor.digest


def test_the_same_files_give_the_same_layer(tmp_path: Path) -> None:
    first = _layer_digest(tmp_path / "a", [RELEASE])
    assert first == _layer_digest(tmp_path / "b", [RELEASE])
    other = DerivedFile(RELEASE.path, b'{"release": "1.4.1"}\n')
    assert _layer_digest(tmp_path / "c", [other]) != first
    assert _layer_digest(tmp_path / "d", [RELEASE], epoch=1) != first


def test_the_layer_is_a_plain_deterministic_tar(tmp_path: Path) -> None:
    import io
    import tarfile

    store = BlobStore(tmp_path)
    written = write_layer_of(
        store,
        [RELEASE, DerivedFile("/usr/local/bin/hook", b"#!/bin/sh\n", True)],
        epoch=0,
    )
    body = store.path(written.descriptor.digest).read_bytes()
    assert body[4:8] == b"\0\0\0\0"  # gzip header: no time
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(body))) as archive:
        members = {item.name: item for item in archive.getmembers()}
    assert members["usr/local/share/release.json"].mode == 0o644
    assert members["usr/local/bin/hook"].mode == 0o755
    assert {item.uid for item in members.values()} == {0}
    assert {item.mtime for item in members.values()} == {0}
    assert {path: digest for path, digest, _ in written.files} == {
        "usr/local/share/release.json": RELEASE.sha256,
        "usr/local/bin/hook": DerivedFile("/x", b"#!/bin/sh\n").sha256,
    }


def test_a_derived_image_adds_at_least_one_file(tmp_path: Path) -> None:
    with pytest.raises(LayerError):
        write_layer_of(BlobStore(tmp_path), [], epoch=0)


# ------------------------------------------------------------------ the append


def _base(registry: OciRegistry, repository: str, arch: str, marker: bytes) -> str:
    """A real base image (gzip layer, ``diff_ids``) in ``repository``."""
    plain = marker * 512
    layer = gzip.compress(plain, mtime=0)
    config = json.dumps(
        {
            "architecture": arch,
            "os": "linux",
            "config": {"Entrypoint": ["/bin/web"]},
            "rootfs": {"type": "layers", "diff_ids": [sha(plain)]},
            "history": [{"created_by": "base"}],
        }
    ).encode()
    registry.blobs[(repository, sha(layer))] = layer
    registry.blobs[(repository, sha(config))] = config
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {
                "mediaType": OCI_CONFIG,
                "digest": sha(config),
                "size": len(config),
            },
            "layers": [
                {"mediaType": OCI_LAYER, "digest": sha(layer), "size": len(layer)}
            ],
        }
    ).encode()
    registry.manifests[(repository, sha(manifest))] = (manifest, OCI_MANIFEST)
    return sha(manifest)


def _client(registry: OciRegistry) -> StreamedOciRegistryClient:
    return StreamedOciRegistryClient(
        RegistryTarget.parse(f"oci://{registry.host}/shop/web").endpoint()
    )


@pytest.fixture
def registry() -> Iterator[OciRegistry]:
    with oci_registry() as found:
        yield found


def _manifest(registry: OciRegistry, repository: str, digest: str) -> dict[str, Any]:
    return dict(json.loads(registry.manifests[(repository, digest)][0]))


def test_the_append_keeps_every_base_layer_below_the_new_one(
    registry: OciRegistry, tmp_path: Path
) -> None:
    base = _base(registry, "shop/web-base", "arm64", b"base")
    store = BlobStore(tmp_path)
    layer = write_layer_of(store, [RELEASE], epoch=0).descriptor
    derived = append(
        _client(registry),
        source_repository="shop/web-base",
        base_digest=base,
        repository="shop/web",
        store=store,
        layer=layer,
        labels={"org.opencontainers.image.version": "1.4.0"},
    )
    assert derived.platforms == {"linux/arm64": derived.digest}
    before = _manifest(registry, "shop/web-base", base)
    after = _manifest(registry, "shop/web", derived.digest)
    assert after["layers"][:-1] == before["layers"]
    assert after["layers"][-1]["digest"] == layer.digest
    config = json.loads(registry.blobs[("shop/web", after["config"]["digest"])])
    old = json.loads(registry.blobs[("shop/web-base", before["config"]["digest"])])
    assert config["rootfs"]["diff_ids"] == [*old["rootfs"]["diff_ids"], layer.diff_id]
    assert config["history"][:-1] == old["history"]
    assert config["history"][-1]["comment"] == HISTORY_COMMENT
    assert config["config"]["Entrypoint"] == ["/bin/web"]  # the base's, kept
    assert config["config"]["Labels"] == {"org.opencontainers.image.version": "1.4.0"}
    # The base layer was mounted into shop/web, never downloaded.
    assert derived.blobs == {before["layers"][0]["digest"]: "mounted"}
    assert not [
        path
        for method, path in registry.requests
        if method == "GET" and before["layers"][0]["digest"] in path
    ]
    # The same base and files give the same derived digest.
    again = append(
        _client(registry),
        source_repository="shop/web-base",
        base_digest=base,
        repository="shop/web",
        store=store,
        layer=layer,
        labels={"org.opencontainers.image.version": "1.4.0"},
    )
    assert again.digest == derived.digest


def test_without_mounts_the_base_blobs_are_copied(
    registry: OciRegistry, tmp_path: Path
) -> None:
    registry.mounts = False
    base = _base(registry, "shop/web-base", "amd64", b"base")
    store = BlobStore(tmp_path)
    layer = write_layer_of(store, [RELEASE], epoch=0).descriptor
    derived = append(
        _client(registry),
        source_repository="shop/web-base",
        base_digest=base,
        repository="shop/web",
        store=store,
        layer=layer,
    )
    assert set(derived.blobs.values()) == {"copied"}
    assert ("shop/web", derived.digest) in registry.manifests


def test_a_base_index_gets_one_derived_manifest_per_platform(
    registry: OciRegistry, tmp_path: Path
) -> None:
    children = {
        arch: _base(registry, "shop/web-base", arch, arch.encode())
        for arch in ("amd64", "arm64")
    }
    entries = [
        {
            "mediaType": OCI_MANIFEST,
            "digest": digest,
            "size": len(registry.manifests[("shop/web-base", digest)][0]),
            "platform": {"os": "linux", "architecture": arch},
        }
        for arch, digest in children.items()
    ]
    # An attestation manifest describes the base, not the derived image.
    entries.append(
        {
            "mediaType": OCI_MANIFEST,
            "digest": children["amd64"],
            "size": 1,
            "platform": {"os": "unknown", "architecture": "unknown"},
        }
    )
    index = json.dumps(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries}
    ).encode()
    registry.manifests[("shop/web-base", sha(index))] = (index, OCI_INDEX)
    store = BlobStore(tmp_path)
    layer = write_layer_of(store, [RELEASE], epoch=0).descriptor
    derived = append(
        _client(registry),
        source_repository="shop/web-base",
        base_digest=sha(index),
        repository="shop/web",
        store=store,
        layer=layer,
    )
    assert derived.media_type == OCI_INDEX
    assert set(derived.platforms) == {"linux/amd64", "linux/arm64"}
    found = _manifest(registry, "shop/web", derived.digest)
    assert [item["platform"]["architecture"] for item in found["manifests"]] == [
        "amd64",
        "arm64",
    ]
    for arch, digest in children.items():
        child = _manifest(registry, "shop/web", derived.platforms[f"linux/{arch}"])
        assert (
            child["layers"][:-1]
            == _manifest(registry, "shop/web-base", digest)["layers"]
        )
        assert child["layers"][-1]["digest"] == layer.digest  # one shared blob


def test_a_missing_base_is_a_clear_error(registry: OciRegistry, tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    layer = write_layer_of(store, [RELEASE], epoch=0).descriptor
    with pytest.raises(LayerError) as failed:
        append(
            _client(registry),
            source_repository="shop/web-base",
            base_digest="sha256:" + "0" * 64,
            repository="shop/web",
            store=store,
            layer=layer,
        )
    assert failed.value.code == "derived-base-unavailable"


# ------------------------------------------------------------------ provenance


def test_the_provenance_names_the_base_digest_and_the_files() -> None:
    record = provenance.record(
        builder="derive",
        host="controller",
        builder_image=None,
        commits={"shop": "a" * 40},
        started_at="2026-11-27T10:00:00Z",
        finished_at="2026-11-27T10:00:01Z",
        derived_from={"image": "web-base", "digest": "sha256:" + "b" * 64},
        files={RELEASE.path: RELEASE.sha256},
        layer="sha256:" + "c" * 64,
    )
    cleaned = provenance.clean(record)
    assert cleaned is not None
    assert cleaned["derived_from"]["digest"] == "sha256:" + "b" * 64
    statement = json.loads(
        provenance.statement("shop/web", "sha256:" + "d" * 64, record)
    )
    definition = statement["predicate"]["buildDefinition"]
    assert definition["buildType"] == provenance.DERIVED_BUILD_TYPE
    assert definition["externalParameters"]["files"] == {RELEASE.path: RELEASE.sha256}
    dependencies = {
        item["name"]: item["digest"] for item in definition["resolvedDependencies"]
    }
    assert dependencies["image:web-base"] == {"sha256": "b" * 64}
    assert dependencies[f"file:{RELEASE.path}"] == {"sha256": RELEASE.sha256[7:]}
