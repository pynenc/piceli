# Restore points of retained data

This page shows how `piceli deploy` takes a verified copy of the data a
release could damage before it changes a stateful workload, and how to put
that copy back with `piceli restore`.

```{admonition} Maturity: preview
:class: note

`RestorePoints`, `Quiesce`, `piceli restore-points` and `piceli restore` are
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
    "db", image=images["db"], replicas=2,
    volumes={"/var/lib/db": ClaimTemplate("data", size="10Gi")},
)
cache = app.deployment(
    "cache", image="docker.io/library/redis:7.4@sha256:…",
    volumes={"/data": ExistingClaim("cache-state")},
)
app.quiesce(cache, Quiesce.exec(["redis-cli", "SAVE"]))

pipeline = Pipeline(app, target, build=images, deliver=NodeLoopbackRegistry(),
                    restore_points=RestorePoints())
```

The run gains a `backup` stage between `deliver` and `plan`. A pipeline
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

`RestorePoints(directory=None, image=None, timeout_seconds=600, run_as_user=0)`:

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
