# Compositions and component contracts

Maturity: **preview** (the API may change before 1.0).

A *composition* deploys components that live in several Git repositories.
Each repository says how its components are built and run, in a
`piceli.toml` at its root (the **component contract**); a separate module,
usually `infra.py` in its own repository, says *where* they run and *when*
they deploy. A push to a component repository deploys by the composition's
rules, and only the components whose files changed are rebuilt and rolled.

```text
shop repo                 catalog repo              infra repo
├── piceli.toml           ├── piceli.toml           └── infra.py
├── web/ …                └── catalog/ …                 Source, Component, Stack,
└── api/ …                                               Environment, Cluster
```

The example in `examples/composition/` is this layout: `shop/` and
`catalog/` are the contents of the two component repositories, `infra.py` the
composition. The kind test `tests/integration/test_composition_kind.py` runs
it end to end.

## The composition module

```python
from piceli.envs import Branches, Environment, Stack, Tag
from piceli.infra import Cluster, Component, Source
from piceli.pipeline.model import Registry

cluster = Cluster(
    "my-cluster",
    api="https://10.0.0.10:6443",
    credentials="my-cluster",
    registry=Registry.in_cluster(on="node-a", repository="shop"),
)
shop = Source("https://git.example.com/team/shop.git")  # name "shop"
catalog = Source("git@git.example.com:team/catalog.git")

web = Component("web", source=shop, settings={"greeting": "hi"})
api = Component("api", source=shop)
items = Component("catalog", source=catalog)
cache = Component.image(
    "docker.io/library/redis:7.2",
    pin="sha256:<64 hex>",
    name="cache",
    contract={"ports": {"redis": 6379}, "health": {"ready": "TCP redis"}},
)

full = Stack("full", [web, api, items, cache])
small = Stack("small", [web, items])

environments = [
    Environment(
        "main",
        namespace="shop-main",
        stack=full,
        cluster=cluster,
        follow={shop: "main", catalog: "main"},
        auto_approve=True,
    ),
    Environment(
        "rc",
        namespace="shop-rc",
        stack=full,
        cluster=cluster,
        follow={shop: Tag("v*-rc*"), catalog: Tag("v*")},
    ),
    Environment.per_branch(
        Branches("wp-*"),
        namespace="shop-{branch}",
        stack=small,
        cluster=cluster,
        follow={shop: "{branch}", catalog: "main"},
        on_nodes=["node-b"],
        limit=2,
        idle_stop="24h",
        auto_approve=True,
    ),
]
```

- `Source(url, name=None)`: a Git repository; its name defaults to the
  repository name of the URL. Never put credentials in the URL: every source
  is fetched with **one** Git Secret (`--credentials-secret` of `gitops
  enable`, and the build Job's `piceli-build-git`).
- `Component(name, source=…, settings={…})`: the contract is the
  `[component.<name>]` table of the source's `piceli.toml` **at the commit
  the environment deploys**, so a contract change ships with the code.
  `settings` override the contract's.
- `Component.image(ref, pin="sha256:…", name=…, contract={…})`: a
  third-party image. The controller copies `ref@pin` into the in-cluster
  registry when it syncs; nodes pull it from there, never from a hosted
  registry at run time. `contract` takes the keys below except `build`
  (and `image.base`/`image.dirs`).
- `Stack(name, [components])`: the components an environment runs (an
  environment without a stack runs every component). `stack.components`
  lists them.
- `Environment(name, namespace=…, follow={Source: rule}, stack=…,
  cluster=…, on_nodes=…, quota=…, auto_approve=…, secrets=[…],
  settings={component: {key: value}})`: a named environment. A rule is a
  branch name (`"main"`, or `Branch("main")`: every push), `Tag("v*")` (the
  latest matching tag; a new one deploys), `Promote()` (`piceli promote ENV
  BRANCH@SHA`), or a list of them. The environment follows *every* source
  its components come from (`composition-invalid` otherwise).
- `Environment.per_branch(Branches("wp-*", fallback="main"),
  namespace="<prefix>{branch}", follow={…}, …)`: one environment per branch
  of the sources that follow `"{branch}"`; a source that follows
  `"{branch}"` but lacks the branch deploys `fallback`. It takes
  `stack`, `on_nodes`, `quota`, `limit` (at most this many run),
  `idle_stop`, `claim_sizes`, `allow_egress`, `auto_approve`, `secrets` and
  `settings` (the `EnvConfig` fields of the same meaning). The environment
  is removed with its branch.
- Optional `name = "shop"`: the app name of everything deployed (default:
  the module file's stem).

A single-source `Environment(follow=Branch("main"))` in an `EnvConfig` keeps
its 0.14 meaning; the new fields change no existing plan hash.

## The `piceli.toml` format

```toml
[component.api]
build = { rust = "crates/api", bin = "api", page_size = "from node" }
image = { base = "docker.io/library/debian@sha256:<64 hex>", user = 10001, dirs = { "/var/lib/api" = "0700" } }
ports = { http = 18080, graph-tls = 18443 }
health = { ready = "GET /health", check = ["api", "check-config"] }
upgrade_check = ["api", "store", "verify", "--read-only", "/var/lib/api"]
volumes = { data = { path = "/var/lib/api", size = "8Gi", retained = true } }
needs = ["cache?", "secret:api-token", "component:db"]
settings = { memory_soft_limit = "auto", retention = "default" }
emits = ["otlp:metrics", "otlp:logs"]
```

Only `[component.<name>]` tables are allowed, and only these keys: any other
key, a bad value or an unpinned base is refused with
`component-contract-invalid`. A component the environment runs that has no
table at the deployed commit is `component-contract-missing`.

| Key | Meaning |
| --- | --- |
| `build` | Exactly one recipe (below). Required, except for `Component.image`. |
| `image` | `base`: the runtime base, **pinned by digest** (`image@sha256:…`; required for `rust` and `python`, a `files` build without it is `scratch`); `user`: a uid or `"uid:gid"` (default `65532`); `dirs`: directories owned by the user, `{path = "0700"}`; `cmd`: the command (`Component.image`: the container command); `workdir`. |
| `ports` | `{name = number}`: named container ports and a Service named after the component with the same ports (names: 1-15 lowercase characters). |
| `health.ready` | `"GET /path"` (an HTTP probe on the `http` port, else the first), `"TCP <port name>"`, or a command: the readiness probe. |
| `health.check` | A command run in the **new** image with the workload's settings before it rolls out (a pre-rollout check, see {doc}`pre_rollout_checks`). |
| `upgrade_check` | A command run in the new image with the retained volumes mounted read-only before it rolls out (the pre-rollout upgrade check). Needs a retained volume. |
| `volumes` | `{name = {path, size, retained}}`. `retained = true` (a size is required): a claim per pod that is never pruned and outlives the workload (the component becomes a StatefulSet with a `ClaimTemplate`). Otherwise RAM-backed scratch (`emptyDir`, `size` its limit). |
| `needs` | `"NAME"` or `"component:NAME"`: the environment runs that component (also a start-order dependency); `"secret:NAME"`: the environment lists the Secret in `secrets=` (it is mounted at `/run/secrets/NAME`). A trailing `?` makes a need optional. An unmet need fails the environment's plan with `component-need-unmet`. |
| `settings` | `{key = "value"}`: environment variables (`memory_soft_limit` → `MEMORY_SOFT_LIMIT`). The composition overrides them: `Component(settings=…)`, then `Environment(settings={component: {…}})`. |
| `emits` | Informational (`["otlp:metrics"]`): what the component sends; Piceli validates it and does nothing else with it yet. |

### Build recipes

Builds are host builds ({doc}`host_builds`): no container VM, a staged
context of exactly the files the build reads, the image assembled as OCI
layers on the pinned base.

| Recipe | Keys | What runs |
| --- | --- | --- |
| `rust` | `rust` (the crate path), `bin` (default: the component name), `features`, `page_size = "from node"`, `paths` | `cargo zigbuild --release --locked` for the node's architecture; the binary at `/usr/local/bin/<bin>`, its entrypoint. Tools: `cargo`, `cargo-zigbuild`, `zig`. |
| `python` | `python` (the package directory), `module`, `paths` | The package at `/app/<dir>` (byte-compiled), `python3 -m <module>` as the entrypoint. |
| `files` | `files = {"src" = "/dest"}`, `paths` | The files and directories at their image paths, nothing compiled. |
| `dockerfile` | `dockerfile`, `context`, `target`, `paths` | Accepted in the contract; it needs a Docker engine, so the controller and its build Job refuse it (`component-build-unsupported`). |

`paths` lists what the build reads, relative to the repository root. The
default: `rust` the crate, `Cargo.toml` and `Cargo.lock`; `python` the
package directory; `files` the listed sources; `dockerfile` the context. List
the other crates of a workspace a binary depends on.

## Change-aware builds

Each component has a **source digest**: a hash of its `build` and `image`
tables and of the Git object id of each of its `paths` at the deployed
commit. Two commits that leave those untouched give the same digest, so:

- the controller builds a component only when no image exists for its
  digest (images are kept per digest on the controller's volume), and
  mirrors a `Component.image` once per `ref@pin`;
- the environment is rendered with every component's image; a component
  whose image did not change renders the same objects and applies as a
  no-op, so only changed components roll.

`settings`, `ports` or `health` changes need no rebuild: they change only
the rendered objects. `piceli gitops sync ENV --component NAME` rebuilds one
component even when its digest has an image.

## Deploying a composition

```text
piceli login my-cluster --kubeconfig ~/my-cluster.yaml     # once per machine
piceli registry install …                                   # the in-cluster registry
piceli gitops enable infra.py --image REPO@sha256:… \
    --credentials-secret git-token --builder-image REPO@sha256:…   # plan, then --approve HASH
piceli gitops status
piceli gitops sync main --component web
piceli promote rc main@abc1234                              # when rc follows Promote()
```

`gitops enable infra.py` imports the module on your machine and puts its
plain-data form (sources, components, environments, the registry; never
credentials) into the controller's config, so the install plan hash covers
every rule; the controller never imports your code. Change the module and
run `gitops enable` again. Without `--kubeconfig` it uses the cluster's
`credentials` profile.

The controller (see {doc}`gitops`) polls every source with `git ls-remote`,
resolves each environment's `follow` to one commit per source (its
**revision**), and deploys an environment when a followed branch moved, a
new matching tag appeared (tags present at the first poll are the baseline),
on `promote` or on `sync`. It reads the contracts at the revision, checks
the needs, builds only changed components in **one build Job** on the
builder node (it fetches every source it needs at its commit, with the one
Git Secret, and pushes to the in-cluster registry), renders the environment
and applies it with the environment's approval (`auto_approve=True` and the
default approval policy: creates and updates; otherwise `piceli gitops
approve ENV HASH`). Without `--builder-image` the controller mirrors
third-party images but cannot build. `piceli gitops run --once --local-build
--kubeconfig F --context C --config config.json --state-dir DIR` runs one
poll on your machine instead, building with the local host tools and
pushing through a port-forward to the registry.

### Status

The status (`piceli gitops status --json`, ConfigMap `piceli-gitops-status`,
schema `piceli.gitops-status.v1`) gains, for a composition controller:

- `sources.<name>`: `url`, `refs` (`{"refs/heads/main": sha,
  "refs/tags/v1.0.0": sha}`, the followed ones) and `last_poll`
  (`error`, a code, when the last `ls-remote` failed);
- `envs.<env>.revision`: `{source: sha}`, `refs`: `{source: ref}`, and
  `last_sync`: when it last finished deploying;
- `envs.<env>.components.<name>`: `source`, `commit`, `digest` (the image's
  manifest digest), `source_digest` (the build cache key), `image`, `state`
  (`building`, `rolling`, `synced`, `unchanged`, `failed`), `health` and
  `updated_at`;
- `controller.composition` (its name) and `controller.environments`.

The other keys are those of a single-repository controller; an environment
is keyed by its name (a branch environment by its branch).

## Not yet

- `Stack(extra=…)`, component variants and replica pairs are not modelled:
  declare a second component in `piceli.toml` instead.
- Branch environments of a composition are not seeded from restore points.
- A `dockerfile` build needs a Docker engine (`component-build-unsupported`).
- The deployed image is the first build platform's (`--platform`); nodes of
  another architecture need their own environment.
