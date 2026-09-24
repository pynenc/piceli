# Operator Workflow

The operator layer builds on {doc}`deployment sessions <deployment_planning>` to
cover everyday release operations: inventory classification, a local release
catalog, promotion of an existing image digest, approvals, safe image garbage
collection, and backup/restore of operator state. Like the {doc}`operations lens
<operations_lens>`, it runs on the operator's machine and needs no external
database or public registry.

```{admonition} Maturity: experimental
:class: warning

These features are available as Python library APIs, and some of them through
`piceli operator` commands. Several are not yet connected end to end:

- `GitBranchWatcher` has no built-in Git resolver yet; you must supply one.
- `health_aware_rollback` and `dependency_safe_partial_release` are library
  functions only. No CLI command or REST endpoint calls them.
- The web UI's *rollback* action only changes the selected catalog entry. It does
  not deploy anything.
- REST calls enforce roles (`viewer` is read-only) but not yet the per-operation
  and per-namespace scopes of `StandingPolicy`.

See the {doc}`roadmap` for the plan to complete this workflow.
```

## Core Architecture

### 1. State Store and Single-Instance Safety
- **Storage Model**: File-backed storage remains the default state engine (`FileStateStore`), residing by default at `~/.config/piceli` or an attached persistent volume path.
- **Concurrency Control**: Exclusive `fcntl.flock(LOCK_EX | LOCK_NB)` on `.instance.lock` ensures exactly one operator instance writes to state at any time, rejecting accidental concurrent writers with `ConcurrentWriterError`.
- **Atomic Persistence**: Files are written with `mkstemp`, restricted to owner-only mode `0o600` (`0o700` for directories), and committed atomically via `os.replace` followed by directory fsync.
- **Backup and Safe Restore**: `store.create_backup(archive_path)` produces a timestamped `.tar.gz` archive of state files. `store.restore_backup(archive_path, destination)` restores state into an empty or owned destination after path safety and integrity validation.

### 2. Reactive Inventory & Classification
Piceli reconciles live Kubernetes cluster observations against declared release sessions and categorizes every observed object:

| Classification | Meaning | Policy |
| :--- | :--- | :--- |
| **`managed`** | Declared by active or catalogued Piceli session archive. | Monitored, reconciled, and updated across releases. |
| **`unmanaged`** | Present in the cluster namespace but not declared by Piceli. | Never adopted, altered, or deleted implicitly. |
| **`unknown`** | Inspection failed due to network/cluster transient errors. | Explicitly reported as unknown; never assumed absent. |

### 3. Opt-in Git/PR Automation & Untrusted PR Isolation
- **Branch Watcher**: `GitBranchWatcher` checks configured branches for new immutable commits when called, using a caller-supplied resolver. It does not run a background daemon.
- **Untrusted PR Security Gate**: **Untrusted PR code NEVER runs with deployment credentials.** PRs can only trigger isolated dry-run previews or lints. Merging or deploying PR changes requires an explicit operator approval via `approve_pr_release` or CLI `piceli operator approve`.
- **Digest Promotion Without Rebuilding**: `promote_release(catalog, source_name, target_name)` creates a new catalog record targeting a new tag or environment (e.g. `staging` -> `production`) while binding the **exact same immutable OCI digest and source provenance** without rebuilding.
- **Dependency-Safe Partial Rollout**: `dependency_safe_partial_release(workflow, components)` inspects the composition dependency graph and aborts with `DependencyUnsatisfiedError` if any required dependency is missing from the rollout set and not present in the live cluster.
- **Health-Aware Rollback**: `health_aware_rollback(workflow, target_release_name, executor)` preflights database migration compatibility, applies the prior release record, and verifies controller/pod readiness. If workloads enter `CrashLoopBackOff`, rollout halts and reports the failure. Rollback digests are strictly protected from GC.

### 4. Owner-Operated Artifact Storage & Safe GC
- **Streamed Container Delivery**: `StreamedOciRegistryClient` streams OCI blobs and manifests directly to in-cluster registries (for example `registry:5000`) or local containerd instances via OCI Distribution Spec v2 without requiring intermediate disk tar duplication or public registry pushes.
- **Multi-Dimensional Reference Tracking**:
  - Source references (Git commit, source digest).
  - Test references.
  - Release references (catalogued releases).
  - Running references (active pods in cluster).
  - Rollback protected digests.
- **Safe GC Invariant**: **Unknown inventory NEVER licenses deletion.** If any node or registry returns an error or incomplete scan, garbage collection aborts with `UnsafeGCError`. Only orphaned images older than retention TTL with zero active, running, or rollback references are reclaimed.

### 5. Unified Operator Interface (Library, CLI, REST, UI)
Identical authorization and operations are exposed across:
- **Python Library**: `piceli.k8s.operator`, `piceli.k8s.automation`, `piceli.artifacts.gc`, `piceli.k8s.operator_state`.
- **CLI Commands**:
  ```sh
  python -m piceli operator status --kubeconfig KUBECONFIG --context CONTEXT --namespace NS
  python -m piceli operator promote --catalog RELEASES.json --source branch-a --target production
  python -m piceli operator approve --state-dir STATE --pr-id 42 --commit HASH --namespace NS --approved-by user
  python -m piceli operator backup --state-dir STATE --output backup.tar.gz
  python -m piceli operator restore --archive-file backup.tar.gz --destination RESTORED_DIR
  python -m piceli operator serve --kubeconfig KUBECONFIG --context CONTEXT --namespace NS --port 9876
  ```
- **Versioned REST API**: `/v1/status`, `/v1/releases`, `/v1/releases/promote`, `/v1/releases/rollback`, `/v1/artifacts`, `/v1/artifacts/gc`, `/v1/automation`, `/v1/logs`, `/v1/forwards`, `/v1/backup/create`.
- **Web UI**: a local dashboard served by `piceli operator serve` (loopback only).

## Examples

Runnable examples are available in [`examples/operator_workflow/`](https://github.com/pynenc/piceli/tree/main/examples/operator_workflow):
1. `commit_and_digest_pipelines.py`: Demonstrates Git commit and direct OCI digest delivery paths.
2. `interrupted_reopen_and_rollback.py`: Exercises interrupted session reopen, resume, and health-aware rollback.
3. `operator_state_and_backup.py`: Exercises atomic state persistence, exclusive flock locking, and backup/restore.
