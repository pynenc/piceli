# Roadmap

This page says plainly what Piceli is for, what it does well, where it is
weak, and what comes next. The maturity of every feature is in the table at
the end. Priorities may change; progress is tracked in
[GitHub issues](https://github.com/pynenc/piceli/issues).

## What Piceli is for

Piceli lets you describe software and the machines it runs on in typed
Python, shows you exactly what would change, and changes only what you
approved. Every change follows the same loop:

1. **Model**: write the app (or the machine) as Python objects. Your editor
   and type checker catch mistakes before anything runs.
2. **Plan**: compare the model with what is live and print the difference,
   with a hash (a short fingerprint of the plan).
3. **Apply**: run that plan only when you pass its exact hash back. If the
   world changed in between, the hash no longer matches and nothing happens.
4. **Observe**: record what ran, check that it works, and roll back when it
   does not.

The same loop now covers four areas:

- **Kubernetes apps**: a typed `App`, environments (dev, staging, prod),
  custom resources, checks after a release and automatic rollback.
- **Delivery from Git (GitOps)**: a controller in the cluster that follows
  a Git repository, keeps one environment per branch, builds images and
  deploys them, with a web UI.
- **Builds**: images built from a pinned source on build hosts (your laptop,
  a server, or a node of the cluster), and a merge train that tests
  candidate branches together before they reach `main`.
- **Machines**: servers, IP addresses, buckets and DNS records created
  through OpenTofu (an open-source Terraform), and NixOS hosts (machines
  whose whole system is one declared build) that follow your release tags.

Agents are first-class users: every command prints one JSON object, uses
fixed exit codes and fixed error codes, and `piceli explain CODE` says what
went wrong and how to fix it.

## Strengths

- **Approval bound to the plan.** You approve a hash, not a command. Helm,
  Kustomize and `kubectl` have nothing like it. It is what makes Piceli safe
  to hand to an agent.
- **A recoverable engine.** Changes use Kubernetes server-side apply (the API
  server merges fields and records who owns each one), with preconditions
  and a journal: a run killed halfway resumes where it stopped, and an object
  another tool owns is never silently overwritten.
- **Short, checked app code.** The same app takes about a quarter of the
  lines it takes in Helm, Kustomize, cdk8s or Pulumi, and CI checks that all
  five render the same objects ({doc}`comparisons`).
- **A machine contract.** JSON on stdout, four exit codes, about 800 error
  codes each with a cause, a fix and whether a retry can succeed.
- **Testable without a cluster.** `piceli render` needs no cluster, and
  `piceli.testing` is a fake Kubernetes API for your own tests.
- **Never an ambient cluster.** Every command names its kubeconfig file and
  context. Piceli never falls back to whatever cluster your shell points at.

## Weak spots

- **Too big for one person.** About 190,000 lines of Python, 159 commands,
  about 800 error codes and 58 documentation pages. The surface grew faster
  than anyone could learn it.
- **A steep start.** Apps, pipelines, release specs, compositions,
  components, stacks, environments, clusters, build hosts, profiles and
  trains are all separate concepts. A newcomer has to learn many of them
  before the first useful deploy.
- **One maintainer.** Reviews, releases, security fixes and support all
  depend on one person, and the release pace (often several a week) makes
  "preview" mean "may change next week".
- **Slow tests.** About 2,700 tests run in parallel in a few minutes, but
  some are slow on their own (one OpenTofu test takes about 50 seconds), and
  the cluster tests run one at a time.
- **One production user.** Build hosts, the merge train, NixOS hosts,
  several clusters, client bundles and OpenTofu machines have each been used
  in production by one team only. Their defaults fit that team; a second one
  may need different ones.
- **Overlap with established tools.** The GitOps controller competes with
  Argo CD and Flux, the builds with Docker Buildx and CI systems, the machines
  with plain OpenTofu. Piceli's angle (one plan, one approval, typed Python
  across all of them) is real, but each piece is younger than the tool it
  replaces.

## What to simplify

- **One way to deploy.** `piceli deploy` (a Python `Pipeline`) and
  `piceli release` (a TOML spec) do much the same job. Keep `deploy`; make
  `release` a thin layer over it or retire it.
- **Retire the old paths.** The `piceli operator` layer and its local
  dashboard predate the GitOps controller and the web UI, which now do
  their job. `piceli heavy` (a lock for heavy work on one machine) is not
  about delivery at all.
- **A core and extras.** Name a core (typed apps, environments, plan,
  approve, apply, checks, import, testing, the agent contract) and promise
  to keep it stable. Mark the rest (builds, trains, machines, NixOS hosts,
  several clusters) as extras that may move to separate packages.
- **Fewer, shorter pages.** Several pages explain the same idea from
  different angles. Merge them, and keep `llms.txt` and the agent page short
  enough to read in one go.

## Next, ranked

1. **A smaller public API and fewer concepts.** The biggest cost for users
   and for the maintainer is the surface itself. Merging `deploy` and
   `release`, and retiring `operator` and `heavy`, shrinks it without
   losing a feature anyone relies on.
2. **A second production user.** Every feature outside the core has one
   user. A second team, ideally on a managed cluster with its own CI, shows
   which half of the surface matters, and becomes the rule: a feature is
   done when two users use it.
3. **Faster tests, measured per step.** For one maintainer, fast feedback is
   the multiplier. Record the time of each test and each train step, then
   cut the slow ones first (the OpenTofu tests above all).
4. **Docs consolidation.** Group the docs into "core" and "extras", merge
   pages that overlap, and shorten the agent material. Easier to learn, and
   cheaper for an agent to read.
5. **Short-lived Git tokens per build.** Today a build host or the
   controller keeps a long-lived read token. A token made for one build,
   limited to its repositories and expiring after it, means a leaked token
   is worth little.
6. **A webhook trigger for builds.** The controller polls Git (every 60
   seconds by default). A webhook (a call Git hosting makes when you push)
   starts a build at once and saves polls; polling stays as the fallback.
7. **Deterministic merges for speculative trials.** A train tries some
   merges ahead of time ("speculative trials"). If the same merge always
   gave the same commit, a speculative result would carry over to the run
   and to the promoted commit without being tested again.
8. **Trains on in-cluster build hosts.** Train steps run on Docker or native
   build hosts today. Running them on a node of the cluster removes the need
   for a separate machine, but it adds surface; it waits for a user who
   needs it.
9. **Approvals by more than one person.** Today one owner approves a plan.
   Teams will want two people, or a role, to approve a production change.
   It matters once there is a team using Piceli, so it comes after the
   second user.

## Feature status

Every feature page starts with its maturity:

- **stable**: no breaking change within a major version; JSON output only
  gains fields.
- **preview**: works and is tested; options, file formats and JSON fields may
  still change in a minor release, always with a changelog entry.
- **experimental**: incomplete or not wired end to end; may change or be
  removed without notice.

| Feature | Page | Maturity |
| --- | --- | --- |
| Object model: templates, `kubernetes` client models, YAML/JSON | {doc}`kubernetes_model/index` | stable |
| CLI overview | {doc}`cli/index` | preview |
| Typed apps (`piceli.App`, `piceli render`) | {doc}`typed_apps` | preview |
| Custom resources and generated models (`app.resource`, `piceli codegen crd`) | {doc}`crds` | preview |
| Environments (`app.environment`, `--env`, `--diff-env`) | {doc}`environments` | preview |
| Reference app: dev, staging and prod in one typed module | {doc}`reference_app` | preview |
| When to use Piceli; the same app in Helm, Kustomize, cdk8s and Pulumi | {doc}`when_to_use`, {doc}`comparisons` | preview |
| Engine: discovery, plans, executor, journals (Python API) | {doc}`deployment_planning` | preview |
| Deploy from source (`piceli deploy`, `piceli.pipeline`, `--ref`) | {doc}`deploy` | preview |
| Deploy from CI with an approval step | {doc}`ci` | preview |
| Releases from a spec (`piceli release`) | {doc}`release_cli` | preview |
| Post-deploy checks and automatic rollback | {doc}`checks` | preview |
| Pre-rollout and upgrade checks | {doc}`pre_rollout_checks` | experimental |
| Field-level diffs and no-op detection | {doc}`plans_and_diffs` | preview |
| Shared state, release lock, plan files (`piceli state`) | {doc}`state` | preview |
| Secrets: generate, template, import, fetch, rotate, back up | {doc}`secrets` | preview |
| Restore points of retained data | {doc}`restore_points` | preview |
| Supported Kubernetes versions and shared ownership | {doc}`compatibility` | preview |
| Managed-cluster credentials (GKE, EKS, AKS, OIDC) | {doc}`managed_clusters` | preview |
| Access and status from the model (`piceli access`, `piceli status`) | {doc}`access` | preview |
| Import and migration (`piceli import live`, `piceli import yaml`) | {doc}`migrate_from_kubectl` | preview |
| Fake Kubernetes API for tests (`piceli.testing`) | {doc}`testing` | preview |
| GitOps controller, per-branch environments, manifest export | {doc}`gitops`, {doc}`environments` | preview |
| Compositions and component contracts | {doc}`compositions`, {doc}`components` | preview |
| Several clusters from one controller | {doc}`multicluster` | preview |
| The controller's OpenTelemetry; deploy events | {doc}`opentelemetry`, {doc}`deploy_events` | experimental |
| Web UI | {doc}`ui` | experimental |
| In-cluster UI with OIDC sign-in | {doc}`ui_cluster_install` | experimental |
| Cluster init, in-cluster registry, node-local registry | {doc}`cluster_init`, {doc}`cluster_registry`, {doc}`node_local_registry` | preview |
| Registry retention | {doc}`registry_retention` | experimental |
| Source identity, containerized builds, image delivery | {doc}`source_identity`, {doc}`containerized_builds`, {doc}`node_delivery` | preview |
| Deterministic OCI builds (artifact API) | {doc}`artifact_delivery` | preview |
| Builds without a VM and node facts | {doc}`host_builds` | experimental |
| Multi-platform images published, attested and signed | {doc}`publishing_images` | experimental |
| Development builds and build hosts (`piceli dev`, `piceli builder serve`) | {doc}`dev_builds` | preview |
| Merge train (`piceli train`, `piceli trains`) | {doc}`train` | preview |
| Helm chart and manifests for other people's clusters; client bundles | {doc}`helm_charts`, {doc}`client_delivery` | preview |
| Machines with OpenTofu (Hetzner, OpenStack, Porkbun DNS) | {doc}`infrastructure` | preview |
| NixOS hosts and their releases | {doc}`single_nixos_host`, {doc}`nix_hosts` | preview |
| Runner hygiene (`piceli cache`, `piceli doctor`, `piceli runs`) | {doc}`maintenance` | preview |
| Machine-wide lock for heavy work (`piceli heavy`) | {doc}`heavy_work` | preview |
| Operations lens (`piceli observe`) | {doc}`operations_lens` | preview |
| Operator workflow (`piceli operator`) | {doc}`operator_workflow` | experimental |
| CLI contract, error codes, agent skill | {doc}`agents` | preview |
| Owner-declared approval policy (`auto_approve`) | {ref}`deploy-approval-policy` | preview |
| Cross-model eval of agents using Piceli | {doc}`contributing/evals` | experimental |

## Getting involved

Feedback on priorities is welcome: open an
[issue on GitHub](https://github.com/pynenc/piceli/issues).
