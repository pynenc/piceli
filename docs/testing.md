# Test your deployment code against a fake Kubernetes API

This page shows how to run your own tests of code that drives Piceli (a
composition, a release spec, a script around `PlanExecutor`) against the
in-process fake Kubernetes API server that Piceli's own acceptance suite uses,
with no cluster.

```{admonition} Maturity: preview
:class: note

`piceli.testing` is public and covered by semantic versioning from 0.4.0: the
names listed below change only in a minor release with a changelog entry. The
server is a test double, not a conformance-tested API server (see
[What it does not do](#what-it-does-not-do)).
```

## Prerequisites

- Piceli installed (`pip install piceli`); `piceli.testing` ships in the wheel.
- `pytest` for the fixture (the context manager needs nothing else).

## Steps

1. **Start a fake cluster in a test.** `fake_cluster()` serves a fresh
   `FakeAPI` on a loopback port and gives you a ready provider:

   ```python
   from piceli.testing import fake_cluster, manifest
   from piceli.k8s.ops.discovery import ResourceIdentity


   def test_settings_are_readable():
       with fake_cluster() as cluster:
           cluster.api.put(manifest("ConfigMap", "settings", value="on"))
           found = cluster.provider.get(
               ResourceIdentity("v1", "ConfigMap", cluster.namespace, "settings")
           )
           assert found.manifest["data"] == {"mode": "on"}
   ```

   Or enable the ready-made fixture in your `conftest.py`:

   ```python
   pytest_plugins = ["piceli.testing.pytest_plugin"]


   def test_with_fixture(piceli_fake_cluster):
       piceli_fake_cluster.api.put(manifest("ConfigMap", "settings"))
   ```

2. **Run commands against it through an explicit kubeconfig.** Write a
   kubeconfig for the server and use the `loopback-http` transport, which
   Piceli accepts only for a literal loopback `http://` address:

   ```python
   kubeconfig = cluster.kubeconfig(tmp_path / "kubeconfig")  # context "fake"
   ```

   ```toml
   # release.toml
   [target]
   kubeconfig = "kubeconfig"
   context = "fake"
   namespace = "piceli-test"          # piceli.testing.TARGET.namespace
   transport = "loopback-http"
   ```

   `piceli import live` takes `--transport loopback-http` for the same
   purpose.

3. **Check what the server received and holds.** `cluster.api.objects` maps
   `(kind, name)` to the stored manifest; `cluster.api.requests` records every
   request (method, path, query, body, content type).

4. **Inject faults.** `cluster.api.inject("PATCH", "/deployments/web",
   status=503)` fails the next matching request once. Other keys: `delay`,
   `after_commit_delay`, `disconnect_before`, `disconnect_after`, `raw` (a
   response body) and `dry_run` (match only dry-run or only real requests).
   To stop a client at an exact step, set `cluster.api.intercept` to a
   function `(request, phase) -> bool`; it is called with `phase`
   `"received"` (before the server acts) and `"committed"` (after it acted,
   before the response), and returning `True` drops the connection there.
   Piceli's own kill tests use it to SIGKILL `piceli release apply` at every
   write.

5. **Model field ownership when you test adoption.** Set
   `cluster.api.field_ownership = True` and seed objects with the field
   managers that created them:

   ```python
   cluster.api.field_ownership = True
   cluster.api.put(deployment, managers=[("kubectl-create", "Update", deployment)])
   ```

   The server then tracks `managedFields`, reports server-side apply
   conflicts and prunes fields a manager stops applying.
   `cluster.api.scale("Deployment", "web", 3)` writes `spec.replicas` the way
   a HorizontalPodAutoscaler does (a `scale` subresource entry of
   `kube-controller-manager`).

### Expected result

Tests run in milliseconds, use the real `kubernetes` client transport and
never read `~/.kube/config`.

### If it fails

- **`403` for a namespaced object.** Every namespaced object lives in one
  namespace, `TARGET.namespace` (`piceli-test`) unless you pass
  `FakeAPI(namespace="my-app")` (0.8.0); requests for another namespace are
  refused.
- **`404` for a kind.** The server serves the kinds in `TYPES`. Pass
  `FakeAPI(types={...})` (plural → `(apiVersion, kind, namespaced)`) and
  `fake_cluster(api)` to serve others.
- **`422` on a patch.** Without `field_ownership`, server-side apply requests
  must set `force=false`, and adoption (a field-manager transfer) needs
  `field_ownership = True`.
- **`loopback-http is only for a literal loopback http:// test API`.** The
  kubeconfig must point at the fake's `http://127.0.0.1:<port>` URL.

## API

Importing `piceli.testing` has no side effects: it starts no server, opens no
socket and reads no kubeconfig. The names are loaded on first use.

| Name | What it is |
| --- | --- |
| `fake_cluster(api=None, **provider_settings)` | Context manager: runs a server and yields a `FakeCluster(api, url, provider)`; `cluster.namespace`, `cluster.kubeconfig(path, context="fake")` |
| `serve(api=None)` | Context manager: yields `(api, url)` for a running server |
| `provider_at(url, **kwargs)` | A `KubernetesProvider` bound to `TARGET` (field manager `piceli-acceptance`, owner `acceptance-owner`) |
| `write_kubeconfig(url, path, context="fake")` | A credential-free kubeconfig for the server |
| `FakeAPI(types=None, *, namespace=TARGET.namespace)` | The server state (`namespace`, 0.8.0: the one namespace it serves, so an app that names its own namespace runs unchanged): `objects`, `requests`, `put(...)`, `inject(...)`, `intercept`, `scale(kind, name, replicas)`, `managers(kind, name)`, `field_ownership`, `ready`, `wait_for_first_consumer`, `terminating_reads` (an `Orphan` delete lingers for that many reads), `fail_pods(workload, reason=…, exit_code=…, restarts=…, logs=…, message=…, events=…)` (0.8.0: with `ready = False` the workload gets a current ReplicaSet and a failing pod, with its log at `pods/NAME/log` and Warning events at `events`; for testing the `apply-crashloop` diagnosis), `pod_logs`, `events` |
| `TARGET` | The `PlanTarget` every server represents (`acceptance-cluster`, `piceli-test`) |
| `TYPES` | The default served kinds: ConfigMap, Secret, Service, Pod, PersistentVolumeClaim, PersistentVolume, Namespace, Deployment, ReplicaSet (0.8.0), NetworkPolicy, ServiceAccount, Role, RoleBinding, ClusterRole, ClusterRoleBinding, a test `Widget` and (0.6.0) `coordination.k8s.io/v1` Lease, for shared state (`state="cluster"`) |
| `manifest(kind, name, value=...)` | A minimal valid object for seeding |
| `field_paths`, `fields_v1`, `paths_of`, `value_at` | Helpers for `managedFields` (FieldsV1) paths |
| `piceli.testing.pytest_plugin` | The `piceli_fake_cluster` fixture |

## What it does not do

- No admission, no defaulting beyond status (Deployments report ready
  replicas, PVCs bind, Services get a cluster IP), no watch, only
  equality and existence label selectors on lists (`k`, `!k`, `k=v`,
  `k!=v`), no Pods created from Deployments (except the failing pods of
  `fail_pods`).
- One namespace (`FakeAPI(namespace=...)`) and one cluster identity (`kube-system` UID `cluster-uid`,
  namespace UID `namespace-uid`).
- It exists to test Piceli's clients, not Kubernetes semantics: keep one
  integration test against a disposable `kind` cluster for anything that
  depends on the real API server.
