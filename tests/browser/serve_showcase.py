"""Disposable UI over the fake Kubernetes API with a Pipeline and GitOps status.

Used by ``make ui-clips`` to record the journeys shown in the docs: no real
cluster, registry or credential is involved, and everything it creates lives in
a temporary directory removed on exit.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

import uvicorn

from piceli import App, Pipeline, Target
from piceli.gitops.state import DirectoryChannel
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.ui_state import (
    private_ui_state_dir,
    remove_launch_token,
    write_launch_token,
)
from piceli.server.app import create_app
from piceli.server.security import uvicorn_log_config
from piceli.services.composition_control import CompositionControl
from piceli.services.environment_control import EnvironmentControl
from piceli.services.pipeline_control import PipelineControl
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest
from tests.ui_composition_fixture import STATUS as COMPOSITION

IMAGE = "registry.example/shop@sha256:" + "a" * 64
SHA = "3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d"
STATUS = {
    "controller": {
        "state": "running",
        "last_poll": "2026-10-01T09:30:00Z",
        "poll_seconds": 60,
    },
    "envs": {
        "main": {
            "namespace": "shop",
            "commit": SHA,
            "deployed_commit": SHA,
            "state": "deployed",
            "health": "healthy",
        },
        "wp-login": {
            "namespace": "shop-wp-login",
            "commit": "b7e4a1c9d2f3081726354a5b6c7d8e9f0a1b2c3d",
            "deployed_commit": "b7e4a1c9d2f3081726354a5b6c7d8e9f0a1b2c3d",
            "state": "deployed",
            "health": "healthy",
            "build": "prebuilt",
        },
        "wp-search": {
            "namespace": "shop-wp-search",
            "commit": "c1d2e3f4a5b60718293a4b5c6d7e8f9001122334",
            "state": "approval-required",
            "health": "unknown",
            "build": "in-cluster",
            "plan_hash": "sha256:" + "d" * 64,
        },
    },
}


@contextmanager
def _channel(directory: Path, status: dict = STATUS):  # type: ignore[no-untyped-def]
    channel = DirectoryChannel(directory)
    channel.publish(status)
    yield channel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4177)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="piceli-ui-showcase-") as directory:
        root = Path(directory)
        with fake_cluster() as cluster:
            cluster.api.put(manifest("Deployment", "api"), owned=True)
            cluster.api.put(manifest("ConfigMap", "settings"))
            kubeconfig = cluster.kubeconfig(root / "kubeconfig")
            target = KubeconfigTarget(
                kubeconfig,
                "fake",
                cluster.namespace,
                transport="loopback-http",
            )
            shop = App("shop")
            web = shop.deployment("web", image=IMAGE, ports=[8080])
            shop.service(web, port=8080)
            pipeline = Pipeline(
                shop,
                Target.kubeconfig(
                    kubeconfig,
                    context="fake",
                    namespace=cluster.namespace,
                    transport="loopback-http",
                ),
                state_dir=root / "pipeline-state",
                execution={
                    "max_seconds": 30,
                    "readiness_seconds": 1,
                    "poll_seconds": 0.05,
                },
            )
            query = QueryService(
                [
                    Registration(
                        "shop",
                        "Shop",
                        target,
                        definition_kind="pipeline",
                        ownership="native",
                    )
                ]
            )
            control = PipelineControl(
                query, "shop", pipeline, "shop.py:pipeline", root / "control"
            )
            environments = EnvironmentControl(
                query,
                "shop",
                controller_target=None,
                channel_factory=lambda: _channel(root / "gitops"),
            )
            given = os.environ.get("PICELI_UI_LAUNCH_TOKEN")
            app = create_app(
                query,
                origin=f"http://127.0.0.1:{options.port}",
                launch_token=given,
                pipeline_control=control,
                environment_control=environments,
                composition_control=CompositionControl(
                    query,
                    "shop",
                    lambda: _channel(root / "composition", COMPOSITION),
                ),
            )
            with ExitStack() as cleanup:
                if given:
                    address = f"http://127.0.0.1:{options.port}/applications"
                else:
                    token_file = write_launch_token(
                        private_ui_state_dir(),
                        options.port,
                        app.state.security.launch_token,
                    )
                    cleanup.callback(remove_launch_token, token_file)
                    address = app.state.security.launch_url()
                print(
                    f"Starting Piceli showcase UI at {address}\nFake data only; Ctrl+C stops.",
                    file=sys.stderr,
                    flush=True,
                )
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=options.port,
                    log_level="info",
                    log_config=uvicorn_log_config(),
                )


if __name__ == "__main__":
    main()
