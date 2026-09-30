"""Publishing multi-platform images to a hosted registry (``artifacts publish``)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildReceipt
from piceli.artifacts.delivery_inputs import DeliveryInputError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.image_manifest import OCI_INDEX
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from piceli.artifacts.node_transport import RunResult
from piceli.artifacts.process import ToolPin
from piceli.artifacts.publish import (
    Publisher,
    PublishError,
    PublishGrant,
    PublishPlan,
    referrers,
)
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryTarget,
    StreamedOciRegistryClient,
    docker_config_credentials,
)
from tests.unit.host_build_support import publish_base, write_project
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import PASSWORD, USER, FakeRegistry

GRANT = 600


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def built(tmp_path: Path, registry: FakeRegistry) -> tuple[BuildReceipt, Path]:
    """A two-platform host build (base pulled from ``registry``)."""
    base = publish_base(registry, architectures=("amd64", "arm64"))
    path = write_project(tmp_path / "project", registry.port, base)
    spec = HostBuildSpec.from_toml(path).with_cache_dir(tmp_path / "cache")
    build = MultiPlatformHostBuild(spec, ("linux/amd64", "linux/arm64"))
    out = tmp_path / "out"
    grant = HostBuildGrant(build.plan().plan_hash, time.time() + GRANT)
    return build.run(grant, out, runner=Recorder()), out


def target(registry: FakeRegistry, prefix: str = "clients/acme") -> str:
    return f"oci://127.0.0.1:{registry.port}/{prefix}"


def publish(plan: PublishPlan, publisher: Publisher | None = None) -> dict[str, Any]:
    publisher = publisher or Publisher()
    return publisher.publish(plan, PublishGrant(plan.digest, time.time() + GRANT))


def client(registry: FakeRegistry) -> StreamedOciRegistryClient:
    return StreamedOciRegistryClient(RegistryEndpoint("127.0.0.1", registry.port))


def uploads(registry: FakeRegistry) -> list[tuple[str, str]]:
    return [
        (method, path)
        for method, path in registry.mutations()
        if "/blobs/uploads/" in path
    ]


def test_the_tag_names_an_index_of_both_platforms(
    registry: FakeRegistry, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    preview = plan.preview()
    assert preview["images"][0]["repository"] == "clients/acme/web"
    assert preview["images"][0]["key"] == "web"
    assert [item["platform"] for item in preview["images"][0]["platforms"]] == [
        "linux/amd64",
        "linux/arm64",
    ]
    registry.requests.clear()
    result = publish(plan)
    assert result["state"] == "published", result
    web = result["images"]["web"]
    index = receipt.data["outputs"]["indexes"]["web"]["digest"]
    assert web["digest"] == index
    assert web["repository"] == f"127.0.0.1:{registry.port}/clients/acme/web"
    assert registry.tags[("clients/acme/web", "1.4.0")] == index
    body, media = registry.manifests[("clients/acme/web", index)]
    assert media == OCI_INDEX
    document = json.loads(body)
    assert [
        (item["platform"]["os"], item["platform"]["architecture"])
        for item in document["manifests"]
    ] == [("linux", "amd64"), ("linux", "arm64")]
    for item in document["manifests"]:
        config = json.loads(
            registry.manifests[("clients/acme/web", item["digest"])][0]
        )["config"]["digest"]
        arch = json.loads(registry.blobs[("clients/acme/web", config)])
        assert arch["architecture"] == item["platform"]["architecture"]
    assert set(web["platforms"]) == {"linux/amd64", "linux/arm64"}
    assert web["blobs"]["uploaded"] > 0
    # The Helm values fragment `piceli chart` reads.
    assert result["values"] == {
        "images": {
            "web": {
                "repository": f"127.0.0.1:{registry.port}/clients/acme/web",
                "tag": "1.4.0",
                "digest": index,
            }
        }
    }
    # The tag moves last: after every manifest and attestation.
    puts = [path for method, path in registry.mutations() if method == "PUT"]
    assert puts[-1].endswith("/manifests/1.4.0")


def test_publishing_twice_pushes_nothing_the_second_time(
    registry: FakeRegistry, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    assert publish(plan)["state"] == "published"
    registry.requests.clear()
    again = publish(plan)
    assert again["state"] == "published"
    assert again["images"]["web"]["result"] == "already-present"
    assert again["images"]["web"]["blobs"]["uploaded"] == 0
    assert uploads(registry) == []
    assert registry.mutations() == []


def test_a_version_tag_is_never_moved_silently(
    registry: FakeRegistry, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    other = "sha256:" + "7" * 64
    registry.tags[("clients/acme/web", "1.4.0")] = other
    registry.manifests[("clients/acme/web", other)] = (b"{}", OCI_INDEX)
    registry.requests.clear()
    refused = publish(plan)
    assert refused["state"] == "failed"
    assert refused["reason"] == "publish-tag-exists"
    # Known before any push (a host build's index): nothing was sent.
    assert registry.mutations() == []
    moved = PublishPlan.from_receipt(
        receipt, out, target(registry), "1.4.0", move_tag=True
    )
    assert moved.digest != plan.digest
    result = publish(moved)
    assert result["state"] == "published"
    assert result["images"]["web"]["previous"] == other


@pytest.mark.parametrize("api", [False, True])
def test_the_sbom_and_provenance_are_referrers_of_each_platform(
    registry: FakeRegistry, built: tuple[BuildReceipt, Path], api: bool
) -> None:
    registry.referrers_api = api
    receipt, out = built
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    result = publish(plan)
    assert result["state"] == "published", result
    web = result["images"]["web"]
    kinds = sorted((item["platform"], item["kind"]) for item in web["attestations"])
    assert kinds == [
        ("linux/amd64", "provenance"),
        ("linux/amd64", "sbom"),
        ("linux/arm64", "provenance"),
        ("linux/arm64", "sbom"),
    ]
    reader = client(registry)
    for item in web["attestations"]:
        subject = web["platforms"][item["platform"]]["digest"]
        assert item["subject"] == subject
        found = referrers(reader, "clients/acme/web", subject)
        assert item["digest"] in {entry["digest"] for entry in found}
        assert (item["fallback_tag"] is None) == api
        manifest = json.loads(
            registry.manifests[("clients/acme/web", item["digest"])][0]
        )
        assert manifest["subject"]["digest"] == subject
        blob = registry.blobs[("clients/acme/web", manifest["layers"][0]["digest"])]
        name = f"web/{item['platform'].replace('/', '-')}"
        expected = receipt.images[name][item["kind"]]["sha256"]
        assert "sha256:" + hashlib.sha256(blob).hexdigest() == expected
    tags = {tag for repo, tag in registry.tags if repo == "clients/acme/web"}
    assert any(tag.startswith("sha256-") for tag in tags) != api


def test_the_grant_binds_the_plan_and_changed_inputs_are_refused(
    registry: FakeRegistry, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    other = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.1")
    with pytest.raises(PublishError) as error:
        Publisher().publish(plan, PublishGrant(other.digest, time.time() + 60))
    assert error.value.code == "publish-not-approved"
    sbom = out / receipt.images["web/linux-arm64"]["sbom"]["path"]
    sbom.write_text("{}")
    registry.requests.clear()
    with pytest.raises(PublishError) as error:
        publish(plan)
    assert error.value.code == "publish-input-changed"
    assert registry.requests == []
    # A tag in --to, a bad tag, an unknown image: refused while planning.
    for to, tag, images in (
        (target(registry) + ":1", "1.4.0", None),
        (target(registry), "1.4.0+build", None),
        (target(registry), "1.4.0", ["api"]),
    ):
        with pytest.raises(PublishError):
            PublishPlan.from_receipt(receipt, out, to, tag, images=images)


def test_a_private_registry_with_docker_config_credentials(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt, out = built
    registry.auth = "basic"
    host = f"127.0.0.1:{registry.port}"
    config = tmp_path / "docker" / "config.json"
    config.parent.mkdir()
    auth = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    config.write_text(json.dumps({"auths": {host: {"auth": auth}}}))
    credentials = docker_config_credentials(config, host)
    assert credentials is not None
    plan = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    anonymous = publish(plan)
    assert anonymous["state"] == "failed"
    assert anonymous["reason"] == "registry-unauthorized"
    result = publish(plan, Publisher(credentials=credentials))
    assert result["state"] == "published", result
    text = json.dumps(result) + json.dumps(plan.preview()) + capsys.readouterr().out
    assert PASSWORD not in text and auth not in text
    assert PASSWORD not in repr(credentials)


def test_docker_config_credential_helpers(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    asked: list[tuple[str, str]] = []

    def helper(name: str, registry: str) -> dict[str, Any]:
        asked.append((name, registry))
        return {"Username": "robot", "Secret": "helper-secret"}

    config.write_text(json.dumps({"credHelpers": {"registry.example": "vault"}}))
    found = docker_config_credentials(config, "registry.example", helper=helper)
    assert found == RegistryCredentials("robot", "helper-secret")
    assert asked == [("vault", "registry.example")]
    # credsStore answers every other registry; a token user is a bearer token.
    config.write_text(json.dumps({"credsStore": "desktop"}))
    token = docker_config_credentials(
        config,
        "other.example",
        helper=lambda name, registry: {"Username": "<token>", "Secret": "t0k"},
    )
    assert token is not None and token.scheme == "bearer"
    config.write_text(json.dumps({"auths": {}}))
    assert docker_config_credentials(config, "registry.example") is None
    for bad in ('{"credHelpers": {"registry.example": "../x"}}', "[]", "{"):
        config.write_text(bad)
        with pytest.raises(DeliveryInputError) as error:
            docker_config_credentials(config, "registry.example", helper=helper)
        assert error.value.code == "invalid-docker-config"


# --- a Docker build: per-platform images in the local engine -------------------


def _layer(marker: bytes) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        info = tarfile.TarInfo("file.txt")
        info.size = len(marker)
        archive.addfile(info, io.BytesIO(marker))
    return out.getvalue()


def _docker_save(arch: str) -> tuple[str, bytes]:
    body = _layer(arch.encode())
    config = json.dumps(
        {
            "architecture": arch,
            "os": "linux",
            "rootfs": {
                "type": "layers",
                "diff_ids": ["sha256:" + hashlib.sha256(body).hexdigest()],
            },
        }
    ).encode()
    image_id = "sha256:" + hashlib.sha256(config).hexdigest()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, data in (
            ("layer/layer.tar", body),
            (f"{image_id[7:]}.json", config),
            (
                "manifest.json",
                json.dumps(
                    [{"Config": f"{image_id[7:]}.json", "Layers": ["layer/layer.tar"]}]
                ).encode(),
            ),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return image_id, out.getvalue()


class _Engine:
    """``docker image inspect|save`` of several local images (runner seam)."""

    def __init__(self, images: dict[str, bytes]) -> None:
        self.images = images

    def capture(self, argv, limits, env):  # type: ignore[no-untyped-def]
        if "inspect" in argv:
            return RunResult("succeeded", 0, argv[-1].encode() + b"\n", 0.0)
        return RunResult("succeeded", 0, b"[]", 0.0)

    def read(self, argv, reader, limits, env, cancel=None):  # type: ignore[no-untyped-def]
        return RunResult("succeeded", 0, b"", 0.0), reader(
            io.BytesIO(self.images[argv[-1]])
        )


def test_a_docker_build_is_joined_in_an_index_when_published(
    registry: FakeRegistry, tmp_path: Path
) -> None:
    amd, amd_tar = _docker_save("amd64")
    arm, arm_tar = _docker_save("arm64")
    receipt = BuildReceipt(
        {
            "revision": "piceli.build-receipt.v1",
            "platforms": ["linux/amd64", "linux/arm64"],
            "outputs": {
                "files": {},
                "images": {
                    "api/linux-amd64": {
                        "image_id": amd,
                        "platform": "linux/amd64",
                        "ref": "example/shop/api:1a2b3c-amd64",
                    },
                    "api/linux-arm64": {
                        "image_id": arm,
                        "platform": "linux/arm64",
                        "ref": "example/shop/api:1a2b3c-arm64",
                    },
                },
            },
        }
    )
    plan = PublishPlan.from_receipt(receipt, tmp_path, target(registry), "2.0.0")
    assert plan.images[0].key == "api" and plan.images[0].index_digest is None
    tool = tmp_path / "docker"
    tool.write_bytes(b"#!/bin/sh\n")
    with pytest.raises(PublishError) as error:
        publish(plan)
    assert error.value.code == "docker-tool-required"
    publisher = Publisher(
        docker=ToolPin.capture(tool),
        docker_socket=Path("/var/run/docker.sock"),
        runner=_Engine({amd: amd_tar, arm: arm_tar}),
    )
    result = publish(plan, publisher)
    assert result["state"] == "published", result
    api = result["images"]["api"]
    assert registry.tags[("clients/acme/api", "2.0.0")] == api["digest"]
    index = json.loads(registry.manifests[("clients/acme/api", api["digest"])][0])
    assert [item["platform"]["architecture"] for item in index["manifests"]] == [
        "amd64",
        "arm64",
    ]
    assert api["attestations"] == []
    # Another index already under the tag (not known before the push): refused
    # before the tag or the index is written.
    registry.tags[("clients/acme/api", "2.0.1")] = api["digest"]
    other = PublishPlan.from_receipt(
        receipt, tmp_path, target(registry, "clients/other"), "2.0.0"
    )
    registry.tags[("clients/other/api", "2.0.0")] = "sha256:" + "5" * 64
    registry.manifests[("clients/other/api", "sha256:" + "5" * 64)] = (
        b"{}",
        OCI_INDEX,
    )
    refused = publish(other, publisher)
    assert refused["reason"] == "publish-tag-exists"
    assert ("clients/other/api", api["digest"]) not in registry.manifests


def test_the_plan_needs_no_network(built: tuple[BuildReceipt, Path]) -> None:
    receipt, out = built
    # A registry that does not exist: planning never contacts it.
    plan = PublishPlan.from_receipt(receipt, out, "oci://127.0.0.1:9/x", "1.0.0")
    assert plan.digest.startswith("sha256:")
    assert RegistryTarget.parse("oci://127.0.0.1:9/x").tag is None


# --- the CLI, and its values fragment in `piceli chart manifests` --------------


def test_cli_plans_publishes_and_its_values_feed_the_chart(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import yaml
    from typer.testing import CliRunner

    from piceli.artifacts.cli import main
    from piceli.k8s.cli import app as cli

    receipt, out = built
    path = out / "receipt.json"
    path.write_text(receipt.to_json())
    base = ["publish", "--receipt", str(path), "--to", target(registry)]
    registry.requests.clear()
    assert main([*base, "--tag", "1.4.0"]) == 3
    plan = json.loads(capsys.readouterr().out)
    assert plan["state"] == "approval-required"
    assert registry.requests == []
    assert main([*base, "--tag", "1.4.0", "--approve", "sha256:" + "0" * 64]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "publish-not-approved"
    values = tmp_path / "values-images.yaml"
    published = tmp_path / "publish.json"
    code = main(
        [
            *base,
            "--tag",
            "1.4.0",
            "--approve",
            plan["digest"],
            "--out",
            str(published),
            "--values-out",
            str(values),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0, result
    assert json.loads(published.read_text())["digest"] == plan["digest"]
    fragment = yaml.safe_load(values.read_text())
    assert fragment == result["values"]
    index = result["images"]["web"]["digest"]
    module = tmp_path / "shop_app.py"
    module.write_text(
        "from piceli import App\n\n"
        'app = App("shop")\n'
        'app.deployment("web", image="example/web:dev", ports=[80])\n'
    )
    rendered = CliRunner().invoke(
        cli, ["chart", "manifests", f"{module}:app", "--values", str(values)]
    )
    assert rendered.exit_code == 0, rendered.output
    documents = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    (deployment,) = [item for item in documents if item["kind"] == "Deployment"]
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == f"127.0.0.1:{registry.port}/clients/acme/web:1.4.0@{index}"
