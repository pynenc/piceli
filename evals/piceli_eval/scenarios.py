"""Workspaces for ``session`` tasks: files, and a fake cluster in a known state.

The cluster is Piceli's own in-process fake Kubernetes API (``piceli.testing``),
served on loopback for the duration of one answer. The workspace gets the
task's ``app.py`` and a kubeconfig for that server only, so every command the
model writes runs against it and nothing else.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from piceli_eval.sandbox import Sandbox, approval_hash

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
APP_FIXTURE = "shop_pipeline.py"
DIGEST_CURRENT = '"2" * 64'
DIGEST_PREVIOUS = '"1" * 64'


def namespace() -> str:
    """The namespace every fake cluster serves."""
    from piceli.testing import TARGET

    return str(TARGET.namespace)


def fixture_text(name: str) -> str:
    """A fixture file with ``{namespace}`` filled in."""
    return (FIXTURES / name).read_text().replace("{namespace}", namespace())


class ScenarioError(RuntimeError):
    """The harness could not prepare a scenario (a harness bug, not a model error)."""


def _deploy(sandbox: Sandbox) -> None:
    planned = sandbox.piceli("deploy", "app.py:pipeline", "--plan", "--json")
    shown = approval_hash(planned)
    if planned.returncode != 0 or shown is None:
        raise ScenarioError(f"scenario plan failed: {planned.stderr[-800:]}")
    applied = sandbox.piceli("deploy", "app.py:pipeline", "--approve", shown, "--json")
    if applied.returncode != 0:
        raise ScenarioError(f"scenario deploy failed: {applied.stderr[-800:]}")


def _kubectl_objects(api: Any) -> None:
    """``web`` created by ``kubectl apply`` before Piceli managed it."""
    labels = {"app.kubernetes.io/name": "web"}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": namespace(), "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "registry.example/shop/web:1.4",
                            "ports": [{"containerPort": 8080}],
                        }
                    ]
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "web", "namespace": namespace(), "labels": labels},
        "spec": {
            "selector": labels,
            "ports": [{"port": 80, "targetPort": 8080}],
        },
    }
    api.field_ownership = True
    for manifest in (deployment, service):
        applied = copy.deepcopy(manifest)
        api.put(manifest, managers=[("kubectl-client-side-apply", "Update", applied)])
    # Checks compare against these: an adopted object keeps its UID, a
    # replaced one does not.
    api.seed_uids = {
        key: api.objects[key]["metadata"]["uid"]
        for key in (("Deployment", "web"), ("Service", "web"))
    }


def _fresh(sandbox: Sandbox, api: Any) -> None:
    return None


def _two_releases(sandbox: Sandbox, api: Any) -> None:
    app = sandbox.ws / "app.py"
    current = app.read_text()
    app.write_text(current.replace(DIGEST_CURRENT, DIGEST_PREVIOUS))
    _deploy(sandbox)
    app.write_text(current)
    _deploy(sandbox)


def _unmanaged(sandbox: Sandbox, api: Any) -> None:
    _kubectl_objects(api)


SCENARIOS: dict[str, Callable[[Sandbox, Any], None]] = {
    "fresh": _fresh,
    "two-releases": _two_releases,
    "unmanaged-objects": _unmanaged,
}


@contextmanager
def prepared(name: str, sandbox: Sandbox) -> Iterator[Any]:
    """Prepare scenario ``name`` in ``sandbox``; yields the fake API (or ``None``)."""
    if name == "none":
        yield None
        return
    if name not in SCENARIOS:
        raise ScenarioError(f"unknown scenario {name!r}")
    from piceli.testing import serve, write_kubeconfig

    with serve() as (api, url):
        write_kubeconfig(url, sandbox.ws / "cluster.kubeconfig", context="test")
        (sandbox.ws / "app.py").write_text(fixture_text(APP_FIXTURE))
        SCENARIOS[name](sandbox, api)
        yield api


def refusal(sandbox: Sandbox) -> dict[str, Any]:
    """The refusal ``piceli deploy --plan`` prints in ``unmanaged-objects``."""
    with prepared("unmanaged-objects", sandbox):
        result = sandbox.piceli("deploy", "app.py:pipeline", "--plan", "--json")
    data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return data
