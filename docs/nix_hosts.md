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

## Declare the host

```python
from piceli.infra import Branch, HostCheck, HostReport, NixCache, NixHost, Tag

cache = NixCache(
    "my-cache-1:<base64 public key>",
    url="s3://nix-cache?endpoint=s3.example.net&region=us-east-1",
    write_credentials="cache-s3-write",  # the builder only
    read_credentials="cache-s3-read",  # what hosts read with
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
                      headers_file="/etc/piceli-host/otlp-headers"),
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
| `read_credentials_file=` | The host's file with the read-only S3 keys (`/etc/piceli-host/cache.env` when the cache has `read_credentials`) |
| `poll_seconds=`, `history=` | How often the agent polls (300) and how many attempts the status keeps (20) |

`Tag("v*")` follows the highest semantic version among the matching tags (an
optional `v`, `MAJOR.MINOR.PATCH`, `-prerelease`, `+build`). A matching tag
that is not a version is ignored, and the status lists it under
`ignored_tags`. Pre-releases count only with `prereleases=True`. A
declaration that does not hold is refused with `host-invalid` or
`host-check-invalid`.

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

The module holds no secret. On the host, as root, put the cache's
**read-only** keys (never the write ones) in a `0600` file. The agent
refuses a file others can read (`host-credentials-exposed`).

```sh
install -d -m 700 /etc/piceli-host
install -m 600 /dev/stdin /etc/piceli-host/cache.env <<'EOF'
AWS_ACCESS_KEY_ID=<read-only key id>
AWS_SECRET_ACCESS_KEY=<read-only secret>
EOF
```

A public-read cache needs no file. The report's headers file has
`Name: value` lines (`Authorization: Bearer ...`), with the same mode. Your
secret tooling (agenix, sops-nix) can write both files.

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

## Status and wait

The agent writes `/var/lib/piceli-host/status.json` (`0644`, no secret,
`schema: piceli.nix-host-status.v1`). It holds `current` and `previous`
(store path, commit, tag, ref, `published_at`, `since`), `poll` (the last
poll's state and reason), `last_attempt` (its stages with `seconds`, the
check results, the `rollback`), `history` (the newest attempts), the
ignored tags and the report's state.

```sh
piceli host status hosts.py:web --infra infra.py:edge --json
piceli host wait hosts.py:web v1.4.0 --timeout 1800 --json
piceli host status root@web-1.example.net
```

Both commands read the file over SSH (`BatchMode`, strict host key
checking: the key must be in `~/.ssh/known_hosts`, or pinned by `piceli
infra register` in the infrastructure's state). Give a `NixHost` or an
SSH target. A `Server` without `Ssh(address=)` needs `--infra` for its
applied address. `wait` takes a commit (7+ hex digits) or a tag. It exits
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

## Reports

With `report=`, the agent sends OTLP/HTTP JSON logs to `ENDPOINT/v1/logs`
(`service.name` `piceli-host-agent`, `host.name` the host's name): the
events `piceli.host.deploy.start`, `piceli.host.stage`,
`piceli.host.check`, `piceli.host.deploy.result`, `piceli.host.rollback`
and `piceli.host.rejected`. They carry the commit, the state, the reason and
the seconds; one deploy shares one trace id. A collector that is down never
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

`piceli explain CODE` prints the cause and the fix of each.
