"""``piceli chart``: a Helm chart and plain manifests from the same render.

The tests that run ``helm`` are skipped when it is not on ``PATH``; run them
with ``nix shell nixpkgs#kubernetes-helm -c uv run pytest tests/unit/app/test_chart.py``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.chart import (
    HELM_CONFIG,
    HELM_CONTENT,
    ChartError,
    build_chart,
    merge_values,
    split_image,
    strip_helm_labels,
    validate,
)
from piceli.k8s.cli import app as cli
from tests.oci_registry import oci_registry

ROOT = Path(__file__).resolve().parents[3]
HELM = shutil.which("helm")
needs_helm = pytest.mark.skipif(HELM is None, reason="needs helm on PATH")
NGINX = "docker.io/library/nginx:1.27@sha256:" + "a" * 64
REDIS = "docker.io/library/redis:7.4@sha256:" + "b" * 64
SHOP = f"""
from piceli import App, ClaimTemplate, Random, Resources, Route, Secrets, SecretVolume

app = App("shop")
secrets = Secrets(password=Random(24))
credentials = app.secret("db-credentials", {{"password": secrets.ref("password")}})
tls = app.secret("shop-tls", {{"tls.crt": secrets.ref("password")}}, type="kubernetes.io/tls")
settings = app.config("settings", {{"greeting": "hello", "template": "{{{{ not helm }}}}"}})
web = app.deployment(
    "web", image="{NGINX}", ports=[80], replicas=2,
    env={{"GREETING": settings.key("greeting"), "PASSWORD": credentials.key("password")}},
    resources=Resources(cpu="100m", memory="64Mi"),
)
service = app.service(web, port=80)
app.ingress("shop", hosts=["shop.example.com"], routes=[Route(service, "/")],
            class_name="nginx", tls_secret=tls)
db = app.stateful_set(
    "db", image="{REDIS}", ports=[6379],
    volumes={{
        "/data": ClaimTemplate("data", size="1Gi", storage_class="fast"),
        "/etc/db": SecretVolume(credentials),
    }},
)
app.cron_job("report", schedule="0 3 * * *", image="{REDIS}", command=["true"])
"""
PIPELINE = """
from piceli import App, Build, NodeLoopbackRegistry, Pipeline, Target

target = Target.kubeconfig(
    "missing.kubeconfig", context="kind-shop", namespace="shop",
    nodes={"edge": "shop-worker"},
)
app = App("shop")
images = Build.spec("build.toml")
app.deployment("web", image=images["web"], ports=[8080])
pipeline = Pipeline(app, target, build=images, deliver=NodeLoopbackRegistry(node="edge"))
"""


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, list(args))


def _module(tmp_path: Path, body: str = SHOP, name: str = "shop.py") -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body))
    return str(path)


def _rendered(target: str, *args: str) -> dict[str, Any]:
    result = _run("render", target, "--format", "json", *args)
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout))


def _objects(components: list[dict[str, Any]], *, secrets: bool = False) -> list:
    found = [
        resource["manifest"]
        for component in components
        for resource in component["resources"]
        if secrets or resource["manifest"]["kind"] != "Secret"
    ]
    return sorted(found, key=_key)


def _key(manifest: dict[str, Any]) -> tuple[str, str]:
    return manifest["kind"], manifest["metadata"]["name"]


def _chart(tmp_path: Path, body: str = SHOP, **options: Any) -> Any:
    rendered = _rendered(_module(tmp_path, body) + ":app")
    return rendered, build_chart(
        rendered["components"],
        name=options.pop("name", "shop"),
        version=options.pop("version", "0.1.0"),
        namespace=rendered["namespace"],
        **options,
    )


def _write(chart: Any, directory: Path) -> Path:
    for item in chart.files():
        path = directory / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.content)
    return directory


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    return subprocess.run(
        [HELM, *args], capture_output=True, text=True, timeout=120, env=env
    )


def _helm_objects(stdout: str) -> list[dict[str, Any]]:
    found = []
    for document in yaml.safe_load_all(stdout):
        if not document:
            continue
        labels = document["metadata"].get("labels") or {}
        assert labels["app.kubernetes.io/managed-by"] == "Helm"
        assert labels["helm.sh/chart"].startswith("shop-")
        found.append(strip_helm_labels(document))
    return sorted(found, key=_key)


# ----------------------------------------------------------- equivalence


def test_default_manifests_equal_piceli_render_without_secrets(tmp_path):
    rendered, chart = _chart(tmp_path)
    assert _objects(chart.manifests()) == _objects(rendered["components"])
    assert [secret.name for secret in chart.secrets] == ["db-credentials", "shop-tls"]
    assert chart.secrets[0].keys == ("password",)
    assert chart.required == ()


def test_values_cover_what_a_client_sets(tmp_path):
    _, chart = _chart(tmp_path)
    values = chart.values
    assert values["images"]["nginx"] == {
        "repository": "docker.io/library/nginx",
        "tag": "1.27",
        "digest": "sha256:" + "a" * 64,
    }
    assert values["imagePullSecrets"] == []
    assert values["workloads"]["web"]["replicas"] == 2
    assert values["workloads"]["web"]["containers"]["web"]["resources"] == {
        "requests": {"cpu": "100m", "memory": "64Mi"}
    }
    assert values["workloads"]["db"]["storage"]["data"] == {
        "size": "1Gi",
        "storageClassName": "fast",
    }
    assert values["ingresses"]["shop"] == {
        "hosts": ["shop.example.com"],
        "className": "nginx",
    }
    assert values["config"]["settings"]["greeting"] == "hello"
    assert values["secrets"] == {
        "db-credentials": {"name": "db-credentials"},
        "shop-tls": {"name": "shop-tls"},
    }
    assert validate(values, chart.schema) == []


def test_values_change_the_manifests(tmp_path):
    _, chart = _chart(tmp_path)
    components = chart.manifests(
        {
            "images": {"nginx": {"repository": "registry.example/web", "tag": "2"}},
            "imagePullSecrets": ["pull"],
            "workloads": {
                "web": {"replicas": 5, "nodeSelector": {"pool": "a"}},
                "db": {"storage": {"data": {"storageClassName": "", "size": "5Gi"}}},
            },
            "ingresses": {"shop": {"hosts": ["shop.client.example"]}},
            "secrets": {"db-credentials": {"name": "client-db"}},
            "config": {"settings": {"greeting": "hola"}},
        },
        namespace="client",
    )
    objects = {_key(item): item for item in _objects(components)}
    web = objects[("Deployment", "web")]
    pod = web["spec"]["template"]["spec"]
    assert web["metadata"]["namespace"] == "client"
    assert web["spec"]["replicas"] == 5
    assert pod["nodeSelector"] == {"pool": "a"}
    assert pod["imagePullSecrets"] == [{"name": "pull"}]
    # the digest default stays: a client that sets only the tag keeps the pin
    assert pod["containers"][0]["image"] == (
        "registry.example/web:2@sha256:" + "a" * 64
    )
    refs = {entry["name"]: entry["valueFrom"] for entry in pod["containers"][0]["env"]}
    assert refs["PASSWORD"]["secretKeyRef"]["name"] == "client-db"
    db = objects[("StatefulSet", "db")]
    claim = db["spec"]["volumeClaimTemplates"][0]["spec"]
    assert "storageClassName" not in claim
    assert claim["resources"]["requests"]["storage"] == "5Gi"
    volumes = db["spec"]["template"]["spec"]["volumes"]
    assert volumes[0]["secret"]["secretName"] == "client-db"
    ingress = objects[("Ingress", "shop")]["spec"]
    assert ingress["rules"][0]["host"] == "shop.client.example"
    assert ingress["tls"][0] == {
        "hosts": ["shop.client.example"],
        "secretName": "shop-tls",
    }
    assert objects[("ConfigMap", "settings")]["data"]["greeting"] == "hola"


def test_null_removes_an_optional_value_like_helm(tmp_path):
    _, chart = _chart(tmp_path)
    components = chart.manifests(
        {"workloads": {"web": {"containers": {"web": {"resources": None}}}}}
    )
    web = {_key(item): item for item in _objects(components)}[("Deployment", "web")]
    assert "resources" not in web["spec"]["template"]["spec"]["containers"][0]
    assert merge_values({"a": {"b": 1, "c": 2}}, {"a": {"b": None}}) == {"a": {"c": 2}}


def test_schema_rejects_bad_values_without_printing_them(tmp_path):
    _, chart = _chart(tmp_path)
    cases = [
        ({"workloads": {"web": {"replicas": -1}}}, "values.workloads.web.replicas"),
        ({"workloads": {"web": {"replicas": "many"}}}, "must be integer"),
        ({"images": {"nginx": {"digest": "sha256:short-secret"}}}, "digest"),
        ({"images": {"nginx": {"tag": "", "digest": ""}}}, "tag or a digest"),
        ({"unknown": 1}, "values.unknown: is not a value of this chart"),
        ({"ingresses": {"shop": {"hosts": ["a.example", "b.example"]}}}, "at most"),
        ({"workloads": {"db": {"storage": {"data": {"size": "lots"}}}}}, "size"),
    ]
    for values, text in cases:
        with pytest.raises(ChartError) as error:
            chart.manifests(values)
        assert error.value.code == "chart-values-invalid"
        assert text in str(error.value)
        assert "short-secret" not in str(error.value)


def test_unresolved_image_is_a_required_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = _module(tmp_path, PIPELINE, "pipeline.py")
    rendered = _rendered(module + ":pipeline")
    chart = build_chart(
        rendered["components"], name="shop", version="1.0.0", namespace="shop"
    )
    assert chart.required == ("images.web",)
    assert chart.values["images"]["web"] == {"repository": "", "tag": "", "digest": ""}
    # the node pin of the delivery node is a value a client clears
    assert chart.values["workloads"]["web"]["nodeSelector"] == {
        "kubernetes.io/hostname": "shop-worker"
    }
    with pytest.raises(ChartError) as error:
        chart.manifests()
    assert "values.images.web.repository: must not be empty" in str(error.value)
    image = {"repository": "registry.example/web", "digest": "sha256:" + "c" * 64}
    components = chart.manifests(
        {
            "images": {"web": image},
            "workloads": {"web": {"nodeSelector": {"kubernetes.io/hostname": None}}},
        }
    )
    pod = _objects(components)[0]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == "registry.example/web@sha256:" + "c" * 64
    assert "nodeSelector" not in pod


def test_secret_values_outside_secrets_are_refused():
    component = {
        "name": "web",
        "dependencies": [],
        "resources": [
            {
                "manifest": {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "c", "namespace": "shop"},
                    "data": {"url": "<redacted>"},
                },
                "secret_bindings": [],
            }
        ],
    }
    with pytest.raises(ChartError) as error:
        build_chart([component], name="shop", version="1.0.0", namespace="shop")
    assert error.value.code == "chart-secret-value"
    component["resources"][0]["manifest"]["kind"] = "Secret"
    with pytest.raises(ChartError) as error:
        build_chart([component], name="shop", version="1.0.0", namespace="shop")
    assert error.value.code == "chart-empty"


def test_names_and_versions_are_validated():
    for name, version in (("Shop", "1.0.0"), ("shop", "1.0"), ("shop", "v1.0.0")):
        with pytest.raises(ChartError) as error:
            build_chart([], name=name, version=version, namespace="shop")
        assert error.value.code == "chart-invalid"


def test_split_image():
    assert split_image("registry:5000/a/b:1.0@sha256:x") == (
        "registry:5000/a/b",
        "1.0",
        "sha256:x",
    )
    assert split_image("registry:5000/a/b") == ("registry:5000/a/b", "", "")


# --------------------------------------------------------- determinism


def test_chart_files_and_archive_are_deterministic(tmp_path):
    charts = []
    for name, version in (("a", "0.1.0"), ("b", "0.1.0"), ("c", "0.2.0")):
        (tmp_path / name).mkdir()
        charts.append(_chart(tmp_path / name, version=version)[1])
    first, second, other = charts
    assert first.archive() == second.archive()
    assert first.oci().digest == second.oci().digest
    assert other.archive() != first.archive()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(first.archive()))) as tar:
        names = tar.getnames()
        assert all(member.mtime == 0 for member in tar.getmembers())
    assert "shop/Chart.yaml" in names and "shop/values.schema.json" in names
    assert "shop/templates/web_deployment.apps_default_web.yaml" in names
    assert not any("_secret." in name for name in names)


def test_cli_package_is_byte_identical_and_refuses_other_content(tmp_path):
    target = _module(tmp_path) + ":app"
    out = tmp_path / "dist"
    first = _run("chart", "package", target, "--out", str(out))
    assert first.exit_code == 0, first.output
    body = json.loads(first.stdout)
    assert body["state"] == "packaged" and body["chart"] == "shop"
    data = (out / "shop-0.1.0.tgz").read_bytes()
    again = _run("chart", "package", target, "--out", str(out))
    assert again.exit_code == 0 and json.loads(again.stdout)["digest"] == body["digest"]
    assert (out / "shop-0.1.0.tgz").read_bytes() == data
    (out / "shop-0.1.0.tgz").write_bytes(b"other")
    refused = _run("chart", "package", target, "--out", str(out))
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "chart-out-refused"


def test_cli_render_writes_a_chart_directory_and_rewrites_only_its_own(tmp_path):
    target = _module(tmp_path) + ":app"
    out = tmp_path / "chart"
    result = _run("chart", "render", target, "--out", str(out), "--version", "1.2.3")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "written" and body["version"] == "1.2.3"
    assert body["secrets"][0] == {
        "name": "db-credentials",
        "type": "Opaque",
        "keys": ["password"],
    }
    chart = yaml.safe_load((out / "Chart.yaml").read_text())
    assert chart["version"] == "1.2.3" and chart["appVersion"] == "1.2.3"
    assert "<redacted>" not in "".join(
        path.read_text() for path in out.rglob("*") if path.is_file()
    )
    assert "db-credentials" in (out / "README.md").read_text()
    again = _run("chart", "render", target, "--out", str(out), "--version", "1.2.3")
    assert again.exit_code == 0, again.output
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("mine")
    refused = _run("chart", "render", target, "--out", str(foreign))
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "chart-out-refused"
    assert (foreign / "notes.txt").read_text() == "mine"


def test_cli_manifests_apply_a_values_file(tmp_path):
    target = _module(tmp_path) + ":app"
    values = tmp_path / "values.yaml"
    values.write_text("workloads:\n  web:\n    replicas: 4\n")
    result = _run("chart", "manifests", target, "--values", str(values))
    assert result.exit_code == 0, result.output
    documents = {_key(d): d for d in yaml.safe_load_all(result.stdout) if d}
    assert documents[("Deployment", "web")]["spec"]["replicas"] == 4
    assert ("Secret", "db-credentials") not in documents
    out = tmp_path / "manifests"
    written = _run("chart", "manifests", target, "-f", str(values), "--out", str(out))
    assert written.exit_code == 0, written.output
    assert "web_deployment.apps_default_web.yaml" in json.loads(written.stdout)["files"]
    values.write_text("workloads:\n  web:\n    replicas: -4\n")
    refused = _run("chart", "manifests", target, "--values", str(values))
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "chart-values-invalid"
    values.write_text("- not a mapping\n")
    refused = _run("chart", "manifests", target, "--values", str(values))
    assert json.loads(refused.stdout)["reason"] == "chart-values-invalid"


def test_cli_publish_pushes_the_helm_layout_after_approval(tmp_path):
    target = _module(tmp_path) + ":app"
    with oci_registry() as registry:
        to = f"oci://{registry.host}/charts"
        preview = _run("chart", "publish", target, "--to", to, "--version", "1.0.0")
        assert preview.exit_code == 3, preview.output
        body = json.loads(preview.stdout)
        assert body["state"] == "approval-required" and registry.mutations() == []
        assert "/charts/shop:1.0.0" in body["target"]
        wrong = _run(
            "chart", "publish", target, "--to", to, "--version", "1.0.0",
            "--approve", "sha256:" + "0" * 64,
        )  # fmt: skip
        assert json.loads(wrong.stdout)["reason"] == "chart-artifact-changed"
        assert registry.mutations() == []
        result = _run(
            "chart", "publish", target, "--to", to, "--version", "1.0.0",
            "--approve", body["digest"],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        published = json.loads(result.stdout)
        assert published["tag"] == "1.0.0"
        assert published["url"] == f"oci://{registry.host}/charts/shop"
        manifest = json.loads(registry.manifests[("charts/shop", "1.0.0")][0])
        assert manifest["config"]["mediaType"] == HELM_CONFIG
        assert manifest["layers"][0]["mediaType"] == HELM_CONTENT
        config = registry.blobs[("charts/shop", manifest["config"]["digest"])]
        assert json.loads(config)["version"] == "1.0.0"
    tagged = _run("chart", "publish", target, "--to", to + ":v1")
    assert json.loads(tagged.stdout)["reason"] == "chart-target-invalid"


def test_cli_refuses_a_render_without_one_app_name(tmp_path):
    target = _module(tmp_path) + ":app"
    result = _run(
        "chart", "render", target, "--out", str(tmp_path / "c"), "--name", "Bad"
    )
    assert json.loads(result.stdout)["reason"] == "chart-invalid"


# ------------------------------------------------------------------ helm


@needs_helm
def test_helm_template_with_defaults_equals_piceli_render(tmp_path):
    rendered, chart = _chart(tmp_path)
    directory = _write(chart, tmp_path / "chart")
    namespace = rendered["namespace"]
    lint = _helm("lint", str(directory), "--namespace", namespace)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    result = _helm("template", "rel", str(directory), "--namespace", namespace)
    assert result.returncode == 0, result.stderr
    assert _helm_objects(result.stdout) == _objects(rendered["components"])
    # a string holding "{{" stays a literal
    config = {_key(d): d for d in _helm_objects(result.stdout)}[
        ("ConfigMap", "settings")
    ]
    assert config["data"]["template"] == "{{ not helm }}"


@needs_helm
def test_helm_template_of_the_reference_app_equals_piceli_render(tmp_path):
    target = str(ROOT / "examples" / "reference" / "app.py") + ":app"
    # In a child process: the example's ``crds`` package must not stay imported
    # (other examples have their own).
    result = subprocess.run(
        [sys.executable, "-m", "piceli", "render", target, "--env", "prod",
         "--format", "json"],
        capture_output=True, text=True, timeout=120, cwd=ROOT,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    rendered = json.loads(result.stdout)
    chart = build_chart(
        rendered["components"],
        name="reference",
        version="0.1.0",
        namespace=rendered["namespace"],
    )
    directory = _write(chart, tmp_path / "reference")
    result = _helm(
        "template", "rel", str(directory), "--namespace", rendered["namespace"]
    )
    assert result.returncode == 0, result.stderr
    found = sorted(
        (strip_helm_labels(d) for d in yaml.safe_load_all(result.stdout) if d),
        key=_key,
    )
    assert found == _objects(rendered["components"])


@needs_helm
def test_helm_and_piceli_apply_the_same_values(tmp_path):
    _, chart = _chart(tmp_path)
    directory = _write(chart, tmp_path / "chart")
    values = {
        "images": {"nginx": {"repository": "registry.example/web", "tag": "2"}},
        "imagePullSecrets": ["pull"],
        "workloads": {
            "web": {"replicas": 5, "nodeSelector": {"pool": "a"}},
            "db": {"storage": {"data": {"storageClassName": ""}}},
        },
        "ingresses": {"shop": {"hosts": ["shop.client.example"], "className": ""}},
        "secrets": {"db-credentials": {"name": "client-db"}},
        "config": {"settings": {"greeting": '<b>hola</b> & "adios"'}},
    }
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values))
    result = _helm(
        "template", "rel", str(directory), "--namespace", "client", "-f", str(path)
    )
    assert result.returncode == 0, result.stderr
    assert _helm_objects(result.stdout) == _objects(chart.manifests(values, "client"))


@needs_helm
def test_helm_schema_rejects_a_missing_required_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = _module(tmp_path, PIPELINE, "pipeline.py")
    rendered = _rendered(module + ":pipeline")
    chart = build_chart(
        rendered["components"], name="shop", version="1.0.0", namespace="shop"
    )
    directory = _write(chart, tmp_path / "chart")
    missing = _helm("template", "rel", str(directory), "--namespace", "shop")
    assert missing.returncode != 0
    assert "repository" in missing.stderr
    ok = _helm(
        "template", "rel", str(directory), "--namespace", "shop",
        "--set", "images.web.repository=registry.example/web",
        "--set", "images.web.tag=1.0.0",
    )  # fmt: skip
    assert ok.returncode == 0, ok.stderr
    assert "registry.example/web:1.0.0" in ok.stdout


@needs_helm
def test_helm_pulls_the_published_chart(tmp_path):
    target = _module(tmp_path) + ":app"
    package = _run("chart", "package", target, "--out", str(tmp_path / "dist"))
    local = json.loads(package.stdout)["digest"]
    with oci_registry() as registry:
        to = f"oci://{registry.host}/charts"
        preview = json.loads(_run("chart", "publish", target, "--to", to).stdout)
        result = _run(
            "chart", "publish", target, "--to", to, "--approve", preview["digest"]
        )
        assert result.exit_code == 0, result.output
        (tmp_path / "pulled").mkdir()
        pulled = _helm(
            "pull", f"{to}/shop", "--version", "0.1.0", "--plain-http",
            "--destination", str(tmp_path / "pulled"),
        )  # fmt: skip
        assert pulled.returncode == 0, pulled.stderr
    data = (tmp_path / "pulled" / "shop-0.1.0.tgz").read_bytes()
    assert "sha256:" + hashlib.sha256(data).hexdigest() == local


def test_existing_claims_are_referenced_by_name(tmp_path):
    body = f"""
from piceli import App, ExistingClaim

app = App("shop")
app.deployment("db", image="{REDIS}", volumes={{"/data": ExistingClaim("db-state")}})
"""
    rendered, chart = _chart(tmp_path, body)
    assert chart.existing_claims == ("db-state",)
    assert chart.values["existingClaims"] == {"db-state": {"claimName": "db-state"}}
    assert _objects(chart.manifests()) == _objects(rendered["components"])
    moved = chart.manifests({"existingClaims": {"db-state": {"claimName": "pd-1"}}})
    volume = _objects(moved)[0]["spec"]["template"]["spec"]["volumes"][0]
    assert volume["persistentVolumeClaim"]["claimName"] == "pd-1"
    assert "db-state" in chart.readme().decode()
