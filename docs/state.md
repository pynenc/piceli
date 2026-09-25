# Share deployment state between runners

By default `piceli deploy` and `piceli release` keep their state (run
journal, release catalog, execution journal, secret store, receipts) in a
local **state directory**. That is right for one developer or one persistent
CI runner. With **shared state** the state lives in the release namespace,
so any runner can plan, any other runner can apply, resume or roll back, and
two deployers of one release never interleave.

```{admonition} Maturity: preview
:class: note

Shared state is new in 0.6.0. The default stays `local`; the choice is
per pipeline (or `release.toml`) and reversible (see
{ref}`state-move`).
```

## Turn it on

```python
pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=Registry("oci://registry.example:5000/my-app"),
    state="cluster",          # default "local"
    state_lease_seconds=60,   # how long a dead runner keeps the lock (5-3600)
    state_dir=os.environ.get("DEPLOY_STATE_DIR", ".piceli-deploy"),
)
```

For a `release.toml`:

```toml
[release]
name = "my-app"
state_dir = ".piceli"
state = "cluster"            # catalog, journal and secret_store must stay in state_dir
state_lease_seconds = 60
```

With `state = "cluster"` the `state_dir` is a **working copy**: every
command refreshes it from the cluster first, and a fresh or deleted
directory loses nothing.

## What lives where

Everything is in the release namespace, labelled
`piceli.io/state=<release>`, and excluded from every discovery Piceli runs
(a plan never shows, adopts or prunes it):

| Object | Holds |
| --- | --- |
| `Lease piceli-lock-<release>` (`coordination.k8s.io/v1`) | The release lock: holder (`<host>/<pid>/<random>`), `leaseDurationSeconds`, `renewTime`, `leaseTransitions` (the fence) |
| `Secret piceli-state-<release>` (type `piceli.io/state`) | The head: generation, chunk names, SHA-256 of the snapshot, the writer's fence |
| `Secret piceli-state-<release>-g<generation>-<n>` | The snapshot of the state directory, gzip-compressed, in chunks of at most 512 KiB (64 chunks at most) |

The snapshot holds the secret store, so it is stored only in Secrets, never
in ConfigMaps. Protect them like any other Secret: RBAC, and encryption at
rest where the cluster offers it. The snapshot leaves out lock files,
temporary files and build outputs and logs.

The target's kubeconfig needs, in the namespace, `get`, `create`, `patch`
and `delete` on `secrets` and on `leases` (`coordination.k8s.io`), besides
what the release itself needs:

```yaml
- apiGroups: [""]
  resources: [secrets]
  verbs: [get, create, patch, delete]
- apiGroups: [coordination.k8s.io]
  resources: [leases]
  verbs: [get, create, patch]
```

## The release lock

Every command that changes state (`piceli deploy`, including `--plan`;
`piceli release plan|apply|rollback|resume|stop|check`; `piceli state
import`) takes the lock of its release, namespace plus release name, not of
a directory:

- **Held by another runner:** the command is refused before it changes
  anything, `pipeline-locked` for a pipeline, `release-locked` for a
  `release.toml`, with the holder and when its lease expires:

  ```json
  {"state": "rejected", "reason": "pipeline-locked", "lock": {"holder": "runner-7/4121/1f0c2a9e", "expires_in": 41}}
  ```

- **Renewed while held:** the holder renews the lease every third of
  `state_lease_seconds`.
- **Released** when the command ends, also when it fails or is interrupted
  (Ctrl-C, `SIGTERM` from a cancelled CI job).
- **Taken over** when the holder died (`SIGKILL`, lost machine): once
  `renewTime + leaseDurationSeconds` passed, the next command takes the lock
  over (compare-and-swap on the Lease) and says so on stderr (`state: took
  over the expired lock of 'my-app' from runner-7/4121/1f0c2a9e`).
- **Fenced:** every state write first renews the lease and checks that the
  holder and `leaseTransitions` are still this run's. A run whose lock was
  taken over (it could not renew for a whole lease duration) stops at its
  next write with `state-lock-lost`, before the next action it would have
  journaled. Runners' clocks must be roughly in sync (NTP): expiry compares
  the Lease's `renewTime` with the local clock.

Reading commands (`piceli release status`, `release diff`, `release secret
show`, `piceli state show|pull|export`) take no lock.

## When the state is written

A command pulls the shared snapshot when it starts and writes it back:

- at every journaled stage change of `piceli deploy`;
- after every execution journal commit of an apply, rollback or resume,
  before the change it records (the same "intent before IO" order the local
  journal keeps), so a runner killed mid-apply is resumed exactly where it
  stopped, from any runner;
- when the command ends (also on failure and interruption).

Unchanged state is not written again. On the first use of `state="cluster"`
an existing local state directory becomes the shared state (`state: moved
the local state of 'my-app' (N files) to the cluster`).

## Plan on one runner, apply on another

`piceli deploy … --plan --out deploy-plan.json` writes a **plan file**
(`piceli.deploy-plan-file.v1`): the combined hash, the stages, the `--ref`
commits, the pipeline and observed target identity (kube-system and
namespace UIDs) and the build, delivery and mirror receipts it relied on.
Any runner applies it:

```sh
piceli deploy --apply deploy-plan.json --approve <combined_hash> --json
```

The plan file never widens an approval. `--apply` refuses a hash that is not
the file's (`deploy-plan-file-mismatch`), another pipeline or declared target
(`deploy-plan-file-mismatch`) and another cluster
(`deploy-plan-target-mismatch`); then it plans again against live state and
runs only when the new combined hash equals the approved one
(`pipeline-plan-changed` otherwise). It needs no build cache: an image whose
delivery receipt says it is in the registry (or node) by digest, and that
the registry still has, is not built again.

## Resume anywhere

After a failed or interrupted run, `piceli deploy … --resume` works on any
runner: the run journal, the approved plan and the commits come from the
cluster. When the build finished on the first runner but the delivery did
not, the images exist only there, so the resume builds them again (the same
approved build plan) before delivering.

(state-move)=
## Export, import, move between backends

```sh
piceli state show --spec deploy/app.py:pipeline            # backend, generation, lock holder
piceli state pull --spec deploy/app.py:pipeline            # refresh the working copy
piceli state export --spec deploy/app.py:pipeline --out state.json
piceli state export --spec deploy/app.py:pipeline --out state.json \
  --include-secrets --key-file ~/.config/my-app/state.key  # owner-only key file, ≥ 32 characters
piceli state import --spec deploy/app.py:pipeline --in state.json --key-file … # prints the digest (exit 3)
piceli state import --spec deploy/app.py:pipeline --in state.json --key-file … --approve <digest>
```

- An export (`piceli.state-export.v1`) holds the public part (run journals,
  receipts, catalog, history, check reports) in the clear. The private part
  (secret store, stored discovery with live Secret bodies, execution
  journals, pending plans, backups of replaced objects) is **left out** unless
  `--include-secrets --key-file` encrypts it (AES-256-GCM with an scrypt key,
  bound to the release and namespace; needs `pip install 'piceli[crypto]'`).
- An import replaces the state of the release (shared or local) after an
  approval of its digest. An export without the private part is refused
  (`state-import-partial`) unless `--allow-partial`: the next release would
  generate new secret values.
- To move from `local` to `cluster`, set `state="cluster"` and run any
  command on the runner that has the directory. To move back, export with
  secrets, set `state="local"` and import.

## If it fails

| Output | Meaning | Next step |
| --- | --- | --- |
| `pipeline-locked` / `release-locked` with `lock.holder` | Another runner deploys this release | Wait; a dead runner frees it after `expires_in` seconds |
| `state-lock-lost` | This run's lock was taken over; it stopped writing | `piceli state show`, then `--resume` when the other run ended |
| `state-access-denied` | The kubeconfig lacks the Secret or Lease verbs | Grant them in the namespace (see above) |
| `state-unavailable` | The API server did not answer | Run the command again |
| `state-corrupt` | A `piceli-state-*` Secret was altered | Never edit them; `piceli state import` an export |
| `deploy-plan-target-mismatch` | The plan was made against another cluster | Apply with that kubeconfig, or plan again |

Every code is explained by `piceli explain <code>` and in
{doc}`reference/errors`.
