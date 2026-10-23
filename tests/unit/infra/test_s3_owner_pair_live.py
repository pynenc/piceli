"""An owner-made S3 pair against a real cloud (opt-in, 0.25.3).

Skipped unless ``PICELI_OPENSTACK_LIVE_S3=NAME`` names a stored pair
(``piceli secrets s3 NAME --prompt``) that the owner made by hand, for a
cloud whose API lets only the account's own user create S3 credentials
(Infomaniak): ``openstack ec2 credentials create``, signed in as the
project's user.

With the endpoint, region and path-style addressing ``NixCache`` uses (the
``Infomaniak`` preset's endpoint; ``PICELI_OPENSTACK_S3_ENDPOINT``
overrides it), signed as the host agent signs, the pair creates a bucket
``piceli-live-s3-<random>``, puts an object under a prefix, lists it, gets
it back and deletes it, then deletes the bucket. ``finally`` deletes the
objects and the bucket whatever happened, then checks that the bucket is
gone; the report (``-s``) holds names and statuses only, never a key.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import urllib.error
import urllib.request
from typing import Any

import pytest

from piceli.infra import Bucket, Infomaniak, NixCache
from piceli.infra.machines import credentials

NAME = os.environ.get("PICELI_OPENSTACK_LIVE_S3", "")
#: The real credentials directory, read before any fixture moves it.
STORE = credentials.credentials_dir()
PUBLIC = "live-cache-1:" + "A" * 43 + "="
pytestmark = [
    pytest.mark.skipif(
        not NAME,
        reason="set PICELI_OPENSTACK_LIVE_S3=NAME, a stored owner-made S3 pair "
        "(creates and deletes a bucket)",
    ),
    pytest.mark.timeout(600),
]


def _pair(name: str) -> tuple[str, str]:
    """The stored pair (by part, as ``piceli secrets s3`` stores it)."""
    path = STORE / f"{name}.json"
    if not path.is_file():
        pytest.fail(f"no stored credential {name!r}: piceli secrets s3 {name} --prompt")
    body = json.loads(path.read_text())
    assert body.get("kind") == "s3-keys", f"{name} is not an S3 pair"
    parts = body.get("parts")
    if isinstance(parts, dict):
        return parts["access_key_id"], parts["secret_access_key"]
    access, _, secret = str(body["value"]).partition(":")
    return access, secret


def _s3(
    method: str, cache: NixCache, path: str, keys: tuple[str, str], body: bytes = b""
) -> tuple[int, bytes]:
    """One path-style request at the cache's endpoint, signed like the agent's."""
    from piceli.dev.nix_verify import sigv4_headers

    url = f"https://{cache.endpoint}{path}"
    headers = sigv4_headers(
        method, url, body, region=cache.region, access_key=keys[0],
        secret_key=keys[1], now=dt.datetime.now(dt.UTC),
    )  # fmt: skip
    request = urllib.request.Request(
        url, data=body if method == "PUT" else None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_an_owner_made_pair_reads_and_writes_a_bucket() -> None:
    keys = _pair(NAME)
    name = f"piceli-live-s3-{secrets.token_hex(3)}"
    # The cache as the owner declares it: the preset's bucket, the pair by name.
    cache = NixCache(
        PUBLIC, bucket=Bucket(name, provider=Infomaniak(credentials="live-cloud")),
        write_credentials=NAME,
        endpoint=os.environ.get("PICELI_OPENSTACK_S3_ENDPOINT") or None,
    )  # fmt: skip
    prefix = f"{name}/probe"
    key = f"/{name}/{prefix}/hello.txt"
    listing = f"/{name}?list-type=2&prefix={prefix}/"
    report: dict[str, Any] = {"bucket": name, "endpoint": cache.endpoint,
                              "statuses": {}}  # fmt: skip
    statuses = report["statuses"]
    created = False
    try:
        statuses["create_bucket"] = _s3("PUT", cache, f"/{name}", keys)[0]
        assert statuses["create_bucket"] == 200, report
        created = True
        statuses["put"] = _s3("PUT", cache, key, keys, b"hello")[0]
        assert statuses["put"] == 200, report
        statuses["list"], listed = _s3("GET", cache, listing, keys)
        assert statuses["list"] == 200, report
        assert f"{prefix}/hello.txt".encode() in listed
        statuses["get"], got = _s3("GET", cache, key, keys)
        assert (statuses["get"], got) == (200, b"hello"), report
        statuses["delete"] = _s3("DELETE", cache, key, keys)[0]
        assert statuses["delete"] == 204, report
        statuses["delete_bucket"] = _s3("DELETE", cache, f"/{name}", keys)[0]
        assert statuses["delete_bucket"] == 204, report
        created = False
    finally:
        if created:
            _s3("DELETE", cache, key, keys)
            statuses["cleanup_bucket"] = _s3("DELETE", cache, f"/{name}", keys)[0]
        statuses["left"] = _s3("GET", cache, listing, keys)[0]
        print("\n" + json.dumps(report, indent=2, sort_keys=True))
    assert statuses["left"] == 404, f"bucket {name} is still there: {report}"
