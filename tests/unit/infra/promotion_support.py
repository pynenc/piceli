"""A flake repository, its input's repository and an environment's history
for the promotion gate and ``piceli promote`` tests (all in ``tmp_path``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.infra.git_support import ENV, Repo, git
from tests.unit.infra.test_nix_releases import PUBLIC, SECRET

__all__ = ["PUBLIC", "SECRET"]

COMPOSITION = """
import os

from piceli.envs import Environment
from piceli.infra import (
    Component, NixCache, NixHost, NixRelease, Promotion, Source, Tag,
)

hosts = Source(os.environ["PROMO_HOSTS_URL"], name="hosts")
app = Source(os.environ["PROMO_APP_URL"], name="app")
api = Component("api", source=app)
cache = NixCache(os.environ["PROMO_PUBLIC"],
                 url="file://" + os.environ["PROMO_CACHE"])
web = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
              follow=Tag("web-v*"), cache=cache)
web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=True,
                         gate=Promotion(input="app", environment="main"))
environments = [Environment("main", follow={app: "main"}, namespace="shop-main")]
"""


def lock(app_url: str, rev: str) -> str:
    """A ``flake.lock`` that pins the input ``app`` (a Git input) at ``rev``."""
    return (
        json.dumps(
            {
                "nodes": {
                    "app": {
                        "flake": False,
                        "locked": {
                            "lastModified": 1,
                            "narHash": "sha256-" + "A" * 43 + "=",
                            "ref": "refs/heads/main",
                            "rev": rev,
                            "revCount": 1,
                            "type": "git",
                            "url": f"file://{app_url}",
                        },
                        "original": {"type": "git", "url": f"file://{app_url}"},
                    },
                    "root": {"inputs": {"app": "app"}},
                },
                "root": "root",
                "version": 7,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def flake_nix(app_url: str) -> str:
    return (
        "{\n"
        f'  inputs.app = {{ url = "git+file://{app_url}"; flake = false; }};\n'
        "  outputs = { self, app }: { };\n"
        "}\n"
    )


def run(
    commit: str,
    run_id: str,
    at: str,
    *,
    state: str = "deployed",
    passed: bool = True,
    rollback: bool = False,
    checks: int = 1,
    source: str = "app",
) -> dict[str, Any]:
    """One run of the environment's history (``piceli.gitops-history.v1``)."""
    results = [{"name": f"c{i}", "passed": passed} for i in range(checks)]
    return {
        "kind": "run",
        "id": run_id,
        "run_id": run_id,
        "env": "main",
        "state": state,
        "started_at": at,
        "finished_at": at,
        "sources": {source: {"commit": commit, "ref": "refs/heads/main"}},
        "checks": {
            "passed": passed and bool(checks),
            "results": results,
            **({"rollback": {"state": "ready"}} if rollback else {}),
        }
        if checks is not None
        else None,
    }


def history(*runs: dict[str, Any], env: str = "main") -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-history.v1",
        "generated_at": "2026-10-23T10:00:00Z",
        "envs": {env: {"runs": list(runs)}},
        "truncated": False,
    }


def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The input ``app`` (commits a, b, c), the flake ``hosts`` pinning
    ``a`` (tag ``web-v1.0.0``) with a working clone, the composition and
    an empty controller state directory."""
    app = Repo(tmp_path / "remotes", "app", {"main.py": "print(1)\n"})
    a = git(app.work, "rev-parse", "HEAD")
    b = app.commit({"main.py": "print(2)\n", "bin/run": "#!/bin/sh\n"})
    git(app.work, "update-index", "--chmod=+x", "bin/run")
    git(app.work, "commit", "-q", "-m", "exec")
    git(app.work, "push", "-q", "origin", "main")
    c = git(app.work, "rev-parse", "HEAD")
    hosts = Repo(
        tmp_path / "remotes",
        "hosts",
        {"flake.nix": flake_nix(app.url), "flake.lock": lock(app.url, a)},
    )
    hosts.tag("web-v1.0.0")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", hosts.url, str(work))
    state = tmp_path / "state"
    state.mkdir()
    cache = tmp_path / "cache"
    for key in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):  # fmt: skip
        monkeypatch.setenv(key, ENV[key])
    for key, value in (
        ("PROMO_HOSTS_URL", hosts.url),
        ("PROMO_APP_URL", app.url),
        ("PROMO_PUBLIC", PUBLIC),
        ("PROMO_CACHE", str(cache)),
    ):
        monkeypatch.setenv(key, value)
    (tmp_path / "infra.py").write_text(COMPOSITION)
    return {"app": app, "hosts": hosts, "work": work, "state": state,
            "cache": cache, "infra": tmp_path / "infra.py",
            "a": a, "b": b, "c": c}  # fmt: skip


def publish_history(state: Path, document: dict[str, Any]) -> None:
    (state / "history.json").write_text(json.dumps(document))
