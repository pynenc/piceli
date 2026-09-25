"""Build smoke checks: environment, entrypoint override and output match.

Reproduces two images that could not self-test with an exit code alone: a
shell-entrypoint image that needs an environment variable, and a server whose
useful check is the version it prints. Every test uses the fake docker runner
from ``test_build_spec``; set ``PICELI_DOCKER_TESTS=1`` to also run the smoke
checks against a real Docker engine.
"""

import json
import os
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import (
    SMOKE_CAPTURE_BYTES,
    BuildGrant,
    BuildSpec,
    BuildSpecError,
    DockerTool,
    run_build_spec_command,
)
from piceli.artifacts.process import ToolPin
from piceli.errors import ERRORS
from tests.unit.test_build_spec import (
    BUILDER,
    FakeDocker,
    grant,
    image_doc,
    parse,
    project,
    write_spec,
)

SHELL_SELF_TEST = {
    "entrypoint": ["/bin/sh", "-c"],
    "command": ['test "$APP_MODE" = self-test && echo "self-test ok"'],
    "env": {"APP_MODE": "self-test", "LANG": "C.UTF-8"},
    "expect_stdout": "^self-test ok$",
}
SERVER_VERSION = {"command": ["--version"], "expect_stdout": r"api \d+\.\d+\.\d+"}
ISOLATION = [
    "--rm",
    "--pull",
    "never",
    "--network",
    "none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,noexec,size=67108864",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--pids-limit",
    "256",
    "--memory",
    "512m",
]


@pytest.fixture
def docker_tool(tmp_path: Path) -> DockerTool:
    binary = tmp_path / "bin" / "docker"
    binary.parent.mkdir()
    binary.write_bytes(b"#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    return DockerTool(ToolPin.capture(binary), {"PATH": "/usr/bin", "HOME": "/h"})


def smoke_spec(tmp_path: Path, smoke: dict[str, Any]) -> BuildSpec:
    project(tmp_path)
    return BuildSpec.from_dict(image_doc(smoke=smoke), tmp_path)


def smoke_run(fake: FakeDocker) -> list[str]:
    [run] = [argv for argv in fake.calls if argv[1] == "run"]
    return run


def assert_isolated(run: list[str]) -> None:
    for flag in ISOLATION:
        assert flag in run
    image = next(index for index, item in enumerate(run) if item.startswith("sha256:"))
    # Every docker option precedes the image; nothing after it is parsed by docker.
    assert all(not item.startswith("--") for item in run[image + 1 :] if item)


# --- reproduction: images that need env / output to self-test -------------------


def test_shell_entrypoint_image_self_tests_with_env(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(tmp_path, SHELL_SELF_TEST)
    fake = FakeDocker()
    fake.smoke_stdout = b"self-test ok\n"
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    run = smoke_run(fake)
    assert_isolated(run)
    image_id = receipt.images["svc"]["image_id"]
    image = run.index(image_id)
    assert "--env=APP_MODE=self-test" in run[:image]
    assert "--env=LANG=C.UTF-8" in run[:image]
    assert "--entrypoint=/bin/sh" in run[:image]
    assert run[image + 1 :] == ["-c", SHELL_SELF_TEST["command"][0]]
    body = receipt.to_dict()
    assert body["steps"][-1]["state"] == "succeeded"
    assert body["smoke"]["svc"]["env"] == SHELL_SELF_TEST["env"]
    assert body["smoke"]["svc"]["entrypoint"] == ["/bin/sh", "-c"]
    assert "self-test ok\n" not in json.dumps(body)


def test_server_version_matches_stdout(tmp_path: Path, docker_tool: DockerTool) -> None:
    spec = smoke_spec(tmp_path, SERVER_VERSION)
    fake = FakeDocker()
    fake.smoke_stdout = b"starting\napi 1.4.2 (linux/arm64)\n"
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert receipt.to_dict()["steps"][-1]["state"] == "succeeded"
    assert receipt.to_dict()["smoke"]["svc"]["expect_stdout"] == r"api \d+\.\d+\.\d+"


def test_unmatched_stdout_fails_with_a_code_and_a_bounded_excerpt(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(tmp_path, SERVER_VERSION)
    fake = FakeDocker()
    fake.smoke_stdout = b"api dev-build\x1b[0m\n" + b"x" * 5000
    progress: list[str] = []
    log = tmp_path / "build.log"
    with pytest.raises(BuildSpecError) as error:
        spec.run(
            grant(),
            tmp_path / "o",
            docker=docker_tool,
            runner=fake,
            progress=progress.append,
            log=log,
        )
    assert error.value.code == "smoke-output-mismatch"
    assert "dev-build" not in str(error.value)
    step = error.value.steps[-1]
    assert step["kind"] == "smoke:svc" and step["state"] == "failed"
    assert step["unmatched"] == ["stdout"]
    assert "dev-build" not in json.dumps(step)
    [line] = [text for text in progress if "did not match" in text]
    assert "api dev-build" in line and "\x1b" not in line
    assert len(line) < 1200 and f"{len(fake.smoke_stdout)} bytes" in line
    assert b"did not match" in log.read_bytes()


def test_output_mismatch_is_a_failed_run_on_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    path = write_spec(tmp_path, image_doc(smoke=SERVER_VERSION))
    fake = FakeDocker()
    fake.smoke_stdout = b"api dev-build\n"
    out = tmp_path / "r.json"
    code = run_build_spec_command(
        parse(
            "run", "--spec", str(path), "--approve-builder", BUILDER, "--out", str(out)
        ),
        runner=fake,
        docker=docker_tool,
    )
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert code == 1 and not out.exists()
    assert body["state"] == "failed" and body["reason"] == "smoke-output-mismatch"
    assert body["steps"][-1]["unmatched"] == ["stdout"]
    assert "dev-build" not in captured.out
    # The human excerpt goes to stderr only.
    assert "dev-build" in captured.err


def test_expect_stderr_uses_search_semantics(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(
        tmp_path, {"command": [], "expect_stderr": "ready", "expect_stdout": r"\A\Z"}
    )
    fake = FakeDocker()
    fake.smoke_stdout = b""
    fake.smoke_stderr = b"loading\nWARN server ready on :8080\n"
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert receipt.to_dict()["steps"][-1]["state"] == "succeeded"
    fake = FakeDocker()
    fake.smoke_stdout = b"unexpected\n"
    fake.smoke_stderr = b"no\n"
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "p", docker=docker_tool, runner=fake)
    assert error.value.code == "smoke-output-mismatch"
    assert error.value.steps[-1]["unmatched"] == ["stdout", "stderr"]


def test_output_is_matched_within_a_bounded_capture(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(tmp_path, {"command": [], "expect_stdout": "MARKER"})
    fake = FakeDocker()
    fake.smoke_stdout = b"x" * SMOKE_CAPTURE_BYTES + b"MARKER\n"
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert error.value.code == "smoke-output-mismatch"


def test_wrong_exit_code_wins_over_output(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(tmp_path, SERVER_VERSION)
    fake = FakeDocker()
    fake.smoke_exit = 2
    fake.smoke_stdout = b"nope\n"
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert error.value.code == "smoke-failed"
    assert "unmatched" not in error.value.steps[-1]


# --- compatibility: existing smoke specs are unchanged --------------------------


def test_existing_smoke_argv_and_declaration_are_unchanged(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = smoke_spec(tmp_path, {"command": ["--self-test"]})
    assert spec.images[0].smoke is not None
    assert spec.images[0].smoke.to_dict() == {
        "command": ["--self-test"],
        "expect_exit": 0,
        "timeout_seconds": 60.0,
    }
    fake = FakeDocker()
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    image_id = receipt.images["svc"]["image_id"]
    name = smoke_run(fake)[4]
    assert smoke_run(fake) == [
        str(docker_tool.tool.path),
        "run",
        "--rm",
        "--name",
        name,
        "--pull",
        "never",
        "--platform",
        "linux/arm64",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=67108864",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "512m",
        image_id,
        "--self-test",
    ]
    assert receipt.to_dict()["steps"][-1] == {
        "platform": "linux/arm64",
        "kind": "smoke:svc",
        "state": "succeeded",
        "exit_code": 0,
        "seconds": 0.01,
    }


def test_example_spec_digest_is_stable() -> None:
    # The shipped example (exit-code smoke only) keeps its declaration digest.
    from tests.unit.test_build_spec import EXAMPLE

    spec = BuildSpec.from_toml(EXAMPLE / "build.toml")
    smoke = spec.to_dict()["images"][0]["smoke"]
    assert sorted(smoke) == ["command", "expect_exit", "timeout_seconds"]


# --- plan: smoke is part of what gets approved ---------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"env": {"APP_MODE": "other"}},
        {"entrypoint": ["/bin/bash", "-c"]},
        {"expect_stdout": "^ok$"},
        {"expect_stderr": "warn"},
        {"timeout_seconds": 30},
    ],
)
def test_changing_smoke_changes_the_plan(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    base = smoke_spec(tmp_path, SHELL_SELF_TEST)
    other = smoke_spec(tmp_path, {**SHELL_SELF_TEST, **change})
    assert base.spec_sha256 != other.spec_sha256
    assert base.plan().plan_hash != other.plan().plan_hash


def test_preview_lists_the_smoke_declaration(tmp_path: Path) -> None:
    preview = smoke_spec(tmp_path, SHELL_SELF_TEST).plan().preview()
    assert preview["outputs"]["smoke"]["svc"] == {
        "command": SHELL_SELF_TEST["command"],
        "entrypoint": ["/bin/sh", "-c"],
        "env": SHELL_SELF_TEST["env"],
        "expect_exit": 0,
        "expect_stdout": "^self-test ok$",
        "timeout_seconds": 60.0,
    }


# --- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "smoke",
    [
        {"command": [], "env": ["A=1"]},
        {"command": [], "env": {"1BAD": "x"}},
        {"command": [], "env": {"A": "x$(id)"}},
        {"command": [], "env": {"A": 1}},
        {"command": [], "env": {"A": "line\nbreak"}},
        {"command": [], "env": {f"K{index}": "v" for index in range(65)}},
        {"command": [], "entrypoint": "/bin/sh"},
        {"command": [], "entrypoint": []},
        {"command": [], "expect_stdout": "("},
        {"command": [], "expect_stdout": ""},
        {"command": [], "expect_stdout": 1},
        {"command": [], "expect_stderr": "a" * 1025},
        {"command": [], "expect_output": "x"},
    ],
)
def test_invalid_smoke_extensions_rejected(
    tmp_path: Path, smoke: dict[str, Any]
) -> None:
    with pytest.raises(BuildSpecError) as error:
        smoke_spec(tmp_path, smoke)
    assert error.value.code == "invalid-spec"


@pytest.mark.parametrize(
    "value",
    ["secret://api/token", "vault://kv/app#key", "op://vault/item/field", "secret:db"],
)
def test_secret_references_in_smoke_env_are_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(BuildSpecError) as error:
        smoke_spec(tmp_path, {"command": [], "env": {"TOKEN": value}})
    assert error.value.code == "smoke-env-secret"
    assert value not in str(error.value)


def test_new_codes_are_registered() -> None:
    for code in ("smoke-output-mismatch", "smoke-env-secret"):
        assert code in ERRORS


# --- Python pipeline API ----------------------------------------------------------


def dockerfile_build(tmp_path: Path, **kwargs: Any) -> Any:
    from piceli.pipeline import Build

    (tmp_path / "Containerfile").write_text(
        "ARG BUILDER\nFROM ${BUILDER} AS api\nCOPY main.txt /main.txt\n"
    )
    (tmp_path / "main.txt").write_text("hello\n")
    return Build(
        document=Build.dockerfile(
            "Containerfile",
            builder=f"docker.io/library/busybox@{BUILDER}",
            targets=["api"],
            include=["Containerfile", "main.txt"],
            platform="linux/arm64",
            **kwargs,
        ).document,
        base=tmp_path,
        images=["api"],
    )


def test_pipeline_build_declares_smoke(tmp_path: Path) -> None:
    from piceli.pipeline import Smoke

    plain = dockerfile_build(tmp_path).load()
    smoked = dockerfile_build(
        tmp_path,
        smoke={
            "api": Smoke(
                ["--version"],
                env={"APP_MODE": "self-test"},
                entrypoint=["/usr/local/bin/api"],
                expect_stdout=r"api \d+",
                timeout_seconds=20,
            )
        },
    ).load()
    check = smoked.images[0].smoke
    assert check is not None
    assert check.env == (("APP_MODE", "self-test"),)
    assert check.entrypoint == ("/usr/local/bin/api",)
    assert check.expect_stdout == r"api \d+"
    assert check.timeout_seconds == 20.0
    assert plain.images[0].smoke is None
    assert plain.plan().plan_hash != smoked.plan().plan_hash
    # A mapping with the TOML keys works too.
    table = dockerfile_build(
        tmp_path, smoke={"api": {"command": ["--version"], "expect_exit": 0}}
    ).load()
    assert table.images[0].smoke is not None


def test_pipeline_smoke_is_validated_when_declared(tmp_path: Path) -> None:
    from piceli.pipeline import PipelineError, Smoke

    with pytest.raises(PipelineError) as error:
        Smoke(["--version"], expect_stdout="(")
    assert error.value.code == "invalid-spec"
    with pytest.raises(PipelineError) as error:
        Smoke([], env={"TOKEN": "secret://api/token"})
    assert error.value.code == "smoke-env-secret"
    with pytest.raises(PipelineError) as error:
        dockerfile_build(tmp_path, smoke={"worker": Smoke(["--version"])})
    assert error.value.code == "pipeline-invalid"


# --- opt-in real docker ------------------------------------------------------------

ALPINE = (
    "docker.io/library/alpine",
    "sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc",
)
CONTAINERFILE = textwrap.dedent(
    """\
    ARG BUILDER
    FROM ${BUILDER} AS api
    COPY api.sh /usr/local/bin/api
    ENTRYPOINT ["/usr/local/bin/api"]
    CMD ["serve"]
    """
)
API_SCRIPT = textwrap.dedent(
    """\
    #!/bin/sh
    case "$1" in
      --version) echo "api 1.4.2"; echo "config: ${APP_MODE:-unset}" >&2 ;;
      serve) while true; do sleep 1; done ;;
      *) exit 64 ;;
    esac
    """
)


def docker_spec(tmp_path: Path, smoke: dict[str, Any]) -> BuildSpec:
    (tmp_path / "Containerfile").write_text(CONTAINERFILE)
    script = tmp_path / "api.sh"
    script.write_text(API_SCRIPT)
    script.chmod(0o755)
    document = {
        "revision": "piceli.build-spec.v1",
        "name": "smoke-e2e",
        "builder": {"image": ALPINE[0], "digest": ALPINE[1]},
        "build": {
            "platforms": ["linux/arm64"],
            "dockerfile": "Containerfile",
            "context": "src",
            "builder_arg": "BUILDER",
        },
        "context": {"src": {"include": ["Containerfile", "api.sh"]}},
        "output": {
            "image": [
                {
                    "name": "api",
                    "repository": "piceli-e2e/smoke-api",
                    "tag": "{image_id:12}",
                    "target": "api",
                    "smoke": smoke,
                }
            ]
        },
    }
    return BuildSpec.from_dict(document, tmp_path)


@pytest.mark.skipif(
    os.environ.get("PICELI_DOCKER_TESTS") != "1",
    reason="set PICELI_DOCKER_TESTS=1 to run real docker buildx builds",
)
@pytest.mark.timeout(600)
def test_real_docker_smoke_env_entrypoint_and_output(tmp_path: Path) -> None:
    def build(smoke: dict[str, Any], out: str) -> dict[str, Any]:
        spec = docker_spec(tmp_path, smoke)
        return spec.run(
            BuildGrant(ALPINE[1], time.time() + 600), tmp_path / out
        ).to_dict()

    # A server printing its version (the default CMD would serve forever).
    receipt = build(
        {
            "command": ["--version"],
            "env": {"APP_MODE": "self-test"},
            "expect_stdout": r"^api \d+\.\d+\.\d+$",
            "expect_stderr": "config: self-test",
            "timeout_seconds": 60,
        },
        "a",
    )
    assert receipt["steps"][-1]["state"] == "succeeded"
    # A shell entrypoint whose check needs the environment.
    receipt = build(
        {
            "entrypoint": ["/bin/sh", "-c"],
            "command": ['test "$APP_MODE" = self-test && echo ok'],
            "env": {"APP_MODE": "self-test"},
            "expect_stdout": "^ok$",
        },
        "b",
    )
    assert receipt["steps"][-1]["state"] == "succeeded"
    # Isolation holds with an entrypoint override: no network, read-only root.
    receipt = build(
        {
            "entrypoint": ["/bin/sh", "-c"],
            "command": [
                "touch /probe 2>/dev/null && echo writable; "
                "cat /sys/class/net/*/operstate 2>/dev/null | grep -v unknown "
                "| grep -q up && echo online; echo done"
            ],
            "expect_stdout": r"\Adone\n\Z",
        },
        "c",
    )
    assert receipt["steps"][-1]["state"] == "succeeded"
    with pytest.raises(BuildSpecError) as error:
        build({"command": ["--version"], "expect_stdout": "api 9"}, "d")
    assert error.value.code == "smoke-output-mismatch"
