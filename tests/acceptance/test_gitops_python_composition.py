"""``piceli gitops enable infra.py`` of a Python composition (fake API).

The composition deploys a Pipeline (``Environment(pipeline=...)``): the
controller config records the composition's own repository (followed at
``--main-branch``, imported at its commit) besides the summary of every
environment, so the install plan hash covers both.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.install import CONFIG_MAP
from piceli.infra.controller import CONFIG_SCHEMA, CompositionConfig
from piceli.k8s.cli import app
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig

IMAGE = "registry.example.com/piceli@sha256:" + "a" * 64
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "python_composition"


@pytest.fixture
def cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeAPI, Path]]:
    monkeypatch.setattr(sys, "dont_write_bytecode", True)  # nothing in examples/
    api = FakeAPI(namespace="piceli-system")
    with fake_cluster(api) as running:
        yield api, write_kubeconfig(running.url, tmp_path / "kubeconfig")


def _enable(kubeconfig: Path, root: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "gitops", "enable", "infra.py", "--root", str(root), "--image", IMAGE,
            "--credentials-secret", "git-token",
            "--kubeconfig", str(kubeconfig), "--context", "fake",
            "--transport", "loopback-http", *extra,
        ],
    )  # fmt: skip


def test_enable_records_the_composition_repository(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    repo = ("--repo", "https://example.com/infra.git", "--main-branch", "release")
    planned = _enable(kubeconfig, EXAMPLE / "infra", *repo)
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    controller = body["controller"]
    assert controller["schema"] == CONFIG_SCHEMA
    assert controller["repo"] == {
        "name": "infra",  # the Source with the same URL keeps its name
        "url": "https://example.com/infra.git",
        "branch": "release",
        "entry": "infra.py",
    }
    environments = controller["composition"]["environments"]
    assert [e["name"] for e in environments] == ["main", "rc", "branches"]
    assert environments[0]["pipeline"]["app"] == "example"
    assert controller["composition"]["cluster"]["registry"]["mirror"]
    assert "follows branch release, imports infra.py" in planned.stderr
    done = _enable(kubeconfig, EXAMPLE / "infra", *repo, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    stored = api.objects[("ConfigMap", CONFIG_MAP)]["data"]["config.json"]
    config = CompositionConfig.from_dict(json.loads(stored))
    assert config.repo is not None and config.repo["branch"] == "release"
    assert [s.key for s in config.sources] == ["api", "infra", "worker"]
    assert config.registry.host == "piceli-registry.piceli-system.svc:5000"
    again = _enable(kubeconfig, EXAMPLE / "infra", *repo)
    assert json.loads(again.stdout)["state"] == "unchanged"


def test_enable_needs_the_repository_of_a_pipeline_composition(
    cluster: tuple[FakeAPI, Path], tmp_path: Path
) -> None:
    _, kubeconfig = cluster
    copy = tmp_path / "infra"  # not a Git clone: no origin remote to follow
    shutil.copytree(EXAMPLE / "infra", copy)
    result = _enable(kubeconfig, copy)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == "composition-invalid"


def test_enable_refuses_a_context_of_an_unfollowed_source(
    cluster: tuple[FakeAPI, Path], tmp_path: Path
) -> None:
    _, kubeconfig = cluster
    copy = tmp_path / "infra"
    shutil.copytree(EXAMPLE / "infra", copy)
    spec = copy / "host-build.toml"
    spec.write_text(spec.read_text().replace('source = "worker"', 'source = "docs"'))
    result = _enable(kubeconfig, copy, "--repo", "https://example.com/infra.git")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == "composition-invalid"
    assert "'docs'" in result.stderr
