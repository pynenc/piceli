"""``piceli bundle --helm``: the client bundle as a Helm chart.

The fixture (``helm_app.py``) is a client app: a StatefulSet with a sidecar,
a settings ConfigMap, a Secret with an imported and two generated values,
cluster RBAC and a toggle. ``helm lint`` and ``helm template`` run when helm
is on PATH (the CI job ``comparisons`` installs it and sets
``PICELI_COMPARISONS=require``, which turns a missing helm into a failure).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.bundle.build import BundleError, build_bundle
from piceli.bundle.helm import (
    HelmChart,
    HelmToggle,
    apply_toggle,
    build_helm,
    json_patch,
    value_name,
)
from piceli.chart import HELM_CONFIG, HELM_CONTENT
from piceli.k8s.cli import app as cli
from tests.oci_registry import oci_registry

ROOT = Path(__file__).resolve().parents[3]
TARGET = "tests/unit/bundle/helm_app.py:chart"
HELM = shutil.which("helm")
REQUIRE = os.environ.get("PICELI_COMPARISONS") == "require"
VALUES = ("--set", "team=t1", "--set", "clusterId=42", "--set", "apiToken=token-1")


@pytest.fixture
def helm() -> str:
    if HELM is None:
        if REQUIRE:
            pytest.fail("PICELI_COMPARISONS=require but helm is not on PATH")
        pytest.skip("needs helm on PATH")
    return HELM


def _run(*args: str) -> Any:
    cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        return CliRunner().invoke(cli, ["bundle", *args])
    finally:
        os.chdir(cwd)


def _write(tmp_path: Path, *extra: str) -> tuple[Path, dict[str, Any]]:
    out = tmp_path / "chart"
    result = _run(TARGET, "--helm", "--out", str(out), "--version", "1.2.3", *extra)
    assert result.exit_code == 0, result.output
    return out, json.loads(result.stdout)


def _template(helm: str, chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [helm, "template", "agent", str(chart), "-n", "edge", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(chart.parent)},
    )


def _objects(text: str) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(text)
        if item
    }


def _pod(objects: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    return objects[("StatefulSet", "agent")]["spec"]["template"]["spec"]


# ------------------------------------------------------------------ files


def test_the_chart_has_metadata_values_and_templates(tmp_path: Path) -> None:
    out, body = _write(tmp_path, "--app-version", "2026.10")
    chart = yaml.safe_load((out / "Chart.yaml").read_text())
    assert chart == {
        "apiVersion": "v2",
        "name": "agent",
        "version": "1.2.3",
        "appVersion": "2026.10",
        "type": "application",
        "description": "agent, packaged by piceli bundle for a client's cluster",
    }
    values = yaml.safe_load((out / "values.yaml").read_text())
    assert values == {
        "apiToken": "",
        "clusterId": "",
        "deleteDataOnUninstall": False,
        "image": {"pullSecrets": [], "registry": ""},
        "relayOnly": False,
        "siteName": "lab",
        "storageClass": "",
        "team": "",
    }
    schema = json.loads((out / "values.schema.json").read_text())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["relayOnly"]["type"] == "boolean"
    assert body["state"] == "written"
    assert body["settings"] == ["clusterId", "siteName", "team"]
    assert body["inputs"] == ["apiToken"]
    assert body["required_values"] == ["clusterId", "team"]
    assert body["toggles"] == ["relayOnly"]
    assert body["cluster_objects"] == [
        "ClusterRole/agent:agent:agent-reader",
        "ClusterRoleBinding/agent:agent:agent-reader",
    ]
    assert (out / "templates" / "piceli-secrets.yaml").is_file()
    assert not (out / "prepare.sh").exists()
    readme = (out / "README.md").read_text()
    assert "helm uninstall agent -n agent" in readme
    assert "--set-file apiToken=FILE" in readme


def test_the_chart_holds_no_secret_value(tmp_path: Path) -> None:
    out, _ = _write(tmp_path)
    text = "".join(path.read_text() for path in out.rglob("*") if path.is_file())
    assert "randBytes 32" in text
    assert "lookup" in text
    assert "API_TOKEN" not in text  # the release engine's import source


def test_the_archive_is_deterministic(tmp_path: Path) -> None:
    first = _write(tmp_path / "a")[1]["archive_digest"]
    second = _write(tmp_path / "b")[1]["archive_digest"]
    assert first == second


def test_value_names_are_camel_case() -> None:
    assert value_name("poet-cluster-id") == "poetClusterId"
    assert value_name("shibuya_token") == "shibuyaToken"
    assert value_name("organisation") == "organisation"


def test_out_must_be_empty(tmp_path: Path) -> None:
    out = tmp_path / "chart"
    out.mkdir()
    (out / "keep").write_text("x")
    result = _run(TARGET, "--helm", "--out", str(out))
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "bundle-out-refused"


def test_push_flags_need_helm(tmp_path: Path) -> None:
    result = _run(TARGET, "--out", str(tmp_path / "b"), "--push", "oci://r.example/c")
    assert json.loads(result.stdout)["reason"] == "bundle-invalid"


def test_a_build_receipt_is_refused_with_helm(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    result = _run(
        TARGET, "--helm", "--out", str(tmp_path / "c"), "--receipt", str(receipt)
    )
    assert json.loads(result.stdout)["reason"] == "bundle-invalid"


# -------------------------------------------------------------- toggles


def test_json_patch_applies_and_tests() -> None:
    document = {"a": [{"b": 1}, {"b": 2}]}
    patched = json_patch(
        document,
        [
            {"op": "test", "path": "/a/1/b", "value": 2},
            {"op": "replace", "path": "/a/1/b", "value": 3},
            {"op": "add", "path": "/a/-", "value": {"b": 4}},
            {"op": "remove", "path": "/a/0"},
        ],
    )
    assert patched == {"a": [{"b": 3}, {"b": 4}]}
    assert document == {"a": [{"b": 1}, {"b": 2}]}
    with pytest.raises(ValueError, match="test failed"):
        json_patch(document, [{"op": "test", "path": "/a/0/b", "value": 9}])
    with pytest.raises(ValueError, match="does not apply"):
        json_patch(document, [{"op": "replace", "path": "/a/9/b", "value": 9}])


def _bundle(components: list[dict[str, Any]]) -> Any:
    from tests.unit.bundle import helm_app

    return build_bundle(
        components,
        name="agent",
        version="1.0.0",
        namespace="agent",
        generators=helm_app.secrets.generators,
    )


def _components() -> list[dict[str, Any]]:
    from piceli.k8s.cli.render import _render_pipeline
    from tests.unit.bundle import helm_app

    def refuse(code: str, message: str) -> Any:
        raise AssertionError(f"{code}: {message}")

    return _render_pipeline(helm_app.pipeline, None, None, refuse)[1]


def test_a_toggle_naming_no_object_is_refused() -> None:
    toggle = HelmToggle("x", patches={"NetworkPolicy/missing": {"spec": {}}})
    with pytest.raises(BundleError, match="lacks") as error:
        apply_toggle(_components(), "x", toggle)
    assert error.value.code == "bundle-invalid"


def test_a_toggle_whose_test_fails_is_refused() -> None:
    toggle = HelmToggle(
        "x",
        patches={
            "NetworkPolicy/agent-egress": [
                {"op": "test", "path": "/spec/egress/0/ports/0/port", "value": 1}
            ]
        },
    )
    with pytest.raises(BundleError, match="test failed"):
        apply_toggle(_components(), "x", toggle)


def test_two_toggles_on_one_object_are_refused() -> None:
    components = _components()
    patch = {"NetworkPolicy/agent-egress": {"metadata": {"annotations": {"a": "b"}}}}
    other = {"NetworkPolicy/agent-egress": {"metadata": {"annotations": {"c": "d"}}}}
    toggles = {
        "one": HelmToggle("one", patches=patch),
        "two": HelmToggle("two", patches=other),
    }
    variants = {
        name: _bundle(apply_toggle(components, name, toggle))
        for name, toggle in toggles.items()
    }
    with pytest.raises(BundleError, match="same object"):
        build_helm(
            _bundle(components),
            variants=variants,
            declaration=HelmChart("agent", toggles=toggles),
        )


def test_a_tls_generator_is_refused() -> None:
    from piceli.app.render import rendered
    from tests.unit.bundle.fixtures import app

    shop, generators = app()
    inputs = {name: generators.ref(name) for name in generators.inputs()}
    components = rendered(shop.render("shop"), inputs)
    bundle = build_bundle(
        components,
        name="shop",
        version="1.0.0",
        namespace="shop",
        generators=generators.generators,
    )
    with pytest.raises(BundleError) as error:
        build_helm(bundle)
    assert error.value.code == "bundle-secret-unsupported"


def test_required_must_name_a_setting() -> None:
    components = _components()
    with pytest.raises(BundleError, match="not settings"):
        build_helm(
            _bundle(components),
            declaration=HelmChart("agent", required=("nope",)),
        )


# ------------------------------------------------------------------ helm


def test_helm_lint_passes(tmp_path: Path, helm: str) -> None:
    out, _ = _write(tmp_path)
    result = subprocess.run(
        [helm, "lint", str(out), "--strict", *VALUES],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_required_values_fail_the_install_clearly(tmp_path: Path, helm: str) -> None:
    out, _ = _write(tmp_path)
    missing = _template(helm, out, "--set", "clusterId=42", "--set", "apiToken=x")
    assert missing.returncode != 0
    assert "team is required: --set team=..." in missing.stderr
    no_token = _template(helm, out, "--set", "team=t", "--set", "clusterId=1")
    assert no_token.returncode != 0
    assert "apiToken is required on the first install" in no_token.stderr
    typo = _template(helm, out, *VALUES, "--set", "teem=t")
    assert typo.returncode != 0  # values.schema.json: no unknown value


def test_default_template_is_the_bundle(tmp_path: Path, helm: str) -> None:
    """With the defaults, helm renders the kustomize bundle's objects."""
    out, _ = _write(tmp_path)
    result = _template(helm, out, *VALUES)
    assert result.returncode == 0, result.stderr
    rendered = _objects(result.stdout)
    base = {
        (item.kind, item.name.replace("agent:", "edge:", 1)): item.manifest
        for item in _bundle_at("1.2.3").objects
    }
    assert set(rendered) == set(base) | {("Secret", "agent-private")}
    for key, manifest in base.items():
        got = json.loads(json.dumps(rendered[key]))
        labels = got["metadata"]["labels"]
        assert labels.pop("app.kubernetes.io/managed-by") == "Helm"
        assert labels.pop("helm.sh/chart") == "agent-1.2.3"
        want = json.loads(json.dumps(manifest))
        if key[0] == "ConfigMap":
            want["data"].update({"team": "t1", "cluster-id": "42"})
        if key[0] == "StatefulSet":
            for path in ("configmaps-digest", "secrets-digest"):
                annotation = f"piceli.io/{path}"
                assert (
                    got["spec"]["template"]["metadata"]["annotations"]
                    .pop(annotation)
                    .startswith("sha256:")
                )
                want["spec"]["template"]["metadata"]["annotations"].pop(annotation)
        if key[0] in {"ClusterRole", "ClusterRoleBinding"}:
            # The cluster-scoped names follow the release namespace (edge).
            want["metadata"]["name"] = want["metadata"]["name"].replace(
                "agent:", "edge:", 1
            )
            want["metadata"]["annotations"]["piceli.io/namespace"] = "edge"
            if key[0] == "ClusterRoleBinding":
                want["roleRef"]["name"] = want["metadata"]["name"]
                want["subjects"][0]["namespace"] = "edge"
        assert got == want, key


def _bundle_at(version: str) -> Any:
    from tests.unit.bundle import helm_app

    return build_bundle(
        _components(),
        name="agent",
        version=version,
        namespace="agent",
        generators=helm_app.secrets.generators,
    )


def test_secrets_are_generated_and_the_input_is_passed(
    tmp_path: Path, helm: str
) -> None:
    out, _ = _write(tmp_path)
    token = tmp_path / "token"
    token.write_text("token-from-file\n")
    result = _template(
        helm, out, "--set", "team=t", "--set", "clusterId=1",
        "--set-file", f"apiToken={token}",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    secret = _objects(result.stdout)[("Secret", "agent-private")]
    data = {
        key: base64.b64decode(value).decode() for key, value in secret["data"].items()
    }
    assert data["api-token"] == "token-from-file"  # trailing newline trimmed
    for key in ("intake-token", "cluster-key"):
        # 32 random bytes, URL-safe base64 without padding (as prepare.sh).
        assert len(data[key]) == 43 and set(data[key]) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
    assert data["intake-token"] != data["cluster-key"]
    assert secret["metadata"]["labels"]["app.kubernetes.io/part-of"] == "agent"


def test_values_change_the_objects(tmp_path: Path, helm: str) -> None:
    out, _ = _write(tmp_path)
    result = _template(
        helm, out, *VALUES,
        "--set", "relayOnly=true",
        "--set", "image.registry=mirror.example/cache",
        "--set", "image.pullSecrets={pull}",
        "--set", "storageClass=fast",
        "--set", "deleteDataOnUninstall=true",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    objects = _objects(result.stdout)
    pod = _pod(objects)
    images = {item["image"] for item in pod["containers"] + pod["initContainers"]}
    assert images == {
        "mirror.example/cache/acme/agent@sha256:" + "a" * 64,
        "mirror.example/cache/acme/collector@sha256:" + "b" * 64,
    }
    assert pod["imagePullSecrets"] == [{"name": "pull"}]
    statefulset = objects[("StatefulSet", "agent")]["spec"]
    assert statefulset["volumeClaimTemplates"][0]["spec"]["storageClassName"] == "fast"
    assert (
        statefulset["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Delete"
    )
    egress = objects[("NetworkPolicy", "agent-egress")]["spec"]["egress"]
    assert egress[1]["ports"] == [
        {"port": 3478, "protocol": "UDP"},
        {"port": 49160, "endPort": 49999, "protocol": "UDP"},
    ]
    default = _objects(_template(helm, out, *VALUES).stdout)
    assert default[("NetworkPolicy", "agent-egress")]["spec"]["egress"][1]["ports"] == [
        {"port": 1, "endPort": 65535, "protocol": "UDP"}
    ]
    kept = default[("StatefulSet", "agent")]["spec"]
    assert kept["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Retain"
    assert "storageClassName" not in kept["volumeClaimTemplates"][0]["spec"]
    assert "imagePullSecrets" not in _pod(default)


# ------------------------------------------------------------------ push


def test_push_needs_the_digest_and_helm_can_pull_it(tmp_path: Path) -> None:
    with oci_registry() as registry:
        to = f"oci://{registry.host}/charts"
        preview = _run(TARGET, "--helm", "--push", to, "--version", "1.2.3")
        assert preview.exit_code == 3, preview.output
        body = json.loads(preview.stdout)
        assert body["state"] == "approval-required" and registry.mutations() == []
        assert "/charts/agent:1.2.3" in body["target"]
        wrong = _run(
            TARGET, "--helm", "--push", to, "--version", "1.2.3",
            "--approve", "sha256:" + "0" * 64,
        )  # fmt: skip
        assert json.loads(wrong.stdout)["reason"] == "chart-artifact-changed"
        assert registry.mutations() == []
        pushed = _run(
            TARGET, "--helm", "--push", to, "--version", "1.2.3",
            "--approve", body["digest"],
        )  # fmt: skip
        assert pushed.exit_code == 0, pushed.output
        published = json.loads(pushed.stdout)
        assert published["url"] == f"oci://{registry.host}/charts/agent"
        manifest = json.loads(registry.manifests[("charts/agent", "1.2.3")][0])
        assert manifest["config"]["mediaType"] == HELM_CONFIG
        assert manifest["layers"][0]["mediaType"] == HELM_CONTENT
        layer = registry.blobs[("charts/agent", manifest["layers"][0]["digest"])]
        assert "sha256:" + hashlib.sha256(layer).hexdigest() == body["archive_digest"]
        if HELM is not None:
            (tmp_path / "pulled").mkdir()
            pulled = subprocess.run(
                [
                    HELM, "pull", f"{to}/agent", "--version", "1.2.3",
                    "--plain-http", "--destination", str(tmp_path / "pulled"),
                ],
                capture_output=True, text=True, timeout=120,
                env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            )  # fmt: skip
            assert pulled.returncode == 0, pulled.stderr
            data = (tmp_path / "pulled" / "agent-1.2.3.tgz").read_bytes()
            assert (
                "sha256:" + hashlib.sha256(data).hexdigest() == body["archive_digest"]
            )
