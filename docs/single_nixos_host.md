# A single NixOS host

```{admonition} Maturity: preview
:class: note

New in 0.24.0. OpenStack machines, Porkbun DNS, Nix builds and Nix host
releases are all preview: parameters may still change in a minor release,
with a changelog entry.
```

This guide takes one NixOS machine from nothing to a host that follows your
release tags on its own. One typed-Python file declares everything: the
machine, its address, firewall and key, the disk image it boots, the
bootstrap key it joins your tailnet with (port 22 is never open to the
internet), the bucket
that holds the Nix binary cache, two DNS records, and the host agent that
deploys every new `v*` tag, checks it and rolls back when a check fails. No
Kubernetes cluster is involved, and none has to be up.

The pieces have their own pages: {doc}`infrastructure` (machines, images,
buckets, DNS), {doc}`dev_builds` (Nix builds and the signed cache) and
{doc}`nix_hosts` (the host agent). The end of this page compares a single
host with a Kubernetes environment.

## The declaration

```python
# web_1.py
from piceli.infra import (
    Bucket,
    BuildHost,
    DevBuilds,
    DevProfile,
    DnsRecord,
    Firewall,
    HostCheck,
    HostReport,
    Image,
    Infomaniak,
    Infrastructure,
    NixCache,
    NixHost,
    Porkbun,
    PrimaryIp,
    Rule,
    Server,
    Ssh,
    Tag,
    UserData,
)

# An OpenStack cloud (Infomaniak's preset: its smallest flavor only, unless
# flavors=[...] lists more) and the registrar's DNS.
cloud = Infomaniak(credentials="infomaniak", prices={"a1-ram2-disk20-perf1": 4.70})
dns = Porkbun(credentials="porkbun")

# The first system: `piceli dev nix build ... --image` (step 2) writes the
# image and this sidecar with its file and sha256.
nixos = Image.from_build("web-1-nixos", "images/web-1-image.image.json")

web = Server(
    "web-1",
    provider=cloud,
    type="a1-ram2-disk20-perf1",
    image=nixos,
    ipv4=PrimaryIp("web-1-port", protect=True),  # a port that outlives the server
    firewall=Firewall(  # a security group: inbound HTTP and HTTPS only, no SSH
        "web",
        rules=[Rule.tcp(80, name="http"), Rule.tcp(443, name="https")],
    ),
    ssh_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyOnly owner@example.org"],
    # Read once, at creation: a pre-authorized tailnet key (step 2).
    user_data=UserData("web-1-boot"),
    ssh=Ssh(user="root", address="web-1.tail-example.ts.net"),  # over the tailnet
)

# The Nix binary cache: a bucket, a signing key and S3 keys that write
# (builds, gc). The host reads it with separate keys, in sops-nix.
nix_bucket = Bucket("example-nix-cache", provider=cloud, protect=True)
cache = NixCache(
    # The key does not exist yet: name it. Step 1's `piceli secrets setup`
    # generates it and prints its public key; then replace this line with
    # it: NixCache("example-cache-1:<base64>", bucket=..., ...).
    key_name="example-cache-1",
    bucket=nix_bucket,
    write_credentials="nix-cache-write",
)

infra = Infrastructure(
    "web-1",
    servers=[web],
    records=[  # both follow the port's addresses
        DnsRecord("example.org", "@", "A", server=web, provider=dns),
        DnsRecord("example.org", "@", "AAAA", server=web, provider=dns),
    ],
    buckets=[nix_bucket],
)

# The host agent: the newest v* tag, checked, reported over OTLP. Its
# secrets are sops-nix's, decrypted with the age identity `web-1-age`.
host = NixHost(
    "web-1",
    server=web,
    flake=".#web-1",
    follow=Tag("v*"),
    cache=cache,
    checks=[
        HostCheck.unit("nginx.service"),
        HostCheck.http("http://127.0.0.1/healthz", timeout=90),
    ],
    report=HostReport("https://otel.example.org:4318",
                      headers_file="/run/secrets/piceli/otlp-headers"),
    age_identity="web-1-age",
)

# Where `piceli dev nix build` runs: a NixOS build machine over SSH. No
# cluster: node=None, and no cluster is ever contacted.
builds = DevBuilds(
    node=None,
    image="ghcr.io/pynenc/piceli-builder@sha256:" + "0" * 64,
    hosts=[BuildHost.docker("nix-1", context="ssh://builder@nix-1.example.org")],
    profiles=[DevProfile.for_nix(timeout="2h")],
)
```

The flake has a `nixosConfigurations.web-1` (nginx, your service, a
`/healthz`) and two outputs of it: `config.system.build.toplevel`, the
system the host switches to, and `config.system.build.openstackImage`, the
disk image it first boots. Replace the SSH key, the tailnet name and the
builder image's digest with yours; the commands below say where each comes
from. The cache names its key with `key_name=` until step 1 creates it:
`secrets status` and `setup` and `infra plan` work meanwhile, while
publishing, `dev nix gc` and `host module` refuse
(`dev-nix-cache-key-unset`) until the public key is in the declaration.

The system joins the tailnet on its first boot with the key in its user
data. NixOS's OpenStack image fetches the user data to
`/etc/ec2-metadata/user-data` (`0600` root); Tailscale reads it from there:

```nix
services.tailscale.enable = true;
services.tailscale.authKeyFile = "/etc/ec2-metadata/user-data";
systemd.services.tailscaled-autoconnect = {
  after = [ "openstack-init.service" ];  # the service that fetches it
  requires = [ "openstack-init.service" ];
};
# Otherwise amazon-init reads the user data as a NixOS configuration.
virtualisation.amazon-init.enable = false;
networking.firewall.trustedInterfaces = [ "tailscale0" ];  # SSH on the tailnet only
```

On an image with cloud-init (Debian, Ubuntu), store a cloud-config
instead: `#cloud-config`, then `runcmd: [[tailscale, up, --auth-key=...]]`
after installing Tailscale.

## Steps

1. **Secrets.** Each is stored once, `0600`, in Piceli's credentials
   directory, and never printed. `piceli secrets status web_1.py` lists
   them with their state and the next command, and `piceli secrets setup
   web_1.py` walks through the missing ones in this order
   ({ref}`host-secrets-setup`); one by one:

   ```sh
   piceli secrets state-key web-1-state --generate        # encrypts the OpenTofu state
   piceli secrets provider infomaniak --prompt            # application credential ID, then secret
   piceli secrets provider porkbun --prompt               # API key (pk1_…), then secret API key (sk1_…)
   piceli secrets nix-cache example-cache-1 --generate    # prints example-cache-1:<base64>
   piceli secrets s3 nix-cache-write --prompt              # key id, then secret: put and delete
   piceli secrets age web-1-age --generate                 # prints age1...: the host's sops key
   piceli secrets user-data web-1-boot --prompt           # the tailnet key, just before step 2
   ```

   The Infomaniak credential is an application credential (its ID, then its
   secret), Porkbun's an API key pair (API key, then secret API key) with API
   access turned on for `example.org`; each part is asked for, checked and
   stored on its own (piped: one per line).
   The S3 keys are EC2 credentials (`openstack ec2 credentials create`).
   Put the printed cache public key in the declaration, in place of
   `key_name="example-cache-1"` (`NixCache("example-cache-1:<base64>",
   bucket=nix_bucket, ...)`: `secrets status` shows it `copy` until
   then), and the `age1...` key in your `.sops.yaml`. The host's own secrets do not go into Piceli:
   step 3 puts them in sops-nix.

   The user data is a Tailscale auth key you mint just before the first
   apply (admin console, Settings, Keys): **single-use, pre-authorized,
   tagged `tag:example`, expiring in one hour**. Any process on the
   instance can read its user data from the metadata service, so the key
   must be useless once the host has joined. Your tailnet policy lets you
   reach `tag:example` on port 22.

2. **The image, then the machines.** Build the image on the build host;
   it lands in `images/` with `images/web-1-image.image.json`, the sidecar
   `Image.from_build` reads (the file, its sha256, the store path, the
   commit):

   ```sh
   export PICELI_DEV_CLUSTER=web_1.py:builds          # the DevBuilds
   piceli dev nix build nixosConfigurations.web-1.config.system.build.openstackImage \
     --image --artifacts-dir images --json
   piceli infra plan web_1.py:infra
   piceli infra apply web_1.py:infra --approve sha256:<plan hash>
   piceli infra register web_1.py:infra web-1         # exit 3: its SSH host keys
   piceli infra register web_1.py:infra web-1 --approve sha256:<digest>
   ```

   `infra plan` exits 3 with the changes (image, port, security group and
   its rules, key pair, server, bucket, two Porkbun records), the monthly
   estimate and the plan hash; it also checks Porkbun's API access for
   `example.org`, and that no record Piceli did not create (Porkbun's
   parking `ALIAS` at the apex, for example) sits where it creates one:
   delete those at Porkbun first (`infra-dns-record-conflict` lists them). It shows `user data of web-1: secret web-1-boot
   (sha256:...)`: only a plan that creates the server reads the key, passes
   it to OpenTofu as a sensitive variable in its environment, and the plan
   hash covers its digest. The key is never in the configuration, the plan
   output or Piceli's records; the provider keeps only its SHA-1 in the
   encrypted state. `infra apply` uploads the image (its sha256 checked
   first), creates the rest and points both records at the port. Nothing
   runs without the hash you approved.

   The first boot joins the tailnet as `web-1`. From then on the user
   data is ignored: later plans never read it and never replace the server
   for it. Delete the spent key: `piceli secrets delete web-1-boot --kind
   user-data` asks you to type its name (`--yes` skips that; it never
   prints the value). A plan that has to replace the server then stops with
   `infra-user-data-missing` until you store a fresh one.

   Everything else goes over the tailnet, to `Ssh(address=)`. `infra
   register` reads the server's SSH host keys and shows their
   fingerprints: compare them with the console, then approve, and they are pinned in the infrastructure's
   state. Every `piceli host ... --infra web_1.py` reads the host with
   strict host key checking against them, and finds `web-1` by its name.

3. **The age identity with `piceli host secrets`; cache keys and OTLP
   headers in sops-nix.** The host's secrets live encrypted in Git, in
   `secrets/web-1.yaml` (`sops secrets/web-1.yaml`, encrypted to the
   `age1...` key): `piceli/cache.env` with the cache's read keys
   (`AWS_ACCESS_KEY_ID=...` and `AWS_SECRET_ACCESS_KEY=...` lines: a second
   EC2 credential, never the write one; at Infomaniak it is not limited to
   reading, see {doc}`infrastructure`) and `piceli/otlp-headers`
   (`Authorization: Bearer ...`). The configuration declares them:

   ```nix
   sops.defaultSopsFile = ./secrets/web-1.yaml;
   sops.age.keyFile = "/var/lib/sops-nix/key.txt";
   sops.secrets."piceli/cache.env" = { };
   sops.secrets."piceli/otlp-headers" = { };
   ```

   sops-nix decrypts them to `/run/secrets/piceli/...` (`0400` root): the
   agent's default for the cache's keys and the `headers_file=` above. The
   only secret Piceli puts on the host is the age identity they decrypt
   with:

   ```sh
   piceli host secrets web-1 --infra web_1.py    # exit 3: host, path, 0600 root, age1..., existing key
   piceli host secrets web-1 --infra web_1.py --approve sha256:<digest>
   ```

   It goes over the tailnet, on ssh's stdin, to
   `/var/lib/sops-nix/key.txt`, and the host re-runs its activation so
   sops-nix decrypts. A host that already has another key is refused
   unless you pass `--replace`.

   Since 0.25.0 Piceli can make the cache's keys itself: declare
   `Bucket(..., keys=[S3Key("nix-cache-write"), S3Key("nix-cache-read",
   access="read")])`, drop `write_credentials=` and `piceli secrets s3`,
   and leave `piceli/cache.env` out of sops. `infra apply` creates and
   stores both pairs, and `piceli host secrets` also writes the read pair
   to `/var/lib/piceli-host/cache.env`. At Infomaniak that pair is separate
   but not read-only (see {doc}`infrastructure`), and the application
   credential must be unrestricted to create it.

4. **The agent's module, into the flake.**

   ```sh
   piceli host module web_1.py:host --output hosts/web-1/piceli-host.nix
   ```

   Commit the file and import it from `nixosConfigurations.web-1`
   (`imports = [ ./hosts/web-1/piceli-host.nix ];`). Every system built
   from then on carries the agent. Render it before you build the image of
   step 2 and the first boot already runs the agent; otherwise switch once
   by hand (`nixos-rebuild switch --flake .#web-1 --target-host
   root@web-1.tail-example.ts.net`).

5. **A release.** Tag the commit and publish its build:

   ```sh
   git tag v1.0.0 && git push origin v1.0.0
   piceli dev nix build nixosConfigurations.web-1.config.system.build.toplevel \
     --ref v1.0.0 --publish web_1.py:cache --json      # exit 3: the publish plan
   piceli dev nix build nixosConfigurations.web-1.config.system.build.toplevel \
     --ref v1.0.0 --publish web_1.py:cache --approve sha256:<digest> --json
   ```

   The build runs isolated, with no key. A separate step signs the closure
   as it uploads it, then Piceli uploads the signed manifest and the
   pointer `piceli/web-1/refs/tags/v1.0.0.json`. The result's `pointers`
   carry its `published_at`.

6. **Wait for the host.** Within `poll_seconds` (300) the agent finds
   `v1.0.0`, verifies the pointer, the manifest and the narinfo against the
   cache key, fetches the system, switches and runs the checks.

   ```sh
   piceli host wait web-1 v1.0.0 --infra web_1.py --json
   ```

   It reads `/var/lib/piceli-host/status.json` over SSH (or give a target:
   `root@web-1.tail-example.ts.net`). It exits 0 once `v1.0.0` runs with its checks
   passed, and 1 on a refused or failed attempt (`host-deploy-failed`), a
   rollback (`host-rolled-back`) or after `--timeout` (`host-wait-timeout`).
   The JSON carries the attempt's stages with their seconds. The same
   events reach the OTLP collector, one trace per deploy.

7. **A rollback.** When a check fails, the agent switches back to the
   system it ran before, marks the build bad and reports it; `host wait`
   exits 1 with `host-rolled-back`, and `piceli host status web-1 --infra web_1.py`
   shows the failed check. The host keeps the old system and never tries
   that build again until it is published again (publishing the same
   commit again retries it; pass the new pointer's `published_at` to `host
   wait --published-after` so an earlier failure does not count). To go
   back on purpose:

   ```sh
   piceli host rollback web-1 --infra web_1.py --json      # the previous system, held
   piceli host pin web-1 v1.0.0 --infra web_1.py --json    # or any published tag, held
   piceli host unpin web-1 --infra web_1.py --json         # follow the newest v* again
   ```

   Both switch, run the checks and hold the build until `unpin`, across
   reboots: the next poll does not deploy the release you left.

8. **Clean the cache.**

   ```sh
   piceli dev nix gc --cache web_1.py:cache                # exit 3: what goes, a digest
   piceli dev nix gc --cache web_1.py:cache --approve sha256:<digest>
   ```

   It keeps the builds of the newest five tags and of `main`, everything
   their closures reach and anything younger than an hour; it deletes old
   tag pointers before their manifests, so the host never finds a pointer
   whose build is gone.

## A single NixOS host or a Kubernetes environment

Piceli deploys to both. A Kubernetes environment is an `App` deployed to a
cluster by `piceli deploy` or the GitOps controller ({doc}`environments`,
{doc}`gitops`); a single host runs one NixOS system that the agent replaces
as a whole.

| | Single NixOS host | Kubernetes environment |
| --- | --- | --- |
| Rollout | The whole system switches at once, on the next poll (300 s by default) | Rolling updates per workload, gated by readiness; the controller deploys each new commit |
| Rollback | Automatic when a check fails: back to the previous system, the build marked bad. On purpose: `piceli host rollback` or `pin`, held until `unpin` | `piceli release rollback` to an earlier release; restore points for retained data ({doc}`restore_points`) |
| Health checks | `HostCheck` units, HTTP and commands after each switch | Probes on every pod, release checks before and after a rollout ({doc}`checks`) |
| Scaling | One machine: a bigger flavor | Replicas over several nodes, an `Autoscaler` |
| Multi-node | None: one host is one failure domain; a second host is a second declaration | Built in: pods move when a node fails |
| Secrets | sops-nix in Git, decrypted with one age identity that `piceli host secrets` puts on the host; the module holds none | Kubernetes Secrets from `piceli secrets` and the release ({doc}`secrets`) |
| Observability | OTLP/HTTP logs of each deploy, stage and check; `host status` over SSH | OTLP traces, events and metrics of the controller ({doc}`opentelemetry`), the UI, `piceli status` |
| Cost and machines | One small server and a bucket; the builds run on a machine you already have | At least one node per cluster (three for a highly available control plane) plus the home cluster |
| When the home cluster fails | Nothing changes: the host deploys from the cache with every cluster down | The controller stops deploying until it is back; running workloads keep running |
| Operations load | NixOS upgrades come with your flake; the agent needs no daemon or controller | k3s or a managed cluster to upgrade, its add-ons, the controller |

**Choose a single host** for one service that fits one machine and may
restart for a minute: a site, an API with a local database, an edge proxy,
a TURN or DNS server. It costs one server, and nothing in it depends on a
cluster, so it is also the place for what must keep working when the
clusters are down.

**Choose a Kubernetes environment** when the service needs more than one
machine, zero-downtime rollouts, many services that share nodes, or
per-branch environments. The cluster's operations cost is then shared by
everything on it.

**Both coexist.** They share one machine model and one operating loop:

- the same `piceli infra plan` and `apply` with the approval of a plan
  hash, one encrypted state and one ownership ledger, for the host's server
  and for cluster nodes alike;
- the same approvals for what ships: a published build is approved like a
  deploy plan, and only what you approved reaches the host;
- the same OTLP collector: point `HostReport` at the endpoint the
  controller's `Otlp` uses and host deploys sit next to cluster deploys,
  with the commit on every event;
- the same Git tags: a tag can publish the host's system and deploy the
  cluster's release.
