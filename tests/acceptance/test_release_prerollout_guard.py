"""Acceptance: ``piceli release`` refuses an App with pre-rollout checks.

Only ``piceli deploy`` runs ``App.pre_rollout`` checks. A release spec whose
composition returns such an App must be refused at plan time, before any
write to the cluster, instead of skipping the checks silently.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from piceli.k8s.cli import app
from tests.acceptance.fake_api import TARGET, serve

DIGEST = "sha256:" + "4" * 64

COMPOSITION = textwrap.dedent(
    """\
    from piceli import App


    def build(ctx):
        app = App("shop")
        web = app.deployment("web", image=ctx.image("web"), ports=[80])
        if CHECKED:
            app.pre_rollout(web, ["web", "check-config"])
        return app
    """
)


def _spec(tmp_path: Path, url: str, checked: bool) -> Path:
    (tmp_path / "composition.py").write_text(f"CHECKED = {checked}\n" + COMPOSITION)
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
            name = "shop"
            owner = "shop"
            field_manager = "shop"
            composition = "composition.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 30
            readiness_seconds = 1
            poll_seconds = 0.05

            [images]
            web = "registry.example/web@{DIGEST}"
            """
        )
    )
    return spec


def test_release_plan_refuses_an_app_with_pre_rollout_checks(tmp_path: Path) -> None:
    runner = CliRunner()
    with serve() as (api, url):
        spec = _spec(tmp_path, url, checked=True)
        planned = runner.invoke(app, ["release", "plan", "--spec", str(spec)])
        writes = [
            item for item in api.requests if item["method"] not in {"GET", "HEAD"}
        ]
    assert planned.exit_code == 2, planned.output
    result = json.loads(planned.stdout)
    assert result["state"] == "rejected"
    assert result["reason"] == "release-prerollout-unsupported"
    assert "piceli deploy" in planned.stderr
    assert writes == []
    assert not (tmp_path / "state" / "plans").exists()


def test_release_plan_accepts_the_same_app_without_checks(tmp_path: Path) -> None:
    runner = CliRunner()
    with serve() as (_api, url):
        spec = _spec(tmp_path, url, checked=False)
        planned = runner.invoke(app, ["release", "plan", "--spec", str(spec)])
    assert planned.exit_code == 0, planned.output
