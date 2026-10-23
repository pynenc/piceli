# Nix host releases

A NixOS host can follow a Git ref of its flake on its own: an agent on the
host finds the newest build of the ref in a binary cache, checks every
signature, fetches the prebuilt system, switches to it, runs your checks
and switches back when one fails. The host never builds and never needs Git
or the repository's credentials, and no cluster is involved: it keeps
working with every cluster down.

The builds come from `piceli dev nix build --publish` ({doc}`dev_builds`),
which signs the closure, its manifest and the ref pointers with the cache's
key.

The GitOps controller can also publish them itself when a tag appears
(0.25.0, a `NixRelease`): the build and the keys stay on a build host. See
[Nix host releases](#gitops-nix-releases) in {doc}`gitops`. A remote
machine that only publishes is declared as a docker host with an SSH
context to itself and `runs=False`, `BuildHost.docker("nix-builder",
context="ssh://builder@builder-1.example.org", runs=False)`, and runs
`piceli builder serve --host nix-builder --publish MODULE:ATTR` (0.25.0):
it claims no image build, and no development run lands on it. `BuildHost.native` would be wrong there: it means
whichever machine runs Piceli.

{doc}`single_nixos_host` takes one host from nothing to its first release
with one complete example, and compares it with a Kubernetes environment.

## Declare the host

```python
from piceli.infra import Branch, HostCheck, HostReport, NixCache, NixHost, Tag

cache = NixCache(
    "my-cache-1:<base64 public key>",
    url="s3://nix-cache?endpoint=s3.example.net&region=us-east-1",
    write_credentials="cache-s3-write",  # the builder only
)

web = NixHost(
    "web-1",
    server=web_server,  # a piceli.infra.Server, or "ssh://root@web-1.example.net"
    flake=".#web-1",  # nixosConfigurations.web-1 of the flake
    follow=Tag("v*"),  # or Branch("main")
    cache=cache,
    checks=[
        HostCheck.unit("nginx.service"),
        HostCheck.http("http://127.0.0.1/healthz", timeout=90),
        HostCheck.command("test", "-s", "/var/lib/app/ready"),
    ],
    report=HostReport("https://collector.example.net:4318",
                      headers_file="/run/secrets/piceli/otlp-headers"),
    age_identity="web-1-age",  # its sops-nix key: piceli host secrets
)
```

| Field | Meaning (default) |
| --- | --- |
| `server=` | A `Server` Piceli created (its `Ssh(user=, port=, address=)`) or an SSH target `ssh://[user@]host[:port]` |
| `flake=` | `.#NAME` (the system of `nixosConfigurations.NAME`) or a full `.#ATTR`; the agent switches only to a build of this attribute |
| `follow=` | `Tag(pattern="v*", prereleases=False)` or `Branch(name="main")` |
| `cache=` | The `NixCache` the builds were published to; an `https://` cache can follow a `Branch` only (tags are listed) |
| `checks=` | `HostCheck.unit(UNIT)`, `HostCheck.http(URL)` (2xx), `HostCheck.command(ARG, ...)` (exit 0); each tried every 2 s until its `timeout=` (60 s) |
| `report=` | `HostReport(endpoint, headers_file=None, insecure=False)`: OTLP/HTTP logs |
| `build=` | The manifests' name, `--name` of the publish (`NAME` of `.#NAME`, else the host's name) |
| `read_credentials_file=` | The host's file with the cache's read S3 keys. When the cache's bucket declares `S3Key(access="read")` (0.25.0): `/var/lib/piceli-host/cache.env`, which `piceli host secrets` writes. Otherwise a sops-nix secret (`/run/secrets/piceli/cache.env` for an `s3://` cache, none otherwise). A path you give is your own file: `host secrets` leaves it alone |
| `poll_seconds=`, `history=` | How often the agent polls (300) and how many attempts the status keeps (20) |
| `generations=` | How many system generations the host keeps (5, at least 2: a rollback needs the previous one); see [Old systems](#old-systems) |
| `age_identity=`, `age_key_file=` | The local credential with the host's age identity, and where `piceli host secrets` puts it (`/var/lib/sops-nix/key.txt`, sops-nix's `sops.age.keyFile`) |

`Tag("v*")` follows the highest semantic version among the matching tags (an
optional `v`, `MAJOR.MINOR.PATCH`, `-prerelease`, `+build`). A matching tag
that is not a version is ignored, and the status lists it under
`ignored_tags`. Pre-releases count only with `prereleases=True`. A
declaration that does not hold is refused with `host-invalid` or
`host-check-invalid`.

### Several hosts in one repository: tag prefixes

Give each host its own tag prefix, and release each one with its own
tags:

```python
web = NixHost("web-1", ..., follow=Tag("web-v*"))   # web-v1.2.3
api = NixHost("api-1", ..., follow=Tag("api-v*"))   # api-v0.4.0
```

A tag counts for a host when it matches the glob **and** what follows the
pattern's literal prefix is a version as above. The prefix (`Tag.prefix`)
is the pattern before its first `*`, `?` or `[`, without the digits and
dots of a version it pins:

| Pattern | Prefix | Counts | Never counts |
| --- | --- | --- | --- |
| `v*` | `v` | `v1.2.3` | `web-v1.2.3` (no match) |
| `web-v*` | `web-v` | `web-v1.10.0` above `web-v1.9.0` | `api-v2.0.0`, `v3.0.0`, `web-vNext` (ignored) |
| `web-*` | `web-` | `web-1.2.3`, `web-v1.2.3` | `web-api-1.2.3` (ignored: `api-1.2.3` is no version) |
| `web-v1.*` | `web-v` | `web-v1.4.0` | `web-v2.0.0` (no match) |
| `*` | (none) | `1.2.3`, `v1.2.3` | `web-v1.2.3` (ignored) |

So two hosts with different prefixes never see each other's tags, in the
agent's resolve and ordering ("never older" included), in the pointer
`dev nix build --publish --tag-pattern 'web-v*'` writes, and in the
versions `dev nix gc --tag-pattern 'web-v*' --tag-pattern 'api-v*'` keeps
(see {doc}`dev_builds`). Put the prefix before the first wildcard:
`Tag("*-v*")` has no prefix, so `web-v1.2.3` is not a version for it.
`Tag(...).version(tag)` gives a tag's sort key, `None` when it does not
count.

### Only verified products: `gate=`

`NixHost(..., gate=Promotion(input="app", environment="main"))` (0.25.0)
makes the agent switch only to a build whose signed manifest carries the
promotion gate's verified `pinned` for that flake input and GitOps
environment; any other build is refused (`host-promotion-missing`). The
gate and `piceli promote` are described in {doc}`gitops` (Nix host
releases).

## Install the agent

```sh
piceli host module hosts.py:web --output hosts/web-1/piceli-host.nix
```

The command writes one self-contained NixOS module: the agent (one
standard-library Python file), its verifier, the host's configuration as
JSON, a systemd oneshot `piceli-host-agent.service` and its timer. Commit the
file and import it from the host's configuration (`imports = [
./piceli-host.nix ];`); Piceli never edits your flake. Install the first
system as usual (`nixos-rebuild`, or an image from `dev nix build --image`).
From then on the agent maintains itself, because it is part of every system
it switches to. Render the module again after you change the declaration or
upgrade Piceli.

## Secrets: one age key, the rest in sops-nix

The module holds no secret. The host's secrets live in Git, encrypted with
[sops-nix](https://github.com/Mic92/sops-nix): the cache's read S3 keys
(never the write ones; since 0.25.0 Piceli can create and install them,
below), the report's headers and your services'
secrets. The only secret Piceli puts on the host is the key sops-nix
decrypts them with, an age identity:

```sh
piceli secrets age web-1-age --generate      # prints age1...: put it in .sops.yaml
piceli host secrets hosts.py:web              # exit 3: the plan and its digest
piceli host secrets hosts.py:web --approve sha256:<digest>
```

`piceli secrets age NAME` stores the identity `0600` in Piceli's
credentials and prints only its public key (`--prompt` imports one,
`AGE-SECRET-KEY-1...`, from stdin). `piceli host secrets` plans first: the
host, the path (`age_key_file=`), mode `0600`, owner root, the public key,
and whether the host has the same key, another one or none. It compares
fingerprints: the host never sends its key back. Approved, the key goes on
ssh's stdin into a temporary file that is renamed over the path, and the
host re-runs its activation (`switch-to-configuration switch`) so sops-nix
decrypts with it. Another key is replaced only with `--replace`
(`host-secret-exists`); the same key is a no-op.

**The cache's read keys from Piceli** (0.25.0). When the cache's bucket
declares `S3Key("cache-read", access="read")`, `piceli infra apply`
creates that pair and stores it (see {doc}`infrastructure`), and `piceli
host secrets` also puts it on the host, at `/var/lib/piceli-host/cache.env`
(`0600` root; `AWS_ACCESS_KEY_ID=` and `AWS_SECRET_ACCESS_KEY=` lines), the
agent's default `read_credentials_file=` for such a cache. The plan's
`cache` shows the path, the credential, a short digest and whether the host
has the same file, another one or none (compared by sha256, never read
back). A different file is a rotated pair: it is replaced without
`--replace`, and nothing is re-activated, since the agent reads the file on
each poll. A host without `age_identity=` gets only this file. The pair is
not read-only at OpenStack (the plan of `infra apply` says
`read_only_enforced: false`); see {doc}`infrastructure` for what that means
and how to limit it. The sops-nix path below still works: give
`read_credentials_file=` the path of your sops secret, and keys you created
yourself.

Declare the secrets in the host's configuration, encrypted to that public
key:

```nix
sops.defaultSopsFile = ./secrets/web-1.yaml;
sops.age.keyFile = "/var/lib/sops-nix/key.txt";
sops.secrets."piceli/cache.env" = { };      # AWS_ACCESS_KEY_ID=... and AWS_SECRET_ACCESS_KEY=... lines
sops.secrets."piceli/otlp-headers" = { };   # Authorization: Bearer ...
```

sops-nix decrypts them to `/run/secrets/piceli/cache.env` and
`/run/secrets/piceli/otlp-headers` (`0400` root), the agent's default for
an `s3://` cache and the path to give `HostReport(headers_file=)`. Any
other path works: `read_credentials_file=` and `headers_file=` take the
`path` of your sops secret. The agent refuses a file that its group or
others can read, or that root does not own (`host-credentials-exposed`). A
`file://` or `https://` cache needs no keys.

## What the agent does

Each poll (one minute after boot, then every `poll_seconds`) goes through
these stages:

1. **resolve**: it reads the signed pointer `piceli/BUILD/refs/heads/BRANCH.json`,
   or lists `refs/tags/` and takes the highest matching version. It refuses
   a pointer that is not signed by the cache's key (`host-signature-invalid`)
   or whose `published_at` is older than one it already saw
   (`host-pointer-replayed`). It never moves to an older version than the one
   it runs.
2. **verify**: it fetches the manifest and checks that its sha256 is the
   pointer's, its signature, its attribute and its system
   (`host-manifest-mismatch`, `host-system-mismatch`). Then it checks the
   store path's `.narinfo`: its signature and that its `NarHash` is the
   manifest's (`host-narinfo-mismatch`). Nothing is fetched into the store
   before all of these hold.
3. **realise**: `nix copy --from` the cache with `builders = ""`,
   `max-jobs = 0`, `substituters = ""`, `require-sigs = true` and only the
   cache's key in `trusted-public-keys`. The host cannot build, and it
   trusts no other key. Then it checks the realised path's NarHash.
4. **switch**: `nix-env -p /nix/var/nix/profiles/system --set PATH`, then
   `PATH/bin/switch-to-configuration switch`. The switch never restarts the
   agent itself.
5. **checks**: in order. The first failure stops them.
6. **rollback**: after a failed switch or check, the agent switches back to
   the previous system and marks the build bad. A bad build is not tried
   again until a newer pointer for it (publish the same commit again to
   retry it). A failed fetch is not a bad build: the next poll tries again.

A system the agent did not switch to (the first install, a manual
`nixos-rebuild`) becomes its current one. When it is a published build, the
agent records its commit and tag.

## Pin, unpin and roll back

```sh
piceli host pin hosts.py:web v1.3.0 --json     # deploy v1.3.0 (older too) and hold it
piceli host unpin hosts.py:web --json          # follow Tag("v*") again
piceli host rollback hosts.py:web --json       # back to the previous system, held
```

Each runs the agent once over SSH (`piceli-host-agent pin TAG`, a command
the module puts on the host's PATH; as root, else `sudo -n`), after a poll
in progress (`host-busy` after 10 minutes). None needs an approval: they go
only to a build that was signed, published and verified, or to the system
the host ran before.

- **pin** resolves the tag's signed pointer and verifies the manifest and
  narinfo as a poll does, then fetches, switches and runs the checks. It may
  go to an older version: that is the point. When a check fails it switches
  back, exits 1 and holds nothing (`host-pin-not-found` when the tag has no
  published build).
- **rollback** switches to the previous system, already in the store
  (`host-rollback-unavailable` when there is none), and runs the checks. It
  also holds that system: otherwise the next poll would deploy the build
  you rolled away from. A second rollback returns to it. A failed check
  switches forward again (exit 1). It marks no build bad.
- **unpin** drops the hold and starts a poll at once: the host deploys the
  newest build of its `follow=` ref again.

A hold lives in `/var/lib/piceli-host/state.json`: it survives reboots and
switches. While it holds, polls deploy nothing (`poll.state` is `held`).
`host status` shows it (`hold`: `pin` or `rollback`, the tag, commit and
`at`), and `host wait` for another build exits 1 at once with
`host-pinned`. Each command reports `piceli.host.pin`, `piceli.host.unpin`
or `piceli.host.rollback` (the request and its outcome) with the deploy's
usual events.

The JSON of the three commands (`piceli.host-control.v1`): `action`,
`state` (`pinned`, `unpinned`, `rolled-back`; `failed` or `rejected` with a
`reason`), `hold`, `current`, `attempt` (its `trigger`, stages, checks and
rollback; `null` when nothing switched), `host` and `target`.

## Old systems

Every switch adds a generation to the system profile, and each one keeps
its whole closure in the store: a 20 GB disk fills with old systems. After
a deploy (a poll, a pin or a rollback) whose checks passed, the agent:

1. deletes the system profile's generations beyond `generations=` (5) with
   `nix-env -p /nix/var/nix/profiles/system --delete-generations +5`. It
   never deletes the current system, the previous one (what `piceli host
   rollback` goes back to) or a held one: when one of them is older than
   the newest five, the agent names the generations it deletes instead of
   `+5` and keeps the newest generation of that system;
2. collects the store (`nix-store --gc`; never `-d`, which deletes every
   old generation), so the space is freed.

Each step has a time limit (10 minutes, one hour for the collection). A
failure or a timeout is reported, `host-prune-failed`, and never fails the
deploy: the next one tries again. A deploy that failed or rolled back
prunes nothing.

The rendered module sets the boot menu's limit to the same number with
`lib.mkDefault` (`boot.loader.grub.configurationLimit` and
`boot.loader.systemd-boot.configurationLimit`), so your configuration can
still set another. The agent's attempt history (`history=`, 20) is separate:
it is a list in the status file, not systems on disk.

`prune` in the status and in each attempt:

```json
{"state": "done", "reason": null, "keep": 5, "current": 7,
 "deleted": [1, 2], "kept": [3, 4, 5, 6, 7], "protected": [],
 "gc": {"state": "done", "reason": null, "paths": 12, "freed": "345.60 MiB",
        "seconds": 41.2},
 "seconds": 41.9}
```

`state` is `done`, `failed` (with `reason: host-prune-failed`; `deleted`
is empty when the deletion failed, `gc.state` says whether the collection
did) or `skipped` (the profile is not a generation link). `protected`
lists generations kept beyond `keep` because they hold the previous or a
held system. `gc.paths` and `gc.freed` come from `nix-store`'s summary
(`null` when it printed none). The OTLP event is `piceli.host.prune`, with
the state, the number of generations deleted, which ones, and what the
collection freed.

## Status and wait

The agent writes `/var/lib/piceli-host/status.json` (`0644`, no secret,
`schema: piceli.nix-host-status.v1`). It holds `current` and `previous`
(store path, commit, tag, ref, `published_at`, `since`, and `pinned`: the
promotion gate's pin of that build from its signed manifest, `input`,
`commit`, `environment`, `verified_run`, `verified_at`, or `null` for a
build published without a gate; a rollback restores the previous build's), `poll` (the last
poll's state and reason), `last_attempt` (its `pinned`, its stages with `seconds`, the
check results, the `rollback`, the `prune`), `history` (the newest
attempts), `prune` (the last one), the ignored tags, the `hold` (or `null`)
and the report's state.

```sh
piceli host status hosts.py:web --infra infra.py:edge --json
piceli host wait web-1 v1.4.0 --infra hosts.py --timeout 1800 --json
piceli host status root@web-1.example.net
```

Both commands read the file over SSH (`BatchMode`, strict host key
checking: the key must be in `~/.ssh/known_hosts`, or pinned by `piceli
infra register` in the infrastructure's state; a server without
`cluster=` is registered for its host keys only). Give a `NixHost`
(`MODULE:ATTR`), the name of one that the `--infra MODULE` declares, or an
SSH target. `--infra` (a module, or `MODULE:ATTR` of its `Infrastructure`)
also gives the server's applied address when its `Ssh` has none, and the
pinned host keys. Every `piceli host` command takes the same arguments. `wait` takes a commit (7+ hex digits) or a tag. It exits
0 once the host runs that build with its checks passed. It exits 1 when the
attempt was refused or failed (`host-deploy-failed`), rolled back
(`host-rolled-back`), or after `--timeout` (`host-wait-timeout`). Its JSON
carries the attempt with its stage times.

Only the newest publication of the rev counts. Publishing the same commit
again retries it, so an earlier failure stays pending once the host has
seen a newer pointer (in its last poll or an attempt in progress), until
the agent records an attempt of that publication. Pass the pointer's
`published_at` from `dev nix build --publish --json` as
`--published-after` to wait for that publication before the host has even
polled:

```sh
piceli host wait hosts.py:web 3f2c1ab --published-after 2026-10-22T10:00:00.000000Z
```

## Every target in one view

`piceli targets` lists every GitOps environment and every NixHost of a
composition in one table (stderr), or one `piceli.targets.v1` object with
`--json`:

```sh
piceli targets --composition infra.py --infra infra.py --json
```

```text
KIND         NAME   WHERE        REVISION                 PINNED           CHECKS      STATE                                    AS OF
environment  main   shop         3f9c2d1e8a7b             product@3f9c2d1  passed 3/3  deployed                                 2026-10-23T09:30:00Z
environment  rc     shop-rc      9e1d0c4b2a33             product@9e1d0c4  failed 2/3  rolled-back (checks-failed-rolled-back)  2026-10-23T09:30:00Z
nix-host     db-1   203.0.113.8  -                        -                none        unreachable (host-unreachable)           -
nix-host     web-1  203.0.113.7  web-v1.2.0 d41c8e7f0a12  product@3f9c2d1  passed 1/1  deployed                                 2026-10-23T09:29:00Z
```

Each row has `kind` (`environment` or `nix-host`), `name`, `where`
(cluster and namespace, or the host's address), the deployed `revision`
(a commit and, for a host, its tag), `pinned` (the product commit it
runs), `checks` (`passed`, `failed`, `running` or `none`, with `at`),
`state` (`deployed`, `deploying`, `waiting`, `held`, `stopped`, `failed`,
`rolled-back`, `unreachable`, `unknown`) with its `reason`, `as_of`, and
`facts`: which source told each fact, `controller` or `host-agent`.

- An environment's `pinned` is its product source's deployed commit: the
  source a promotion gate verified in it, else its only source. Without
  either, `pinned.source` is `null` and `pinned.commits` lists every
  source's commit.
- A NixHost's `pinned` is the host agent's own `current.pinned` (the pin
  in the manifest of the build it runs; `facts.pinned` is `host-agent`).
  From an agent before that field, or when the host is not read, it is
  the `pinned` (`input`, `commit`, `environment`, `verified_run`,
  `verified_at`) of its release in the controller's status, when the host
  runs that release's latest commit; `null` without a gate.

The controller is read once, as `gitops status` reads it: from the
composition's home `Cluster` with its `piceli login` profile, as `gitops
enable` finds it (`--kubeconfig --context` or `--state-dir` win). Without
that profile the environments show `unreachable` (`profile-not-found`,
with the `piceli login` command to run); the hosts declared by `--composition` (its
`NixRelease`s) and `--infra` (its `NixHost`s, with their servers' applied
addresses and pinned host keys) over SSH, in parallel, each within
`--timeout` seconds (default 20). An unreachable source never fails the
view: its rows say `unreachable` with the reason, so host rows still work
with the home cluster down. Exit 0 when every target is reachable and none
failed; 1 when a target failed, rolled back or is unreachable, or its
checks failed; 2 when nothing is named (`targets-input-required`). The UI
shows the same document in its Targets view.

## Reports

With `report=`, the agent sends OTLP/HTTP JSON logs to `ENDPOINT/v1/logs`
(`service.name` `piceli-host-agent`, `host.name` the host's name): the
events `piceli.host.deploy.start`, `piceli.host.stage`,
`piceli.host.check`, `piceli.host.deploy.result`, `piceli.host.rollback`,
`piceli.host.rejected`, `piceli.host.pin` and `piceli.host.unpin`. They
carry the commit, the state, the reason, the seconds and the deploy's
`piceli.trigger` (`poll`, `pin` or `rollback`); one deploy or command
shares one trace id. A collector that is down never
blocks or fails a deploy. The agent makes one 2-second try per run, keeps up
to 500 records in `otlp-queue.json` and sends them at the next run.

## Codes

| Code | Meaning |
| --- | --- |
| `host-invalid`, `host-check-invalid` | The declaration was refused |
| `host-not-found`, `host-address-unknown`, `host-unreachable`, `host-status-missing` | `piceli host` cannot load the host or read its status |
| `host-wait-timeout`, `host-deploy-failed`, `host-rolled-back` | Outcomes of `piceli host wait` (exit 1) |
| `host-signature-invalid`, `host-pointer-replayed`, `host-manifest-mismatch`, `host-narinfo-mismatch`, `host-system-mismatch` | The agent refused a build; nothing was realised |
| `host-cache-unreachable`, `host-realise-failed`, `host-credentials-missing`, `host-credentials-exposed` | The agent could not read or fetch; it tries again at the next poll |
| `host-switch-failed`, `host-check-failed`, `host-rollback-failed`, `host-build-bad`, `host-report-failed` | A switch or check failed (rolled back), or the report was not delivered |
| `host-pinned`, `host-pin-not-found`, `host-rollback-unavailable`, `host-busy`, `host-control-unsupported` | `pin`, `unpin`, `rollback`, and `wait` on a held host |
| `host-secret-undeclared`, `host-secret-exists`, `host-secret-plan-changed`, `host-secret-write-failed`, `host-secret-activation-failed` | `piceli host secrets` |

`piceli explain CODE` prints the cause and the fix of each.
