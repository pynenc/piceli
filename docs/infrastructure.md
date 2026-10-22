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
| `Server(name, provider=, type=, image=, location=, ipv4=, ipv6=, firewall=, ssh_keys=, install=, ssh=, cluster=, labels=)` | One machine. `ipv4`/`ipv6`: `True` (an address that goes with the server), `False`, or a `PrimaryIp`. `location` defaults to the provider's. `image` is an image name, or an `Image` where the provider uploads them. Without `cluster=` it is a plain host (`register` does not apply). |
| `PrimaryIp(name, kind="ipv4", protect=False)` | A fixed address, created in the server's location and assigned to it; it stays when the server is replaced. `protect=True` turns on delete protection (destroy then fails until it is off). |
| `Firewall(name, rules=[...])`, `Rule.tcp(port, sources=, name=)`, `Rule.udp(...)`, `Rule.icmp(...)` | Inbound rules; a port or a range `(low, high)`; sources default to every address. Everything else is dropped. Outbound is not filtered. |
| `DnsRecord(zone, name, type, value=… \| server=… \| servers=[…], ttl=, provider=)` | One record set (`A`, `AAAA`, `CNAME`, `TXT`; `@` is the apex) in a zone you already have at the provider (default: the first server's). Several servers give one record set with every address; the servers may be at another provider than the record. `ttl` defaults to the provider's (300 s; Porkbun 600 s). Piceli never creates or deletes the zone. |
| `Hook(argv, timeout=3600)` | The command that installs the OS. Placeholders `{name}`, `{id}`, `{ipv4}`, `{ipv6}`, `{address}`. |
| `Ssh(user="root", address=None, port=22, kubeconfig_path="/etc/rancher/k3s/k3s.yaml")` | How Piceli reaches the server to read its k3s kubeconfig. |
| `Image(name, file= \| url=, sha256=, format="qcow2", min_disk_gb=, properties=)` | A disk image to upload (OpenStack). `sha256` is required and checked before the upload (`infra-image-checksum`). |
| `Bucket(name, provider=, protect=False)` | An S3-compatible object-storage bucket (OpenStack Swift). Never emptied: destroying one that holds objects fails. |
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
prices=, ip_prices=, currency="EUR")` renders for the
`terraform-provider-openstack/openstack` OpenTofu provider (3.4.0, pinned
with every platform's hashes). `flavors=` is an allow-list, like
`Hetzner(server_types=)`: a server with another flavor is refused when it
is declared. `Infomaniak(credentials=, region="dc3-a")` fills in
Infomaniak Public Cloud's values: `auth_url`
`https://api.pub1.infomaniak.cloud/identity/v3`, the external network
`ext-net1` (a public IPv4 and IPv6 per port), prices in CHF, and
`flavors=["a1-ram2-disk20-perf1"]`, its smallest (1 vCPU, 2 GB); `dc4-a`
is the other region.

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
| `Image` | `openstack_images_image_v2`, private, named by its checksum: at apply time Piceli copies the file (or downloads the URL) into the state directory, verifies it, uploads it, then removes the copy |
| `Bucket` | `openstack_objectstorage_container_v1` (Swift) |
| `user_data=UserData(NAME)` | `user_data` from the sensitive variable `user_data_<server>`, with `ignore_changes`: read at creation only |

The credential is an application credential (Horizon: Identity →
Application Credentials, or `openstack application credential create`),
stored as `ID:SECRET`: `printf '%s:%s' "$ID" "$SECRET" | piceli secrets
provider cloud --prompt`. OpenTofu gets it only as
`OS_APPLICATION_CREDENTIAL_ID` and `OS_APPLICATION_CREDENTIAL_SECRET`; both
are redacted from its output. S3 access to a bucket needs EC2 credentials
(`openstack ec2 credentials create`; at Infomaniak the endpoint is
`https://s3.pub1.infomaniak.cloud`); Piceli never creates these long-lived
keys. OpenStack has no price API here: `prices=` (monthly net per flavor) and
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

Ownership marks: metadata on servers and buckets, tags on ports, security
groups and images, the description of security group rules. Key pairs carry
none: their name (`<infra>-<digest>`) and the ledger stand in.

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
  as `APIKEY:SECRETKEY`: `printf '%s:%s' "$KEY" "$SECRET" | piceli secrets
  provider porkbun --prompt`. OpenTofu gets it only as `PORKBUN_API_KEY`
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

`piceli infra apply --approve HASH` plans again and applies that plan file
only when its hash is the approved one (`infra-plan-changed` otherwise):
what runs is what was reviewed, also when something changed at the provider
in between. `piceli infra destroy` works the same with a destroy plan and
its own hash.

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

Provider tokens are local credentials, like cluster credentials are local
profiles: `piceli secrets provider NAME --prompt` reads the token from stdin
(typed without echo, or piped) into `$PICELI_CREDENTIALS_DIR/NAME.json`
(`0600`). A token given as an argument or in `HCLOUD_TOKEN` (or
`OS_APPLICATION_CREDENTIAL_SECRET`, `PORKBUN_API_KEY`,
`PORKBUN_SECRET_API_KEY`) is refused
(`infra-credential-refused`) without echoing it, and a credential file other
users can read is refused (`infra-credential-unsafe`). Piceli passes the token
to OpenTofu in its environment only (`HCLOUD_TOKEN`), never as an argument,
closes OpenTofu's stdin, passes no `TF_LOG`, and redacts every token and the
passphrase from OpenTofu's output before it is shown.

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
| `piceli infra destroy REF [--approve HASH]` | The same for a destroy plan | 0, 1, 2, 3 |
| `piceli infra status REF [--json]` | Read-only status | 0, 2 |
| `piceli infra install REF SERVER [--approve DIGEST]` | Run the install hook | 0, 1 `infra-install-failed`, 2, 3 |
| `piceli infra register REF SERVER [--kubeconfig F] [--approve DIGEST]` | Register the server's k3s | 0, 1 `infra-k3s-not-ready`, 2, 3 |
| `piceli secrets provider NAME --prompt` | Store a provider token | 0, 2 |
| `piceli secrets state-key NAME --generate\|--prompt` | Store the state passphrase | 0, 2 |
| `piceli secrets delete NAME [--kind KIND] [--yes]` | Delete a stored credential (asks first; never printed) | 0, 2, 3 |

`--tofu PATH` (or `$PICELI_TOFU`) chooses the OpenTofu binary; otherwise
`tofu` on `PATH`. Error codes: area `infra` in {doc}`reference/errors`.
