"""Pipeline targets: exec credential plugin opt-in, checks context, auto-rollback output.

Exec plugins are the fake plugin of ``tests/unit/release/test_exec_credentials``;
HTTP goes only to the loopback fake API (``piceli.testing``).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import App
from piceli.checks import Checks
from piceli.errors import ERRORS
from piceli.k8s.access import resolve_target
from piceli.k8s.cli import app as cli
from piceli.k8s.ops.exec_credentials import ExecPolicy
from piceli.k8s.release_runner import ReleaseError
from piceli.pipeline import CheckContext, Pipeline, PipelineError, Target
from piceli.pipeline.checks import check_context
from piceli.pipeline.compose import pinned_images, release_spec
from piceli.pipeline.runner import PipelineRunner, _Work
from piceli.testing import TARGET, serve
from tests.unit.release.test_exec_credentials import (
    exec_user,
    kubeconfig,
    make_plugin,
    runs,
)

PIN = "sha256:" + "c" * 64
CACHE = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64


def _pipeline(**target: Any) -> Pipeline:
    app = App("shop")
    web = app.deployment("web", image=CACHE, ports=[8080])
    return Pipeline(
        app,
        Target.kubeconfig("kc", context="explicit", namespace="shop", **target),
        checks=Checks.http(web, "/", expect=200),
        rollback_on_failed_checks=True,
    )


# ------------------------------------------------------------------- G2


@pytest.mark.parametrize(
    "keys",
    [
        {"exec_sha256": PIN},
        {"exec_pass_env": ["AWS_PROFILE"]},
        {"exec_timeout_seconds": 5},
        {"allow_exec": True, "exec_sha256": "c" * 64},
        {"allow_exec": True, "exec_pass_env": ["BAD NAME"]},
        {"allow_exec": True, "exec_pass_env": "AWS_PROFILE"},
        {"allow_exec": True, "exec_timeout_seconds": 301},
        {"allow_exec": "yes"},
    ],
)
def test_target_exec_options_are_validated_like_the_spec_target(keys) -> None:
    with pytest.raises(PipelineError) as caught:
        Target.kubeconfig("kc", context="ctx", namespace="shop", **keys)
    assert caught.value.code == "pipeline-invalid"


def test_target_exec_options_reach_the_release_spec_and_checks() -> None:
    pipeline = _pipeline(
        allow_exec=True,
        exec_sha256=PIN,
        exec_pass_env=["AWS_PROFILE"],
        exec_timeout_seconds=20,
    )
    policy = ExecPolicy(True, PIN, ("AWS_PROFILE",), 20)
    assert pipeline.target.exec_policy() == policy
    images = pinned_images(pipeline, {})
    spec = release_spec(pipeline, images)
    assert spec.kubeconfig_target().exec_policy == policy
    # Without the opt-in no exec key reaches the spec.
    plain = _pipeline()
    assert plain.target.exec_policy() == ExecPolicy()
    spec = release_spec(plain, pinned_images(plain, {}))
    assert spec.kubeconfig_target().exec_policy == ExecPolicy()
    assert not spec.model.target.allow_exec

    context = CheckContext(
        target=pipeline.target,
        kubeconfig=pipeline.target.kubeconfig,
        context="explicit",
        namespace="shop",
        release="shop-1",
        images={"web": CACHE},
    )
    with check_context(context) as adapted:
        assert adapted.exec_policy == policy
        assert adapted.images == {"web": CACHE}
        assert adapted.release == "shop-1" and adapted.namespace == "shop"


def test_access_target_of_a_pipeline_carries_its_exec_policy(tmp_path: Path) -> None:
    (tmp_path / "deploy.py").write_text(
        textwrap.dedent(
            f"""
            from piceli import App, Pipeline, Target

            app = App("shop")
            app.deployment("web", image="{CACHE}")
            pipeline = Pipeline(app, Target.kubeconfig(
                "kc", context="explicit", namespace="shop", allow_exec=True,
                exec_sha256="{PIN}",
            ))
            """
        )
    )
    resolved = resolve_target(f"{tmp_path / 'deploy.py'}:pipeline", tmp_path)
    assert resolved.exec_policy == ExecPolicy(True, PIN)


def test_release_spec_for_release_commands_carries_the_checks() -> None:
    pipeline = _pipeline()
    images = pinned_images(pipeline, {})
    deploy_spec = release_spec(pipeline, images)
    assert deploy_spec.model.checks == ()  # piceli deploy runs them itself
    spec = release_spec(pipeline, images, with_checks=True)
    assert [check.label for check in spec.model.checks] == ["http-deployment-web"]
    assert spec.model.release.rollback_on_failed_checks is True


# ---------------------------------------------- exec gating on the fake API


def _module(tmp_path: Path, kc: Path, name: str, **target: Any) -> str:
    options = "".join(f", {key}={value!r}" for key, value in target.items())
    (tmp_path / f"{name}.py").write_text(
        textwrap.dedent(
            f"""
            from piceli import App, Pipeline, Target

            app = App("shop")
            app.deployment("web", image="{CACHE}")
            pipeline = Pipeline(
                app,
                Target.kubeconfig(
                    {str(kc)!r}, context="explicit", namespace="{TARGET.namespace}",
                    transport="loopback-http"{options},
                ),
                state_dir="state",
            )
            """
        )
    )
    return f"{tmp_path / name}.py:pipeline"


def test_release_and_status_run_the_plugin_only_when_the_target_allows_it(
    tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path / "bin")
    runner = CliRunner()
    with serve() as (_api, url):
        kc = kubeconfig(tmp_path / "kc", url, exec_user(str(plugin)))
        refused = _module(tmp_path, kc, "refused")
        result = runner.invoke(cli, ["release", "plan", "--spec", refused])
        assert result.exit_code == 2, result.stdout
        assert json.loads(result.stdout)["reason"] == "exec-auth-not-allowed"
        result = runner.invoke(cli, ["status", refused, "--json"])
        assert "exec-auth-not-allowed" in json.loads(result.stdout)["errors"]
        assert runs(plugin) == 0

        allowed = _module(tmp_path, kc, "allowed", allow_exec=True)
        result = runner.invoke(cli, ["release", "plan", "--spec", allowed])
        assert result.exit_code == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["state"] == "planned"
        assert runs(plugin) >= 1
        before = runs(plugin)
        result = runner.invoke(cli, ["status", allowed, "--json"])
        document = json.loads(result.stdout)
        assert "exec-auth-not-allowed" not in document["errors"]
        assert runs(plugin) > before


# ------------------------------------------------------------------- G7


class _Rollbacks:
    def __init__(self, *, target: Exception | None, plan: Exception | None) -> None:
        self.target, self.fail = target, plan

    def resolve_rollback_target(self, name: str) -> str:
        if self.target is not None:
            raise self.target
        return "shop-previous"

    def plan(self, **_: Any) -> Any:
        assert self.fail is not None
        raise self.fail


@pytest.mark.parametrize(
    ("runner", "state", "reason"),
    [
        (
            _Rollbacks(
                target=ReleaseError("no previous", code="no-previous-release"),
                plan=None,
            ),
            "unavailable",
            "checks-rollback-unavailable",
        ),
        (
            _Rollbacks(
                target=None,
                plan=ReleaseError("gone", code="unknown-release"),
            ),
            "rejected",
            "unknown-release",
        ),
        (
            _Rollbacks(target=None, plan=RuntimeError("boom")),
            "rejected",
            "pipeline-stage-error",
        ),
    ],
)
def test_automatic_rollback_reports_follow_the_output_contract(
    runner, state, reason
) -> None:
    pipeline = _pipeline()
    work = _Work(runner=runner)  # type: ignore[arg-type]
    value = PipelineRunner(pipeline)._rollback(work)
    assert value["state"] == state
    assert value["reason"] == reason and reason in ERRORS
    assert isinstance(value["message"], str) and value["message"]
    assert "refused" not in json.dumps(value)


def test_no_refused_state_remains_in_the_sources() -> None:
    root = Path(__file__).resolve().parents[3] / "piceli"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if '"state": "refused"' in path.read_text()
        or 'state="refused"' in path.read_text()
    ]
    assert offenders == []


def test_access_refuses_an_exec_user_unless_the_target_allows_it(
    tmp_path: Path,
) -> None:
    import sys

    plugin = make_plugin(tmp_path / "bin")
    kc = kubeconfig(tmp_path / "kc", "https://127.0.0.1:1", exec_user(str(plugin)))
    (tmp_path / "forwarded.py").write_text(
        textwrap.dedent(
            f"""
            from piceli import App, Pipeline, Target

            app = App("shop")
            web = app.deployment("web", image="{CACHE}", ports=[8080])
            app.service(web, port=8080, access=app.access.forward(local=1))
            pipeline = Pipeline(app, Target.kubeconfig(
                {str(kc)!r}, context="explicit", namespace="shop",
            ))
            """
        )
    )
    result = CliRunner().invoke(
        cli,
        [
            "access",
            f"{tmp_path / 'forwarded.py'}:pipeline",
            "--kubectl",
            sys.executable,  # never started: the refusal comes first
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert json.loads(result.stdout)["reason"] == "exec-auth-not-allowed"
    assert runs(plugin) == 0


def test_default_runner_hands_checks_a_piceli_checks_context(monkeypatch) -> None:
    """``Checks.http`` in a Pipeline uses the real check context (no AttributeError)."""
    import piceli.checks as checks_module
    from piceli.checks import CheckContext as ChecksContext
    from piceli.pipeline.checks import default_runner

    seen: list[Any] = []

    def run_checks(checks: Any, context: Any) -> Any:
        seen.append(context)
        return "report"

    monkeypatch.setattr(checks_module, "run_checks", run_checks)
    pipeline = _pipeline(allow_exec=True)
    context = CheckContext(
        target=pipeline.target,
        kubeconfig=Path("/nonexistent/kubeconfig"),
        context="explicit",
        namespace="shop",
        release="shop-1",
    )
    assert default_runner()(pipeline.checks, context) == "report"
    assert isinstance(seen[0], ChecksContext)
    assert seen[0].exec_policy == ExecPolicy(True)
    assert callable(seen[0].http_get)
