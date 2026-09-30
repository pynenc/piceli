# Restore points of retained data

This page shows how `piceli deploy` takes a verified copy of the data a
release could damage before it changes a stateful workload, and how to put
that copy back with `piceli restore`.

```{admonition} Maturity: preview
:class: note

`RestorePoints`, `Quiesce`, `RestoreVerify`, `piceli restore-points` and
`piceli restore` (with `--to-new-claim`) are
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

`RestorePoints(directory=None, image=None, timeout_seconds=600, run_as_user=0, include="touched")`:

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
