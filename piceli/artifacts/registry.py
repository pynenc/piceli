"""OCI Distribution v2 client for in-cluster (registry.example:5000) and local streamed delivery.

Eliminates mandatory public registry pushes and disk-heavy intermediate tars by streaming
OCI layers directly to owner-operated registries or containerd workers.
"""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import BinaryIO

from piceli.artifacts.plan import validate_digest

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class RegistryEndpoint:
    """Connection specification for an owner-operated OCI registry (e.g. registry.example:5000)."""

    host: str
    port: int = 5000
    use_tls: bool = False
    auth_token: str | None = None

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{self.host}:{self.port}"


class StreamedOciRegistryClient:
    """Stream blobs and manifests directly to an owner-operated OCI registry without intermediate disk duplication."""

    def __init__(self, endpoint: RegistryEndpoint) -> None:
        self.endpoint = endpoint

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | BinaryIO | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> urllib.request.Request:
        url = f"{self.endpoint.base_url}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if self.endpoint.auth_token:
            req.add_header("Authorization", f"Bearer {self.endpoint.auth_token}")
        return req

    def check_v2_support(self) -> bool:
        """Verify registry endpoint responds to OCI Distribution v2."""
        req = self._request("GET", "/v2/")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status in {200, 401}
        except (urllib.error.URLError, OSError):
            return False

    def has_blob(self, repository: str, digest: str) -> bool:
        """Check if layer or config blob already exists in the repository."""
        validate_digest(digest)
        req = self._request("HEAD", f"/v2/{repository}/blobs/{digest}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return False
            raise

    def push_blob_stream(self, repository: str, stream: BinaryIO, expected_digest: str) -> str:
        """Stream a blob to the registry using monolithic or single-request PUT upload."""
        validate_digest(expected_digest)

        # 1. Initiate upload
        init_req = self._request("POST", f"/v2/{repository}/blobs/uploads/")
        with urllib.request.urlopen(init_req, timeout=10) as resp:
            location = resp.headers.get("Location")
            if not location:
                raise RuntimeError("registry did not provide upload Location")

        # Handle relative or absolute location
        if location.startswith("/"):
            upload_url = location
        else:
            parsed = urllib.parse.urlsplit(location)
            upload_url = parsed.path + ("?" + parsed.query if parsed.query else "")

        # Add digest query param for monolithic upload completion
        sep = "&" if "?" in upload_url else "?"
        upload_path = f"{upload_url}{sep}digest={expected_digest}"

        data = stream.read()
        put_req = self._request(
            "PUT",
            upload_path,
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
        )
        with urllib.request.urlopen(put_req, timeout=30) as resp:
            if resp.status not in {201, 202, 200}:
                raise RuntimeError(f"failed to complete blob upload: status {resp.status}")

        return expected_digest

    def push_manifest(
        self, repository: str, reference: str, manifest_bytes: bytes, media_type: str
    ) -> str:
        """Push an OCI or Docker manifest JSON to the registry."""
        digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        req = self._request(
            "PUT",
            f"/v2/{repository}/manifests/{reference}",
            data=manifest_bytes,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(len(manifest_bytes)),
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in {200, 201}:
                raise RuntimeError(f"failed to push manifest: status {resp.status}")
        return digest
