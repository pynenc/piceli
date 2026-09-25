# When to use Piceli, and when not to

A short decision guide. Each point links the test, example or page behind
it; {doc}`comparisons` shows the same app written with Piceli, Helm,
Kustomize, cdk8s and Pulumi and compares them in detail.

```{admonition} Maturity: preview
:class: note

Piceli is pre-alpha. The guidance below describes this release and changes
as features land; the {doc}`roadmap` lists the maturity of every feature.
```

## Use Piceli when

- **Your team writes Python and wants the application typed.** A typed `App`
  validates every declaration and every per-environment difference before
  anything reaches a cluster ({src}`test_probe_validation <tests/unit/app/test_model.py>`,
  {src}`test_values_are_type_checked_at_declaration <tests/unit/app/test_environments.py>`),
  and dev, staging and prod come from one module without YAML patches
  ({doc}`reference_app`, {src}`examples/reference/app.py`). The
  {doc}`comparison app <comparisons>` takes 66 lines in Piceli against 214 to
  295 in the other four tools.
- **A person (or an agent) must approve exactly what changes.** A plan shows
  every change against the live cluster; only its hash can be approved, and a
  plan that changed since is refused
  ({src}`test_plan_changed_and_approval_required <tests/acceptance/test_deploy_pipeline.py>`).
  The commands, their side effects and approval rules are machine readable
  (`piceli help-json`, {doc}`agents`).
- **You deploy to a cluster you share with other tools or people.** Piceli
  talks only to the kubeconfig file and context you name
  ({src}`test_named_context_is_used_never_current_context <tests/unit/cli/test_cluster_access.py>`),
  never changes an object it does not own until you adopt it
  ({src}`test_unauthorized_adoption_is_refused_before_any_write <tests/acceptance/test_adoption.py>`),
  and reports a `kubectl edit` of a declared field as drift
  ({src}`test_later_kubectl_edit_shows_as_drift <tests/acceptance/test_adoption.py>`).
- **Runs get interrupted.** Every run is journaled: `--resume` continues
  after a crash at any write
  ({src}`test_killed_apply_resumes_to_the_release <tests/acceptance/test_kill_resume.py>`),
  and a failed post-deploy check rolls back automatically
  ({src}`test_failed_checks_roll_back_to_the_previous_release <tests/acceptance/test_deploy_pipeline.py>`).
- **You want source to running release in one command.** `piceli deploy`
  builds pinned images, delivers them by digest, plans, applies and checks,
  and skips unchanged stages ({doc}`deploy`,
  {src}`test_deploy_rerun_is_noop_and_one_change_moves_one_deployment <tests/acceptance/test_deploy_pipeline.py>`).
- **You want to test your deployment code.** `piceli render` needs no
  cluster, and `piceli.testing` is a fake Kubernetes API for your own tests
  ({doc}`testing`); the README quick start runs against it in CI
  ({src}`tests/acceptance/test_readme_quickstart.py`).

## Do not use Piceli (yet) when

- **You need stable APIs.** Piceli is pre-alpha and its APIs change between
  releases ({doc}`changelog`). Helm, Kustomize, cdk8s and Pulumi are mature.
- **You install many third-party packages.** Most vendors ship Helm charts;
  Piceli has no package format ({doc}`roadmap`). Use Helm for those, or
  render a chart with `helm template` and load the YAML
  ({ref}`faq-existing-yaml`).
- **You run GitOps.** Argo CD and Flux reconcile Helm and Kustomize from Git
  continuously across clusters. Piceli applies on request; continuous
  reconciliation is not available ({doc}`roadmap`). `piceli render` can
  still produce the YAML those controllers apply.
- **Your infrastructure is more than Kubernetes.** Pulumi (or
  OpenTofu/Terraform) manages databases, DNS, buckets and clusters in one
  state with the Kubernetes objects; Piceli covers Kubernetes only
  ({doc}`roadmap`).
- **Your team does not use Python.** Helm and Kustomize need no programming
  language; cdk8s and Pulumi support several.
- **You expect removed objects to disappear.** Helm and Pulumi delete what
  you remove from the code; Piceli prunes only from a release spec with
  `prune = true`, and never Namespaces, volumes, claims or Secrets
  ({doc}`release_cli`).

## Using Piceli next to other tools

- **From YAML or a running namespace:** `piceli import yaml` and
  `piceli import live` write a typed module
  ({src}`test_imported_namespace_is_all_no_op_after_adoption <tests/acceptance/test_import_live.py>`,
  {doc}`migrate_from_kubectl`).
- **Existing manifests alongside a typed app:** see
  {ref}`faq-existing-yaml`.
- **Into existing pipelines:** `piceli render` prints plain manifests for
  `kubectl`, Argo CD or Flux; the {doc}`comparisons` test checks that they
  match what Helm, Kustomize, cdk8s and Pulumi produce for the same app.
