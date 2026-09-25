"""Acceptance: the reference app's ``dev`` environment deployed to the fake API.

The example (``examples/reference``) is copied to a temporary directory and
deployed with ``piceli deploy --env dev`` through a small wrapper module that
keeps the example's app and secrets and points the ``dev`` target at an
in-process fake API server (``loopback-http``); the example's checks need a
real cluster (``tests/integration/test_reference_app_kind.py``). The store password is
decrypted by the real ``sops`` binary with the committed example-only age key,
so the test is skipped when ``sops`` is not on ``PATH``.

It checks that every object is applied, that the secret values reach the
Secret and never the output, that a second deploy is a no-op, that changing
one environment value changes exactly one object, and that a rollback
restores it.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.testing import TARGET, TYPES, FakeAPI, serve, write_kubeconfig
from tests.secret_source_fakes import isolate_credentials

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "reference"
SOPS = shutil.which("sops")
TYPES_WITH_CRDS = {
    **TYPES,
    "certificates": ("cert-manager.io/v1", "Certificate", True),
}

pytestmark = pytest.mark.skipif(SOPS is None, reason="needs the sops binary on PATH")

WRAPPER = f"""
from pathlib import Path

from piceli import Pipeline, Target
from piceli.app.render import load_target

HERE = Path(__file__).parent
MODULE = str(HERE / "{{module}}")
app = load_target(MODULE + ":app", HERE)
example = load_target(MODULE + ":pipeline", HERE)
pipeline = Pipeline(
    app,
    {{{{
        "dev": Target.kubeconfig(
            "kubeconfig", context="fake", namespace="{TARGET.namespace}",
            transport="loopback-http",
        )
    }}}},
    secrets=example.secrets,
    state_dir="state",
    execution={{{{"max_seconds": 60, "readiness_seconds": 5, "poll_seconds": 0.05}}}},
)
"""


def _deploy(directory: Path, wrapper: str, *args: str) -> tuple[int, Any, Any]:
    result = CliRunner().invoke(
        cli, ["deploy", str(directory / wrapper) + ":pipeline", "--env", "dev", *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, lines[-1] if lines else {}, result


def _release(directory: Path, wrapper: str, *args: str) -> tuple[int, Any, Any]:
    result = CliRunner().invoke(
        cli,
        [
            "release",
            *args,
            "--spec",
            str(directory / wrapper) + ":pipeline",
            "--env",
            "dev",
        ],
    )
    return result.exit_code, json.loads(result.stdout or "{}"), result


def _password(directory: Path) -> str:
    """The example's store password, decrypted here (never printed)."""
    decrypted = subprocess.run(  # fixed argv; the example-only key
        [str(SOPS), "--decrypt", "--output-type", "json", "secrets/example.enc.yaml"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(directory),
            "SOPS_AGE_KEY_FILE": str(directory / "secrets" / "example-only.agekey"),
        },
    )
    return json.loads(decrypted.stdout)["store"]["password"]


def _hpa(api: FakeAPI) -> tuple[int, int]:
    spec = api.objects[("HorizontalPodAutoscaler", "web")]["spec"]
    return spec["minReplicas"], spec["maxReplicas"]


def test_reference_dev_deploys_changes_one_object_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "reference"
    shutil.copytree(EXAMPLE, directory)
    isolate_credentials(monkeypatch, tmp_path / "home")
    monkeypatch.setenv(
        "SOPS_AGE_KEY_FILE", str(directory / "secrets" / "example-only.agekey")
    )
    # One changed value: the dev autoscaler's max_replicas (2 -> 3).
    source = (directory / "app.py").read_text()
    before = 'autoscalers={"web": Scaling(min_replicas=1, max_replicas=2)}'
    assert before in source
    (directory / "app_v2.py").write_text(
        source.replace(before, before.replace("max_replicas=2", "max_replicas=3"))
    )
    for wrapper, module in (("dev.py", "app.py"), ("dev_v2.py", "app_v2.py")):
        (directory / wrapper).write_text(WRAPPER.format(module=module))
    password = _password(directory)

    with serve(FakeAPI(types=TYPES_WITH_CRDS)) as (api, url):
        write_kubeconfig(url, directory / "kubeconfig")

        code, planned, result = _deploy(directory, "dev.py", "--plan", "--json")
        assert code == 0, result.output
        assert planned["state"] == "planned"
        assert planned["environment"]["name"] == "dev"
        creates = {
            f"{c['kind']}/{c['name']}"
            for c in planned["stages"]["plan"]["changes"]
            if c["operation"] == "create"
        }
        assert {
            "StatefulSet/store",
            "Job/migrate",
            "CronJob/report",
            "HTTPRoute/site",
            "Ingress/site",
            "Certificate/site-tls",
            "NetworkPolicy/internal",
            "HorizontalPodAutoscaler/web",
            "PodDisruptionBudget/web",
            "Role/api-reader",
        } <= creates

        code, done, result = _deploy(
            directory,
            "dev.py",
            "--approve",
            planned["combined_hash"],
            "--json",
        )
        assert code == 0, result.output
        assert done["state"] == "ready", done
        assert password not in result.output
        secret = api.objects[("Secret", "store-credentials-1")]["data"]
        assert base64.b64decode(secret["password"]).decode() == password
        conf = base64.b64decode(secret["redis.conf"]).decode()
        assert f"requirepass {password}\n" in conf
        url_value = base64.b64decode(secret["url"]).decode()
        assert url_value == f"redis://:{password}@store:6379/0"
        assert _hpa(api) == (1, 2)
        assert api.objects[("ConfigMap", "settings")]["data"]["LOG_LEVEL"] == "debug"

        # Unchanged: the plan is a no-op.
        code, again, result = _release(directory, "dev.py", "plan")
        assert code == 0, result.output
        assert set(again["summary"]) == {"no-op"}, again["summary"]

        # One value changed: exactly one object is updated.
        code, changed, result = _release(directory, "dev_v2.py", "plan")
        assert code == 0, result.output
        updates = [
            f"{a['kind']}/{a['name']}"
            for a in changed["actions"]
            if a["operation"] != "no-op"
        ]
        assert updates == ["HorizontalPodAutoscaler/web"], changed["actions"]
        code, done, result = _deploy(directory, "dev_v2.py", "--auto-approve", "--json")
        assert code == 0, result.output
        assert _hpa(api) == (1, 3)

        # Roll back to the first release.
        code, planned, result = _release(directory, "dev_v2.py", "rollback", "previous")
        assert code == 3, result.output
        assert planned["intent"] == "rollback"
        code, outcome, result = _release(
            directory,
            "dev_v2.py",
            "rollback",
            "previous",
            "--approve",
            planned["plan_hash"],
        )
        assert code == 0, result.output
        assert outcome["state"] == "succeeded", outcome
        assert _hpa(api) == (1, 2)
        assert password not in result.output
        for path in (directory / "state").rglob("*.json"):
            text = path.read_text()
            if "secret" not in path.parts and "secrets" not in path.parts:
                assert password not in text, path
