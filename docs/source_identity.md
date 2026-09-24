# Source identity

```{admonition} Maturity: preview
:class: note

Options and the lock format may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

A build is only reproducible if you know exactly which source it consumed.
Piceli records that as a **source identity** per checkout, taken from git
alone:

| Field | Meaning |
| --- | --- |
| `commit` | Full object ID of `HEAD`. |
| `dirty` | `true` if any path in scope differs from `HEAD`. |
| `diff_sha256` | Digest of the uncommitted working-tree state (`null` when clean). |
| `changed_paths` | Number of paths that differ. |
| `subpath` | Optional directory the identity is limited to. |
| `repository`, `path`, `remote_url` | Informational: directory name, declared path, `origin` URL without credentials. |

```{admonition} Maturity
:class: note

Identity capture, locks and `verify`: beta (the lock format
`piceli.inputs-lock.v1` is a contract). `--only` and partial locks: beta, new
in this release.
```

Nothing depends on local evidence files (hand-written receipts, copied hash
lists). Deleting or editing such a file cannot break or fake verification.

## Declare the sources

```toml
# inputs.toml — paths are relative to this file
[[source]]
name = "service-a"
path = "../service-a"      # must be the top level of a git work tree
ref = "v1.4.0"             # optional: HEAD must equal this commit, tag or branch

[[source]]
name = "shared-lib"
path = "../shared-lib"
subpath = "crates/core"    # optional: identity covers only this directory
allow_dirty = true         # optional, default false
```

## Record and verify

```sh
piceli inputs record --spec inputs.toml --out inputs.lock.json
piceli inputs verify --spec inputs.toml --lock inputs.lock.json
```

`record` fails (exit 2) if a source is not a git repository root, has no
commit, does not match its `ref`, or has uncommitted changes without
`allow_dirty = true`. It prints the lock as JSON and writes it atomically to
`--out`.

`verify` recaptures every source (or the lock's selection, see `--only`) and compares `commit`, `dirty`,
`diff_sha256` and `subpath` with the lock. It also compares a digest of the
declaration, so editing `inputs.toml` after recording is reported. It prints a
`piceli.inputs-verification.v1` JSON document and exits `0` when everything
matches, `1` on drift (with a one-line summary on stderr), and `2` on invalid
input. Moving a checkout or changing its remote is not drift.

### Check some sources: `--only`

```sh
piceli inputs verify --spec inputs.toml --lock inputs.lock.json --only shared-lib
piceli inputs record --spec inputs.toml --only shared-lib --only service-a --out partial.lock.json
```

`--only NAME` (repeatable) limits `record` or `verify` to the named sources of
the same `inputs.toml`, so you can check one pinned dependency before building
only that dependency, without a second declaration. An unknown name is
rejected (exit 2) with the fixed reason
`{"state": "rejected", "reason": "unknown-source", "source": "<NAME>"}`.

- `verify --only` compares just those sources with the lock's entries for
  them; drift in other sources is not reported. A name that the lock does
  not hold is drift (`present`). The report lists `"only": [...]`.
- `record --only` writes a **partial lock**: the digest of the whole
  declaration, the selected identities, and `"only": [...]`. `verify` without
  `--only` then checks exactly that selection.
- A build needs every source: a partial lock passed to `build-spec run
  --lock` is rejected with `source-drift` for the sources it lacks.

A drift report looks like this:

```json
{"drifts": [{"actual": true, "expected": false, "field": "dirty", "name": "service-a"},
            {"actual": "sha256:…", "expected": null, "field": "diff_sha256", "name": "service-a"}],
 "state": "drift", "...": "..."}
```

## Identity in builds: provenance, not the drift gate

`build-spec run` records every source (or verifies it against `--lock`) when
the build starts, and writes those identities into the receipt's `sources`.
It does **not** fail when a source's whole-checkout identity changes during
the build: a busy working tree (docs, scripts, other services) keeps
changing, and a long build should not be thrown away for files it never read.
Instead the build re-hashes the files each context actually staged, and a
change to one of those fails it with `context-changed`. Sources that moved
anyway are listed in the receipt's `sources_changed_during_build`. See
{ref}`build-drift` for the full table.

`pinned_sources` (below) keeps the strict whole-source check for callers that
want it.

## How the dirty state is hashed

`diff_sha256` follows the policy `piceli.source-dirty.v1`:

- The changed paths are those reported by `git status --untracked-files=all
  --no-renames`, restricted to `subpath`. **Untracked files count; files matched
  by `.gitignore` do not.** Keep build outputs ignored.
- Each path contributes its current working-tree state, not a textual diff: a
  file's SHA-256, size and executable bit; a symlink's target; `deleted` for a
  removed file. Staging a change does not alter the identity. Restoring the
  committed bytes makes the source clean again.
- A nested repository or submodule contributes its `HEAD` commit and a digest of
  its own status listing, not the content of its uncommitted changes. Declare it
  as its own source if those matter.
- The digest is SHA-256 of the canonical JSON list of these entries, sorted by
  path. It does not depend on diff drivers, colour, pager or locale settings.

Git runs with an explicit argument list (no shell), a per-call timeout
(`--timeout`, default 30 s), `GIT_OPTIONAL_LOCKS=0` so `status` never writes the
index, and `core.fsmonitor=false` so repository configuration cannot start a
hook. `GIT_DIR` and similar variables are ignored.

What this does **not** prove: that the commit is reviewed, signed or pushed, or
that files outside the declared sources (toolchains, caches) are unchanged.
Pin those separately, for example with `ToolPin`, or build in a pinned
container.

## Python API

```python
from pathlib import Path

from piceli.artifacts import InputsLock, InputsSpec, pinned_sources, verify_inputs

spec = InputsSpec.from_toml(Path("inputs.toml"))
lock = InputsLock.from_json(Path("inputs.lock.json").read_text())

report = verify_inputs(spec, lock)
print(report.ok, report.summary())

# Bracket a build: verify at start, fail if any checkout changes before the end.
with pinned_sources(spec, lock) as sources:
    run_build()  # your build step
    receipt = {"sources": sources.to_dict()["sources"]}
```

`pinned_sources(spec)` without a lock records the identities at entry instead.
On normal exit it recaptures them and raises `SourceDriftError` (a `ValueError`)
with the `InputsVerification` attached if anything changed. An exception raised
by the build itself propagates unchanged.

`record_inputs`, `verify_inputs` and `compare_inputs` take `only=[names]` for
the `--only` behavior; `InputsSpec.select(names)` raises `UnknownSourceError`
(code `unknown-source`) for an undeclared name. `open_sources(spec, lock)` is
the entry half of `pinned_sources` (record, or verify every source against the
lock) and is what builds use.

Other public names: `SourceSpec`, `SourceIdentity`, `capture_source_identity`
(one checkout, no policy), `record_inputs` (enforces `ref` and `allow_dirty`),
`compare_inputs` (two locks, no git), `SourceDrift` and `SourceIdentityError`.
`UnknownSourceError` and `open_sources` are imported from
`piceli.artifacts.source_identity`.
All are frozen dataclasses or functions; importing them runs no command.
