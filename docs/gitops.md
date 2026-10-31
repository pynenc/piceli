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
until it succeeds. Since 0.41.0 a failure that repeats the attempt before it
(the same code, and the same output on the same revision and inputs) is not
retried again: the environment is `failed` at once, with `stopped:
"same-failure"` in its status and history entry. Failures that may pass on
their own (an unreachable host or network, a timeout, a quarantined host,
disk or memory pressure) are still retried. A new push or `piceli gitops
sync` tries again. `piceli gitops status [--json]` shows everything:

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

(gitops-enable-wait)=
### Wait for every environment after an install: `--wait` (0.28.0)

`piceli gitops enable infra.py --approve sha256:… --wait` installs or
upgrades the controller, then waits until every environment runs the
revision it follows with its checks passed. While it waits it prints one
line per environment when its state changes, and once a minute:

```text
waiting up to 900s for every environment to run its revision
main: deploying, stage build 2m
staging: building (waiting for a build host) 3m
main: running 3f2a91c07d4e, checks passed
staging: failed in stage checks (pipeline-checks-failed)
staging: failed in stage checks (pipeline-checks-failed) at 8c1d2e3f4a5b
  failing checks: home (status 503)
  log tail:
    ...
```

It stops at the first failure: an environment that failed (its reason
code, stage and log tail), checks that fail on a running release, a
composition that does not import at its commit (nothing can deploy), or
Git unreadable for two polls. It only judges the status after the new
controller's first poll, so an old status never ends the wait.
`--timeout` (default `15m`) and `--interval` (seconds between reads,
default 10) tune it.

Exit codes, for scripts:

| Exit | Meaning |
| --- | --- |
| 0 | every environment runs its revision, checks passed |
| 1 | a failure (the JSON's `wait.failure`: `env`, `reason`, `stage`, `commit`, `detail`, `log_tail`) |
| 2 | the time ran out (`gitops-wait-timeout`); also a refused argument, before any wait |
| 3 | no `--approve`: the plan was printed, nothing installed |

`piceli promote HOST --from ENV --approve sha256:… --wait` does the same
for a Nix host ({ref}`promote-wait`).

(gitops-enable-canary)=
### Test an upgrade before it replaces the controller: `--canary` (0.28.0)

A new controller version can fail on a composition the old one deployed
(a keyword the new API renamed, a stricter check). Without a test, you
find out only after the live controller was replaced, and nothing deploys
until you fix it. `--canary` runs the new version first:

```bash
piceli gitops enable infra.py --image ghcr.io/example/piceli@sha256:… \
  --approve sha256:… --canary --wait
```

Before the approved plan replaces the controller, Piceli runs the new
image once, as a Kubernetes Job in the controller's namespace. The Job
runs the new version's own code (`piceli gitops canary`) and, applying
nothing:

- imports the composition at the followed branch's head;
- resolves the revision each environment would deploy;
- loads each build spec and resolves each component, without building
  (an image the live controller already built for the same sources is
  reused; any other counts as a build);
- plans each environment (a dry run: what would change), so refusals such
  as `retained-content-differs` or `versioned-secret-generated` show up.

```text
canary: running ghcr.io/example/piceli@sha256:… on the composition (nothing is applied)
composition: imports at 3f2a91c07d4e
main: imports, plans 3 changes, builds 2 components: ok
rc: not checked (waiting for a ref of shop)
canary: ok
```

A failure refuses the upgrade with `gitops-canary-failed` (exit 2),
naming the first failure; the live controller keeps running:

```text
composition: does not import at 8c1d2e3f4a5b (composition-invalid): importing infra.py failed: TypeError: ...
canary: failed
rejected: the new controller's canary failed: the composition: import failed (composition-invalid): ...; the live controller was not replaced [gitops-canary-failed]
```

`--force` replaces the controller anyway. A passing canary goes on to the
normal install, and `--wait` after it. Under `--json` the result is the
`canary` object of the output. `--timeout` (default `15m`) bounds the Job.

Why a Job with the new image: only it runs the new version's code, with
the controller's own Git credentials and access, inside the cluster. The
Job's pod is the new controller's pod with three changes: it runs `gitops
canary` instead of `gitops run`, its state directory is an empty scratch
volume (never the live state claim), and its config is a copy in its own
ConfigMap. It plans with "plan only" and has no way to build, copy images,
create namespaces or apply. The Job, its pod and its ConfigMap are removed
when the command ends: pass, fail or crash (the Job also removes itself ten
minutes after it ends).

What it does not check: builds themselves (only that they can start),
checks after a deploy, and environments waiting for a ref that does not
exist yet. The image must be 0.28.0 or newer (an older one has no `gitops
canary`, and the canary fails saying so). With no controller installed
yet there is nothing to replace, and the canary is skipped. It runs only
with `--approve`, for a composition (`gitops enable infra.py`).

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
piceli gitops cache clear --crate geometry     # only these crates' artifacts
```

The controller runs it between steps, as a short Job on the builder node
(the build Job's image and claim, no Secret), and records
`controller.cache_clear` (`at`, `crates`, `state`; from 0.38.0 also
`reason` and `message` when the node refused or evicted the Job's pod).
Since 0.35.0 the Job tolerates the `node.kubernetes.io/disk-pressure`
taint: it frees space, so it runs on a node whose disk is full. Since
0.38.0 it has the priority class `system-node-critical` too, because under
DiskPressure the kubelet admits only critical pods (a ResourceQuota of the
namespace that limits that class stops it; the refusal says so).

**The build cache's budget** (0.35.0). On a storage class without quotas
(k3s `local-path`) a claim is a directory on the node's disk, and nothing
stops it from growing past its size until the node is full. So each build
Job first measures its cache claim and evicts the least recently used
**units** (each `target/<arch>-<page size>`, `target/shared`, each
`stage/<name>` and `blobs`) while the cache holds more than 80% of the
claim (`JobSettings.cache_budget`, `ClusterBuildConfig(cache_budget=)`) or
its disk has less than 15% free. Its log says what went (`[build] cache
budget: evicted target/arm64-16384 (3.2 GiB, over-budget)`), its receipt
carries `cache` (`used_bytes`, `max_bytes`, `evicted`, `over_budget`,
`fs_free_bytes`, `fs_low`), and the controller publishes it under
`controller.storage.caches`.

**Disk pressure** (0.35.0). Every minute the controller reads which nodes
report `DiskPressure` and the development cache's last budget check
({doc}`dev_builds`), and publishes them as `controller.storage` (`nodes`,
`caches`, `checked_at`). A node under pressure raises `node-disk-pressure`
and a cache still over its budget raises `cache-over-budget`, each resolved
once the condition ends (`piceli gitops alarms`).

**Recovering from disk pressure** (0.38.0). No run or build starts on a
node under disk pressure, so the budget eviction they do before they start
never runs there. When such a node holds a Piceli cache (the build cache,
or the development cache of `DevBuilds(node=...)`), the controller starts
that eviction itself: the cache-clear Job in its evict-to-budget mode, with
the critical priority above, at most once per poll window (a minute, or
the poll interval when longer). The controller's role in the development
namespace cannot create Jobs (untrusted code runs there), so for the
development cache `storage.relief` records the refusal and the problem
says to run `piceli dev cache clear`, which runs with your rights. It
publishes:

- `storage.relief` (`at`, and per cache: `node`, `state`, `evicted`,
  `freed_bytes`, the `job`, and the node's `reason` and `message` when it
  refused the pod);
- `storage.caches`: each cache's last measured use (`used_bytes`,
  `max_bytes`, `fs_free_bytes`, …) from the runs', the builds' or this
  Job's report;
- `storage.problem` and `controller.storage_problem`: a plain sentence
  naming the node and the cache. While it is there the controller's state
  is `degraded` and `gitops status` reports `health: degraded` with it.

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

The waiting environment says what it waits for (`waiting_for` in its status,
and on its line in `piceli gitops status`), for example:

```text
main: retrying 1a2b3c4d5e6f (build-waiting); waiting for a build host (docker)
  to build web: offered to builder-1; not claimed; in-cluster fallback at
  2026-01-01T10:00:20Z
```

When there is no fallback it says why: `claimed by <host> since <time>`,
the builder node is quarantined, `no in-cluster builder image`, or
`builds=("hosts",)`. A claim lives while its host reports (0.27.1): a host
that sent no claim or progress report for 10 minutes is taken for dead, so
the fallback may take the build and another host may claim it.

(build-identity)=
**A build is named by its inputs, not its commit (0.42.0).** A wanted build
is named by each image's change key (see {doc}`compositions`, "change
key") and the platforms. A commit that changes nothing an image reads (for
example a `flake.lock` no image includes) asks for the same build: a host's
claim or an in-cluster Job that is running continues, nothing is cancelled
or offered again, and its images serve the new revision. Before 0.42.0 such
a commit cancelled the host's running build and offered a new one.

(build-replacement)=
**A replacement goes to the host that held the build (0.42.0).** When an
environment's inputs really change while a host builds them, the new build
is offered first to that host. The in-cluster fallback waits while the host
is alive (it sent a report in the last 10 minutes, the same rule as a
claim), even past `host_wait`; the waiting environment says `offered first
to build host <host>, which held the build these inputs replace`. When the
host stops reporting, or claims another build, the usual rules apply
(`host_wait` when no host is alive). A build that replaces one the cluster
was building goes back to the cluster at once.

(cluster-build-jobs)=
**An in-cluster build never stops the controller (0.42.0).** The controller
starts the build Job, records it (`controller.cluster_builds` in its
status) and looks at it once per poll. While it runs, other environments,
approvals, NixRelease publishes and `piceli gitops builds` go on as usual;
the environment waits (`build-waiting`) and says, for example:

```text
main: retrying 1a2b3c4d5e6f (build-waiting); building web in the cluster:
  Job piceli-image-build-0a1b2c3d4e since 2026-01-01T10:00:20Z (running)
```

A passed Job's receipt is read as before; a failed one fails the step with
its log tail and is kept until the next build of its key. A restarted
controller looks at the same Job again (it is named after the build's
inputs). `piceli gitops cancel ENV` deletes the Job, and a commit with other
inputs replaces it. `piceli gitops run --once` still waits for its build.

On a build host, with the cluster's credential profile:

```sh
piceli builder serve --cluster infra.py:my_cluster            # until Ctrl-C
piceli builder serve --cluster infra.py:my_cluster --once     # one build
```

It takes the oldest wanted build nobody holds (request `build-claim`; a
claim with no report from its host for 10 minutes lapses), fetches each source at its commit
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

(build-host-handover)=
**A failure before the build runs is handed over (0.29.0).** When a host
fails before the build's first command (it cannot fetch a source, or its
container engine cannot start the build container), the build is not
failed. The host answers `build-failed` with `early`; the controller drops
its claim, **quarantines the host for that source** and hands the build on:

- to another declared build host that may take it, which gets 60 seconds
  before the in-cluster fallback runs;
- to the in-cluster fallback at once when no other host may take it;
- with `builds=("hosts",)` and no other host, the environment fails with
  the host's reason.

A quarantined host's claims of builds that need that source are refused,
and its `builder serve` skips them and says why. The quarantine lasts until
`piceli gitops release-host HOST` (the status shows it under
`controller.source_quarantine`; the waiting environment says `handed over
by HOST (reason)`). A failure after the build started (a compiler error)
still fails the step, as before.

(build-hosts-declared)=
**Only declared hosts build (0.29.0).** When the composition declares build
hosts (`DevBuilds(hosts=[...])`), the controller takes claims, answers and
standbys only from them; a request from any other host is refused
(`rejected_requests`, `not declared in the composition`). A host still
running after you removed it from the composition says so in its `builder
serve` output and claims nothing until it is declared again:

```text
builder: the controller's composition does not declare build host old-host
  (it declares builder-1, builder-2); claiming nothing. Declare it in
  DevBuilds(hosts=[...]) or stop this service (piceli builder uninstall
  --host old-host)
```

The status lists the declared hosts (`controller.build_hosts_declared`). A
composition that declares none takes any host, as before.

(builder-take-next)=
**Test a host by hand: `--once --take-next` (0.29.0).** A short `host_wait`
(for example `20s`) lets the in-cluster fallback win every race against a
`builder serve --once` started by hand. With `--take-next` the host stands
by: it tells the controller (`build-standby`), takes the next build it may
(one waiting now, or the next one asked for), claims it and builds it,
then exits. While it stands by, the fallback waits for it, at most 2
minutes past its usual time:

```sh
piceli builder serve --cluster infra.py:my_cluster --host builder-1 \
  --once --take-next --timeout 600 --json
# elsewhere, to give it a build at a commit already built:
piceli gitops build main
```

`piceli gitops build ENV` builds the environment's images again at the
commit it follows, through its usual plan and approval. Since 0.29.0 it
also does so when that commit is already deployed (before, it did nothing
then, so a host could not be tested on a built commit).

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

(builder-service)=
#### Run a build host as a service (0.28.0)

`builder serve` in a terminal stops when the terminal closes or the machine
sleeps, and builds wait. `piceli builder install` makes the build host
serve on its own. Run it **on the build host itself**, in the directory
where `--cluster MODULE:ATTR` loads, with the same arguments `builder serve`
takes:

```sh
# Smoke test first: the credentials this host needs, then one build.
piceli builder serve --cluster infra.py:my_cluster --host builder-1 --check
piceli builder serve --cluster infra.py:my_cluster --host builder-1 --once --json

# Install the service (as you: a user service).
piceli builder install --cluster infra.py:my_cluster --host builder-1
# Or as root, running as the user "builder" (a system service).
sudo piceli builder install --cluster infra.py:my_cluster --host builder-1 \
  --user builder --state-dir /var/lib/piceli-builder

# Remove it.
piceli builder uninstall --host builder-1
```

- **Linux:** a systemd unit `piceli-builder-builder-1.service`, in
  `/etc/systemd/system` as root, else in `~/.config/systemd/user`. A user
  service stops when you log out unless lingering is on
  (`sudo loginctl enable-linger $USER`). Logs go to the journal:
  `journalctl -u piceli-builder-builder-1 -f` (add `--user` for a user
  service).
- **macOS:** a launchd property list `piceli.builder.builder-1.plist`, a
  LaunchDaemon in `/Library/LaunchDaemons` as root, else a LaunchAgent in
  `~/Library/LaunchAgents`. Logs go to `builder.log` in the state
  directory (default `~/.cache/piceli/builder`).

The service runs the same `piceli` program that ran `install` (its absolute
path; the Piceli version is in the unit's description), with the exact
`builder serve` arguments. It starts again 10 seconds after a failure and
is stopped with SIGINT, as Ctrl-C stops `serve` (SIGTERM, which launchd
sends, stops it the same way). Its `PATH` holds the directories where
docker, git and kubectl are now; `install` refuses when one is missing
(`builder-service-invalid`, naming it). `--print` writes the unit to
stdout and changes nothing; since 0.29.0 it checks nothing either, so it
prints the unit on a machine that does not hold the secrets yet (run
`builder serve --check` where the service runs).

**`--check` (0.29.0) also tries every source and the scratch directory.**
Besides the credentials, `builder serve --check` runs `git ls-remote` of
every source the controller follows, as the build fetches it (the URL, then
the same repository over SSH, never prompting), and runs the builder image
as a build does (its user, no capabilities) to write into a scratch archive
directory and the build cache. Each result is a line and a field of
`piceli.builder-check.v1` (`sources`: `reachable` with `via`, or
`unreachable` with Git's message; `scratch`: `writable` or `not-writable`);
the command exits 1 when one fails.

Before it writes anything, `install` runs the checks of `builder serve
--check`: the cluster's credential profile and kubeconfig reach the
controller, the keys of each `--publish` cache are in this machine's
secrets, and the host holds every credential its NixReleases need. A
missing one refuses with `infra-credential-missing`, naming it and the
command that adds it. No secret value goes into the unit or its arguments:
only `HOME`, `PATH`, and `PICELI_CREDENTIALS_DIR` / `PICELI_PROFILES_DIR`
when they are set where `install` runs.

**One service per host name.** Never run two servers under the same
`--host` name: the controller takes them for one host. Different names on
one machine or many are fine; the controller decides which claim wins.

**The service user.** It needs access to the container engine (on Linux,
the `docker` group) and holds its own Piceli state: its credential profile
(`~/.config/piceli/profiles`, `piceli login`), its secrets
(`~/.config/piceli/credentials`, `piceli secrets ...`), its Git access
(credential helper or SSH keys) and its state directory. Run `piceli
login` and `piceli secrets` as that user, or set `PICELI_PROFILES_DIR` and
`PICELI_CREDENTIALS_DIR` before `install`. As root, `install` checks with
that user's home, and the `piceli` program it runs must be one that user
may run (for example installed under `/usr/local/bin` or in its own home).

**Kubernetes permissions.** The credential profile of `builder serve`
needs, and nothing more:

- in the controller's namespace (`piceli-system`): ConfigMaps `get`,
  `create`, `patch` (it reads the status, writes its requests);
- to push images, in the registry's namespace: services `get`, pods `get`
  and `list`, `pods/portforward` `create` (the push goes through
  `kubectl port-forward service/<registry>`).

A host that delivers into the nodes over SSH, or only publishes Nix
releases, needs only the first line.

(builder-nixos)=
**NixOS.** On NixOS `/etc/systemd/system` is read-only: keep the unit in
your configuration. `piceli builder install ... --print` shows the unit
`install` would write. To upgrade Piceli or change the builder image without
a `nixos-rebuild`, keep the unit fixed and put what changes in files under
the state directory that the unit reads at start (0.29.0):

- `/var/lib/piceli-builder/version`: the Piceli version, for example
  `0.29.0`;
- `/var/lib/piceli-builder/serve.args`: the `builder serve` arguments, one
  per line (`#` comments), read by `--args-file`:

```text
# builder serve arguments for builder-1
--image
ghcr.io/pynenc/piceli-builder@sha256:<digest>
--poll
15
```

```nix
users.users.builder.extraGroups = [ "docker" ];
systemd.services.piceli-builder-builder-1 = {
  description = "Piceli build host builder-1";
  wantedBy = [ "multi-user.target" ];
  wants = [ "network-online.target" ];
  after = [ "network-online.target" "docker.service" ];
  path = [ pkgs.docker pkgs.git pkgs.kubectl pkgs.uv ];
  environment.HOME = "/var/lib/piceli-builder";
  # The version and the flags are read when the service starts.
  script = ''
    exec uvx --from "piceli==$(cat /var/lib/piceli-builder/version)" \
      piceli builder serve --cluster infra.py:my_cluster --host builder-1 \
      --state-dir /var/lib/piceli-builder \
      --version-file /var/lib/piceli-builder/version \
      --args-file /var/lib/piceli-builder/serve.args
  '';
  serviceConfig = {
    User = "builder";
    WorkingDirectory = "/srv/deploy";  # where infra.py is
    Restart = "on-failure";
    RestartSec = 10;
    KillSignal = "SIGINT";
    TimeoutStopSec = 120;
  };
};
```

An upgrade is then: write the new version (and the new image digest in
`serve.args`), `systemctl restart piceli-builder-builder-1`, and `builder
status` shows the new version. 0.33.0: with `--version-file` the host
writes the controller's version itself and exits 75; `Restart` starts it
again on that version ({ref}`builder-version`). If the deploy directory pins Piceli itself
(a `uv.lock`), run `uv run piceli ...` there instead of `uvx` and upgrade by
updating the lock. The fixed unit without these files is the same service
`install` writes:

```nix
users.users.builder.extraGroups = [ "docker" ];
systemd.services.piceli-builder-builder-1 = {
  description = "Piceli build host builder-1";
  wantedBy = [ "multi-user.target" ];
  wants = [ "network-online.target" ];
  after = [ "network-online.target" "docker.service" ];
  path = [ pkgs.docker pkgs.git pkgs.kubectl ];
  environment.HOME = "/var/lib/piceli-builder";
  serviceConfig = {
    User = "builder";
    WorkingDirectory = "/srv/deploy";  # where infra.py is
    ExecStart = "/path/to/piceli builder serve --cluster infra.py:my_cluster --host builder-1";
    Restart = "on-failure";
    RestartSec = 10;
    KillSignal = "SIGINT";
    TimeoutStopSec = 120;
  };
};
```

(builder-version)=
#### The same Piceli version as the controller (0.33.0)

A build step may use a command or a format that only the newer Piceli
knows, so a build host claims only while it runs the controller's version
(`controller.version` in the status). On a mismatch `builder serve`:

1. claims nothing; the controller builds in the cluster after `host_wait`,
   with its own image;
2. tells the controller its version (request `build-host-version`), so
   `piceli builder status` shows the host `version-mismatch`, `piceli
   gitops hosts` prints `VERSION MISMATCH` and the `build-host-version`
   alarm fires (resolved once the host runs the same version);
3. with `--version-file PATH`, writes the controller's version into PATH
   (one line, only when it changed);
4. exits **75** (`EX_TEMPFAIL`, rejection `builder-version-mismatch`).

Its service then installs that version as the service user, without root,
and starts it again. With Piceli installed by `uv tool install` into the
service user's home, a systemd unit does it before each start:

```ini
[Service]
User=builder
Environment=HOME=/var/lib/piceli-builder
ExecStartPre=/bin/sh -c 'uv tool install "piceli==$(cat /var/lib/piceli-builder/version)"'
ExecStart=/var/lib/piceli-builder/.local/bin/piceli builder serve --cluster infra.py:my_cluster --host builder-1 --version-file /var/lib/piceli-builder/version
# no --image: the host builds in the controller's builder image
Restart=on-failure
RestartSec=10
```

`uv tool install` with the version already installed does nothing. The
`uvx --from "piceli==$(cat .../version)"` unit above follows the controller
the same way once `--version-file` points at its version file. A unit that
`builder install` wrote runs a fixed `piceli`: after a mismatch it restarts
every 10 seconds and claims nothing until that program is upgraded (`uv
tool upgrade piceli`, `pipx upgrade piceli`). `builder serve --check`
reports both versions and both builder images (`version` in
`piceli.builder-check.v1`) and exits 1 on a version mismatch.

**The builder image is the controller's too.** The build steps run in the
builder image, so a host on the right version still fails in an old image.
The controller publishes its build Job's image (`controller.builder_image`);
`builder serve` builds in that image, pulls it again when it changes, and
claims nothing while it cannot pull it. `--image` is an override only: the
host keeps it and says once that it differs from the controller's. Leave
`--image` out of `serve.args` to follow the controller. The versions compare exactly; a controller that publishes
none (or `unknown`) is not checked.

#### Which build hosts serve: `piceli builder status` (0.28.0)

```sh
piceli builder status --cluster infra.py:my_cluster [--json]
```

```text
builder-1        building      Piceli 0.28.0   seen 2026-11-02T09:14:05Z; claim 3f2a91c07d4e live (build); last build 3f2a91c07d4e claimed
builder-2        claim-expired Piceli 0.27.1   seen 2026-11-02T08:40:00Z; claim 8c1d2e3f4a5b expired; last build 8c1d2e3f4a5b claimed
builder-3        quarantined   Piceli 0.28.0   seen 2026-11-01T21:00:00Z; last build 1a2b3c4d5e6f built; QUARANTINED since ...
builder-4        never seen (declared; is builder serve running there? ...)
```

One line per build host the controller heard from, and per declared host:
its Piceli version, when it was last seen (a claim, a progress report, an
answer or a health record), the build it holds (`live`, or `expired` after
10 minutes without a report), its last build and outcome (`claimed`,
`built`, `published`, `failed`) and its quarantine. A declared host never
seen is not serving. Each claim and progress report carries the host's
Piceli version; the controller keeps each host's last sighting in its
status (`controller.build_hosts`). `--json` prints
`piceli.builder-status.v1`.

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

### Images a merge train built: `prebuilt` (0.26.0)

A merge train that promotes a commit can build its images first, for
components and for pipeline environments' build specs (`Step.images`, see
{doc}`train`), and offer them with a `prebuilt` request
before the push this controller reacts to. Declare
`Controller(on="node-a", prebuilt=True)` to take them (off by default).
The controller keeps an offer after checking that it names a declared
component or an image of a pipeline build spec, that its reference points
into that image's repository of
this registry and is pinned by its digest, and that the digest is in the
registry. It uses an offer only when a step's own change key for the
component (computed from its own clone of the commit) is the offered key,
and the digest is still in the registry: the image is then recorded as
its own build, with the train's provenance (`builder: "train"`, the run,
the tag and the host), and the step deploys without asking a host or
starting a Job. An offer under another key is never used. Current offers
show in the status (`controller.prebuilt`), each with the commit of every
source its key was made with (`sources`). When a step builds a component
although an offer of it exists, the component's status entry says why
(`prebuilt`: `{"used": false, "key": ..., "differs": {source: {"offered",
"here"}}}`): typically another repository the build reads moved between
the train's offer and this controller's poll, so its key differs and it
builds as before. Registry delivery only: a controller delivering into the
nodes refuses offers.

(gitops-builds)=
### Every build: list, start, cancel (0.26.0)

`piceli gitops builds` lists every build the controller knows, newest
first: NixRelease publishes, environment image builds (on a build host or
the in-cluster builder) and merge-train handoffs (`prebuilt` offers).
`--target NAME` and `--state STAGE` (or `active`) filter it; `--json`
prints `piceli.builds.v1`, the document the UI's Builds page shows:

```json
{"schema": "piceli.builds.v1", "builds": [{
  "id": "web-1@2026-10-24T10:00:00Z", "kind": "nix-publish", "target": "web-1",
  "trigger": {"kind": "cli", "by": "ana", "detail": "cli web-v1.4.0"},
  "commit": "3f2c…", "tag": "web-v1.4.0", "branch": null, "host": "mini",
  "stage": "published",
  "stages": [{"stage": "approval-required", "started_at": "…", "finished_at": "…", "seconds": 40.0},
             {"stage": "queued", "…": "…"}, {"stage": "fetching-inputs", "…": "…"},
             {"stage": "building", "…": "…"}, {"stage": "uploading", "…": "…"}],
  "log_tail": null, "requested_at": "…", "started_at": "…", "finished_at": "…",
  "seconds": 312.4, "plan_digest": "sha256:…", "reason": null, "images": [],
  "running_on": ["web-1"], "host_switch": "switched",
  "actions": {"approve": false, "retry": true, "cancel": false}}]}
```

The trigger's `kind` is `tag`, `branch`, `schedule`, `push`, `sync`, `cli`,
`ui` or `train`. The stage is `approval-required`, `queued`,
`fetching-inputs`, `building`, `signing`, `uploading`, `published`,
`failed`, `cancelled` or `rejected` (the promotion gate); a NixRelease's
signing and upload run as one step on the build host and show as
`uploading`. `running_on` names the targets (`piceli targets`) whose
deployed revision is the build's commit (or tag). A NixHost counts only
when its own agent reports the revision as its current system with its
checks passed (0.37.0); the controller alone knows only what it published.
A published NixRelease's `host_switch` says `switched` then, and
`not-switched-yet` before (the CLI prints "published, host not switched
yet"); it is `null` for other builds.

While a build host builds, it reports its stage and the last 4000
characters of its log (`build-progress`, at each stage and at most every
15 seconds), so the record of a running build shows its stage and a live
log tail. At each stage it also checks that the build is still wanted:
one cancelled, or superseded by a newer commit, stops there.

`piceli gitops build NAME [--ref TAG|COMMIT]` starts a build by hand: a
NixRelease plans the ref ({ref}`below <gitops-nix-releases>`), an
environment rebuilds every image at the commit it follows and deploys it
with its usual plan and approval (`--ref` may only name that commit or
ref). `piceli gitops cancel NAME` drops a NixRelease's publish, or the
image builds an environment waits for on build hosts or in the cluster
(0.42.0: its build Job is deleted): the step fails with `cancelled` until
`piceli gitops sync`. Retry is `piceli gitops sync NAME`.

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
### Nix host releases (0.25.0)

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
`piceli secrets s3 cache-s3-write --prompt`, or carried from your machine
with `piceli secrets backup --only`: {ref}`build-host-secrets`):

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
builds images as before, unless `--publish-only` (0.25.0) is given.

**A remote machine that only publishes is a docker host with an SSH
context to itself.** `BuildHost.native` means the machine running Piceli:
every machine that loads the composition (a laptop running `dev run`, the
owner's `piceli dev nix build`) would take it for itself, so it cannot name
a remote machine. Declare the publisher as a docker host reached over SSH
with `runs=False` (0.25.0), and run `builder serve` on that machine:

```python
from piceli.infra import BuildHost, DevBuilds, DevProfile

dev = DevBuilds(
    node=None,
    image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
    profiles=[DevProfile.for_nix()],
    hosts=[
        BuildHost.docker("nix-builder", runs=False,  # it only publishes
                         context="ssh://builder@builder-1.example.org"),
        BuildHost.docker("laptop"),
    ],
)
```

```sh
# on builder-1, with the cache's keys in its own piceli secrets
piceli builder serve --cluster infra.py:my_cluster --host nix-builder \
  --publish infra.py:cache
```

`--publish-only` claims only the `nix-publish` builds of its caches,
whatever the host's kind: it never claims an image build (it says so once),
so who builds the cluster's images does not change, and it needs no
cluster registry. Without `--publish` it is refused (`dev-nix-invalid`).

`runs=False` makes the host's role part of the declaration (and of the
plans and `piceli dev hosts`, which shows it as publishing only):
`builder serve` on it implies `--publish-only` and refuses to start
without `--publish` or with `--no-publish-only` (`dev-host-publish-only`);
`piceli dev run`, `piceli dev nix build` and `builder serve` without
`--host` never pick it, so an agent's runs never land on the publisher.
`--host nix-builder` still names it.

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

**A branch, a schedule, by hand (0.26.0).** A host that follows
`Branch("main")` publishes each new head of the branch: the plan names the
branch and its pointer (`piceli/<build>/refs/heads/main.json`) and no tag,
and the build host checks that the commit is on the branch before it
builds. `schedule=` plans the newest tag or head again on a schedule
(`hourly`, `daily` at 00:00 UTC, `weekly` on Monday, or five cron fields
`MINUTE HOUR DAY MONTH WEEKDAY` in UTC):

```python
web_release = NixRelease(web, source=hosts, builder="mini", schedule="0 4 * * *")
```

`piceli gitops build web-1 --ref web-v1.4.0` (a tag of the release, its
branch or a full commit; the newest without `--ref`) plans that ref now,
and so does the UI's **Build now**. Every trigger goes through the same
gate, plan and approval; `--approve sha256:<digest>` approves it in one go
when the plan's digest is that one. The release records `trigger_kind`
(`tag`, `branch`, `schedule`, `sync`, `cli`, `ui`) and `requested_by`.
`piceli gitops cancel web-1` drops a planned, queued or running publish:
a build host that holds it stops before its next stage and its answer is
refused; the release is `cancelled` (`gitops-build-cancelled`) until a
newer tag or head, a sync or a build.

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

#### A promotion gate and `piceli promote` (0.25.0)

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

(promote-wait)=
##### Follow the release to the host: `--wait` (0.28.0)

`--wait` follows the release to the host after the push:

```sh
piceli promote web-1 --from main --composition infra.py --repo ../hosts \
    --approve sha256:<digest> --wait --timeout 15m
# web-1: promoted web-v1.4.1 at 9b1e0c2d3f4a
# web-1: publishing (waiting for a build host) 1m
# web-1: publishing (build host builder-1) 2m
# web-1: published; the host has not run it yet 6m
# web-1: running web-v1.4.1, checks passed
```

It reads the controller's publish of the tag (a failed publish is the
first failure, with the build host's log tail) and the host's own status
over SSH, as `piceli host wait` does (a failed or rolled-back attempt is a
failure). Exit 0: the host runs the tag with its checks passed; 1: a
failure; 2: the time ran out (`gitops-wait-timeout`). The JSON carries the
result under `wait`.

The host is the `NixRelease`'s own `NixHost`. Its server's applied address
and pinned host keys come from the `Infrastructure` that declares the
server: the one the composition imports from its directory (a
`machines.py` beside `infra.py`), or (0.31.0) the one `--machines
MODULE[:ATTR]` names. `--infra` is an older name of `--composition`, not of
the machines module.

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

(gitops-alarms)=
### Alarms (0.28.0)

The controller can tell you when something needs you, instead of you
finding out when you look. Declare where alarms go on the composition's
`Controller`:

```python
from piceli.infra import Alarms, Controller, Notify

controller = Controller(
    on="node-1",
    alarms=Alarms(
        targets=[
            Notify.ntfy("https://ntfy.example.org/deploys"),
            Notify.webhook(url_env="ALARM_WEBHOOK_URL"),
            Notify.desktop(),
        ],
        secret="piceli-alarms",  # holds ALARM_WEBHOOK_URL
        waiting_after="10m",
    ),
)
```

The targets are the merge train's notifiers ({doc}`train`):
`Notify.ntfy(url)` posts a short text to an [ntfy](https://ntfy.sh) topic,
`Notify.webhook(url)` posts the alarm as JSON (`piceli.alarm.v1`), and
`Notify.desktop()` is a desktop notification on your machine (below).
`when="failure"` skips the "recovered" and "restarted" messages.

After every poll the controller sends an alarm when:

| Event | When | Next command in the message |
| --- | --- | --- |
| `env-failed` | an environment failed, failed two attempts in a row, or its checks fail on the running release | `piceli gitops status` |
| `env-waiting` | an environment waits longer than `waiting_after` for a build host (`build-waiting`) or an approval (`approval-required`) | `piceli gitops builds`, or the `piceli gitops approve ENV HASH` line |
| `nix-release-failed` | a `NixRelease` publish failed | `piceli gitops builds --state failed` |
| `nix-release-waiting` | a `NixRelease` publish waits longer than `waiting_after` for a build host or an approval | `piceli gitops builds`, or its approve line |
| `composition-failed` | the composition's own repository does not import at its commit, so nothing deploys | `piceli gitops status` |
| `controller-restarted` | the controller runs a new Piceli version | `piceli gitops status` |
| `build-host-version` | a build host runs another Piceli version than the controller | `install piceli VERSION on HOST ...` |
| `node-disk-pressure` (0.35.0) | a node of the cluster reports `DiskPressure`: pods are evicted and nothing new starts there | `piceli dev cache clear; piceli gitops cache clear` |
| `cache-over-budget` (0.35.0) | a build cache is still over its budget after eviction, or its disk is nearly full (below) | `piceli dev cache clear` or `piceli gitops cache clear` |

Each message is short and names the environment or host, the reason code
and the next command:

```text
piceli: staging failed
staging failed in stage build (component-build-failed) at 3f2a91c07d4e: error: cargo build failed. Next: piceli gitops status
```

Each alarm is sent **once**: the controller keeps the alarms it sent in its
state, so a later poll or a restart does not send it again. When the
problem ends it sends one message (`staging recovered (was
component-build-failed)`); a branch environment that is torn down sends
nothing more.

**Secrets.** A URL or token that is a secret is named by environment
variable (`url_env=`, `token_env=` for ntfy, `headers_env=` for a webhook)
and stored in the Kubernetes Secret named by `Alarms(secret=)`, in the
controller's namespace, under a key of the same name:

```sh
kubectl -n piceli-system create secret generic piceli-alarms \
  --from-literal=ALARM_WEBHOOK_URL='https://hooks.example.org/T0/abc'
```

The controller's pod gets each variable from that Secret. The value is
never in the controller's arguments, logs, state or status; a target is
shown by its host name or `$VARIABLE` only.

**A failed send never stops the controller.** It is logged with
`gitops-alarm-failed` and listed under `controller.alarms.failures` in
`piceli gitops status --json`, next to the open alarms and how many were
sent.

**Desktop notifications** cannot come from the cluster. Run the follower on
your machine; it reads the controller's status and raises the same alarms:

```sh
piceli gitops alarms --kubeconfig cluster.kubeconfig --context my-cluster
# ALARM staging failed: staging failed in stage build (...). Next: piceli gitops status
piceli gitops alarms --follow --kubeconfig cluster.kubeconfig --context my-cluster
```

Without `--follow` it prints the alarms as they stand (and the sends the
controller could not make) and exits. `--follow` reads every `--interval`
seconds (30 by default) and raises each new alarm once, until you stop it.

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

#### The history on the command line: `gitops history`, `show`, `compare` (0.38.0)

The same history, from a terminal or an agent. Each command reads it as
`gitops status` reads the status (`--kubeconfig` and `--context`, or
`--state-dir`), changes nothing, and prints one JSON object with `--json`:

```console
$ piceli gitops history main --limit 2 --kubeconfig kc --context prod
main: 2 run(s)
  20261030T100000Z-1a2b  deployed; rolled web  2026-10-30T10:00:00Z .. 2026-10-30T10:06:10Z (6m 10s)
    trigger push shop/main
    sources shop@3f2a91c07d4e
    components web sha256:9b1e0c2d3f, worker sha256:77ab01c2d3
    checks 6/6 passed
  ...
$ piceli gitops show 20261030T1000 --logs --kubeconfig kc --context prod
$ piceli gitops compare main --kubeconfig kc --context prod
```

A build that failed on a build host shows the host's stage and the failing
command's last lines (0.41.0):

```console
$ piceli gitops show main@2026-10-30T10:09:00Z --logs --state-dir state
main:
  main@2026-10-30T10:09:00Z  failed (component-build-failed)  ...
    failure component-build-failed in stage build on build host builder-1
    not retried: the same failure as the attempt before, on the same inputs (a new revision or piceli gitops sync tries again)
--- failed-attempt 1 component-build-failed (stage build) 2026-10-30T10:08:00Z
[piceli] linux/amd64 command 1/1 (bash): failed in 0.1s
[piceli] the failing command's last 1 line(s) of output:
  | bash: site/scripts/build.sh: No such file or directory
--- failure component-build-failed (stage build) 2026-10-30T10:09:00Z
...
```

| Command | JSON (`schema`) | Top-level fields |
| --- | --- | --- |
| `gitops history [ENV] [--limit N]` | `piceli.gitops-runs.v1` | `history_schema`, `generated_at`, `truncated`, `environments` (`name`, `runs`) |
| `gitops show RUN [--env ENV] [--logs]` | `piceli.gitops-run.v1` | `env`, `run`; with `--logs` also `logs` (`source`, `attempt`, `at`, `reason`, `stage`, `host`, `text`) and `logs_note` |
| `gitops compare ENV [A] [B]` | `piceli.gitops-compare.v1` | `env`, `before`, `after`, `sources`, `components`, `images_changed`, `resources` (`added`, `removed`, `changed`, `partial`), `fields` |

- A run is the history's own entry (`piceli.gitops-history.v1`, above) with
  two windows added, each `started_at`, `finished_at` and `seconds`:
  `window` (the run, from its first step to its end) and `release_window`
  (from the start of its apply to its end, its checks settled; the run's
  start when it applied nothing). The release marker the controller sends
  over OpenTelemetry carries the same window ({doc}`deploy_events`).
- `RUN` is a run's `id` or `run_id`, or a unique start of one.
- `compare` without runs takes the running release and the one before it.
  With one run, it compares that run with the running release. `before` and
  `after` hold each run's identity, release, plan hashes, both windows and
  `checks_passed`. `resources` adds up the plans of the runs after `before`
  up to `after` (an object added and then removed is in neither list;
  `partial` when a plan was longer than the history keeps).
- `--logs` prints what the controller keeps: the redacted log tail of a
  failed build and of the revision's earlier failed attempts. The output of
  running workloads is in `piceli logs`.
- The history keeps object kinds and names, never field values, so
  `compare` lists no before/after values (`fields.available` is `false`);
  the UI's Compare revisions shows them for plans the UI saved.
- No history yet: `gitops-history-unavailable`. An environment or run the
  history does not hold, or fewer than two runs: `gitops-run-unknown`.

(gitops-cycle-report)=

#### Cycle report (0.28.0)

A **cycle** is one commit's way from a push to running with its checks
passed, on one target: an environment, a Nix release, a Nix host or a
train. Piceli records each cycle and its phases, so you do not keep the
table by hand:

- the controller adds `cycle` to each history run and to each environment
  in its status: `queued` (first seen to its first step), `approval`,
  `waiting-for-host`, `building` and `delivering` (a build host's build:
  asked, claimed, built and uploaded, delivered; or the controller's own
  builds), `applying` and `checking`. The same seconds go with the
  `piceli.deploy.finished` OpenTelemetry event (`piceli.cycle.seconds`,
  `piceli.cycle.phase.<name>.seconds`), through the composition's
  existing `telemetry=`;
- a Nix release keeps its stages (`approval`, `queue`, `fetch`, `build`,
  `publish`), as before;
- a train run keeps `cycle` in its `run.json` and on its root span:
  `queued`, `merging`, `testing <step>`, `promoting`, `images`,
  `publishing`, `pushing`;
- `piceli host wait` keeps the host switch it saw (`polling`,
  `downloading`, `activating`, `checking`, `boot`) in
  `~/.local/state/piceli/cycle/hosts.json` (the newest 200).

`piceli cycle report` reads them:

```console
$ piceli cycle report --since 7d --kubeconfig kc --context prod
cycle report since 2026-10-17T09:00:00Z: 3 target(s), push to running and checked
  environment  main                 12 cycle(s)  p50 6m 40s  p90 9m 12s  slowest phase building (4m 05s)  slowest cycle 11m 30s (3f2a91c0d4e5)
  nix-host     web-1                4 cycle(s)  p50 8m 02s  p90 9m 40s  slowest phase building (5m 10s)  slowest cycle 9m 40s (77ab01c2d3e4)
  train        shop                 9 cycle(s)  p50 14m 20s  p90 21m 03s  slowest phase testing unit (9m 30s)  slowest cycle 21m 03s (a1b2c3d4e5f6)
  note: main: push time unknown for 12 of 12 cycle(s); timed from when Piceli first saw the commit (first at 2026-10-17T09:12:00Z)
```

Per target it gives the count of cycles that ended running and checked
(and how many failed), the p50 (median) and p90 (nine in ten were as
fast or faster) of their time, nearest-rank, the
slowest phase (the highest p50 by phase) and the slowest single cycle.
`--target NAME` keeps one target, `--since` takes `24h`, `7d` or an ISO
time, `--json` prints `piceli.cycle-report.v1` and `--markdown` prints a
table to paste into your docs, marked as generated. The controller is read
only with `--kubeconfig` and `--context` (or `--state-dir`); without them
the report has the local trains and hosts and says so.

What it cannot know, it says: the controller learns of a push when it polls,
so a cycle runs from when Piceli first saw the commit. A Nix host's switch
is known only when `piceli host wait` saw it; a host cycle starts at the
controller's release of the same commit when the report has it.

#### Compatibility of the documents

`piceli.gitops-status.v1` and `piceli.gitops-history.v1` (and, from
0.38.0, `piceli.gitops-runs.v1`, `piceli.gitops-run.v1` and
`piceli.gitops-compare.v1`) change only
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
