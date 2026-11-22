"""0.74.0: ``piceli artifacts copy`` promotes an image by digest between two
in-process OCI registries: the index, every platform manifest and blob, and
the referrers (SBOM, provenance, a signature of the SBOM), digests kept."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.cli import build_parser, run_copy_command
from piceli.artifacts.image_copy import (
    Copier,
    CopyError,
    CopyGrant,
    CopyPlan,
    CopySource,
)
from piceli.artifacts.publish import referrers
from piceli.artifacts.registry import RegistryTarget, StreamedOciRegistryClient
from piceli.artifacts.registry_delivery import RegistryDelivery
from tests.oci_registry import OCI_INDEX, OciRegistry, oci_registry, sha

REPO = "shop/api"
BOTH = ("linux/amd64", "linux/arm64")


@pytest.fixture
def source() -> Iterator[OciRegistry]:
    with oci_registry() as registry:
        yield registry


@pytest.fixture
def target() -> Iterator[OciRegistry]:
    with oci_registry() as registry:
        yield registry


def _built(registry: OciRegistry) -> tuple[str, dict[str, str], list[str]]:
    """What a pipeline build leaves: one manifest per platform, the index the
    builder pushes over them, and referrers attached the way a build does."""
    children = {
        platform: registry.add_image(
            REPO, arch=platform.split("/")[1], marker=platform.encode()
        )[0]
        for platform in BOTH
    }
    delivery = RegistryDelivery()
    repository = RegistryTarget.parse(f"oci://{registry.host}/{REPO}")
    index = delivery.push_index(repository, children)
    attached = [
        delivery.attach_referrer(
            repository, children["linux/arm64"], b'{"provenance": 1}'
        )["digest"],
        delivery.attach_referrer(
            repository, index, b'{"spdx": 1}', kind="sbom", title="api.sbom.json"
        )["digest"],
    ]
    # A signature of the SBOM: a referrer of a referrer.
    attached.append(
        delivery.attach_referrer(
            repository, attached[1], b'{"signature": 1}', title="sig.json"
        )["digest"]
    )
    return index, children, attached


def _run(*arguments: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    args = build_parser().parse_args(["copy", *arguments])
    code = run_copy_command(args)
    return code, json.loads(capsys.readouterr().out)


def _client(registry: OciRegistry) -> StreamedOciRegistryClient:
    return StreamedOciRegistryClient(
        RegistryTarget.parse(f"oci://{registry.host}/{REPO}").endpoint()
    )


def test_the_builders_index_holds_every_platform(source: OciRegistry) -> None:
    index, children, _ = _built(source)
    body, media = source.manifests[(REPO, index)]
    assert media == OCI_INDEX and sha(body) == index
    document = json.loads(body)
    assert {
        f"{item['platform']['os']}/{item['platform']['architecture']}": item["digest"]
        for item in document["manifests"]
    } == children


def test_a_copy_keeps_the_index_every_platform_and_the_referrers(
    source: OciRegistry,
    target: OciRegistry,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    index, children, attached = _built(source)
    origin = f"oci://{source.host}/{REPO}@{index}"
    to = f"oci://{target.host}/example/api:1.4.0"
    code, plan = _run("--from", origin, "--to", to, capsys=capsys)
    assert code == 3 and plan["state"] == "approval-required"
    assert target.requests == []  # the plan contacts nothing
    receipt_path = tmp_path / "copy.json"
    code, receipt = _run(
        "--from", origin, "--to", to, "--approve", plan["plan_hash"],
        "--out", str(receipt_path), capsys=capsys,
    )  # fmt: skip
    assert code == 0, receipt
    assert receipt["state"] == "copied" and receipt["digest"] == index
    assert receipt["media_type"] == OCI_INDEX
    assert receipt["platforms"] == children
    assert json.loads(receipt_path.read_text())["digest"] == index
    # The target serves the same bytes under the tag and every digest.
    assert (
        target.manifests[("example/api", "1.4.0")][0]
        == (source.manifests[(REPO, index)][0])
    )
    for digest in children.values():
        assert ("example/api", digest) in target.manifests
    client = StreamedOciRegistryClient(
        RegistryTarget.parse(f"oci://{target.host}/example/api").endpoint()
    )
    assert {item["digest"] for item in receipt["referrers"]} == set(attached)
    listed = {
        item["digest"]
        for subject in (index, children["linux/arm64"], attached[1])
        for item in referrers(client, "example/api", subject)
    }
    assert listed == set(attached)
    assert receipt["blobs"]["uploaded"] > 0
    # Again: everything is there, nothing is uploaded.
    code, again = _run(
        "--from", origin, "--to", to, "--approve", plan["plan_hash"], capsys=capsys
    )  # fmt: skip
    assert code == 0 and again["result"] == "already-present"
    assert again["blobs"]["uploaded"] == 0


def test_a_tag_that_names_another_image_is_refused_before_any_push(
    source: OciRegistry, target: OciRegistry
) -> None:
    index, _, _ = _built(source)
    other, body = target.add_image("example/api", arch="amd64")
    target.manifests[("example/api", "1.4.0")] = (body, "x")
    plan = CopyPlan.parse(
        f"oci://{source.host}/{REPO}@{index}",
        f"oci://{target.host}/example/api:1.4.0",
    )
    receipt = Copier().copy(plan, CopyGrant(plan.digest, 4e9))
    assert receipt["state"] == "failed" and receipt["reason"] == "copy-tag-exists"
    assert target.mutations() == []
    moved = CopyPlan.parse(
        f"oci://{source.host}/{REPO}@{index}",
        f"oci://{target.host}/example/api:1.4.0",
        move_tag=True,
    )
    receipt = Copier().copy(moved, CopyGrant(moved.digest, 4e9))
    assert receipt["state"] == "copied" and receipt["previous"] == other


def test_a_single_platform_image_and_no_referrers(
    source: OciRegistry, target: OciRegistry
) -> None:
    digest, _ = source.add_image(REPO, arch="arm64")
    plan = CopyPlan.parse(
        f"{source.host}/{REPO}@{digest}",  # oci:// may be left out
        f"oci://{target.host}/example/api",
        referrers=False,
    )
    receipt = Copier().copy(plan, CopyGrant(plan.digest, 4e9))
    assert receipt["state"] == "copied"
    assert receipt["platforms"] == {"linux/arm64": digest}
    assert receipt["referrers"] == [] and receipt["target"]["tag"] is None


def test_bytes_that_do_not_hash_to_the_digest_fail_the_copy(
    source: OciRegistry, target: OciRegistry
) -> None:
    index, _, _ = _built(source)
    source.tamper.add(index)
    plan = CopyPlan.parse(
        f"oci://{source.host}/{REPO}@{index}", f"oci://{target.host}/example/api:1"
    )
    receipt = Copier().copy(plan, CopyGrant(plan.digest, 4e9))
    assert receipt["state"] == "failed"
    assert receipt["reason"] == "mirror-digest-mismatch"
    assert ("example/api", "1") not in target.manifests


def test_the_plan_needs_a_digest_and_its_own_approval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(CopyError, match="digest"):
        CopySource.parse("oci://registry.example/shop/api:1.0")
    with pytest.raises(CopyError):
        CopyPlan.parse(
            "oci://registry.example/shop/api@sha256:" + "1" * 64,
            "oci://ghcr.io/example/api",
            move_tag=True,
        )
    plan = CopyPlan.parse(
        "oci://127.0.0.1:5000/shop/api@sha256:" + "1" * 64,
        "oci://ghcr.io/example/api:1.4.0",
    )
    assert plan.to_dict()["source"].endswith("?tls=false")  # loopback: plain HTTP
    with pytest.raises(CopyError) as raised:
        Copier().copy(plan, CopyGrant("sha256:" + "2" * 64, 4e9))
    assert raised.value.code == "copy-not-approved"
    code, refused = _run(
        "--from", "oci://127.0.0.1:5000/shop/api:latest",
        "--to", "oci://ghcr.io/example/api:1", capsys=capsys,
    )  # fmt: skip
    assert code == 2 and refused["reason"] == "copy-invalid"
