"""Acceptance: import a kubectl-made namespace, adopt it, and re-plan.

The fake API server (``piceli.testing``) stands in for the cluster. After the
generated module is adopted with ``--adopt-all-desired`` and applied, a second
plan changes nothing: ConfigMaps, Deployments and NetworkPolicies are
``no-op``; the Service plans ``apply`` only because the server allocated its
cluster IP (a server default the plan still compares), and the Secret plans a
metadata-only ``apply`` because public plans never compare secret values. The
objects' content is unchanged throughout.
"""

from __future__ import annotations

import base64
import copy
import json
import textwrap
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from piceli.importing.clean import scrub, strip_defaults
from piceli.k8s.cli import app as cli
from piceli.k8s.cli.release import app as release
from piceli.k8s.ops.plan import manifest_contains
from piceli.testing import TARGET, FakeAPI, fake_cluster

NS = TARGET.namespace
DIGEST = "sha256:" + "4" * 64
PASSWORD = base64.b64encode(b"kept-across-the-import").decode()


def _stack() -> list[dict[str, Any]]:
    meta = lambda name, **extra: {"name": name, "namespace": NS, **extra}  # noqa: E731
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta("settings"),
            "data": {"mode": "fast"},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": meta("cache-auth"),
            "type": "Opaque",
            "data": {"password": PASSWORD},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": meta("cache-data"),
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta("cache", labels={"app": "cache"}),
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "cache"}},
                "template": {
                    "metadata": {"labels": {"app": "cache"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "redis",
                                "image": "redis:7.2",
                                "ports": [{"containerPort": 6379}],
                                "env": [
                                    {
                                        "name": "MODE",
                                        "valueFrom": {
                                            "configMapKeyRef": {
                                                "name": "settings",
                                                "key": "mode",
                                            }
                                        },
                                    },
                                    {
                                        "name": "PASSWORD",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "cache-auth",
                                                "key": "password",
                                            }
                                        },
                                    },
                                ],
                                "securityContext": {"runAsNonRoot": True},
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/data"}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": "cache-data"},
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta("cache", labels={"app": "cache"}),
            "spec": {
                "ports": [{"port": 6379, "targetPort": 6379}],
                "selector": {"app": "cache"},
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": meta("cache-ingress"),
            "spec": {
                "podSelector": {"matchLabels": {"app": "cache"}},
                "policyTypes": ["Ingress"],
                "ingress": [{"ports": [{"port": 6379, "protocol": "TCP"}]}],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": meta("cache-5d9c7-abcde", labels={"app": "cache"}),
            "spec": {"containers": [{"name": "redis", "image": "redis:7.2"}]},
            "status": {
                "containerStatuses": [
                    {
                        "name": "redis",
                        "imageID": f"docker.io/library/redis@{DIGEST}",
                    }
                ]
            },
        },
    ]


def _seed(api: FakeAPI) -> None:
    api.field_ownership = True
    for item in _stack():
        content = {k: v for k, v in item.items() if k not in {"apiVersion", "kind"}}
        stored = api.put(item, managers=[("kubectl-create", "Update", content)])
        if item["kind"] == "Pod":  # put() replaces status for known kinds only
            api.objects[("Pod", item["metadata"]["name"])]["status"] = item["status"]
        assert stored


def _content(api: FakeAPI) -> dict[tuple[str, str], Any]:
    result = {}
    for (kind, name), value in api.objects.items():
        if kind in {"Namespace", "Pod"}:
            continue
        clean = scrub(value)
        clean["metadata"].pop("annotations", None)
        if kind == "Secret":
            clean["data"] = copy.deepcopy(value["data"])
        result[(kind, name)] = strip_defaults(clean)
    return result


def _spec(tmp_path: Path) -> Path:
    spec = tmp_path / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{NS}"
            transport = "loopback-http"

            [release]
            name = "shop"
            owner = "shop"
            field_manager = "shop"
            composition = "app.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 30
            readiness_seconds = 2
            poll_seconds = 0.05

            [images]
            cache = "docker.io/library/redis@{DIGEST}"

            [secrets.cache-auth-password]
            type = "import"
            secret = {{ name = "cache-auth", key = "password" }}
            """
        )
    )
    return spec


def _release(*args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(release, list(args))
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def test_imported_namespace_is_all_no_op_after_adoption(tmp_path: Path) -> None:
    with fake_cluster() as cluster:
        _seed(cluster.api)
        before = _content(cluster.api)
        cluster.kubeconfig(tmp_path / "kubeconfig")
        imported = CliRunner().invoke(
            cli,
            [
                "import",
                "live",
                "--kubeconfig",
                str(tmp_path / "kubeconfig"),
                "--context",
                "fake",
                "--namespace",
                NS,
                "--transport",
                "loopback-http",
                "--name",
                "shop",
                "--out",
                str(tmp_path / "app.py"),
            ],
        )
        assert imported.exit_code == 0, imported.output
        assert {request["method"] for request in cluster.api.requests} == {"GET"}
        summary = json.loads(imported.stdout)
        assert summary["images"][0]["running_digest_ref"] == (
            f"docker.io/library/redis@{DIGEST}"
        )
        module = (tmp_path / "app.py").read_text()
        assert PASSWORD not in module
        assert "cache-redis = " in module  # the running digest, ready to pin

        spec = _spec(tmp_path)
        code, planned, output = _release(
            "plan", "--spec", str(spec), "--adopt-all-desired"
        )
        assert code == 0, output
        assert planned["summary"] == {"adopt": 5}
        code, applied, output = _release(
            "apply", "--spec", str(spec), "--approve", planned["plan_hash"]
        )
        assert code == 0, output
        assert applied["execution"]["state"] == "ready"
        assert _content(cluster.api) == before  # adoption changed no content

        code, again, output = _release("plan", "--spec", str(spec))
        assert code == 0, output
        operations = {
            (action["kind"], action["name"]): action["operation"]
            for action in again["actions"]
        }
        assert operations == {
            ("ConfigMap", "settings"): "no-op",
            ("Deployment", "cache"): "no-op",
            ("NetworkPolicy", "cache-ingress"): "no-op",
            # the server-allocated cluster IP is still compared (a server
            # default), and secret values are never compared in public
            ("Service", "cache"): "apply",
            ("Secret", "cache-auth"): "apply",
        }
        code, _, output = _release(
            "apply", "--spec", str(spec), "--approve", again["plan_hash"]
        )
        assert code == 0, output
        assert _content(cluster.api) == before

        # every rendered field is exactly what the cluster holds
        for (kind, name), value in before.items():
            live = cluster.api.objects[(kind, name)]
            if kind in {"Secret", "PersistentVolumeClaim"}:
                continue
            assert manifest_contains(
                live, {k: v for k, v in value.items() if k != "metadata"}
            )
