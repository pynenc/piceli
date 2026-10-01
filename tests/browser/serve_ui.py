"""Disposable real UI service over the public fake Kubernetes HTTP API."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

import uvicorn

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.ui_state import (
    private_ui_state_dir,
    remove_launch_token,
    write_launch_token,
)
from piceli.server.app import create_app
from piceli.server.security import uvicorn_log_config
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
            # Test runners pass their own token and never print it.
            given = os.environ.get("PICELI_UI_LAUNCH_TOKEN")
            app = create_app(
                QueryService([registration]),
                origin=f"http://127.0.0.1:{options.port}",
                launch_token=given,
            )
            security = app.state.security
            with ExitStack() as cleanup:
                if given:
                    address = (
                        f"http://127.0.0.1:{options.port}/applications "
                        "(launch token from PICELI_UI_LAUNCH_TOKEN)"
                    )
                else:
                    token_file = write_launch_token(
                        private_ui_state_dir(), options.port, security.launch_token
                    )
                    cleanup.callback(remove_launch_token, token_file)
                    address = (
                        f"{security.launch_url()}\n"
                        f"Launch token file (removed on exit): {token_file}"
                    )
                print(
                    f"Starting Piceli fake UI at {address}\n"
                    "Read-only demo; press Ctrl+C to stop.",
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
            assert all(request["method"] == "GET" for request in cluster.api.requests)


if __name__ == "__main__":
    main()
