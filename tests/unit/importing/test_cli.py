"""``piceli import yaml`` end to end, and the refusals of both commands."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml
from typer.testing import CliRunner

from piceli.importing.sources import Selector, running_images
from piceli.k8s.cli import app
from tests.unit.importing import objects
from tests.unit.importing.objects import SECRET_VALUE

runner = CliRunner()
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "schemas"
        / "piceli-import-summary-v1.schema.json"
    ).read_text()
)


def _files(tmp_path: Path) -> Path:
    directory = tmp_path / "manifests"
    (directory / "nested").mkdir(parents=True, exist_ok=True)
    stack = objects.stack()
    (directory / "app.yaml").write_text(yaml.safe_dump_all(stack[:3]))
    (directory / "nested" / "more.yml").write_text(
        yaml.safe_dump({"apiVersion": "v1", "kind": "List", "items": stack[3:]})
    )
    (directory / "namespace.json").write_text(
        json.dumps(
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "shop"}}
        )
    )
    (directory / "README.md").write_text("ignored")
    return directory


def _invoke(*args: str):
    return runner.invoke(app, ["import", *args])


def test_yaml_prints_the_module(tmp_path: Path, ruff_clean) -> None:
    result = _invoke("yaml", str(_files(tmp_path)))
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith('"""Typed app for namespace shop, imported by')
    assert SECRET_VALUE not in result.stdout
    ruff_clean(result.stdout)
    assert "skipped Namespace/shop: cluster-scoped" in result.stderr


def test_yaml_out_writes_the_file_and_prints_a_summary(tmp_path: Path) -> None:
    out = tmp_path / "app.py"
    result = _invoke(
        "yaml", str(_files(tmp_path)), "--out", str(out), "--name", "store"
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    jsonschema.validate(summary, SCHEMA)
    assert summary["state"] == "imported" and summary["app"] == "store"
    assert summary["out"] == str(out)
    assert summary["module_sha256"]
    assert {item["as"] for item in summary["objects"]} == {"typed", "existing-claim"}
    assert summary["secret_inputs"] == [
        {"input": "cache-auth-password", "secret": "cache-auth", "key": "password"}
    ]
    assert 'App("store"' in out.read_text()
    again = _invoke("yaml", str(_files(tmp_path)), "--out", str(out))
    assert again.exit_code == 2
    assert json.loads(again.stdout) == {
        "state": "rejected",
        "reason": "import-output-refused",
    }
    forced = _invoke("yaml", str(_files(tmp_path)), "--out", str(out), "--force")
    assert forced.exit_code == 0, forced.output


def test_yaml_json_includes_the_module(tmp_path: Path) -> None:
    result = _invoke(
        "yaml", str(_files(tmp_path)), "--json", "--select", "Deployment/cache"
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    jsonschema.validate(summary, SCHEMA)
    assert summary["module"].startswith('"""')
    assert [(item["kind"], item["name"]) for item in summary["objects"]] == [
        ("Deployment", "cache")
    ]


def test_select_by_label(tmp_path: Path) -> None:
    result = _invoke("yaml", str(_files(tmp_path)), "--json", "--select", "app=cache")
    kinds = sorted(item["kind"] for item in json.loads(result.stdout)["objects"])
    assert kinds == ["Deployment", "Service"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["--select", "deployment/cache"], "import-select-invalid"),
        (["--select", "Deployment/none"], "import-nothing-selected"),
        (["--namespace", "other"], "import-manifests-invalid"),
        (["--name", "Not_A_Label"], "import-name-invalid"),
    ],
)
def test_yaml_refusals(tmp_path: Path, args: list[str], code: str) -> None:
    result = _invoke("yaml", str(_files(tmp_path)), *args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout) == {"state": "rejected", "reason": code}


def test_yaml_invalid_documents(tmp_path: Path) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "a.yaml").write_text("kind: ConfigMap\n")
    result = _invoke("yaml", str(directory))
    assert json.loads(result.stdout)["reason"] == "import-manifests-invalid"
    (directory / "a.yaml").write_text("{unclosed")
    result = _invoke("yaml", str(directory))
    assert json.loads(result.stdout)["reason"] == "import-manifests-invalid"
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _invoke("yaml", str(empty))
    assert json.loads(result.stdout)["reason"] == "import-nothing-selected"


def test_live_refuses_a_missing_kubeconfig_before_any_request(tmp_path: Path) -> None:
    result = _invoke(
        "live",
        "--kubeconfig",
        str(tmp_path / "missing"),
        "--context",
        "kind-shop",
        "--namespace",
        "shop",
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "state": "rejected",
        "reason": "import-target-invalid",
    }


def test_live_refuses_a_bad_selector_first(tmp_path: Path) -> None:
    result = _invoke(
        "live",
        "--kubeconfig",
        str(tmp_path / "missing"),
        "--context",
        "c",
        "--namespace",
        "shop",
        "--select",
        "nonsense",
    )
    assert json.loads(result.stdout)["reason"] == "import-select-invalid"


def test_selector_parsing() -> None:
    assert Selector.parse("Deployment/api") == Selector(kind="Deployment", name="api")
    assert Selector.parse("app.kubernetes.io/name=api") == Selector(
        label="app.kubernetes.io/name", value="api"
    )
    assert Selector.parse("tier=") == Selector(label="tier", value="")


def test_running_images_from_pods() -> None:
    pods = [
        {
            "metadata": {"labels": {"app": "cache", "pod-template-hash": "x"}},
            "status": {
                "containerStatuses": [
                    {
                        "name": "redis",
                        "imageID": "docker.io/library/redis@sha256:" + "a" * 64,
                    },
                    {
                        "name": "exporter",
                        "imageID": "docker-pullable://example.org/e@sha256:" + "b" * 64,
                    },
                ]
            },
        },
        {"metadata": {"labels": {"app": "other"}}, "status": {}},
    ]
    assert running_images([objects.deployment()], pods) == {
        ("cache", "redis"): "docker.io/library/redis@sha256:" + "a" * 64,
        ("cache", "exporter"): "example.org/e@sha256:" + "b" * 64,
    }
