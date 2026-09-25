"""``piceli render`` prints manifests without a cluster."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "release"


def _invoke(*args: str):
    return CliRunner().invoke(cli, ["render", *args])


def test_render_release_example_from_spec_as_yaml():
    result = _invoke("--spec", str(EXAMPLE / "release.toml"))
    assert result.exit_code == 0, result.output
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert [(d["kind"], d["metadata"]["name"]) for d in documents] == [
        ("ConfigMap", "web-config"),
        ("Secret", "web-tls"),
        ("Secret", "web-token"),
        ("Deployment", "web"),
    ]
    assert {d["metadata"]["namespace"] for d in documents} == {"release-demo"}
    assert documents[0]["data"] == {"greeting": "hello from piceli"}
    assert documents[2]["data"] == {"token": "<redacted>"}


def test_render_json_names_secret_inputs_not_values():
    result = _invoke(
        str(EXAMPLE / "composition.py") + ":build",
        "--spec",
        str(EXAMPLE / "release.toml"),
        "--format",
        "json",
        "--namespace",
        "preview",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "rendered" and payload["namespace"] == "preview"
    components = {c["name"]: c for c in payload["components"]}
    assert components["web"]["dependencies"] == ["config"]
    bindings = [
        b for r in components["config"]["resources"] for b in r["secret_bindings"]
    ]
    assert {b["input"] for b in bindings} == {"api-token", "web-tls.crt", "web-tls.key"}


def test_render_app_object_without_spec(tmp_path):
    (tmp_path / "shop.py").write_text(
        textwrap.dedent(
            """
            from piceli import App

            app = App("shop")
            web = app.deployment("web", image="nginx:1.27", ports=[80])
            app.service(web, port=80)
            """
        )
    )
    result = _invoke(str(tmp_path / "shop.py") + ":app", "--namespace", "shop")
    assert result.exit_code == 0, result.output
    kinds = [d["kind"] for d in yaml.safe_load_all(result.stdout) if d]
    assert kinds == ["Deployment", "Service"]


def test_render_rejections(tmp_path):
    result = _invoke("--format", "json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-target-invalid"

    result = _invoke("nowhere.py:app", "--format", "json")
    assert result.exit_code == 2
    assert "file not found" in json.loads(result.stdout)["message"]

    # a composition function needs its images: without a spec it is rejected
    result = _invoke(str(EXAMPLE / "composition.py") + ":build", "--format", "json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-model-invalid"

    (tmp_path / "bad.py").write_text(
        "from piceli import App\napp = App('shop')\napp.deployment('web', image='x', node='gpu')\n"
    )
    result = _invoke(str(tmp_path / "bad.py") + ":app")
    assert result.exit_code == 2
    assert "pinned to node 'gpu'" in result.stderr

    (tmp_path / "other.py").write_text("value = 3\n")
    result = _invoke(str(tmp_path / "other.py") + ":value")
    assert result.exit_code == 2
    assert "must be an App" in result.stderr


def test_import_piceli_is_cheap_and_exports_lazily():
    code = textwrap.dedent(
        """
        import sys
        import piceli
        heavy = [m for m in ("piceli.app", "pydantic", "kubernetes") if m in sys.modules]
        assert not heavy, heavy
        from piceli import App, ExistingClaim
        assert App.__module__ == "piceli.app.app"
        assert "kubernetes" not in sys.modules, "piceli.app must not import kubernetes"
        assert "App" in dir(piceli)
        try:
            piceli.Nope
        except AttributeError:
            pass
        else:
            raise AssertionError
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)


def test_render_typed_app_example():
    spec = ROOT / "examples" / "typed_app" / "release.toml"
    result = _invoke("--spec", str(spec), "--format", "json")
    assert result.exit_code == 0, result.output
    components = {c["name"]: c for c in json.loads(result.stdout)["components"]}
    assert components["api"]["dependencies"] == ["cache", "cache-password"]
    kinds = [r["manifest"]["kind"] for c in components.values() for r in c["resources"]]
    assert "PersistentVolumeClaim" not in kinds
    assert sorted(kinds) == sorted(
        ["Deployment", "Deployment", "Service", "Service", "NetworkPolicy", "Secret"]
    )


MODEL_ERRORS = {
    "duplicate-service": (
        """
        from piceli import App

        app = App("shop")
        web = app.deployment("web", image="nginx:1.27", ports=[80])
        app.service(web, port=80)
        app.service(web, port=80)
        """,
        "app",
        "importing",
        "ValueError: Service 'web' is already declared",
    ),
    "bad-keyword": (
        """
        from piceli import App

        app = App("shop")
        web = app.deployment("web", image="nginx:1.27", portz=[80])
        """,
        "app",
        "importing",
        "TypeError: ",
    ),
    "any-exception": (
        """
        raise RuntimeError("the model module broke")
        """,
        "app",
        "importing",
        "RuntimeError: the model module broke",
    ),
    "composition-raises": (
        """
        def build(ctx):
            raise TypeError("build() got an unexpected keyword argument 'x'")
        """,
        "build",
        "evaluating",
        "TypeError: build() got an unexpected keyword argument 'x'",
    ),
}


@pytest.mark.parametrize("case", sorted(MODEL_ERRORS))
@pytest.mark.parametrize("output", ["yaml", "json"])
def test_render_rejects_a_model_that_raises(tmp_path, monkeypatch, case, output):
    source, attribute, doing, text = MODEL_ERRORS[case]
    (tmp_path / "model.py").write_text(textwrap.dedent(source))
    monkeypatch.delenv("PICELI_DEBUG", raising=False)
    result = _invoke(str(tmp_path / "model.py") + ":" + attribute, "--format", output)
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)  # one JSON object, whatever --format says
    assert payload["state"] == "rejected"
    assert payload["reason"] == "render-target-invalid"
    assert payload["message"].startswith(f"{doing} ")
    assert text in payload["message"]
    assert "Traceback" not in result.stdout + result.stderr


def test_render_debug_prints_the_traceback_on_stderr(tmp_path, monkeypatch):
    (tmp_path / "model.py").write_text('raise RuntimeError("broke")\n')
    monkeypatch.setenv("PICELI_DEBUG", "1")
    result = _invoke(str(tmp_path / "model.py") + ":app")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-target-invalid"
    assert "Traceback" in result.stderr and "Traceback" not in result.stdout


def test_render_spec_composition_that_raises_is_rejected(tmp_path):
    (tmp_path / "compose.py").write_text('raise TypeError("bad keyword")\n')
    spec = (EXAMPLE / "release.toml").read_text()
    spec = spec.replace('"composition.py:build"', f'"{tmp_path / "compose.py"}:build"')
    (tmp_path / "release.toml").write_text(spec)
    result = _invoke("--spec", str(tmp_path / "release.toml"))
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["reason"] == "render-target-invalid"
    assert "TypeError: bad keyword" in payload["message"]
