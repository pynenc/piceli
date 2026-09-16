# Changelog

The changelog documents the history of changes and version releases for Piceli.

For detailed information on each version, please visit the [Piceli GitHub Releases page](https://github.com/pynenc/piceli/releases).

## Unreleased

- Added the read-only local operations lens: Python inventory API, JSON CLI,
  loopback REST endpoints, and owner-only non-secret port-forward preferences.
  It reconciles an explicit deployment-session archive with an explicit
  kubeconfig and distinguishes declared, missing, unknown, and undeclared
  objects without acquiring deployment authority.

- Added `DeploymentSession`, a Python-first provider-free boundary for one-time
  private input materialization, canonical opaque-reference interchange, exact
  preview/apply/resume, explicit rotation and owner-scoped stop.
- Added canonical `DeploymentRevision` and `ExecutionBundle` interchange for
  exact, secret-safe resume. Retained owned resources reconcile without a new
  SSA operation annotation; PVC first-consumer execution defers only readiness,
  never treats Pending claims as ready.
- Added public, deterministic build/OCI APIs and a thin `piceli artifacts` CLI
  with source/tool pins, bounded process groups, cancellation, secret-safe
  receipts and explicit local Docker import without tags or push.
- Added bounded OTLP deployment-operation spans/logs with exact journal states
  and signal-level drop/rejection/unknown-delivery accounting. Joined local
  acceptance covers the real recoverable executor, fault API and Poet restart.

- Replaced discovery authority v1 with validated v2 coverage, scope, timestamps,
  provenance and capture-time byte/deadline limits; retained historical v1 files.
- Added an explicit-client Kubernetes provider and scoped executor with private
  versioned secret bindings, durable intent/receipts, readiness, cancellation,
  resume and ownership-limited compensation. Qualified the provider against a
  fault-injecting local API, including process death and ambiguous replies.
- Added `make local-test-env` and `make test-local-executor`, hash-locked test
  dependencies, retained acceptance evidence and an executable composition example.
  Removed the unavailable exact Python interpreter pin from pre-commit bootstrap.

- Added immutable deployment components and target-bound observed snapshots,
  deterministic offline create/adopt/apply/no-op/delete plans, UID/version and
  absence preconditions, secret-safe hashes, retained-resource protection and
  child-first pruning.
- Switched `piceli deploy plan` to the pure planner and added tests proving that
  import and planning neither create Kubernetes clients nor mutate input intent.
- Required explicit cluster binding and resource adoption, and reject stale
  snapshots, wrong namespaces and unsafe unmanaged descendants.
- Published bounded portable discovery schema v1 and a pure provider protocol
  covering continuation, API scope, coverage failures, SSA conflicts and readiness.
- Bound snapshots to discovery/defaulting identity, preserved explicit desired
  defaults, blocked absence/pruning under incomplete coverage, and made built-in
  Namespace/PV/PVC/Secret retention non-overridable.

## Version 0.0.4

- First version of the docs

## Version 0.0.3

- CLI
- Integration tests
- Github pipelines

## Version 0.0.2

- MVP
- automatic deployment
- deploymnet graph and automatic dependencies
- automatic rollback
- comparison functionalities, adding ignoring/default paths

## Version 0.0.1

- Refactor legacy libs
- Adding support for kubernetes official python library
- Adding support for yaml and json
- Unit tests for piceli templates
