"""State directory, lock, encryption config, credentials, tofu location and secret handling."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.infra.machines import credentials, state
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import VOLATILE, plan_hash
from piceli.infra.machines.render import Rendered
from piceli.infra.machines.tofu import TofuResult, locate, redact
from piceli.k8s.cli import app as cli

from .machines_support import PASSPHRASE, TOKEN

runner = CliRunner()


def _code(caught: pytest.ExceptionInfo[InfraError]) -> str:
    return caught.value.code


def test_state_dir_defaults_under_xdg_and_is_private(homes: Path) -> None:
    path = state.state_dir("edge", None)
    assert path == homes / "xdg-state" / "piceli" / "infra" / "edge"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_state_dir_is_refused_inside_a_git_work_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(InfraError) as caught:
        state.state_dir("edge", repo / "infra-state")
    assert _code(caught) == "infra-state-in-git"
    assert not (repo / "infra-state").exists()


def test_one_command_at_a_time(tmp_path: Path) -> None:
    with state.locked(tmp_path):
        with pytest.raises(InfraError) as caught, state.locked(tmp_path):
            pass
        assert _code(caught) == "infra-state-locked"
    with state.locked(tmp_path):  # released
        pass
    assert stat.S_IMODE((tmp_path / state.LOCK_FILE).stat().st_mode) == 0o600


def test_encryption_config_escapes_the_passphrase() -> None:
    env = state.encryption_env(PASSPHRASE)
    text = env["TF_ENCRYPTION"]
    assert "enforced = true" in text and "plan {" in text and "state {" in text
    # No interpolation and no early end of the string, whatever the passphrase.
    assert '"test-passphrase-$$${not}-\\"quoted\\"-\\\\x"' in text


def test_credentials_are_private_and_checked(homes: Path) -> None:
    path = credentials.save_credential("hcloud", "provider-token", TOKEN)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert credentials.load_credential("hcloud", "provider-token") == TOKEN
    with pytest.raises(InfraError) as caught:
        credentials.load_credential("hcloud", "state-key")
    assert _code(caught) == "infra-credential-invalid"
    with pytest.raises(InfraError) as caught:
        credentials.load_credential("missing", "state-key")
    assert _code(caught) == "infra-credential-missing"
    assert "state-key missing --generate" in str(caught.value)
    os.chmod(path, 0o644)
    with pytest.raises(InfraError) as caught:
        credentials.load_credential("hcloud", "provider-token")
    assert _code(caught) == "infra-credential-unsafe"
    with pytest.raises(InfraError):
        credentials.save_credential("short", "state-key", "too-short")
    with pytest.raises(InfraError):
        credentials.save_credential("ctl", "provider-token", "a\nb")


def test_secrets_provider_reads_stdin_and_never_prints(homes: Path) -> None:
    result = runner.invoke(
        cli, ["secrets", "provider", "hcloud", "--prompt"], input=TOKEN + "\n"
    )
    assert result.exit_code == 0, result.output
    assert TOKEN not in result.stdout and TOKEN not in result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "stored" and body["kind"] == "provider-token"
    assert credentials.load_credential("hcloud", "provider-token") == TOKEN


def test_secrets_provider_refuses_an_argument_without_echoing_it(homes: Path) -> None:
    result = runner.invoke(cli, ["secrets", "provider", "hcloud", "--prompt", TOKEN])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "infra-credential-refused"
    assert TOKEN not in result.stdout and TOKEN not in result.stderr
    assert not credentials.credential_present("hcloud")


def test_secrets_provider_refuses_the_environment(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HCLOUD_TOKEN", TOKEN)
    result = runner.invoke(
        cli, ["secrets", "provider", "hcloud", "--prompt"], input=TOKEN + "\n"
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "infra-credential-refused"
    assert TOKEN not in result.stdout + result.stderr


def test_state_key_generate_and_no_silent_replace(homes: Path) -> None:
    first = runner.invoke(cli, ["secrets", "state-key", "edge-state", "--generate"])
    assert first.exit_code == 0, first.output
    value = credentials.load_credential("edge-state", "state-key")
    assert len(value) >= 32 and value not in first.stdout + first.stderr
    again = runner.invoke(cli, ["secrets", "state-key", "edge-state", "--generate"])
    assert again.exit_code == 2
    assert json.loads(again.stdout)["reason"] == "infra-credential-exists"
    assert credentials.load_credential("edge-state", "state-key") == value
    both = runner.invoke(
        cli, ["secrets", "state-key", "x-state", "--generate", "--prompt"]
    )
    assert json.loads(both.stdout)["reason"] == "infra-prompt-required"


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_locate_requires_opentofu_and_a_recent_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PICELI_TOFU", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(InfraError) as caught:
        locate()
    assert _code(caught) == "infra-tofu-missing"
    terraform = _script(tmp_path / "terraform", 'echo "Terraform v1.9.0"\n')
    with pytest.raises(InfraError) as caught:
        locate(str(terraform))
    assert _code(caught) == "infra-tofu-unsupported"
    old = _script(tmp_path / "old-tofu", 'echo "OpenTofu v1.7.3"\n')
    with pytest.raises(InfraError) as caught:
        locate(str(old))
    assert _code(caught) == "infra-tofu-unsupported"
    good = _script(
        tmp_path / "tofu", 'echo "OpenTofu v1.12.6"; echo "on linux_amd64"\n'
    )
    monkeypatch.setenv("PICELI_TOFU", str(good))
    assert locate().version == (1, 12, 6)


def test_secrets_reach_tofu_through_the_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recording tofu: argv and environment written to files; output echoes the token."""
    record = tmp_path / "record"
    fake = _script(
        tmp_path / "tofu",
        'if [ "$1" = version ]; then echo "OpenTofu v1.12.6"; exit 0; fi\n'
        f'printf "%s\\n" "$@" > {record}.argv\n'
        f"env > {record}.env\n"
        'echo "token is $HCLOUD_TOKEN"; echo "key $TF_ENCRYPTION" >&2\n',
    )
    monkeypatch.setenv("TF_LOG", "trace")  # never passed through
    monkeypatch.setenv("SOME_OTHER_SECRET", "ambient")
    tofu = locate(str(fake))
    tofu.secrets = [PASSPHRASE]
    env = {"HCLOUD_TOKEN": TOKEN, **state.encryption_env(PASSPHRASE)}
    result = tofu.run(["plan", "-json"], cwd=tmp_path, env=env)
    argv = (tmp_path / "record.argv").read_text()
    child = (tmp_path / "record.env").read_text()
    assert TOKEN not in argv and PASSPHRASE not in argv
    assert f"HCLOUD_TOKEN={TOKEN}" in child
    assert "TF_LOG" not in child and "SOME_OTHER_SECRET" not in child
    assert "TF_IN_AUTOMATION=1" in child
    assert TOKEN not in result.stdout and "<redacted>" in result.stdout
    assert PASSPHRASE not in result.stderr


def test_redact_replaces_longest_first() -> None:
    assert redact("abc abcdef", ["abc", "abcdef"]) == "<redacted> <redacted>"


def test_diagnostics_from_json_lines() -> None:
    lines = "\n".join(
        [
            json.dumps(
                {
                    "type": "diagnostic",
                    "diagnostic": {
                        "severity": "error",
                        "summary": "Bad",
                        "detail": "thing",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "diagnostic",
                    "diagnostic": {"severity": "warning", "summary": "meh"},
                }
            ),
        ]
    )
    assert TofuResult(1, lines, "").diagnostics() == ["Bad thing"]
    assert TofuResult(1, "", "one\ntwo\n").diagnostics() == ["one", "two"]


def test_plan_hash_ignores_timestamp_and_attribute_order() -> None:
    rendered = Rendered({"terraform": {}}, "", {})
    first = {
        "timestamp": "a",
        "relevant_attributes": [{"a": 1}, {"b": 2}],
        "resource_changes": [],
    }
    second = {
        "timestamp": "b",
        "relevant_attributes": [{"b": 2}, {"a": 1}],
        "resource_changes": [],
    }
    assert "timestamp" in VOLATILE
    assert plan_hash("apply", rendered, first) == plan_hash("apply", rendered, second)
    assert plan_hash("apply", rendered, first) != plan_hash("destroy", rendered, first)
    changed = {**first, "resource_changes": [{"address": "x"}]}
    assert plan_hash("apply", rendered, first) != plan_hash("apply", rendered, changed)


def test_the_tofu_child_cannot_read_stdin(tmp_path: Path) -> None:
    fake = _script(
        tmp_path / "tofu",
        'if [ "$1" = version ]; then echo "OpenTofu v1.12.6"; exit 0; fi\nread line; echo "read:$line"\n',
    )
    tofu = locate(str(fake))
    result = tofu.run(["plan"], cwd=tmp_path, env={})
    assert result.stdout.strip() == "read:"
    assert subprocess.DEVNULL  # documented: stdin is closed
