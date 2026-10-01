"""Disposable forward-mode UI (composition views) over the fake Kubernetes API.

The server is what ``piceli ui forward-serve`` runs in its pod, with a fake
status channel in a temporary directory; nothing reads a cluster.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from piceli.gitops.state import DirectoryChannel
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.security import uvicorn_log_config
from piceli.services.composition_control import CompositionControl
from piceli.services.logs import LogService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest
from tests.ui_composition_fixture import STATUS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4186)
    options = parser.parse_args()
    token = os.environ.get("PICELI_UI_LAUNCH_TOKEN")
    if not token:
        raise SystemExit("run through tests/browser/run.py (PICELI_UI_LAUNCH_TOKEN)")
    with tempfile.TemporaryDirectory(prefix="piceli-ui-composition-") as directory:
        root = Path(directory)
        channel = DirectoryChannel(root / "gitops")
        channel.publish(STATUS)
        with fake_cluster() as cluster:
            cluster.api.put(manifest("Deployment", "web"), owned=True)
            cluster.api.put(manifest("Deployment", "worker"), owned=True)
            target = KubeconfigTarget(
                cluster.kubeconfig(root / "kubeconfig"),
                "fake",
                cluster.namespace,
                transport="loopback-http",
            )
            query = QueryService([Registration("cluster", "piceli-system", target)])

            @contextmanager
            def open_channel() -> Iterator[DirectoryChannel]:
                yield channel

            app = create_app(
                query,
                origin=f"http://127.0.0.1:{options.port}",
                launch_token=token,
                logs=LogService(query),
                composition_control=CompositionControl(query, "cluster", open_channel),
            )
            print(
                f"Starting Piceli composition UI at http://127.0.0.1:{options.port}/ "
                "(fake data; launch token from the environment)",
                file=sys.stderr,
                flush=True,
            )
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=options.port,
                log_level="warning",
                log_config=uvicorn_log_config(),
            )


if __name__ == "__main__":
    main()
