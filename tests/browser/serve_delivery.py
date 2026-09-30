"""Disposable delivery acceptance server; every mutation uses the real service.

Fixture routes share the application's session, Origin and CSRF middleware.
They select two fixed source revisions and fake readiness, never browser code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import HTTPException
from kubernetes.client import AppsV1Api, CoreV1Api
from pydantic import BaseModel, ConfigDict, Field

from piceli.artifacts.process import ToolPin
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
from piceli.k8s.release_spec import ReleaseSpec
from piceli.server.app import create_app
from piceli.services.contracts import Principal
from piceli.services.evaluation import (
    DockerEvaluator,
    RendererConfig,
    SourceSelection,
    denied_paths,
)
from piceli.services.operations import OperationService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store
from piceli.testing import fake_cluster
from tests.integration.test_release_cli_kind import DIGEST_1

APPLICATIONS = ("shop-desktop", "shop-tablet", "shop-phone")


class SourceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1, le=3)


class Readiness(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ready: bool


def source(name: str, version: int) -> str:
    """These constants are the only source content accepted by fixture routes."""
    return f"""from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

def build(ctx):
    replicas = {version}
    resource = ResourceIntent.from_manifest({{
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {{"name": {name!r}, "namespace": ctx.namespace}},
        "spec": {{"replicas": replicas,
            "selector": {{"matchLabels": {{"app": {name!r}}}}},
            "template": {{
                "metadata": {{"labels": {{"app": {name!r}}}, "annotations": {{"fixture.piceli.dev/version": {str(version)!r}}}}},
                "spec": {{
                    "volumes": [{{"name": "logger-state", "emptyDir": {{}}}}],
                    "containers": [
                        {{"name": "api", "image": ctx.image("api"), "ports": [{{"containerPort": 80}}]}},
                        {{"name": "logger", "image": ctx.image("api"),
                         "command": ["/bin/sh", "-c", "if [ ! -f /run/piceli-test/restarted ]; then touch /run/piceli-test/restarted; echo piceli-previous-marker; exit 1; fi; echo piceli-current-marker; sleep 3600"],
                         "volumeMounts": [{{"name": "logger-state", "mountPath": "/run/piceli-test"}}]}}
                    ]
                }}
            }}
        }}
    }})
    return DeploymentComposition((DeploymentComponent("api", (resource,)),))
"""


@contextmanager
def target(directory: Path) -> Iterator[tuple[dict[str, Any], Any]]:
    kubeconfig = os.environ.get("PICELI_KIND_KUBECONFIG")
    context = os.environ.get("PICELI_KIND_CONTEXT")
    if bool(kubeconfig) != bool(context):
        raise ValueError("kind requires both explicit kubeconfig and context")
    if kubeconfig and context:
        path = Path(kubeconfig).resolve(strict=True)
        client = api_client_from_kubeconfig(path, context)
        api = CoreV1Api(client)
        namespace = "piceli-ui-" + uuid.uuid4().hex[:10]
        try:
            api.create_namespace({"metadata": {"name": namespace}})
            try:
                yield (
                    {
                        "kubeconfig": str(path),
                        "context": context,
                        "namespace": namespace,
                    },
                    None,
                )
            finally:
                api.delete_namespace(namespace)
        finally:
            client.close()
    else:
        with fake_cluster() as cluster:
            yield (
                {
                    "kubeconfig": str(cluster.kubeconfig(directory / "kubeconfig")),
                    "context": "fake",
                    "namespace": cluster.namespace,
                    "cluster_uid": "cluster-uid",
                    "namespace_uid": "namespace-uid",
                    "transport": "loopback-http",
                },
                cluster.api,
            )


def renderer_config(path: Path) -> RendererConfig:
    document = json.loads(path.read_text())
    tool = document.pop("docker")
    socket = Path(document.pop("socket"))
    return RendererConfig(
        **document,
        docker=ToolPin(Path(tool["path"]), tool["sha256"]),
        socket=socket,
    )


def serve(directory: Path, config: RendererConfig, port: int) -> None:
    with target(directory) as (target_spec, fake):
        control = directory / "control"
        registrations = []
        sources = {}
        source_paths = {}
        for name in APPLICATIONS:
            root = (directory / name).resolve()
            root.mkdir()
            path = root / "composition.py"
            path.write_text(source(name, 1))
            source_paths[name] = path
            spec = ReleaseSpec.from_dict(
                {
                    "target": target_spec,
                    "release": {
                        "name": name,
                        "owner": name,
                        "field_manager": name,
                        "composition": "composition.py:build",
                        "state_dir": "state",
                    },
                    "images": {"api": f"docker.io/library/nginx@{DIGEST_1}"},
                    "execution": {
                        "max_seconds": 300 if fake is None else 60,
                        "readiness_seconds": 240 if fake is None else 10,
                        "poll_seconds": 0.2,
                    },
                },
                root,
            )
            registrations.append(Registration.from_release(name, name.title(), spec))
            sources[name] = SourceSelection(
                root,
                ("composition.py",),
                "composition.py:build",
                (*denied_paths(spec), control),
            )
        query = QueryService(registrations)
        from piceli.services.access import AccessService
        from piceli.services.logs import LogService

        kubectl_path = shutil.which("kubectl") if fake is None else None
        access = AccessService(
            query, kubectl=Path(kubectl_path) if kubectl_path else None
        )
        logs = LogService(query)
        operations = OperationService(
            query,
            Store(control / "operations.sqlite"),
            DockerEvaluator(control / "evaluations", config),
            sources,
            principal=Principal(id="local", name="Local user"),
        )
        app = create_app(
            query,
            origin=f"http://127.0.0.1:{port}",
            operations=operations,
            access=access,
            logs=logs,
            launch_token=os.environ["PICELI_UI_LAUNCH_TOKEN"],
        )

        def registered(id: str) -> Path:
            if id not in source_paths:
                raise HTTPException(404)
            return source_paths[id]

        @app.post("/api/v1/__fixture/{id}/source")
        def change_source(id: str, request: SourceVersion) -> dict[str, int]:
            registered(id).write_text(source(id, request.version))
            return {"version": request.version}

        @app.post("/api/v1/__fixture/{id}/readiness")
        def readiness(id: str, request: Readiness) -> dict[str, bool]:
            registered(id)
            if fake is None:
                raise HTTPException(409, "Readiness injection is fake-cluster only")
            fake.ready = request.ready
            return {"ready": fake.ready}

        @app.post("/api/v1/__fixture/{id}/observe")
        def observe(id: str) -> dict[str, Any]:
            registered(id)
            # Assert against Kubernetes, independently of operation DTO outcomes.
            client = api_client_from_kubeconfig(
                Path(target_spec["kubeconfig"]),
                target_spec["context"],
                transport=target_spec.get("transport", "https"),
            )
            try:
                deployment = AppsV1Api(client).read_namespaced_deployment(
                    id, target_spec["namespace"]
                )
                return {
                    "replicas": deployment.spec.replicas,
                    "ready": deployment.status.ready_replicas or 0,
                    "version": deployment.spec.template.metadata.annotations[
                        "fixture.piceli.dev/version"
                    ],
                    "image": deployment.spec.template.spec.containers[0].image,
                }
            finally:
                client.close()

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4178)
    options = parser.parse_args()
    config = renderer_config(Path(os.environ["PICELI_DELIVERY_RENDERER"]))
    with tempfile.TemporaryDirectory(prefix="piceli-delivery-service-") as temporary:
        serve(Path(temporary), config, options.port)


if __name__ == "__main__":
    main()
