"""OpenStack (and the Infomaniak preset): rendering, credentials, ownership, prices."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import (
    Bucket,
    DnsRecord,
    Firewall,
    Hetzner,
    Image,
    Infomaniak,
    Infrastructure,
    OpenStack,
    PrimaryIp,
    Rule,
    Server,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.images import stage_image
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.openstack import OPENSTACK_PIN
from piceli.infra.machines.plan import INVENTORY, Workspace, changes_of, estimate
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record

from .machines_support import PASSPHRASE, needs_tofu

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTests owner@example"
CRED_ID = "0123456789abcdef0123456789abcdef"
CRED_SECRET = "app-credential-secret-" + "s" * 40
OS = OpenStack(
    credentials="cloud",
    auth_url="https://identity.example.net/v3",
    region="region-1",
    public_network="public",
    prices={"small": 4.7},
    ip_prices={"ipv4": 2.0},
    currency="CHF",
)
IMAGE_BYTES = b"not really a disk image\n"
IMAGE_SHA = hashlib.sha256(IMAGE_BYTES).hexdigest()
OURS = {"piceli.io/managed-by": "piceli", "piceli.io/infra": "web"}


def _server(**overrides: Any) -> Server:
    values: dict[str, Any] = {
        "name": "host-1",
        "provider": OS,
        "type": "small",
        "image": "debian-12",
        "ipv4": PrimaryIp("host-1-ip"),
    }
    values.update(overrides)
    return Server(**values)


def _invalid(match: str) -> Any:
    return pytest.raises(InfraError, match=match)


def _resources(infra: Infrastructure) -> dict[str, dict[str, Any]]:
    return render(infra).config["resource"]


def test_a_plain_host_needs_no_cluster_and_boots_on_its_port() -> None:
    server = _server(firewall=[Rule.tcp(443)], ssh_keys=[KEY])
    assert server.cluster is None
    config = render(Infrastructure("web", servers=[server])).config
    instance = config["resource"]["openstack_compute_instance_v2"]["server-host-1"]
    port = "openstack_networking_port_v2.ip-host-1-ip"
    assert instance["network"] == [{"port": "${" + port + ".id}"}]
    assert instance["flavor_name"] == "small"
    assert instance["image_name"] == "debian-12"
    assert "security_groups" not in instance  # a given port carries its groups
    assert instance["metadata"]["piceli.io:managed-by"] == "piceli"
    assert instance["metadata"]["piceli.io:infra"] == "web"
    assert config["data"]["openstack_networking_network_v2"]["public-network"] == {
        "name": "public",
        "external": True,
    }
    outputs = config["output"]["server-host-1"]["value"]
    assert outputs["ipv4"].endswith(".access_ip_v4}")
    assert outputs["ipv6"].endswith(".access_ip_v6}")
    assert config["provider"]["openstack"] == {
        "auth_url": "https://identity.example.net/v3",
        "region": "region-1",
    }


def test_the_port_holds_the_address_and_survives_a_replaced_server() -> None:
    first = _resources(Infrastructure("web", servers=[_server(image="debian-12")]))
    second = _resources(Infrastructure("web", servers=[_server(image="debian-13")]))
    port = first["openstack_networking_port_v2"]["ip-host-1-ip"]
    assert port == second["openstack_networking_port_v2"]["ip-host-1-ip"]
    assert port["network_id"] == (
        "${data.openstack_networking_network_v2.public-network.id}"
    )
    assert port["name"] == "host-1-ip"
    assert "piceli.io:managed-by=piceli" in port["tags"]
    # Both servers attach the same port: the address stays with it.
    for config in (first, second):
        server = config["openstack_compute_instance_v2"]["server-host-1"]
        assert server["network"] == [
            {"port": "${openstack_networking_port_v2.ip-host-1-ip.id}"}
        ]
    assert "lifecycle" not in port


def test_a_protected_port_cannot_be_destroyed() -> None:
    server = _server(ipv4=PrimaryIp("host-1-ip", protect=True))
    port = _resources(Infrastructure("web", servers=[server]))[
        "openstack_networking_port_v2"
    ]["ip-host-1-ip"]
    assert port["lifecycle"] == {"prevent_destroy": True}


def test_a_server_without_a_port_joins_the_public_network() -> None:
    server = _server(ipv4=True, firewall=Firewall("web", [Rule.tcp(443)]))
    resources = _resources(Infrastructure("web", servers=[server]))
    instance = resources["openstack_compute_instance_v2"]["server-host-1"]
    assert instance["network"] == [
        {"uuid": "${data.openstack_networking_network_v2.public-network.id}"}
    ]
    assert instance["security_groups"] == [
        "${openstack_networking_secgroup_v2.firewall-web.name}"
    ]
    assert "openstack_networking_port_v2" not in resources


def test_a_network_id_needs_no_lookup() -> None:
    provider = OpenStack(
        credentials="cloud",
        auth_url="https://identity.example.net/v3",
        region="region-1",
        public_network="2c0f1f6e-8a7b-4c35-9d0e-5f1f3a6b7c8d",
    )
    config = render(Infrastructure("web", servers=[_server(provider=provider)])).config
    assert "data" not in config
    port = config["resource"]["openstack_networking_port_v2"]["ip-host-1-ip"]
    assert port["network_id"] == "2c0f1f6e-8a7b-4c35-9d0e-5f1f3a6b7c8d"


def test_security_group_has_inbound_rules_only_and_keeps_default_egress() -> None:
    firewall = Firewall(
        "web",
        [
            Rule.tcp(443, name="https"),
            Rule.udp((3478, 3479)),
            Rule.icmp(sources=["10.0.0.0/8", "::/0"]),
        ],
    )
    resources = _resources(Infrastructure("web", servers=[_server(firewall=firewall)]))
    group = resources["openstack_networking_secgroup_v2"]["firewall-web"]
    assert group["name"] == "web"
    assert group["delete_default_rules"] is False  # OpenStack's egress rules stay
    assert "piceli.io:infra=web" in group["tags"]
    rules = list(resources["openstack_networking_secgroup_rule_v2"].values())
    assert {rule["direction"] for rule in rules} == {"ingress"}
    seen = {
        (
            rule["protocol"],
            rule["ethertype"],
            rule.get("port_range_min"),
            rule.get("port_range_max"),
            rule["remote_ip_prefix"],
        )
        for rule in rules
    }
    assert seen == {
        ("tcp", "IPv4", 443, 443, "0.0.0.0/0"),
        ("tcp", "IPv6", 443, 443, "::/0"),
        ("udp", "IPv4", 3478, 3479, "0.0.0.0/0"),
        ("udp", "IPv6", 3478, 3479, "::/0"),
        ("icmp", "IPv4", None, None, "10.0.0.0/8"),
        ("ipv6-icmp", "IPv6", None, None, "::/0"),
    }
    for rule in rules:
        assert rule["security_group_id"] == (
            "${openstack_networking_secgroup_v2.firewall-web.id}"
        )
        assert "piceli.io/managed-by=piceli" in rule["description"]
    # The port carries the group (Nova ignores groups for a given port).
    port = resources["openstack_networking_port_v2"]["ip-host-1-ip"]
    assert port["security_group_ids"] == [
        "${openstack_networking_secgroup_v2.firewall-web.id}"
    ]


def test_key_pair() -> None:
    resources = _resources(Infrastructure("web", servers=[_server(ssh_keys=[KEY])]))
    (name, pair), = resources["openstack_compute_keypair_v2"].items()  # fmt: skip
    assert pair["public_key"] == KEY
    assert pair["name"].startswith("web-")
    instance = resources["openstack_compute_instance_v2"]["server-host-1"]
    assert instance["key_pair"] == f"${{openstack_compute_keypair_v2.{name}.name}}"
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyForTests other@example"
    with _invalid("one key pair"):
        _server(ssh_keys=[KEY, other])


def test_image_upload_with_a_checksum(tmp_path: Path) -> None:
    disk = tmp_path / "disk.qcow2"
    disk.write_bytes(IMAGE_BYTES)
    image = Image(
        "nixos-base",
        file=disk,
        sha256=IMAGE_SHA,
        properties={"hw_firmware_type": "uefi"},
    )
    infra = Infrastructure("web", servers=[_server(image=image)])
    rendered = render(infra)
    body = rendered.config["resource"]["openstack_images_image_v2"]["image-nixos-base"]
    assert body["local_file_path"] == f"images/{IMAGE_SHA}.qcow2"
    assert body["disk_format"] == "qcow2" and body["container_format"] == "bare"
    assert body["visibility"] == "private"
    assert body["properties"] == {"hw_firmware_type": "uefi"}
    assert "piceli.io:managed-by=piceli" in body["tags"]
    assert str(tmp_path) not in rendered.config_text  # content-addressed, no path
    instance = rendered.config["resource"]["openstack_compute_instance_v2"][
        "server-host-1"
    ]
    assert instance["image_id"] == "${openstack_images_image_v2.image-nixos-base.id}"
    with _invalid("sha256"):
        Image("nixos-base", file=disk)  # type: ignore[call-arg]
    with _invalid("sha256"):
        Image("nixos-base", file=disk, sha256="abc")
    with _invalid("one of file= or url="):
        Image("nixos-base", file=disk, url="https://example.com/x", sha256=IMAGE_SHA)
    with _invalid("cannot upload images"):
        Server(
            "edge-1",
            provider=Hetzner(credentials="hcloud", location="fsn1"),
            type="cax11",
            image=image,
        )


def test_staging_verifies_the_checksum(tmp_path: Path) -> None:
    disk = tmp_path / "disk.raw"
    disk.write_bytes(IMAGE_BYTES)
    good = Image("base", file=disk, sha256=IMAGE_SHA, format="raw")
    staged = stage_image(good, tmp_path / "state")
    assert staged == tmp_path / "state" / good.local_path
    assert staged.read_bytes() == IMAGE_BYTES
    bad = Image("base", file=disk, sha256="0" * 64, format="raw")
    with pytest.raises(InfraError) as caught:
        stage_image(bad, tmp_path / "other")
    assert caught.value.code == "infra-image-checksum"
    assert not any((tmp_path / "other").rglob("*.raw"))  # nothing half-written kept
    missing = Image("base", file=tmp_path / "nope.raw", sha256=IMAGE_SHA, format="raw")
    with pytest.raises(InfraError) as caught:
        stage_image(missing, tmp_path / "other")
    assert caught.value.code == "infra-image-unavailable"


def test_bucket() -> None:
    infra = Infrastructure(
        "web",
        servers=[_server()],
        buckets=[
            Bucket("nix-cache", provider=OS),
            Bucket("keep", provider=OS, protect=True),
        ],
    )
    containers = _resources(infra)["openstack_objectstorage_container_v1"]
    cache = containers["bucket-nix-cache"]
    assert cache["name"] == "nix-cache"
    assert cache["metadata"] == {
        "Piceli-Managed-By": "piceli",
        "Piceli-Infra": "web",
        "Piceli-Resource": "bucket-nix-cache",
    }
    assert "force_destroy" not in cache  # a non-empty bucket is never emptied
    assert containers["bucket-keep"]["lifecycle"] == {"prevent_destroy": True}
    assert Infrastructure("only", buckets=[Bucket("nix-cache", provider=OS)])
    with _invalid("bucket name"):
        Bucket("A_b", provider=OS)
    with _invalid("cannot hold buckets"):
        Bucket("nix-cache", provider=Hetzner(credentials="hcloud", location="fsn1"))


def test_openstack_refuses_what_it_cannot_render() -> None:
    with _invalid("ipv6"):
        _server(ipv6=PrimaryIp("v6", kind="ipv6"))
    with _invalid("public network"):
        _server(ipv4=False)
    with _invalid("region"):
        _server(location="elsewhere")
    with _invalid("DNS"):
        DnsRecord("example.com", "www", "A", server=_server())
    with _invalid("auth_url"):
        OpenStack(credentials="c", auth_url="ftp://x", region="r", public_network="p")
    with _invalid("credentials"):
        OpenStack(auth_url="https://x.example/v3", region="r", public_network="p")


def test_credentials_go_to_the_environment_only(homes: Path) -> None:
    env = OS.credential_env(f"{CRED_ID}:{CRED_SECRET}")
    assert env == {
        "OS_APPLICATION_CREDENTIAL_ID": CRED_ID,
        "OS_APPLICATION_CREDENTIAL_SECRET": CRED_SECRET,
    }
    with pytest.raises(InfraError) as caught:
        OS.credential_env(CRED_SECRET)
    assert caught.value.code == "infra-credential-invalid"
    assert CRED_SECRET not in str(caught.value)
    credentials.save_credential("cloud", "provider-token", f"{CRED_ID}:{CRED_SECRET}")
    image = Image("base", url="https://images.example.com/base.qcow2", sha256=IMAGE_SHA)
    infra = Infrastructure(
        "web",
        servers=[_server(image=image, ssh_keys=[KEY], firewall=[Rule.tcp(22)])],
        buckets=[Bucket("nix-cache", provider=OS)],
    )
    rendered = render(infra)
    text = rendered.config_text + rendered.lock + json.dumps(infra.describe())
    for secret in (CRED_ID, CRED_SECRET, "OS_APPLICATION_CREDENTIAL"):
        assert secret not in text


def test_ownership_uses_tags_metadata_and_descriptions(tmp_path: Path) -> None:
    infra = Infrastructure(
        "web",
        servers=[_server(firewall=[Rule.tcp(443)], ssh_keys=[KEY])],
        buckets=[Bucket("nix-cache", provider=OS)],
        state_dir=tmp_path,
    )
    rendered = render(infra)
    ws = Workspace(infra, tmp_path, None, rendered, {})  # type: ignore[arg-type]
    write_record(tmp_path, INVENTORY, {"created": sorted(rendered.resources)})
    ours: dict[str, dict[str, Any]] = {}
    for type_, items in rendered.config["resource"].items():
        for name, body in items.items():
            before = dict(body)
            before.pop("lifecycle", None)
            ours[f"{type_}.{name}"] = before
    changes = [
        {
            "address": a,
            "mode": "managed",
            "change": {"actions": ["delete"], "before": b},
        }
        for a, b in ours.items()
    ]
    _, foreign = changes_of(ws, {"resource_changes": changes})
    assert foreign == ()
    # The same resources, without Piceli's marks or of another infrastructure.
    stripped = []
    for address, before in ours.items():
        other = json.loads(json.dumps(before).replace("web", "shop"))
        stripped.append(
            {"address": address, "mode": "managed",
             "change": {"actions": ["delete"], "before": other}}
        )  # fmt: skip
    _, foreign = changes_of(ws, {"resource_changes": stripped})
    assert set(foreign) == set(ours)
    # Labelled like ours but not in the ledger.
    write_record(tmp_path, INVENTORY, {"created": []})
    _, foreign = changes_of(ws, {"resource_changes": changes})
    assert set(foreign) == set(ours)


def test_infomaniak_preset() -> None:
    provider = Infomaniak(credentials="cloud")
    assert provider.auth_url == "https://api.pub1.infomaniak.cloud/identity/v3"
    assert provider.region == "dc3-a" and provider.location == "dc3-a"
    assert provider.public_network == "ext-net1"
    assert provider.currency == "CHF"
    assert provider.name == "openstack"
    assert provider.describe()["preset"] == "infomaniak"
    assert Infomaniak(credentials="cloud", region="dc4-a").region == "dc4-a"
    assert provider.pin() is OPENSTACK_PIN


def test_pin_has_every_platform() -> None:
    assert OPENSTACK_PIN.source == (
        "registry.opentofu.org/terraform-provider-openstack/openstack"
    )
    assert sum(h.startswith("h1:") for h in OPENSTACK_PIN.hashes) == 14
    assert sum(h.startswith("zh:") for h in OPENSTACK_PIN.hashes) == 15
    lock = render(Infrastructure("web", servers=[_server()])).lock
    assert f'version     = "{OPENSTACK_PIN.version}"' in lock


def test_prices_are_a_declared_estimate(tmp_path: Path) -> None:
    infra = Infrastructure("web", servers=[_server()], state_dir=tmp_path)
    ws = Workspace(infra, tmp_path, None, render(infra), {})  # type: ignore[arg-type]
    result = estimate(ws)
    assert result == {
        "currency": "CHF",
        "monthly_net": pytest.approx(6.7),
        "complete": True,
        "items": [
            {
                "kind": "server",
                "name": "host-1",
                "server": "host-1",
                "monthly_net": 4.7,
            },
            {"kind": "ip", "name": "host-1-ip", "server": "host-1", "monthly_net": 2.0},
        ],
    }
    bare = OpenStack(
        credentials="cloud",
        auth_url="https://identity.example.net/v3",
        region="region-1",
        public_network="public",
    )
    infra = Infrastructure("web", servers=[_server(provider=bare)], state_dir=tmp_path)
    ws = Workspace(infra, tmp_path, None, render(infra), {})  # type: ignore[arg-type]
    assert estimate(ws) is None


def test_rendering_is_deterministic(tmp_path: Path) -> None:
    def build() -> Infrastructure:
        return Infrastructure(
            "web",
            servers=[
                _server(
                    image=Image("base", url="https://images.example.com/b.qcow2",
                                sha256=IMAGE_SHA),
                    firewall=[Rule.udp(3478), Rule.tcp(443)],
                    ssh_keys=[KEY],
                    labels={"team": "web"},
                )
            ],
            buckets=[Bucket("nix-cache", provider=OS)],
        )  # fmt: skip

    assert render(build()).config_text == render(build()).config_text


@needs_tofu
@pytest.mark.skipif(
    __import__("os").environ.get("PICELI_TOFU_NETWORK") != "1",
    reason="downloads the pinned openstack provider (set PICELI_TOFU_NETWORK=1)",
)
@pytest.mark.timeout(240)
def test_the_pinned_provider_validates_the_configuration(
    homes: Path, tmp_path: Path
) -> None:
    """``tofu init`` verifies the pinned hashes; ``tofu validate`` accepts every resource."""
    from piceli.infra.machines.plan import workspace

    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    credentials.save_credential("cloud", "provider-token", f"{CRED_ID}:{CRED_SECRET}")
    image = Image("base", url="https://images.example.com/b.qcow2", sha256=IMAGE_SHA,
                  properties={"hw_firmware_type": "uefi"}, min_disk_gb=20)  # fmt: skip
    firewall = Firewall("web", [Rule.tcp(443), Rule.udp((3478, 3479)), Rule.icmp()])
    infra = Infrastructure(
        "web",
        servers=[
            _server(image=image, firewall=firewall, ssh_keys=[KEY],
                    ipv4=PrimaryIp("host-1-ip", protect=True)),
            _server(name="host-2", ipv4=True, firewall=firewall),
        ],
        buckets=[Bucket("nix-cache", provider=OS, protect=True)],
        state_dir=homes / "state",
    )  # fmt: skip
    with workspace(infra) as ws:
        validated = ws.run("validate", "-json")
    assert validated.code == 0, validated.stdout
    assert json.loads(validated.stdout)["warning_count"] == 0, validated.stdout
    assert CRED_SECRET not in validated.stdout
