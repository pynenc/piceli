# Restore points of retained data

This page shows how `piceli deploy` takes a verified copy of the data a
release could damage before it changes a stateful workload, and how to put
that copy back with `piceli restore`.

```{admonition} Maturity: preview
:class: note

`RestorePoints`, `Quiesce`, `RestoreVerify`, `piceli restore-points`,
`piceli restore` (with `--to-new-claim`) and growing or moving a claim
(`ExistingClaim(expand_to=...)`, `migrate_from=`) are
**preview**: tested end to end on `kind`, but names, options and JSON fields
may still change in a minor release (always with a changelog entry).
```

A release that changes the image of a database, or the claim templates of a
StatefulSet, can rewrite what its volumes hold (a new storage format, a
migration). Piceli never deletes a PersistentVolumeClaim, but it cannot undo
what a new version writes into one. A *restore point* is a copy of every
retained claim such a release touches, taken while nothing writes to it, and
checked before the release goes on.

## Turn it on

```python
from piceli import App, ClaimTemplate, ExistingClaim, Pipeline, Quiesce, RestorePoints

app = App("shop")
db = app.stateful_set(
    "db",
    image=images["db"],
    replicas=2,
    volumes={"/var/lib/db": ClaimTemplate("data", size="10Gi")},
)
cache = app.deployment(
    "cache",
    image="docker.io/library/redis:7.4@sha256:…",
    volumes={"/data": ExistingClaim("cache-state")},
)
app.quiesce(cache, Quiesce.exec(["redis-cli", "SAVE"]))

pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=NodeLoopbackRegistry(),
    restore_points=RestorePoints(),
)
```

The run gains a `backup` stage between `deliver` and `plan`. When the app
also declares a pre-rollout check ({doc}`pre_rollout_checks`), the
`prerollout` stage runs first (`deliver`, `prerollout`, `backup`, `plan`):
no writer is stopped until the new image has passed its checks. A pipeline
without `restore_points` has no `backup` stage, and its plans and hashes are
unchanged.

## Which claims, and why

At `--plan`, the `backup` stage compares every workload the release applies
with the live one. A workload *changes* when:

- a container or init container image differs (a build image that is not
  delivered yet counts as a change, and the stage is planned again after
  delivery);
- its claim templates (size, storage class, access modes) or its claim mounts
  differ;
- it is new and mounts a claim that already exists.

A changed workload touches every existing, bound claim it mounts writably:
for a StatefulSet each claim of its templates, `<template>-<name>-<ordinal>`,
one per replica (also replicas scaled away), and every `ExistingClaim`. A
read-only mount is not touched. The plan shows each claim with the reason,
and each writer that will be stopped:

```text
  backup   claim data-db-0 of StatefulSet/db: image of container db changes
  backup   claim data-db-1 of StatefulSet/db: image of container db changes
  backup   stop StatefulSet/db (2 replica(s)) until its pods are gone
```

The claims, the writers, their quiesce hooks and the `RestorePoints` settings
are part of the combined hash. At run time the stage is planned again with the
delivered images: it may cover fewer claims than approved (an image turned
out unchanged), never more (`restore-point-plan-changed`; plan again). When
no changed workload writes a claim, the stage is `skipped`.

## What the backup stage does

1. **Quiesce hooks.** Each hook declared with `app.quiesce(workload, ...)`
   runs in every running pod of that workload: `Quiesce.exec(command)` runs
   a command (no shell; it must exit 0), `Quiesce.http(path, port)` sends a
   request through the API server's pod proxy (it must answer `expect`,
   default 200). Output is never printed or stored.
2. **Stop every writer.** Every Deployment or StatefulSet of the release that
   mounts a touched claim writably is scaled to zero through the `scale`
   subresource. A DaemonSet, Job, CronJob or undeclared workload that writes
   the claim cannot be stopped: the plan is refused
   (`restore-point-writer-unsupported`).
3. **Wait until the writers' pods are gone.** Not until they are unready:
   until no pod in the namespace, running or terminating, mounts a touched
   claim writably, and each writer reports zero replicas
   (`restore-point-writers-remain` after `timeout_seconds`).
4. **Copy each claim.** A helper Job mounts the claim read-only and sleeps;
   Piceli streams `tar -czf - .` from it over `pods/exec` into
   `<directory>/<restore point id>/<nn>-<claim>.tar.gz` (a private file,
   hashed while it streams), then asks the helper for the claim's *content
   digest* (the SHA-256 of `"<sha256>  ./<path>"` lines of every file,
   sorted) and deletes the Job.
5. **Verify.** The archive is read back: its SHA-256 must match, every member
   must be a safe relative path, and the content digest computed from the
   archive must equal the one computed from the claim.
6. **Start again.** Writers whose replicas the release declares stay stopped:
   the apply starts them with the new release (the old version never starts
   on the copied data). Autoscaled writers are started again right after the
   copy. When the run ends before its apply (failure, interruption,
   `--until backup` or `--until plan`), every writer still at zero is
   started again with its previous replica count.

The release plan is made after the stage, from the stopped state, and the
result and the run summary name the restore point:

```text
{"event": "result", "state": "ready", "restore_point": "rp-20260929t101500z-3fa2c1", …}
```

`summary.json` gets `restore_point` (id, directory, and per claim its
workload, size and SHA-256) and `summary.md` a "Restore point" section.

## Settings

`RestorePoints(directory=None, image=None, timeout_seconds=600, run_as_user=0, include="touched", max_claim_bytes=None, over_limit="fail")`:

- `include`: `"touched"` covers the claims of the workloads the release
  changes; `"all"` also covers, whenever there is a restore point, every other
  existing claim the app's Deployments and StatefulSets write (their writers
  stop too), so the restore point holds the whole app at one moment.
- `directory`: where archives go, relative to the declaring file. Default:
  `<state_dir>/restore-points`. It stays on the machine that runs Piceli:
  `piceli cache prune` never removes it and shared state
  (`state="cluster"`) never copies it. Copy it somewhere safe yourself.
- `image`: the helper's image, pinned by digest. It needs `sh`, `tar`,
  `gzip`, `find`, `sort`, `sha256sum`, `head` and `sleep` (busybox has them).
  Default: each writer's current image, which is already on the node, so
  nothing new is pulled; set it when that image lacks the tools (distroless).
- `timeout_seconds`: bound for each wait and each copy.
- `max_claim_bytes` (0.17.0): a size guard, off by default. Before any
  quiesce hook runs or any writer stops, a read-only helper measures each
  claim next to its running writer (`du`, in bytes of the claim, not of the
  gzip archive). A claim holding more is handled by `over_limit`:
  `"fail"` (default) fails the stage with nothing stopped
  (`restore-point-claim-too-large`); `"skip"` leaves the claim out of the
  restore point with a warning (`skipped_claims` in the backup stage's
  output), and a writer whose every claim is left out is not stopped. When
  every claim is left out, no restore point is taken. Measuring needs a
  helper pod next to the running writer: storage that attaches a claim to a
  single pod (`ReadWriteOncePod`) cannot be measured before the writers stop.
- `run_as_user`: the helper's user. `0` (default) reads files of any owner
  and restores their ownership; the helper gets only the capabilities
  `CHOWN`, `DAC_OVERRIDE`, `DAC_READ_SEARCH`, `FOWNER` and `FSETID`, no
  service account token and no privilege escalation. A namespace that
  enforces the `restricted` pod security level refuses it.

The helper follows the writer's `nodeSelector`, tolerations and pull
secrets; a claim bound to one node (a local-path volume) pulls it there. The
Kubernetes user running Piceli needs `create`/`delete` on Jobs, `list` on
pods and claims, `patch` on the writers' `scale` subresource, and `create` on
`pods/exec` (and `pods/proxy` for `Quiesce.http`).

## Turn it off, or set it per environment

`Pipeline(..., restore_points=None)` (the default) takes no restore points:
the run has no `backup` stage and no writer is ever stopped for one.

A composition environment that deploys a pipeline can choose its own
(0.17.0), so test environments skip the backups production keeps:

```python
Environment(
    "main",
    namespace="shop",
    pipeline=pipeline,
    follow={app_repo: "main"},
    restore_points="all",
)
Environment.per_branch(
    "wp-*",
    namespace="shop-{branch}",
    pipeline=pipeline,
    follow={app_repo: "{branch}"},
    restore_points="off",
)
```

- `"off"`: none (refused when the app grows or moves a claim, which only the
  backup stage does);
- `"touched"` / `"all"`: the pipeline's `RestorePoints` (or the defaults)
  with that `include`;
- a `RestorePoints(...)`: these settings for this environment;
- not set: the pipeline's `restore_points`.

## Attempts and retries

Every attempt writes a new restore point (`rp-<time>-<hex>`); a retry never
writes into an earlier attempt's directory. One process at a time takes a
restore point into a directory (`restore-point-busy` otherwise, before
anything stops). A point whose process died mid-copy (a killed controller)
is closed by the next attempt: `state: interrupted`, its `.partial` files
removed. Its writers may still be at zero replicas: the replica counts are
written to the record before any writer is scaled, and the next attempt
starts them at those counts. When the backup stage fails, its writers are
started again on the running release before the deploy reports the failure
(and before a GitOps controller backs off); the status' `failure` lists them
(`writers_started`).

## List and verify

```sh
piceli restore-points deploy/app.py:pipeline --verify --json
```

lists the restore points newest first (id, state, claims, sizes, digests)
and with `--verify` reads every archive back (exit `1` when one does not
match; each claim's `check` names the code). It is read-only and never
contacts the cluster.

## Put one back

A restore replaces every file of the claims, so it plans first:

```sh
piceli restore deploy/app.py:pipeline --point rp-20260929t101500z-3fa2c1
```

verifies the archives, checks that the claims exist, lists the writers it
will stop and prints the `restore_hash` (exit `3`). After the owner agrees:

```sh
piceli restore deploy/app.py:pipeline --point rp-… --approve sha256:…
```

It plans again (`restore-plan-changed` on any difference), holds the
pipeline's state lock, stops the writers and waits until their pods are gone,
empties each claim and extracts its archive in a read-write helper Job,
checks the content digest in the cluster and scales the writers back to
their previous replica counts. `--claim NAME` (repeatable) restores only
some claims; `--image` overrides the helper image. A receipt is written to
`<directory>/<id>/restores/`.

To go back to the data *and* the version that wrote it, roll the release
back first (`piceli release rollback`, or a failed check with
`rollback_on_failed_checks`), then restore: the writers restart on the
restored data with the old version.

## Prove a restore point in scratch claims

An archive that verifies is a complete copy; whether the app can open it is
another question. `--to-new-claim` answers it without touching live data:
the restore point goes into new, Piceli-owned scratch claims, and a
read-only command the app declares checks the copy.

Declare the check next to the workload:

```python
from piceli import RestoreVerify

app.restore_verify(db, RestoreVerify(["db", "verify", "--read-only", "/var/lib/db"]))
app.restore_verify(cache, RestoreVerify(["sh", "-c", "test -s /data/dump.rdb"]))
```

`RestoreVerify(command, timeout_seconds=300)`: `command` is an argv run as
the container's entrypoint; exit `0` is `PASS`. A declaration renders no
object and never changes a release's plan hash. Only a Deployment or
StatefulSet that mounts a retained claim can have one.

Then plan and approve:

```sh
piceli restore deploy/app.py:pipeline --point rp-… --to-new-claim
piceli restore deploy/app.py:pipeline --point rp-… --to-new-claim --approve sha256:…
```

The plan (exit `3`) verifies the archives offline and, per restored claim,
names its scratch claim (`piceli-verify-<id suffix>-<nn>`), its storage
class, size and access modes (read from the source claim) and, for a
node-local volume, the node its volume lives on; and per workload with a
declared check, the image, the command and the mount paths. `verify_hash`
covers all of it and `--keep`. With `--approve HASH`, Piceli plans again
(`restore-verify-plan-changed` on any difference), then for each claim:

1. **Create the scratch claim** like the source, labelled
   `app.kubernetes.io/managed-by=piceli`, `piceli.io/scratch=true` and
   `piceli.io/restore-verify=<id>`, with the source's name in the
   `piceli.io/source-claim` annotation.
2. **Restore into it** in a helper Job (the same helper as `piceli
   restore`, pinned to the source volume's node for node-local storage) and
   check the content digest in the cluster (`restore-verify-mismatch`).
3. **Run the check.** One Job per workload replica with the workload's
   *current* image and pod settings (environment, Secret and ConfigMap
   mounts, security context, service account, node selector), the scratch
   copies mounted **read-only** at the paths the workload mounts the
   originals, the declared command as entrypoint. Other claims become empty
   directories. Its output is never printed or stored; the exit code decides.
4. **Delete the scratch claims**, on success, failure and interrupt alike,
   unless `--keep`. Piceli deletes only a claim labelled
   `piceli.io/scratch=true`, with preconditions on its uid; a scratch claim
   that stays is `restore-verify-cleanup-failed` and is named.

Writers are never stopped, no live claim is mounted, and the state lock is
not taken, so it can run beside a deploy. A receipt goes to
`<directory>/<id>/verifies/` and into the result:

```text
{"state": "passed", "verify_hash": "sha256:…", "receipts": [{"point": "rp-…",
 "claims": [{"claim": "data-db-0", "scratch_claim": "piceli-verify-3fa2c1-00",
 "result": "PASS", "verify": "passed", "content_sha256": "…"}, …], …}]}
```

A claim is `FAIL` when it did not restore (`restore-verify-scratch-failed`),
its digest did not match (`restore-verify-mismatch`) or the check did not
exit `0` or could not start (`restore-verify-failed`); the command exits `1`
with `restore-verify-failed`. A claim whose workload declares no check is
`PASS` on its digest alone, with `"verify": "not-declared"`.

`--claim NAME` verifies only some claims; `--image` overrides the helper
image (not the check's, which is always the workload's); `--keep` leaves the
scratch claims for a closer look (delete them with `kubectl delete pvc -l
piceli.io/scratch=true`; the next plan refuses while they exist,
`restore-verify-scratch-exists`). `--all` instead of `--point` plans every
verified restore point under one hash.

The scratch claims need room for a full copy of each restored claim, for as
long as the verify runs. The Kubernetes user needs, besides what `piceli
restore` needs, `create`, `get` and `delete` on PersistentVolumeClaims and
`get` on PersistentVolumes (to pin node-local volumes; without it the
scheduler places the helper).

### On a schedule

Piceli has no scheduler. Run the two steps from CI or a cron job on a
machine that holds the restore point directory, where the owner agreed in
advance to the storage it uses:

```sh
hash=$(piceli restore deploy/app.py:pipeline --all --to-new-claim --json | jq -r .verify_hash)
piceli restore deploy/app.py:pipeline --all --to-new-claim --approve "$hash"
```

Exit `1` means a restore point does not verify: alert on it.

## Grow or move a claim

A claim that fills up needs a larger volume. With `restore_points`, the
deploy's `backup` stage grows a claim or moves its data to a new one, after
the restore point and while every writer is still stopped. Declare the size
the claim should have:

```python
# A StatefulSet: the template's size (claim templates are immutable, so the
# StatefulSet object is recreated; its pods and claims are orphaned and kept).
db = app.stateful_set(
    "db", image=images["db"], replicas=2,
    volumes={"/var/lib/db": ClaimTemplate("data", size="20Gi")},  # was 10Gi
)
pipeline = Pipeline(app, target, restore_points=RestorePoints(),
                    replace=["StatefulSet/db"], ...)

# An existing claim: the size it should have.
cache = app.deployment(
    "cache", image=..., volumes={"/data": ExistingClaim("cache-state", expand_to="4Gi")},
)
```

The plan shows, for each claim, one of two steps:

- **Grow in place** when the claim's StorageClass has `allowVolumeExpansion:
  true`: Piceli patches the claim's requested size and waits until the
  volume reports it, or until only the node-side file system resize is left
  (`FileSystemResizePending`, finished by the kubelet when the pod mounts
  the claim again). A driver that reports the resize infeasible, or no
  growth within `timeout_seconds`, is `claim-expansion-failed`.
- **Move** otherwise. A class without expansion (the local-path
  provisioner, many node-local classes) cannot grow in place, and a claim's
  class cannot change: the plan is refused with `claim-migration-required`
  and the declaration that moves the data. A move goes to a claim with
  another name, so the app names it:

  ```python
  # Each data-db-<n> moves into a new data-2-db-<n> (20Gi).
  volumes = {"/var/lib/db": ClaimTemplate("data-2", size="20Gi", migrate_from="data")}
  # cache-state moves into a new cache-state-2 (8Gi, same class unless given).
  volumes = {
      "/data": ExistingClaim(
          "cache-state-2", size="8Gi", storage_class="fast", migrate_from="cache-state"
      )
  }
  ```

```text
  backup   grow claim data-db-0 of StatefulSet/db from 10Gi to 20Gi (storage class fast allows expansion)
  backup   move claim cache-state of Deployment/cache (512Mi) to new claim cache-state-2 (8Gi, storage class local-path); copy, verify, switch at apply; the old claim is kept
```

A move, after the restore point was taken and verified:

1. **Create the new claim** with the declared size, access modes and class
   (default: the old claim's), labelled `app.kubernetes.io/managed-by=piceli`
   and `piceli.io/migration=target`, with the old claim's name in the
   `piceli.io/migrated-from` annotation.
2. **Copy.** A helper Job mounts the new claim read-write (pinned to the old
   volume's node for node-local storage, so the new volume is made there)
   and extracts the restore point's archive of the old claim into it: the
   copy is exactly the verified restore point, and the old claim is only
   ever mounted read-only.
3. **Verify.** The content digest is computed in the cluster and must equal
   the restore point's (`claim-migration-mismatch`). When the workload
   declares `app.restore_verify(...)`, its command runs in a Job with the
   workload's image and pod settings and the new claim mounted
   **read-only** at the workload's path (`claim-migration-verify-failed`).
4. **Mark the old claim** with `piceli.io/migration=source` and the new
   claim's name in `piceli.io/migrated-to`. It is never deleted: keep it
   for a rollback, and delete it yourself once the new claim has proved
   itself (`kubectl delete pvc NAME`).
5. **Switch.** The release's apply mounts the new claim (a Deployment rolls
   to it; a StatefulSet is recreated through `replace=`, its claims kept)
   and starts the writers, which never ran on the old claim after the copy.

When anything fails before the switch (the copy, the digest, the verify
command), every writer is started again on its **old** claim, the release is
not applied, and the run fails with the code. The new claim stays and is
emptied and filled again by the next run, so a failed move is safe to run
again. Once a workload mounts the new claim, the declaration has nothing
left to do and later plans show nothing.

Choices and limits:

- A size smaller than the claim's is refused (`claim-shrink-refused`); so is
  a move into a smaller claim.
- A new claim name that already exists and was not made by Piceli for that
  move is refused (`claim-migration-target-exists`).
- `size=` and `migrate_from=` need `restore_points`
  (`claim-growth-needs-restore-points`) and a run that reaches the apply
  (`claim-growth-needs-apply` with `--until backup`). A pipeline without
  `restore_points` keeps the old behaviour: a larger `ClaimTemplate` changes
  only the template.
- The copy goes through the API server twice (the restore point, then into
  the new claim), like a restore. For volumes of many gigabytes, prefer a
  class with expansion.
- An autoscaled writer (no declared `replicas`) is started again on its old
  claim after the copy and switched by the apply; what it writes in between
  stays on the old claim.
- The node-side file system resize of an offline-only CSI driver finishes
  when the pod starts; the result says `filesystem-resize-pending`.
- The Kubernetes user needs, besides what restore points need, `patch` and
  `create` on PersistentVolumeClaims, `get` on StorageClasses and
  PersistentVolumes.

## Limits

- The copy is a file-level archive of a stopped volume, not a database dump;
  it is consistent because every writer is gone, not because the database
  was asked.
- Archives are as large as the data (gzip level of the image's `tar`) and
  go through the API server; for large volumes prefer volume snapshots.
- File names with a newline or backslash are archived but may make the
  content digest check fail (`restore-point-checksum-mismatch`).
- Special files (devices, FIFOs) are refused when verifying.
- Nothing is printed from a claim: plans, results, summaries and errors carry
  names, sizes and digests only.
- `--to-new-claim` needs the source claim to still exist (its storage class
  and size shape the scratch claim) and a storage class that provisions
  volumes (`restore-verify-scratch-unsupported` for a static volume). The
  check runs with the workload's current image, not the one that wrote the
  data.
