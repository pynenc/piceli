"""``piceli codegen crd``: deterministic, importable pydantic models from CRD schemas.

The golden files are the example's generated modules
(``examples/environments/crds``), generated from the pinned CRDs vendored in
``tests/fixtures/crds`` (cert-manager v1.16.2 Certificate, prometheus-operator
v0.78.2 ServiceMonitor).
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from piceli import App
from piceli.codegen import CodegenError, generate, load_crds
from piceli.codegen.crd import select_crd
from piceli.k8s.cli import app as cli
from piceli.testing import FakeAPI, serve, write_kubeconfig

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "crds"
GOLDEN = ROOT / "examples" / "environments" / "crds"
CASES = {
    "cert-manager-certificate.yaml": "certificate.py",
    "prometheus-servicemonitor.yaml": "service_monitor.py",
}


def _crd(fixture: str) -> dict[str, Any]:
    return load_crds((FIXTURES / fixture).read_text())[0]


def _module(source: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_loader(name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # pydantic resolves forward references here
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def _shuffled(value: Any, rng: random.Random) -> Any:
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffled(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_shuffled(item, rng) for item in value]
    return value


@pytest.mark.parametrize(("fixture", "golden"), sorted(CASES.items()))
def test_generated_modules_match_the_golden_files(fixture, golden):
    module = generate(_crd(fixture))
    assert module.source == (GOLDEN / golden).read_text(), (
        "regenerate: piceli codegen crd tests/fixtures/crds/"
        f"{fixture} --out examples/environments/crds/{golden}"
    )


@pytest.mark.parametrize("fixture", sorted(CASES))
def test_output_depends_only_on_the_schema(fixture):
    crd = _crd(fixture)
    expected = generate(crd).source
    # Key order (a cluster's JSON vs a file's YAML) and unrelated metadata
    # never change the output.
    for seed in range(3):
        shuffled = _shuffled(json.loads(json.dumps(crd)), random.Random(seed))
        shuffled["metadata"]["uid"] = f"uid-{seed}"
        shuffled["metadata"]["resourceVersion"] = str(seed)
        assert generate(shuffled).source == expected


def test_certificate_models_validate_at_declaration_and_render_by_alias():
    certificate = _module(
        (GOLDEN / "certificate.py").read_text(), "_golden_certificate"
    )
    assert certificate.API_VERSION == "cert-manager.io/v1"
    assert certificate.KIND == "Certificate"
    assert certificate.SCOPE == "namespaced"
    assert certificate.CRD == "certificates.cert-manager.io"
    spec = certificate.CertificateSpec(
        secret_name="shop-tls",
        dns_names=["shop.example.com"],
        issuer_ref={"name": "letsencrypt", "kind": "ClusterIssuer"},
        private_key={"algorithm": "ECDSA", "size": 256},
        duration="2160h",
    )
    # Kubernetes names are accepted too.
    same = certificate.CertificateSpec.model_validate(
        {
            "secretName": "shop-tls",
            "dnsNames": ["shop.example.com"],
            "issuerRef": {"name": "letsencrypt", "kind": "ClusterIssuer"},
            "privateKey": {"algorithm": "ECDSA", "size": 256},
            "duration": "2160h",
        }
    )
    assert same == spec
    with pytest.raises(ValidationError, match="'RSA', 'ECDSA' or 'Ed25519'"):
        certificate.CertificateSpec(
            secret_name="x", issuer_ref={"name": "i"}, private_key={"algorithm": "DSA"}
        )
    with pytest.raises(ValidationError, match=r"issuer_ref|issuerRef"):
        certificate.CertificateSpec(secret_name="x")  # required
    with pytest.raises(ValidationError, match="Extra inputs"):
        certificate.CertificateSpec(secret_name="x", issuer_ref={"name": "i"}, dns=[])
    app = App("shop")
    app.resource(certificate.API_VERSION, certificate.KIND, "shop-tls", spec)
    [component] = app.render("shop").components
    manifest = component.resources[0].manifest
    assert manifest["spec"] == {
        "secretName": "shop-tls",
        "dnsNames": ["shop.example.com"],
        "issuerRef": {"name": "letsencrypt", "kind": "ClusterIssuer"},
        "privateKey": {"algorithm": "ECDSA", "size": 256},
        "duration": "2160h",
    }
    with pytest.raises(ValidationError, match="of kind Certificate, not Issuer"):
        app.resource(certificate.API_VERSION, "Issuer", "other", spec)


def test_service_monitor_models_keep_int_or_string_patterns_and_maps():
    monitor = _module(
        (GOLDEN / "service_monitor.py").read_text(), "_golden_service_monitor"
    )
    spec = monitor.ServiceMonitorSpec(
        selector={"matchLabels": {"app.kubernetes.io/name": "api"}},
        endpoints=[
            {"port": "http", "interval": "30s", "targetPort": 8080},
            {"targetPort": "metrics", "params": {"format": ["prometheus"]}},
        ],
    )
    dumped = spec.model_dump(
        mode="json", by_alias=True, exclude_unset=True, exclude_none=True
    )
    assert dumped["endpoints"][0]["targetPort"] == 8080  # int or string
    assert dumped["endpoints"][1]["targetPort"] == "metrics"
    assert dumped["endpoints"][1]["params"] == {"format": ["prometheus"]}
    with pytest.raises(ValidationError, match="String should match pattern"):
        monitor.ServiceMonitorSpec(
            selector={}, endpoints=[{"port": "http", "interval": "every minute"}]
        )


SYNTHETIC = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": "gizmos.example.test"},
    "spec": {
        "group": "example.test",
        "scope": "Cluster",
        "names": {"kind": "Gizmo", "plural": "gizmos"},
        "versions": [
            {"name": "v1alpha1", "served": True, "storage": False},
            {
                "name": "v1",
                "served": True,
                "storage": True,
                "schema": {
                    "openAPIV3Schema": {
                        "type": "object",
                        "properties": {
                            "spec": {
                                "type": "object",
                                "required": ["size"],
                                "properties": {
                                    "size": {
                                        "x-kubernetes-int-or-string": True,
                                        "anyOf": [
                                            {"type": "integer"},
                                            {"type": "string"},
                                        ],
                                    },
                                    "class": {"type": "string", "minLength": 1},
                                    "replicas": {
                                        "type": "integer",
                                        "minimum": 0,
                                        "maximum": 10,
                                    },
                                    "ratio": {
                                        "type": "number",
                                        "minimum": 0,
                                        "exclusiveMinimum": True,
                                    },
                                    "mode": {
                                        "type": "string",
                                        "enum": ["fast", "safe", None],
                                        "nullable": True,
                                    },
                                    "labels": {
                                        "type": "object",
                                        "additionalProperties": {"type": "string"},
                                        "maxProperties": 3,
                                    },
                                    "config": {
                                        "type": "object",
                                        "x-kubernetes-preserve-unknown-fields": True,
                                    },
                                    "template": {
                                        "type": "object",
                                        "x-kubernetes-embedded-resource": True,
                                        "x-kubernetes-preserve-unknown-fields": True,
                                        "properties": {
                                            "kind": {"type": "string"},
                                        },
                                    },
                                    "items": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "name": {"type": "string"},
                                                "json": {"type": "boolean"},
                                            },
                                        },
                                    },
                                    "model_name": {"type": "string"},
                                    "modelName": {"type": "string"},
                                    "weird": {"type": "string", "pattern": "(?<=x"},
                                },
                            },
                            "status": {"type": "object"},
                        },
                    }
                },
            },
        ],
    },
}


def test_synthetic_schema_features():
    generated = generate(SYNTHETIC)
    assert generated.scope == "cluster" and generated.version == "v1"
    assert generated.api_version == "example.test/v1"
    assert generated.module_name == "gizmo"
    gizmo = _module(generated.source, "_synthetic_gizmo")
    assert gizmo.SCOPE == "cluster"
    spec = gizmo.GizmoSpec(
        size="10Gi",
        class_="gold",  # a keyword gets a trailing underscore
        config={"anything": {"goes": [1]}},
        template={"kind": "Pod", "spec": {"containers": []}},  # open object
        items=[{"name": "a", "json_": True}],
        labels={"a": "b"},
        mode=None,
        f_model_name="n",  # modelName: model_* is pydantic's namespace
        f_model_name_2="m",  # model_name: collisions are numbered
        weird="kept as a plain string (the pattern is not a Python regex)",
    )
    dumped = spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped["class"] == "gold"
    assert dumped["items"] == [{"name": "a", "json": True}]
    assert dumped["template"] == {"kind": "Pod", "spec": {"containers": []}}
    assert dumped["model_name"] == "m" and dumped["modelName"] == "n"
    assert gizmo.GizmoSpec(size=3).size == 3
    for bad in (
        {"size": 1, "replicas": 11},
        {"size": 1, "ratio": 0},
        {"size": 1, "mode": "slow"},
        {"size": 1, "labels": {"a": "1", "b": "2", "c": "3", "d": "4"}},
        {"size": 1, "items": []},
        {"size": 1, "class": ""},
        {},
    ):
        with pytest.raises(ValidationError):
            gizmo.GizmoSpec.model_validate(bad)
    app = App("shop")
    resource = app.resource(gizmo.API_VERSION, gizmo.KIND, "g", gizmo.GizmoSpec(size=1))
    assert resource.scope == "cluster"
    with pytest.raises(CodegenError, match="'v1alpha1' has no openAPIV3Schema"):
        generate(SYNTHETIC, version="v1alpha1")


def test_a_crd_without_properties_in_spec_gets_an_open_model():
    crd = json.loads(json.dumps(SYNTHETIC))
    schema = crd["spec"]["versions"][1]["schema"]["openAPIV3Schema"]
    schema["properties"]["spec"] = {
        "type": "object",
        "x-kubernetes-preserve-unknown-fields": True,
    }
    module = _module(generate(crd).source, "_open_gizmo")
    assert module.GizmoSpec(anything=1).model_dump() == {"anything": 1}


@pytest.mark.parametrize(
    ("change", "code", "message"),
    [
        (lambda crd: crd["spec"].pop("group"), "crd-invalid", "spec.group"),
        (
            lambda crd: crd["spec"]["versions"][1].pop("schema"),
            "crd-invalid",
            "no openAPIV3Schema",
        ),
        (
            lambda crd: [v.update(served=False) for v in crd["spec"]["versions"]],
            "crd-invalid",
            "serves no version",
        ),
    ],
)
def test_invalid_crds_are_refused(change, code, message):
    crd = json.loads(json.dumps(SYNTHETIC))
    change(crd)
    with pytest.raises(CodegenError, match=message) as error:
        generate(crd)
    assert error.value.code == code


def test_loading_and_selecting_crds():
    with pytest.raises(CodegenError, match="not valid YAML") as error:
        load_crds("a: [")
    assert error.value.code == "crd-invalid"
    with pytest.raises(CodegenError, match="no apiextensions"):
        load_crds("apiVersion: v1\nkind: ConfigMap\n")
    both = (
        "---\n".join((FIXTURES / name).read_text() for name in sorted(CASES))
        + "---\n"
        + json.dumps({"kind": "List", "items": [SYNTHETIC]})
    )
    crds = load_crds(both)
    assert len(crds) == 3
    with pytest.raises(CodegenError, match="several CRDs") as error:
        select_crd(crds, None)
    assert error.value.code == "crd-not-found"
    assert (
        select_crd(crds, "ServiceMonitor")["spec"]["names"]["kind"] == "ServiceMonitor"
    )
    assert select_crd(crds, "gizmos.example.test")["spec"]["group"] == "example.test"
    with pytest.raises(CodegenError, match="no CRD named 'nope'"):
        select_crd(crds, "nope")
    with pytest.raises(CodegenError, match="no version 'v9'"):
        generate(SYNTHETIC, version="v9")


# ----------------------------------------------------------------------- CLI


def _run(*args: str) -> Any:
    return CliRunner().invoke(cli, ["codegen", "crd", *args])


def test_cli_prints_the_module_or_json(tmp_path):
    fixture = FIXTURES / "prometheus-servicemonitor.yaml"
    result = _run(str(fixture))
    assert result.exit_code == 0, result.output
    assert result.stdout == (GOLDEN / "service_monitor.py").read_text()
    assert "generated 36 models for ServiceMonitor" in result.stderr
    result = _run(str(fixture), "--json")
    payload = json.loads(result.stdout)
    assert payload["state"] == "generated" and payload["out"] is None
    assert payload["module"] == (GOLDEN / "service_monitor.py").read_text()
    assert payload["kind"] == "ServiceMonitor" and payload["spec_class"] == (
        "ServiceMonitorSpec"
    )


def test_cli_out_replaces_only_generated_files(tmp_path):
    fixture = str(FIXTURES / "cert-manager-certificate.yaml")
    out = tmp_path / "certificate.py"
    result = _run(fixture, "--out", str(out))
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert summary["state"] == "generated" and summary["out"] == str(out)
    assert "module" not in summary
    assert out.read_text() == (GOLDEN / "certificate.py").read_text()
    # Regenerating over a generated file is the normal workflow.
    assert _run(fixture, "--out", str(out)).exit_code == 0
    handwritten = tmp_path / "models.py"
    handwritten.write_text("MINE = 1\n")
    result = _run(fixture, "--out", str(handwritten))
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "codegen-output-refused"
    assert handwritten.read_text() == "MINE = 1\n"
    assert _run(fixture, "--out", str(handwritten), "--force").exit_code == 0
    result = _run(fixture, "--out", str(tmp_path / "missing" / "x.py"))
    assert json.loads(result.stdout)["reason"] == "codegen-output-refused"


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ([], "codegen-flags-conflict"),
        (["FILE", "--from-cluster"], "codegen-flags-conflict"),
        (["--from-cluster", "--context", "c", "--crd", "x"], "codegen-flags-conflict"),
        (["FILE", "--context", "c"], "codegen-flags-conflict"),
        (["FILE", "--crd", "nope"], "crd-not-found"),
        (["FILE", "--version", "v9"], "crd-invalid"),
    ],
)
def test_cli_rejections(args, reason):
    fixture = str(FIXTURES / "cert-manager-certificate.yaml")
    result = _run(*(fixture if item == "FILE" else item for item in args))
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["reason"] == reason


CRD_TYPES = {
    "customresourcedefinitions": (
        "apiextensions.k8s.io/v1",
        "CustomResourceDefinition",
        False,
    )
}


def test_from_cluster_generates_the_same_module_as_the_file(tmp_path):
    api = FakeAPI(types=CRD_TYPES)
    document = yaml.safe_load((FIXTURES / "cert-manager-certificate.yaml").read_text())
    api.put(json.loads(json.dumps(document)))
    with serve(api) as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="kind-test")
        base = [
            "--from-cluster",
            "--kubeconfig",
            str(kubeconfig),
            "--context",
            "kind-test",
            "--transport",
            "loopback-http",
        ]
        result = _run(*base, "--crd", "certificates.cert-manager.io")
        assert result.exit_code == 0, result.output
        assert result.stdout == (GOLDEN / "certificate.py").read_text()
        reads = [r for r in api.requests if "customresourcedefinitions" in r["path"]]
        assert {r["method"] for r in reads} == {"GET"}  # read-only
        result = _run(*base, "--crd", "issuers.cert-manager.io")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["reason"] == "crd-not-found"
        api.inject(
            "GET", "/customresourcedefinitions/certificates.cert-manager.io", status=403
        )
        result = _run(*base, "--crd", "certificates.cert-manager.io")
        assert result.exit_code == 2
        assert json.loads(result.stdout)["reason"] == "codegen-cluster-read-failed"
        assert "do-not-publish" not in result.output
        result = _run(
            *base[:4],
            "wrong-context",
            *base[5:],
            "--crd",
            "certificates.cert-manager.io",
        )
        assert result.exit_code == 2
        assert json.loads(result.stdout)["reason"] == "target-refused"
