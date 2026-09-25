# Custom resources and any other kind

This page shows how to declare objects the typed `App` has no call for, such
as an operator's custom resources (a cert-manager `Certificate`, a Prometheus
`ServiceMonitor`), with a typed spec generated from the operator's
CustomResourceDefinition (CRD). The spec is validated when you declare it,
renders with the Kubernetes field names and is planned, applied, pruned and
rolled back like every other object of the app.

```{admonition} Maturity: preview
:class: note

`app.resource(...)` and `piceli codegen crd` are new in 0.7.0. Names and
options may still change before 1.0; the generated modules depend only on
pydantic.
```

## Prerequisites

- Piceli 0.7.0 or later.
- The operator's CRD: its manifest file (operators publish one per release),
  or a cluster where it is installed and a kubeconfig context that may read
  `customresourcedefinitions`.
- The operator and its CRDs are installed outside the app. An app never
  creates, changes or deletes a CRD, a Namespace or a PersistentVolume.

## Steps

1. **Generate the models.** From the CRD file:

   ```text
   piceli codegen crd cert-manager.crds.yaml --crd certificates.cert-manager.io \
       --out deploy/crds/certificate.py
   ```

   or from a cluster, through an explicit kubeconfig and context (one read
   of the CRD, nothing is written to the cluster):

   ```text
   piceli codegen crd --from-cluster --kubeconfig cluster.kubeconfig \
       --context my-cluster --crd servicemonitors.monitoring.coreos.com \
       --out deploy/crds/service_monitor.py
   ```

   `--crd` takes the CRD's name (`plural.group`) or its kind; it is needed
   when the file holds several CRDs. `--version` picks a version (default:
   the storage version). Without `--out` the module is printed.

2. **Declare the resource.** The generated module has `API_VERSION`, `KIND`
   and `SCOPE` constants and a `<Kind>Spec` model:

   ```{literalinclude} ../examples/environments/app.py
   :language: python
   :start-at: tls = app.resource(
   :end-before: app.environment("dev"
   ```

   Fields are snake_case (`secret_name`); the Kubernetes names
   (`secretName`) are accepted too, and nested objects can be models or
   mappings. A misspelt field, a wrong enum value or a missing required field
   raises a pydantic `ValidationError` at declaration.

3. **Render and release it** like any app: `piceli render
   deploy/app.py:app`, then `piceli release plan` or `piceli deploy
   --plan` (see {doc}`typed_apps` and {doc}`deploy`).

### Expected output

`piceli codegen crd --out FILE` prints one JSON summary on stdout:

```json
{"api_version": "cert-manager.io/v1", "classes": 15, "crd": "certificates.cert-manager.io",
 "generator": "piceli-codegen-crd/1", "kind": "Certificate", "out": "deploy/crds/certificate.py",
 "schema_sha256": "sha256:…", "scope": "namespaced", "source_sha256": "sha256:…",
 "spec_class": "CertificateSpec", "state": "generated", "version": "v1"}
```

The declared Certificate renders as:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  labels:
    app.kubernetes.io/part-of: shop
  name: shop-tls
  namespace: shop-prod
spec:
  dnsNames:
  - shop.example.com
  issuerRef:
    kind: ClusterIssuer
    name: letsencrypt
  secretName: shop-tls
```

Only the fields you set are rendered. Schema defaults are not copied into
the model: the API server applies them, so Piceli never claims ownership of
fields you did not declare.

### If it fails

| Reason | Meaning | Fix |
| --- | --- | --- |
| `crd-invalid` | Not an `apiextensions.k8s.io/v1` CRD, or the version has no schema | Pass the CRD manifest the operator publishes; check `--version`. |
| `crd-not-found` | `--crd` names no CRD in the file or cluster, or the file holds several and `--crd` is missing | Pass `--crd plural.group` or the kind. |
| `codegen-output-refused` | `--out` exists and Piceli did not generate it, or its directory is missing | Choose another path, or `--force`. |
| `codegen-cluster-read-failed` | The API server refused or failed the read | Check the context's permission to get CRDs. |
| `resource-scope-mismatch` (at plan time) | The declared scope contradicts the server's discovery | Use the scope the CRD states (the generated `SCOPE`). |
| `execution-refused` "requires private version references" | A field whose name looks like a secret holds a literal value | Put the value in a Secret and reference it, or declare the field `public=` if it is not secret material. |

## The generated module

`piceli codegen crd` is a small generator for Kubernetes *structural*
schemas (every CRD version served by `apiextensions.k8s.io/v1` has one), not
a general OpenAPI tool:

- **Deterministic.** Properties are sorted and class names come from the
  field path (`CertificateSpecPrivateKey`), with no timestamps or tool
  versions, so the same schema always generates the same file, from a file or
  from a cluster. `SCHEMA_SHA256` identifies the schema; `GENERATOR` the
  output format. Regenerate after upgrading the operator and review the diff.
- **Kubernetes extensions.** `x-kubernetes-int-or-string` becomes
  `int | str`; `x-kubernetes-preserve-unknown-fields` objects accept unknown
  fields (others refuse them); maps become `dict[str, …]`; `nullable` fields
  accept `None`; enums become `Literal[...]`; `minimum`, `maximum`,
  `minLength`, `maxLength`, `pattern`, `minItems` and `maxItems` become field
  constraints (a pattern Python cannot compile is left out).
- **Names.** A field named like a Python keyword gets a trailing underscore
  (`class_`); one in pydantic's `model_` namespace gets an `f_` prefix; the
  Kubernetes name is always its alias.
- **Formatting.** The module starts with `# ruff: noqa`; keep generated files
  out of your formatter (as `extend-exclude`), so regenerating stays a clean
  diff.
- **No new dependency.** The module imports only pydantic (and the standard
  library); Piceli needs no extra for code generation. General JSON-schema
  generators do not know the Kubernetes extensions above and their output
  changes between versions, so Piceli ships its own.

## Rules the model enforces

### Scope

`app.resource(api_version, kind, name, spec, *, fields=None, scope=None,
public=(), labels=None, annotations=None, component=None)` declares one
object:

- `scope` defaults to the generated spec's `SCOPE`, to `cluster` for built-in
  cluster-scoped kinds (`PriorityClass`, `StorageClass`, webhooks, …), and to
  `namespaced` otherwise. At plan time the release compares it with the API
  server's discovery and refuses a contradiction (`resource-scope-mismatch`).
- A generated spec also checks its identity: using a `CertificateSpec` with
  another `kind` or `apiVersion` is a `ValidationError`.
- `fields` sets other top-level keys for kinds whose body is not under
  `spec` (`fields={"value": 1000}` for a PriorityClass). `apiVersion`,
  `kind`, `metadata`, `spec` and `status` cannot be set there.

### Cluster-scoped objects are per namespace

A cluster-scoped resource (a `ClusterIssuer`, a `PriorityClass`) follows the
rules of the app's ClusterRoles (see {doc}`typed_apps`):

- it renders without a namespace and annotated
  `piceli.io/namespace: <release namespace>`;
- only the release in that namespace (with its owner) manages, changes or
  prunes it; an object of the same name managed from another namespace is
  refused with `resource-requires-adoption` until adopted explicitly;
- plans flag it with `"cluster_scoped": true` (`[cluster-scoped]` in the
  text);
- `Namespace`, `CustomResourceDefinition` and `PersistentVolume` are refused:
  they belong to the cluster's administration.

Names are not prefixed: when the same app is deployed to several namespaces
of one cluster, give each namespace's cluster-scoped objects their own names
(for example with an {doc}`environment <environments>` override).

### Secret references and redacted fields

Plans never show secret values: fields whose names look sensitive
(`password`, `token`, `secret`, `privateKey`, …) are redacted, and a release
refuses to apply a redacted literal. References are recognized and shown:
`secretName` and other `*Name`/`*File`/`*Path` strings, `{name, key}`
selectors (`bearerTokenSecret`, `passwordSecretRef`) and metadata templates
(`secretTemplate`). A field that looks sensitive but holds no secret material,
such as a Certificate's `privateKey` *settings*, is declared with `public=`:

```python
app.resource(
    certificate.API_VERSION,
    certificate.KIND,
    "shop-tls",
    certificate.CertificateSpec(
        secret_name="shop-tls",
        issuer_ref={"name": "letsencrypt", "kind": "ClusterIssuer"},
        private_key={"algorithm": "ECDSA", "size": 256},
    ),
    public=["spec.privateKey"],
)
```

`public` renders as the `piceli.io/public-fields` annotation, so a reviewer
sees it in the plan. It never applies to a Secret's data. Real secret values
belong in `app.secret(...)` and are referenced by name.

### Readiness

A release waits for each object to be ready. A custom resource is ready when
its `Ready` condition is `True` for the current generation; one whose
controller reports no `Ready` condition is ready once applied. A Certificate
that cannot be issued therefore fails the release after
`readiness_seconds`, like a Deployment that never becomes ready.

### Overrides and environments

`app.override(resource, patch)` patches the rendered object like any other,
and an {doc}`environment <environments>` replaces a resource's spec with
`specs={"shop-tls": …}` (validated into the same generated model).

## `piceli codegen crd` contract

```text
piceli codegen crd [FILE] [--crd NAME] [--version V] [--out FILE] [--force] [--json]
piceli codegen crd --from-cluster --kubeconfig F --context C --crd NAME
                   [--transport https|loopback-http] [--allow-exec] [--exec-sha256 S] [...]
```

| | |
| --- | --- |
| Input | A CRD file (YAML or JSON, several documents or a `List` allowed), or one CRD read from a cluster with `--from-cluster` (explicit `--kubeconfig` and `--context`; exec credential plugins only with `--allow-exec`). |
| Output | Without `--out`: the module on stdout (`--json`: one object with the module as `module`). With `--out`: the file, and one JSON summary on stdout. |
| Side effects | `--out` writes one file and replaces it only when Piceli generated it (or with `--force`). `--from-cluster` sends one GET. |
| Retry | Always safe: the output depends only on the schema. |
| Approval | None. |
| Exit codes | `0` generated, `2` rejected (`crd-invalid`, `crd-not-found`, `codegen-flags-conflict`, `codegen-output-refused`, `codegen-cluster-read-failed`, `target-refused`). |

## A complete example

`examples/environments/` renders a Deployment, a Service, a ConfigMap, a
cert-manager `Certificate` and a Prometheus `ServiceMonitor` for three
environments. Its models in `examples/environments/crds/` were generated
from the CRDs vendored in `tests/fixtures/crds/` (cert-manager v1.16.2 and
prometheus-operator v0.78.2, Apache-2.0; each file records its source URL),
and a test regenerates them byte for byte.
