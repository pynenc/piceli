# The same app in Piceli, Helm, Kustomize, cdk8s and Pulumi

This page takes one small application and writes it five times: with Piceli,
as a Helm chart, as Kustomize bases and overlays, with cdk8s (Python) and with
Pulumi (Python, Kubernetes provider). A test renders every version for `dev`,
`staging` and `prod` and checks that all five produce the same Kubernetes
objects. The page then compares size, the steps to deploy, change and roll
back, and the safety features, and says where the other tools are the better
choice.

```{admonition} Maturity: preview
:class: note

The examples and the equality test are part of the test suite. The numbers
below are checked by a test, so they change only together with the examples.
Piceli is pre-alpha; Helm, Kustomize, cdk8s and Pulumi are mature projects
with far larger user bases (see [Where the others are better](#where-the-others-are-better)).
```

## The app

A slice of the {doc}`reference app <reference_app>`: two web servers (`web`
and `api`, busybox serving a ConfigMap), their Services, an autoscaler and a
disruption budget for `web`, one NetworkPolicy that lets only the app's pods
talk to each other, and restricted pods (non-root, read-only root file
system, no API token). Nine objects per environment. The environments
differ in four ways:

| | dev | staging | prod |
| --- | --- | --- | --- |
| Namespace | `webapp-dev` | `webapp-staging` | `webapp-prod` |
| `LOG_LEVEL` (ConfigMap `settings`) | `debug` | `info` | `warning` |
| `api` replicas | 1 | 1 | 3 |
| `web` autoscaler (min–max) | 1–2 | 2–6 | 3–10 |
| Requests and memory limits | default | default | larger for `web` and `api` |

The five versions, each written the way its tool's documentation suggests:

| Tool | Version | Files |
| --- | --- | --- |
| Piceli | this release | {src}`examples/comparisons/piceli/app.py` |
| Helm | 4.3.0 | {src}`examples/comparisons/helm/webapp/` (chart, `values.yaml`, `values-<env>.yaml`) |
| Kustomize | 5.8.1 | {src}`examples/comparisons/kustomize/` (`base/`, `overlays/<env>/`) |
| cdk8s | 2.70.104, `cdk8s-plus-34` 2.0.60 | {src}`examples/comparisons/cdk8s/main.py` |
| Pulumi | 3.264.0, `pulumi-kubernetes` 4.34.2 | {src}`examples/comparisons/pulumi/` (`__main__.py`, `Pulumi.<env>.yaml`) |

The tool versions are pinned in the CI job `comparisons` of
{src}`.github/workflows/ci.yml` and in each example's `requirements.txt`.

### How the equality is tested

{src}`tests/unit/comparisons/test_comparisons.py` renders each version and
compares it with `piceli render --env <env>`: the same objects (kind, name,
namespace), the same labels, the same `data` and the same `spec`, field by
field. Nothing contacts a cluster:

- Helm: `helm template webapp examples/comparisons/helm/webapp --namespace webapp-<env> --values values-<env>.yaml`;
- Kustomize: `kustomize build examples/comparisons/kustomize/overlays/<env>`
  (or `kubectl kustomize`);
- cdk8s: `python main.py` synthesizes `dist/webapp-<env>.k8s.yaml`;
- Pulumi: the program runs against Pulumi's own unit-test mocks
  ([`pulumi.runtime.set_mocks`](https://www.pulumi.com/docs/iac/concepts/testing/unit/),
  {src}`tests/unit/comparisons/pulumi_render.py`) with the stack's
  `Pulumi.<env>.yaml` as configuration, and the test compares the inputs of
  every Kubernetes resource.

Locally each tool's test is skipped when the tool is missing; the CI job sets
`PICELI_COMPARISONS=require`, so there a missing tool fails. To run them:

```bash
uv run --with-requirements examples/comparisons/cdk8s/requirements.txt \
       --with-requirements examples/comparisons/pulumi/requirements.txt \
       pytest tests/unit/comparisons -v     # plus helm and kustomize on PATH
```

## Size

Counted by `test_docs_table_counts_files_and_lines` in
{src}`tests/unit/comparisons/test_comparisons.py`: every file that makes up
the app (dependency pins excluded), non-blank lines that are not comments or
Python docstrings.

| Measured | Piceli | Helm | Kustomize | cdk8s | Pulumi |
| --- | --- | --- | --- | --- | --- |
| Files | 1 | 10 | 9 | 2 | 5 |
| Lines of configuration | 66 | 246 | 295 | 214 | 238 |

What the numbers mean, and what they do not:

- Piceli's typed `App` fills in what the others spell out: selectors and
  labels, the Service's target port, the pod security settings on every pod
  (`PodDefaults`), the Deployment's initial replicas from the autoscaler.
  The others write every field of the Kubernetes API object.
- The Helm and Kustomize versions repeat the pod template (Helm moves the
  shared part into `_helpers.tpl`); cdk8s and Pulumi share it through a
  Python function, as Piceli does internally.
- cdk8s and Pulumi use their low-level typed classes here so that the output
  is identical. [cdk8s+](https://cdk8s.io/docs/latest/plus/) offers
  higher-level constructs that are shorter, but they add their own defaults
  (for example on security contexts and resources), so the output would
  differ from the others. Pulumi also accepts plain dictionaries instead of
  `…Args` classes, which is shorter and less typed.
- Line counts measure effort to read and review, not quality. A long Helm
  chart can be the right call when it is what your platform team already
  reviews.

## Steps to deploy, change and roll back

For `prod`, from a checkout with the tool installed and an explicit
kubeconfig.

| | Piceli | Helm | Kustomize | cdk8s | Pulumi |
| --- | --- | --- | --- | --- | --- |
| Preview | `piceli deploy app.py:pipeline --env prod --plan` (prints a hash) | `helm template …`, or the [helm-diff](https://github.com/databus23/helm-diff) plugin against the live release | [`kubectl diff -k overlays/prod`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_diff/) | `cdk8s synth`, then `kubectl diff -f dist/webapp-prod.k8s.yaml` | [`pulumi preview --stack prod`](https://www.pulumi.com/docs/iac/cli/commands/pulumi_preview/) |
| Deploy | `piceli deploy … --approve <hash>` | [`helm upgrade --install webapp ./webapp -n webapp-prod -f values-prod.yaml`](https://helm.sh/docs/helm/helm_upgrade/) | [`kubectl apply -k overlays/prod`](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/) | `kubectl apply -f dist/webapp-prod.k8s.yaml` | [`pulumi up --stack prod`](https://www.pulumi.com/docs/iac/cli/commands/pulumi_up/) (previews, then asks) |
| Change | Edit `app.py`; plan, then approve the new hash | Edit values or templates; `helm upgrade` | Edit a patch; `kubectl apply -k` | Edit `main.py`; synth and apply | Edit code or stack config; `pulumi up` |
| Roll back | `piceli release rollback previous --spec app.py:pipeline --env prod`, then `--approve <hash>` | [`helm rollback webapp <revision>`](https://helm.sh/docs/helm/helm_rollback/) | Revert in Git and apply again ([`kubectl rollout undo`](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#rolling-back-a-deployment) for a single Deployment) | Revert in Git, synth and apply again | Revert in Git and `pulumi up` again |
| Objects removed from the code | Left in the cluster by default; a release spec with `[release] prune = true` ({doc}`release_cli`) deletes them, never Namespaces, PersistentVolumes, claims or Secrets ({src}`test_prune_is_conditional_orphan_delete_and_retention_is_permanent <tests/acceptance/test_local_executor.py>`) | Deleted by `helm upgrade` | Left in the cluster unless you use [`kubectl apply --prune`](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/declarative-config/) | Left in the cluster unless the applier prunes | Deleted by `pulumi up` |

Piceli's rows are exercised by
{src}`test_plan_changed_and_approval_required <tests/acceptance/test_deploy_pipeline.py>`,
{src}`test_deploy_rerun_is_noop_and_one_change_moves_one_deployment <tests/acceptance/test_deploy_pipeline.py>`,
{src}`test_reference_dev_deploys_changes_one_object_and_rolls_back <tests/acceptance/test_reference_app_fake_api.py>`
and {src}`test_prune_and_rollback_of_every_kind <tests/acceptance/test_release_kinds.py>`.

## Safety features

Each Piceli cell links the test that proves it; each cell for another tool
links that tool's documentation.

| | Piceli | Helm | Kustomize (+ `kubectl`) | cdk8s | Pulumi |
| --- | --- | --- | --- | --- | --- |
| Apply exactly what was reviewed | The approval is the plan's hash; a plan that changed since is refused with `pipeline-plan-changed` ({src}`test_plan_changed_and_approval_required <tests/acceptance/test_deploy_pipeline.py>`) | No binding between a preview and the upgrade | No binding between `kubectl diff` and `kubectl apply` | Same as the applier it hands the YAML to | Interactive confirmation of the preview; [update plans](https://www.pulumi.com/docs/iac/concepts/update-plans/) (`--save-plan`, `up --plan`) constrain `up` to a saved preview (plan format documented as experimental) |
| Field ownership | Server-side apply with UID and resourceVersion preconditions; a field another manager owns is a conflict, not an overwrite ({src}`test_update_meeting_a_foreign_manager_still_conflicts <tests/acceptance/test_adoption.py>`) | Helm 4 supports [server-side apply](https://github.com/helm/helm/releases/tag/v4.0.0) | [`kubectl apply --server-side`](https://kubernetes.io/docs/reference/using-api/server-side-apply/) on request; client-side apply by default | Depends on the applier | [Server-side apply by default](https://www.pulumi.com/registry/packages/kubernetes/how-to-guides/managing-resources-with-server-side-apply/) in provider v4 |
| Drift | Every plan reads the live cluster; a `kubectl edit` of a declared field shows as drift ({src}`test_later_kubectl_edit_shows_as_drift <tests/acceptance/test_adoption.py>`) | Not built in; the helm-diff plugin can compare with live objects | [`kubectl diff`](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_diff/) | `kubectl diff` | [`pulumi refresh`](https://www.pulumi.com/docs/iac/cli/commands/pulumi_refresh/) and `preview --refresh` |
| Existing objects | Never changed until adopted explicitly in a plan ({src}`test_unauthorized_adoption_is_refused_before_any_write <tests/acceptance/test_adoption.py>`, {src}`test_cli_adoption_is_planned_bound_and_applied <tests/acceptance/test_release_adopt_cli.py>`); `piceli import live` writes a typed module for them ({src}`test_imported_namespace_is_all_no_op_after_adoption <tests/acceptance/test_import_live.py>`) | Refuses objects it did not create unless they carry its release metadata ([`helm install`](https://helm.sh/docs/helm/helm_install/)) | `kubectl apply` updates an existing object of the same name | Depends on the applier | [`import`](https://www.pulumi.com/docs/iac/concepts/options/import/) option or [`pulumi import`](https://www.pulumi.com/docs/iac/cli/commands/pulumi_import/); with server-side apply, a create of an existing object updates it ("upsert") |
| Secrets | Values are read or generated at plan time into a private store; plans, logs and output carry digests only ({src}`test_sensitive_values_are_redacted_in_changes_and_unified_diff <tests/unit/ops/test_field_diff.py>`, {src}`test_reference_dev_deploys_changes_one_object_and_rolls_back <tests/acceptance/test_reference_app_fake_api.py>`); SOPS, Vault and AWS sources ({doc}`secrets`) | Values are plain text; release records, including rendered Secrets, are [stored as Secrets](https://helm.sh/docs/topics/advanced/) in the namespace; plugins such as [helm-secrets](https://github.com/jkroepke/helm-secrets) | [`secretGenerator`](https://kubectl.docs.kubernetes.io/references/kustomize/kustomization/secretgenerator/) from plain files; plugins such as [KSOPS](https://github.com/viaduct-ai/kustomize-sops) | Values end up in the synthesized YAML | [Encrypted secrets](https://www.pulumi.com/docs/iac/concepts/secrets/) in stack configuration and state |
| Roll back | A planned, approved rollback to any recorded release, and automatically when post-deploy checks fail ({src}`test_failed_checks_roll_back_to_the_previous_release <tests/acceptance/test_deploy_pipeline.py>`) | [`helm rollback`](https://helm.sh/docs/helm/helm_rollback/) to any revision | Git revert and apply | Git revert, synth and apply | Git revert and `pulumi up` |
| Interrupted runs | A journal; `--resume` continues where it stopped, even after `SIGKILL` at any write ({src}`test_killed_apply_resumes_to_the_release <tests/acceptance/test_kill_resume.py>`) | Re-run `helm upgrade`; a release left `pending-*` needs a [`helm rollback`](https://helm.sh/docs/helm/helm_rollback/) first | Re-run `kubectl apply` (idempotent) | Re-run the apply | Pending operations are recorded in the state and must be resolved ([`pulumi refresh`](https://www.pulumi.com/docs/iac/cli/commands/pulumi_refresh/)) |
| Typed validation before any cluster call | Pydantic models validate each declaration and each environment override ({src}`test_probe_validation <tests/unit/app/test_model.py>`, {src}`test_values_are_type_checked_at_declaration <tests/unit/app/test_environments.py>`) | Optional [`values.schema.json`](https://helm.sh/docs/topics/charts/); templates are text | None until the API server (or a separate validator) | Typed classes for every Kubernetes field | Typed `…Args` classes for every Kubernetes field |
| Cluster selection | Only an explicit kubeconfig file and context; never the current context ({src}`test_named_context_is_used_never_current_context <tests/unit/cli/test_cluster_access.py>`) | The current context unless `--kube-context` | The current context unless `--context` | Depends on the applier | The ambient kubeconfig unless the provider sets one (the example sets [`kubeconfig` and `context`](https://www.pulumi.com/registry/packages/kubernetes/api-docs/provider/)) |
| Autoscaled replicas | The autoscaler keeps `spec.replicas`; a re-apply never resets it ({src}`test_autoscaler_takes_replicas_without_diff_drift_or_reset <tests/acceptance/test_autoscaled_replicas.py>`) | The chart should omit `replicas` when an HPA is used ([Kubernetes docs](https://kubernetes.io/docs/tasks/run-application/horizontal-pod-autoscale/)); these examples set it to match Piceli's output | Same | Same | Same |

## Where the others are better

Piceli is the youngest tool here, and for many teams one of the others is
the right choice today:

- **Ecosystem.** Most vendors ship a Helm chart, and
  [Artifact Hub](https://artifacthub.io/) lists thousands of them. Piceli has
  no package format yet (the {doc}`roadmap` lists reusable packages as not
  started); to run a vendor's chart with Piceli you render it with
  `helm template` and load the YAML ({ref}`faq-existing-yaml`), which loses
  `helm upgrade` for that chart.
- **Maturity.** [Helm](https://www.cncf.io/projects/helm/) is a graduated
  CNCF project, Kustomize is built into `kubectl`, and Pulumi and cdk8s have
  years of production use. Piceli is pre-alpha: APIs change between releases
  (see the {doc}`changelog`).
- **GitOps controllers.** [Argo CD](https://argo-cd.readthedocs.io/en/stable/user-guide/application_sources/)
  and Flux ([Helm](https://fluxcd.io/flux/components/helm/),
  [Kustomize](https://fluxcd.io/flux/components/kustomize/)) reconcile Helm
  charts and Kustomize overlays from Git continuously, across many clusters,
  with a UI. Piceli applies on request (from a laptop or CI); continuous
  reconciliation is on the {doc}`roadmap`, not available. You can still feed
  `piceli render` output to those controllers.
- **More than Kubernetes.** Pulumi manages cloud resources (databases, DNS,
  buckets, clusters) in the same program and state as the Kubernetes
  objects, with policy as code and many languages. Piceli covers Kubernetes
  only.
- **Secrets at rest.** Pulumi encrypts secrets in its configuration and
  state by default; Piceli keeps generated values in a private local store
  (or, with `state="cluster"`, in Secrets of the release namespace) and
  relies on SOPS, Vault or AWS for encrypted sources.
- **Removing objects.** Helm and Pulumi delete what you remove from the code
  by default; Piceli prunes only from a release spec with `prune = true`.
- **Languages and people.** Helm and Kustomize need no programming language;
  cdk8s and Pulumi support several. Piceli is Python only.

See {doc}`when_to_use` for a short decision guide.
