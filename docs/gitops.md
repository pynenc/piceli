# GitOps: Piceli's controller and manifest export

Piceli runs the GitOps loop itself. `piceli gitops enable` installs a
controller in the cluster that polls a Git repository and keeps
[one environment per branch](#gitops-controller) at the branch's head,
deployed with the same plans, approvals, builds and checks as `piceli deploy`;
`piceli env`, `piceli envs` and `piceli promote` operate on those
environments (see {doc}`environments`), and the {doc}`web UI <ui>` shows the
controller and its pending approvals.

For clusters that apply manifests some other way, Piceli also exports what it
models: `piceli publish` pushes the rendered manifests as an OCI artifact and
`piceli render --out DIR` writes them as files to commit to Git. This page
says exactly what the plan, approval and journal no longer cover once
something other than Piceli applies the export.

```{admonition} Maturity: preview
:class: note

`piceli publish` and `render --out` are **preview**: the artifact layout is
the one `flux push artifact` produces and a kind test has Flux reconcile a
published artifact, but options and JSON fields may still change in a minor
release. Signing (cosign) is not built in yet (see [Signing](#gitops-signing)).
The controller is experimental (see its section).
```

(gitops-controller)=
## Piceli's own GitOps controller

```{admonition} Maturity: experimental
:class: warning

`piceli gitops` and `piceli promote` are new in 0.13.0; options, the status
document and the RBAC may change in a minor release.
```

A cluster runs Piceli itself as the controller: it polls a Git repository and
keeps **one environment per branch** at the branch's head, reusing
`piceli deploy`'s plans, approvals, builds and checks. No webhook, no laptop
step.

```sh
piceli gitops enable deploy/app.py:pipeline \
  --repo https://git.example.com/org/shop.git --branches 'main,wp-*' \
  --poll 60s --credentials-secret shop-git \
  --image registry.example.com/piceli@sha256:<digest> \
  --builder-image registry.example.com/piceli-builder@sha256:<digest> \
  --kubeconfig cluster.kubeconfig --context my-cluster
# prints the install plan and its hash, exit 3; then the same command with
#   --approve sha256:<plan hash>
```

What the controller does on every poll (`--poll`, default 60s):

| Event | What happens |
| --- | --- |
| A branch matching `--branches` (not main) has a new head | Build it in the cluster (a Job on the builder node, cache per branch) or use the images pushed for that exact commit with `piceli env push`, then deploy it to the branch's environment (its own namespace). Every push redeploys. |
| An untagged push to main | Nothing. |
| A new tag matching `--tags` (default `v*`) | Main is planned at the tag's commit. Tags that exist when the controller starts are the baseline and deploy nothing. |
| `piceli promote BRANCH@SHA` | Main is planned at that commit (only a commit the controller saw on that branch). |
| A branch is deleted (or stops matching) | Its environment is torn down (namespace and claims) with the controller's local state of it (pipeline state, build receipts); main's environment never is. |
| A push changes, adds or removes a check but no manifest | Nothing is applied, backed up or rolled out: the checks run against the running release (a **verification**, `last_action: verified`). A failing check never rolls such a release back: the environment stays `deployed` with `health: degraded` until a passing verification (a later push or `piceli gitops sync`) clears it. |

Approval stays the owner's:

- A **branch** deploys by itself only when the pipeline declares an
  `auto_approve=ApprovalPolicy(...)` and the plan is inside it; otherwise the
  branch waits in `approval-required` with the plan hash.
- **Main** always waits for a hash approval, unless the owner enabled the
  controller with `--main-auto-approve` (then main too applies plans inside
  the policy).
- `piceli gitops approve BRANCH sha256:<hash>` releases the waiting plan; the
  controller applies it on its next poll only if the plan is still the same
  (a new push or a changed plan makes the approval stale:
  `gitops-approval-stale`). When the approved plan changes under the
  controller (an apply that stopped half way), it plans again: the policy
  applies the new plan when it covers it, otherwise the branch waits in
  `approval-required` with the new hash.
- An approval is not asked for twice: when a plan's hash is exactly the
  owner-approved plan hash of the release running now (a reverted change
  plans it again), the controller applies it with that hash. Only the
  running release's plan counts: a hash the owner never approved, an older
  approved plan once another plan runs, or any plan after a failed step, a
  degraded verification or an idle stop still waits for approval. The hash
  is `deployed_plan_hash` in the status (`null` when the running release was
  applied by the policy).

The controller works **one step at a time**: teardowns first, then deploys,
oldest push first. A failed step is retried with exponential backoff (30s,
60s, … capped at 1h) and, after five attempts, left `failed` until the next
push; a failing branch never blocks the others, and a teardown is retried
until it succeeds. `piceli gitops status [--json]` shows everything:

```sh
piceli gitops status --kubeconfig cluster.kubeconfig --context my-cluster
# gitops controller: healthy (last poll 2026-09-30T10:00:00Z)
#   repo https://git.example.com/org/shop.git branches main,wp-*
#   main: approval-required 3f2a91c07d4e; approve: piceli gitops approve main sha256:…
#   wp-login: deployed 8c1d2e3f4a5b in shop-wp-login
```

To wait for one commit to reach an environment (an agent's integration
round, a CI job), `piceli gitops wait` (0.17.0) blocks until it is
deployed, failed (with the stage and cause), waiting for an approval,
superseded by a newer revision, or `--timeout` seconds passed:

```sh
piceli gitops wait main 3f2a91c --kubeconfig cluster.kubeconfig --context my-cluster --json
# main: pending
# main: pending, stage build
# main: retrying, attempt 1, next retry 2026-09-30T10:04:30Z
# main: deployed
# main 3f2a91c: deployed
```

Each deploy records the digest of the check set it verified (its
`checks_hash`), so unchanged checks never run again on the next sync of an
unchanged release, and a changed check set always does. On start, the
controller also drops the local state of environments torn down before (by
hand, or by an older controller): the state of a namespace that no
environment names is removed only when the namespace no longer exists; a
live environment's state is never deleted.

`piceli gitops disable` plans (exit 3) and, with `--approve`, removes the
controller. It never deletes an environment or the namespace, and keeps the
state claim unless `--delete-state`.

### The controller image

The controller runs the Piceli CLI (`piceli gitops run`) and `git`; its build
Jobs run a builder image with Piceli and the host-build tools. Every release
publishes both, for linux/amd64 and linux/arm64, built from the released
wheel; the [release notes](https://github.com/pynenc/piceli/releases) list
their digests:

- `ghcr.io/pynenc/piceli-controller@sha256:…` for `--image` (Piceli with the
  `ui` extra, `git`, `openssh-client`; the UI installed in the cluster runs
  it too: when a composition's `Cluster` declares a `Ui`, `gitops enable`
  plans the UI's objects with the new image in the same plan, so one
  approval upgrades both, see {doc}`cluster_init`);
- `ghcr.io/pynenc/piceli-builder@sha256:…` for `--builder-image` (the
  controller plus `cargo` with the linux and wasm32 targets,
  `cargo-zigbuild`, `zig`, `uv`, and `gcc`, `make`, `cmake`, `pkg-config`,
  `curl`, `xz` for crates with C builds such as `jemalloc-sys` and
  `aws-lc-sys`).

Both packages are public: nodes pull them anonymously. Each release checks
that an anonymous pull works and warns, with the settings link, when a
package is private. When the project's Docker Hub
credentials are configured, the release also copies them, same digests, to
`docker.io/pynenc/piceli-controller` and `docker.io/pynenc/piceli-builder`.

`--image` must be pinned by digest; the controller never pulls a moving tag,
and the image's Piceli version should match your CLI's. To build them
yourself (another registry, more tools), use `images/Dockerfile` with the
wheel of your version:

```console
$ pip download --no-deps --dest ctx/dist "piceli==0.14.3"
$ docker buildx build -f images/Dockerfile --target controller \
    --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-controller:0.14.3 --push ctx
$ docker buildx build -f images/Dockerfile --target builder \
    --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-builder:0.14.3 --push ctx
```

The push prints each digest. Add whatever your pipeline module imports
besides Piceli with a `FROM …piceli-controller@sha256:…` image of your own
(and end it with `RUN python -B -m piceli.integrity write --out
/usr/local/share/piceli/files.sha256`, so the self-check below covers your
files too).

**Self-check (0.15.1).** The images ship the SHA-256 of every file of their
Python installation (`/usr/local/share/piceli/files.sha256`, written as the
last build step). The controller and UI Deployments set
`PICELI_SELF_CHECK` to it, and `piceli` verifies every file before it
imports anything else (under a second). A node whose disk or memory altered
the unpacked image (the container then fails at some import with an
unrelated error, such as `ValueError: code: co_varnames is too small` from
a damaged `.pyc`) gets one message instead, exit status 70:

```text
controller image files corrupted on this node; remove the image from the node's containerd and restart (1 file(s) differ from the image: …)
```

Remove the image from that node (`crictl rmi <image@digest>` on the node),
delete the pod so it pulls again, and check the node's disk and memory.
The containers keep their last log lines as their termination message
(`FallbackToLogsOnError`), so the status below shows why any crash
happened. An image older than the check (no manifest) starts as before.
Without `--builder-image`, a branch deploys only images pushed with `piceli
env push`.

### What gets installed

| Object | Purpose |
| --- | --- |
| Namespace `piceli-system` (`--namespace`) | The controller's home (never deleted by `disable`). |
| ServiceAccount, Role and RoleBinding `piceli-gitops` | In its namespace only: ConfigMaps (status, requests), Leases, build Jobs, their Pods and logs, cache claims, Events. |
| ClusterRole and ClusterRoleBinding `piceli-gitops` | Read nodes; create, read and delete namespaces (one per branch); read PersistentVolumes (a branch teardown lists the volumes bound to its claims; deleting them is the `--delete-volumes` opt-in below); get the EndpointSlice `default/kubernetes` (the API server's addresses, for `allow_api`); create RoleBindings that bind **only** `piceli-gitops-deployer` (the `bind` verb is limited to that name). No Secrets, nothing else cluster-wide. `--cluster-rbac` adds ClusterRoles/ClusterRoleBindings for apps that declare them; `piceli cluster init` keeps that rule. |
| ClusterRole `piceli-gitops-deployer` | The namespaced kinds an app deploys, the `scale` subresource of Deployments and StatefulSets (restore points) and reads (`get`, `list`, `watch`) of `metrics.k8s.io` pods and nodes, so a release can grant them to its own Role without an escalation refusal, and `get` on `services/proxy` and `pods/proxy` (HTTP and metric checks use a port forward of the API, `pods/portforward`, and this proxy as a fallback); used only through a RoleBinding in each environment's namespace, so the controller can change nothing outside them. |
| PersistentVolumeClaim `piceli-gitops-state` (`--storage`, `--storage-class`) | The Git mirror, the last-seen commits and tags, build receipts. |
| ConfigMap `piceli-gitops-config` | The settings (pipeline, repository, globs, poll, build options); never a credential. |
| Deployment `piceli-gitops` | One replica, `Recreate`, non-root, read-only root filesystem, no privilege escalation; the credentials Secret mounted read-only. Its pod template carries `piceli.io/config-hash`, so `gitops enable` with changed settings (a new `--builder-image`) restarts the controller. |

**A new state volume** (0.23.0). The release state of every environment,
generated secret values included, lives on the state volume. When it starts
empty (the controller moved to a node where the volume is new), the first
deploy adopts the generated Secrets the previous controller created, instead
of generating new values that the retained Secrets would refuse: only
Secrets with this app's owner annotation and field manager, values kept, the
adoption shown in the plan and covered by its hash. See "When the state
starts empty" in {doc}`secrets`.

**Git credentials** stay in a Secret you create in the controller's namespace
(`--credentials-secret`): `username` and `password` (a token) for HTTPS, or
`ssh-privatekey` and `known_hosts` for SSH. The controller hands them to Git
through a credential helper that reads the files, so they appear in no command
line, environment, log, status or error; a `--repo` URL with credentials in
it is refused (`gitops-repo-invalid`). The build Job reads its own Secret
(`--build-git-secret`, default `piceli-build-git`).

**When Git fails** (0.19.0). A failing `git ls-remote`, `clone` or `fetch`
keeps the last lines of Git's stderr with the credential values, URL user
info and token-like strings masked, so the cause is visible without the
secret:

- `piceli gitops status` prints `last error: gitops-git-failed: shop: git
  ls-remote failed (exit 128): remote: Invalid username or token. …` and one
  `source NAME:` line per failing source; the JSON has
  `controller.last_error_detail` and `sources.NAME.error_detail`;
- the controller logs it once when it starts failing (and `source NAME: git
  works again` when it recovers), and emits a `piceli.controller.git.failed`
  event and a `cicd.pipeline.run.errors` point with `error.type:
  gitops-git-failed`;
- `piceli gitops wait ENV COMMIT` for a commit the controller has not seen
  fails after two failed polls in a row, `stage: fetch`, `cause:
  gitops-git-failed`, with `controller_error` (any other outcome carries
  `controller_error` too, so a "deployed" older commit no longer hides it);
- for HTTPS sources on `github.com` the controller asks GitHub every 6 hours
  when the token expires and, from 7 days before, warns in
  `controller.warnings` (`gitops-token-expiring`, `source`, `expires_at`)
  and the log.

**A corrupted build cache** (0.19.0). A cluster build step that fails
with a loader or link error on a cached artifact (a proc-macro `.so` the
loader refuses, invalid crate metadata, a truncated object) has the blamed
crates' artifacts removed and runs once more; the step in the receipt
carries `repaired` (`kind`, `crates`). A failed step's Job log ends with
`[piceli] the first error:` and the first `error` line with the 19 after
it, so the failure's `log_tail` in `piceli gitops status` shows the cause,
not only the last lines. To empty the cache by hand:

```sh
piceli gitops cache clear                 # every cargo target on the build cache claim
piceli gitops cache clear --crate canvas     # only these crates' artifacts
```

The controller runs it between steps, as a short Job on the builder node
(the build Job's image and claim, no Secret), and records
`controller.cache_clear` (`at`, `crates`, `state`).

**Why a check failed** (0.19.0). A failed check's own reason (the string a
`Checks.python` entry returns, an HTTP status, an exit code) is kept in
`envs.ENV.checks.results[].detail` and `verification.failed[].detail`;
`piceli gitops status` prints `failing: dashboards: stale source for
k8s.cluster`, `piceli gitops wait` adds `failed_checks` (`name`, `code`,
`detail`) and a `failing checks:` line, the controller's log names it, and
the UI shows it under the check.

```{warning}
The controller imports the pipeline module of every branch it deploys:
whoever can push a branch matching `--branches` runs code with the
controller's rights. Keep the globs narrow and protect those branches as you
would protect a CI runner's secrets.
```

### Named environments and idle stop

When the pipeline declares `EnvConfig(environments=[...])` (see
{doc}`environments`), `gitops enable` reads them from the working tree
(`--root`, default `.`) into the controller's config, so the install plan
hash covers every environment's trigger; change them and run `gitops enable`
again. A pipeline file that is there and does not load is refused
(`gitops-pipeline-invalid`); without the file the controller runs as in
0.13. Per environment:

- `Branch("main")`: every push to `main` deploys it (no tag needed);
- `Tag("v*-rc*")`: the latest new matching tag deploys it;
- `Promote()`: `piceli promote rc main@<sha>` deploys that commit (an
  environment without `Promote()` drops the request with
  `gitops-promote-not-allowed`; `piceli promote BRANCH@SHA` still targets
  main).

Each waits for `piceli gitops approve NAME HASH` unless it declares
`auto_approve=True` and the plan is inside the pipeline's `auto_approve`
policy. A commit already built for one environment is not built again for
another. `EnvConfig(idle_stop="24h")` scales a branch environment without a
push for that long to zero (state `stopped`, reason `idle-stop`); the next
push starts it again. The status lists the rules under
`controller.environments` and `controller.idle_stop_seconds`.

### Compositions: many sources

`piceli gitops enable infra.py` (a module, no `:ATTR`) installs the same
controller for a composition (see {doc}`components`): it polls every
`Source` of the module with one Git Secret, resolves each environment's
`follow={source: rule}` to one commit per source, builds only the components
whose source digest changed (one build Job that fetches every source it
needs) and rolls only those; unchanged components apply as no-op.
`--branches` and `--tags` are not used: the module names its sources and
rules. When an environment deploys a pipeline (`Environment(pipeline=…)`,
see {doc}`compositions`), the controller also follows the composition's own
repository (`--repo`, default the origin remote of `--root`; branch
`--main-branch`) and imports the module at its commit; each output image is
rebuilt only when its change key changed. The status adds `sources`,
`envs.<env>.revision` and `envs.<env>.components` (see {doc}`components`).

`piceli gitops sync [ENV] [--component NAME]` asks the controller to deploy
an environment (or every one) now at its revision; with `--component` a
composition controller also rebuilds that component. The deploy still needs
the environment's usual approval.

A component's `commit` is the commit of the source whose change triggered
its build (the run's trigger first when several changed; an image whose
change key did not move keeps the source and commit it was built at), and
`sources` lists the commit of every source it reads. The controller's log
says `rolled web` only for components whose image digest or workload
changed, else `rolled nothing`. A failed attempt (code, time and the build
log's tail, at most 2000 characters) stays in `failed_attempts` until the
revision deploys; the run that then succeeds lists them in the history.

(gitops-stop-named)=
#### Stop a named environment

A composition's named environment can be stopped: its Deployments and
StatefulSets are scaled to zero (the namespace, volumes and every other
object stay), and the controller neither plans nor deploys it, whatever is
pushed, until it is started again.

```sh
piceli env stop rc --cluster infra.py:cluster    # a request; the next poll stops it
piceli env start rc --cluster infra.py:cluster   # scales back; deploys the moved revision
```

Or declare it in the composition, `Environment("rc", …, stopped=True)`:
pushed, the controller stops it on its next poll and starts it when the
declaration is removed. **The declaration wins**: `piceli env start` of a
declared stop is refused (`gitops-env-stop-declared`), by the CLI when the
module it reads says so and by the controller otherwise; a requested stop
lasts until `env start`, also after a declared stop is removed. The status
shows `state: stopped`, `reason` `declared` or `requested` and `stop`
(`by`, `via`, `at`; for a declared stop, `at` is when the controller first
saw the declaration, 0.16.0); a sync or promotion while stopped is not deployed (a
promotion is kept for the start). Starting scales the workloads back to the
replicas they had and, when the revision moved meanwhile, deploys it with
the environment's usual approval (an unchanged release the owner approved
is not asked for again). Branch environments keep their idle stop. Both
commands only write a request (`--state-dir DIR` for a local controller);
the UI's **Stop** and **Start** write the same ones.

### Every platform

Since 0.21.0 the controller builds every image for every platform it is
enabled with, and deploys one OCI index over them: each node pulls its own
architecture, so a Deployment can land on any node and a DaemonSet runs on
all of them. Without `--platform`, `piceli gitops enable` takes the
architectures of the cluster's declared nodes (`Node(arch=...)`; a cluster
of arm64 and amd64 nodes builds both). An image's change key covers its
platforms, so changing them rebuilds once.

A Rust build for the other architecture cross-compiles with `cargo zigbuild`
(no emulation; the build takes about as long as the native one plus the
link with zig's linker); a build spec whose commands cannot cross-compile
needs a build host of that architecture.

### Builds on build hosts

When the cluster's own builder node is down, or a laptop or a Mac mini
builds faster, the controller can hand its builds to build hosts:

```python
controller=Controller(on="node-a", builds=("hosts", "cluster"), host_wait="30m"),
dev=DevBuilds(
    node=None,
    image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
    hosts=[BuildHost.docker("laptop", memory="7Gi")],
),
```

- `builds=("hosts",)`: the controller never runs a build Job. It publishes
  what it waits for in its status (`controller.builds_wanted`: the spec or
  components, every source at its commit, the platforms, each image's
  repository and change key) and the environment waits (`retrying`, reason
  `build-waiting`, its components `waiting-build`; no attempt is counted
  and nothing goes to the history while it waits).
- `builds=("hosts", "cluster")`: the same, and after `host_wait` without a
  host claiming it, the in-cluster Job builds it.

On a build host, with the cluster's credential profile:

```sh
piceli builder serve --cluster infra.py:my_cluster            # until Ctrl-C
piceli builder serve --cluster infra.py:my_cluster --once     # one build
```

It takes the oldest wanted build nobody holds (request `build-claim`; a
claim not answered in 4 hours lapses), fetches each source at its commit
with **this machine's own Git access** (mirrors in
`~/.cache/piceli/builder/git`, fetched incrementally), checks each one out
with its `.git` (`~/.cache/piceli/builder/checkouts`, kept between builds),
builds it in a container of the builder image on the host's engine
(`piceli build job-run --archives-only`: the in-cluster build's code, every
platform, its cache in the `piceli-build-cache` volume), pushes the images to the cluster's
registry through a port-forward (only the blobs it lacks) and one OCI
index per image, and answers `built` (each image's key, pull reference,
digest and platforms, and a receipt: host, times, builder image, commits).
A failure answers `build-failed` with the output's tail: the environment's
step fails with it, as an in-cluster build would.

**Private sources.** Sources are fetched at the URLs the composition
declares, never with a prompt. An HTTPS URL is fetched with the host's Git
credentials (its credential helper); when that fails, the same repository is
fetched over SSH with the host's keys (`https://github.com/org/repo.git` as
`git@github.com:org/repo.git`, for this process only: the user's Git config
is not changed). SSH runs in batch mode unless `GIT_SSH_COMMAND` names
another command. A source neither reaches fails the build with both errors.

**Source identity.** A build spec with an inputs file verifies each source's
Git identity (its commit, a clean tree, its origin) inside the builder
container, as an in-cluster build does. The container runs as its own user
(65532) and reads the checkouts read-only, so it first copies them into its
cache volume (`/cache/builder-src`, owned by that user, file times kept,
removed when the build ends): Git verifies the copy without any exception
to its ownership check. Files a commit does not change keep their times in
the checkout and the copy, so incremental builds (cargo) rebuild only what
changed.

**The queue.** `builds_wanted` holds only what a step waits for now:

- an environment that asks for a newer build (a new commit) drops the build
  it asked for before; one whose step ends without waiting (deployed,
  failed, stopped) drops its builds; an entry no step asked for in three
  polls (at least 5 minutes) goes, which also clears what an older
  controller left behind;
- a build a host reported failed shows `failed` (host, reason, output tail)
  while listed, hosts never take it, and the controller does not offer the
  same inputs again: the environment fails at once with the host's tail
  until its inputs change or `piceli gitops sync` retries it;
- a host never builds again what it just answered (built or failed) while
  the controller still lists it.

The controller accepts a `built` request only for a build it asked for,
with every image built from the same inputs (its change key) and its digest
present in its registry; then it records the images as its own builds and
the environment deploys them through the same plan, approval and checks.
Anything else is a rejected request (`rejected_requests` in the status).

The builder image must be the controller's Piceli version. Its registry
must be on a node that works: `Registry.in_cluster(on=...)` places it, and
moving it means a new claim, which the next builds and mirrors fill again.

#### Delivery into the nodes, without a registry

When the cluster has no registry that works (or you prefer not to use
one), build hosts can import what they build straight into the nodes'
containerd over SSH, with `ctr images import`:

```python
cluster = Cluster(
    "my-cluster",
    api="https://10.0.0.1:6443",
    credentials="my-cluster",
    nodes=[
        Node("node-a", arch="amd64", roles=["controller"],
             target="ssh://root@10.0.0.1?runtime=k3s-containerd"),
        Node("node-b", arch="arm64",
             target="ssh://ops@10.0.0.2?runtime=containerd"),
    ],
    controller=Controller(on="node-a", builds=("hosts",), delivery="nodes"),
    dev=DevBuilds(node=None, image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
                  hosts=[BuildHost.docker("laptop")]),
)
```

- `Controller(delivery="nodes")` (the default is `"registry"`) needs
  `builds=("hosts",)`; with `("hosts", "cluster")` the in-cluster fallback
  still pushes to the registry, so a registry must be declared.
- Every declared node needs `target=`: a workload can land on any node, so
  every node receives every image. The target is the same as for a
  pipeline's `NodeImport`: `ssh://[user@]host[:port]?runtime=containerd` or
  `runtime=k3s-containerd` (`k3s ctr`), with `namespace=` (default
  `k8s.io`) and `sudo=` (default `true`: `sudo -n ctr`); `docker://name?runtime=containerd`
  for a kind node. The controller's configuration keeps the targets
  without their SSH user.
- The controller must build every node's platform; `piceli gitops enable`
  does by default (the architectures of the declared nodes). A node whose
  platform is not built fails the step (`composition-invalid`).

The wanted build then names each node and its platform
(`controller.builds_wanted[].deliver`: `{"mode": "nodes", "nodes":
{"node-a": "linux/amd64", "node-b": "linux/arm64"}}`). `piceli builder
serve` builds every platform as before and, instead of pushing, imports
into each node the archive of the node's architecture, over SSH with this
host's own access (its keys and, when `SSH_AUTH_SOCK` is set, its agent;
SSH runs in batch mode). No docker is needed on the build host for this
step and nothing is pushed anywhere. The import streams the archive
through a relay that checks the image's config digest before containerd
sees a complete image (a mismatch imports nothing), then asks the node
which config digest the name now points at.

Every node knows the image by the same name,
`node.piceli.invalid/<repository>:sha256-<hex>`: a content tag of the
image's config digests (the config digest itself for one platform, else
the SHA-256 of its `platform=config` lines), so the name can never point at
other content. The `.invalid` domain never resolves: a node that lacks the
image cannot pull one from elsewhere, so its pod fails to start and the
rollout's checks fail instead of running something else. Workloads keep
the default pull policy (`IfNotPresent` for this tag), which uses the
imported image.

The answer (`built`) carries, per image, its config digest per platform
and each node's import receipt (platform, result, the name and the config
digest the node reported). The controller accepts it only when every node
it asked for is there, each with its own platform's config digest under
that name; then it deploys through the same plan, approval and checks.
Otherwise the request is rejected (`rejected_requests`) and the build stays
wanted. The controller does not ask the nodes itself: it has no SSH access
or node credentials, and the API's `node.status.images` carries no config
digest, lags the kubelet's status updates and lists at most 50 images, so
the receipts of the import, which verified the config digest on the node,
are the evidence.

A node added later receives images at the next build of each component;
`piceli gitops sync ENV --component NAME` rebuilds one now. Third-party
images (`Component.image(...)`) are copied into a registry, so they still
need `Registry.in_cluster(...)`.

### Where each image was built (0.22.0)

Every image the controller records keeps its **provenance**: the machine
that built it (the build host's name, or the node its build Job ran on),
the builder image, the source commits, the toolchains when the build
reported them, and when the build started and ended. It is kept with the
image in the controller's cache, so a later deploy that reuses the image
keeps it too, and shows per component in `piceli gitops status --json` and
in every run of the history:

```json
"components": {"web": {"image": "registry…/shop/web@sha256:…", "digest": "sha256:…",
  "provenance": {"builder": "host", "host": "mini",
                 "builder_image": "ghcr.io/pynenc/piceli-builder@sha256:…",
                 "commits": {"shop": "3f2a91c0…"},
                 "started_at": "2026-10-18T09:00:00Z", "finished_at": "2026-10-18T09:30:00Z"}}}
```

`piceli builder serve` also pushes it next to each image it delivers, as an
in-toto Statement (SLSA provenance v1, subject the image digest) attached
as an OCI 1.1 referrer (`artifactType: application/vnd.in-toto+json`); a
registry that refuses referrers only loses the copy, never the delivery.

When a machine turns out faulty, list what it built and rebuild it:

```sh
piceli gitops suspects --host mini --since 2026-10-01 --json \
  --kubeconfig ~/.kube/lab --context lab
piceli gitops rebuild --host mini --since 2026-10-01 \
  --kubeconfig ~/.kube/lab --context lab
```

`suspects` lists every deployed image the host (or node) built, per
environment, cluster and component, from the status; `--since` keeps
builds that ended at or after that time. `rebuild` asks the controller to
drop those images' cached build results and deploy each environment that
runs one (trigger `sync rebuild mini`): the images are built again,
elsewhere when the host is quarantined, and deployed through the usual
plan, approval and checks. `controller.last_rebuild` in the status says
what it took; a host that built nothing deployed is a rejected request.

### Faulty build hosts and nodes: quarantine (0.22.0)

The controller records a health event for a build host when its
`build-failed` answer's output shows a machine-health sign (an internal
compiler error, a segfault, an illegal instruction, a bus error, a machine
check, an undefined symbol at link; see {doc}`dev_builds`), and for the
builder node when one of its build Jobs fails that way. After
`Controller(quarantine_after=2, quarantine_window="24h")` events (the
defaults; `None` turns it off) the host or node is quarantined:

- a quarantined host's `build-claim` is refused (`rejected_requests`);
  `piceli builder serve` on it reads that from the status, says why and
  claims nothing, as it does while its own record on the host (see
  {doc}`dev_builds`) quarantines it;
- with `builds=("hosts", "cluster")` a quarantined builder node is no
  fallback: the build keeps waiting for a host;
- with `builds=("cluster",)` the step fails with `build-host-quarantined`
  (retried with backoff) and nothing runs on the node.

`piceli gitops hosts` shows each one's events and state
(`controller.build_health` in the status); `piceli gitops release-host
NAME` ends a quarantine once the machine is sound. A build that passed but
met a corrupt cached blob (`cache-corrupt` in a host build's log: the blob
is discarded and fetched or written again) also counts against its host.

(gitops-nix-releases)=
### Nix host releases (0.24.0)

The controller can also publish the releases of a {doc}`NixHost <nix_hosts>`,
so a new tag needs no hand-run `piceli dev nix build --publish`. Declare a
`NixRelease` in the composition module, next to the host and the `Source`
that holds its flake:

```python
from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag

hosts = Source("https://git.example.org/org/hosts.git")
cache = NixCache("cache-1:<base64 public key>",
                 url="s3://nix-cache?endpoint=s3.example.org&region=us-east-1",
                 write_credentials="cache-s3-write")
web = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
              follow=Tag("web-v*"), cache=cache)
web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=False)
```

`piceli gitops enable infra.py` puts it into the controller's
configuration (the install plan hash covers it). A composition may hold
releases and no environment. A `NixRelease` follows `Tag(...)` only, and
its name (the host's) may not be an environment's: `approve` and `sync`
name either.

**The write keys stay on the build host.** The controller in the cluster
never holds them. On the build host named by `builder=` (a docker or
native host of the cluster's `DevBuilds(hosts=[...])` with
`DevProfile.for_nix()`), with the cache's signing key and S3 write keys in
its own Piceli secrets (`piceli secrets nix-cache cache-1 --prompt`,
`piceli secrets s3 cache-s3-write --prompt`):

```sh
piceli builder serve --cluster infra.py:my_cluster --host mini \
  --publish infra.py:cache
```

`--publish MODULE:ATTR` (repeat it for several caches) names the caches
this host publishes to. It reads every key when it starts and refuses to
start without one (`infra-credential-missing`) or with another cache's
(`dev-nix-key-mismatch`). It claims a publish only when it is the
release's builder and holds that cache's keys, and says once why it skips
any other. Its claim carries the cache key's signature over the claim
(`piceli.nix-publish-claim.v1`, the build's id and the host), which the
controller checks with the cache's public key: the key itself is never
sent. A native host with `--publish` only publishes; a docker host also
builds images as before.

What happens when a tag appears in the source:

1. The controller takes the highest version of the host's pattern (the
   version after its prefix, see {doc}`nix_hosts`). Tags that exist when it
   first sees the release are the **baseline** and publish nothing;
   `piceli gitops sync web-1` publishes the newest one. A newer tag starts
   a new attempt (an older or equal one never does).
2. It plans the publish, `piceli.nix-release-plan.v1`: the attribute, the
   build's name, the commit and the tag, the source, the builder, the
   cache, the manifest key and the tag's pointer. With
   `auto_approve=True` it goes on; otherwise the release waits in
   `approval-required` until `piceli gitops approve web-1
   sha256:<plan digest>`.
3. It asks for the publish in `controller.builds_wanted` (kind
   `nix-publish`, with the plan and its digest). The release is
   `waiting-build`, then `publishing` once its builder claimed it.
4. The build host fetches the source at the commit with its own Git
   access, checks that the tag points at that commit (a tag moved since
   fails the publish), builds the attribute as a development run (as
   `piceli dev nix build`), signs and pushes the closure, then uploads the
   signed manifest (its `commit` and `tag` are the tag's) and the tag's
   pointer, exactly as `dev nix build --publish` does. It refuses a plan
   whose pointers are not exactly its tag's. It answers `nix-published`
   with the signed manifest, the pointer and its stages.
5. The controller records the release `published` only when the manifest
   is signed by the cache's key and names the planned build, commit, tag
   and attribute, and the pointer is the tag's. Anything else is a
   rejected request (`rejected_requests`).

A failed fetch, build or publish is answered `build-failed`: the release
is `failed` with the host's reason and output tail
(`gitops-nix-release-failed`) and is not tried again until a newer tag or
`piceli gitops sync` (`sync web-1` retries that release, `sync` alone
every failed one). A quarantined build host claims nothing. The hosts
deploy the published build on their own, as {doc}`nix_hosts` describes.

`piceli gitops status --json` lists each release under `nix_releases`:

```json
"nix_releases": {"web-1": {
  "host": "web-1", "source": "hosts", "builder": "mini", "auto_approve": false,
  "follow": {"kind": "tag", "pattern": "web-v*", "prereleases": false},
  "latest_tag": "web-v1.4.0", "latest_commit": "3f2c…",
  "state": "published", "reason": null, "trigger": "tag web-v1.4.0",
  "tag": "web-v1.4.0", "commit": "3f2c…",
  "plan": {"schema": "piceli.nix-release-plan.v1", "…": "…"},
  "plan_digest": "sha256:…", "build_id": "0a1b…",
  "approval": {"via": "cli", "at": "2026-10-23T10:04:00Z", "requested_at": "…"},
  "requested_at": "2026-10-23T10:00:00Z", "started_at": "2026-10-23T10:05:00Z",
  "finished_at": "2026-10-23T10:20:00Z", "seconds": 1200.0,
  "stages": {"approval": {"started_at": "…", "finished_at": "…", "seconds": 240.0},
             "queue": {"seconds": 60.0}, "fetch": {"seconds": 4.1},
             "build": {"seconds": 590.0}, "publish": {"seconds": 300.0}},
  "published": {"host": "mini", "manifest": "piceli/web-1/3f2c….json",
                "manifest_sha256": "sha256:…", "store_path": "/nix/store/…",
                "system": "x86_64-linux", "nar_hash": "sha256:…",
                "pointers": [{"ref": "refs/tags/web-v1.4.0", "commit": "3f2c…",
                              "published_at": "…"}]},
  "published_tag": "web-v1.4.0", "published_commit": "3f2c…"}}
```

`state` is `idle` (no matching tag), `baseline`, `approval-required` (with
`approve_command`), `waiting-build`, `publishing` (with `claimed`),
`published` or `failed` (with `failure`: `host`, `reason`, `log_tail`),
or `rejected` by a promotion gate (below).
The history (`piceli-gitops-history`) keeps each attempt under
`nix_releases.<name>.runs` (newest first; the tag, commit, state, trigger,
approval, stages, build host and manifest digest), and the controller's
telemetry sends each as a trace `PUBLISH <name> <tag>` with a span per
stage and the event `piceli.nix.release.finished` (see
{doc}`opentelemetry`).

#### A promotion gate and `piceli promote` (0.24.0)

A host often runs a product from another repository, pinned as a flake
input (`inputs.app.url = "git+https://git.example.org/org/app.git"`). A
`Promotion` gate lets a release publish only a product commit one of the
composition's environments ran with its release checks passed:

```python
from piceli.infra import NixRelease, Promotion

app = Source("https://git.example.org/org/app.git")
environments = [Environment("main", follow={app: "main"}, namespace="shop-main",
                            stack=full, cluster=cluster)]
web_release = NixRelease(web, source=hosts, builder="mini",
                         gate=Promotion(input="app", environment="main"))
```

`input` is the input's name in `flake.lock`; `environment` an
`Environment` of the same composition that follows the input's source
(`source=`, default: the source named like the input). The composition
refuses a gate naming another environment or a source it does not follow.
The gate lives on the `NixRelease`, not on the `NixHost`: it is a rule of
who publishes, checked against an environment of the same composition, and
the host itself never reads the controller's history.

**The rule.** At plan time the gate reads `flake.lock` at the release
commit and takes the input's `locked.rev`. The commit qualifies when the
environment's history (`piceli-gitops-history`) has a run of it in state
`deployed` whose checks ran (at least one) and all passed, without a
rollback. A run that failed, degraded or rolled back does not count, nor an
environment without checks.

- **The controller** checks a new tag's commit before it plans the publish.
  Refused, the release is `rejected` with `reason`
  `promotion-not-verified` and `refusal` (the input, the commit, the
  environment and `newest_passed`, the newest commit it did pass); nothing
  is asked of a build host. It checks again on every poll, so the tag
  publishes once the environment passed that commit. A flake without the
  input is `promotion-input-missing`.
- **A hand-run publish** of a gated host runs the same gate, always:
  `piceli dev nix build ATTR --ref web-v1.4.0 --publish infra.py:cache`
  finds the gated `NixRelease` (or `NixHost` with `gate=`) of the same
  cache and attribute in the `--publish` module, or in `--composition
  MODULE` when given; `--release HOST` names it explicitly. It reads the
  history as `piceli gitops status` does (`--state-dir`,
  `--kubeconfig FILE --context NAME`, else the composition cluster's
  credential profile from `piceli login`, `--namespace`) and refuses before
  any build. A history it cannot read refuses too
  (`promotion-history-unreachable`): the gate fails closed.

The result goes into the release plan (so the approval covers it), into
the signed build manifest (an optional field: older manifests verify
unchanged) and into `piceli gitops status` under the release:

```json
"pinned": {"input": "app", "commit": "9b1e…", "environment": "main",
           "verified_run": "20261023T101500Z-3f2c9a10", "verified_at": "2026-10-23T10:20:00Z"}
```

`verified_run` is the newest qualifying run of that commit and
`verified_at` when it finished.

**Passed commits outlive the history.** The history lists the last 20 runs
of an environment, so the controller also keeps, in its state, a record of
the commits each gated environment passed: per environment and source,
each commit's first and last passed run and their times, for the last 200
commits per source (the oldest by last pass drop out). The gate checks the
record as well as the runs, so a verified commit keeps qualifying after
many deploys. The record is published in the history document as
`passed_commits` (only for gated environments), where a hand-run gate and
`piceli promote` read it:

```json
"passed_commits": {"main": {"app": {"9b1e…": {
  "first_run": "20261021T090000Z-1a2b3c4d", "first_at": "2026-10-21T09:05:00Z",
  "last_run": "20261023T101500Z-3f2c9a10", "last_at": "2026-10-23T10:20:00Z"}}}}
```

**The host checks it too.** `NixHost(..., gate=Promotion(input="app",
environment="main"))` puts the gate into the agent's configuration: the
agent switches only to a build whose signed manifest carries a `pinned` of
that input and environment, and refuses any other (`host-promotion-missing`,
nothing is realised). A `NixRelease` of that host takes the host's gate
(giving another one is refused), so the gate is declared once.

**`piceli promote HOST --from ENV`** writes the pin for you:

```sh
piceli promote web-1 --from main --composition infra.py --repo ../hosts
# plan, exit 3; then the same command with --approve sha256:<digest>
```

It takes the newest commit `ENV` passed, computes in the flake's working
copy the `flake.lock` that pins the input to exactly it, the next tag of
the host's pattern (a patch bump of the highest release; `--bump major|minor`)
and prints the plan with its digest (`piceli.promote.v1`):

```json
{"schema": "piceli.promote.v1", "state": "approval-required",
 "digest": "sha256:…", "approve_command": "piceli promote web-1 --from main … --approve sha256:…",
 "plan": {"schema": "piceli.promote-plan.v1", "host": "web-1",
          "pinned": {"input": "app", "commit": "9b1e…", "environment": "main",
                     "verified_run": "…", "verified_at": "…"},
          "change": {"file": "flake.lock", "input": "app", "from": "41d0…",
                     "to": "9b1e…", "method": "nix", "lock_sha256": "…"},
          "commit_message": "promote app to 9b1e… from main\n\n…",
          "base": "c0ff…", "remote": "origin", "branch": "main",
          "tag": "web-v1.4.1", "previous_tag": "web-v1.4.0", "bump": "patch"}}
```

`--approve` recomputes the plan (`promote-plan-changed` when anything
moved), commits `flake.lock` alone, creates an annotated tag and pushes the
branch and the tag with `git push --atomic` (`promote-push-failed`, exit
1, leaves both local). With `nix` on the `PATH` the lock comes from `nix
flake lock --override-input` in a temporary worktree; without it the input's
node is edited (`rev`, `lastModified`, `revCount`, and the `narHash`
computed from a temporary bare clone of the input; a `git`, `github` or
`gitlab` input without submodules or export rules). It refuses a working
copy with uncommitted changes to tracked files (`promote-dirty`) and a
branch without an upstream, or behind, ahead of or diverged from it
(`promote-not-in-sync`). When the lock already pins the commit the state is
`current` (exit 0): with a tag of the pattern at `HEAD` there is nothing to
do; without one the plan only tags `HEAD`, and `--approve` tags and pushes
the tag.

### Removed objects are deleted (pruning)

New in 0.14.7. Every sync, of a branch, a named environment or main, and of
both controllers (one repository, a composition), plans the deletion of the
objects an earlier release of the app created and the current render no
longer declares: a removed workload, its Service, its NetworkPolicy, a
Deployment that became a StatefulSet of the same name. They are `delete`
actions of the same plan, so the same plan hash covers them, and they follow
the apply's approval rules:

- a branch environment with `EnvConfig(auto_approve=True)`, or an
  environment whose plan is inside the pipeline's `auto_approve` policy,
  deletes them without asking: a policy covers a prune (the class `prune`:
  the delete of an object this release's owner wrote and no longer declares)
  unless it denies `"prune"` or `"delete"`; it counts towards `max_objects`,
  and a cluster-scoped one also needs `cluster_scoped`;
- any other environment waits for `piceli gitops approve ENV HASH`, and the
  plan it shows lists the deletes.

Only an object whose `piceli.io/owner` annotation is this release's owner
(or an inherited owner) in the environment's namespace is ever deleted: never
an object another owner or a person wrote, never one without that
annotation. The deletes run **after** every new object is applied and ready
and **before** the checks: a removed workload is deleted `Foreground` (its
pods first, so a Service of the same name and the checks see only the new
pods), and the next step starts once it is gone. A renamed workload therefore
switches in one sync: the new StatefulSet becomes ready behind the shared
Service, then the old Deployment and its pods go.

Data is never deleted automatically. Claims, Secrets, objects marked
`piceli.io/retained`, the claims a removed StatefulSet created from its
claim templates, and a StatefulSet whose retention policy would delete its
claims are **kept, orphaned**: the plan, the status and the deployment
history list them under `kept_orphaned`, each with `why` (`claim`, `secret`,
`retained`, `deletes-claims`) and the exact command that deletes it, for
when its data is no longer needed:

```json
{"kind": "PersistentVolumeClaim", "name": "data-db-0", "namespace": "shop-main",
 "why": "claim", "command": "kubectl --namespace shop-main delete persistentvolumeclaim data-db-0"}
```

A claim's volume stays after that while its reclaim policy is `Retain`.

When a deploy's checks fail and its release is rolled back
(`rollback_on_failed_checks`), the rollback restores the previous release
whole (0.16.0): what the failed release removed (by its prune, or by hand)
and the previous release declares is created again, and what only the
failed release declares is pruned, with the same data rules as above (see
{doc}`checks`). From 0.14.7 to 0.15.1 the rollback restored only the
objects both releases declare, which could leave a workload without an
object it needs, such as the Role a failed release had removed. The
controller then does **not** retry that revision: the environment is `failed` with reason
`checks-failed-rolled-back` and no `next_attempt_at`, until a new revision
or `piceli gitops sync ENV` (one try, no loop of restore point, apply and
rollback on every poll).
`Pipeline(prune=False)` turns pruning off; `piceli deploy` (outside an
environment) prunes only with `Pipeline(prune=True)`.

### Traces, events and metrics (OpenTelemetry)

From 0.16 the controller sends OpenTelemetry when the composition's
`Controller(telemetry=Otlp("https://collector:4317", headers_secret=...))`
says where (or the standard `OTEL_EXPORTER_OTLP_*` variables): one trace per
deploy with a span per stage (builds, plan, approval wait, restore point,
pre-rollout, apply, checks, rollback), events for approvals, rollouts,
failed checks, rollbacks, failed builds, stops and starts, and CI/CD
metrics with a heartbeat that ticks during long builds. It never blocks or
fails a deploy. See {doc}`opentelemetry` for every name.

### Status and requests (for tools)

The controller publishes its status in the ConfigMap `piceli-gitops-status`
(key `status.json`, schema `piceli.gitops-status.v1`) of its namespace;
`piceli gitops status --json` prints it with `health` (`healthy`,
`degraded`, `stale`, `starting`, `down`), `deployment_ready` and, from
0.15.1, `controller_live`: the controller's pod as Kubernetes sees it
(`state` `running`, `down`, `stale` or `starting`; `message`, such as
`CrashLoopBackOff, 37 restarts; last error: …` or the self-check's;
`pod` with its node, restarts, waiting reason and last exit;
`status_is_stale` and `note` when the document was written before the
controller stopped: the environments' states are then as of `last_poll`,
and the text output says so first). `piceli cluster status` adds the same
under `controller.live` and prints the message; the UI shows the
controller down with it. Without permission to read the controller's pods,
only the age of the last poll decides (`stale`). And
`piceli envs` reads it (`piceli.gitops.state.read_status`). Per branch,
`envs.<branch>` holds `commit` (wanted), `deployed_commit`, `state`
(`pending`, `retrying`, `approval-required`, `deployed`, `failed`,
`deleting`,
`stopped`), `plan_hash`, `deployed_plan_hash` (the owner-approved plan of
the running release), `reason` (an error code), `attempts`,
`next_attempt_at`, `pushed_at`, `updated_at`, `namespace` and `trigger`
(`push`, `tag v1.2.0`, `promote BRANCH@SHA`). After a deploy, `deleted`
lists the objects it pruned (`kind`, `name`) and `kept_orphaned` the ones it
kept with the command deleting each (see above); both are empty lists when
it pruned nothing. A deployed environment has
`health` (`healthy`, or `degraded` after a failing verification, with
`reason: pipeline-checks-failed`) and `last_action`: `deployed` (something
was applied), `verified` (nothing was applied; a changed check set ran
against the running release) or `unchanged`. A composition controller adds
`checks` (0.14.7): the last checks a run executed, after a rollout too:

```json
{"state": "passed", "passed": 6, "total": 6, "failed": [],
 "results": [{"name": "web-index", "passed": true}, {"name": "store-ready", "passed": true}],
 "at": "2026-10-03T10:00:00Z", "trigger": "push web/main",
 "run_id": "20261003T095800Z-…", "action": "deployed"}
```

(`failed` names the failing checks, each result has its `code` when it
failed; `rollback` is there when the failing checks rolled the release back;
a run whose checks were skipped keeps the previous `checks`. Since 0.15.0
an `unchanged` deploy after a rolled-back release, a revert for example,
shows the last passing checks of the images that run, or no `checks` when
there are none: never the failed run's.) While the controller works on an
environment, its record has `in_progress` (`{"action": "deploy", "since":
…}`; also `teardown`, `stop`), published when the step starts and removed
when it ends (0.15.0; the status no longer waits for the whole poll).
`gitops status` prints it as `checks passed 6/6 at …`. `verification` describes the
last verification (`null` after a deploy that applied):

```json
{"state": "failed", "trigger": "checks-changed",
 "checks_hash": "sha256:…", "rolled": [], "at": "2026-10-02T10:00:00Z",
 "failed": [{"check": "http-login", "code": "check-failed",
             "detail": "GET /login returned 500"}]}
```

`state` is `verified` or `failed` (`failed` lists the failing checks);
`trigger` is `checks-changed` (the check set changed since its last
verification) or `unverified` (the release's last verification failed, or
predates 0.14.5); `rolled` is always empty. A failed step adds `failure`:
`log_tail` (the scrubbed end of a failed build Job's log, at most 4000
characters) and `kept_job` (that Job, kept with its pod log until the next
build of the same branch), or `denied` (the `verb`, `resource`, `namespace`
and HTTP `status` of the request the Kubernetes API refused, as Piceli asked
for it; never the server's answer). Dropped requests are listed in
`rejected_requests` with their code.

With several clusters (0.15, {doc}`multicluster`) each environment placed on
several has `clusters.<cluster>` (`state`, with `unreachable` while its API
does not answer, `health`, `checks`, `revision`, `reason`, `last_contact`,
`namespace`, `api`, `held_by` when a rollout holds it), and the status has
`clusters.<name>` (reach of every cluster) and `removals` (placements
removed and what was kept).

A composition controller's components carry `provenance` (0.22.0: where
each image was built, see above), and its `controller` adds `build_health`
(each build host and builder node with health events: `kind` `host` or
`node`, `quarantined`, `reason`, `since`, `recent`, `policy` and the last
`events`) and `last_rebuild` (the last `gitops rebuild` it took: `host`,
`since`, `at`, `envs`, `dropped`), each only when there is one.

`piceli gitops approve`, `piceli gitops sync`, `piceli promote` and `piceli
env stop|start` add one key each to the ConfigMap `piceli-gitops-requests`
(kinds `approve`, `sync`, `promote`, `stop`, `start`; 0.22.0: `rebuild`
from `gitops rebuild`, `host-release` from `gitops release-host`); the controller
removes a request once it handled it, oldest `at` first (a stop and a start
waiting together: the later one wins). An approval stands while the plan
keeps its hash: a push that changes nothing in the plan does not ask again.
A branch environment's run names its branch's push as its trigger, and a
deploy applied by the owner's policy records `policy` as its approver. Locally, `piceli gitops run --once --state-dir DIR` (with
`--kubeconfig`/`--context`) runs one poll, and `status`, `approve` and
`promote` take `--state-dir DIR` instead of a cluster.

#### Heartbeat (0.16.0)

`controller.last_poll` is written when a poll starts, so it stands still
while one step runs (a long build or rollout). The controller therefore
also publishes `controller.heartbeat_at`, refreshed every
`controller.heartbeat_seconds` (30) by a thread of its own while the process
is alive, also during builds and deploys. The heartbeat writes only that
field of the status document (no history), never at the same time as the
step's own publish. `gitops status` (`health`), `cluster status` and the
UI's `controller_live` report `stale` when the heartbeat is older than three
heartbeats and a minute; for a controller without `heartbeat_at` (before
0.16.0) the age of `last_poll` decides, as before.

#### Restarts during a step (0.16.0)

A controller that stops during a step (a crash, an out-of-memory kill, a
node drain) left its record `in_progress` and its run journal `running`.
When it starts again it marks them: the record loses `in_progress` and has
`interrupted` (`action`, `since`, `at`: when the restart found it) until its
next step, and every run journal still `running` becomes `interrupted`
(reason `gitops-run-interrupted`, `finished_at` when it was detected, its
running stage `interrupted` too). The history lists that run as
`interrupted`; the environment is retried as before.

#### Deployment history (`piceli-gitops-history`)

A composition controller publishes each environment's recent runs in the
ConfigMap `piceli-gitops-history` (key `history.json`, schema
`piceli.gitops-history.v1`), which the UI reads. Since 0.16.0 each run
(`kind: "run"`) has, besides its outcome, plan, checks and images:

- `stages.<stage>` with `state`, `seconds`, `started_at` and `finished_at`
  for `inputs`, `build`, `deliver`, `prerollout`, `backup` (the restore
  point), `plan`, `apply` and `checks`, plus `prune` (the apply's deletes)
  and `rollback` (the automatic rollback after failed checks) when they ran;
- `builds.<image>`: `started_at`, `finished_at` and `state` (`done`,
  `failed`, `cached`) of each image the controller built or mirrored
  (images built by one build Job share its times) and of the run's builds;
- `approval_wait` (`started_at`, `finished_at`, `seconds`): from the plan
  waiting for approval to the owner's approval;
- `approved_by` (`via`, `at`); `at` is set for `policy` approvals too (when
  the run the policy approved was created);
- `state` and `run_state` `interrupted` for a run the controller died under.

Stops, starts and teardowns are entries of their own, newest first next to
the runs: `kind` (`stop`, `start`, `teardown`), `action` (the same), `state`
(`stopped`, `started`, `removed`), `at`, `by` (`declared`, `requested`,
`idle`, `removed`), `via` (`cli`, `ui`, `declaration`, `controller`),
`started_at`, `finished_at`, `namespace` and, for a stop, `stop` (`by`,
`via`, `at`). A torn-down branch environment keeps its entries in the
history (the newest 20 such environments).

#### Compatibility of the documents

`piceli.gitops-status.v1` and `piceli.gitops-history.v1` change only
**additively** within v1: a release adds fields, and never removes, renames
or changes the type or meaning of one. A tool that reads them keeps working
across Piceli releases as long as it ignores fields it does not know. A test
in Piceli's suite fails when a documented field disappears. A change that is
not additive would be a new schema (`v2`) published next to the old one.

## What an export does not cover

With `piceli deploy` or `piceli release`, Piceli computes a plan against the
live cluster, applies only the plan you approved, journals every write and
can resume or roll back. When another controller applies an exported
artifact, **none of that runs**: that controller applies whatever the
artifact holds, on its own schedule.

| | `piceli deploy` / `release` / Piceli's controller | An exported artifact applied by Flux or Argo CD |
| --- | --- | --- |
| Typed model, environments, `render`, `--diff-env` | yes | yes (you render before publishing) |
| Review before the change | the plan, field by field, against the live cluster | the rendered files (and the artifact digest you approve); no live diff from Piceli |
| Approval | the plan hash | the artifact digest, for the **push**; what the controller then applies is governed by its own configuration (sync policy, PR review) |
| Journal, resume, `release rollback` | yes | no; roll back by pointing the controller at a previous digest |
| Adoption and replacement rules, owner labels, pruning safety | yes | the controller's (Flux `prune`, Argo CD `prune`) |
| Generated and rotated secrets, private versions | yes | no: Secrets must come from outside the artifact (see [Secrets](#gitops-secrets)) |
| `[[checks]]` and automatic rollback | yes | no; use the controller's health checks |
| Builds and image delivery | yes | no; render from a spec whose images are pinned by digest |
| `piceli status`, `piceli access` | yes | `status` reads a Piceli release's state, so it does not see a controller's objects; `access` port forwards still work |

To let other people install the app on their own clusters with Helm or
plain YAML, see {doc}`helm_charts`.

Do not run both on the same objects: a Piceli release and another controller
would fight over the fields.

## Publish an OCI artifact

```sh
piceli publish app.py:app --env prod --to oci://registry.example/shop/app:prod
```

Without `--approve` nothing is sent. Piceli renders the target (same
`TARGET`, `--spec`, `--namespace` and `--env` as `piceli render`), turns every
object into a file, packages the files, and prints the artifact's digest
(exit `3`):

```text
{"state": "approval-required", "digest": "sha256:8b83…", "files": ["settings_configmap.core_shop_settings.yaml", "web_deployment.apps_shop_web.yaml", "web_service.core_shop_web.yaml"], "target": "oci://registry.example/shop/app:prod?tls=true", "namespace": "shop", "omitted_secrets": [], "config": {…}, "layer": {…}, "media_type": "application/vnd.oci.image.manifest.v1+json"}
```

Review the files (`piceli render … --out /tmp/review` writes the very same
bytes), then push that exact artifact:

```sh
piceli publish app.py:app --env prod --to oci://registry.example/shop/app:prod \
  --credentials /path/to/registry.json --approve sha256:8b83…
```

Piceli uploads the blobs the registry does not have yet, puts the manifest
**by digest**, then under the tag (when `--to` has one), reads both back and
prints `{"state": "published", "digest": …, "reference":
"registry.example/shop/app@sha256:8b83…", "url": "oci://registry.example/shop/app", "tag": "prod", …}`.

- **Deterministic.** The same render always has the same digest, so the
  approval binds the content: if the model, an option or an annotation
  changed, `--approve` is refused with `gitops-artifact-changed`. Publishing
  the same artifact again pushes nothing new and is safe to retry.
- **Credentials** come only from `--credentials FILE`, a private (`chmod 600`)
  JSON file with `{"username": …, "password": …}` or `{"token": …}`, the same
  as `piceli artifacts deliver`. They are never printed. `--ca-file` trusts a
  private CA. Plain HTTP is accepted only for a loopback registry.
- **Annotations.** `--source URL` and `--revision REV` set
  `org.opencontainers.image.source` and `org.opencontainers.image.revision`
  (Flux shows the revision, for example `main@sha1:<commit>`); the namespace
  and `--env` are recorded as `io.piceli.render.namespace` and
  `io.piceli.render.environment`. No creation time is recorded, so the
  digest stays reproducible.
- It never contacts a cluster.

### Layout

The artifact is exactly what `flux push artifact` produces:

| Part | Media type | Content |
| --- | --- | --- |
| Manifest | `application/vnd.oci.image.manifest.v1+json` | canonical JSON, one layer, the annotations above |
| Config | `application/vnd.cncf.flux.config.v1+json` | an empty image config naming the layer's uncompressed digest |
| Layer | `application/vnd.cncf.flux.content.v1.tar+gzip` | a tar of the files, sorted, with zero timestamps and owners, gzip without name or time |

Each object is one file, `<component>_<kind>.<group>_<namespace>_<name>.yaml`
(`core` for the core group, `cluster` for cluster-scoped objects), YAML with
sorted keys and a `# piceli component: …` comment. There is no
`kustomization.yaml`: Flux generates one, Argo CD reads the directory.
Components and their dependencies are not ordering hints for a controller;
Flux and Argo CD apply namespaces and CRDs first, then the rest.

## Consuming the artifact with Flux

Point an `OCIRepository` at the repository and a `Kustomization` at it. Pin
the digest to apply exactly what was approved, or follow a tag:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: shop
  namespace: flux-system
spec:
  interval: 5m
  url: oci://registry.example/shop/app
  ref:
    digest: sha256:8b83…        # or: tag: prod
  secretRef:
    name: registry-pull          # a docker-registry Secret, for a private registry
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: shop
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: OCIRepository
    name: shop
  path: ./
  prune: true
  wait: true
```

The namespace the objects name must exist (create it in another
Kustomization, or add a `Namespace` to the app with `app.resource`). An HTTP
registry needs `insecure: true` on the `OCIRepository`; use it only for a
test registry. When the artifact follows a tag, Flux reports the applied
revision as `prod@sha256:<digest>`, the digest `piceli publish` printed.

## Consuming the export with Argo CD

**From OCI** (Argo CD 3.1 and later). Argo CD's repo server accepts only a
few layer media types by default; add Flux's content type to
`ARGOCD_REPO_SERVER_OCI_LAYER_MEDIA_TYPES` on the `argocd-repo-server`
Deployment (keep the defaults in the list):

```text
ARGOCD_REPO_SERVER_OCI_LAYER_MEDIA_TYPES=application/vnd.oci.image.layer.v1.tar,application/vnd.oci.image.layer.v1.tar+gzip,application/vnd.cncf.helm.chart.content.v1.tar+gzip,application/vnd.cncf.flux.content.v1.tar+gzip
```

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: shop
  namespace: argocd
spec:
  project: default
  source:
    repoURL: oci://registry.example/shop/app
    targetRevision: sha256:8b83…   # or the tag, e.g. prod
    path: .
  destination:
    server: https://kubernetes.default.svc
    namespace: shop
  syncPolicy:
    automated:
      prune: true
```

**From Git.** Render into a directory of your GitOps repository and commit it:

```sh
piceli render app.py:app --env prod --out deploy/shop/prod
git -C deploy add shop/prod && git -C deploy commit -m "shop: render prod"
```

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: shop
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://git.example/platform/deploy.git
    targetRevision: main
    path: shop/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: shop
```

`--out DIR` writes the same files as the artifact's layer and a
`.piceli-render` marker listing them, and prints one `{"state": "written",
"files": […], "removed": […], "omitted_secrets": […]}` object. The directory
must be absent, empty, or one a previous `--out` wrote: then only the files
that render listed are replaced or removed, files of your own (a README, a
`kustomization.yaml`) stay, and a new file never overwrites one Piceli did not
write (`render-out-refused`). Flux can consume the same directory with a
`GitRepository` source.

(gitops-secrets)=
## Secrets

A controller applies the files as they are, so they must never carry secret
values, and Piceli's secret store (generated, templated and rotated values,
private versions) does not reach the controller. The rules:

- **A `Secret` object is refused** (`gitops-secrets-present`) unless you pass
  `--secrets external`. Then every Secret is left out of the files and listed
  in `omitted_secrets` (`namespace/name`); the workloads still reference them
  by name, so they must exist before the controller applies. Provide them with:
  - **SOPS:** commit SOPS-encrypted Secret manifests to Git and let a Flux
    `Kustomization` with `decryption.provider: sops` apply them (Argo CD needs
    a SOPS plugin). Piceli's own `Sops` source decrypts at plan time for
    `piceli deploy`; that does not apply here.
  - **External Secrets Operator:** declare an `ExternalSecret` in the app. Its
    `secretKey` and `secretStoreRef` fields look sensitive, so declare them
    public:

    ```python
    app.resource(
        "external-secrets.io/v1",
        "ExternalSecret",
        "db",
        {
            "secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"},
            "target": {"name": "db"},
            "data": [
                {
                    "secretKey": "password",
                    "remoteRef": {"key": "shop/db", "property": "password"},
                }
            ],
        },
        public=["spec.secretStoreRef", "spec.data.*.secretKey"],
    )
    ```

  - or created by hand or by another tool.
- **A redacted value is refused** (`gitops-secret-value`): an env var or a
  field named like a password, token or credential that holds a literal, or a
  value Piceli injects at apply time. Move it into a Secret, or declare a field
  that is not secret with `public=` (the `piceli.io/public-fields`
  annotation).
- **A placeholder image is refused** (`gitops-image-unresolved`): a
  `Pipeline`'s build images are placeholders until `piceli deploy` builds
  them. Render from a `release.toml` whose `[images]` or receipts pin every
  image by digest.

(gitops-signing)=
## Signing

`piceli publish` does not sign. Until it does, sign the published digest with
cosign and have Flux verify it:

```sh
cosign sign registry.example/shop/app@sha256:8b83…
```

```yaml
spec:
  verify:
    provider: cosign
    secretRef:
      name: cosign-public-key
```

Always sign the digest, never the tag.

## Errors

| Code | Meaning |
| --- | --- |
| `gitops-secrets-present` | The render holds a Secret; pass `--secrets external` and provide it outside the files. |
| `gitops-secret-value` | A non-Secret object holds a redacted or injected value. |
| `gitops-image-unresolved` | A placeholder image; pin the image by digest. |
| `gitops-empty` | Nothing left to hand off. |
| `gitops-target-invalid` | `--to` is not `oci://host[:port]/repository[:tag]`, or an annotation value is invalid. |
| `gitops-artifact-changed` | `--approve` is not this render's digest; publish without it and approve again. |
| `gitops-push-failed` and the registry codes (`registry-unauthorized`, `registry-unreachable`, …) | The push failed (exit `1`); safe to retry. |
| `render-out-refused` | `--out` names a directory Piceli did not write, or would overwrite a foreign file. |
| `gitops-repo-invalid`, `gitops-image-unpinned`, `gitops-config-invalid` | `gitops enable` refused its options: credentials in the URL, an image not pinned by digest, a bad glob or size. |
| `gitops-plan-changed` | `gitops enable --approve` or `gitops disable --approve` names a hash that is not the current plan's. |
| `gitops-target-required`, `gitops-not-installed`, `gitops-cluster-failed` | No cluster named, no controller in the namespace, or the API refused a request. |
| `gitops-approval-stale`, `gitops-promote-unknown`, `gitops-request-invalid` | A request the controller dropped (listed in `rejected_requests` of the status). |
| `gitops-git-failed`, `gitops-pipeline-invalid`, `gitops-step-failed`, `gitops-port-unavailable` | A poll or a branch step failed; recorded in the status and retried. |
| `gitops-state-invalid`, `gitops-controller-locked` | The controller's state is unreadable, or another controller holds it. |

`piceli explain <code>` prints the cause and the fix; {doc}`reference/errors`
lists every code.

### Volumes of a torn-down branch

Deleting a branch deletes its namespace and claims. With a storage class
whose reclaim policy is `Delete` the volumes go with the claims. With
`Retain` they stay: the teardown lists them in its result (`volumes_left`)
and the log. To have the controller delete them, opt in with `piceli gitops
enable --delete-volumes` (or `Controller(delete_volumes=True)` in a
composition): it grants the controller `delete` on PersistentVolumes
cluster-wide, which Kubernetes cannot narrow further; teardown still
deletes only the volumes bound to the branch's own claims. Teardown also
removes the ClusterRoles and ClusterRoleBindings the branch's release
created (labelled `piceli.io/env-namespace`).
