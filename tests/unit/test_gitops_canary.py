"""0.28.0: ``gitops enable --canary`` tests the new controller before it replaces the live one.

Two halves, no cluster: the in-pod dry run (``gitops canary``) on local Git
sources with fake ports (nothing is built or deployed), and the
``gitops enable`` side against a fake Kubernetes API that records the
canary Job and returns its log.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.canary import DryPorts, dry_run, summary_lines
from piceli.infra import CompositionError
from piceli.k8s.cli import app as cli
from tests.unit.infra.test_controller import world

__all__ = ["world"]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "composition"
IMAGE = "ghcr.io/example/piceli@sha256:" + "1" * 64
PLAN_HASH = "sha256:" + "e" * 64


# ------------------------------------------------------------- the dry run (in the pod)


def _planned(changes: int) -> Any:
    def plan(pipeline: Any, name: str, **_kw: Any) -> dict[str, Any]:
        rows = [{"operation": "update", "kind": "Deployment", "name": f"d{i}"}
                for i in range(changes)]  # fmt: skip
        return {
            "state": "planned",
            "plan_hash": PLAN_HASH,
            "deploy": {"stages": {"plan": {"changes": rows}}},
        }

    return plan


def _dry(world: dict[str, Any], planner: Any, known: Any = None) -> dict[str, Any]:
    controller = world["controller"]
    return dry_run(controller, DryPorts(world["ports"], planner, known=known or {}))


def test_the_dry_run_imports_plans_and_counts_builds_without_doing_them(
    world: dict[str, Any],
) -> None:
    body = _dry(world, _planned(3))
    assert body["state"] == "ok" and body["failure"] is None
    main = body["envs"]["main"]
    assert main["state"] == "ok" and main["changes"] == 3
    assert sorted(main["builds"]) == ["api", "catalog", "web"]
    # Nothing was built, mirrored or deployed by the real ports.
    ports = world["ports"]
    assert ports.builder.built == [] and ports.builder.mirrored == []
    assert ports.deployed == []
    assert "main: imports, plans 3 changes, builds 3 components: ok" in (
        summary_lines(body)
    )
    # A tag environment whose tag is not pushed yet waits, as the live one does.
    assert body["envs"]["rc"] == {
        "state": "skipped",
        "why": "waiting for a ref of shop",
    }
    assert "rc: not checked (waiting for a ref of shop)" in summary_lines(body)


def test_a_known_image_is_not_counted_as_a_build(world: dict[str, Any]) -> None:
    first = _dry(world, _planned(0))
    digests = first["envs"]["main"]["digests"]
    known = {("web", digests["web"]): ("registry.example:5000/web@sha256:" + "a" * 64,
                                       "sha256:" + "a" * 64)}  # fmt: skip
    world["controller"].state["envs"] = {}
    body = _dry(world, _planned(1), known)
    assert sorted(body["envs"]["main"]["builds"]) == ["api", "catalog"]


def test_a_refused_plan_fails_the_environment_with_its_code(
    world: dict[str, Any],
) -> None:
    def refuse(pipeline: Any, name: str, **_kw: Any) -> Any:
        raise CompositionError(
            "composition-invalid", "versioned Secret db-password: generated"
        )

    body = _dry(world, refuse)
    assert body["state"] == "failed"
    assert body["failure"] == {
        "env": "main",
        "stage": "plan",
        "reason": "composition-invalid",
        "detail": "versioned Secret db-password: generated",
    }
    assert any(
        line.startswith("main: plan failed (composition-invalid)")
        for line in summary_lines(body)
    )


def test_a_composition_that_does_not_import_fails_first(
    world: dict[str, Any],
) -> None:
    """The incident: the followed branch's head does not import on the new version."""
    controller = world["controller"]

    class Settings:
        name, branch, entry = "shop", "main", "infra.py"

    class Repo:
        settings = Settings()
        commit = None
        checkout = None

        def load(self, commit: str) -> Any:
            raise CompositionError(
                "composition-invalid",
                "importing infra.py failed: TypeError: Environment() got an "
                "unexpected keyword argument 'rollout_window'",
            )

    controller.repo = Repo()
    controller._composition = None
    body = _dry(world, _planned(1))
    assert body["state"] == "failed"
    assert body["composition"]["state"] == "failed"
    assert body["failure"]["stage"] == "import"
    assert body["failure"]["reason"] == "composition-invalid"
    assert "TypeError" in body["failure"]["detail"]
    assert summary_lines(body)[0].startswith("composition: does not import at ")


# ------------------------------------------------------------- gitops enable --canary


class FakePlan:
    changes = True
    plan_hash = PLAN_HASH
    action = "enable"
    objects: list[Any] = []

    def to_dict(self) -> dict[str, Any]:
        return {"plan_hash": self.plan_hash}


def _result(state: str = "ok", **failure: Any) -> dict[str, Any]:
    ok = state == "ok"
    return {
        "schema": "piceli.gitops-canary.v1",
        "version": "0.28.0",
        "state": state,
        "composition": {"state": "imports" if ok else "failed", "commit": "b" * 40},
        "envs": {
            "main": {"state": "ok", "changes": 3, "builds": ["api", "web"]}
        } if ok else {},
        "failure": None if ok else {"env": None, "stage": "import", **failure},
    }  # fmt: skip


class FakeApi:
    """Records every call; the canary Job ends with ``outcome`` (or never)."""

    client = object()

    def __init__(self, log: str | None, *, live: bool = True, ends: bool = True):
        self.log = log
        self.live = live
        self.ends = ends
        self.calls: list[tuple[str, str]] = []
        self.created: dict[str, Any] = {}

    def call(self, path: str, method: str, body: Any = None, *_a: Any, **_k: Any):
        self.calls.append((method, path))
        if method == "POST":
            self.created[body["kind"]] = body
            return body
        if method == "DELETE":
            return {}
        if path.endswith("/deployments/piceli-gitops"):
            return {"metadata": {"name": "piceli-gitops"}} if self.live else None
        if "/jobs/" in path:
            return {"status": {"succeeded": 1} if self.ends else {"active": 1}}
        if path.split("?")[0].endswith("/pods"):
            return {"items": [{"metadata": {"name": "piceli-gitops-canary-x1"},
                               "status": {"phase": "Succeeded"}}]}  # fmt: skip
        return None

    def text(self, path: str, query: Any = ()) -> str | None:
        self.calls.append(("LOG", path))
        return self.log


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    import piceli.gitops.install as install
    import piceli.k8s.cli.gitops as gitops

    holder: dict[str, Any] = {"executed": 0}

    @contextmanager
    def api(*_args: Any, **_kw: Any) -> Iterator[Any]:
        yield holder["api"]

    def execute(*_a: Any, **_k: Any) -> list[Any]:
        holder["executed"] += 1
        return []

    monkeypatch.setattr(gitops, "_api", api)
    monkeypatch.setattr(install, "plan_objects", lambda *a, **k: FakePlan())
    monkeypatch.setattr(install, "execute", execute)
    monkeypatch.setattr("piceli.gitops.canary.time.sleep", lambda _s: None)
    yield holder


def _enable(*extra: str) -> Any:
    return CliRunner().invoke(cli, [
        "gitops", "enable", str(EXAMPLE / "infra.py"), "--image", IMAGE,
        "--kubeconfig", "kc", "--context", "c", "--approve", PLAN_HASH,
        "--canary", *extra,
    ])  # fmt: skip


def _deleted(api: FakeApi) -> set[str]:
    return {path.rsplit("/", 1)[-1] + ":" + path.split("/")[-2]
            for method, path in api.calls if method == "DELETE"}  # fmt: skip


def test_a_passing_canary_runs_the_new_image_then_enables(
    cluster: dict[str, Any],
) -> None:
    api = cluster["api"] = FakeApi("starting\n" + json.dumps(_result()) + "\n")
    result = _enable()
    assert result.exit_code == 0, result.output
    assert "main: imports, plans 3 changes, builds 2 components: ok" in result.stderr
    assert "canary: ok" in result.stderr
    assert cluster["executed"] == 1
    body = json.loads(result.stdout)
    assert body["state"] == "enabled" and body["canary"]["state"] == "ok"
    job = api.created["Job"]
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == IMAGE
    assert container["args"][:2] == ["gitops", "canary"]
    assert pod["serviceAccountName"] == "piceli-gitops"
    assert pod["restartPolicy"] == "Never"
    volumes = {v["name"]: v for v in pod["volumes"]}
    # Never the live state volume nor the live config: scratch and its own copy.
    assert volumes["state"] == {"name": "state", "emptyDir": {}}
    assert volumes["config"]["configMap"]["name"] == "piceli-gitops-canary"
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "piceli-gitops-canary"
    assert job["spec"]["backoffLimit"] == 0
    assert api.created["ConfigMap"]["data"]["config.json"]
    assert _deleted(api) == {
        "piceli-gitops-canary:jobs",
        "piceli-gitops-canary:configmaps",
    }


def test_a_composition_that_does_not_import_on_the_canary_refuses_enable(
    cluster: dict[str, Any],
) -> None:
    detail = "importing infra.py failed: TypeError: unexpected keyword"
    failed = _result("failed", reason="composition-invalid", detail=detail)
    failed["composition"]["detail"] = detail
    failed["composition"]["reason"] = "composition-invalid"
    api = cluster["api"] = FakeApi(json.dumps(failed))
    result = _enable()
    assert result.exit_code == 2, result.output
    assert cluster["executed"] == 0  # the live controller is untouched
    body = json.loads(result.stdout)
    assert body["reason"] == "gitops-canary-failed"
    assert "composition-invalid" in body["message"] and "TypeError" in body["message"]
    assert body["canary"]["state"] == "failed"
    assert "piceli-gitops-canary:jobs" in _deleted(api)

    # --force replaces it anyway.
    api = cluster["api"] = FakeApi(json.dumps(failed))
    result = _enable("--force")
    assert result.exit_code == 0, result.output
    assert cluster["executed"] == 1
    assert "--force" in result.stderr
    assert json.loads(result.stdout)["canary"]["state"] == "failed"


def test_an_image_without_the_canary_command_or_a_timeout_fails_and_cleans_up(
    cluster: dict[str, Any],
) -> None:
    api = cluster["api"] = FakeApi("Error: No such command 'canary'.\n")
    result = _enable()
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["canary"]["failure"]["reason"] == "gitops-canary-failed"
    assert "No such command" in body["message"]
    assert len(_deleted(api)) == 2

    api = cluster["api"] = FakeApi(None, ends=False)
    result = _enable("--timeout", "0")
    assert result.exit_code == 2, result.output
    assert "timed out" in json.loads(result.stdout)["message"]
    assert len(_deleted(api)) == 2


def test_the_canary_is_removed_when_the_command_crashes(
    cluster: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = cluster["api"] = FakeApi(None, ends=False)

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("piceli.gitops.canary.time.sleep", interrupted)
    result = _enable()
    assert result.exit_code != 0
    assert cluster["executed"] == 0
    assert len(_deleted(api)) == 2


def test_no_live_controller_means_nothing_to_test(cluster: dict[str, Any]) -> None:
    api = cluster["api"] = FakeApi(None, live=False)
    result = _enable()
    assert result.exit_code == 0, result.output
    assert "no controller to replace" in result.stderr
    assert json.loads(result.stdout)["canary"]["state"] == "skipped"
    assert not [call for call in api.calls if call[0] in {"POST", "DELETE"}]


def test_canary_is_for_a_composition_controller(cluster: dict[str, Any]) -> None:
    result = CliRunner().invoke(cli, [
        "gitops", "enable", "pipeline.py:PIPELINE", "--image", IMAGE,
        "--repo", "https://git.example.org/shop.git", "--kubeconfig", "kc",
        "--context", "c", "--canary",
    ])  # fmt: skip
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == "gitops-config-invalid"


def test_the_canary_command_tests_only_a_composition_controller(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"pipeline": "pipeline.py:PIPELINE"}))
    result = CliRunner().invoke(cli, [
        "gitops", "canary", "--config", str(config),
        "--state-dir", str(tmp_path / "scratch"), "--service-account",
    ])  # fmt: skip
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == "gitops-config-invalid"
