"""``piceli promote``: pin a verified commit in a flake and tag it (0.25.0).

``piceli promote web --from main --composition infra.py`` takes the
:class:`~piceli.infra.NixRelease` of the host ``web`` and its
:class:`~piceli.infra.Promotion` gate, finds the newest commit of the gated
input the environment ran with its release checks passed
(:func:`piceli.infra.promotion.newest`), and plans, in the flake's working
copy:

- the ``flake.lock`` that pins the input to exactly that commit;
- the commit of that file alone, with a message naming the run;
- the next tag of the host's ``Tag`` pattern (a patch bump by default);
- the push of the commit and the tag to the branch's upstream remote.

The plan (:data:`PLAN_SCHEMA`) has a digest; ``--approve DIGEST``
recomputes it, refuses when anything changed, and only then writes,
commits, tags and pushes (atomically: both refs or none).

**What the digest covers (0.74.0).** What the promotion changes: the host,
the input, its old and new commit, the lock's hash, the tag, the base, the
remote and branch. Not which run verified the commit: "the environment
verified the pinned commit" is checked at plan and again at approve, and a
later run of the same commit (a re-verification) keeps the approval valid.
The run is named in the commit message, the tag and the result.

**Resuming (0.74.0).** A push that failed leaves the commit and the tag
local; the same ``--approve`` command recomputes the plan from the
upstream commit, checks that the local commit is exactly its lock change,
and pushes it (``resume`` in the plan, ``resumed`` in the result).

**Transport retries (0.74.0, a backoff since 0.78.0).** A ``git fetch``,
``push``, ``ls-remote`` or ``clone`` that fails with a transport error (a
timeout such as ``ssh: connect to host … Operation timed out``, a refused or
reset connection, a name not resolved) runs up to :data:`GIT_ATTEMPTS`
times: after :data:`RETRY_PAUSE` seconds, then after two to four times
that (jitter). Each retry is listed in ``git_retries`` (command, attempt,
pause); Git's output never is.

**The lock.** With ``nix`` on the ``PATH``, ``nix flake lock
--override-input NAME URL?rev=REV`` runs in a temporary worktree of the
working copy's commit (removed afterwards). Without it, the input's node
is edited directly: its ``rev``, ``lastModified``, ``revCount`` (when the
lock has one) and ``narHash``, the NAR hash of the commit's tree computed
here from a temporary bare clone of the input. That path needs Git access
to the input and refuses (``promote-lock-failed``) an input that is not
``git``, ``github`` or ``gitlab``, a tree with submodules, or a
``.gitattributes`` that changes exports: those need ``nix``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from piceli.infra.promotion import LOCK_FILE, Promotion, PromotionError

PLAN_SCHEMA = "piceli.promote-plan.v1"
RESULT_SCHEMA = "piceli.promote.v1"
BUMPS = ("major", "minor", "patch")
#: The first version of a pattern without a release yet.
FIRST = (0, 1, 0)
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "LC_ALL": "C"}
#: Seconds before the first retry of a Git command that failed in transport;
#: the second waits two to four times as long.
RETRY_PAUSE = 2.0
#: Runs of a Git command that keeps failing in transport (the first included).
GIT_ATTEMPTS = 3
#: The Git commands that reach a remote (and are retried).
_NETWORK = frozenset({"fetch", "push", "ls-remote", "clone"})
_TRANSPORT = re.compile(
    r"timed out|timeout|connection (?:refused|reset|closed)|could not resolve|"
    r"temporary failure in name resolution|network is unreachable|"
    r"no route to host|the remote end hung up|early eof|rpc failed|"
    r"unexpected disconnect|broken pipe|ssl_|gnutls|tls connection",
    re.IGNORECASE,
)


def _run(
    args: Sequence[str],
    cwd: Path | None,
    *,
    data: bytes | None = None,
    timeout: int = 600,
    retried: list[dict[str, Any]] | None = None,
    before_retry: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """``git ARGS``; a command of :data:`_NETWORK` that fails in transport
    (a timeout, or a transport message on stderr) runs again, up to
    :data:`GIT_ATTEMPTS` runs, after a growing pause (:func:`_pause`), each
    noted in ``retried``. Git's output is never kept in the note (it may
    name a remote's credentials)."""
    attempts = GIT_ATTEMPTS if args[0] in _NETWORK else 1
    for attempt in range(attempts):
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=cwd,
                input=data,
                capture_output=True,
                env={**os.environ, **_GIT_ENV},
                check=False,
                timeout=timeout,
            )
            transport = done.returncode != 0 and bool(
                _TRANSPORT.search(done.stderr.decode("utf-8", "replace"))
            )
        except subprocess.TimeoutExpired:
            done = subprocess.CompletedProcess(["git", *args], 124, b"", b"")
            transport = True
        if not transport or attempt + 1 == attempts:
            return done
        pause = _pause(attempt + 1)
        if retried is not None:
            retried.append(
                {
                    "command": f"git {args[0]}",
                    "attempt": attempt + 1,
                    "pause_seconds": pause,
                }
            )
        time.sleep(pause)
        if before_retry is not None:
            before_retry()
    raise AssertionError("unreachable")  # pragma: no cover


def _pause(attempt: int) -> float:
    """Seconds before retry ``attempt``: :data:`RETRY_PAUSE`, then 2-4 times it."""
    if attempt <= 1:
        return RETRY_PAUSE
    return round(RETRY_PAUSE * 2 ** (attempt - 1) * random.uniform(1.0, 2.0), 3)


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    data: bytes | None = None,
    retried: list[dict[str, Any]] | None = None,
) -> str:
    done = _run(args, repo, data=data, retried=retried)
    if check and done.returncode != 0:
        raise PromotionError(
            "promote-git-failed", f"git {args[0]} failed in the working copy"
        )
    return done.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------- working copy


def working_copy(
    path: Path, retried: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The flake's working copy, fetched and checked: clean (tracked files;
    untracked ones are ignored, as flakes ignore them), on a branch with an
    upstream, neither behind, ahead of nor diverged from it.

    One exception (0.74.0): ``HEAD`` one commit ahead that changes only
    ``flake.lock`` on top of the upstream commit is a promotion whose push
    did not happen. ``head`` is then the upstream commit and ``pending``
    names the local commit and the tags at it (:func:`plan` checks that
    the commit is exactly the planned change, else refuses it as ahead).

    :raises PromotionError: ``promote-dirty``, ``promote-not-in-sync``."""
    top = Path(_git(path, "rev-parse", "--show-toplevel"))
    if _git(top, "status", "--porcelain", "--untracked-files=no"):
        raise PromotionError(
            "promote-dirty", "the working copy has uncommitted changes"
        )
    branch = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not branch:
        raise PromotionError(
            "promote-not-in-sync", "the working copy is on no branch (detached HEAD)"
        )
    remote = _git(top, "config", f"branch.{branch}.remote", check=False)
    merge = _git(top, "config", f"branch.{branch}.merge", check=False)
    if not remote or remote == "." or not merge.startswith("refs/heads/"):
        raise PromotionError(
            "promote-not-in-sync",
            f"branch {branch} has no upstream on a remote (git push -u REMOTE {branch})",
        )
    upstream = merge.removeprefix("refs/heads/")
    _git(top, "fetch", "--quiet", "--tags", remote, retried=retried)
    counts = _git(
        top, "rev-list", "--left-right", "--count", f"HEAD...{remote}/{upstream}"
    ).split()
    ahead, behind = (int(item) for item in counts)
    head = _git(top, "rev-parse", "HEAD")
    pending = None
    if (ahead, behind) == (1, 0) and _git(
        top, "diff", "--name-only", "HEAD~1", "HEAD"
    ) == LOCK_FILE:
        pending = {"commit": head, "tags": _git(top, "tag", "--points-at", head)
                   .split()}  # fmt: skip
        head = _git(top, "rev-parse", f"{remote}/{upstream}")
    elif ahead or behind:
        raise not_in_sync(branch, f"{remote}/{upstream}", ahead, behind)
    return {
        "path": top,
        "head": head,
        "branch": upstream,
        "local_branch": branch,
        "remote": remote,
        "pending": pending,
    }


def not_in_sync(branch: str, upstream: str, ahead: int, behind: int) -> PromotionError:
    """``promote-not-in-sync`` of a branch ahead of, behind or diverged
    from its upstream."""
    where = (
        "diverged from"
        if ahead and behind
        else "behind"
        if behind
        else "ahead of (the push would publish commits the plan does not show)"
    )
    return PromotionError(
        "promote-not-in-sync",
        f"branch {branch} is {where} {upstream}: {ahead} ahead, {behind} behind",
    )


# ---------------------------------------------------------------- tags


def next_tag(tags: Sequence[str], pattern: str, bump: str) -> tuple[str, str | None]:
    """``(next tag, highest tag)`` of the ``Tag`` ``pattern`` among ``tags``:
    ``bump`` the highest release (pre-releases do not count)."""
    from piceli.hosts.agent import tag_prefix, tag_version

    prefix = tag_prefix(pattern)
    ranked = [
        (version, tag)
        for tag in tags
        if (version := tag_version(pattern, tag)) is not None and version[3] == 1
    ]
    if not ranked:
        return prefix + ".".join(str(n) for n in FIRST), None
    version, highest = max(ranked)
    major, minor, patch = version[:3]
    major, minor, patch = {
        "major": (major + 1, 0, 0),
        "minor": (major, minor + 1, 0),
        "patch": (major, minor, patch + 1),
    }[bump]
    rest = highest[len(prefix) :]
    lead = "v" if rest.startswith("v") and not prefix.endswith("v") else ""
    return f"{prefix}{lead}{major}.{minor}.{patch}", highest


def tags_at(repo: Path, commit: str, pattern: str) -> list[str]:
    """The tags of ``pattern`` that point at ``commit``."""
    from piceli.hosts.agent import tag_version

    return sorted(
        tag
        for tag in _git(repo, "tag", "--points-at", commit).split()
        if tag_version(pattern, tag) is not None
    )


# ---------------------------------------------------------------- the lock


def override_url(original: Mapping[str, Any], rev: str) -> str:
    """The ``--override-input`` URL of an input's ``original`` at ``rev``.

    :raises PromotionError: ``promote-lock-failed`` (another input type)."""
    kind = original.get("type")
    query = {k: str(original[k]) for k in ("ref", "dir") if original.get(k)}
    if kind in ("github", "gitlab", "sourcehut"):
        query.pop("ref", None)
        url = f"{kind}:{original['owner']}/{original['repo']}/{rev}"
        if original.get("host"):
            query["host"] = str(original["host"])
    elif kind == "git":
        url = "git+" + str(original["url"]).removeprefix("git+")
        query["rev"] = rev
    else:
        raise PromotionError(
            "promote-lock-failed",
            f"the input's type {kind!r} is not git, github, gitlab or sourcehut",
        )
    if not query:
        return url
    return url + "?" + "&".join(f"{k}={v}" for k, v in sorted(query.items()))


def lock_with_nix(
    repo: Path, head: str, name: str, url: str, nix: str = "nix"
) -> bytes:
    """``flake.lock`` after ``nix flake lock --override-input`` in a temporary
    worktree of ``head`` (removed afterwards)."""
    with tempfile.TemporaryDirectory(prefix="piceli-promote-") as work:
        tree = Path(work) / "flake"
        _git(repo, "worktree", "add", "--quiet", "--detach", str(tree), head)
        try:
            done = subprocess.run(
                [
                    nix, "--extra-experimental-features", "nix-command flakes",
                    "flake", "lock", "--override-input", name, url,
                ],
                cwd=tree, capture_output=True, check=False, timeout=1800,
            )  # fmt: skip
            if done.returncode != 0:
                raise PromotionError(
                    "promote-lock-failed",
                    f"nix flake lock --override-input {name} failed",
                )
            return (tree / LOCK_FILE).read_bytes()
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree), check=False)
            _git(repo, "worktree", "prune", check=False)


def _nar_string(data: bytes) -> bytes:
    return len(data).to_bytes(8, "little") + data + b"\0" * (-len(data) % 8)


def nar_hash(entries: Mapping[str, tuple[str, bytes]]) -> str:
    """The SRI ``sha256-…`` NAR hash of a tree given as ``{path: (git mode,
    content)}`` (``100644``, ``100755`` files, ``120000`` symbolic links)."""
    root: dict[str, Any] = {}
    for path, item in entries.items():
        node = root
        *parents, leaf = path.split("/")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = item
    digest = hashlib.sha256()
    s = _nar_string

    def walk(node: Any) -> None:
        digest.update(s(b"("))
        if isinstance(node, dict):
            digest.update(s(b"type") + s(b"directory"))
            for name in sorted(node, key=lambda text: text.encode()):
                digest.update(s(b"entry") + s(b"(") + s(b"name") + s(name.encode()))
                digest.update(s(b"node"))
                walk(node[name])
                digest.update(s(b")"))
        else:
            mode, content = node
            if mode == "120000":
                digest.update(s(b"type") + s(b"symlink") + s(b"target") + s(content))
            else:
                digest.update(s(b"type") + s(b"regular"))
                if mode == "100755":
                    digest.update(s(b"executable") + s(b""))
                digest.update(s(b"contents") + s(content))
        digest.update(s(b")"))

    digest.update(s(b"nix-archive-1"))
    walk(root)
    return "sha256-" + base64.b64encode(digest.digest()).decode()


def _clone_url(original: Mapping[str, Any]) -> str:
    kind = original.get("type")
    if kind == "git":
        return str(original["url"]).removeprefix("git+")
    if kind in ("github", "gitlab"):
        host = original.get("host") or f"{kind}.com"
        return f"https://{host}/{original['owner']}/{original['repo']}.git"
    raise PromotionError(
        "promote-lock-failed",
        f"without nix only git, github and gitlab inputs are pinned, not {kind!r}",
    )


def tree_of(clone: Path, rev: str) -> dict[str, tuple[str, bytes]]:
    """``{path: (mode, content)}`` of ``rev``'s tree in a bare clone.

    :raises PromotionError: ``promote-lock-failed`` (submodules, exports)."""
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", rev],
        cwd=clone, capture_output=True, check=True,
    ).stdout  # fmt: skip
    rows = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, raw = record.partition(b"\t")
        mode, kind, oid = meta.decode().split(" ")
        if kind != "blob":
            raise PromotionError(
                "promote-lock-failed", "the input has submodules: pin it with nix"
            )
        rows.append((raw.decode("utf-8", "surrogateescape"), mode, oid))
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=clone,
        input=b"".join(oid.encode() + b"\n" for _, _, oid in rows),
        capture_output=True,
        check=True,
    ).stdout
    entries: dict[str, tuple[str, bytes]] = {}
    at = 0
    for path, mode, _ in rows:
        end = batch.index(b"\n", at)
        size = int(batch[at:end].split()[2])
        entries[path] = (mode, batch[end + 1 : end + 1 + size])
        at = end + 1 + size + 1
    for path, (_, content) in entries.items():
        if path.rsplit("/", 1)[-1] == ".gitattributes" and b"export-" in content:
            raise PromotionError(
                "promote-lock-failed",
                "the input's .gitattributes changes exports: pin it with nix",
            )
    return entries


def lock_by_edit(
    lock: bytes, name: str, rev: str, retried: list[dict[str, Any]] | None = None
) -> bytes:
    """``flake.lock`` with the input's node pinned to ``rev`` (no nix)."""
    from piceli.infra.promotion import lock_node

    parsed = json.loads(lock)
    key, node = lock_node(parsed, name)
    locked = dict(node["locked"])
    url = _clone_url(node.get("original") or locked)
    with tempfile.TemporaryDirectory(prefix="piceli-promote-") as work:
        clone = Path(work) / "input.git"
        done = _run(
            ["clone", "--quiet", "--bare", "--", url, str(clone)],
            None, timeout=1800, retried=retried,
            # a clone that stopped half-way is removed before the retry
            before_retry=lambda: shutil.rmtree(clone, ignore_errors=True),
        )  # fmt: skip
        if done.returncode != 0:
            raise PromotionError(
                "promote-lock-failed", f"the input {name} could not be cloned"
            )
        found = subprocess.run(
            ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
            cwd=clone, capture_output=True, check=False,
        )  # fmt: skip
        if found.returncode != 0:
            raise PromotionError(
                "promote-lock-failed", f"the input {name} has no commit {rev[:12]}"
            )
        locked["narHash"] = nar_hash(tree_of(clone, rev))
        locked["rev"] = rev
        stamp = subprocess.run(
            ["git", "log", "-1", "--format=%ct", rev],
            cwd=clone, capture_output=True, check=True, text=True,
        ).stdout.strip()  # fmt: skip
        locked["lastModified"] = int(stamp)
        if "revCount" in locked:
            count = subprocess.run(
                ["git", "rev-list", "--count", rev],
                cwd=clone, capture_output=True, check=True, text=True,
            ).stdout.strip()  # fmt: skip
            locked["revCount"] = int(count)
    parsed["nodes"][key]["locked"] = locked
    return (json.dumps(parsed, indent=2, sort_keys=True) + "\n").encode()


def pinned_lock(
    repo: Path,
    head: str,
    name: str,
    rev: str,
    *,
    nix: str | None = None,
    retried: list[dict[str, Any]] | None = None,
) -> tuple[bytes, str]:
    """``(new flake.lock, method)``: ``nix`` when it is on the PATH (or
    given), else ``edit``. :raises PromotionError: the lock does not pin
    ``rev`` afterwards (``promote-lock-failed``)."""
    from piceli.infra.promotion import lock_node, locked_rev

    lock = _git(repo, "show", f"{head}:{LOCK_FILE}").encode()
    _, node = lock_node(json.loads(lock), name)
    tool = nix if nix is not None else shutil.which("nix")
    if tool:
        url = override_url(node.get("original") or node["locked"], rev)
        found, method = lock_with_nix(repo, head, name, url, tool), "nix"
    else:
        found, method = lock_by_edit(lock, name, rev, retried), "edit"
    if locked_rev(found, name) != rev:
        raise PromotionError(
            "promote-lock-failed", f"the new {LOCK_FILE} does not pin {rev[:12]}"
        )
    return found, method


# ---------------------------------------------------------------- plan

#: Fields of ``pinned`` that name the run that verified the commit, outside
#: the digest (0.74.0).
RUN_FIELDS = ("verified_run", "verified_at")


def digest(body: Mapping[str, Any]) -> str:
    """The digest of a plan: what the promotion changes, not the run that
    verified the pinned commit (``pinned``'s :data:`RUN_FIELDS` and the
    commit message, which quotes them, are left out)."""
    from piceli.dev.nix import plan_digest

    covered = {key: value for key, value in body.items() if key != "commit_message"}
    if isinstance(body.get("pinned"), Mapping):
        covered["pinned"] = {
            key: value for key, value in body["pinned"].items() if key not in RUN_FIELDS
        }
    return plan_digest(covered)


def plan(
    *,
    host: str,
    pattern: str,
    gate: Promotion,
    history: Any,
    repo: Path,
    bump: str = "patch",
    nix: str | None = None,
) -> dict[str, Any]:
    """The promotion plan (``state``: ``approval-required`` or ``current``).

    ``resume`` (0.74.0): the local commit and tag a failed push left, which
    :func:`apply` pushes instead of committing again. ``git_retries``: the
    Git commands retried after a transport error.

    :raises PromotionError: a refusal (``promotion-not-verified`` when the
        environment passed no commit, the working copy's refusals)."""
    from piceli.infra.promotion import locked_rev, newest, pinned

    if bump not in BUMPS:
        raise PromotionError("promote-invalid", "--bump is major, minor or patch")
    best = newest(history, gate)
    if best is None:
        raise PromotionError("promotion-not-verified",
            f"environment {gate.environment} has no run with passed release "
            f"checks of {gate.source_key}",
            {"input": gate.input, "environment": gate.environment,
             "commit": None, "newest_passed": None},
        )  # fmt: skip
    retried: list[dict[str, Any]] = []
    copy = working_copy(repo, retried)
    top, head, pending = copy["path"], copy["head"], copy["pending"]
    current = locked_rev(
        _git(top, "show", f"{head}:{LOCK_FILE}", check=False) or None, gate.input
    )
    # tags only a failed push left are local: the plan was made without them
    skip = set(pending["tags"]) if pending else set()
    if current == best["commit"] and not pending:
        at_head = tags_at(top, head, pattern)
        if at_head:
            listed = _git(top, "ls-remote", "--tags", "--refs", copy["remote"],
                          retried=retried).splitlines()  # fmt: skip
            remote_tags = {line.rpartition("refs/tags/")[2] for line in listed}
            published = [item for item in at_head if item in remote_tags]
            if published:
                return {"state": "current", "plan": None, "tag": published[-1],
                        "pinned": pinned(gate, best), "base": head,
                        "git_retries": retried}  # fmt: skip
            skip.update(at_head)
            pending = {"commit": head, "tags": at_head}
    tags = [item for item in _git(top, "tag", "--list").split() if item not in skip]
    tag, previous = next_tag(tags, pattern, bump)
    body: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "host": host,
        "pinned": pinned(gate, best),
        "base": head,
        "remote": copy["remote"],
        "branch": copy["branch"],
        "tag": tag,
        "previous_tag": previous,
        "bump": bump,
    }
    found: dict[str, Any]
    if current == best["commit"]:
        if pending and pending["commit"] != head:  # a lock commit, not this pin
            raise not_in_sync(
                copy["local_branch"], f"{copy['remote']}/{copy['branch']}", 1, 0
            )
        body.update(change=None, commit_message=None)
        found = {"state": "current", "plan": body, "digest": digest(body),
                 "git_retries": retried}  # fmt: skip
    else:
        lock, method = pinned_lock(
            top, head, gate.input, best["commit"], nix=nix, retried=retried
        )
        body["change"] = {
            "file": LOCK_FILE,
            "input": gate.input,
            "from": current,
            "to": best["commit"],
            "method": method,
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
        }
        body["commit_message"] = (
            f"promote {gate.input} to {best['commit'][:12]} from "
            f"{gate.environment}\n\n{LOCK_FILE} pins {gate.input} to "
            f"{best['commit']}, which {gate.environment} ran with its release "
            f"checks passed (run {best['run_id']}, {best['verified_at']}). "
            f"Released as {tag}.\n"
        )
        found = {"state": "approval-required", "plan": body,
                 "digest": digest(body), "lock": lock,
                 "git_retries": retried}  # fmt: skip
        if pending and _git(
            top, "rev-parse", f"{pending['commit']}:{LOCK_FILE}"
        ) != _git(top, "hash-object", "--stdin", data=lock):
            raise not_in_sync(
                copy["local_branch"], f"{copy['remote']}/{copy['branch']}", 1, 0
            )
    if pending:
        others = [item for item in tags_at(top, pending["commit"], pattern)
                  if item != tag]  # fmt: skip
        if others:
            raise PromotionError(
                "promote-not-in-sync",
                f"tag {others[-1]} at {pending['commit'][:12]} is not on "
                f"{copy['remote']}: run the --approve command that made it "
                f"again, or delete it (git tag -d {others[-1]})",
            )
        found["resume"] = {
            "commit": pending["commit"],
            "tag": tag if tag in pending["tags"] else None,
        }
    return found


def apply(
    found: Mapping[str, Any],
    repo: Path,
    *,
    say: Callable[[str], None] = lambda _l: None,
) -> dict[str, Any]:
    """Write, commit, tag and push the (re-computed, approved) plan; with
    the plan's ``resume``, push the local commit (and tag) a failed push
    left.

    :raises PromotionError: ``promote-push-failed`` (the commit and the tag
        stay local; the same ``--approve`` command pushes them)."""
    body = found["plan"]
    retried: list[dict[str, Any]] = list(found.get("git_retries") or ())
    resume = found.get("resume")
    top = Path(_git(repo, "rev-parse", "--show-toplevel"))
    commit = body["base"]
    if resume:
        commit = resume["commit"]
        say(f"resuming: {LOCK_FILE} is committed as {commit[:12]}, not pushed")
    elif body.get("change") is not None:
        (top / LOCK_FILE).write_bytes(found["lock"])
        _git(top, "commit", "--quiet", "-m", body["commit_message"], "--", LOCK_FILE)
        commit = _git(top, "rev-parse", "HEAD")
        say(f"committed {LOCK_FILE} as {commit[:12]}")
    pinned = body["pinned"]
    if not (resume and resume.get("tag")):
        _git(
            top, "tag", "-a", body["tag"], commit, "-m",
            f"{body['tag']}: {pinned['input']} {pinned['commit'][:12]} verified on "
            f"{pinned['environment']} (run {pinned['verified_run']})",
        )  # fmt: skip
        say(f"tagged {body['tag']}")
    refs = [f"refs/tags/{body['tag']}"]
    if body.get("change") is not None:
        refs.insert(0, f"{commit}:refs/heads/{body['branch']}")
    _git(
        top, "push", "--atomic", "--quiet", body["remote"], *refs,
        check=False, retried=retried,
    )  # fmt: skip
    remote_tag = _git(
        top, "ls-remote", "--tags", body["remote"], f"refs/tags/{body['tag']}^{{}}",
        check=False, retried=retried,
    )  # fmt: skip
    result = {"commit": commit, "tag": body["tag"], "remote": body["remote"],
              "branch": body["branch"], "verified_run": pinned["verified_run"],
              "resumed": bool(resume), "git_retries": retried}  # fmt: skip
    if commit not in remote_tag:
        raise PromotionError(
            "promote-push-failed",
            f"the push to {body['remote']} failed; the commit {commit[:12]} and "
            f"tag {body['tag']} are local: run the same --approve command again "
            "to push them",
            result,
        )
    say(f"pushed {', '.join(refs)} to {body['remote']}")
    return result
