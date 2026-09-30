"""Explicit projected service-account credential for a single cluster scope."""

from __future__ import annotations

import ipaddress
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from piceli.k8s.ops.provider_factory import KubeconfigTarget


@dataclass(frozen=True)
class InClusterCredential:
    """An installation supplied API URL, CA, token file and namespace."""

    api_server: str
    ca_file: Path
    token_file: Path
    namespace: str
    cluster_uid: str | None = None
    namespace_uid: str | None = None

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.api_server)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("cluster API server must be an explicit HTTPS origin")
        try:
            loopback = ipaddress.ip_address(endpoint.hostname).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            raise ValueError("cluster API server must not be loopback")
        if not (
            self.ca_file.is_absolute()
            and self.token_file.is_absolute()
            and self.ca_file.is_file()
            and self.token_file.is_file()
        ):
            raise ValueError("projected CA and token must be explicit readable files")
        KubeconfigTarget(
            Path("/explicit/incluster"), "piceli-incluster", self.namespace
        )

    def write_kubeconfig(self, destination: Path) -> KubeconfigTarget:
        """Persist only file references, never token bytes, with private mode."""
        if not destination.is_absolute() or destination.is_symlink():
            raise ValueError("credential destination must be an absolute regular path")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.parent.resolve() != destination.parent:
            raise ValueError("credential directory must not follow a symlink")
        os.chmod(destination.parent, 0o700)
        document = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": "piceli-incluster",
                    "cluster": {
                        "server": self.api_server,
                        "certificate-authority": str(self.ca_file),
                    },
                }
            ],
            "users": [
                {
                    "name": "piceli-service-account",
                    "user": {"tokenFile": str(self.token_file)},
                }
            ],
            "contexts": [
                {
                    "name": "piceli-incluster",
                    "context": {
                        "cluster": "piceli-incluster",
                        "user": "piceli-service-account",
                        "namespace": self.namespace,
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory(
            prefix="piceli-incluster-config-", dir=destination.parent
        ) as directory:
            staged = Path(directory) / "config"
            staged.write_text(yaml.safe_dump(document, sort_keys=True))
            staged.chmod(0o600)
            os.replace(staged, destination)
        return KubeconfigTarget(
            destination,
            "piceli-incluster",
            self.namespace,
            cluster_uid=self.cluster_uid,
            namespace_uid=self.namespace_uid,
        )
