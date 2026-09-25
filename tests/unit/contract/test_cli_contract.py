"""``piceli explain``, ``piceli help-json`` and the shared output helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import __main__ as entry
from piceli import reference_docs
from piceli.cli_contract import (
    COMMANDS,
    EXIT_CODES,
    Rejected,
    emit_json,
    help_tree,
    leaf_commands,
    reject,
)
from piceli.errors import ERRORS
from piceli.k8s.cli import app

runner = CliRunner()


def test_explain_prints_the_entry() -> None:
    result = runner.invoke(app, ["explain", "tool-pin-mismatch"])
    assert result.exit_code == 0
    assert result.stdout.startswith("tool-pin-mismatch: Tool differs from its pin")
    assert "retry-safe: no" in result.stdout


def test_explain_json_is_the_registry_entry() -> None:
    result = runner.invoke(app, ["explain", "deadline-exceeded", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == ERRORS["deadline-exceeded"].to_dict()


def test_explain_unknown_code_is_rejected() -> None:
    result = runner.invoke(app, ["explain", "no-such-code", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "state": "rejected",
        "reason": "unknown-error-code",
        "message": "Unknown error code",
    }
    assert "unknown-error-code" in result.stderr


def test_reject_prints_json_on_stdout_and_text_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(Rejected) as raised:
        reject("tool-pin-mismatch")
    assert raised.value.code == 2 and raised.value.reason == "tool-pin-mismatch"
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "state": "rejected",
        "reason": "tool-pin-mismatch",
        "message": ERRORS["tool-pin-mismatch"].title,
    }
    assert err.startswith("rejected: Tool differs from its pin [tool-pin-mismatch]")
    assert ERRORS["tool-pin-mismatch"].fix in err


def test_reject_refuses_unregistered_codes() -> None:
    with pytest.raises(ValueError, match="unregistered"):
        reject("made-up-code")


def test_emit_json_is_one_sorted_line(capsys: pytest.CaptureFixture[str]) -> None:
    emit_json({"b": 1, "a": Path("/x")})
    assert capsys.readouterr().out == '{"a": "/x", "b": 1}\n'


def _tree() -> dict[str, Any]:
    result = runner.invoke(app, ["help-json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_help_json_covers_every_command_with_a_contract() -> None:
    tree = _tree()
    assert tree["schema"] == "piceli.help.v1"
    assert tree["error_codes"] == sorted(ERRORS)
    assert set(tree["conventions"]["exit_codes"]) == {str(c) for c in EXIT_CODES}
    leaves = {leaf["path"]: leaf for leaf in leaf_commands(tree["root"])}
    assert set(leaves) == set(COMMANDS), "update COMMANDS in piceli/cli_contract.py"
    for path, leaf in leaves.items():
        contract = leaf["contract"]
        assert contract is not None, path
        assert leaf["help"], path
        effects = contract["side_effects"]
        assert effects["cluster"] in {"none", "reads", "writes"}
        assert contract["contract"] in {"conforms", "partial"}
        assert set(contract["exit_codes"]) <= {str(c) for c in EXIT_CODES}
        for param in leaf["params"]:
            assert param["type"] in {
                "text",
                "integer",
                "float",
                "boolean",
                "path",
                "choice",
            }
            if param["type"] == "choice":
                assert param.get("choices"), param


def test_version_option_prints_the_package_version() -> None:
    import importlib.metadata

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"piceli {importlib.metadata.version('piceli')}\n"
    root = _tree()["root"]
    assert "--version" in {
        flag for param in root["params"] for flag in param.get("flags", ())
    }
    assert root["help"].startswith("Kubernetes infrastructure as typed Python")


def test_help_json_describes_options() -> None:
    leaves = {leaf["path"]: leaf for leaf in leaf_commands(_tree()["root"])}
    apply = leaves["release apply"]
    assert apply["contract"]["approval_required"] is True
    assert apply["contract"]["side_effects"]["cluster"] == "writes"
    params = {param["name"]: param for param in apply["params"]}
    assert params["spec"] == {
        "name": "spec",
        "kind": "option",
        "type": "text",
        "required": True,
        "multiple": False,
        "flags": ["--spec"],
        "help": (
            "path/to/release.toml, or MODULE:ATTR (path/to/file.py:ATTR) naming "
            "a piceli Pipeline: the release `piceli deploy` manages"
        ),
    }
    assert params["adopt"]["multiple"] is True
    deliver = leaves["artifacts deliver"]
    assert ["archive", "image"] in deliver["mutually_exclusive"]
    timeout = {param["name"]: param for param in deliver["params"]}["timeout"]
    assert timeout["type"] == "float" and timeout["default"] == 600
    assert leaves["release plan"]["contract"]["side_effects"]["read_only"] is False
    assert leaves["release status"]["contract"]["side_effects"]["read_only"] is True


def test_dash_dash_help_json_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["piceli", "--help-json"])
    with pytest.raises(SystemExit) as raised:
        entry.main()
    assert raised.value.code in (0, None)
    assert json.loads(capsys.readouterr().out) == help_tree()


def test_reference_pages_are_current() -> None:
    stale = [
        str(path)
        for path, content in reference_docs.pages().items()
        if not path.exists() or path.read_text() != content
    ]
    assert not stale, "run `python -m piceli.reference_docs` (make docs-reference)"


def test_reference_check_mode(tmp_path: Path) -> None:
    assert reference_docs.main(["--check", "--docs", str(tmp_path)]) == 1
    assert reference_docs.main(["--docs", str(tmp_path)]) == 0
    assert reference_docs.main(["--check", "--docs", str(tmp_path)]) == 0
    errors = (tmp_path / "reference" / "errors.md").read_text()
    for code in ERRORS:
        assert f"(error-{code})=" in errors


def test_contract_modules_import_without_side_effects() -> None:
    code = (
        "import sys; import piceli.errors, piceli.cli_contract, "
        "piceli.reference_docs; "
        "loaded = [m for m in ('kubernetes', 'typer', 'piceli.k8s') if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else None]


def test_agents_page_matches_command_metadata() -> None:
    text = (reference_docs.DOCS / "agents.md").read_text()
    safe = _section(text, "## Commands an agent may run without asking")
    ask = _section(text, "## Commands that need the owner's approval")
    for path, contract in COMMANDS.items():
        name = f"`piceli {path}`"
        changes = contract.approval_required or contract.cluster.endswith("writes")
        if name in safe:
            assert not changes, f"{path} changes state but is listed as safe"
        if changes:
            assert name in ask, f"{path} needs approval: list it in docs/agents.md"


def test_llms_txt_links_to_existing_pages() -> None:
    root = reference_docs.DOCS.parent
    text = (root / "llms.txt").read_text()
    prefix = "https://docs.pynenc.org/projects/piceli/en/(?:stable|latest)/"
    pages = re.findall(prefix + r"([\w/]+)\.html", text)
    assert "agents" in pages and "reference/errors" in pages
    for page in pages:
        assert (reference_docs.DOCS / f"{page}.md").exists(), page
