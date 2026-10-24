# Machines with OpenTofu

```{admonition} Maturity: preview
:class: note

New in 0.15.0: Hetzner Cloud, one server type to start with; 0.24.0 adds OpenStack clouds (with an Infomaniak preset), uploaded images, buckets and DNS records at Porkbun. Its parameters may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

Servers, fixed IPs, firewalls and DNS records are declared in typed Python,
like an `App`. Piceli renders the [OpenTofu](https://opentofu.org)
configuration, owns its state (encrypted, locked, never in Git) and applies
a plan only with the approval of its hash, as it does for deploys. Piceli
does not own the operating system: an install hook you declare installs it,
then Piceli waits for the machine's k3s and registers it as a cluster it
deploys to.
A machine without a cluster can run NixOS and follow your release tags:
{doc}`single_nixos_host` declares one, with its image, bucket and DNS.

```python
# machines.py
from piceli.infra import (
    Cluster,
    DnsRecord,
    Firewall,
    Hetzner,
    Hook,
    Infrastructure,
    PrimaryIp,
    Rule,
    Server,
    Ssh,
)

hcloud = Hetzner(
    credentials="hcloud", location="fsn1"
)  # the token: piceli secrets provider
edge = Firewall(
    "edge",
    rules=[
        Rule.tcp(443, name="https"),
        Rule.udp(3478, name="turn"),
        Rule.tcp(
            22, sources=["100.64.0.0/10"], name="ssh"
        ),  # SSH from a private network only
    ],
)
edge_cluster = Cluster(
    "edge-1", api="https://edge-1.example.net:6443", credentials="edge-1"
)

edge_1 = Server(
    "edge-1",
    provider=hcloud,
    type="cax11",
    image="debian-12",
    ipv4=PrimaryIp("edge-1-v4"),  # a fixed address that outlives the server
    ipv6=True,
    firewall=edge,  # or a list of rules: firewall=[Rule.tcp(443), Rule.udp(3478)]
    ssh_keys=["ssh-ed25519 AAAA… owner@example.com"],
    install=Hook(["install-os", "--host", "{name}", "root@{ipv4}"]),
    ssh=Ssh(user="root", address="edge-1.example.net"),  # default: the server's IPv4
    cluster=edge_cluster,
)

infra = Infrastructure(
    "edge",
    servers=[edge_1],
    records=[DnsRecord("example.com", "www", "A", server=edge_1)],
)
```

```console
$ piceli secrets state-key edge-state --generate        # once: the state passphrase
$ piceli secrets provider hcloud --prompt               # once: type the API token
$ piceli infra plan machines.py:infra                   # exit 3: changes, estimate, plan hash
$ piceli infra apply machines.py:infra --approve sha256:…
$ piceli infra install machines.py:infra edge-1         # exit 3: the rendered hook, its digest
$ piceli infra install machines.py:infra edge-1 --approve sha256:…
$ piceli infra register machines.py:infra edge-1        # exit 3: host-key fingerprints, digest
$ piceli infra register machines.py:infra edge-1 --approve sha256:…
$ piceli infra status machines.py:infra --json
$ piceli cluster init machines.py:edge_cluster          # then as any declared cluster
```

## The declaration

Every class checks its values when it is built (`infra-invalid`), so a wrong
declaration fails at import, never halfway through a plan.

| Class | What it is |
| --- | --- |
| `Server(name, provider=, type=, image=, location=, ipv4=, ipv6=, firewall=, ssh_keys=, install=, ssh=, cluster=, labels=, user_data=, on_image_change="rebuild")` | One machine. `ipv4`/`ipv6`: `True` (an address that goes with the server), `False`, or a `PrimaryIp`. `location` defaults to the provider's. `image` is an image name, or an `Image` where the provider uploads them. Without `cluster=` it is a plain host (`register` does not apply). `on_image_change` (0.26.2): what a new image does, `"rebuild"` in place or `"replace"` ({ref}`below <server-image-change>`). |
| `PrimaryIp(name, kind="ipv4", protect=False)` | A fixed address, created in the server's location and assigned to it; it stays when the server is replaced. `protect=True` turns on delete protection (destroy then fails until it is off). |
| `Firewall(name, rules=[...])`, `Rule.tcp(port, sources=, name=)`, `Rule.udp(...)`, `Rule.icmp(...)` | Inbound rules; a port or a range `(low, high)`; sources default to every address. Everything else is dropped. Outbound is not filtered. |
| `DnsRecord(zone, name, type, value=… \| server=… \| servers=[…], ttl=, provider=)` | One record set (`A`, `AAAA`, `CNAME`, `TXT`; `@` is the apex) in a zone you already have at the provider (default: the first server's). Several servers give one record set with every address; the servers may be at another provider than the record. `ttl` defaults to the provider's (300 s; Porkbun 600 s). Piceli never creates or deletes the zone. |
| `Hook(argv, timeout=3600)` | The command that installs the OS. Placeholders `{name}`, `{id}`, `{ipv4}`, `{ipv6}`, `{address}`. |
| `Ssh(user="root", address=None, port=22, kubeconfig_path="/etc/rancher/k3s/k3s.yaml")` | How Piceli reaches the server to read its k3s kubeconfig. |
| `Image(name, file= \| url=, sha256=, format="qcow2", min_disk_gb=, properties=)` | A disk image to upload (OpenStack). `sha256` is required and checked before the upload (`infra-image-checksum`). `Image.from_build(name, sidecar, upload="host")` reads `piceli dev nix build --image`'s sidecar and (0.25.3) uploads from the build host that built it; `ImageHost` is that host. |
| `Bucket(name, provider=, protect=False, keys=[])` | An S3-compatible object-storage bucket (OpenStack Swift). Never emptied: destroying one that holds objects fails. `keys=` (0.25.0): the `S3Key` pairs Piceli creates for it (below). |
| `S3Key(name, access="write")` | An S3 key pair of a bucket, created by `infra apply` and stored as the local credential `name` (`s3-keys`); `access="read"` for the hosts' pair. |
| `Infrastructure(name, servers=, records=, state_dir=, state_key=, backend=, home=, buckets=)` | Everything one OpenTofu state holds. |

Each resource Piceli renders is labelled `piceli.io/managed-by=piceli`,
`piceli.io/infra=<name>` and `piceli.io/resource=<kind>-<name>`.

### Health-checked DNS failover (later)

A `DnsRecord` with several servers answers with every address. Failover that
drops an unhealthy server's address is not implemented yet; the seam is the
record set itself: a health checker changes its answers through the same
plan and approval (or a provider's own failover feature is rendered for the
same record), never by editing DNS behind Piceli's back.

## Providers

`Hetzner(credentials=, location=, endpoint=, server_types=("cax11",), poll_interval=)`
renders `hcloud_server`, `hcloud_primary_ip`, `hcloud_firewall`,
`hcloud_ssh_key` and `hcloud_zone_rrset` for the `hetznercloud/hcloud`
OpenTofu provider, pinned to one version with every platform's lock-file
hashes: `tofu init -lockfile=readonly` refuses a download that does not match.
Another server type is refused unless `server_types=` lists it.
`endpoint=` points at another API (a mock in tests).

A provider is one subclass of `piceli.infra.Provider` (render a server, an
IP, a firewall, a record; name the pinned OpenTofu provider and the
environment variables of its credential; optionally `prices()`), so another
provider needs no change elsewhere.
`piceli.testing.infra.FakeProvider` renders OpenTofu's built-in
`terraform_data` instead: a real plan, apply and encrypted state, nothing
created anywhere (see {doc}`testing`).

### OpenStack and Infomaniak

`OpenStack(credentials=, auth_url=, region=, public_network=, flavors=,
prices=, ip_prices=, currency="EUR", public_network_external=True)` renders for the
`terraform-provider-openstack/openstack` OpenTofu provider (3.4.0, pinned
with every platform's hashes). `flavors=` is an allow-list, like
`Hetzner(server_types=)`: a server with another flavor is refused when it
is declared. `public_network=` is a network id, or a name OpenTofu looks
up: `public_network_external=True` matches only a `router:external`
network with that name, `False` only one that is not, `None` the name
alone. A server's port asks for no address: Neutron gives it one from an
IPv4 subnet and one from a DHCPv6 subnet of the network.
`Infomaniak(credentials=, region="dc3-a")` fills in
Infomaniak Public Cloud's values: `auth_url`
`https://api.pub1.infomaniak.cloud/identity/v3`, the shared public network
`ext-net1` (a public IPv4 and IPv6 per port), found by its name alone
(`public_network_external=None`: it is not `router:external`), prices in
CHF, and `flavors=["a1-ram2-disk20-perf1"]`, its smallest (1 vCPU, 2 GB);
`dc4-a` is the other region. It creates no S3 keys (0.25.3): see
[pairs you make yourself](#s3-pairs-you-make-yourself).

```python
from piceli.infra import Bucket, Image, Infomaniak, Infrastructure, PrimaryIp, Rule, Server

cloud = Infomaniak(credentials="cloud", prices={"a1-ram2-disk20-perf1": 4.70})
base = Image("host-base", file="result/host.qcow2", sha256="9f86d0…")
host = Server(
    "host-1", provider=cloud, type="a1-ram2-disk20-perf1", image=base,
    ipv4=PrimaryIp("host-1-port", protect=True),  # the port keeps the address
    firewall=[Rule.tcp(443), Rule.udp(3478)], ssh_keys=["ssh-ed25519 AAAA… owner@example.com"],
)  # no cluster=: a plain host
infra = Infrastructure("web", servers=[host], buckets=[Bucket("my-cache", provider=cloud)])
```

| Declared | Rendered |
| --- | --- |
| `Server` | `openstack_compute_instance_v2`, flavor `type`, image by name, id or `Image` |
| `PrimaryIp` | `openstack_networking_port_v2` on the public network; the server attaches to it, so a replaced server gets the same port and addresses, without a floating IP. `protect=True`: `prevent_destroy`, a destroy plan fails |
| `Firewall`, `Rule` | `openstack_networking_secgroup_v2` (on the port), one ingress `openstack_networking_secgroup_rule_v2` per rule and source; the group's default egress rules stay, so outbound is not filtered |
| `ssh_keys` | `openstack_compute_keypair_v2`; one key per server (Nova boots with one key pair) |
| `Image` | `openstack_images_image_v2`, private, tagged with its sha256 (`piceli.io:image-sha256=…`): Piceli uploads it itself and OpenTofu adopts it (0.25.3, {ref}`below <image-uploads>`) |
| `Bucket` | `openstack_objectstorage_container_v1` (Swift) |
| `S3Key` | `openstack_identity_ec2_credential_v3`: an EC2 credential of the application credential's user in its project (0.25.0) |
| `user_data=UserData(NAME)` | `user_data` from the sensitive variable `user_data_<server>`, with `ignore_changes`: read at creation only (a rebuild for a new image keeps the original; `on_image_change="replace"` passes the current one) |

The credential is an application credential (Horizon: Identity →
Application Credentials, or `openstack application credential create`),
stored by part: `piceli secrets provider cloud --provider openstack --prompt`
asks for the application credential ID, then its secret (see
[Secrets](#secrets): credentials with two parts). OpenTofu gets it only as
`OS_APPLICATION_CREDENTIAL_ID` and `OS_APPLICATION_CREDENTIAL_SECRET`; both
are redacted from its output. S3 access to a bucket needs EC2 credentials
(at Infomaniak the endpoint is `https://s3.pub1.infomaniak.cloud`): declare
them as `S3Key`s (below; not at Infomaniak), or create them yourself
(`openstack ec2 credentials create`) and store them with `piceli secrets s3
NAME --prompt`.
OpenStack has no price API here: `prices=` (monthly net per flavor) and
`ip_prices=` (`{"ipv4": …}`) are your own table, shown as the plan's
estimate. One region per declaration; `ipv4=False` and `ipv6=PrimaryIp` are
refused (the port holds both families). OpenStack serves no DNS: give a
`DnsRecord` `provider=Porkbun(...)` (below).

**User data.** `Server(user_data=UserData("web-1-boot"))` gives the server
a stored secret at creation: a cloud-config, a script, or one value the
image reads (a pre-authorized VPN key). Store it with `piceli secrets
user-data web-1-boot --file PATH` (or `--prompt`). Only a plan that creates
or replaces the server reads it: the plan runs once without it, and when it
creates the server, again with the value in OpenTofu's environment
(`TF_VAR_user_data_web_1`, redacted from every output). The plan lists
`user_data` (server, secret name, a short sha256 digest), which the plan
hash covers. Later plans never read it, and a changed, expired or deleted
value never replaces the server. A creating plan without it fails with
`infra-user-data-missing`. The provider keeps only the value's SHA-1 in
the (encrypted) state. The instance's metadata service serves the user data
to any process on it, so a key in it should be single-use and short-lived.
{doc}`single_nixos_host` uses one to join a tailnet on first boot.

(server-image-change)=
**A new image: rebuilt or replaced** (0.26.2). By default
(`on_image_change="rebuild"`) a server whose image changes (a new `Image`
checksum, another image name) is updated in place: the OpenStack provider
changes `image_id` with a Nova rebuild. The disk is rewritten, but the
server keeps its id, its port and its *original* user data (`user_data` is
ignored after creation), so a rebuilt host that bootstraps with a
single-use key boots with a spent one. For a server with `user_data`, the
plan then carries a warning, on stderr and in its JSON `warnings`, which
never blocks it:

```text
  update   server web-1 (openstack_compute_instance_v2.server-web-1)
  warning: server web-1 is rebuilt in place with its original user data (a spent bootstrap key?): use Server(on_image_change="replace") or piceli infra replace
```

With `Server(..., on_image_change="replace")` the plan replaces the server
instead (`tofu plan -replace=ADDRESS`, planned again): it is destroyed
first, then created on the same `PrimaryIp` port (the same IPv4 and IPv6;
the port attaches to one server at a time) with the current `UserData`,
which the plan then needs, as for a create (`infra-user-data-missing`). The
plan lists it in `replaced` (server and why), which the plan hash covers:

```text
  replace  server web-1 (openstack_compute_instance_v2.server-web-1)
  user data of web-1: secret web-1-boot (sha256:…), passed at creation only
  server web-1: replaced (its image changed), destroyed first
```

Choose `"replace"` for a host that bootstraps with a one-time key (a
pre-authorized tailnet key): store a fresh key (`piceli secrets user-data
web-1-boot --file PATH`) before the plan. Whatever the server keeps on its
own disk is gone either way. `piceli infra replace REF SERVER` replaces one
server on request, image changed or not: the same plan, approval and hash
as `infra apply`, with `-replace` for that server (`--approve HASH` to run
it; an `infra apply` approval never runs it).

(image-uploads)=
**Image uploads** (0.25.3). OpenTofu's image resource uploads from the
machine that runs `tofu apply` in one stream: no retry, no progress, and a
dropped connection leaves a half-made image (`saving`) that the next plan
replaces. Glance has no resumable upload (`PUT /v2/images/{id}/file` and
the import API's `glance-direct` staging are each one stream), so Piceli
uploads the image itself, before `tofu apply`, and OpenTofu adopts it
(`tofu import`):

- **From where it was built.** `Image.from_build(name, sidecar)` of an
  image built on a remote docker host (the sidecar's `built_on` and
  `store_file`) is sent from that host: a short `docker run --rm -i` of the
  run image, the host's `/nix` read-only, over the container CLI's SSH
  transport. It gets a fresh Keystone token on its stdin (never in an
  argument, a log or the state), revoked after each attempt. Any other
  `Image` (and `upload="local"`) is sent from this machine.
- **In the plan,** each upload is a line, and the plan hash covers it:

  ```text
    create   image web-1-nixos (openstack_images_image_v2.image-web-1-nixos)
    image web-1-nixos: upload, upload from build host build-1 (451.0 MB, sha256 0d5c8c55e1f2…)
  ```

  An image in the state that a failed upload left unfinished, or whose
  sha256 tag is not the declared one, is replaced: `image web-1-nixos:
  replace (a failed upload left it in saving), …`. Images at the cloud with
  this image's ownership tags that the state does not hold (an upload that
  was killed) are listed as `stale_images` and deleted before the upload.
- **Retries.** An attempt that fails is retried (three attempts, 10 s and
  30 s apart), on the same image record when Glance put it back to
  `queued`, else on a new one after deleting the old (Infomaniak leaves a
  dropped upload in `saving`). When all fail: `infra-image-upload-failed`
  (exit 1), the half-made image deleted.
- **Progress** on stderr, at most once a second on a terminal (every 10 s
  otherwise): `image web-1-nixos: 212.0 MB of 451.0 MB (47%), 38.2 MB/s,
  ETA 6s`. Its first line names the full log, `image-upload-NAME.log` in
  the state directory. stdout keeps one JSON object with
  `uploaded_images` (id, bytes, seconds, rate, attempts, from).
- **Clean-up.** A failed or interrupted upload (`Ctrl-C` too) deletes the
  image record it created, once its tags show it is Piceli's.
- **Integrity.** The bytes sent must have the declared sha256, and Glance's
  `checksum` (MD5) and `os_hash_value` (SHA-512 at Infomaniak) must be
  their hashes; otherwise the image is deleted (`infra-image-checksum`).

After the uploads OpenTofu plans again: that plan must be the approved one
less the uploads (`infra-plan-changed` otherwise; the uploaded images stay
adopted), and only it is applied. A replaced active image is deleted after
the apply succeeded (until then `images.json` lists it as `retired`).

Ownership marks: metadata on servers and buckets, tags on ports, security
groups and images, the description of security group rules. Key pairs and
EC2 credentials carry none: their name (`<infra>-<digest>`) and the ledger stand in.

**S3 keys** (0.25.0). A bucket declares the key pairs it needs, and
`infra apply` creates them:

```python
nix_bucket = Bucket("my-cache", provider=cloud, keys=[
    S3Key("cache-write"),                 # the build host: put and delete
    S3Key("cache-read", access="read"),   # the hosts
])
cache = NixCache("cache-1:…", bucket=nix_bucket)  # write_credentials: cache-write
```

`infra plan` lists them in `s3_keys` (never a value):

```json
{"name": "cache-read", "bucket": "my-cache", "access": "read", "action": "create",
 "credential": "cache-read", "stored": false, "read_only_enforced": false,
 "how": "not enforced by the cloud: an EC2 credential has its user's project roles …"}
```

After the apply (also a partial one) Piceli reads each pair out of the
state and stores it as the credential of its name (`s3-keys`,
`ACCESS_KEY_ID:SECRET`, `0600`), where `NixCache` (`write_credentials`
defaults to the bucket's write key), `builder serve --publish`, `dev nix
build --publish` and `piceli host secrets` (the read key, see
{doc}`nix_hosts`) load it. The values are redacted from OpenTofu's output
as soon as they are known; an EC2 credential's id is its access key id, so
a partial apply does not report it. `s3-keys.json` in the state directory
records each key's credential name and a short digest of what was stored.
A re-apply changes nothing; a plan stores again a pair the state holds and
the store lost. Removing an `S3Key` deletes the pair through the
plan (approved like any change) and then its stored credential, if it still
holds what Piceli wrote. A credential of that name that Piceli did not
store is never replaced (`infra-credential-exists`): delete it first
(`piceli secrets delete NAME`) or pick another name.

The secret is in the OpenTofu state (the provider keeps it there in plain
text), so Piceli creates key pairs only into a state and plan files that
OpenTofu encrypts and refuses to read unencrypted
(`infra-state-unencrypted`); `piceli infra` always configures that.

**What "read" means at OpenStack.** Keystone gives an EC2 credential the
roles its user has in the project, and Swift lets any operator role
(`member` at Infomaniak) read and write every container of the project.
Piceli authenticates with one application credential and cannot create a
user with fewer roles, so `S3Key(access="read")` is a **separate pair, not
a read-only one**: the plan says `read_only_enforced: false`. What keeps it
safe enough for a host: the host only reads with it, Nix and the host agent
verify every build's signature (a write cannot make a host run something
unsigned, only delete or spoil builds), and it can be removed alone. To
limit what a leaked pair reaches, put the cache bucket in an OpenStack
project of its own (a separate `Infrastructure` with that project's
application credential): its keys then reach only the cache. Two more
limits come from Keystone itself: since its OSSA-2026-005 fix, a
*restricted* application credential (the default) may not create EC2
credentials (`infra-s3-key-refused`: use an unrestricted one, or create the
keys yourself), and a `reader`-only identity may not either.

**Rotation** is not a command yet: remove the key, apply, add it back and
apply (a new pair, stored over the old one), then run `piceli host secrets`
again for a read key.

(s3-pairs-you-make-yourself)=
**S3 pairs you make yourself** (0.25.3). Infomaniak's API lets only the
account's own user create EC2 credentials, never an application
credential, restricted or not. The `Infomaniak` preset therefore creates
no S3 keys (`supports_s3_keys` is false), and a `Bucket(keys=[...])` there
is refused when the module loads, before any plan:

```text
infra-s3-keys-unsupported: Bucket my-cache: Piceli cannot create S3 keys at
infomaniak: its API lets only the account's own user create S3 credentials,
not an application credential. Remove keys= and make each pair by hand,
signed in as the project's user: openstack ec2 credentials create; store
it: piceli secrets s3 cache-write --prompt (write), piceli secrets s3
cache-read --prompt (read); then declare NixCache(bucket=...,
write_credentials="cache-write", read_credentials="cache-read")
```

Make each pair signed in as the project's user (the OpenStack CLI with
that user's password, not an application credential), store it, and
name both in the cache:

```sh
openstack ec2 credentials create        # once for the build host, once for the hosts
piceli secrets s3 cache-write --prompt  # access key id, then secret
piceli secrets s3 cache-read --prompt
```

```python
nix_bucket = Bucket("my-cache", provider=cloud)   # no keys=
cache = NixCache("cache-1:…", bucket=nix_bucket,
                 write_credentials="cache-write", read_credentials="cache-read")
```

`piceli secrets status` lists both `pending`, created by `owner`, with
`piceli secrets s3 NAME --prompt` as the next command, until they are
stored; `piceli host secrets` installs the read pair on the host exactly as
it installs a bucket's read key (see {doc}`nix_hosts`). Both pairs have
the user's project roles: the read pair is separate, not read-only, as
described above. `read_credentials=` and a bucket's `S3Key(access="read")`
are alternatives (giving both is refused), and it may not name the write
pair.

To move to a newer OpenStack provider, change `OPENSTACK_PIN` in
`piceli/infra/machines/openstack.py` and take its hashes from `tofu
providers lock` with one `-platform=` per platform the registry lists.

### Porkbun DNS

`Porkbun(credentials=)` keeps `DnsRecord`s at Porkbun, for domains
registered there, whatever provider the servers are at. It renders one
`porkbun_dns_record` per answer for the `marcfrederick/porkbun` OpenTofu
provider (1.3.3, Porkbun API v3, pinned with every platform's hashes);
Porkbun holds no servers.

```python
from piceli.infra import DnsRecord, Infomaniak, Infrastructure, Porkbun, PrimaryIp, Server

cloud, dns = Infomaniak(credentials="cloud"), Porkbun(credentials="porkbun")
host = Server("host-1", provider=cloud, type="a1-ram2-disk20-perf1",
              image="debian-12", ipv4=PrimaryIp("host-1-port"))
infra = Infrastructure("web", servers=[host], records=[
    DnsRecord("example.com", "www", "A", server=host, provider=dns),
    DnsRecord("example.com", "www", "AAAA", server=host, provider=dns),
    DnsRecord("example.com", "@", "TXT", value="v=spf1 -all", provider=dns),
])
```

- **Addresses are references.** A record's answer for a server comes from
  the server's own provider, in the same configuration: a server on a port
  (`PrimaryIp`) answers with the port's address, so a replaced server
  changes nothing; any other server answers with its current address, and
  the record follows it in the same apply. No address is written in the
  configuration.
- **Credentials.** An API key pair (Porkbun: Account → API Access), stored
  by part: `piceli secrets provider porkbun --prompt` asks for the API key
  (`pk1_…`), then the secret API key (`sk1_…`). OpenTofu gets it only as `PORKBUN_API_KEY`
  and `PORKBUN_SECRET_API_KEY`; both are redacted from its output.
- **API access per domain.** Turn it on for each domain (Domain Management
  → Details → API Access). A plan that changes records at Porkbun asks its
  API (`POST /dns/retrieve/DOMAIN`, the stored key pair in the request body
  only) once per domain, so an approved plan does not fail later for this:
  a domain without API access fails the plan with `infra-dns-api-disabled`.
  When the check cannot run (no key pair, no answer within 15 s, another
  refusal such as a wrong key pair) the plan fails with
  `infra-dns-check-failed` rather than pass a check it did not run. The
  plan JSON's `checks` says whether it ran and for which domains
  (`{"check": "porkbun-api-access", "ran": true, "domains": {"example.com":
  "enabled"}}`); the check is not part of the plan hash.
- **Records in the way.** The same answer lists the zone's records. A
  registered domain usually holds records Piceli did not create and never
  touches: a parking `ALIAS` at the apex and a wildcard `CNAME` (Porkbun's
  defaults), `NS`, records added by hand. For each record the plan
  creates, one of them is a conflict when it is a `CNAME` or `ALIAS` at the
  same name, any record at a name where Piceli creates a `CNAME`, or a
  record of the same name and type (for `TXT`, which holds several values,
  the same content: Porkbun refuses that). A record in the ledger is
  Piceli's, so replacing its own record is no conflict. A conflict fails
  the plan with `infra-dns-record-conflict`; its `conflicts` lists each one
  and stderr says the fix: remove or change those records at the registrar
  (Domain Management → DNS), then plan again.

  ```json
  {"state": "rejected", "reason": "infra-dns-record-conflict",
   "message": "records Piceli did not create sit where this plan creates records: ...",
   "conflicts": [{"record": {"type": "ALIAS", "name": "example.com",
                             "content": "pixie.porkbun.com"},
                  "planned": {"type": "A", "name": "example.com"},
                  "why": "a CNAME or ALIAS allows no other record at its name"}],
   "warnings": []}
  ```

  A wildcard (`*.example.com`) answers only names that have no records of
  their own, so it does not conflict with Piceli's explicit names. It is a
  warning in the plan's `checks`, with the names it covers and why, as is
  a `TXT` record with other content beside one Piceli creates:

  ```json
  {"check": "porkbun-record-conflicts", "ran": true, "creates": 2,
   "foreign": 3, "conflicts": [],
   "warnings": [{"kind": "wildcard",
                 "record": {"type": "CNAME", "name": "*.example.com",
                            "content": "pixie.porkbun.com"},
                 "covers": ["www.example.com"],
                 "why": "a wildcard answers only names without records of their own; ..."}]}
  ```
- **Ownership.** Porkbun records carry no labels (the OpenTofu provider
  cannot set their notes), so the ledger alone marks what Piceli created; a
  destroy or update of any other record is `infra-foreign-resource`. Porkbun
  refuses a second record with the same name, type and content, so Piceli
  never takes over one it did not create.
- **TTL and values.** At least 600 s (Porkbun's minimum and default). `TXT`
  values are given without quotes. DNS at the registrar is free: no estimate
  lines.

## Plans and approval

`piceli infra plan` writes `main.tf.json` and `.terraform.lock.hcl` into the
state directory, runs `tofu init`, `tofu plan -json -out=FILE` and `tofu
show -json FILE`, and prints the changes (create, update, replace, delete),
the monthly estimate and the plan hash: `sha256` of the configuration and
lock file, the operation and the plan JSON without its timestamp. Exit 3
(0 when nothing changes).

An apply (or destroy) that fails after changing something exits 1, whatever
refused it, with `"partially_applied": true`, `applied` (address, action and
the provider's id where OpenTofu reports one) and `not_applied` (`failed`
for the change that errored); stderr lists the same. The state keeps what
was applied: plan again, and the next plan shows what remains. A failure
before anything changed keeps its code and exit status.

When OpenTofu fails (`infra-tofu-failed`, `infra-apply-failed`), the error
names the resource or data source it failed on, from OpenTofu's JSON
diagnostics: the message says `tofu plan failed on
data.openstack_networking_network_v2.public-network`, `failed_on` lists the
addresses, and each `tofu:` line on stderr starts with its address. Its
whole output, redacted like everything else, is kept in
`tofu-<command>.log` in the state directory (`tofu_log`, readable by you
only; removed when that command next succeeds); `--verbose` on `infra
plan`, `apply` and `destroy` prints it on stderr.

`piceli infra apply --approve HASH` plans again and applies that plan file
only when its hash is the approved one (`infra-plan-changed` otherwise):
what runs is what was reviewed, also when something changed at the provider
in between. `piceli infra destroy` works the same with a destroy plan and
its own hash, and `piceli infra replace REF SERVER` (0.26.2) with a plan
that also replaces that server ({ref}`above <server-image-change>`).

Piceli changes or deletes only what it created. Before an apply, the
addresses the plan creates go into the ownership ledger
(`inventory.json`). A plan that would update, replace or delete a resource
that is not in the ledger, or does not carry Piceli's labels for this
infrastructure (for example one imported into the state by hand), is
refused whole (`infra-foreign-resource`) before anything runs.

The estimate comes from the provider's prices (Hetzner's `GET /pricing`,
or an OpenStack provider's declared table; net monthly) for the declared
server types and primary IPs; without them it is left out, never an error.

## State: Piceli's, encrypted, never in Git

- **Where.** `Infrastructure(state_dir=)`, default
  `$XDG_STATE_HOME/piceli/infra/<name>` (`~/.local/state/piceli/infra/<name>`),
  mode `0700`. A directory inside a Git work tree is refused
  (`infra-state-in-git`), and so are configuration files Piceli did not write
  there (`infra-state-unexpected`).
- **One at a time.** Every `piceli infra` command holds the directory's lock
  while it runs (`infra-state-locked`).
- **Encrypted.** OpenTofu's own state encryption (`pbkdf2` key provider,
  `aes_gcm`, enforced) covers the state and plan files. The configuration
  reaches OpenTofu only in its environment (`TF_ENCRYPTION`), with the
  passphrase from the local credential `Infrastructure(state_key=)` (default
  `<name>-state`). An unencrypted state is never written nor read; only
  OpenTofu (1.8 or later) is accepted, never another binary.
- **Back the key up.** `piceli secrets state-key NAME --generate` stores a
  random passphrase in `$PICELI_CREDENTIALS_DIR/NAME.json` (default
  `~/.config/piceli/credentials`); without it the state cannot be read.
  `--prompt` takes one from your password manager instead; replacing an
  existing key needs `--replace`.
- **Remote state (optional).** `backend=HttpState(address, lock_address=,
  credentials=)` stores the state with OpenTofu's `http` backend; it only
  ever receives ciphertext. The password is a local credential, sent as
  `TF_HTTP_PASSWORD`. Changing the backend of an existing state is refused
  (`infra-backend-changed`): move it yourself with `tofu init
  -migrate-state`. A `StateBackend` subclass adds another backend.
- **Records.** Next to the state Piceli keeps small JSON files without
  secrets: `inventory.json` (addresses, ids, labels, the ledger, the last
  estimate), `installs.json`, `registrations.json` and `known_hosts`.

## Secrets

`piceli secrets status MODULE` lists every secret a declaration needs, with
who creates it and the next command, and `piceli secrets setup MODULE` walks
through the missing ones: see {ref}`host-secrets-setup`.

Provider tokens are local credentials, like cluster credentials are local
profiles: `piceli secrets provider NAME --prompt` reads the token from stdin
(typed without echo, or piped) into `$PICELI_CREDENTIALS_DIR/NAME.json`
(`0600`). A token given as an argument or in `HCLOUD_TOKEN` (or
`OS_APPLICATION_CREDENTIAL_SECRET`, `PORKBUN_API_KEY`,
`PORKBUN_SECRET_API_KEY`) is refused
(`infra-credential-refused`) without echoing it, unless you pass
`--from-env` to read the provider's own variables on purpose (scripts). A
credential file other users can read is refused (`infra-credential-unsafe`).
Piceli passes the token to OpenTofu in its environment only
(`HCLOUD_TOKEN`), never as an argument, closes OpenTofu's stdin, passes no
`TF_LOG`, and redacts every token and the passphrase from OpenTofu's output
before it is shown.

### Credentials with two parts

A Porkbun API key pair and an OpenStack application credential (Infomaniak
included) have two parts. `piceli secrets provider` asks for each by the name
the provider's console gives it, says where to find it, and checks it before
anything is stored:

```console
$ piceli secrets provider porkbun --prompt
Step 1 of 2: Porkbun API key (pk1_…). Where: porkbun.com > Account > API Access > Create API Key; …
Porkbun API key (pk1_…): pk1_…
Step 2 of 2: Porkbun secret API key (sk1_…). Where: …
Porkbun secret API key (sk1_…, not echoed):
stored credential porkbun (provider-token, Porkbun API key pair): API key and secret API key; no value was printed
```

| Provider | Parts, in order | `--from-env` |
| --- | --- | --- |
| `porkbun` | Porkbun API key (`pk1_…`), Porkbun secret API key (`sk1_…`) | `PORKBUN_API_KEY`, `PORKBUN_SECRET_API_KEY` |
| `openstack` (`infomaniak`) | Application credential ID (32 hex), Application credential secret | `OS_APPLICATION_CREDENTIAL_ID`, `OS_APPLICATION_CREDENTIAL_SECRET` |
| `hetzner` (`hcloud`) | Hetzner Cloud API token | `HCLOUD_TOKEN` |
| `token` | Provider API token | `PICELI_PROVIDER_TOKEN` |

- **Which provider.** `--provider porkbun|openstack|infomaniak|hetzner|token`;
  else `--infra MODULE:ATTR`, whose provider with `credentials=NAME` tells;
  else the provider stored under NAME before; else the name, alone or
  before a dash (`porkbun`, `porkbun-dns`, `infomaniak`, `hcloud`).
  Otherwise it is one API token.
- **Each part is checked as it is entered**, never printed: not empty, no
  space around it, no `:` inside, and the known shape (Porkbun's `pk1_` and
  `sk1_`, an application credential ID of 32 hex characters). A wrong part
  (a swapped pair, a single `KEY:SECRET` line, only one of the two) is
  refused with `infra-credential-part-invalid`, whose `part` names it
  (`api_key`, `secret_api_key`, …), and nothing is stored. Identifiers
  are echoed as you type them; secrets are not.
- **Piped**, for scripts and tests: one part per line, in the order above:
  `printf '%s\n%s\n' "$KEY" "$SECRET" | piceli secrets provider porkbun --prompt`.
- **Stored by part**: `{"provider": "porkbun", "parts": {"api_key": …,
  "secret_api_key": …}}`. The `stored` line and its JSON name the parts
  (`"parts": ["api_key", "secret_api_key"]`), never their values. A
  credential stored as one `ID:SECRET` value by an earlier version still
  reads.
- **One part stored earlier?** Every plan checks the credentials its
  providers use before any API call and refuses one that lacks a part
  (`infra-credential-incomplete`), with the command to run: "porkbun: one
  part of two stored, re-run `piceli secrets provider porkbun --prompt`".
  Running that command
  replaces the credential; no delete is needed.
  `piceli.infra.machines.credential_parts.credential_reports()` reports
  every stored credential's completeness (names and counts, never values);
  `secrets backup --json` lists the incomplete ones in `incomplete`.

`piceli secrets s3 NAME --prompt` does the same for a bucket's S3 keys: the
S3 access key id (echoed), then the S3 secret access key, stored by part
(`--from-env` reads `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`). The
pairs `infra apply` creates are stored by part the same way. A credential
stored by part needs Piceli 0.25.0 or later to read it.

## Back the secret store up

The store (`$PICELI_CREDENTIALS_DIR`) is the only copy of your provider
tokens, state passphrases and the registered clusters' kubeconfigs: a lost
laptop without a backup means an unreadable OpenTofu state. Keep an
encrypted copy elsewhere:

```console
$ piceli secrets backup-key --out /Volumes/usb/piceli-offline.key --add   # prints age1...
$ piceli secrets backup --to /Volumes/backup/piceli-secrets.age \
    --recipient age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
$ piceli secrets backup --to /Volumes/backup/piceli-secrets.age --force   # later: no flags
```

- **What it holds.** Every file under `$PICELI_CREDENTIALS_DIR`, whatever
  its kind (the `NAME.json` credentials, `kubeconfigs/`, kinds a later
  version adds), and the profiles of `$PICELI_PROFILES_DIR` (pointers; one
  pointing into the store is moved with it on restore). Not the
  `known_hosts` of an infrastructure: it sits next to its state, and
  `piceli infra register` pins the keys again.
- **How it is encrypted.** With the [age](https://age-encryption.org)
  command, so `age -d -i KEY FILE | tar t` reads it without Piceli. The
  tar is streamed into age; no plaintext touches the disk. An existing
  file is replaced only with `--force`.
- **Which recipients.** At least two, kept apart, so one lost key is not
  the end: with one, losing that key loses the store and its backup
  together, and the backup is refused (`secrets-backup-single-recipient`)
  unless you pass `--single-recipient`, which the backup's manifest, the
  remembered marker and `staleness()` record (`single_recipient: true`).
  The recommended pair:
  1. an age key whose identity lives in your password manager
     (`age-keygen`, store the `AGE-SECRET-KEY-1...` line there, pass the
     `age1...` public key);
  2. an offline age key, made by one command that prints only its
     recipient: `piceli secrets backup-key --out /Volumes/usb/piceli-offline.key
     --add` writes the private key (`0600`; refused inside the store, and
     over an existing file without `--force`) and adds its `age1...`
     recipient to the remembered ones. It is the only copy: keep it
     offline, on a USB drive or printed on paper, and delete it from this
     machine.

  A hardware key is the alternative to the offline file:
  [age-plugin-yubikey](https://github.com/str4d/age-plugin-yubikey) creates
  an identity on a YubiKey (`age-plugin-yubikey` to set it up,
  `age-plugin-yubikey --list` prints its `age1yubikey1...` recipient); pass
  that with `--recipient`, and keep the plugin on `PATH` for a restore. An
  SSH key (`ssh-ed25519 ...`) works too. Pass public keys only.
  `--recipients-file FILE` takes them one per line; the recipients are
  remembered in `credentials.backup.json` next to the store, with the time,
  sha256 and names of the last backup (never a value).
- **Is it current?** `backup --json` reports `staleness`: the last backup's
  time, the newest file's, and the names changed or added since.

On a new machine, install Piceli and age, then:

```console
$ piceli secrets restore piceli-secrets.age --identity ~/backup.key   # or the hardware key's plugin
```

Into an empty store it restores at once (files `0600`, directories `0700`)
and remembers the recipients. Into a store that is not empty it lists what
it would add, what is identical and what differs, with a digest, exits 3
and writes nothing; `--approve DIGEST` restores those. An archive with an
absolute path, `..`, a link or a file outside the store is refused
(`secrets-restore-archive-invalid`). Read the identity from the password
manager into a file only for the restore, and delete it afterwards.

(build-host-secrets)=
### Moving a build host's keys there

A `NixRelease(..., builder="mini")` is published by `piceli builder serve
--publish` on the build host `mini`, which reads the cache's signing key and
S3 write pair from its own store. They are made on your machine (`piceli
secrets setup`, `infra apply`); carry them over with the same encrypted
archive, for that machine's key only:

1. **On the build host**, make the key that receives them (or use
   `age-keygen -o ~/.config/piceli/transfer.key`):

   ```console
   mini$ piceli secrets backup-key --out ~/.config/piceli/transfer.key
   recipient: age1...
   ```

2. **On your machine**, write just those secrets, encrypted to it.
   `piceli secrets status MODULE` prints this command, with the names, for
   each secret a release's build host needs:

   ```console
   $ piceli secrets backup --only cache-1 --only cache-write \
       --to /tmp/x.age --recipient age1...
   $ scp /tmp/x.age mini:/tmp/x.age
   $ rm /tmp/x.age
   ```

   `--only NAME` (repeatable) takes credentials of the store by name
   (`secrets-backup-only-unknown` otherwise). It needs `--recipient` (the
   remembered recipients are your own, not the build host's) and one is
   enough; the archive's manifest says `only: true`. It is not a backup of
   your store: the last backup's marker, its staleness and the remembered
   recipients do not change.

3. **On the build host**, restore them and delete the file:

   ```console
   mini$ piceli secrets restore /tmp/x.age --identity ~/.config/piceli/transfer.key
   mini$ rm /tmp/x.age
   ```

   Into an empty store they are added at once; into one that is not empty
   the restore lists what it adds or replaces and exits 3, and `--approve
   DIGEST` writes them. A transfer never becomes the build host's backup
   configuration.

## The OS hand-off and the cluster

1. **Install.** `piceli infra install machines.py:infra edge-1` renders the
   server's `Hook` with its outputs and prints it with a digest (exit 3).
   `--approve DIGEST` runs exactly that command: no shell, your working
   directory and environment minus provider tokens and the state passphrase,
   its output on stderr. For example `nixos-anywhere --flake .#edge-1
   root@{ipv4}`, or a manual step: skip the hook and install by hand.
2. **Register.** `piceli infra register machines.py:infra edge-1` takes the
   server's k3s kubeconfig over SSH: `ssh-keyscan` reads the host keys, the
   preview shows their fingerprints and the digest covers them; approved,
   they are pinned in the state directory's `known_hosts` and `ssh`
   (`StrictHostKeyChecking=yes` against that file only) reads
   `/etc/rancher/k3s/k3s.yaml` (with `sudo -n` when `Ssh(user=)` is not
   root), retrying until k3s has written it. Or give it yourself:
   `--kubeconfig FILE [--context NAME]`. Piceli sets its server to
   `Cluster(api=)` (k3s must list that address in its `--tls-san`), waits up
   to `--wait` seconds for a Ready node (`infra-k3s-not-ready`), stores it
   in `$PICELI_CREDENTIALS_DIR/kubeconfigs/<cluster>.yaml` (`0600`, never in
   Git, never printed) and makes it the profile `Cluster(credentials=)`.
   With `Infrastructure(home=HOME_CLUSTER)` and the multi-cluster
   registration, the home cluster's GitOps controller also gets the new
   cluster's credentials. A server without `cluster=` (a Nix host,
   {doc}`nix_hosts`) is registered for its host keys only: the same preview
   and approval pin them, and `piceli host ... --infra MODULE` reads the
   host against them (0.24.0).
3. **Deploy.** The server's cluster is then a declared cluster like any
   other: `piceli cluster init`, environments, GitOps.

`piceli infra destroy` deletes the servers, IPs, firewalls and records
Piceli created; it leaves the credential profiles of their clusters
(`profiles_left`; `piceli logout NAME`).

## Status and the UI

`piceli infra status machines.py:infra [--json]` reads the records only (no
OpenTofu, no credential, no network): each server's state, id, addresses,
install outcome, cluster registration (with the pinned host-key
fingerprints) and monthly price, the records and the estimate
(`piceli.infra.status.v1`).

`piceli ui serve … --infra machines.py:infra` adds a read-only **Machines**
page to the local UI with the same information; plans and applies stay on
the command line with their approvals.

## Commands

| Command | Does | Exit |
| --- | --- | --- |
| `piceli infra plan REF` | Plan; print changes, estimate, hash | 3 changes, 0 none, 2 refused |
| `piceli infra apply REF --approve HASH` | Re-plan, apply the approved plan | 0, 1 `infra-apply-failed` or partially applied, 2, 3 without `--approve` |
| `piceli infra replace REF SERVER [--approve HASH]` | The same, replacing that server on its port with its current user data (0.26.2) | 0, 1, 2, 3 |
| `piceli infra destroy REF [--approve HASH]` | The same for a destroy plan | 0, 1, 2, 3 |
| `piceli infra status REF [--json]` | Read-only status | 0, 2 |
| `piceli infra install REF SERVER [--approve DIGEST]` | Run the install hook | 0, 1 `infra-install-failed`, 2, 3 |
| `piceli infra register REF SERVER [--kubeconfig F] [--approve DIGEST]` | Register the server's k3s | 0, 1 `infra-k3s-not-ready`, 2, 3 |
| `piceli secrets provider NAME --prompt\|--from-env [--provider P] [--infra REF]` | Store a provider credential, part by part | 0, 2 |
| `piceli secrets state-key NAME --generate\|--prompt` | Store the state passphrase | 0, 2 |
| `piceli secrets delete NAME [--kind KIND] [--yes]` | Delete a stored credential (asks first; never printed) | 0, 2, 3 |
| `piceli secrets backup --to PATH [--recipient R] [--force] [--single-recipient]` | Back the store up, encrypted with age (two recipients) | 0, 1, 2 |
| `piceli secrets backup-key --out PATH [--add] [--force]` | Create the offline age key; print only its recipient | 0, 1, 2 |
| `piceli secrets restore PATH [--identity F] [--approve DIGEST]` | Restore it (plans first into a used store) | 0, 1, 2, 3 |

`--tofu PATH` (or `$PICELI_TOFU`) chooses the OpenTofu binary; otherwise
`tofu` on `PATH`. Error codes: area `infra` in {doc}`reference/errors`.
