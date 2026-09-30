"""Disposable real UI service over the public fake Kubernetes HTTP API."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import uvicorn

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4177)
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="piceli-ui-browser-") as directory:
        with fake_cluster() as cluster:
            cluster.api.put(manifest("Deployment", "web"), owned=True)
            cluster.api.put(manifest("ConfigMap", "settings"))
            cluster.api.put(
                manifest("Secret", "credential", value="YnJvd3Nlci1wcml2YXRl")
            )
            registration = Registration(
                id="shop",
                name="Shop",
                target=KubeconfigTarget(
                    cluster.kubeconfig(Path(directory) / "kubeconfig"),
                    "fake",
                    cluster.namespace,
                    cluster_uid="cluster-uid",
                    namespace_uid="namespace-uid",
                    transport="loopback-http",
                ),
            )
            app = create_app(
                QueryService([registration]), origin=f"http://127.0.0.1:{options.port}"
            )
            print(
                f"Starting Piceli fake UI at http://127.0.0.1:{options.port}/applications "
                "(read-only demo; press Ctrl+C to stop)",
                file=sys.stderr,
                flush=True,
            )
            uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="info")
            assert all(request["method"] == "GET" for request in cluster.api.requests)


if __name__ == "__main__":
    main()
