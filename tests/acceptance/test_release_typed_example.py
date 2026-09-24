"""Acceptance: the typed ``examples/release`` composition plans and applies through
``piceli release`` against the fake API, exactly like the dict example did."""

from __future__ import annotations

import base64
import json
import shutil
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from tests.acceptance.fake_api import TARGET, serve

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "release"
DIGEST = "sha256:" + "3" * 64
OPENSSL = Path("/usr/bin/openssl")

pytestmark = pytest.mark.skipif(not OPENSSL.exists(), reason="needs /usr/bin/openssl")


def _run(spec: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result


def test_typed_example_plans_and_applies(tmp_path):
    shutil.copy(EXAMPLE / "composition.py", tmp_path / "composition.py")
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                users: [{{name: nobody, user: {{}}}}]
                contexts: [{{name: fake, context: {{cluster: fake, user: nobody}}}}]
                """
            )
        )
        spec = tmp_path / "release.toml"
        spec.write_text(
            textwrap.dedent(
                f"""
                [target]
                kubeconfig = "kubeconfig"
                context = "fake"
                namespace = "{TARGET.namespace}"
                transport = "loopback-http"

                [release]
                name = "web"
                owner = "typed-example"
                field_manager = "typed-example"
                composition = "composition.py:build"
                state_dir = "state"

                [execution]
                max_seconds = 30
                readiness_seconds = 1
                poll_seconds = 0.05

                [images]
                web = "docker.io/library/nginx@{DIGEST}"

                [secrets.api-token]
                type = "random"

                [secrets.web-tls]
                type = "tls-self-signed"
                dns_names = ["web"]

                [values]
                greeting = "typed"
                replicas = 2
                """
            )
        )
        code, planned, result = _run(spec, "plan")
        assert code == 0, result.output
        assert planned["summary"] == {"create": 4}
        code, applied, result = _run(spec, "apply", "--approve", planned["plan_hash"])
        assert code == 0, result.output
        assert applied["execution"]["state"] == "ready"
        deployment = api.objects[("Deployment", "web")]
        assert deployment["spec"]["replicas"] == 2
        assert deployment["spec"]["selector"] == {
            "matchLabels": {"app.kubernetes.io/name": "web"}
        }
        assert api.objects[("ConfigMap", "web-config")]["data"] == {"greeting": "typed"}
        token = api.objects[("Secret", "web-token")]["data"]["token"]
        assert base64.b64decode(token) and token != "<private>"
        assert set(api.objects[("Secret", "web-tls")]["data"]) == {"tls.crt", "tls.key"}
