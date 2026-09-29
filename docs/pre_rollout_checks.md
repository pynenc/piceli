# Pre-rollout checks

```{admonition} Maturity: experimental
:class: note

`App.pre_rollout`, `UpgradeCheck` and the `prerollout` deploy stage are new in
0.9.0 and may change in a minor release, with a changelog entry. See the
{doc}`roadmap` for every feature's status.
```

A release that swaps a workload's image can fail in ways a readiness probe
finds too late: the new binary cannot read the Secret mounted at
`/etc/app`, or it cannot open the store the old version wrote. By then the
pods have already changed. A **pre-rollout check** finds both *before* the
first pod changes: `piceli deploy` runs a short Job with the **new image** and
the workload's **real Secrets, mounts, environment and security context**,
and the release proceeds only if the Job succeeds.

```python
from piceli import App, ClaimTemplate, SecretVolume, UpgradeCheck

app = App("shop")
db = app.stateful_set(
    "db", image=images["db"], ports=[5432], replicas=2,
    env={"TOKEN": credentials.key("token")},
    volumes={
        "/etc/db": SecretVolume("db-keys"),
        "/var/lib/db": ClaimTemplate("data", size="10Gi"),
    },
)
app.pre_rollout(
    db,
    ["db", "check-config"],                                   # new image, real Secrets
    upgrade=UpgradeCheck(["db", "verify", "/var/lib/db"]),    # retained claim, read-only
)
```

Both commands are argv lists run as the container's entrypoint. Exit `0`
passes. A declaration renders no object, so it never changes the plan hash of
a release (`piceli render` output is identical), and an app that declares no
check keeps the six deploy stages it has always had.

## What the check Job is made of

The Job is derived from the workload's *rendered* pod (with the delivered
image), so it cannot drift from the real pods:

| Kept from the workload | Not carried |
| --- | --- |
| the new image, `imagePullPolicy`, working directory | probes, ports, lifecycle hooks |
| `env` and `envFrom`, including `secretKeyRef` and `configMapKeyRef` | init containers and sidecars |
| Secret, ConfigMap, projected, downward API and `emptyDir` volumes, at the same mount paths | the workload's labels and selector: no Service or NetworkPolicy selects a check pod |
| pod and container security context, resources, service account, token automount | claims, for the configuration check (see below) |
| node selector, tolerations, image pull secrets, runtime class, priority class | affinity and anti-affinity rules |

The Job runs `restartPolicy: Never`, `backoffLimit: 0` and
`activeDeadlineSeconds` set to the check's timeout. Its labels are
`piceli.io/pre-rollout`, `piceli.io/pre-rollout-app` and
`piceli.io/pre-rollout-run`.

For the configuration check (`command`), volumes that hold data (claims,
host paths, network volumes) become empty directories: the command checks
configuration, never retained data.

## The upgrade check: the retained volume, read-only

`UpgradeCheck` answers "can the new binary open the store the running one
wrote". Its Job mounts the claims the running workload uses at the same paths
with `readOnly: true` on the mount and on the claim, so the command sees the
real data and cannot change it:

- a StatefulSet's `ClaimTemplate` claims, one Job per ordinal
  (`data-db-0`, `data-db-1`, ...), for the workload's `replicas`;
- an `ExistingClaim`, once.

A claim that does not exist yet (the first release) or is not `Bound` is
skipped and listed under `skipped` in the stage output. `UpgradeCheck(...,
volumes=["/var/lib/db"])` narrows it to some mount paths.

### ReadWriteOnce claims and other nodes

A `ReadWriteOnce` claim can be mounted by several pods **on the same node**,
not on two nodes. When a running pod holds the claim, Piceli pins the check
Job to that pod's node with a `nodeAffinity` on the node name, so the
read-only mount works. If nothing holds the claim, the scheduler places the
Job where the volume can attach.

Limits of that design:

- `ReadWriteOncePod` claims cannot be shared with any second pod. When a
  running pod holds one, planning refuses with `prerollout-claim-exclusive`.
  Use `ReadWriteOnce`, drop the `UpgradeCheck`, or check the store at startup.
- A node-local volume (such as `local-path`) is tied to its node anyway; the
  same pinning applies.
- A read-only mount of a filesystem that needs journal recovery after an
  unclean shutdown may fail to mount; the check then fails as
  `not-startable`.
- If the pinned node is full or cordoned the Job cannot schedule: it fails as
  `not-startable` (`scheduling`) after a short grace, not after the timeout.

## In the plan

`piceli deploy MODULE:ATTR --plan` gains a `prerollout` stage (after `plan`,
before `apply`) **only when the app declares a check**. It lists, per check,
the workload, the commands and timeouts, and every Secret and ConfigMap the
pod reads, marked `source: cluster` (must exist now) or `source: release`
(created or changed by this release). The declaration is covered by the
combined hash.

Planning reads the cluster (never writes) and **refuses** a check that cannot
work, with exit `2` and before any build or delivery:

- `prerollout-mount-missing`: a Secret or ConfigMap the release does not create
  is absent, or lacks a referenced key;
- `prerollout-claim-exclusive`: see above.

## At run time

The stage runs after the release plan and before `apply`:

1. It removes check Jobs of this app that an interrupted run left behind.
2. For every declared check whose workload the release changes, it re-reads the
   mounted objects (a missing external one fails right away), creates the Job,
   waits, records the outcome and **always deletes the Job** (also on
   `Ctrl-C`; `ttlSecondsAfterFinished` covers a deployer that is killed).
3. It stops at the first failing check. Nothing of the release has been applied:
   the pods are unchanged, and `--resume` after the fix runs the stage again.

A release that changes nothing skips the stage (`skipped`, `why: unchanged`);
a workload the release leaves alone is listed under `skipped` with
`workload-unchanged`.

A check fails with one of:

| Code | Meaning |
| --- | --- |
| `prerollout-failed` | the command exited non-zero |
| `prerollout-timeout` | no result within `timeout_seconds` (plus 30 s of slack) |
| `prerollout-not-startable` | the pod could not start: `category` `mount` (Secret or ConfigMap missing or unreadable, volume attach), `image` (bad reference, pull back-off) or `scheduling`. A pod stuck on a mount or scheduling event for 20 s fails at once instead of waiting for the timeout |
| `prerollout-unavailable` | the API refused to create the Job (see RBAC) |

### Secrets that this release creates or changes

The check runs **before** `apply`, so it reads Secrets and ConfigMaps as they
are in the cluster now:

- one the release **creates** does not exist yet: it is marked `optional` for
  the check and listed as `unverified` in the outcome (the check cannot prove
  that mount);
- one the release **changes** is read in its current, older form.

So a first release cannot prove Secrets it creates itself, and a release that
adds a key to a mounted Secret is checked against the old Secret. Objects the
release does not touch (created out of band, or by an earlier release) are
checked exactly as the pods will see them.

## The run summary and events

The stage output (`piceli runs`, the run summary's `stages.prerollout`,
`piceli watch --json` `stage` events) is additive JSON:

```json
{
  "checks": [
    {"workload": "db", "kind": "config", "job": "db-cfg-1a2b3c4d",
     "state": "passed", "seconds": 4.2, "cleaned": true, "exit_code": 0},
    {"workload": "db", "kind": "upgrade", "job": "db-up0-1a2b3c4d",
     "state": "failed", "seconds": 6.0, "exit_code": 1, "cleaned": true,
     "claims": ["data-db-0"], "node": "worker-1",
     "log_tail": "store format 7 is newer than 6"}
  ],
  "skipped": [], "removed_stale": 0
}
```

`piceli watch --json` shows the stage as `{"stage": "prerollout", ...}` and the
progress lines `[prerollout] db: config check running as Job ...`. The
`deploy --json` result lists `prerollout` under `stages`. `--until prerollout`
stops after the stage.

`log_tail` is bounded (the last 30 lines, 300 characters each, 4 000 in all)
and **scrubbed**: the values of every Secret the pod references are replaced,
then `password=...`/`token: ...`/bearer strings, then any long token-like
run. Scrubbing is best effort; do not print secrets from a check command.

## Requirements

The deploying identity needs, in the target namespace: `create`, `get`,
`list` and `delete` on `jobs.batch`; `get` and `list` on `pods`, `pods/log`
and `events`; `get` on `secrets`, `configmaps` and `persistentvolumeclaims`.
`get secrets` is used to read key names and to redact values from logs; if it
is denied, those reads are skipped and the Job itself is the check.

## Limits

- One main container per check; init containers and sidecars are not run.
- Network policies that select the workload's labels do not apply to the check
  pod, so a check that needs a policy-gated peer may not reach it.
- Checks run one after another and stop at the first failure.
- A check runs when the release plan changes the workload; a change that only
  affects an object the workload mounts (a Secret) does not trigger it.

## Testing

`piceli.testing.FakeAPI.job_result(prefix, exit_code=…, logs=…, waiting=…,
events=…)` decides how check Jobs end in acceptance tests; see {doc}`testing`.
