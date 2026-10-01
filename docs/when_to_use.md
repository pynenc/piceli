# When to use Piceli, and when not to

A short fit guide. Each point links the test, example or page behind it;
{doc}`comparisons` shows the same app written with Piceli, Helm, Kustomize,
cdk8s and Pulumi and compares them in detail.

```{admonition} Maturity: preview
:class: note

Piceli is pre-alpha. The guide below describes this release and changes
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

## Current limits

What Piceli does not do today, so you can judge the fit:

- **APIs still change.** Piceli is pre-alpha and its APIs change between
  releases ({doc}`changelog`).
- **No package format.** Piceli has no reusable, versioned package of
  components ({doc}`roadmap`). A vendor's Helm chart can be rendered with
  `helm template` and its YAML loaded ({ref}`faq-existing-yaml`).
- **Kubernetes only.** Piceli does not manage databases, DNS, buckets or
  clusters ({doc}`roadmap`); it has GKE cluster helpers only.
- **Python only.** Apps are written in Python.
- **Pruning is deliberate.** Removed objects disappear only from a release
  spec with `prune = true`, and never Namespaces, volumes, claims or Secrets
  ({doc}`release_cli`).

## GitOps, environments and the web UI

Continuous delivery is part of Piceli, not something to add next to it:

- **One environment per branch from Git.** `piceli gitops enable` installs a
  controller that polls a repository and keeps one environment per branch at
  its head, with the same plans, approvals and checks as `piceli deploy`;
  `piceli env`, `piceli envs` and `piceli promote` operate on those
  environments ({doc}`gitops`, {doc}`environments`).
- **A web UI.** `piceli ui serve` locally, or installed in the cluster with
  OIDC, shows applications, environments, the GitOps controller and Pipeline
  plans with their approval step ({doc}`ui`).
- **Export when you want it.** `piceli publish --to oci://` produces an OCI
  artifact of the rendered manifests, `piceli render --out DIR` writes them as
  files and `piceli chart` writes a Helm chart for clusters that want one
  ({doc}`gitops`, {doc}`helm_charts`).

## Bringing what you have

- **From YAML or a running namespace:** `piceli import yaml` and
  `piceli import live` write a typed module
  ({src}`test_imported_namespace_is_all_no_op_after_adoption <tests/acceptance/test_import_live.py>`,
  {doc}`migrate_from_kubectl`).
- **Existing manifests alongside a typed app:** see
  {ref}`faq-existing-yaml`.
- **Plain manifests:** `piceli render` prints them; the {doc}`comparisons`
  test checks that they match what Helm, Kustomize, cdk8s and Pulumi produce
  for the same app.
