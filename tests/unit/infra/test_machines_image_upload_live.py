"""Piceli's image upload against a real OpenStack cloud (opt-in, 0.25.3).

Skipped unless ``PICELI_OPENSTACK_LIVE=1``. It uploads a small image named
``piceli-live-<random>-…`` with :func:`piceli.infra.machines.glance.upload_image`:

- from this machine, with the first attempt's connection dropped after
  1 MB (the sender's socket raises): Piceli retries, and the report says
  what Glance left of the dropped attempt (``queued`` or ``saving``);
- from a build host, when ``PICELI_OPENSTACK_LIVE_BUILD_HOST`` names one
  (``ssh://user@host``, with ``PICELI_OPENSTACK_LIVE_BUILD_IMAGE``, the
  run image pinned by digest, ``PICELI_OPENSTACK_LIVE_STORE_FILE``, a small
  image file in its Nix store, and ``PICELI_OPENSTACK_LIVE_STORE_SHA256``);
  otherwise the report says it was not run.

Each upload must become ``active`` with Glance's hashes equal to the bytes
sent. ``finally`` deletes every image with the prefix and fails if one is
left. The report (``-s``) holds no secret. The credential and cloud are the
end-to-end test's (``PICELI_OPENSTACK_CREDENTIAL``, default ``infomaniak``).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import ImageHost
from piceli.infra.machines import glance as glance_module
from piceli.infra.machines.glance import Glance, HostSender, LocalSender, upload_image
from piceli.infra.machines.openstack import IMAGE_SHA_TAG, INFOMANIAK_AUTH_URL

from .test_machines_openstack_live import LIVE, _cirros, _setting, _stored_credential

pytestmark = [
    pytest.mark.skipif(
        not LIVE, reason="set PICELI_OPENSTACK_LIVE=1 (creates resources)"
    ),
    pytest.mark.timeout(1800),
]


def _small_image(directory: Path) -> tuple[Path, str, str]:
    """CirrOS (qcow2) when its site answers, else a small raw disk."""
    found = _cirros(directory)
    if found is not None:
        return found[0], found[1], "qcow2"
    path = directory / "blank.raw"
    path.write_bytes(b"\0" * (8 << 20))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), "raw"


def test_upload_retry_and_build_host_against_the_cloud(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential = _stored_credential()
    identifier, _, secret = credential.partition(":")
    del credential
    glance = Glance(
        _setting("AUTH_URL", INFOMANIAK_AUTH_URL), _setting("REGION", "dc3-a"),
        identifier, secret,
    )  # fmt: skip
    prefix = f"piceli-live-{secrets.token_hex(3)}"
    tags = [f"piceli.io:infra={prefix}", "piceli.io:managed-by=piceli"]
    report: dict[str, Any] = {
        "prefix": prefix,
        "import_methods": glance.import_methods(),
    }
    lines: list[str] = []
    try:
        path, sha, disk_format = _small_image(tmp_path)

        def body(name: str) -> dict[str, Any]:
            return {"name": f"{prefix}-{name}", "container_format": "bare",
                    "disk_format": disk_format, "visibility": "private",
                    "tags": [*tags, f"piceli.io:resource=image-{name}",
                             IMAGE_SHA_TAG + sha]}  # fmt: skip

        # -------------------------------------------- local, first attempt dropped
        original = glance_module.image_sender.send
        attempts = {"n": 0}
        left_after_drop: list[str] = []

        def dropping(
            request: Mapping[str, Any], progress: Any = None, **kw: Any
        ) -> Any:
            attempts["n"] += 1
            if attempts["n"] > 1:
                return original(request, progress, **kw)

            def drop(sent: int) -> None:
                if sent >= 1 << 20:
                    raise ConnectionResetError("dropped by the test")

            return original(request, drop, **kw)

        def note(line: str) -> None:
            lines.append(line)
            if (" is " in line and "deleted" in line) or "back to queued" in line:
                left_after_drop.append(line.split(": ", 1)[1])

        events: list[dict[str, Any]] = []
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(glance_module.image_sender, "send", dropping)
            local = upload_image(
                glance, name="local", sha256=sha, body=body("local"),
                sender=LocalSender(path), tags=tags, log=note,
                progress=lambda e: events.append(dict(e)),
            )  # fmt: skip
        record = glance.get(local.id) or {}
        assert record.get("status") == "active" and local.attempts == 2
        report["local"] = {
            **local.to_dict(), "format": disk_format,
            "os_hash_algo": record.get("os_hash_algo"),
            "after_dropped_attempt": left_after_drop,
            "progress_events": len(events),
        }  # fmt: skip

        # -------------------------------------------- from a build host
        context = os.environ.get("PICELI_OPENSTACK_LIVE_BUILD_HOST")
        if context:
            host = ImageHost(
                "live-build-host", context=context,
                image=os.environ["PICELI_OPENSTACK_LIVE_BUILD_IMAGE"],
                path=os.environ["PICELI_OPENSTACK_LIVE_STORE_FILE"],
            )  # fmt: skip
            host_sha = os.environ["PICELI_OPENSTACK_LIVE_STORE_SHA256"]
            remote = upload_image(
                glance, name="host", sha256=host_sha,
                body={**body("host"), "tags": [*tags, "piceli.io:resource=image-host",
                                               IMAGE_SHA_TAG + host_sha]},
                sender=HostSender(host), tags=tags, log=lines.append,
            )  # fmt: skip
            found = glance.get(remote.id) or {}
            assert found.get("status") == "active"
            report["build_host"] = {**remote.to_dict(),
                                    "os_hash_algo": found.get("os_hash_algo")}  # fmt: skip
        else:
            report["build_host"] = "not run: PICELI_OPENSTACK_LIVE_BUILD_HOST unset"
    finally:
        problems = []
        for image in glance.find(["piceli.io:managed-by=piceli"]):
            if str(image.get("name") or "").startswith(prefix):
                try:
                    glance.delete(str(image["id"]))
                except Exception as error:  # report, then fail
                    problems.append(f"delete {image['id']}: {error}")
        left = [
            str(i.get("name")) for i in glance.find(["piceli.io:managed-by=piceli"])
            if str(i.get("name") or "").startswith(prefix)
        ]  # fmt: skip
        report["left"], report["problems"] = left, problems
        report["log"] = lines
        text = json.dumps(report, indent=2, sort_keys=True)
        assert secret not in text
        with capsys.disabled():
            print("\n" + text)
    assert not left and not problems
