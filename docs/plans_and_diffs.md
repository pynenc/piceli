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
  `dry_run_unavailable` with a reason code.
* **Secret values are never compared.** Objects with values bound to secret
  versions (and every `Secret`) are not dry-run and always plan as `apply`.
  Their bound fields appear in the diff's `not_compared` list, never their
  values.
* **Unmanaged objects** are never dry-run: planning them requires an explicit
  `--adopt` or `--replace` (see {doc}`release_cli`).

A map key that the composition no longer declares (a label, a ConfigMap key)
is not removed by an `apply` today, because a merge patch only sets the keys
it carries. The server's answer keeps the key, so the plan shows no change for
it. Remove such a key with a one-off edit, or replace the object.

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

Each changed object in `release plan --json` (and in `release diff`) has:

| Field | Meaning |
| --- | --- |
| `changes` | `{path, op, before, after}` per field: `path` is a JSON pointer, `op` is `add`, `remove` or `replace`; lists are compared by position |
| `unified` | the same change as a unified diff of the object in YAML |
| `basis` | `server-dry-run` (the "after" side is the server's answer) or `client` (desired manifest merged locally; no server defaults) |
| `not_compared` | JSON pointers bound to secret versions |

Values are redacted with the same rules as public plans: a secret value is
shown as `"<redacted>"`.

## Safety

The dry runs never change the cluster: every request carries `dryRun=All`,
which the API server honours for `PATCH` (its options are query parameters).
This was verified on kind: the `resourceVersion` of every object is unchanged
after `plan` and `diff`. `plan` and `diff` remain the commands an agent may run
without asking (see {doc}`agents`).
