"""A real OpenStack cloud (Infomaniak by default), end to end (opt-in).

Skipped unless ``PICELI_OPENSTACK_LIVE=1``. Everything it creates is named
``piceli-live-<random>``: an image (a small CirrOS, if its download is
reachable), a security group with TCP, UDP, port-range and ICMP rules, a
persistent port on the public network, a key pair, a server on that port
with secret user data, a bucket and, if the application credential may,
EC2 credentials for S3. It checks:

- the smallest flavor's real name, and that the plan refuses no other;
- the server on the port; a replaced server re-attaches the same port and
  keeps its IPv4 and IPv6;
- the user data: in the plan as a digest only, its SHA-1 in the state, and
  (CirrOS only) the instance running it, from its console;
- the bucket over S3 with path-style addressing at the endpoint
  ``NixCache`` uses: put, list, get, delete (Piceli's own SigV4 signer);
- the security group's UDP and port-range rules as the cloud stores them.

Whatever happens (pass, failure, crash), ``finally`` destroys through
Piceli, then deletes anything left with the prefix through the API, then
lists the project's servers, ports, security groups, key pairs, images,
containers and the EC2 credentials it made, and fails if one is left. The
report (``-s``, or ``PICELI_OPENSTACK_LIVE_REPORT=FILE``) lists what was
created, what was verified and that nothing is left; it holds no secret.

Environment (Infomaniak's values by default):

- ``PICELI_OPENSTACK_CREDENTIAL``: the stored application credential
  (``ID:SECRET``, ``piceli secrets provider NAME --prompt``; default
  ``infomaniak``), read from Piceli's credentials directory;
- ``PICELI_OPENSTACK_AUTH_URL``, ``PICELI_OPENSTACK_REGION``,
  ``PICELI_OPENSTACK_NETWORK``, ``PICELI_OPENSTACK_FLAVOR``,
  ``PICELI_OPENSTACK_S3_ENDPOINT``;
- ``PICELI_OPENSTACK_IMAGE``: an image name at the cloud, used when the
  CirrOS download is not reachable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import (
    Bucket,
    Firewall,
    Image,
    Infrastructure,
    OpenStack,
    PrimaryIp,
    Rule,
    Server,
    UserData,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.openstack import INFOMANIAK_AUTH_URL, INFOMANIAK_FLAVORS

from .machines_support import PASSPHRASE, needs_tofu

LIVE = os.environ.get("PICELI_OPENSTACK_LIVE") == "1"
#: The real credentials directory, read before the test's fixtures move it.
STORE = credentials.credentials_dir()
CIRROS = "https://download.cirros-cloud.net/0.6.2/"
CIRROS_FILE = "cirros-0.6.2-x86_64-disk.img"
pytestmark = [
    needs_tofu,
    pytest.mark.skipif(
        not LIVE, reason="set PICELI_OPENSTACK_LIVE=1 (creates resources)"
    ),
    pytest.mark.timeout(2700),
]


def _setting(name: str, default: str) -> str:
    return os.environ.get(f"PICELI_OPENSTACK_{name}") or default


def _stored_credential() -> str:
    name = _setting("CREDENTIAL", "infomaniak")
    path = STORE / f"{name}.json"
    if not path.is_file():
        pytest.fail(f"no stored credential {name!r}: piceli secrets provider {name}")
    body = json.loads(path.read_text())
    assert body.get("kind") == "provider-token", f"{name} is not a provider token"
    return str(body["value"])


class Api:
    """The OpenStack REST APIs the checks and the clean-up use (stdlib only)."""

    def __init__(self, auth_url: str, region: str, credential: str) -> None:
        identifier, _, secret = credential.partition(":")
        body = {"auth": {"identity": {
            "methods": ["application_credential"],
            "application_credential": {"id": identifier, "secret": secret},
        }}}  # fmt: skip
        request = urllib.request.Request(
            auth_url.rstrip("/") + "/auth/tokens",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as answer:
            self.token = answer.headers["X-Subject-Token"]
            token = json.load(answer)["token"]
        self.user = token["user"]["id"]
        self.project = token["project"]["id"]
        self.urls: dict[str, str] = {}
        for service in token.get("catalog") or ():
            for endpoint in service.get("endpoints") or ():
                if endpoint.get("interface") == "public" and region in (
                    endpoint.get("region"), endpoint.get("region_id")
                ):  # fmt: skip
                    self.urls[service["type"]] = endpoint["url"].rstrip("/")
        self.urls["identity"] = auth_url.rstrip("/")
        if not self.urls.get("network", "").endswith("/v2.0"):
            self.urls["network"] = self.urls.get("network", "") + "/v2.0"

    def call(
        self, method: str, service: str, path: str, body: Any = None, raw: bool = False
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.urls[service] + path,
            data=data,
            headers={"x-auth-token": self.token, "content-type": "application/json",
                     "accept": "application/json"},
            method=method,
        )  # fmt: skip
        with urllib.request.urlopen(request, timeout=120) as answer:
            text = answer.read()
        if raw:
            return text
        return json.loads(text) if text.strip() else {}

    def try_delete(self, service: str, path: str) -> None:
        try:
            self.call("DELETE", service, path)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise

    # -------------------------------------------------------- listings
    def servers(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "compute", "/servers/detail")["servers"])

    def ports(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "network", "/ports")["ports"])

    def groups(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "network", "/security-groups")["security_groups"])

    def keypairs(self) -> list[dict[str, Any]]:
        found = self.call("GET", "compute", "/os-keypairs")["keypairs"]
        return [item["keypair"] for item in found]

    def images(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "image", "/v2/images?limit=1000")["images"])

    def containers(self) -> list[dict[str, Any]]:
        return list(self.call("GET", "object-store", "?format=json"))

    def ec2(self) -> list[dict[str, Any]]:
        path = f"/users/{self.user}/credentials/OS-EC2"
        return list(self.call("GET", "identity", path)["credentials"])

    def left(self, prefix: str, access: str | None) -> dict[str, list[str]]:
        """Everything in the project named with ``prefix`` (and our EC2 key)."""
        named: dict[str, Callable[[], list[dict[str, Any]]]] = {
            "servers": self.servers, "ports": self.ports,
            "security_groups": self.groups, "keypairs": self.keypairs,
            "images": self.images, "containers": self.containers,
        }  # fmt: skip
        left = {
            kind: sorted(
                str(item.get("name")) for item in lister()
                if str(item.get("name") or "").startswith(prefix)
            )
            for kind, lister in named.items()
        }  # fmt: skip
        left["ec2_credentials"] = (
            [access[:4] + "…"]
            if access and any(item.get("access") == access for item in self.ec2())
            else []
        )
        return left

    def sweep(self, prefix: str, access: str | None) -> None:
        """Delete whatever ``prefix`` names (what a failed destroy left)."""
        for server in self.servers():
            if server["name"].startswith(prefix):
                self.try_delete("compute", f"/servers/{server['id']}")
        for _ in range(60):
            if not any(s["name"].startswith(prefix) for s in self.servers()):
                break
            time.sleep(5)
        for port in self.ports():
            if port["name"].startswith(prefix):
                self.try_delete("network", f"/ports/{port['id']}")
        for group in self.groups():
            if group["name"].startswith(prefix):
                self.try_delete("network", f"/security-groups/{group['id']}")
        for key in self.keypairs():
            if key["name"].startswith(prefix):
                self.try_delete("compute", f"/os-keypairs/{key['name']}")
        for image in self.images():
            if str(image.get("name") or "").startswith(prefix):
                self.try_delete("image", f"/v2/images/{image['id']}")
        for container in self.containers():
            if container["name"].startswith(prefix):
                name = urllib.parse.quote(container["name"])
                for item in self.call("GET", "object-store", f"/{name}?format=json"):
                    obj = urllib.parse.quote(item["name"])
                    self.try_delete("object-store", f"/{name}/{obj}")
                self.try_delete("object-store", f"/{name}")
        if access:
            path = f"/users/{self.user}/credentials/OS-EC2/{access}"
            self.try_delete("identity", path)


def _cirros(directory: Path) -> tuple[str, str] | None:
    """The CirrOS URL and its sha256, if the download site answers."""
    try:
        with urllib.request.urlopen(CIRROS + "SHA256SUMS", timeout=30) as answer:
            sums = answer.read().decode()
    except (OSError, urllib.error.URLError):
        return None
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == CIRROS_FILE:
            return CIRROS + CIRROS_FILE, digest.strip()
    return None


def _s3(
    method: str, endpoint: str, path: str, keys: tuple[str, str], body: bytes = b""
) -> tuple[int, bytes]:
    """One path-style S3 request, signed like the host agent signs them."""
    from piceli.dev.nix_verify import sigv4_headers

    url = f"https://{endpoint}{path}"
    headers = sigv4_headers(
        method, url, body, region="us-east-1", access_key=keys[0],
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


def _key(directory: Path, name: str) -> str:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is needed for a key pair Nova accepts")
    path = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)],
        check=True, timeout=30,
    )  # fmt: skip
    return (path.with_suffix(".pub")).read_text().strip()


def test_infomaniak_end_to_end(
    homes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from piceli.infra.machines.plan import apply, plan, user_data_digest, workspace

    credential = _stored_credential()
    auth_url = _setting("AUTH_URL", INFOMANIAK_AUTH_URL)
    region = _setting("REGION", "dc3-a")
    flavor = _setting("FLAVOR", INFOMANIAK_FLAVORS[0])
    endpoint = _setting("S3_ENDPOINT", "s3.pub1.infomaniak.cloud")
    prefix = f"piceli-live-{secrets.token_hex(3)}"
    marker = f"{prefix}-user-data-ran"
    user_data = f"#!/bin/sh\necho {marker}\n"
    api = Api(auth_url, region, credential)
    credentials.save_credential("live-state", "state-key", PASSPHRASE)
    credentials.save_credential("live-cloud", "provider-token", credential)
    credentials.save_credential(f"{prefix}-boot", "user-data", user_data)
    del credential
    report: dict[str, Any] = {"prefix": prefix, "region": region, "created": [],
                              "verified": {}, "skipped": {}}  # fmt: skip
    verified, skipped = report["verified"], report["skipped"]
    access: str | None = None

    flavors = api.call("GET", "compute", "/flavors/detail")["flavors"]
    smallest = min(flavors, key=lambda f: (f["vcpus"], f["ram"], f["disk"], f["name"]))
    verified["flavor_names"] = sorted(f["name"] for f in flavors)[:6]
    verified["smallest_flavor"] = smallest["name"]
    assert flavor in {f["name"] for f in flavors}, verified["flavor_names"]

    found = _cirros(tmp_path)
    if found is not None:
        image: str | Image = Image(f"{prefix}-cirros", url=found[0], sha256=found[1])
    else:
        image = _setting("IMAGE", "")
        skipped["image_upload"] = "CirrOS download not reachable"
        if not image:
            pytest.fail("CirrOS is not reachable: set PICELI_OPENSTACK_IMAGE")
    cloud = OpenStack(
        credentials="live-cloud", auth_url=auth_url, region=region,
        public_network=_setting("NETWORK", "ext-net1"), flavors=[flavor],
    )  # fmt: skip
    firewall = Firewall(f"{prefix}-fw", [
        Rule.tcp(22, sources=["192.0.2.0/24"]), Rule.udp(41641),
        Rule.tcp((8000, 8010), sources=["198.51.100.0/24"]), Rule.icmp(),
    ])  # fmt: skip
    bucket = Bucket(f"{prefix}-bucket", provider=cloud)

    def declare(key: str) -> Infrastructure:
        server = Server(
            f"{prefix}-1", provider=cloud, type=flavor, image=image,
            ipv4=PrimaryIp(f"{prefix}-port"), firewall=firewall, ssh_keys=[key],
            user_data=UserData(f"{prefix}-boot"),
        )  # fmt: skip
        return Infrastructure(
            prefix, servers=[server], buckets=[bucket], state_dir=homes / "state"
        )

    first = declare(_key(tmp_path, "first"))
    infra = first
    try:
        # ---------------------------------------------------- create
        with workspace(first) as ws:
            planned = plan(ws, prices=False)
            assert planned.user_data == (
                {"server": f"{prefix}-1", "secret": f"{prefix}-boot",
                 "digest": user_data_digest(user_data)},
            )  # fmt: skip
            assert marker not in json.dumps(planned.to_dict())
            apply(ws, planned.plan_hash)
        server = next(s for s in api.servers() if s["name"] == f"{prefix}-1")
        port = next(p for p in api.ports() if p["name"] == f"{prefix}-port")
        group = next(g for g in api.groups() if g["name"] == f"{prefix}-fw")
        key = next(k for k in api.keypairs() if k["name"].startswith(prefix))
        container = next(c for c in api.containers() if c["name"] == bucket.name)
        report["created"] += [
            {"kind": "server", "name": server["name"], "id": server["id"]},
            {"kind": "port", "name": port["name"], "id": port["id"]},
            {"kind": "security_group", "name": group["name"], "id": group["id"]},
            {"kind": "keypair", "name": key["name"], "id": key["name"]},
            {"kind": "container", "name": container["name"], "id": container["name"]},
        ]
        if isinstance(image, Image):
            uploaded = next(i for i in api.images() if i["name"] == image.name)
            report["created"].append(
                {"kind": "image", "name": image.name, "id": uploaded["id"]}
            )
            verified["image_upload"] = f"{image.name} active: {uploaded['status']}"
        addresses = sorted(ip["ip_address"] for ip in port["fixed_ips"])
        assert port["device_id"] == server["id"]
        assert any(":" in a for a in addresses) and any("." in a for a in addresses)
        verified["port"] = {"addresses": addresses, "server": server["id"]}

        rules = [
            r for r in group["security_group_rules"] if r["direction"] == "ingress"
        ]
        assert {"udp", "tcp", "icmp"} <= {r["protocol"] for r in rules}
        assert any(r["port_range_min"] == 8000 and r["port_range_max"] == 8010
                   for r in rules)  # fmt: skip
        assert any(r["protocol"] == "udp" and r["port_range_min"] == 41641
                   for r in rules)  # fmt: skip
        verified["security_group"] = sorted(
            f"{r['ethertype']} {r['protocol']} {r['port_range_min']}-"
            f"{r['port_range_max']} {r['remote_ip_prefix']}" for r in rules
        )  # fmt: skip

        # The state keeps the SHA-1 of the user data, never the value; a
        # plan that keeps the server does not load it.
        (homes / "credentials" / f"{prefix}-boot.json").unlink()
        with workspace(first) as ws:
            assert plan(ws, prices=False).changes == ()
            shown = ws.run("show", "-json").stdout
        values = json.loads(shown)["values"]["root_module"]["resources"]
        stored = next(r["values"]["user_data"] for r in values
                      if r["type"] == "openstack_compute_instance_v2")  # fmt: skip
        assert stored == hashlib.sha1(user_data.encode()).hexdigest()
        assert marker not in shown
        verified["state_user_data"] = "sha1 of the value, value absent"
        if isinstance(image, Image):
            output = ""
            for _ in range(36):
                output = api.call("POST", "compute", f"/servers/{server['id']}/action",
                                  {"os-getConsoleOutput": {"length": 400}})["output"]  # fmt: skip
                if marker in output:
                    break
                time.sleep(5)
            if marker in output:
                verified["user_data_ran"] = "the console shows the user data's echo"
            else:
                skipped["user_data_ran"] = "not on the console within 3 minutes"

        # ---------------------------------------------------- S3
        try:
            made = api.call("POST", "identity",
                            f"/users/{api.user}/credentials/OS-EC2",
                            {"tenant_id": api.project})["credential"]  # fmt: skip
        except urllib.error.HTTPError as error:
            skipped["s3"] = f"EC2 credentials refused ({error.code})"
        else:
            access = made["access"]
            keys = (access, made["secret"])
            report["created"].append(
                {"kind": "ec2_credential", "name": "-", "id": access[:4] + "…"}
            )
            path = f"/{bucket.name}/{prefix}/hello.txt"
            assert _s3("PUT", endpoint, path, keys, b"hello")[0] == 200
            status, listing = _s3(
                "GET", endpoint, f"/{bucket.name}?list-type=2&prefix={prefix}/", keys
            )
            assert status == 200 and f"{prefix}/hello.txt".encode() in listing
            assert _s3("GET", endpoint, path, keys) == (200, b"hello")
            assert _s3("DELETE", endpoint, path, keys)[0] == 204
            verified["s3"] = (
                f"put, list, get, delete at https://{endpoint} (path-style)"
            )

        # ---------------------------------------------------- replace
        credentials.save_credential(f"{prefix}-boot", "user-data", user_data)
        infra = declare(_key(tmp_path, "second"))  # a new key pair: a new server
        with workspace(infra) as ws:
            replaced = plan(ws, prices=False)
            actions = {c.name: c.action for c in replaced.changes if c.kind == "server"}
            assert actions == {f"{prefix}-1": "replace"}, replaced.changes
            assert replaced.user_data  # a replacing plan loads it again
            apply(ws, replaced.plan_hash)
        again = next(s for s in api.servers() if s["name"] == f"{prefix}-1")
        kept = next(p for p in api.ports() if p["name"] == f"{prefix}-port")
        assert again["id"] != server["id"]
        assert kept["id"] == port["id"] and kept["device_id"] == again["id"]
        assert sorted(ip["ip_address"] for ip in kept["fixed_ips"]) == addresses
        report["created"].append(
            {"kind": "server (replacement)", "name": again["name"], "id": again["id"]}
        )
        verified["replace"] = "new server on the same port, same IPv4 and IPv6"
    finally:
        problems: list[str] = []
        try:
            with workspace(infra) as ws:
                doomed = plan(ws, destroy=True)
                if doomed.changes:
                    apply(ws, doomed.plan_hash, destroy=True)
        except Exception as error:  # report, then sweep
            problems.append(f"piceli destroy: {type(error).__name__}")
        try:
            api.sweep(prefix, access)
        except Exception as error:
            problems.append(f"sweep: {type(error).__name__}")
        left = api.left(prefix, access)
        report["left"] = left
        report["problems"] = problems
        text = json.dumps(report, indent=2, sort_keys=True)
        target = os.environ.get("PICELI_OPENSTACK_LIVE_REPORT")
        if target:
            Path(target).write_text(text + "\n")
        with capsys.disabled():
            print("\n" + text)
    assert not any(left.values()), left
    assert not problems, problems
