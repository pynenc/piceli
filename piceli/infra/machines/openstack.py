"""OpenStack: servers on a persistent public port, security groups, images, buckets.

Rendered for the ``terraform-provider-openstack/openstack`` OpenTofu
provider, pinned to one version and its lock-file hashes. The credential is
an OpenStack application credential, stored by part (its ID and its secret:
``piceli secrets provider NAME --prompt``); OpenTofu reads it from
``OS_APPLICATION_CREDENTIAL_ID`` and ``OS_APPLICATION_CREDENTIAL_SECRET`` in
its environment, never from the configuration, a plan or the state.

What each declared object becomes:

- ``Server`` → ``openstack_compute_instance_v2`` (flavor ``Server.type``,
  one of ``flavors=``). ``Server(user_data=UserData(...))`` sets
  ``user_data`` from a sensitive variable, ignored after creation
  (``lifecycle.ignore_changes``); the provider keeps only its SHA-1 in the
  state.
- ``Server(ipv4=PrimaryIp(...))`` → ``openstack_networking_port_v2`` on the
  public network, holding the public IPv4 (and the IPv6 the network gives):
  the port asks for no address, so Neutron gives it one from an IPv4 subnet
  and one from a DHCPv6 subnet of that network.
  The server attaches to that port, so a replaced server re-attaches it and
  keeps its addresses without a floating IP. ``protect=True`` renders
  ``lifecycle { prevent_destroy = true }``: a destroy plan fails. A
  ``DnsRecord`` (at a DNS provider such as Porkbun) answers with the port's
  address, so it does not change when the server is replaced.
- ``Firewall`` → ``openstack_networking_secgroup_v2`` (default egress rules
  kept) and one ingress ``openstack_networking_secgroup_rule_v2`` per rule
  and source.
- ``ssh_keys`` → ``openstack_compute_keypair_v2`` (one per server: Nova
  boots with a single key pair).
- ``Image`` → ``openstack_images_image_v2`` (private, checksum verified).
- ``Bucket`` → ``openstack_objectstorage_container_v1`` (Swift).
- ``Bucket(keys=[S3Key(...)])`` (0.25.0) → one
  ``openstack_identity_ec2_credential_v3`` per key: an EC2 credential of
  the application credential's user in its project, the S3 API's key pair.
  Its secret is in the state (encrypted, see
  :mod:`piceli.infra.machines.state`). Keystone gives an EC2 credential the
  roles its user has in the project, and Swift's ``keystoneauth`` lets an
  operator role (``member`` at Infomaniak) read **and write** every
  container of the project: ``S3Key(access="read")`` is a separate pair,
  revocable on its own, not one the cloud limits to reading
  (``read_only_enforced: false`` in the plan). Since Keystone's
  OSSA-2026-005 fix, a *restricted* application credential may not create
  EC2 credentials at all (``infra-s3-key-refused``).

Ownership labels: server metadata and container metadata, tags on ports,
security groups and images, the description of security group rules; key
pairs carry none, so their name ``<infra>-<digest>`` and the ledger stand in.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from piceli.infra.machines.model import (
    INFRA_LABEL,
    MANAGED_BY,
    RESOURCE_LABEL,
    Bucket,
    DnsRecord,
    Firewall,
    Image,
    InfraError,
    PrimaryIp,
    S3Key,
    Server,
)
from piceli.infra.machines.provider import Pin, Prices, Provider, Resource

__all__ = [
    "INFOMANIAK_AUTH_URL",
    "INFOMANIAK_FLAVORS",
    "OPENSTACK_PIN",
    "Infomaniak",
    "OpenStack",
]

#: terraform-provider-openstack/openstack, as ``tofu providers lock`` locked
#: it for every platform the registry publishes (refresh: see the docs).
OPENSTACK_PIN = Pin(
    local_name="openstack",
    source="registry.opentofu.org/terraform-provider-openstack/openstack",
    version="3.4.0",
    hashes=(
        "h1:BB0YFgWM3KZTFY16pCYIzBqM4IpmDpT03m29VZEV2OU=",
        "h1:CalEmc/Tn9xv1Xx2bX92EUdsF8z6Lldmb4/gwT5lSRw=",
        "h1:DwSYyG1VNcEMUVY1GE9gbo2C3UPS8W2bFGk4WGVJnDY=",
        "h1:FpAT7j83xnW4SkiUL6A0eXo9V1bYbABt1Eh467INBgk=",
        "h1:FqJxdGUazKAfnZa11qT7h5PstlSTeE4xTKPUiWEgPmg=",
        "h1:GgJWG1fXiDuRBcdDuzOWRW0TMW1Lx5N8O6+80v17LZc=",
        "h1:HsPiqtbovSqh8jHAuMDR3tNeRNCOBEeHCvRbkgAHiaY=",
        "h1:IwC35geJo3X96yLwIqcjSzoLL1Nt+6qJA6BoBMlGgMw=",
        "h1:MVSoVvhjbu7s1pfYfsiYED8A++XfAoyOlSX1x9PW68E=",
        "h1:YMwKmPy+svf02jWyflqaPaQVX3eIXciwGeA8uwYgRBw=",
        "h1:n1yIddr5lsLoYBhlfUmCwIYiCsynDkLvzAE98Tceo/A=",
        "h1:nON9j1AYtfbOAmaml0lcPCfEQcb0OLB0K693HjdFggQ=",
        "h1:rqRC/cidHw8t2vQb85cjMoWThGSCWnkEmTmmNSaGBVs=",
        "h1:tzU+JfFe/RN+7swp0k5V5wUmuZtRNOjKX+spIGcRuKM=",
        "zh:11b3c88e24197a29b13cf5ab41771944bd16707b561645323e8cbb4f1da00b7b",
        "zh:1c3e89cf19118fc07d7b04257251fc9897e722c16e0a0df7b07fcd261f8c12e7",
        "zh:2dc60dbbbb822fbb1e7b17e3b89e3e6a7a8fe54a716d4477d2fa40f836c4de0f",
        "zh:68f851e333f0ab2367e9b2680739d47e4151ca9f803934ef265f6fa70a28e6e0",
        "zh:6995b0bcae5a291d7b0a4d3a5df109eb97f8d1d34be6eb27768fe29b5e0d8def",
        "zh:7b68bf81891a0445c543629d4d160226a6f2d5e8e5ee4baf21e0221412437d17",
        "zh:8f117b8b17280598a92c6202b183a77168fe573397cfb597b9c2028da281846d",
        "zh:b0a23b990eb9260e9616fd8471f11b37485f92df0e5eef46cd286f87396b0e0a",
        "zh:baa5052ab3207f3a996c1fd715935d79a4e1c57d3d24e3c549b6c20ea9307e38",
        "zh:c012cbda058aa7b76b718074fd73829ecd4009ad98e3dafe496e5f26099625c0",
        "zh:c1c95e5dbe546a935a62c63814aef04718b58dd4258e5da40d681b43322924e9",
        "zh:c720bdbdf432041f99572bac40d24659239b989cbf1540f92ebb441900ac8bae",
        "zh:c92ac802a3dd1abbf9c9ccf0aba1f261fe9f8c2e7fafc196c7119b73c25a0772",
        "zh:fd4faf25ee72acef4ab12c93ea9c7a5ccb41315e8851609e08b37d3c91ea6584",
        "zh:ff347392412fc7950635e2c1c5bb878d88b661c1c5ba052edd290d971a14028f",
    ),
)
#: Infomaniak Public Cloud's Keystone (its application-credential guide).
INFOMANIAK_AUTH_URL = "https://api.pub1.infomaniak.cloud/identity/v3"
#: Infomaniak's smallest flavor; another is refused unless
#: ``Infomaniak(flavors=...)`` lists it.
INFOMANIAK_FLAVORS = ("a1-ram2-disk20-perf1",)
ID_ENV = "OS_APPLICATION_CREDENTIAL_ID"
SECRET_ENV = "OS_APPLICATION_CREDENTIAL_SECRET"
NETWORK_LOOKUP = "public-network"
EC2_TYPE = "openstack_identity_ec2_credential_v3"
#: OpenTofu's error when Keystone refuses to create an EC2 credential.
_EC2_REFUSED = re.compile(
    rf"Error creating {EC2_TYPE}.*\b(?:403|401)\b|{EC2_TYPE}.*(?:Forbidden|not allowed)",
    re.S,
)
_URL = re.compile(r"https?://[^\s?#@]+")
_TOKEN = re.compile(r"[A-Za-z0-9][-A-Za-z0-9_.]{0,127}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}")
_KEY_NAME = re.compile(r"([a-z](?:[-a-z0-9]*[a-z0-9])?)-[0-9a-f]{12}")
#: Swift metadata is sent as HTTP headers: keys without ``/``.
_SWIFT_KEYS = {
    MANAGED_BY: "Piceli-Managed-By",
    INFRA_LABEL: "Piceli-Infra",
    RESOURCE_LABEL: "Piceli-Resource",
}


def _key(label: str) -> str:
    """A label key as Nova metadata and tags accept it (``/`` becomes ``:``)."""
    return label.replace("/", ":")


def _tags(labels: Mapping[str, str]) -> list[str]:
    return sorted(f"{_key(k)}={v}" for k, v in labels.items())


def _from_pairs(pairs: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in pairs if isinstance(pairs, (list, tuple, set)) else ():
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            found[key.replace(":", "/")] = value
    return found


@dataclass(frozen=True)
class OpenStack(Provider):
    """An OpenStack cloud, authenticated with an application credential.

    :param credentials: The local credential holding an application
        credential, its ID and its secret stored by part (``piceli secrets
        provider NAME --prompt``).
    :param auth_url: The identity (Keystone v3) URL.
    :param region: The region every resource goes into (also the servers'
        default location; one region per declaration).
    :param public_network: The public network's name (looked up) or id.
    :param public_network_external: How the name is matched (0.25.2):
        ``True`` (the default) only a ``router:external`` network, ``False``
        only one that is not, ``None`` the name alone. A cloud whose servers
        attach to a shared provider network that is not ``router:external``
        (Infomaniak's ``ext-net1``) needs ``None``; with ``True`` OpenTofu
        finds no network.
    :param prices: Monthly net price per flavor, for the plan's estimate.
    :param ip_prices: Monthly net price of a persistent port's address
        (``{"ipv4": ...}``).
    :param currency: The currency of those prices.
    :param flavors: The flavors a server may use; any other is refused
        (required: clouds name their flavors differently).
    """

    credentials: str | None = None
    auth_url: str | None = None
    region: str | None = None
    public_network: str | None = None
    public_network_external: bool | None = True
    prices_monthly: Mapping[str, float] = field(default_factory=dict)
    ip_prices: Mapping[str, float] = field(default_factory=dict)
    currency: str = "EUR"
    flavors: tuple[str, ...] = ()
    #: The servers' default location: the region.
    location: str | None = field(default=None, repr=False)

    name = "openstack"
    #: The preset that filled the values (``Infomaniak``), for ``describe()``.
    preset: ClassVar[str | None] = None
    supports_images = True
    supports_buckets = True
    supports_dns = False
    supports_user_data = True
    supports_s3_keys = True

    def __init__(
        self,
        credentials: str | None = None,
        auth_url: str | None = None,
        region: str | None = None,
        public_network: str | None = None,
        prices: Mapping[str, float] | None = None,
        ip_prices: Mapping[str, float] | None = None,
        currency: str = "EUR",
        flavors: Sequence[str] | None = None,
        public_network_external: bool | None = True,
    ) -> None:
        set_ = object.__setattr__
        set_(self, "credentials", credentials)
        set_(self, "auth_url", auth_url)
        set_(self, "region", region)
        set_(self, "location", region)
        set_(self, "public_network", public_network)
        set_(self, "public_network_external", public_network_external)
        set_(self, "prices_monthly", dict(prices or {}))
        set_(self, "ip_prices", dict(ip_prices or {}))
        set_(self, "currency", currency)
        self._check()
        what = type(self).__name__
        if flavors is None or isinstance(flavors, str) or not flavors:
            raise InfraError(
                "infra-invalid", f"{what}(flavors=) lists the flavors servers may use"
            )
        if not all(isinstance(f, str) and _TOKEN.fullmatch(f) for f in flavors):
            raise InfraError(
                "infra-invalid", f"{what}(flavors=) are names like a1-ram2-disk20-perf1"
            )
        set_(self, "flavors", tuple(sorted(set(flavors))))

    def _check(self) -> None:
        from piceli.profiles import ProfileError, check_name

        what = type(self).__name__
        if self.credentials is None:
            raise InfraError(
                "infra-invalid",
                f"{what}(credentials=) names the application credential",
            )
        try:
            check_name(self.credentials)
        except ProfileError:
            raise InfraError(
                "infra-invalid", f"{what}(credentials=) must be a credential name"
            ) from None
        if not isinstance(self.auth_url, str) or not _URL.fullmatch(self.auth_url):
            raise InfraError("infra-invalid", f"{what}(auth_url=) is an http(s) URL")
        for attribute in ("region", "public_network"):
            value = getattr(self, attribute)
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise InfraError(
                    "infra-invalid", f"{what}({attribute}=) is a name like ext-net"
                )
        if self.public_network_external not in (True, False, None):
            raise InfraError(
                "infra-invalid",
                f"{what}(public_network_external=) is True, False or None",
            )
        for attribute in ("prices_monthly", "ip_prices"):
            for key, value in getattr(self, attribute).items():
                if (
                    not isinstance(key, str)
                    or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or value < 0
                ):
                    raise InfraError(
                        "infra-invalid",
                        f"{what}: prices map names to monthly amounts (>= 0)",
                    )
        if set(self.ip_prices) - {"ipv4", "ipv6"}:
            raise InfraError("infra-invalid", f"{what}(ip_prices=) keys are ipv4, ipv6")
        if not isinstance(self.currency, str) or not re.fullmatch(
            r"[A-Z]{3}", self.currency
        ):
            raise InfraError("infra-invalid", f"{what}(currency=) is like EUR or CHF")

    def __hash__(self) -> int:
        return hash((self.name, self.auth_url, self.region))

    # ------------------------------------------------------------ pinning
    def pin(self) -> Pin:
        return OPENSTACK_PIN

    def token_env(self) -> str:
        return SECRET_ENV

    def credential_env(self, parts: Mapping[str, str]) -> dict[str, str]:
        identifier = parts.get("application_credential_id")
        secret = parts.get("application_credential_secret")
        if not identifier or not secret:
            raise InfraError(
                "infra-credential-invalid",
                f"credential {self.credentials!r} is not an application credential "
                "(its ID and its secret)",
            )
        return {ID_ENV: identifier, SECRET_ENV: secret}

    def provider_block(self) -> dict[str, Any]:
        return {"auth_url": self.auth_url, "region": self.region}

    def describe(self) -> dict[str, Any]:
        body = {
            **super().describe(),
            "auth_url": self.auth_url,
            "public_network": self.public_network,
            "public_network_external": self.public_network_external,
        }
        if self.preset is not None:
            body["preset"] = self.preset
        return body

    def check_server(self, server: Server) -> None:
        if server.type not in self.flavors:
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: flavor {server.type!r} is not one of "
                f"{', '.join(self.flavors)} ({type(self).__name__}(flavors=...) adds one)",
            )
        if server.location != self.region:
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: location must be the provider's region "
                f"{self.region} (one OpenStack region per declaration)",
            )
        if server.ipv4 is False:
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: an OpenStack server is on the public network "
                "(ipv4=True or ipv4=PrimaryIp(...))",
            )
        if isinstance(server.ipv6, PrimaryIp):
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: the port of ipv4=PrimaryIp(...) also holds "
                "the ipv6 address; ipv6= is True or False",
            )
        if len(server.ssh_keys) > 1:
            raise InfraError(
                "infra-invalid",
                f"Server {server.name}: OpenStack boots with one key pair "
                "(one ssh key; add more with the OS install)",
            )

    # ------------------------------------------------------------ rendering
    def _network(self) -> str:
        assert self.public_network is not None
        if _UUID.fullmatch(self.public_network):
            return self.public_network
        return f"${{data.openstack_networking_network_v2.{NETWORK_LOOKUP}.id}}"

    def data_sources(self) -> list[Resource]:
        assert self.public_network is not None
        if _UUID.fullmatch(self.public_network):
            return []
        lookup: dict[str, Any] = {"name": self.public_network}
        if self.public_network_external is not None:
            lookup["external"] = self.public_network_external
        return [Resource("openstack_networking_network_v2", NETWORK_LOOKUP, lookup)]

    @staticmethod
    def _group(firewall: Firewall) -> Resource:
        return Resource(
            "openstack_networking_secgroup_v2", f"firewall-{firewall.name}", {}
        )

    def render_primary_ip(
        self, ip: PrimaryIp, server: Server, labels: Mapping[str, str]
    ) -> Resource:
        body: dict[str, Any] = {
            "name": ip.name,
            "network_id": self._network(),
            "admin_state_up": True,
            "tags": _tags(labels),
        }
        if isinstance(server.firewall, Firewall):
            body["security_group_ids"] = [self._group(server.firewall).ref("id")]
        if ip.protect:
            body["lifecycle"] = {"prevent_destroy": True}
        return Resource("openstack_networking_port_v2", f"ip-{ip.name}", body)

    def render_firewall(
        self, firewall: Firewall, labels: Mapping[str, str]
    ) -> list[Resource]:
        group = Resource(
            "openstack_networking_secgroup_v2",
            f"firewall-{firewall.name}",
            {
                "name": firewall.name,
                "description": "Managed by Piceli: inbound rules only",
                "delete_default_rules": False,
                "tags": _tags(labels),
            },
        )
        description = " ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        rules: list[Resource] = []
        for rule in firewall.rules:
            for source in rule.sources:
                v6 = ":" in source
                protocol = rule.protocol
                if protocol == "icmp" and v6:
                    protocol = "ipv6-icmp"
                body: dict[str, Any] = {
                    "direction": "ingress",
                    "ethertype": "IPv6" if v6 else "IPv4",
                    "protocol": protocol,
                    "remote_ip_prefix": source,
                    "security_group_id": group.ref("id"),
                    "description": description,
                }
                if isinstance(rule.port, tuple):
                    body["port_range_min"], body["port_range_max"] = rule.port
                elif rule.port is not None:
                    body["port_range_min"] = body["port_range_max"] = rule.port
                digest = hashlib.sha256(
                    json.dumps([protocol, rule.port_text, source]).encode()
                ).hexdigest()[:8]
                port = (rule.port_text or "all").replace("-", "to")
                rules.append(
                    Resource(
                        "openstack_networking_secgroup_rule_v2",
                        f"firewall-{firewall.name}-{rule.protocol}-{port}-{digest}",
                        body,
                    )
                )
        rules.sort(key=lambda item: item.name)
        return [group, *rules]

    def render_ssh_key(
        self, name: str, digest: str, public_key: str, labels: Mapping[str, str]
    ) -> Resource:
        return Resource(
            "openstack_compute_keypair_v2",
            f"key-{digest}",
            {"name": name, "public_key": public_key},
        )

    def render_image(self, image: Image, labels: Mapping[str, str]) -> Resource:
        body: dict[str, Any] = {
            "name": image.name,
            "container_format": "bare",
            "disk_format": image.format,
            "local_file_path": image.local_path,
            "visibility": "private",
            "tags": _tags(labels),
        }
        if image.properties:
            body["properties"] = dict(image.properties)
        if image.min_disk_gb is not None:
            body["min_disk_gb"] = image.min_disk_gb
        return Resource("openstack_images_image_v2", f"image-{image.name}", body)

    def render_bucket(self, bucket: Bucket, labels: Mapping[str, str]) -> Resource:
        body: dict[str, Any] = {
            "name": bucket.name,
            "metadata": {
                _SWIFT_KEYS[k]: v for k, v in labels.items() if k in _SWIFT_KEYS
            },
        }
        if bucket.protect:
            body["lifecycle"] = {"prevent_destroy": True}
        return Resource(
            "openstack_objectstorage_container_v1", f"bucket-{bucket.name}", body
        )

    def render_s3_key(
        self, key: S3Key, bucket: Resource, labels: Mapping[str, str]
    ) -> Resource:
        # The application credential's own user and project: no arguments.
        # EC2 credentials carry no labels (the ledger owns them); the
        # bucket is a dependency so a destroy removes the keys first.
        return Resource(EC2_TYPE, f"s3key-{key.name}", {"depends_on": [bucket.address]})

    def s3_key_values(self, values: Mapping[str, Any]) -> tuple[str, str] | None:
        access, secret = values.get("access"), values.get("secret")
        if isinstance(access, str) and isinstance(secret, str) and access and secret:
            return access, secret
        return None

    def s3_key_enforcement(self, key: S3Key) -> dict[str, Any]:
        if key.access == "write":
            return {
                "read_only_enforced": False,
                "how": "an EC2 credential of the application credential's user: "
                "read and write on every container of the project",
            }
        return {
            "read_only_enforced": False,
            "how": "not enforced by the cloud: an EC2 credential has its user's "
            "project roles (read and write on every container of the project); "
            "a separate pair, revocable alone; hosts only read with it and verify "
            "every build's signature. A project of its own for the cache bucket "
            "limits it to the cache",
        }

    def hides_id(self, type_: str) -> bool:
        return type_ == EC2_TYPE

    def ledger_owned(self, type_: str) -> bool:
        return type_ == EC2_TYPE

    def known_error(self, diagnostics: Sequence[str]) -> InfraError | None:
        if any(_EC2_REFUSED.search(line) for line in diagnostics):
            return InfraError(
                "infra-s3-key-refused",
                "the cloud refused to create an EC2 credential (S3 keys) with this "
                "application credential; only an unrestricted one may (Keystone's "
                "OSSA-2026-005), or create the keys yourself and store them with "
                "piceli secrets s3 NAME --prompt",
                diagnostics,
            )
        return None

    def render_server(
        self,
        server: Server,
        *,
        ips: Mapping[str, Resource],
        firewall: Resource | None,
        labels: Mapping[str, str],
        ssh_keys: list[Resource],
    ) -> Resource:
        body: dict[str, Any] = {
            "name": server.name,
            "flavor_name": server.type,
            "metadata": {_key(k): v for k, v in labels.items()},
        }
        if isinstance(server.image, Image):
            body["image_id"] = Resource(
                "openstack_images_image_v2", f"image-{server.image.name}", {}
            ).ref("id")
        elif _UUID.fullmatch(server.image):
            body["image_id"] = server.image
        else:
            body["image_name"] = server.image
        if isinstance(server.ipv4, PrimaryIp):
            body["network"] = [{"port": ips[server.ipv4.name].ref("id")}]
        else:
            body["network"] = [{"uuid": self._network()}]
            if firewall is not None:
                body["security_groups"] = [firewall.ref("name")]
        if ssh_keys:
            body["key_pair"] = ssh_keys[0].ref("name")
        return Resource("openstack_compute_instance_v2", f"server-{server.name}", body)

    def render_user_data(self, server: Resource, reference: str) -> Resource:
        body = dict(server.body)
        body["user_data"] = reference
        lifecycle = dict(body.get("lifecycle") or {})
        lifecycle["ignore_changes"] = ["user_data"]
        body["lifecycle"] = lifecycle
        return Resource(server.type, server.name, body)

    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource:
        raise InfraError("infra-invalid", "OpenStack serves no DNS records here")

    def address(self, server: Resource, family: str) -> str:
        return server.ref(f"access_ip_{family[-2:]}")

    def record_address(
        self,
        server: Server,
        resource: Resource,
        family: str,
        ips: Mapping[str, Resource],
    ) -> str:
        # A server on a persistent port answers with the port's address: a
        # replaced server re-attaches the port, and the record stays as it is.
        if not isinstance(server.ipv4, PrimaryIp):
            return self.address(resource, family)
        port = ips[server.ipv4.name]
        test = '!strcontains(ip, ":")' if family == "ipv4" else 'strcontains(ip, ":")'
        return (
            f"${{[for ip in {port.type}.{port.name}.all_fixed_ips : ip if {test}][0]}}"
        )

    def server_id(self, server: Resource) -> str:
        return server.ref("id")

    # ------------------------------------------------------------ state
    def labels_of(self, values: Mapping[str, Any]) -> Mapping[str, str]:
        metadata = values.get("metadata")
        if isinstance(metadata, Mapping):
            return {k.replace(":", "/"): str(v) for k, v in metadata.items()}
        return _from_pairs(values.get("tags"))

    def resource_labels(
        self, type_: str, values: Mapping[str, Any]
    ) -> Mapping[str, str]:
        if type_ == "openstack_objectstorage_container_v1":
            metadata = values.get("metadata")
            back = {v: k for k, v in _SWIFT_KEYS.items()}
            if not isinstance(metadata, Mapping):
                return {}
            return {back[k]: str(v) for k, v in metadata.items() if k in back}
        if type_ == "openstack_networking_secgroup_rule_v2":
            text = values.get("description")
            return _from_pairs(text.split()) if isinstance(text, str) else {}
        if type_ == "openstack_compute_keypair_v2":
            # Key pairs carry no labels: their name and the ledger stand in.
            found = _KEY_NAME.fullmatch(str(values.get("name") or ""))
            if found is None:
                return {}
            return {MANAGED_BY: "piceli", INFRA_LABEL: found.group(1)}
        return self.labels_of(values)

    # ------------------------------------------------------------ prices
    def prices(self, token: str | None) -> Prices | None:
        """The declared monthly prices (no price API is called)."""
        if not self.prices_monthly and not self.ip_prices:
            return None
        assert self.region is not None
        return Prices(
            self.currency,
            {(k, self.region): float(v) for k, v in self.prices_monthly.items()},
            {(k, self.region): float(v) for k, v in self.ip_prices.items()},
        )


class Infomaniak(OpenStack):
    """Infomaniak Public Cloud: :class:`OpenStack` with its documented values.

    ``auth_url`` is Infomaniak's Keystone, ``public_network`` its shared
    public network ``ext-net1`` (a public IPv4 and IPv6 per port), found by
    its name alone: it is not ``router:external``
    (``public_network_external=None``; 0.25.2), ``region``
    ``dc3-a`` by default (``dc4-a`` is the other), prices in CHF, and
    ``flavors`` its smallest, ``a1-ram2-disk20-perf1`` (1 vCPU, 2 GB, 20 GB):
    a bigger one must be listed.
    """

    preset = "infomaniak"

    def __init__(
        self,
        credentials: str | None = None,
        region: str = "dc3-a",
        public_network: str = "ext-net1",
        prices: Mapping[str, float] | None = None,
        ip_prices: Mapping[str, float] | None = None,
        currency: str = "CHF",
        auth_url: str = INFOMANIAK_AUTH_URL,
        flavors: Sequence[str] = INFOMANIAK_FLAVORS,
        public_network_external: bool | None = None,
    ) -> None:
        super().__init__(
            credentials=credentials,
            auth_url=auth_url,
            region=region,
            public_network=public_network,
            prices=prices,
            ip_prices=ip_prices,
            currency=currency,
            flavors=flavors,
            public_network_external=public_network_external,
        )
