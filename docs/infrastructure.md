# Machines with OpenTofu

```{admonition} Maturity: preview
:class: note

New in 0.15.0: one provider (Hetzner Cloud) and one server type to start with. Its parameters may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

Servers, fixed IPs, firewalls and DNS records are declared in typed Python,
like an `App`. Piceli renders the [OpenTofu](https://opentofu.org)
configuration, owns its state (encrypted, locked, never in Git) and applies
a plan only with the approval of its hash, as it does for deploys. Piceli
does not own the operating system: an install hook you declare installs it,
then Piceli waits for the machine's k3s and registers it as a cluster it
deploys to.

```python
# machines.py
from piceli.infra import (
    Cluster, DnsRecord, Firewall, Hetzner, Hook, Infrastructure, PrimaryIp, Rule, Server, Ssh,
)

hcloud = Hetzner(credentials="hcloud", location="fsn1")  # the token: piceli secrets provider
edge = Firewall("edge", rules=[
    Rule.tcp(443, name="https"),
    Rule.udp(3478, name="turn"),
    Rule.tcp(22, sources=["100.64.0.0/10"], name="ssh"),  # SSH from a private network only
])
edge_cluster = Cluster("edge-1", api="https://edge-1.example.net:6443", credentials="edge-1")

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
| `Server(name, provider=, type=, image=, location=, ipv4=, ipv6=, firewall=, ssh_keys=, install=, ssh=, cluster=, labels=)` | One machine. `ipv4`/`ipv6`: `True` (an address that goes with the server), `False`, or a `PrimaryIp`. `location` defaults to the provider's. |
| `PrimaryIp(name, kind="ipv4", protect=False)` | A fixed address, created in the server's location and assigned to it; it stays when the server is replaced. `protect=True` turns on delete protection (destroy then fails until it is off). |
| `Firewall(name, rules=[...])`, `Rule.tcp(port, sources=, name=)`, `Rule.udp(...)`, `Rule.icmp(...)` | Inbound rules; a port or a range `(low, high)`; sources default to every address. Everything else is dropped. Outbound is not filtered. |
| `DnsRecord(zone, name, type, value=… \| server=… \| servers=[…], ttl=300, provider=)` | One record set (`A`, `AAAA`, `CNAME`, `TXT`; `@` is the apex) in a zone you already have at the provider. Several servers give one record set with every address. Piceli never creates or deletes the zone. |
| `Hook(argv, timeout=3600)` | The command that installs the OS. Placeholders `{name}`, `{id}`, `{ipv4}`, `{ipv6}`, `{address}`. |
| `Ssh(user="root", address=None, port=22, kubeconfig_path="/etc/rancher/k3s/k3s.yaml")` | How Piceli reaches the server to read its k3s kubeconfig. |
| `Infrastructure(name, servers=, records=, state_dir=, state_key=, backend=, home=)` | Everything one OpenTofu state holds. |

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
environment variable of its token; optionally `prices()`), so another
provider (OVH, Infomaniak) needs no change elsewhere.
`piceli.testing.infra.FakeProvider` renders OpenTofu's built-in
`terraform_data` instead: a real plan, apply and encrypted state, nothing
created anywhere (see {doc}`testing`).

## Plans and approval

`piceli infra plan` writes `main.tf.json` and `.terraform.lock.hcl` into the
state directory, runs `tofu init`, `tofu plan -json -out=FILE` and `tofu
show -json FILE`, and prints the changes (create, update, replace, delete),
the monthly estimate and the plan hash: `sha256` of the configuration and
lock file, the operation and the plan JSON without its timestamp. Exit 3
(0 when nothing changes).

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
net monthly) for the declared server types and primary IPs; without them
it is left out, never an error.

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
(`0600`). A token given as an argument or in `HCLOUD_TOKEN` is refused
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
   cluster's credentials.
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
| `piceli infra apply REF --approve HASH` | Re-plan, apply the approved plan | 0, 1 `infra-apply-failed`, 2, 3 without `--approve` |
| `piceli infra destroy REF [--approve HASH]` | The same for a destroy plan | 0, 1, 2, 3 |
| `piceli infra status REF [--json]` | Read-only status | 0, 2 |
| `piceli infra install REF SERVER [--approve DIGEST]` | Run the install hook | 0, 1 `infra-install-failed`, 2, 3 |
| `piceli infra register REF SERVER [--kubeconfig F] [--approve DIGEST]` | Register the server's k3s | 0, 1 `infra-k3s-not-ready`, 2, 3 |
| `piceli secrets provider NAME --prompt` | Store a provider token | 0, 2 |
| `piceli secrets state-key NAME --generate\|--prompt` | Store the state passphrase | 0, 2 |

`--tofu PATH` (or `$PICELI_TOFU`) chooses the OpenTofu binary; otherwise
`tofu` on `PATH`. Error codes: area `infra` in {doc}`reference/errors`.
