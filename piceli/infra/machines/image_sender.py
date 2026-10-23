"""Send an image file to Glance, here or on the build host that built it (0.25.3).

Standard library only: ``piceli infra apply`` runs this file's source with
``python3 -I -c`` on the build host that holds the image (a short
``docker run --rm -i`` of the run image, with the host's ``/nix`` mounted
read-only), or calls :func:`run` in its own process for a local file. Both
paths are the same code.

Its input is one JSON object on **stdin** (never an argument or the
environment): ``mode`` (``check`` or ``send``), the file, and for ``send``
the image's upload URL and a short-lived Keystone token. ``check`` reports
whether the file is there, its size and its sha256, before Piceli creates
anything at the cloud. ``send`` streams the file with one
``PUT /v2/images/{id}/file`` (Glance offers no resumable upload), hashing
what it sends (sha256, MD5 and SHA-512: Glance's ``checksum`` and
``os_hash_value``), and prints ``PICELI-IMAGE-PROGRESS {"sent": N}`` at most
every half second, then one ``PICELI-IMAGE-RESULT {json}`` line. Neither
carries the token or anything the server answered beyond its status code.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

PROGRESS_MARKER = "PICELI-IMAGE-PROGRESS "
RESULT_MARKER = "PICELI-IMAGE-RESULT "
CHUNK = 1 << 20
#: Seconds one socket operation may block before the attempt fails.
SOCKET_TIMEOUT = 300.0
#: CA bundles tried when the default context has none (a container of the
#: run image with only the host's ``/nix`` mounted).
CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
    "/nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt",
    "/run/current-system/sw/etc/ssl/certs/ca-bundle.crt",
)


def _context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca"):
        return context
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.isfile(paths.cafile)) or (
        paths.capath and os.path.isdir(paths.capath)
    ):
        return context
    for candidate in (os.environ.get("SSL_CERT_FILE") or "", *CA_FILES):
        if candidate and os.path.isfile(candidate):
            return ssl.create_default_context(cafile=candidate)
    return context


def check(path: str) -> dict[str, Any]:
    """Whether ``path`` is a file, its size and its sha256."""
    if not os.path.isfile(path):
        return {"state": "missing"}
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return {"state": "missing"}
    return {"state": "ok", "bytes": size, "sha256": digest.hexdigest()}


def send(
    request: Mapping[str, Any],
    progress: Callable[[int], None] = lambda _sent: None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """PUT the file to ``request["url"]``; the outcome, never the token.

    ``state``: ``sent`` (Glance answered 2xx; ``sha256``, ``md5``,
    ``sha512`` of the bytes sent), ``failed`` (``why``: ``missing``,
    ``connection``, ``http-<status>``, ``changed``), ``bytes`` sent.
    """
    path = str(request["file"])
    url = urllib.parse.urlsplit(str(request["url"]))
    token = str(request["token"])
    hashes = {name: hashlib.new(name) for name in ("sha256", "md5", "sha512")}
    try:
        size = os.path.getsize(path)
        handle = open(path, "rb")  # noqa: SIM115 - closed below
    except OSError:
        return {"state": "failed", "why": "missing", "bytes": 0}
    timeout = float(request.get("timeout") or SOCKET_TIMEOUT)
    connection: http.client.HTTPConnection
    if url.scheme == "https":
        connection = http.client.HTTPSConnection(
            url.hostname or "", url.port, timeout=timeout, context=_context()
        )
    else:
        connection = http.client.HTTPConnection(
            url.hostname or "", url.port, timeout=timeout
        )
    target = url.path + (f"?{url.query}" if url.query else "")
    sent = 0
    last = clock()
    try:
        with handle:
            connection.putrequest("PUT", target)
            connection.putheader("X-Auth-Token", token)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                for hasher in hashes.values():
                    hasher.update(chunk)
                connection.send(chunk)
                sent += len(chunk)
                now = clock()
                if now - last >= 0.5:
                    last = now
                    progress(sent)
            progress(sent)
            if sent != size:
                connection.close()
                return {"state": "failed", "why": "changed", "bytes": sent}
            answer = connection.getresponse()
            answer.read()
            status = answer.status
    except (OSError, http.client.HTTPException):
        return {"state": "failed", "why": "connection", "bytes": sent}
    finally:
        connection.close()
    if not 200 <= status < 300:
        return {"state": "failed", "why": f"http-{status}", "bytes": sent}
    result: dict[str, Any] = {"state": "sent", "bytes": sent}
    result.update({name: hasher.hexdigest() for name, hasher in hashes.items()})
    return result


def run(
    request: Mapping[str, Any],
    progress: Callable[[int], None] = lambda _sent: None,
) -> dict[str, Any]:
    """One request: ``check`` or ``send``."""
    if request.get("mode") == "check":
        return check(str(request["file"]))
    if request.get("mode") == "send":
        return send(request, progress)
    return {"state": "failed", "why": "invalid"}


def main() -> int:  # pragma: no cover - runs on the build host
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        request = {}

    def report(sent: int) -> None:
        sys.stdout.write(PROGRESS_MARKER + json.dumps({"sent": sent}) + "\n")
        sys.stdout.flush()

    result = run(request if isinstance(request, dict) else {}, report)
    sys.stdout.write(RESULT_MARKER + json.dumps(result, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0 if result.get("state") in ("ok", "sent") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
