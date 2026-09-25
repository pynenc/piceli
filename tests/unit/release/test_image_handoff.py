"""Image handoff: immutable references from build, registry and node receipts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from piceli.cli_contract import Rejected
from piceli.k8s.cli.release import _refuse
from piceli.k8s.release_spec import (
    ImageHandoffError,
    ImageRef,
    NodeRef,
    ReleaseSpec,
    content_tag,
    load_delivery_receipt,
    load_images_from,
)

CONFIG_A = "sha256:" + "a" * 64
CONFIG_B = "sha256:" + "b" * 64
MANIFEST_A = "sha256:" + "1" * 64
MANIFEST_B = "sha256:" + "2" * 64
LOCAL_MANIFEST = "sha256:" + "3" * 64


def _spec(tmp_path: Path, **overrides: Any) -> ReleaseSpec:
    value: dict[str, Any] = {
        "target": {"kubeconfig": "kc", "context": "ctx", "namespace": "demo"},
        "release": {
            "name": "web",
            "owner": "owner",
            "field_manager": "manager",
            "composition": "compose.py:build",
            "state_dir": "state",
        },
    }
    value.update(overrides)
    return ReleaseSpec.from_dict(value, tmp_path)


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document))
    return path


def _build(path: Path, **images: dict[str, Any]) -> Path:
    return _write(
        path,
        {"revision": "piceli.build-receipt.v1", "outputs": {"images": images}},
    )


def _registry(path: Path, config: str, manifest: str, repo: str, **extra: Any) -> Path:
    document = {
        "schema": "piceli.registry-delivery.v1",
        "result": "pushed",
        "approved_digest": config,
        "image": {"manifest_digest": manifest, "config_digest": config},
        "pull_ref": f"127.0.0.1:5000/{repo}@{manifest}",
    }
    document.update(extra)
    return _write(path, document)


def _node(path: Path, config: str, reference: str, **extra: Any) -> Path:
    document = {
        "schema": "piceli.node-delivery.v1",
        "result": "imported",
        "state": "succeeded",
        "approved_digest": config,
        "image": {
            "reference": reference,
            "config_digest": config,
            "node_target_digest": LOCAL_MANIFEST,
        },
    }
    document.update(extra)
    return _write(path, document)


def _local(config: str, ref: str) -> dict[str, Any]:
    return {"image_id": config, "digest": None, "platform": "linux/arm64", "ref": ref}


# --- the immutability rule ------------------------------------------------------


def test_content_tag() -> None:
    assert content_tag(CONFIG_A) == "sha256-" + "a" * 12
    assert content_tag(CONFIG_A, 64) == "sha256-" + "a" * 64
    with pytest.raises(ValueError):
        content_tag("latest")
    with pytest.raises(ValueError):
        content_tag(CONFIG_A, 8)


def test_digest_references_are_immutable() -> None:
    image = ImageRef("a", MANIFEST_A, "repo/a", "r1", MANIFEST_A)
    assert image.immutable
    assert image.reference == f"repo/a@{MANIFEST_A}"


@pytest.mark.parametrize(
    "tag",
    ["r1", "latest", "sha256-" + "a" * 12],  # a content tag without node proof
)
def test_build_receipt_tag_is_never_a_reference(tmp_path: Path, tag: str) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, f"app/api:{tag}"))
    image = _spec(tmp_path, images_from="build.json").images()["api"]
    assert image.identity == CONFIG_A
    assert not image.immutable
    with pytest.raises(ImageHandoffError) as refused:
        _ = image.reference
    assert refused.value.code == "image-not-immutable"


def test_build_receipt_with_manifest_digest_is_immutable(tmp_path: Path) -> None:
    _build(
        tmp_path / "build.json",
        api={"image_id": CONFIG_A, "digest": LOCAL_MANIFEST, "ref": "app/api:r1"},
    )
    image = _spec(tmp_path, images_from="build.json").images()["api"]
    assert image.reference == f"app/api@{LOCAL_MANIFEST}"


# --- node-delivery receipts ------------------------------------------------------


def test_node_receipt_with_content_tag(tmp_path: Path) -> None:
    reference = f"docker.io/app/api:{content_tag(CONFIG_A)}"
    _node(tmp_path / "api.node.json", CONFIG_A, reference)
    spec = _spec(tmp_path, images={"api": {"receipt": "api.node.json"}})
    image = spec.images()["api"]
    assert image.reference == reference
    assert image.identity == CONFIG_A
    assert image.digest is None
    assert image.source == "piceli.node-delivery.v1"
    # a longer prefix (up to the full digest) is also a content tag
    full = f"docker.io/app/api:sha256-{'a' * 64}"
    _node(tmp_path / "full.json", CONFIG_A, full, result="already-present")
    assert load_delivery_receipt("api", tmp_path / "full.json").reference == full


def test_node_receipt_pin_is_the_config_digest(tmp_path: Path) -> None:
    reference = f"docker.io/app/api:{content_tag(CONFIG_A)}"
    _node(tmp_path / "api.node.json", CONFIG_A, reference)
    load_delivery_receipt("api", tmp_path / "api.node.json", CONFIG_A)
    with pytest.raises(ImageHandoffError) as refused:
        load_delivery_receipt("api", tmp_path / "api.node.json", CONFIG_B)
    assert refused.value.code == "image-digest-mismatch"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"reference": "docker.io/app/api:r1"}, "image-not-immutable"),
        ({"reference": "docker.io/app/api:latest"}, "image-not-immutable"),
        (
            {"reference": f"docker.io/app/api:{content_tag(CONFIG_B)}"},
            "image-not-immutable",
        ),
        ({"reference": "docker.io/app/api:sha256-aaaaaaaaaa"}, "image-not-immutable"),
        ({"reference": f"docker.io/app/api@{CONFIG_A}"}, "receipt-invalid"),
        ({"approved_digest": CONFIG_B}, "receipt-invalid"),
        ({"result": "rejected"}, "delivery-not-succeeded"),
        ({"result": "failed"}, "delivery-not-succeeded"),
        ({"result": "pushed"}, "delivery-not-succeeded"),
        ({"config_digest": "sha256:short"}, "receipt-invalid"),
    ],
)
def test_invalid_node_receipts(
    tmp_path: Path, overrides: dict[str, Any], code: str
) -> None:
    reference = overrides.pop("reference", f"docker.io/app/api:{content_tag(CONFIG_A)}")
    config = overrides.pop("config_digest", CONFIG_A)
    path = _node(tmp_path / "r.json", CONFIG_A, reference, **overrides)
    if config != CONFIG_A:
        document = json.loads(path.read_text())
        document["image"]["config_digest"] = config
        _write(path, document)
    with pytest.raises(ImageHandoffError) as refused:
        load_delivery_receipt("api", path)
    assert refused.value.code == code


def test_build_receipt_is_not_a_delivery_receipt(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:r1"))
    with pytest.raises(ImageHandoffError) as refused:
        load_delivery_receipt("api", tmp_path / "build.json")
    assert refused.value.code == "receipt-invalid"


# --- images_from lists and merge rules -------------------------------------------


def test_images_from_list_merges_build_and_deliveries(tmp_path: Path) -> None:
    _build(
        tmp_path / "build.json",
        api=_local(CONFIG_A, "app/api:dev"),
        worker=_local(CONFIG_B, "app/worker:dev"),
    )
    _registry(tmp_path / "api.json", CONFIG_A, MANIFEST_A, "app/api")
    node_ref = f"docker.io/app/worker:{content_tag(CONFIG_B)}"
    _node(tmp_path / "worker.json", CONFIG_B, node_ref)
    spec = _spec(tmp_path, images_from=["build.json", "api.json", "worker.json"])
    images = spec.images()
    assert images["api"].reference == f"127.0.0.1:5000/app/api@{MANIFEST_A}"
    assert images["api"].identity == MANIFEST_A
    assert images["api"].platform == "linux/arm64"  # kept from the build
    assert images["worker"].reference == node_ref
    assert images["worker"].identity == CONFIG_B
    # the order of the list does not matter
    reordered = _spec(tmp_path, images_from=["worker.json", "api.json", "build.json"])
    assert reordered.images() == images


def test_images_from_single_path_still_works(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    assert list(_spec(tmp_path, images_from="build.json").images()) == ["api"]
    with pytest.raises(ImageHandoffError, match="receipt-unmatched"):
        _registry(tmp_path / "api.json", CONFIG_A, MANIFEST_A, "app/api")
        _spec(tmp_path, images_from="api.json").images()


def test_images_from_list_must_not_be_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lists no receipt"):
        _spec(tmp_path, images_from=[])


def test_delivery_matching_no_built_image_is_refused(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    _registry(tmp_path / "other.json", CONFIG_B, MANIFEST_B, "app/other")
    with pytest.raises(ImageHandoffError) as refused:
        _spec(tmp_path, images_from=["build.json", "other.json"]).images()
    assert refused.value.code == "receipt-unmatched"


def test_two_deliveries_of_one_image_are_refused(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    _registry(tmp_path / "r.json", CONFIG_A, MANIFEST_A, "app/api")
    _node(tmp_path / "n.json", CONFIG_A, f"docker.io/app/api:{content_tag(CONFIG_A)}")
    with pytest.raises(ImageHandoffError) as refused:
        _spec(tmp_path, images_from=["build.json", "r.json", "n.json"]).images()
    assert refused.value.code == "image-declared-twice"


def test_one_image_in_two_build_receipts_is_refused(tmp_path: Path) -> None:
    _build(tmp_path / "one.json", api=_local(CONFIG_A, "app/api:dev"))
    _build(tmp_path / "two.json", api=_local(CONFIG_B, "app/api:dev"))
    with pytest.raises(ImageHandoffError) as refused:
        _spec(tmp_path, images_from=["one.json", "two.json"]).images()
    assert refused.value.code == "image-declared-twice"


def test_unknown_document_in_images_from_is_refused(tmp_path: Path) -> None:
    _write(tmp_path / "x.json", {"schema": "something-else"})
    with pytest.raises(ImageHandoffError) as refused:
        _spec(tmp_path, images_from=["x.json"]).images()
    assert refused.value.code == "receipt-invalid"


def test_named_receipt_overrides_built_image(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    _registry(tmp_path / "api.json", CONFIG_A, MANIFEST_A, "app/api")
    spec = _spec(
        tmp_path, images_from="build.json", images={"api": {"receipt": "api.json"}}
    )
    assert spec.images()["api"].reference == f"127.0.0.1:5000/app/api@{MANIFEST_A}"


def test_named_receipt_for_another_image_is_refused(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    _registry(tmp_path / "api.json", CONFIG_B, MANIFEST_B, "app/api")
    spec = _spec(
        tmp_path, images_from="build.json", images={"api": {"receipt": "api.json"}}
    )
    with pytest.raises(ImageHandoffError) as refused:
        spec.images()
    assert refused.value.code == "image-digest-mismatch"


def test_manifest_digest_matches_a_containerd_store_build(tmp_path: Path) -> None:
    # A containerd-store build records a manifest digest; a registry push of
    # the same manifest matches it even when image_id is not the config digest.
    _build(
        tmp_path / "build.json",
        api={"image_id": LOCAL_MANIFEST, "digest": MANIFEST_A, "ref": "app/api:dev"},
    )
    _registry(tmp_path / "api.json", CONFIG_A, MANIFEST_A, "app/api")
    images = load_images_from([tmp_path / "build.json", tmp_path / "api.json"])
    assert images["api"].reference == f"127.0.0.1:5000/app/api@{MANIFEST_A}"


def test_plain_declaration_and_images_from_still_collide(tmp_path: Path) -> None:
    _build(tmp_path / "build.json", api=_local(CONFIG_A, "app/api:dev"))
    spec = _spec(
        tmp_path, images_from="build.json", images={"api": f"repo/api@{MANIFEST_A}"}
    )
    with pytest.raises(ImageHandoffError) as refused:
        spec.images()
    assert refused.value.code == "image-declared-twice"


# --- CLI surface -----------------------------------------------------------------


def test_refusal_json_carries_the_code(capsys: pytest.CaptureFixture[str]) -> None:
    image = ImageRef(
        "api", CONFIG_A, "app/api", "dev", image_id=CONFIG_A, ref="app/api:dev"
    )
    with pytest.raises(ImageHandoffError) as refused:
        _ = image.reference
    with pytest.raises(Rejected) as exited:
        _refuse(refused.value)
    assert exited.value.code == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["state"] == "rejected"
    assert refusal["reason"] == refusal["code"] == "image-not-immutable"
    assert refusal["message"].startswith("image-not-immutable: image 'api'")


def test_two_images_example(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3] / "examples" / "two-images"
    for name in ("composition.py", "registry.py", "release.toml", "registry.toml"):
        shutil.copy(root / name, tmp_path / name)
    _registry(
        tmp_path / "alpha.delivery.json", CONFIG_A, MANIFEST_A, "two-images/alpha"
    )
    _registry(tmp_path / "beta.delivery.json", CONFIG_B, MANIFEST_B, "two-images/beta")
    node = {"primary": NodeRef("two-images-control-plane", "uid-1")}

    spec = ReleaseSpec.from_toml(tmp_path / "release.toml")
    composition = spec.load_composition()(spec.context(spec.images(), {}, node))
    pods = {
        component.name: component.resources[0].manifest["spec"]["template"]["spec"]
        for component in composition.components
    }
    assert sorted(pods) == ["alpha", "beta"]
    assert pods["alpha"]["containers"][0]["image"] == (
        f"127.0.0.1:5000/two-images/alpha@{MANIFEST_A}"
    )
    assert pods["beta"]["nodeSelector"] == {
        "kubernetes.io/hostname": "two-images-control-plane"
    }

    registry = ReleaseSpec.from_toml(tmp_path / "registry.toml")
    built = registry.load_composition()(registry.context(registry.images(), {}, node))
    (component,) = built.components
    kinds = sorted(resource.ref.kind for resource in component.resources)
    assert kinds == ["ConfigMap", "Deployment", "PersistentVolumeClaim"]
