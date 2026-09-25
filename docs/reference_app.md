# A realistic app: dev, staging and prod from one typed module

This page walks through `examples/reference/app.py`, a complete application
described in one typed Python module and deployed to three environments: web
and api Deployments with an autoscaler and a disruption budget, a Redis
StatefulSet with its own volume per pod, a Job and a CronJob, a Gateway API
HTTPRoute, a cert-manager `Certificate`, a network policy, a ServiceAccount
with RBAC, restricted pods, a password from a SOPS file and post-deploy
checks. There is no YAML, no manifest dict and no `app.override(...)` in it:
every object and every difference between environments is typed and checked
when it is declared.

```{admonition} Maturity: preview
:class: note

The example uses preview features (typed apps, environments, custom
resources, external secret sources). It is tested on every change: the
three environments are rendered and compared in unit tests, `dev` is deployed
to a fake API server in acceptance tests and to a `kind` cluster in CI.
```

## Prerequisites

- Piceli 0.7.0 or later, `kubectl`, and [`sops`](https://github.com/getsops/sops)
  on `PATH` (to decrypt the store password; rendering does not need it).
- To deploy `dev`: a disposable `kind` cluster, the Gateway API CRDs and the
  cert-manager `Certificate` CRD (no controller is needed: the HTTPRoute and
  the Certificate are only stored). Nothing is built: the images are small
  public images pinned by digest.

```{warning}
`examples/reference/secrets/example-only.agekey` is a **public** age key,
committed so that the example and its tests run offline. Never use it, or
anything it decrypts, for real secrets: create your own key with
`age-keygen`, keep it out of version control and encrypt your own files for it.
```

## Steps

1. **Declare the app once, with defaults for every pod.** `PodDefaults` puts
   the Kubernetes `restricted` Pod Security settings and a read-only root
   filesystem on every container, and keeps API tokens out of pods that are
   not bound to a declared account:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: app = App(
   :end-before: "# --------------------------------------------------------------- secrets"
   ```

2. **Take the store password from a SOPS file.** `Sops(...)` names one value
   of an encrypted file; two templates derive the connection URL and the Redis
   configuration from it. The app only holds opaque references: the values
   are read at plan time into Piceli's private store and written only into
   the Secret (see {ref}`external-secret-sources`):

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: secrets = Secrets(
   :end-before: "# ---------------------------------------------------------- configuration"
   ```

3. **The stateful store.** A StatefulSet with a `ClaimTemplate` gets one
   PersistentVolumeClaim per pod (never pruned by a release) and a headless
   Service named `store`. Redis reads its configuration, password included,
   from the mounted Secret:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: store = app.stateful_set(
   :end-before: "# ---------------------------------------------------------------- workers"
   ```

4. **Workers.** `migrate` is a Job that runs after the store is ready and
   before the api rolls out (`app.depends`); `report` is a nightly CronJob:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: migrate = app.job(
   :end-before: "# -------------------------------------------------------------------- api"
   ```

5. **The api and its permissions.** `app.service_account(...)` declares a
   ServiceAccount, a Role and a RoleBinding from typed `Rule`s; only the pods
   bound to it get a token:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: api_account = app.service_account(
   :end-before: "# -------------------------------------------------------------------- web"
   ```

6. **The web front end.** The autoscaler owns the replica count (`web`
   declares no `replicas=`), and the disruption budget keeps one pod up during
   node maintenance. `access=` tells `piceli status` and `piceli access` how to
   reach it from a laptop:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: web = app.deployment(
   :end-before: "# ---------------------------------------------------------------- traffic"
   ```

7. **Traffic and TLS.** An HTTPRoute attaches to a shared Gateway; an Ingress
   is declared too, in its own component, for local clusters without a
   Gateway controller. The Certificate's spec is a model generated from the
   cert-manager CRD (`piceli codegen crd
   tests/fixtures/crds/cert-manager-certificate.yaml --out
   examples/reference/crds/certificate.py`, see {doc}`crds`), so a typo or a
   wrong enum value fails here. One NetworkPolicy lets only this release's pods
   reach its pods:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: app.http_route(
   :end-before: "# ----------------------------------------------------------- environments"
   ```

8. **Environments name only what differs.** `dev` logs more and scales less;
   `staging` and `prod` use their own hosts and issuers and leave out the
   Ingress; `prod` gets more replicas, bigger containers and wider
   autoscaler bounds (`Scaling`). Every key is checked against the declared
   objects when the environment is declared:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: def certificate(
   :end-before: "# ------------------------------------------------------------------ deploy"
   ```

9. **One target per environment, secrets and checks.** The pipeline deploys
   each environment to its own cluster and namespace, with its own state. The
   checks run after every deploy: the web page answers, and the store holds
   the schema version the Job wrote. A failed check rolls the release back:

   ```{literalinclude} ../examples/reference/app.py
   :language: python
   :start-at: pipeline = Pipeline(
   ```

10. **Render and compare the environments** (no cluster, no secret values):

    ```text
    piceli render examples/reference/app.py:app --env prod
    piceli render examples/reference/app.py:app --env staging --diff-env prod
    ```

11. **Deploy `dev` to a disposable kind cluster.** The `dev` target reads
    `REFERENCE_KUBECONFIG`, `REFERENCE_CONTEXT` and `REFERENCE_NAMESPACE`
    (defaults: `examples/reference/reference.kubeconfig`, `kind-reference`,
    `reference-dev`); it never uses `~/.kube/config` or the current context:

    ```text
    kind create cluster --name reference --kubeconfig examples/reference/reference.kubeconfig
    export KC="--kubeconfig examples/reference/reference.kubeconfig --context kind-reference"
    kubectl $KC apply --server-side -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml
    kubectl $KC apply --server-side -f tests/fixtures/crds/cert-manager-certificate.yaml
    kubectl $KC create namespace reference-dev
    export SOPS_AGE_KEY_FILE=$PWD/examples/reference/secrets/example-only.agekey

    piceli deploy examples/reference/app.py:pipeline --env dev --plan
    piceli deploy examples/reference/app.py:pipeline --env dev --approve <combined hash>
    piceli status examples/reference/app.py:dev
    piceli access examples/reference/app.py:dev      # http://127.0.0.1:18080/
    ```

12. **Change one value and deploy again.** Raise `dev`'s
    `Scaling(max_replicas=2)` to `3` and plan: only
    `HorizontalPodAutoscaler/web` changes. Deploy it, then roll back to the
    previous release:

    ```text
    piceli release plan --spec examples/reference/app.py:pipeline --env dev
    piceli deploy examples/reference/app.py:pipeline --env dev --plan
    piceli deploy examples/reference/app.py:pipeline --env dev --approve <combined hash>
    piceli release rollback previous --spec examples/reference/app.py:pipeline --env dev
    piceli release rollback previous --spec examples/reference/app.py:pipeline --env dev --approve <plan hash>
    ```

### Expected output

`--diff-env` prints only what differs between staging and prod: the override
values, then each changed object field by field. The namespace, which also
appears inside the RoleBinding's subjects, is reported once and is not a
difference:

```text
environments: staging (a) -> prod (b)
namespace: reference-staging -> reference-prod
values:
    autoscalers.web: (absent) -> {"max_replicas": 10, "min_replicas": 3}
    config.settings: (absent) -> {"LOG_LEVEL": "warning"}
    hosts.HTTPRoute/site: ["staging.reference.example.com"] -> ["reference.example.com", "www.reference.example.com"]
    …
~ HorizontalPodAutoscaler/web (component web):
    spec.maxReplicas: 6 -> 10
    spec.minReplicas: 2 -> 3
…
6 changed, 0 only in staging, 0 only in prod, 13 identical
```

The first `dev` deploy creates 20 objects, waits for the store, the Job and
the Deployments in dependency order, then runs the checks:

```text
[plan] release reference-… (reapply): 20 create
[apply] reference-…: waiting for StatefulSet/store to be ready (1s)
[apply] reference-…: waiting for Job/migrate to be ready (1s)
[checks] reference-…: passed
deploy ready: release reference-…
```

### If it fails

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `secret-source-tool-missing` | `sops` is not on `PATH` | Install `sops`. |
| `secret-source-auth-failed` | `sops` found no key for the file | Export `SOPS_AGE_KEY_FILE` as an absolute path (the pipeline passes only that variable to `sops`). |
| `discovery-incomplete` (`cert-manager.io/v1/Certificate: api-unavailable`) | The cert-manager or Gateway API CRDs are missing | Install them (step 11). |
| `pipeline-checks-failed`, `rolled-back` | A check failed after the apply; the previous release was re-applied | Read `checks.results` in the run and fix the app. |
| `namespace-not-found` | Targets never create namespaces | `kubectl create namespace reference-dev` with the same kubeconfig and context. |

## What the example shows, and where to read more

| In the example | Page |
| --- | --- |
| `App`, `PodDefaults`, `Security.restricted`, workloads, Services, RBAC, network policies, autoscalers, disruption budgets, routes | {doc}`typed_apps` |
| `app.resource(...)` with a spec generated by `piceli codegen crd` | {doc}`crds` |
| `app.environment(...)`, `Scaling`, `--env`, `--diff-env`, one target per environment | {doc}`environments` |
| `Sops`, `Template`, secrets that are never printed | {doc}`secrets` |
| `Checks.http`, `Checks.exec`, rollback on failed checks | {doc}`checks` |
| `piceli deploy`, `piceli release rollback`, `piceli status` | {doc}`deploy`, {doc}`access` |

Limits worth knowing:

- A `NetworkPolicy` from `app.network_policy` admits pods of the same
  namespace only; a Gateway or ingress controller in another namespace needs
  a policy the typed model does not declare yet.
- `Pipeline(secrets=...)` is shared by every environment: here all three read
  the same SOPS file. Per-environment files need one pipeline module per
  environment for now.
- `busybox` serves static files as stand-ins for the web and api servers;
  replace the images and commands with your own.
