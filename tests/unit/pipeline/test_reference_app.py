"""The reference app (``examples/reference``): three environments, no cluster.

Structural render tests of ``dev``, ``staging`` and ``prod`` through
``piceli render``, the typed ``--diff-env`` between staging and prod, and the
example's promises: every object typed (no ``app.override``, no mapping
specs), restricted pods, pinned images, the committed CRD module generated
from the vendored fixture, and a SOPS file encrypted for the committed
example-only age key. None of these tests needs ``sops`` or a cluster.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel
from typer.testing import CliRunner

from piceli.app import App
from piceli.app.render import load_target
from piceli.app.resource import Resource
from piceli.codegen import generate, load_crds
from piceli.k8s.cli import app as cli
from piceli.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "reference"
MODULE = EXAMPLE / "app.py"
ENVIRONMENTS = ("dev", "staging", "prod")
WORKLOADS = {"Deployment", "StatefulSet", "Job", "CronJob"}
ALWAYS = {
    ("ConfigMap", "settings"),
    ("ConfigMap", "site"),
    ("Secret", "store-credentials-1"),
    ("StatefulSet", "store"),
    ("Service", "store"),
    ("Job", "migrate"),
    ("CronJob", "report"),
    ("ServiceAccount", "api-reader"),
    ("Role", "api-reader"),
    ("RoleBinding", "api-reader"),
    ("Deployment", "api"),
    ("Service", "api"),
    ("Deployment", "web"),
    ("Service", "web"),
    ("HorizontalPodAutoscaler", "web"),
    ("PodDisruptionBudget", "web"),
    ("HTTPRoute", "site"),
    ("Certificate", "site-tls"),
    ("NetworkPolicy", "internal"),
}


def _render(*args: str) -> Any:
    return CliRunner().invoke(cli, ["render", *args])


def _documents(env: str) -> dict[tuple[str, str], dict[str, Any]]:
    result = _render(f"{MODULE}:app", "--env", env)
    assert result.exit_code == 0, result.output
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


@pytest.fixture(scope="module")
def rendered() -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    return {env: _documents(env) for env in ENVIRONMENTS}


def _pod(doc: dict[str, Any]) -> dict[str, Any]:
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def test_each_environment_renders_its_objects_in_its_namespace(rendered) -> None:
    for env, documents in rendered.items():
        expected = ALWAYS | ({("Ingress", "site")} if env == "dev" else set())
        assert set(documents) == expected, env
        namespaces = {doc["metadata"].get("namespace") for doc in documents.values()}
        assert namespaces == {f"reference-{env}"}


def test_environment_values(rendered) -> None:
    def hpa(env: str) -> tuple[int, int]:
        spec = rendered[env][("HorizontalPodAutoscaler", "web")]["spec"]
        return spec["minReplicas"], spec["maxReplicas"]

    assert {env: hpa(env) for env in ENVIRONMENTS} == {
        "dev": (1, 2),
        "staging": (2, 6),
        "prod": (3, 10),
    }
    replicas = {
        env: rendered[env][("Deployment", "api")]["spec"]["replicas"]
        for env in ENVIRONMENTS
    }
    assert replicas == {"dev": 1, "staging": 1, "prod": 3}
    # The autoscaler owns web's replica count in every environment: web
    # renders its min_replicas as the initial size only.
    assert {
        env: rendered[env][("Deployment", "web")]["spec"]["replicas"]
        for env in ENVIRONMENTS
    } == {"dev": 1, "staging": 2, "prod": 3}
    levels = {
        env: rendered[env][("ConfigMap", "settings")]["data"]["LOG_LEVEL"]
        for env in ENVIRONMENTS
    }
    assert levels == {"dev": "debug", "staging": "info", "prod": "warning"}
    hosts = {
        env: rendered[env][("HTTPRoute", "site")]["spec"]["hostnames"]
        for env in ENVIRONMENTS
    }
    assert hosts == {
        "dev": ["dev.reference.example.com"],
        "staging": ["staging.reference.example.com"],
        "prod": ["reference.example.com", "www.reference.example.com"],
    }
    for env in ENVIRONMENTS:
        certificate = rendered[env][("Certificate", "site-tls")]
        assert certificate["apiVersion"] == "cert-manager.io/v1"
        assert certificate["spec"]["dnsNames"] == hosts[env]
        assert certificate["spec"]["privateKey"] == {"algorithm": "ECDSA", "size": 256}
    issuers = {
        env: rendered[env][("Certificate", "site-tls")]["spec"]["issuerRef"]["name"]
        for env in ENVIRONMENTS
    }
    assert issuers == {
        "dev": "self-signed",
        "staging": "letsencrypt-staging",
        "prod": "letsencrypt",
    }
    web = _pod(rendered["prod"][("Deployment", "web")])["containers"][0]
    assert web["resources"]["requests"]["cpu"] == "100m"


def test_every_pod_is_restricted_pinned_and_sized(rendered) -> None:
    for env, documents in rendered.items():
        for (kind, name), doc in documents.items():
            if kind not in WORKLOADS:
                continue
            pod = _pod(doc)
            assert pod["securityContext"] == {
                "fsGroup": 10001,
                "runAsGroup": 10001,
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "seccompProfile": {"type": "RuntimeDefault"},
            }, (env, kind, name)
            for container in pod["containers"]:
                assert re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", container["image"])
                assert container["securityContext"] == {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                }
                assert container["resources"]["requests"]["cpu"]
            # Only the api is bound to a declared account and gets a token.
            bound = (kind, name) == ("Deployment", "api")
            assert pod["automountServiceAccountToken"] is bound, (env, kind, name)


def test_store_keeps_its_data_and_reads_its_secret(rendered) -> None:
    store = rendered["prod"][("StatefulSet", "store")]
    assert store["spec"]["serviceName"] == "store"
    assert store["spec"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Retain",
        "whenScaled": "Retain",
    }
    (claim,) = store["spec"]["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "data"
    assert rendered["prod"][("Service", "store")]["spec"]["clusterIP"] == "None"
    secret = rendered["prod"][("Secret", "store-credentials-1")]
    assert secret["data"] == dict.fromkeys(
        ("password", "redis.conf", "url"), "<redacted>"
    )
    policy = rendered["prod"][("NetworkPolicy", "internal")]["spec"]
    selector = {"app.kubernetes.io/part-of": "reference"}
    assert policy["podSelector"] == {"matchLabels": selector}
    assert policy["ingress"] == [{"from": [{"podSelector": {"matchLabels": selector}}]}]


def test_staging_to_prod_diff_is_typed() -> None:
    result = _render(
        f"{MODULE}:app", "--env", "staging", "--diff-env", "prod", "--format", "json"
    )
    assert result.exit_code == 0, result.output
    diff = json.loads(result.stdout)
    assert diff["state"] == "diffed"
    assert diff["namespaces"] == {"a": "reference-staging", "b": "reference-prod"}
    changed = {f"{o['kind']}/{o['name']}": o for o in diff["objects"]}
    assert set(changed) == {
        "Deployment/api",
        "Deployment/web",
        "HorizontalPodAutoscaler/web",
        "Certificate/site-tls",
        "HTTPRoute/site",
        "ConfigMap/settings",
    }
    assert diff["summary"] == {"changed": 6, "only-a": 0, "only-b": 0, "same": 13}
    assert changed["HorizontalPodAutoscaler/web"]["fields"] == [
        {"path": "spec.maxReplicas", "a": 6, "b": 10},
        {"path": "spec.minReplicas", "a": 2, "b": 3},
    ]
    assert {"path": "spec.replicas", "a": 1, "b": 3} in changed["Deployment/api"][
        "fields"
    ]
    assert {
        "path": "autoscalers.web",
        "b": {"max_replicas": 10, "min_replicas": 3},
    } in (diff["values"])
    text = _render(f"{MODULE}:app", "--env", "staging", "--diff-env", "prod")
    assert text.exit_code == 0, text.output
    assert "~ HorizontalPodAutoscaler/web (component web):" in text.stdout
    assert "RoleBinding" not in text.stdout  # the namespace is not a difference
    assert text.stdout.endswith(
        "6 changed, 0 only in staging, 0 only in prod, 13 identical\n"
    )
    dev = _render(f"{MODULE}:app", "--env", "dev", "--diff-env", "staging")
    assert "- Ingress/site (component dev-ingress): only in dev" in dev.stdout


def test_the_pipeline_renders_each_target_with_secret_bindings() -> None:
    result = _render(f"{MODULE}:pipeline", "--env", "prod", "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["namespace"], payload["environment"]) == ("reference-prod", "prod")
    bindings = {
        binding["pointer"]: binding["input"]
        for component in payload["components"]
        for resource in component["resources"]
        for binding in resource["secret_bindings"]
    }
    assert bindings == {
        "/data/password": "store_password",
        "/data/url": "store_url",
        "/data/redis.conf": "store_conf",
    }
    result = _render(f"{MODULE}:dev")
    assert result.exit_code == 0, result.output
    assert "namespace: reference-dev" in result.stdout


def test_the_module_is_typed_end_to_end() -> None:
    app = load_target(f"{MODULE}:app", ROOT)
    pipeline = load_target(f"{MODULE}:pipeline", ROOT)
    assert isinstance(app, App) and isinstance(pipeline, Pipeline)
    assert not app._overrides  # no untyped patches
    resources = [item for item in app.objects if isinstance(item, Resource)]
    assert resources and all(isinstance(r.spec, BaseModel) for r in resources)
    for env in app.environments:
        assert all(isinstance(spec, BaseModel) for spec in env.specs.values())
    assert sorted(pipeline.targets) == ["dev", "prod", "staging"]
    assert {spec.type for spec in pipeline.secrets.values()} == {"sops", "template"}
    assert [check.target for check in pipeline.checks] == [
        "service/web",
        "statefulset/store",
    ]
    assert pipeline.rollback_on_failed_checks


def test_the_certificate_module_is_generated_from_the_vendored_crd() -> None:
    fixture = ROOT / "tests" / "fixtures" / "crds" / "cert-manager-certificate.yaml"
    module = generate(load_crds(fixture.read_text())[0])
    assert module.source == (EXAMPLE / "crds" / "certificate.py").read_text(), (
        "regenerate: piceli codegen crd "
        "tests/fixtures/crds/cert-manager-certificate.yaml "
        "--out examples/reference/crds/certificate.py"
    )


def test_the_sops_file_is_encrypted_for_the_example_only_key() -> None:
    key = (EXAMPLE / "secrets" / "example-only.agekey").read_text()
    assert key.startswith("# EXAMPLE ONLY")
    public = re.search(r"^# public key: (age1[0-9a-z]+)$", key, re.MULTILINE)
    assert public is not None
    document = yaml.safe_load((EXAMPLE / "secrets" / "example.enc.yaml").read_text())
    assert document["store"]["password"].startswith("ENC[AES256_GCM,")
    assert [item["recipient"] for item in document["sops"]["age"]] == [public[1]]
    rules = yaml.safe_load((EXAMPLE / ".sops.yaml").read_text())
    assert rules["creation_rules"][0]["age"] == public[1]
