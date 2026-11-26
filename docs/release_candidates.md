# Release candidates

```{admonition} Maturity: experimental
:class: note

Release candidates are new in 0.83.0 and may change in a minor release, with a
changelog entry. See the {doc}`roadmap` for every feature's status.
```

A **release candidate** is a commit that an environment already tested, with
its images copied to the registry paths you release from. The GitOps
controller prepares it for you: once an environment passes a commit (its deploy
run ended with every check green) and no newer commit passes for a quiet time,
it copies the images of that run, by digest, to your outside registry. The
bytes you release are the bytes that were tested.

## Declare it

```python
from piceli.infra import Candidate, CandidateCopy, Controller

controller = Controller(
    on="node-a",
    candidate=Candidate(
        environment="staging",
        quiet="30m",
        keep=5,
        copies=(
            CandidateCopy(
                component="web",
                to="oci://ghcr.io/example/shop-web",
                tag="{commit}",
            ),
        ),
    ),
)
```

- `environment`: the environment whose passed commits are candidates. With
  several sources, the first source's commit names the candidate, unless you
  set `source`.
- `source` (optional, 0.85.0): the name of the followed source whose commit
  names the candidate, for example `source="product"` for an environment that
  follows `{workspace: "main", product: "main"}`. It is the name the source has
  in the environment (the key `gitops history` lists under `sources`), not the
  name of a Python variable. That commit fills `{commit}`
  in a copy's tag, its passed commits are the ones recorded, and the commit you
  give to `candidate request` is that source's. Unset keeps the first source.
  An environment that does not follow the source is refused when the
  composition loads (`candidate-source-unknown`, naming the sources it follows).
- A copy takes the run's image of the component, whatever commit built it: an
  image whose inputs did not change since an older commit (`current_for` in
  `gitops history`) is the one the candidate copies. Nothing compares the build
  commit to the candidate's (0.86.0).
- `quiet` (default `30m`): how long no newer commit may have passed.
- `keep` (default `5`): how many of the newest candidates stay. A released
  candidate always stays.
- `copies`: one `CandidateCopy` per image. `component` is the component whose
  image of the passed run is copied; `to` is `oci://host/repository` (no tag, no
  digest); `tag` is optional and may hold `{commit}` (the first 12 characters of
  the commit). Without a tag the image is copied by digest only. A component
  and a target appear once. `copies` may be empty: the candidate is then a
  ready marker. It copies nothing and becomes `ready` after the quiet time (or a
  `request`) once the run that passed the commit is found; `digests` lists the
  image digests of that run (a component without an image is not listed).
- `credentials` of a copy names a Secret in the controller's namespace with the
  key `credentials.json`: `{"username": "...", "password": "..."}` or
  `{"token": "..."}`. The controller mounts it as a file, never as a variable.
  Without it, the copy is anonymous. The Secret may be missing when the
  controller starts: the mount is optional, so a missing Secret fails the
  candidate with `candidate-credentials-missing` (the detail names the Secret),
  never the controller.

The copies are done by the resumable copy set (`piceli.artifacts.copy_set`):
every image is copied by digest, a ledger remembers what is done, and a retry
continues where it stopped.

## States

`piceli gitops status` prints one line per candidate:

```text
candidate abc1234: ready
candidate def5678: not ready: waiting for a quiet time until 2026-11-26T10:30:00Z
candidate 0123abc: not ready: candidate-no-image (component web has no image reference)
```

| State | Meaning |
| --- | --- |
| `waiting` | The commit passed; the quiet time is not over. |
| `building` | The images are being copied. A temporary failure (a registry that does not answer) is tried again, 60 seconds later, then 5, 15 and 30 minutes, up to five tries. A missing credentials Secret is tried again the same way (it may be created later; a mounted Secret appears without a restart), and a `request` tries at once. |
| `ready` | Every image is copied. |
| `failed` | `reason` says why; it is not tried again until you ask. |
| `cancelled` | A newer commit passed while it was building. A request continues it from its ledger. |

Reasons: `candidate-no-image` (a component of the run has no image; the detail
names it), `candidate-no-component` (the run has no such component),
`candidate-run-missing` (the history no longer has the run), `candidate-copy-error`,
`candidate-credentials-missing`, and the reason the copy set reports.
`candidate-superseded` is the reason of a cancelled candidate.

A newer passed commit cancels a candidate still building (the copy stops between
two images) and starts a new quiet time. The same lines are in the status
document (`piceli gitops status --json`) as `candidates`: a list of
`{commit, state, since, reason, detail, copies, released, run, passed_at,
digests}` (and `quiet_until` while waiting). `copies` maps each component to
`pending`, `copied` or `failed`.

## Commands

```console
$ piceli gitops candidate request abc1234     # start it now, no quiet wait
$ piceli gitops candidate release abc1234     # mark a ready one released
$ piceli gitops candidate list [--json]       # every candidate, newest first
```

`request` accepts a commit that passed (7 to 40 characters). A candidate that is
already building or ready is left as it is; a failed or cancelled one starts
again. `release` accepts only a ready candidate and keeps it whatever `keep`
says. All three take `--kubeconfig` and `--context`, or `--state-dir` for a local
controller.

## Protected digests

The controller keeps the newest `keep` candidates and every released one, and
forgets the others. The images of the kept candidates must survive the registry
cleanup. `piceli gitops candidate list --json` gives their digests; pass them to
the cleanup as pins:

```console
$ piceli gitops candidate list --json | jq -r '.candidates[].digests[]' > pins.txt
$ piceli artifacts retention --pin-file pins.txt
```

In code, `piceli.gitops.candidates.protected_digests(state)` returns the same set
from the controller's state.

## Not in this release

Nix closure candidates, a macOS build host, records-only paths and an upgrade
environment are not part of 0.83.0.
