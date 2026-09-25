# Compatibility and shared ownership

What Piceli supports, and what it guarantees when other writers (autoscalers,
operators, admission webhooks, other tools) share the objects a release
manages. Every guarantee on this page is an integration test that runs on a
real [kind](https://kind.sigs.k8s.io/) cluster for each supported Kubernetes
version.

(supported-kubernetes-versions)=
## Supported Kubernetes versions

Piceli supports the **four most recent Kubernetes minor versions**:

| Kubernetes | Tested on (kind node) | When |
| --- | --- | --- |
| 1.37 | `kindest/node:v1.37.0` | every pull request and push, and nightly |
| 1.36 | `kindest/node:v1.36.4` | nightly |
| 1.35 | `kindest/node:v1.35.8` | nightly |
| 1.34 | `kindest/node:v1.34.11` | nightly |

The node images are pinned by digest in
[`.github/kind-nodes.json`](https://github.com/pynenc/piceli/blob/main/.github/kind-nodes.json)
(kind v0.33.0); CI runs the whole integration suite on them. When a new
Kubernetes minor is released, a Piceli minor release adds it and drops the
oldest one, with a changelog line.

Older clusters are likely to work: Piceli uses only stable API machinery
(discovery, server-side apply, JSON merge patch with UID and
`resourceVersion` preconditions, `dryRun=All`, `managedFields`). They are
not tested. A managed Kubernetes service (GKE, EKS, AKS, …) is supported when
its version is in the range; its extra admission webhooks behave like any
other webhook (below).

A read the API server throttles (`429`, from API priority and fairness or a
watch cache that is still initializing, as for a CRD installed a moment
before) is sent again after its `Retry-After`, like client-go does; writes
are not.

The `kubernetes` Python client is required at `>=29.0.0`. Piceli uses it for
configuration loading and the HTTP transport, not for typed API models, so its
version does not have to match the cluster's. The nightly run also tests the
lowest allowed client.

## Who owns what

A release **owns the objects** it declares (the `piceli.io/owner`
annotation) but only **the fields it declares**. Kubernetes records which
field manager wrote each field (`metadata.managedFields`); Piceli reads that
record to decide what it may change:

* An update of an object the release owns is a JSON merge patch with the
  observed UID and `resourceVersion` as preconditions. It writes exactly the
  declared fields and never touches a field it does not carry.
* A key an earlier release declared and the new one dropped is removed only
  when no other field manager owns it (see {doc}`plans_and_diffs`).
* `release plan` reports as `drift` every declared field that another
  manager also owns, with the manager's name.

### Autoscalers own `spec.replicas`

A HorizontalPodAutoscaler changes `spec.replicas` of its target through the
`scale` subresource. Declaring the field in a release would reset the count
on every apply and show a diff forever, so Piceli leaves the field to the
autoscaler. For each workload an autoscaler targets (declared in the release,
or live and covered by discovery), the plan chooses one mode:

| Mode | When | What the plan declares |
| --- | --- | --- |
| `initial` | the workload does not exist yet | the declared `replicas` (the initial size) |
| `held` | it exists and no autoscaler has written the field yet | the **live** value: nothing changes and the field is never removed |
| `yielded` | the autoscaler (a `scale` subresource or controller entry) owns the field | nothing: `replicas` is left out of the write |

In every mode a plan also never removes `/spec/replicas` from an autoscaled
workload, even when an earlier release declared it and Piceli still co-owns
it: removing it would reset the count to the server default.

This is the one rule for typed apps and for plain manifests. `app.autoscaler`
cooperates with it: the targeted workload refuses `replicas=` and renders the
autoscaler's `min_replicas` as its `spec.replicas`, which is only the
`initial` size (a new workload starts at the autoscaler's minimum instead of
one pod). An environment that changes `min_replicas` changes that initial
size; for a workload that exists the plan keeps the live count and the
autoscaler applies its new minimum. A plain manifest without `replicas`
starts at the server default (1).

`release plan --json` lists them under `autoscaled`:

```json
"autoscaled": [{"resource": {"api_version": "apps/v1", "kind": "Deployment",
                "namespace": "shop", "name": "web"},
               "field": "/spec/replicas",
               "autoscalers": ["HorizontalPodAutoscaler/web"],
               "mode": "yielded"}]
```

A new image, a rollback or a re-plan of the same release therefore neither
resets the count nor shows a `replicas` diff, and an autoscaler that scales
the workload (through the `scale` subresource) while Piceli waits for it to
become ready is not reported as drift. An autoscaler created by
another tool counts when discovery covers its kind: add
`"autoscaling/v2/HorizontalPodAutoscaler"` to `[discovery] kinds`. To stop
autoscaling, remove the autoscaler from the release (with `prune = true`) or
delete it; the declared `replicas` then applies again.

Tested by `tests/integration/test_ownership_hpa_kind.py`: a release adds an
autoscaler to a running Deployment, the real controller scales it, and a
re-plan, a new image and a rollback all keep its count.

### Operators and controllers writing other fields

An operator commonly writes fields of a custom resource the release declares
(defaults, a `status`, labels). Piceli keeps every field it does not declare
across new releases and rollbacks, and never reports them as drift.

When an operator writes a field the release **does** declare, the plan shows
it as `drift` naming the operator and as a change in the diff; applying the
plan writes the declared value back (it is the release's field), so an
operator that keeps writing it will write it back too. Do not declare fields
a controller manages; leave them out of the release.

Tested by `tests/integration/test_ownership_operator_kind.py` (a CRD, and an
operator-like writer with its own field manager for `spec` and `status`).

(readiness-rules)=
### When an object is ready

A release waits for every object it writes to be ready, in dependency order,
with one rule per kind:

1. **Kinds with a specific rule**: Deployments, StatefulSets and DaemonSets
   (rolled out, observed generation, updated and available pods), Jobs
   (complete), Services (a LoadBalancer needs an ingress address),
   PersistentVolumeClaims (bound), and so on.
2. **Kinds that need another controller to act are ready once they exist**:
   ConfigMaps, Secrets, ServiceAccounts, RBAC, NetworkPolicies and CronJobs,
   and HorizontalPodAutoscalers (they need metrics), PodDisruptionBudgets,
   Ingresses and HTTPRoutes (they need an ingress or Gateway controller).
   Their status is not waited for, so a cluster without a metrics server or
   an ingress controller still completes the release.
3. **Every other kind**, custom resources and built-in kinds alike, follows
   the common status conventions (as in `kstatus`): not ready while
   `status.observedGeneration` is behind `metadata.generation`, while a
   `Reconciling` or `Stalled` condition is `True`, or while a `Ready`
   condition is not `True`. An object that reports no such status is ready
   once applied; a malformed `status` is `readiness-unsupported`.

A custom resource whose controller can never make it ready (a Certificate
that cannot be issued) therefore fails the release after
`readiness_seconds`, like a Deployment that never becomes ready.

### Mutating admission webhooks

A mutating webhook (a sidecar or policy injector, a defaulting webhook) may
change what Piceli writes. The write goes through it like any other client's,
and the plan compares against the server's dry run of the write, which runs
the webhook too: a re-plan of the same release is `no-op` and a change shows
only its own fields. The webhook must support dry run (`sideEffects: None` or
`NoneOnDryRun`); otherwise the object is listed in `dry_run_unavailable` and
planned with a literal comparison, which can only over-report changes.

Tested by `tests/integration/test_ownership_webhook_kind.py`: a real in-cluster
webhook adds an annotation and an environment variable to every Deployment.
(A webhook is used rather than a MutatingAdmissionPolicy because it works on
every supported minor without feature gates.)

### Adoption with other field managers present

Adopting an existing object (`--adopt`) transfers the fields of **client**
managers (`kubectl`, other tools' `Apply` or `Update` entries) to the release,
then applies the release's manifest, so a client-written field the release
does not declare is removed. **Controllers** (a manager named
`kube-controller-manager`, `…-controller` or `…-controller-manager`, the
kubelet, the scheduler) and every subresource entry (`status`, `scale`) are
kept: their fields survive the adoption. The plan lists the transferred
managers per object (`adoption.transferred_managers`).

### Pruning and field removal

With `prune = true`, an object an earlier release managed and the new one no
longer declares is deleted **as a whole**, including fields other managers
wrote to it. Mark an object `piceli.io/retained: "true"` to keep it (retained
objects are never deleted or rewritten). A dropped map key, by contrast, is
removed only when no other manager owns it.

Adoption and pruning are tested by
`tests/integration/test_ownership_third_manager_kind.py`.

## Interrupted executions

An `apply` or a `rollback` can stop at any step (the process is killed, the
machine restarts). The next command converges; see
{ref}`interrupted-executions` and {ref}`rollback-boundary` for what a rollback
restores and what it cannot.
