# How plans decide what changes

This page explains how `piceli release plan` decides whether an existing
object is `no-op` or `apply`, and where the field-level diff of a change comes
from.

```{admonition} Maturity: preview
:class: note

The decision rule is stable within a minor release. The `diffs` JSON fields
may gain fields; existing fields keep their meaning. See the {doc}`roadmap`.
```

## The problem: the live object is never the manifest

The API server changes what it stores. A Deployment created from a 20-line
manifest comes back with defaults (`revisionHistoryLimit: 10`, a rolling
update `strategy`, `imagePullPolicy`, `terminationMessagePath`, …), with
canonical values (`cpu: 0.5` is stored as `500m`), with allocated values (a
Service's `clusterIP`) and with controller bookkeeping (a
`deployment.kubernetes.io/revision` annotation, a claim's protection
finalizer). Comparing the desired manifest with the live object field by
field therefore reports a change for every object, even when applying it
would change nothing.

A hand-written list of "fields the server defaults" would be incomplete and
different for every Kubernetes version, CRD and admission webhook. Piceli asks
the API server instead.

## The rule: compare what the server would store

For every desired object that exists and that the release already manages,
`plan` sends the **exact write that `apply` would send**, as a server-side
dry run:

* a JSON merge patch of the desired manifest, by the release's field manager,
  with the owner annotation, and with the observed UID and `resourceVersion`
  as preconditions (the same request the executor sends for an `apply` of an
  object it owns);
* with `dryRun=All`. The API server runs defaulting, validation and admission
  and answers with the object it would store, but persists nothing.

The object is `no-op` when that answer equals the live object, ignoring only
`status`, runtime metadata (`resourceVersion`, `generation`, `managedFields`,
timestamps, `uid`) and Piceli's own `piceli.io/owner`/`piceli.io/operation`
annotations. Otherwise it is `apply`, and the diff between the live object and
the answer is the change: exactly the fields the write would change, with
server defaults on both sides.

Why this request and not a server-side apply: an `apply` of an object this
release owns is a merge patch. A server-side apply dry run would answer a
different question. Fields that Piceli wrote by create or merge patch are
owned by an `Update` entry, so a server-side apply that drops them would keep
them (the object would look unchanged although the executor's write removes
them), and a changed value would conflict with Piceli's own earlier entry.
Dry-running the executor's own request keeps "no-op" equal to "applying
changes nothing".

A `no-op` action is still checked when the release is applied: the executor
re-reads the object and requires that it still contains the declared values,
or still matches the server's answer from planning.

## What is never guessed

* **No evidence, no no-op.** When a dry run is not available (the identity
  lacks the `patch` verb, an admission webhook rejects dry runs, the object
  changed in between, more than 256 objects, the discovery deadline), the
  object is compared literally with the desired manifest, which can only
  report a change that is not there. The plan lists these objects in
  `dry_run_unavailable` with a reason code. An object that changed between
  discovery and its dry run (`conflict`, typically a controller updating
  the status while a rollout finishes) makes the plan capture discovery and
  the dry runs again, up to three times, before it falls back.
* **Secret values are never shown.** Objects with values bound to secret
  versions (and every `Secret`) are not dry-run. They are compared privately
  instead (see below). Their bound fields appear in the diff's
  `not_compared` list, never their values.
* **Unmanaged objects** are never dry-run: planning them requires an explicit
  `--adopt` or `--replace` (see {doc}`release_cli`).

## Secret-bound objects: private comparison

A `Secret`, or any object with a value bound to a secret version, is `no-op`
when its live content already equals the desired content with the secret
versions resolved. The comparison runs in the planning process only:

* the bound versions are resolved from the local secret store and put into
  the desired manifest in memory; the live side is the private discovery
  evidence (never the public discovery JSON);
* both sides are reduced to an HMAC-SHA256 under a random key that exists
  only for that one comparison, and compared in constant time. No value,
  digest or fingerprint is stored, printed, journaled or sent in an event;
  the plan only records the resulting operation, `no-op` or `apply`;
* the rules are the literal ones: a Secret's `stringData` is compared as the
  base64 `data` the server stores, an omitted `type` matches `Opaque`, and
  discovery's defaulted fields are ignored. Anything else that differs (a
  rotated value, an extra key, a changed label) is `apply`;
* only objects this release already manages are compared, and a version that
  cannot be resolved counts as a difference.

This reveals one bit per object, whether it changed, to anyone who can read
the plan; the value itself is never reachable. A new release carries its
secret values over from earlier releases (see {doc}`secrets`), so changing an
image leaves the Secret `no-op`. `release diff` compares privately only when
the composition is unchanged (it never materializes the secret inputs of a new
release), so there the Secret of a changed composition shows as `apply`.

## Removing keys the composition dropped (three-way)

An `apply` of an object the release owns is a JSON merge patch, and a merge
patch only changes the keys it carries: a label or ConfigMap key that the
composition no longer declares would stay on the object. (Lists, such as a
container's `env`, are replaced whole by a merge patch, so a dropped list item
is already removed.)

The plan therefore compares three manifests per object: what earlier releases
of this owner declared (every release in the catalog, merged), the new desired
manifest, and the live object. A map key is removed when:

* an earlier release declared it (a key only some other writer ever added is
  never removed);
* the new desired manifest does not declare it;
* it is present on the live object; and
* no other field manager owns it in `metadata.managedFields` (the status
  subresource aside). A key that `kubectl edit` or a controller has since
  written is left alone.

The removals are part of the action (`removes`, a list of JSON pointers, in
`release plan --json` and in the plan hash), turn an otherwise unchanged
object into `apply`, and appear in the diff as `remove` changes. The executor
sends them as explicit `null`s in its merge patch, which deletes exactly those
keys. Within `metadata`, only `labels` and `annotations` keys are removed, and
never Piceli's own annotations. Retained objects (PVCs, Secrets, …) are never
rewritten, so they get no removals.

The release names whose declarations were used are stored with the plan, so
`apply --approve <hash>` rebuilds exactly the same removals.

## Evidence, hashes and purity

The dry-run answers are captured together with discovery and stored in the
private discovery evidence of the plan (never in the public discovery JSON).
`build_plan` stays a pure function of the composition, that evidence and the
authorization: `apply --approve <hash>` rebuilds the plan from the stored
evidence and must reproduce the approved hash.

* An answer is used only for the desired manifest it was made for (by digest)
  and the `resourceVersion` it was made against.
* The answers are part of the snapshot hash when present, so a snapshot
  without them keeps the hash it had before.
* The diff is derived from the plan and the snapshot. It is not part of the
  plan hash: the same state always gives the same plan hash, and the diff can
  change presentation without invalidating approvals.

## Reading a diff

```{figure} _static/img/release-plan-diff.png
:alt: A terminal shows piceli release plan for the example release after three edits. It lists 2 apply and 2 no-op; under apply ConfigMap/web-config the field /data/greeting changes from "hello from piceli" to "hello again from piceli", and under apply Deployment/web /spec/replicas changes from 1 to 2 and the container image changes between two shortened nginx digests, shown before and after on separate lines. The secrets are carried over and a plan hash is printed. Below, piceli release diff prints the same changes as unified YAML diffs between the live objects and the release, with server defaults such as revisionHistoryLimit and imagePullPolicy on both sides.
:width: 100%

`release plan` summarizes the changed fields of each `apply`; `release diff`
prints them as unified diffs against the server's dry-run answer
(`examples/release` with a new image, `greeting` and `replicas`).
```

Each changed object in `release plan --json` (and in `release diff`) has:

| Field | Meaning |
| --- | --- |
| `changes` | `{path, op, before, after}` per field: `path` is a JSON pointer, `op` is `add`, `remove` or `replace`; lists are compared by position |
| `unified` | the same change as a unified diff of the object in YAML |
| `basis` | `server-dry-run` (the "after" side is the server's answer) or `client` (desired manifest merged locally; no server defaults) |
| `not_compared` | JSON pointers bound to secret versions (compared privately, never shown) |

Values are redacted with the same rules as public plans: a secret value is
shown as `"<redacted>"`.

## Safety

The dry runs never change the cluster: every request carries `dryRun=All`,
which the API server honours for `PATCH` (its options are query parameters).
This was verified on kind: the `resourceVersion` of every object is unchanged
after `plan` and `diff`. `plan` and `diff` remain the commands an agent may run
without asking (see {doc}`agents`).

(interrupted-executions)=
## Interrupted apply and rollback

The executor journals every action before and after its write, and every
write is preconditioned on the object's UID and `resourceVersion` (a create
on the object's absence). An execution can therefore be killed at any step
and the next command converges:

| Interrupted | Recovery |
| --- | --- |
| `apply` of a new release | `piceli release resume` continues the same execution (same grant, same operation IDs). |
| `apply` re-applying an existing release, or `rollback` | not resumable: run the same `apply`/`rollback` again; it re-plans against the live state. |

At each step the resume decides from the journal and the live object:

* **before the write reached the object** (a create whose object is still
  absent, or a write whose object still has the recorded version): the write
  is sent again, exactly as planned, once `[execution] write_settle_seconds`
  (default 60) have passed since it was sent. A request already on its way
  can still reach the API server after its client died; until then the
  resume stops as `blocked` (`ambiguous-write-blocked`,
  `ambiguous-content-blocked` or `ambiguous-delete-blocked`) and can simply
  be run again;
* **after the server applied it** but before Piceli recorded the answer: the
  live object carries the action's operation ID (or contains the declared
  content), so the action counts as applied without a second write;
* **after the receipt** (during readiness): readiness is checked again.

Anything else (the object changed in between in a way that does not contain
the planned content) stops as `blocked` with an `ambiguous-*` code instead of
guessing; plan again. A second `resume` of a finished execution writes
nothing.

This is tested by killing `piceli release apply` (a SIGKILL of its process)
at every write of an apply and of a rollback, before the write, after the
server applied it and at the next request
(`tests/acceptance/test_kill_resume.py`), and mid-rollout on kind
(`tests/integration/test_kill_kind.py`).

(rollback-boundary)=
## What a rollback restores, and what it cannot

`piceli release rollback <release>` re-plans that release's archived
composition against the live cluster and applies the difference, exactly like
an `apply` (see {doc}`release_cli`). It restores what the release declares:

* every declared field of every object the release declares: images, command,
  environment, ConfigMap data, labels, annotations, resources, `replicas`
  (unless an autoscaler owns it, see {doc}`compatibility`);
* declared objects that were deleted since (they are created again);
* with `prune = true`, the removal of objects a later release added.

It reuses the release's own secret versions; nothing is regenerated.

It **cannot** restore:

* **Data.** Volumes, databases and object stores keep what the newer version
  wrote; a schema migration is not reversed. PersistentVolumeClaims,
  PersistentVolumes, Namespaces and Secrets are retained objects that a
  rollback never deletes or rewrites (see {doc}`secrets` for rotating a
  Secret).
* **External side effects** of the newer version: messages sent, jobs that
  ran, calls to other services, DNS or cloud resources created outside the
  cluster, images pushed to a registry.
* **What others own**: fields other field managers wrote (an autoscaler's
  replica count, an operator's fields and `status`, webhook injections),
  objects outside the release, and objects that controllers create from the
  release's objects (Pods, ReplicaSets, Jobs' Pods), which follow their owner
  again only as their controller reconciles.
* **Replaced objects**: a `replace` action is not undone (see
  {doc}`release_cli`).

Roll back application data with the application's own tools (backups,
reversible migrations) before or after the Piceli rollback.
