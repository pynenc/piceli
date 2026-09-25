"""Every command marked ``conforms`` refuses with the documented contract.

For each command, one rejection path runs without a cluster and checks the
machine contract (``docs/agents.md``):

- stdout is exactly one JSON object: ``{"state": "rejected", "reason": <code>,
  "message": <text>, …}``, and ``reason`` is a registered error code;
- the exit status is ``2``;
- stderr is human text only (no JSON object) and names the code.

A command whose contract is ``conforms`` in :data:`piceli.cli_contract.COMMANDS`
must have a case here, so a new command cannot silently skip the contract.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.artifacts import cli as artifacts_cli
from piceli.cli_contract import COMMANDS
from piceli.errors import ERRORS
from piceli.k8s.cli import app

DIGEST = "sha256:" + "a" * 64
Argv = Callable[[Path], list[str]]


@pytest.fixture
def files(tmp_path: Path) -> Path:
    """Inputs that are present but invalid; nothing here reaches a cluster."""
    (tmp_path / "junk").write_text("not { valid ] in any format\n")
    (tmp_path / "bad-release.toml").write_text("surprise = 1\n")
    (tmp_path / "inputs.toml").write_text('[[source]]\nname = "app"\npath = "app"\n')
    (tmp_path / "kubeconfig").write_text("{}\n")
    (tmp_path / "profile.toml").write_text("")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "approvals.json").write_text("[]\n")
    return tmp_path


def _release(command: str) -> Argv:
    extra = {
        "rollback": ["previous"],
        "secret show": ["password"],
    }.get(command, [])
    return lambda p: [
        "release",
        *command.split(),
        *extra,
        "--spec",
        str(p / "bad-release.toml"),
    ]


_OBSERVE_FORWARD = ["--user", "me", "--name", "db"]

CASES: dict[str, tuple[Argv, str]] = {
    **{
        f"release {command}": (_release(command), "invalid-release-spec")
        for command in (
            "plan",
            "preview",
            "apply",
            "rollback",
            "resume",
            "stop",
            "status",
            "secret show",
            "diff",
            "check",
        )
    },
    "render": (
        lambda p: ["render", str(p / "missing.py") + ":app"],
        "render-target-invalid",
    ),
    **{
        f"state {command}": (
            lambda p, command=command, extra=extra: [
                "state",
                command,
                "--spec",
                str(p / "bad-release.toml"),
                *[str(p / item) if item.endswith(".json") else item for item in extra],
            ],
            "invalid-release-spec",
        )
        for command, extra in (
            ("show", []),
            ("pull", []),
            ("export", ["--out", "export.json"]),
            ("import", ["--in", "export.json"]),
        )
    },
    "status": (
        lambda p: ["status", str(p / "missing-release.toml")],
        "access-target-invalid",
    ),
    "access": (
        lambda p: ["access", str(p / "missing-release.toml")],
        "access-target-invalid",
    ),
    "access stop": (
        lambda p: ["access", "stop", "--stale", str(p / "missing-release.toml")],
        "access-target-invalid",
    ),
    "import yaml": (
        lambda p: ["import", "yaml", str(p / "no-such-dir")],
        "import-manifests-invalid",
    ),
    "import live": (
        lambda p: (
            ["import", "live", "--kubeconfig", str(p / "missing-kubeconfig")]
            + ["--context", "test", "--namespace", "shop"]
        ),
        "import-target-invalid",
    ),
    "deploy": (
        lambda p: ["deploy", str(p / "missing.py") + ":pipeline"],
        "pipeline-not-found",
    ),
    "inputs record": (
        lambda p: ["inputs", "record", "--spec", str(p / "junk")],
        "invalid-inputs-spec",
    ),
    "inputs verify": (
        lambda p: (
            ["inputs", "verify", "--spec", str(p / "inputs.toml")]
            + ["--lock", str(p / "junk")]
        ),
        "inputs-lock-invalid",
    ),
    "artifacts preview": (
        lambda p: ["artifacts", "preview", "--plan", str(p / "junk")],
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts build": (
        lambda p: (
            ["artifacts", "build", "--plan", str(p / "junk")]
            + ["--source-root", str(p), "--output", str(p / "layout")]
        ),
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts pin": (
        lambda p: (
            ["artifacts", "pin", "--source-root", str(p / "missing")]
            + ["--public-file", "a.txt"]
        ),
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts inspect": (
        lambda p: ["artifacts", "inspect", "--layout", str(p / "missing")],
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts import-local": (
        lambda p: (
            ["artifacts", "import-local", "--layout", str(p / "missing")]
            + ["--docker", str(p / "missing-docker"), "--docker-sha256", DIGEST]
            + ["--socket", str(p / "sock"), "--approve-digest", DIGEST]
        ),
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts preview-command": (
        lambda p: ["artifacts", "preview-command", "--spec", str(p / "junk")],
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts execute-command": (
        lambda p: (
            ["artifacts", "execute-command", "--spec", str(p / "junk")]
            + ["--source-root", str(p), "--approve-plan", "0" * 64]
        ),
        "invalid-or-unavailable-artifact-input",
    ),
    "artifacts deliver": (
        lambda p: (
            ["artifacts", "deliver", "--archive", str(p / "junk")]
            + ["--to", "docker://node", "--approve-digest", "sha256:short"]
        ),
        "invalid-approved-digest",
    ),
    "artifacts build-spec preview": (
        lambda p: ["artifacts", "build-spec", "preview", "--spec", str(p / "no.toml")],
        "spec-unreadable",
    ),
    "artifacts build-spec run": (
        lambda p: (
            ["artifacts", "build-spec", "run", "--spec", str(p / "no.toml")]
            + ["--approve-builder", DIGEST, "--out", str(p / "receipt.json")]
        ),
        "spec-unreadable",
    ),
    "observe status": (
        lambda p: (
            ["observe", "status", "--archive", str(p / "junk")]
            + ["--kubeconfig", str(p / "kubeconfig"), "--context", "test"]
        ),
        "invalid-session-archive",
    ),
    "observe forward-save": (
        lambda p: (
            ["observe", "forward-save", *_OBSERVE_FORWARD]
            + ["--namespace", "default", "--target", "service/db"]
            + ["--local-port", "0", "--remote-port", "5432"]
            + ["--preferences", str(p / "prefs.json")]
        ),
        "invalid-forward-preference",
    ),
    "observe forward-list": (
        lambda p: (
            ["observe", "forward-list", "--user", "me"]
            + ["--preferences", str(p / "junk")]
        ),
        "invalid-preference-store",
    ),
    "observe forward-command": (
        lambda p: (
            ["observe", "forward-command", *_OBSERVE_FORWARD]
            + ["--kubeconfig", str(p / "kubeconfig"), "--context", "test"]
            + ["--preferences", str(p / "prefs.json")]
        ),
        "no-saved-forwards",
    ),
    "observe forward-run": (
        lambda p: (
            ["observe", "forward-run", *_OBSERVE_FORWARD]
            + ["--kubeconfig", str(p / "kubeconfig"), "--context", "test"]
            + ["--preferences", str(p / "prefs.json")]
        ),
        "target-refused",
    ),
    "observe logs-command": (
        lambda p: (
            ["observe", "logs-command", "--namespace", "Not_A_Name"]
            + ["--target", "pod/web", "--kubeconfig", str(p / "kubeconfig")]
            + ["--context", "test"]
        ),
        "invalid-log-request",
    ),
    "observe logs-run": (
        lambda p: (
            ["observe", "logs-run", "--namespace", "Not_A_Name"]
            + ["--target", "pod/web", "--kubeconfig", str(p / "kubeconfig")]
            + ["--context", "test"]
        ),
        "target-refused",
    ),
    "observe serve": (
        lambda p: (
            ["observe", "serve", "--archive", str(p / "junk")]
            + ["--kubeconfig", str(p / "kubeconfig"), "--context", "test"]
            + ["--ui-config", str(p / "junk")]
        ),
        "invalid-access-profile",
    ),
    "observe forwards apply": (
        lambda p: (
            ["observe", "forwards", "apply", "--profile", str(p / "junk")]
            + ["--kubeconfig", str(p / "kubeconfig"), "--context", "test"]
        ),
        "target-refused",
    ),
    "observe forwards status": (
        lambda p: (
            ["observe", "forwards", "status"]
            + ["--profile", str(p / "profile.toml"), "--only", "nope"]
        ),
        "unknown-shortcut",
    ),
    "operator status": (
        lambda p: [
            "operator",
            "status",
            "--kubeconfig",
            str(p / "kubeconfig"),
            "--context",
            "test",
        ],
        "target-refused",
    ),
    "operator promote": (
        lambda p: (
            ["operator", "promote", "--catalog", str(p / "junk")]
            + ["--source", "a", "--target", "b"]
        ),
        "invalid-release-catalog",
    ),
    "operator approve": (
        lambda p: (
            ["operator", "approve", "--state-dir", str(p / "state")]
            + ["--pr-id", "1", "--commit", "0" * 40, "--namespace", "default"]
            + ["--approved-by", "owner"]
        ),
        "operator-state-unavailable",
    ),
    "operator backup": (
        lambda p: (
            ["operator", "backup", "--state-dir", str(p / "state")]
            + ["--output", str(p / "junk" / "backup.tar.gz")]
        ),
        "backup-refused",
    ),
    "operator restore": (
        lambda p: (
            ["operator", "restore", "--archive-file", str(p / "junk")]
            + ["--destination", str(p / "restored")]
        ),
        "restore-refused",
    ),
    "operator serve": (
        lambda p: (
            [
                "operator",
                "serve",
                "--kubeconfig",
                str(p / "kubeconfig"),
                "--context",
                "test",
            ]
            + ["--ui-config", str(p / "junk")]
        ),
        "invalid-access-profile",
    ),
    "explain": (lambda p: ["explain", "no-such-code"], "unknown-error-code"),
    "codegen crd": (lambda p: ["codegen", "crd", str(p / "junk")], "crd-invalid"),
}


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    if argv[0] == "artifacts":
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = artifacts_cli.main(argv[1:])
        return status, out.getvalue(), err.getvalue()
    result = CliRunner().invoke(app, argv)
    return result.exit_code, result.stdout, result.stderr


def test_every_conforming_command_has_a_rejection_case() -> None:
    conforming = {
        path for path, item in COMMANDS.items() if item.contract == "conforms"
    }
    # help-json has no rejection path: it takes no input.
    assert conforming - {"help-json"} == set(CASES)


def test_no_command_is_partial() -> None:
    assert [
        path for path, item in COMMANDS.items() if item.contract != "conforms"
    ] == []


@pytest.mark.parametrize(
    "argv",
    [
        ["render", "--spec", "{p}/missing-release.toml"],
        ["render", "--spec", "{p}/junk"],
        ["render"],
    ],
)
def test_render_refuses_with_json_in_any_format(argv: list[str], files: Path) -> None:
    for extra in ([], ["--format", "yaml"], ["--format", "json"]):
        status, stdout, stderr = _invoke([arg.format(p=files) for arg in argv] + extra)
        assert status == 2, (stdout, stderr)
        body = json.loads(stdout)
        assert body["state"] == "rejected"
        assert body["reason"] == "render-target-invalid"
        assert "[render-target-invalid]" in stderr


@pytest.mark.parametrize("path", sorted(CASES))
def test_rejection_follows_the_contract(path: str, files: Path) -> None:
    argv, reason = CASES[path]
    status, stdout, stderr = _invoke(argv(files))
    assert status == 2, (stdout, stderr)
    assert 2 in COMMANDS[path].exit_codes
    lines = stdout.splitlines()
    assert len(lines) == 1, stdout  # exactly one JSON object on stdout
    body = json.loads(lines[0])
    assert body["state"] == "rejected"
    assert body["reason"] == reason and reason in ERRORS
    assert isinstance(body["message"], str) and body["message"]
    if path.startswith("release "):
        assert body["code"] == reason  # 0.3.0 alias, kept for one release
    assert f"[{reason}]" in stderr
    for line in stderr.splitlines():
        assert not line.lstrip().startswith(("{", "[")), line  # human text only
