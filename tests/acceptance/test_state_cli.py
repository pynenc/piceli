"""Acceptance: ``piceli state show|pull|export|import`` and restoring a lost state.

Uses the shared-state shop pipeline of ``test_deploy_shared_state`` on the
fake API: deploy, export (with and without the encrypted secret material),
lose every state object in the namespace, import, deploy again and check
that nothing was regenerated.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.test_deploy_shared_state import Runner, shared  # noqa: F401

pytest.importorskip("cryptography")


def state(runner: Runner, *args: str) -> tuple[int, dict[str, Any], Any]:
    code, lines, result = runner.invoke(
        "state", args[0], "--spec", str(runner.root / "app.py:pipeline"), *args[1:]
    )
    return code, (lines[-1] if lines else {}), result


def key_file(root: Path, mode: int = 0o600) -> Path:
    path = root / "state.key"
    path.write_text("k" * 40 + "\n")
    path.chmod(mode)
    return path


def secret_value(api: Any) -> bytes:
    return base64.b64decode(api.objects[("Secret", "credentials")]["data"]["password"])


def test_show_and_pull_the_shared_state(shared) -> None:  # noqa: F811
    api, root = shared
    code, _, result = Runner(root, "one").deploy("--auto-approve")
    assert code == 0, result.stdout + result.stderr
    fresh = Runner(root, "two")
    code, body, result = state(fresh, "show")
    assert code == 0, result.stdout + result.stderr
    assert body["backend"] == "cluster" and body["lock"] is None
    assert body["shared"]["generation"] >= 1 and body["local_files"] == 0
    code, body, _ = state(fresh, "pull")
    assert code == 0 and body["state"] == "pulled"
    assert body["local_files"] > 0
    assert (fresh.state_dir / "release" / "catalog.json").is_file()


def test_export_leaves_secrets_out_or_encrypts_them(shared) -> None:  # noqa: F811
    api, root = shared
    runner = Runner(root, "one")
    code, _, _ = runner.deploy("--auto-approve")
    assert code == 0
    value = secret_value(api)
    code, body, result = state(runner, "export", "--out", str(root / "plain.json"))
    assert code == 0, result.stdout + result.stderr
    assert body["private"]["state"] == "excluded" and body["private"]["files"] > 0
    text = (root / "plain.json").read_text()
    assert value.decode() not in text and base64.b64encode(value).decode() not in text
    code, body, _ = state(runner, "export", "--out", str(root / "plain.json"))
    assert (code, body["reason"]) == (2, "state-output-exists")
    code, body, _ = state(
        runner, "export", "--out", str(root / "x.json"), "--include-secrets"
    )
    assert (code, body["reason"]) == (2, "state-key-required")
    loose = key_file(root, 0o644)
    code, body, _ = state(
        runner,
        "export",
        "--out",
        str(root / "x.json"),
        "--include-secrets",
        "--key-file",
        str(loose),
    )
    assert (code, body["reason"]) == (2, "state-key-required")
    code, body, result = state(
        runner,
        "export",
        "--out",
        str(root / "full.json"),
        "--include-secrets",
        "--key-file",
        str(key_file(root)),
    )
    assert code == 0, result.stdout + result.stderr
    assert body["private"]["state"] == "encrypted"
    text = (root / "full.json").read_text()
    assert value.decode() not in text and base64.b64encode(value).decode() not in text
    assert json.loads(text)["private"]["cipher"] == "AES-256-GCM"


def test_a_lost_state_is_restored_from_an_export(shared) -> None:  # noqa: F811
    api, root = shared
    runner = Runner(root, "one")
    code, _, _ = runner.deploy("--auto-approve")
    assert code == 0
    value = secret_value(api)
    key = key_file(root)
    code, _, _ = state(
        runner,
        "export",
        "--out",
        str(root / "full.json"),
        "--include-secrets",
        "--key-file",
        str(key),
    )
    code, _, _ = state(runner, "export", "--out", str(root / "plain.json"))
    # Disaster: every state object in the namespace and the runner are gone.
    with api.lock:
        for item in [
            (kind, name)
            for kind, name in api.objects
            if kind in {"Secret", "Lease"} and name.startswith("piceli-")
        ]:
            del api.objects[item]
    import shutil

    shutil.rmtree(runner.state_dir)
    other = Runner(root, "two")
    code, body, _ = state(other, "import", "--in", str(root / "plain.json"))
    assert (code, body["reason"]) == (2, "state-import-partial")
    code, body, _ = state(other, "import", "--in", str(root / "full.json"))
    assert (code, body["reason"]) == (2, "state-key-required")
    wrong = root / "wrong.key"
    wrong.write_text("w" * 40)
    wrong.chmod(0o600)
    code, body, _ = state(
        other, "import", "--in", str(root / "full.json"), "--key-file", str(wrong)
    )
    assert (code, body["reason"]) == (2, "state-key-required")
    args = ("import", "--in", str(root / "full.json"), "--key-file", str(key))
    code, body, result = state(other, *args)
    assert code == 3 and body["state"] == "approval-required", result.stdout
    digest = body["import_digest"]
    assert ("Secret", "piceli-state-shop") not in api.objects  # nothing changed
    code, body, _ = state(other, *args, "--approve", "0" * 64)
    assert (code, body["reason"]) == (2, "state-import-changed")
    code, body, result = state(other, *args, "--approve", digest)
    assert code == 0, result.stdout + result.stderr
    assert body["state"] == "imported" and body["generation"] == 1
    # The restored state knows the deployed release and its secret versions.
    code, lines, result = Runner(root, "three").deploy("--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert lines[-1]["stages"]["apply"] == "skipped"
    assert secret_value(api) == value


def test_an_export_of_another_release_is_refused(shared, tmp_path) -> None:  # noqa: F811
    api, root = shared
    runner = Runner(root, "one")
    code, _, _ = runner.deploy("--auto-approve")
    code, _, _ = state(runner, "export", "--out", str(root / "plain.json"))
    document = json.loads((root / "plain.json").read_text())
    (root / "other.json").write_text(json.dumps({**document, "name": "other"}))
    code, body, _ = state(
        runner, "import", "--in", str(root / "other.json"), "--allow-partial"
    )
    assert (code, body["reason"]) == (2, "state-export-invalid")
    (root / "broken.json").write_text(json.dumps({**document, "public": "AAAA"}))
    code, body, _ = state(runner, "import", "--in", str(root / "broken.json"))
    assert (code, body["reason"]) == (2, "state-export-invalid")
