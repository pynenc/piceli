"""``piceli secrets delete NAME``: remove a stored credential (0.24.0).

The spent bootstrap key of a server's user data, for example. The value is
never printed; without ``--yes`` it asks on a terminal and otherwise exits
3 with nothing deleted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra.machines import credentials
from piceli.k8s.cli import app
from piceli.k8s.cli import infra as infra_cli

KEY = "tskey-auth-kExample-0123456789abcdef"


def _run(*args: str, stdin: str = "") -> Any:
    return CliRunner().invoke(app, ["secrets", "delete", *args], input=stdin)


def test_delete_removes_the_credential_and_never_prints_it(homes: Path) -> None:
    path = credentials.save_credential("web-1-boot", "user-data", KEY)
    done = _run("web-1-boot", "--kind", "user-data", "--yes")
    assert done.exit_code == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == {
        "state": "deleted", "name": "web-1-boot", "kind": "user-data",
        "path": str(path),
    }  # fmt: skip
    assert not path.exists()
    assert KEY not in done.stdout + done.stderr
    assert "deleted credential web-1-boot (user-data)" in done.stderr
    # Again: it is gone.
    again = _run("web-1-boot", "--yes")
    assert again.exit_code == 2
    assert json.loads(again.stdout)["reason"] == "infra-credential-missing"


@pytest.mark.parametrize(
    ("option", "kind"),
    [
        ("provider", "provider-token"),
        ("state-key", "state-key"),
        ("nix-cache", "nix-signing-key"),
        ("s3", "s3-keys"),
        ("age", "age-identity"),
        ("user-data", "user-data"),
        ("s3-keys", "s3-keys"),  # the stored kind's own name works too
    ],
)
def test_kind_names_follow_the_secrets_commands(
    homes: Path, option: str, kind: str
) -> None:
    value = "x" * 32 if kind != "s3-keys" else "AKID:secret"
    credentials.save_credential("one", kind, value)
    done = _run("one", "--kind", option, "--yes")
    assert done.exit_code == 0, done.stdout
    assert json.loads(done.stdout)["kind"] == kind


def test_another_kind_is_refused_and_kept(homes: Path) -> None:
    path = credentials.save_credential("hcloud", "provider-token", KEY)
    done = _run("hcloud", "--kind", "user-data", "--yes")
    assert done.exit_code == 2
    body = json.loads(done.stdout)
    assert body["reason"] == "infra-credential-invalid"
    assert "provider-token" in body["message"]
    assert path.exists() and KEY not in done.stdout + done.stderr
    unknown = _run("hcloud", "--kind", "password", "--yes")
    assert unknown.exit_code == 2 and path.exists()
    assert json.loads(unknown.stdout)["reason"] == "infra-credential-invalid"


def test_without_yes_it_needs_approval(homes: Path) -> None:
    path = credentials.save_credential("web-1-boot", "user-data", KEY)
    done = _run("web-1-boot")
    assert done.exit_code == 3
    assert json.loads(done.stdout) == {
        "state": "approval-required", "name": "web-1-boot", "kind": "user-data",
        "path": str(path),
        "approve_command": "piceli secrets delete web-1-boot --kind user-data --yes",
    }  # fmt: skip
    assert path.exists()


def test_on_a_terminal_it_asks_for_the_name(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infra_cli, "_interactive", lambda: True)
    path = credentials.save_credential("web-1-boot", "user-data", KEY)
    declined = _run("web-1-boot", stdin="web-1\n")
    assert declined.exit_code == 3 and path.exists()
    confirmed = _run("web-1-boot", stdin="web-1-boot\n")
    assert confirmed.exit_code == 0, confirmed.stdout
    assert json.loads(confirmed.stdout.splitlines()[-1])["state"] == "deleted"
    assert not path.exists()


def test_a_damaged_credential_can_still_be_deleted(homes: Path) -> None:
    path = credentials.save_credential("old", "provider-token", KEY)
    path.write_text("not json")
    assert _run("old", "--kind", "provider", "--yes").exit_code == 2
    done = _run("old", "--yes")
    assert done.exit_code == 0
    assert json.loads(done.stdout)["kind"] is None and not path.exists()


def test_a_name_that_is_not_one_is_refused(homes: Path) -> None:
    done = _run("../web-1-boot", "--yes")
    assert done.exit_code == 2
    assert json.loads(done.stdout)["reason"] == "infra-credential-invalid"
