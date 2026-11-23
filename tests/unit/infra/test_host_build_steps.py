"""0.76.0: a host build sends its steps; ``piceli gitops builds`` shows them.

``builder serve`` sends each command's seconds (its build container's
receipt) with ``built``, and with ``build-failed`` when the build ran
commands. The controller keeps the valid ones where an in-cluster build's
go. A builder before 0.76.0 sends none: the build is still recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import (
    build_claim_request,
    build_failed_request,
    built_request,
    request,
)
from piceli.infra import remote_builds
from tests.unit.infra.test_remote_builds import _image_builds, _images, _world

STEPS = [
    {"platform": "any", "kind": "shared-1", "tool": "sh", "state": "succeeded",
     "exit_code": 0, "seconds": 2.5},
    {"platform": "linux/amd64", "kind": "command-1", "tool": "cargo",
     "state": "succeeded", "exit_code": 0, "seconds": 30.0},
    {"platform": "linux/amd64", "kind": "image-api", "state": "succeeded",
     "seconds": 1.25},
]  # fmt: skip
RAN = [STEPS[0], {**STEPS[1], "state": "failed", "exit_code": 101}]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _claimed(world: dict[str, Any]) -> dict[str, Any]:
    controller, channel = world["controller"], world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "laptop"))
    controller.poll_once()
    return wanted


def test_a_host_builds_steps_show_in_gitops_builds(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    wanted = _claimed(world)
    channel.add_request(
        *built_request(
            wanted["id"], "laptop", _images(wanted), {"host": "laptop"}, STEPS
        )
    )
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    (built,) = [item for item in _image_builds(world) if item["stage"] == "published"]
    assert built["host"] == "laptop" and built["steps"] == STEPS


def test_a_host_build_that_failed_keeps_the_steps_that_ran(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    wanted = _claimed(world)
    channel.add_request(
        *build_failed_request(
            wanted["id"], "laptop", "the build exited 1", "error[E0308]", steps=RAN
        )
    )
    main = controller.poll_once()["envs"]["main"]
    assert main["reason"] == "component-build-failed"
    (failed,) = [item for item in _image_builds(world) if item["stage"] == "failed"]
    assert failed["steps"] == RAN


def test_a_builder_before_0_76_0_sends_no_steps_and_the_build_is_recorded(
    world: dict[str, Any],
) -> None:
    controller, channel = world["controller"], world["channel"]
    wanted = _claimed(world)
    key, body = built_request(wanted["id"], "laptop", _images(wanted), {})
    assert "steps" not in body  # as a 0.75.0 builder's answer
    channel.add_request(key, body)
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    (built,) = [item for item in _image_builds(world) if item["stage"] == "published"]
    assert built["host"] == "laptop" and built["steps"] == []


def test_malformed_steps_from_a_host_are_dropped(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    wanted = _claimed(world)
    bad = [
        "not a step",
        {"kind": "command-2", "seconds": "30"},  # seconds as text
        {"kind": "command-3", "seconds": float("inf")},
        {"kind": "command-4", "seconds": -1},
        {"kind": "x" * 65, "seconds": 1},  # too long
        {"kind": "command-5", "tool": 7, "seconds": 1},
        {"kind": "command-6", "exit_code": True, "seconds": 1},
        {"seconds": 1},  # no kind
    ]
    # Sent as is (not through built_request), as a less trusted host might.
    channel.add_request(
        *request(
            "built",
            id=wanted["id"],
            host="laptop",
            images=_images(wanted),
            receipt={},
            steps=[*bad, {**STEPS[1], "extra": "x" * 5000}],
        )
    )
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    (built,) = [item for item in _image_builds(world) if item["stage"] == "published"]
    assert built["steps"] == [STEPS[1]]  # the valid one, without its extra field


def test_host_steps_are_bounded() -> None:
    many = [{"kind": f"command-{i}", "seconds": 1} for i in range(200)]
    found = remote_builds.host_steps(many)
    assert len(found) == remote_builds.MAX_STEPS == 64
    assert found[0] == {"kind": "command-0", "seconds": 1.0}
    assert remote_builds.host_steps({"kind": "command-1"}) == []
    assert remote_builds.host_steps(None) == []


#: 0.78.0: steps with their start offsets; the shared command overlapped
#: the platform's command.
TIMED = [
    {**STEPS[0], "seconds": 40.0, "start_seconds": 0.5},
    {**STEPS[1], "start_seconds": 0.75},
    {**STEPS[2], "start_seconds": 40.5},
]


def test_step_start_offsets_reach_gitops_builds(world: dict[str, Any]) -> None:
    controller, channel = world["controller"], world["channel"]
    wanted = _claimed(world)
    channel.add_request(
        *built_request(
            wanted["id"], "laptop", _images(wanted), {"host": "laptop"}, TIMED
        )
    )
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
    (built,) = [item for item in _image_builds(world) if item["stage"] == "published"]
    assert built["steps"] == TIMED


def test_host_steps_keep_only_a_valid_start_offset() -> None:
    steps = [
        {"kind": f"command-{i}", "seconds": 1, "start_seconds": start}
        for i, start in enumerate([-1, float("inf"), True, "2", 2])
    ]
    found = remote_builds.host_steps(steps)
    assert [item.get("start_seconds") for item in found] == [None] * 4 + [2.0]


def test_gitops_builds_shows_the_wall_time_of_overlapping_commands() -> None:
    from piceli.k8s.cli.gitops import _commands_line

    assert _commands_line(TIMED) == "; 2 command(s) in 40s (70s of work)"
    # Without offsets (a builder before 0.78.0), or one after the other: the sum.
    assert _commands_line(STEPS) == "; 2 command(s) in 32s"
    sequential = [
        {**STEPS[0], "start_seconds": 0.0},
        {**STEPS[1], "start_seconds": 2.5},
    ]
    assert _commands_line(sequential) == "; 2 command(s) in 32s"


# ------------------------------------------------------------ builder serve


def _worker(world: dict[str, Any], tmp_path: Path, receipt: dict[str, Any], code: int):
    from piceli.artifacts.cluster_build import encode_receipt
    from piceli.infra.builder_serve import BuilderServe

    def container(
        argv: list[str], sources: dict[str, Path], out: Path, line: Any
    ) -> int:
        archives = []
        for spec in (a for a in argv if a.startswith("{")):
            item = json.loads(spec)
            for platform in ("linux/amd64", "linux/arm64"):
                archives.append(
                    {
                        "archive": f"{platform.replace('/', '-')}/{item['component']}.oci.tar",
                        "platform": platform,
                        "repository": item["repository"],
                        "image_id": "sha256:" + "1" * 64,
                        "digest": "sha256:" + ("a" if "amd" in platform else "b") * 64,
                    }
                )
        line("[build] compiling")
        line(encode_receipt({"archives": archives, **receipt}))
        return code

    def push(image_id: str, repository: str, archive: Path) -> str:
        digest = "sha256:" + ("a" if "amd64" in str(archive) else "b") * 64
        return f"registry.example:5000/{repository}@{digest}"

    return BuilderServe(
        host="laptop",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=world["channel"],
        container=container,
        push=push,
        index=lambda repository, digests: "sha256:" + "9" * 64,
        root=tmp_path / "builder",
        sleep=lambda _s: world["controller"].poll_once(),
    )


def _answer(world: dict[str, Any], kind: str) -> dict[str, Any]:
    (body,) = [b for b in world["channel"].requests().values() if b["kind"] == kind]
    return body


def test_builder_serve_sends_a_passed_builds_steps(
    world: dict[str, Any], tmp_path: Path
) -> None:
    world["controller"].poll_once()
    worker = _worker(world, tmp_path, {"steps": STEPS}, 0)
    assert worker.once() is not None
    assert _answer(world, "built")["steps"] == STEPS
    world["controller"].poll_once()
    (built,) = [item for item in _image_builds(world) if item["stage"] == "published"]
    assert built["steps"] == STEPS


def test_builder_serve_sends_the_steps_a_failed_build_ran(
    world: dict[str, Any], tmp_path: Path
) -> None:
    world["controller"].poll_once()
    worker = _worker(world, tmp_path, {"state": "failed", "steps": RAN}, 1)
    assert worker.once() is not None
    answer = _answer(world, "build-failed")
    assert answer["steps"] == RAN and not answer.get("early")
    world["controller"].poll_once()
    (failed,) = [item for item in _image_builds(world) if item["stage"] == "failed"]
    assert failed["steps"] == RAN
