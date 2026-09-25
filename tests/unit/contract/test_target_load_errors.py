"""Every command that imports a ``MODULE:ATTR`` target keeps the contract when it raises.

A model module that raises while importing (a duplicate name, a wrong
keyword, any exception) is a rejection: one JSON object on stdout with the
command's code and a ``message`` naming the exception type, exit ``2``, and
never a traceback.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

MODULES = {
    "duplicate-service": (
        """
        from piceli import App

        app = App("shop")
        web = app.deployment("web", image="nginx:1.27", ports=[80])
        app.service(web, port=80)
        app.service(web, port=80)
        pipeline = app
        """,
        "ValueError: Service 'web' is already declared",
    ),
    "bad-keyword": (
        """
        from piceli import App

        app = App("shop")
        app.deployment("web", image="nginx:1.27", portz=[80])
        pipeline = app
        """,
        "TypeError: ",
    ),
    "any-exception": ('raise KeyError("missing")\n', "KeyError: 'missing'"),
}

#: (arguments before the target, arguments after it, the rejection's code)
COMMANDS = {
    "render": (["render"], ["--format", "json"], "render-target-invalid"),
    "deploy": (["deploy"], ["--plan", "--json"], "pipeline-load-failed"),
    "release plan": (["release", "plan", "--spec"], [], "pipeline-load-failed"),
    "release status": (["release", "status", "--spec"], [], "pipeline-load-failed"),
    "status": (["status"], ["--json"], "access-target-invalid"),
    "access": (["access"], [], "access-target-invalid"),
}


@pytest.mark.parametrize("module", sorted(MODULES))
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_target_module_that_raises_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, command: str
) -> None:
    source, text = MODULES[module]
    before, after, code = COMMANDS[command]
    (tmp_path / "model.py").write_text(textwrap.dedent(source))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PICELI_DEBUG", raising=False)
    result = CliRunner().invoke(cli, [*before, "model.py:pipeline", *after])
    assert result.exit_code == 2, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.stdout
    body = json.loads(lines[0])
    assert body["state"] == "rejected"
    assert body["reason"] == code
    assert "failed: " in body["message"] and text in body["message"]
    assert "Traceback" not in result.stdout + result.stderr
