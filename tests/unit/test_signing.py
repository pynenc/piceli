"""Signing published images with cosign (fake cosign here; real one in integration)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildReceipt
from piceli.artifacts.process import ToolPin
from piceli.artifacts.publish import Publisher, PublishError, PublishGrant, PublishPlan
from piceli.artifacts.registry import RegistryCredentials
from piceli.artifacts.signing import CosignSigner, verify_argv
from tests.unit.test_publish import built, registry, target
from tests.unit.test_registry_delivery import FakeRegistry

__all__ = ["built", "registry"]


class FakeCosign:
    """Records each call and what the registry held at that moment."""

    def __init__(self, registry: FakeRegistry, exit_code: int = 0) -> None:
        self.registry = registry
        self.exit_code = exit_code
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: Sequence[str], environment: Mapping[str, str], timeout: float
    ) -> int:
        config = Path(environment["DOCKER_CONFIG"]) / "config.json"
        self.calls.append(
            {
                "argv": list(argv),
                "env": dict(environment),
                "docker_config": json.loads(config.read_text()),
                "docker_config_mode": config.stat().st_mode & 0o777,
                "tags": dict(self.registry.tags),
            }
        )
        return self.exit_code


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "cosign.key"
    path.write_text("-----BEGIN ENCRYPTED SIGSTORE PRIVATE KEY-----\nx\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def cosign(tmp_path: Path) -> ToolPin:
    tool = tmp_path / "cosign"
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return ToolPin.capture(tool)


def signed(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    signer: CosignSigner,
) -> tuple[PublishPlan, dict[str, Any]]:
    receipt, out = built
    plan = PublishPlan.from_receipt(
        receipt, out, target(registry), "1.4.0", signing=signer.public()
    )
    publisher = Publisher(credentials=signer.credentials, signer=signer)
    return plan, publisher.publish(plan, PublishGrant(plan.digest, time.time() + 600))


def test_every_published_digest_is_signed_before_the_tag_moves(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    key: Path,
    cosign: ToolPin,
) -> None:
    fake = FakeCosign(registry)
    signer = CosignSigner(
        cosign, key, RegistryCredentials("robot", "s3cret-value"), runner=fake
    )
    plan, result = signed(registry, built, signer)
    assert result["state"] == "published", result
    web = result["images"]["web"]
    expected = [
        web["digest"],
        *(item["digest"] for item in web["platforms"].values()),
        *(item["digest"] for item in web["attestations"]),
    ]
    references = [call["argv"][-1] for call in fake.calls]
    repository = f"127.0.0.1:{registry.port}/clients/acme/web"
    assert references == [f"{repository}@{value}" for value in expected]
    assert [item["digest"] for item in web["signatures"]] == expected
    for call in fake.calls:
        # Signed while the version tag did not exist yet.
        assert ("clients/acme/web", "1.4.0") not in call["tags"]
        argv = call["argv"]
        assert argv[1:3] == ["sign", "--key"] and "--allow-http-registry" in argv
        assert "--signing-config" in argv and "s3cret-value" not in " ".join(argv)
        auth = call["docker_config"]["auths"][f"127.0.0.1:{registry.port}"]
        assert auth["auth"]  # credentials reach cosign through a private file
        assert call["docker_config_mode"] == 0o600
        assert call["env"]["HOME"] != os.environ.get("HOME")
        assert not Path(call["env"]["HOME"]).exists()  # removed after signing
    assert plan.to_dict()["signing"] == {
        "tool": "cosign",
        "tool_sha256": cosign.sha256,
        "key_sha256": signer.key_sha256,
        "transparency_log": False,
    }
    assert "s3cret-value" not in json.dumps(result)


def test_a_signing_failure_leaves_the_tag_alone(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    key: Path,
    cosign: ToolPin,
) -> None:
    signer = CosignSigner(cosign, key, runner=FakeCosign(registry, exit_code=1))
    _plan, result = signed(registry, built, signer)
    assert result["state"] == "failed" and result["reason"] == "sign-failed"
    assert ("clients/acme/web", "1.4.0") not in registry.tags


def test_the_plan_binds_the_key_and_the_key_must_be_private(
    registry: FakeRegistry,
    built: tuple[BuildReceipt, Path],
    key: Path,
    cosign: ToolPin,
    tmp_path: Path,
) -> None:
    receipt, out = built
    signer = CosignSigner(cosign, key, runner=FakeCosign(registry))
    unsigned = PublishPlan.from_receipt(receipt, out, target(registry), "1.4.0")
    with pytest.raises(PublishError) as error:
        Publisher(signer=signer).publish(
            unsigned, PublishGrant(unsigned.digest, time.time() + 60)
        )
    assert error.value.code == "publish-not-approved"
    other = tmp_path / "other.key"
    other.write_text("another key\n")
    other.chmod(0o600)
    assert (
        CosignSigner(cosign, other).public()["key_sha256"]
        != signer.public()["key_sha256"]
    )
    other.chmod(0o644)
    with pytest.raises(PublishError) as error:
        CosignSigner(cosign, other)
    assert error.value.code == "sign-key-invalid"
    # A key replaced after the plan is refused before cosign runs.
    key.write_text("replaced\n")
    fake = FakeCosign(registry)
    signer.runner = fake
    plan = PublishPlan.from_receipt(
        receipt, out, target(registry), "1.4.0", signing=signer.public()
    )
    result = Publisher(signer=signer).publish(
        plan, PublishGrant(plan.digest, time.time() + 60)
    )
    assert result["reason"] == "sign-key-invalid" and fake.calls == []


def test_the_verification_command() -> None:
    argv = verify_argv(
        Path("/bin/cosign"), Path("/k/cosign.pub"), "r.example/a@sha256:x"
    )
    assert argv == [
        "/bin/cosign",
        "verify",
        "--key",
        "/k/cosign.pub",
        "--insecure-ignore-tlog=true",
        "r.example/a@sha256:x",
    ]
