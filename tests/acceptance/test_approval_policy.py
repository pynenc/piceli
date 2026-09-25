"""Acceptance: ``--approve-if-policy`` applies only plans inside the owner's policy (M5.9).

``release apply --approve-if-policy`` on a ``release.toml`` and ``deploy
--approve-if-policy`` on a pipeline, against the fake API. Proves that a plan
outside the policy is refused with the human approval instructions, that the
policy is part of the hash, that no command-line flag can widen it, and that
the secret and exec rules are unchanged.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.k8s.cli.release import app as release_cli
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy
from tests.acceptance.test_release_cli import release_env  # noqa: F401 (fixture)
from tests.unit.release.test_exec_credentials import (
    exec_user,
    kubeconfig,
    make_plugin,
    runs,
)

POLICY = (
    'auto_approve = { allow = ["create", "apply", "no-op"], '
    'deny = ["delete", "replace", "adopt", "cluster_scoped"], max_objects = 10 }'
)

EXTRA = """
    if ctx.values.get("extra") == "yes":
        extra = ResourceIntent.from_manifest(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("extra"),
             "data": {"on": "yes"}}
        )
        return DeploymentComposition((
            DeploymentComponent("config", (config, token, extra)),
            DeploymentComponent("worker", (worker,), dependencies=("config",)),
        ))
    return DeploymentComposition(("""


def _spec(tmp_path: Path, *, policy: str | None = POLICY, extra: str = "no") -> None:
    """The release_env spec with pruning, an optional ConfigMap and the policy."""
    compose = tmp_path / "compose.py"
    text = compose.read_text()
    if "extra" not in text:
        compose.write_text(text.replace("    return DeploymentComposition((", EXTRA, 1))
    spec = tmp_path / "release.toml"
    text = spec.read_text()
    if "prune = true" not in text:
        text = text.replace('state_dir = "state"', 'state_dir = "state"\nprune = true')
    lines = [line for line in text.splitlines() if not line.startswith("auto_approve")]
    lines = [line for line in lines if not line.startswith("extra =")]
    text = "\n".join(lines) + "\n"
    if policy is not None:
        text = text.replace("prune = true", f"prune = true\n{policy}")
    text = text.replace('mode = "blue"', f'mode = "blue"\nextra = "{extra}"')
    spec.write_text(text)


def _release(tmp_path: Path, *args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(
        release_cli, [*args, "--spec", str(tmp_path / "release.toml")]
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result


def _plans(tmp_path: Path) -> set[str]:
    return {path.stem for path in (tmp_path / "state" / "plans").glob("*.json")}


def test_release_policy_applies_inside_and_refuses_a_delete(release_env) -> None:  # noqa: F811
    api, tmp_path = release_env
    _spec(tmp_path, extra="yes")
    code, payload, result = _release(tmp_path, "apply", "--approve-if-policy")
    assert code == 0, result.stdout + result.stderr
    assert payload["approved_by"] == "policy"
    assert payload["release_state"] == "ready"
    assert ("ConfigMap", "extra") in api.objects
    assert "inside the owner's approval policy" in result.stderr
    # Secrets are never printed, also on the policy path.
    password = base64.b64decode(
        api.objects[("Secret", "credential")]["data"]["password"]
    ).decode()
    assert password not in result.stdout + result.stderr

    # Dropping the ConfigMap prunes it: a delete is outside every policy.
    _spec(tmp_path, extra="no")
    code, payload, result = _release(tmp_path, "apply", "--approve-if-policy")
    assert code == 3, result.stdout + result.stderr
    assert payload["state"] == "approval-required"
    assert payload["reason"] == "approval-policy-exceeded"
    assert payload["policy"]["violations"] == ["delete ConfigMap/extra"]
    assert ("ConfigMap", "extra") in api.objects  # nothing was applied
    # The normal human instructions: the stored plan's hash to approve.
    plan_hash = payload["plan_hash"]
    assert f"--approve {plan_hash}" in result.stderr
    assert plan_hash in _plans(tmp_path)
    code, payload, result = _release(tmp_path, "apply", "--approve", plan_hash)
    assert code == 0, result.stdout + result.stderr
    assert ("ConfigMap", "extra") not in api.objects
    assert "approved_by" not in payload


def test_release_policy_change_changes_the_plan_hash(release_env) -> None:  # noqa: F811
    api, tmp_path = release_env
    _spec(tmp_path)
    code, first, _ = _release(tmp_path, "plan")
    assert code == 0
    _spec(tmp_path, policy=POLICY.replace("max_objects = 10", "max_objects = 11"))
    code, second, _ = _release(tmp_path, "plan")
    assert code == 0
    assert first["release"] == second["release"]  # same objects, same release
    assert first["plan_hash"] != second["plan_hash"]
    _spec(tmp_path, policy=None)
    code, third, _ = _release(tmp_path, "plan")
    assert len({first["plan_hash"], second["plan_hash"], third["plan_hash"]}) == 3
    assert ("Deployment", "worker") not in api.objects


def test_release_without_a_policy_refuses_before_planning(release_env) -> None:  # noqa: F811
    api, tmp_path = release_env
    _spec(tmp_path, policy=None)
    code, payload, _ = _release(tmp_path, "apply", "--approve-if-policy")
    assert code == 2
    assert payload["reason"] == "approval-policy-missing"
    assert not (tmp_path / "state" / "plans").exists() or not _plans(tmp_path)
    assert ("Deployment", "worker") not in api.objects


@pytest.mark.parametrize(
    "flags",
    [
        ["--approve", "0" * 64],
        ["--auto-approve"],
        ["--adopt", "ConfigMap/settings"],
        ["--replace", "ConfigMap/settings"],
        ["--adopt-all-desired"],
        ["--rotate", "password"],
    ],
)
def test_no_flag_can_add_to_what_the_policy_applies(release_env, flags) -> None:  # noqa: F811
    api, tmp_path = release_env
    _spec(tmp_path)
    code, payload, _ = _release(tmp_path, "apply", "--approve-if-policy", *flags)
    assert code == 2
    assert payload["reason"] == "approve-if-policy-flags-conflict"
    assert ("Deployment", "worker") not in api.objects


def test_the_cli_has_no_way_to_pass_or_widen_a_policy() -> None:
    """The policy comes only from the owner's pipeline or spec, never a flag."""
    result = CliRunner().invoke(cli, ["help-json"])

    def walk(node: dict[str, Any]) -> Any:
        yield node
        for child in node.get("commands", ()):
            yield from walk(child)

    commands = {node["path"]: node for node in walk(json.loads(result.stdout)["root"])}
    for name in ("deploy", "release apply"):
        params = commands[name]["params"]
        flags = [flag for param in params for flag in param.get("flags", ())]
        policy_flags = [flag for flag in flags if "polic" in flag or "allow" in flag]
        assert policy_flags == ["--approve-if-policy"], (name, policy_flags)
        [param] = [p for p in params if "--approve-if-policy" in p.get("flags", ())]
        assert param["type"] == "boolean"  # takes no value
    for args in (
        ["deploy", "x.py:pipeline", "--approve-if-policy=delete"],
        ["deploy", "x.py:pipeline", "--policy", "allow=delete"],
        ["release", "apply", "--spec", "r.toml", "--auto-approve-policy", "x"],
    ):
        assert CliRunner().invoke(cli, args).exit_code == 2


def test_release_exec_rule_is_unaffected(tmp_path: Path) -> None:
    """--approve-if-policy never lets an exec credential plugin run."""
    from piceli.testing import TARGET, serve

    plugin = make_plugin(tmp_path / "bin")
    with serve() as (_api, url):
        kc = kubeconfig(tmp_path / "kc", url, exec_user(str(plugin)))
        (tmp_path / "deploy.py").write_text(
            textwrap.dedent(
                f"""
                from piceli import App, ApprovalPolicy, Pipeline, Target

                app = App("shop")
                app.deployment("web", image="docker.io/library/redis:7@sha256:{"d" * 64}")
                pipeline = Pipeline(
                    app,
                    Target.kubeconfig({str(kc)!r}, context="explicit",
                                      namespace="{TARGET.namespace}",
                                      transport="loopback-http"),
                    state_dir="state",
                    auto_approve=ApprovalPolicy(),
                )
                """
            )
        )
        entry = f"{tmp_path / 'deploy.py'}:pipeline"
        for args in (
            ["release", "apply", "--spec", entry, "--approve-if-policy"],
            ["deploy", entry, "--approve-if-policy"],
        ):
            result = CliRunner().invoke(cli, args)
            assert result.exit_code == 2, result.stdout + result.stderr
            lines = [json.loads(x) for x in result.stdout.splitlines() if x.strip()]
            assert lines[-1]["reason"] == "exec-auth-not-allowed"
        assert runs(plugin) == 0


# ------------------------------------------------------------ piceli deploy


def _policy(tmp_path: Path, policy: str | None) -> None:
    for name in [n for n in sys.modules if n.startswith("_piceli_render_")]:
        del sys.modules[name]  # the CLI caches a pipeline file by path
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)  # same size+mtime
    app = tmp_path / "app.py"
    text = app.read_text()
    text = text.replace(
        "from piceli import App,", "from piceli import ApprovalPolicy, App,", 1
    )
    marker = '    state_dir="state",\n'
    head, _, tail = text.partition(marker)
    tail = "\n".join(
        line for line in tail.splitlines() if not line.startswith("    auto_approve=")
    )
    extra = f"    auto_approve={policy},\n" if policy else ""
    app.write_text(head + marker + extra + tail + "\n")


def test_deploy_policy_runs_inside_and_refuses_outside(shop) -> None:
    api, tmp_path = shop
    _policy(tmp_path, "ApprovalPolicy(max_objects=2)")
    code, events, result = deploy(tmp_path, "--approve-if-policy")
    # cache, web and the credentials Secret: three changes > max_objects.
    assert code == 3, result.stdout + result.stderr
    final = events[-1]
    assert (final["state"], final["reason"]) == (
        "approval-required",
        "approval-policy-exceeded",
    )
    assert final["policy"]["violations"] == ["max_objects: 3 changed objects > 2"]
    assert f"--approve {final['combined_hash']}" in result.stderr
    assert FakeBackend.calls == []  # nothing built, delivered or applied
    assert ("Deployment", "web") not in api.objects

    _policy(tmp_path, "ApprovalPolicy(max_objects=3)")
    code, events, result = deploy(tmp_path, "--approve-if-policy", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert events[-1]["approved_by"] == "policy"
    assert FakeBackend.calls == ["build", "deliver"]
    assert ("Deployment", "web") in api.objects
    runs_dir = sorted((tmp_path / "state" / "runs").glob("*.json"))
    assert json.loads(runs_dir[-1].read_text())["approved_by"] == "policy"
    # The run summary records who approved it (policy, not a human hash).
    summary = json.loads(Path(events[-1]["summary"]["json"]).read_text())
    assert summary["approved_by"] == "policy"
    markdown = Path(events[-1]["summary"]["markdown"]).read_text()
    assert "| Approved by | the owner's approval policy |" in markdown


def test_deploy_policy_is_part_of_the_combined_hash(shop) -> None:
    api, tmp_path = shop
    hashes = []
    for policy in (None, "ApprovalPolicy()", "ApprovalPolicy(max_objects=9)"):
        _policy(tmp_path, policy)
        code, events, _ = deploy(tmp_path, "--plan")
        assert code == 0
        hashes.append(events[-1]["combined_hash"])
    assert len(set(hashes)) == 3
    # The hash planned under one policy does not approve the pipeline under another.
    code, events, _ = deploy(tmp_path, "--approve", hashes[1])
    assert (code, events[-1]["reason"]) == (2, "pipeline-plan-changed")
    assert FakeBackend.calls == []


def test_deploy_policy_refusals(shop) -> None:
    api, tmp_path = shop
    code, events, _ = deploy(tmp_path, "--approve-if-policy")
    assert (code, events[-1]["reason"]) == (2, "approval-policy-missing")
    _policy(tmp_path, "ApprovalPolicy()")
    for flags in (
        ["--plan"],
        ["--approve", "0" * 64],
        ["--auto-approve"],
        ["--resume"],
    ):
        code, events, _ = deploy(tmp_path, "--approve-if-policy", *flags)
        assert (code, events[-1]["reason"]) == (2, "deploy-flags-conflict"), flags
    _policy(tmp_path, 'ApprovalPolicy(allow={"create", "delete"})')
    code, events, _ = deploy(tmp_path, "--plan")
    assert (code, events[-1]["reason"]) == (2, "approval-policy-invalid")
    assert FakeBackend.calls == []
