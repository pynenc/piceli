"""``piceli gitops history``, ``show`` and ``compare`` (0.38.0).

They read the history document the UI reads (``piceli.gitops-history.v1``),
built here by the real :mod:`piceli.gitops.history` from run journals and
controller events (:func:`tests.gitops_history_fixture.sample_history`) and
published to a local controller's ``--state-dir``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops import releases
from piceli.gitops.state import DirectoryChannel
from piceli.k8s.cli import app
from tests.gitops_history_fixture import (
    ASSETS,
    LAYER_BIN,
    LAYER_PAGES,
    PRODUCT,
    PRODUCT_OLD,
    WEB,
    WEB_IMAGE,
    sample_history,
)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    DirectoryChannel(directory).publish_history(sample_history(directory))
    return directory


def cli(*argv: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, ["gitops", *argv, "--json"])
    out = result.stdout.strip().splitlines()
    return result.exit_code, json.loads(out[-1]) if out else {}, result.stderr


def test_history_lists_each_environment_newest_first_with_windows(
    state: Path,
) -> None:
    code, body, text = cli("history", "--state-dir", str(state))
    assert code == 0, text
    assert body["schema"] == "piceli.gitops-runs.v1"
    assert body["history_schema"] == "piceli.gitops-history.v1"
    assert set(body) >= {"generated_at", "truncated", "environments"}
    envs = {item["name"]: item["runs"] for item in body["environments"]}
    assert set(envs) == {"main", "wp-login"}
    main = envs["main"]
    starts = [run["started_at"] for run in main]
    assert starts == sorted(starts, reverse=True) and len(main) == 3
    deployed = main[1]
    assert deployed["trigger"] == "push product/main"
    assert deployed["approved_by"]["via"] == "cli"
    assert deployed["sources"]["product"]["commit"] == PRODUCT
    web = next(c for c in deployed["components"] if c["name"] == "web")
    assert web["digest"] == WEB and web["built"] is True
    assert deployed["window"]["seconds"] == 120.0
    assert deployed["release_window"]["finished_at"] == deployed["finished_at"]
    assert deployed["checks"]["results"]
    assert "main:" in text and "push product/main" in text
    code, limited, _ = cli("history", "main", "--limit", "1", "--state-dir", str(state))
    assert code == 0
    assert [len(e["runs"]) for e in limited["environments"]] == [1]


def test_show_prints_one_run_and_its_log_tail(state: Path) -> None:
    run_id = releases.history(DirectoryChannel(state).read_history(), "wp-login")[
        "environments"
    ][0]["runs"][0]["id"]
    code, body, text = cli("show", run_id[:12], "--logs", "--state-dir", str(state))
    assert code == 0, text
    assert body["schema"] == "piceli.gitops-run.v1"
    assert body["env"] == "wp-login" and body["run"]["id"] == run_id
    assert body["run"]["state"] == "failed"
    (log,) = body["logs"]
    assert log["source"] == "failure" and "npm ERR!" in log["text"]
    assert "piceli logs" in body["logs_note"]
    assert "npm ERR!" in text
    code, plain, _ = cli("show", run_id, "--state-dir", str(state))
    assert code == 0 and "logs" not in plain


def _deployed(state: Path) -> dict[str, Any]:
    document = DirectoryChannel(state).read_history()
    runs = releases.history(document, "main")["environments"][0]["runs"]
    return next(run for run in runs if run["trigger"] == "push product/main")


def test_history_components_carry_their_image_and_layers(state: Path) -> None:
    code, body, text = cli("history", "main", "--state-dir", str(state))
    assert code == 0, text
    run = _deployed(state)
    web, worker = (
        next(c for c in run["components"] if c["name"] == name)
        for name in ("web", "worker")
    )
    assert web["image"] == {"repository": "shop/web", "reference": WEB_IMAGE}
    assert web["layers"] == [
        {"mapping": "/usr/local/bin/web", "digest": LAYER_BIN},
        {"mapping": "/srv/pages", "digest": LAYER_PAGES},
    ]
    # Never invented: a component the run recorded no image for has none.
    assert "image" not in worker and "layers" not in worker
    # The run's own ``images`` keeps its shape (the layers are the component's).
    assert "layers" not in run["images"]["web"]
    assert run["images"]["web"]["reference"] == WEB_IMAGE


def test_show_artifacts_prints_what_the_run_built(state: Path) -> None:
    run_id = _deployed(state)["id"]
    code, body, text = cli("show", run_id, "--artifacts", "--state-dir", str(state))
    assert code == 0, text
    assert body["schema"] == "piceli.gitops-artifacts.v1"
    assert set(body) == {"schema", "env", "run", "state", "commits", "components"}
    assert body["env"] == "main" and body["run"] == run_id
    assert body["state"] == "deployed"
    assert body["commits"] == {"assets": ASSETS, "product": PRODUCT}
    web = next(c for c in body["components"] if c["name"] == "web")
    assert set(web) == {
        "name", "source", "commit", "digest", "image", "platforms", "layers",
        "provenance",
    }  # fmt: skip
    assert web["source"] == "product" and web["commit"] == PRODUCT
    assert web["digest"] == WEB and web["image"]["reference"] == WEB_IMAGE
    assert [layer["mapping"] for layer in web["layers"]] == [
        "/usr/local/bin/web",
        "/srv/pages",
    ]
    media = next(c for c in body["components"] if c["name"] == "media")
    assert media["image"] is None and media["layers"] == []
    assert WEB_IMAGE in text and LAYER_BIN in text and "/srv/pages" in text
    code, other, _ = cli(
        "show", run_id, "--artifacts", "--env", "main", "--state-dir", str(state)
    )
    assert code == 0 and other == body
    code, error, _ = cli("show", "nope", "--artifacts", "--state-dir", str(state))
    assert code == 2 and error["reason"] == "gitops-run-unknown"


def test_artifacts_helper_shows_any_run_and_none_when_unknown(state: Path) -> None:
    document = DirectoryChannel(state).read_history()
    failed = releases.history(document, "wp-login")["environments"][0]["runs"][0]
    body = releases.artifacts(document, failed["id"])
    assert body is not None and body["state"] == "failed"
    assert body["env"] == "wp-login"
    (web,) = body["components"]
    assert web["digest"] is None and web["image"] is None
    assert web["platforms"] == {} and web["layers"] == []
    assert releases.artifacts(document, "nope") is None
    assert releases.artifacts({}, "x") is None
    assert releases.artifacts(document, failed["id"], env="main") is None


def test_layers_and_image_are_bounded() -> None:
    from piceli.gitops import history

    assert history._image_of("registry.example.org/a/b:1.0@sha256:" + "a" * 64) == {
        "repository": "a/b",
        "reference": "registry.example.org/a/b:1.0@sha256:" + "a" * 64,
    }
    assert history._image_of("registry.example.org/a/b:1.0") is None
    assert history._image_of(None) is None
    many = [{"mapping": f"/m/{n}" + "x" * 500, "digest": "sha256:" + "a" * 64}
            for n in range(40)]  # fmt: skip
    kept = history._layers([*many, {"mapping": "", "digest": "sha256:x"}])
    assert len(kept) == history.MAX_LAYERS
    assert all(len(item["mapping"]) <= 200 for item in kept)


def test_compare_defaults_to_the_running_release_and_the_one_before(
    state: Path,
) -> None:
    code, body, text = cli("compare", "main", "--state-dir", str(state))
    assert code == 0, text
    assert body["schema"] == "piceli.gitops-compare.v1"
    assert set(body) == {
        "schema", "env", "before", "after", "sources", "components",
        "images_changed", "resources", "fields",
    }  # fmt: skip
    assert body["before"]["release"] == "shop-1a2b3c4d5e6f"
    assert body["after"]["release"] == "shop-2b3c4d5e6f70"
    assert (
        body["before"]["window"]["started_at"] < body["after"]["window"]["started_at"]
    )
    sources = {s["name"]: s for s in body["sources"]}
    assert sources["product"]["before"]["commit"] == PRODUCT_OLD
    assert sources["product"]["after"]["commit"] == PRODUCT
    assert sources["product"]["changed"] is True
    assert sources["assets"]["after"]["commit"] == ASSETS
    assert sources["assets"]["changed"] is False
    assert "web" in body["images_changed"]
    assert body["resources"]["changed"] == [{"kind": "Deployment", "name": "web"}]
    assert body["resources"]["added"] == [] and body["resources"]["removed"] == []
    assert body["fields"]["available"] is False
    assert "images changed" in text


def test_compare_of_named_runs_and_the_object_net() -> None:
    def entry(n: int, changes: list[tuple[str, str, str]]) -> dict[str, Any]:
        return {
            "id": f"r{n}", "kind": "run", "state": "deployed", "action": "deployed",
            "started_at": f"2026-10-01T0{n}:00:00Z",
            "finished_at": f"2026-10-01T0{n}:05:00Z",
            "components": [{"name": "web", "digest": f"sha256:{n}"}],
            "plan": {"changes": [{"operation": o, "kind": k, "name": m}
                                 for o, k, m in changes]},
        }  # fmt: skip

    document = {
        "schema": "piceli.gitops-history.v1",
        "envs": {
            "main": {
                "runs": [
                    entry(3, [("delete", "Job", "seed"), ("update", "Service", "web")]),
                    entry(2, [("create", "Job", "seed"), ("create", "ConfigMap", "x")]),
                    entry(1, [("create", "Deployment", "web")]),
                ]
            }
        },
    }
    body = releases.compare(document, "main", "r1", "r3")
    assert body["resources"]["added"] == [{"kind": "ConfigMap", "name": "x"}]
    assert body["resources"]["changed"] == [{"kind": "Service", "name": "web"}]
    assert body["resources"]["removed"] == []
    assert body["images_changed"] == ["web"]
    assert releases.compare(document, "main", "r3", "r1")["before"]["id"] == "r1"
    assert releases.compare(document, "main", "r2")["after"]["id"] == "r3"
    assert body["after"]["release_window"]["seconds"] == 300.0


def test_refusals_name_their_codes(tmp_path: Path, state: Path) -> None:
    code, body, _ = cli("history", "--state-dir", str(tmp_path))
    assert code == 2 and body["reason"] == "gitops-history-unavailable"
    code, body, _ = cli("history", "staging", "--state-dir", str(state))
    assert code == 2 and body["reason"] == "gitops-run-unknown"
    code, body, _ = cli("show", "nope", "--state-dir", str(state))
    assert code == 2 and body["reason"] == "gitops-run-unknown"
    code, body, _ = cli("compare", "wp-login", "--state-dir", str(state))
    assert code == 2 and body["reason"] == "gitops-run-unknown"
