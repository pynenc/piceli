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
import shutil
import subprocess
import tempfile
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


def _git(repo: Path, *args: str, check: bool = True, data: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=data,
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
        check=False,
        timeout=600,
    )
    if check and done.returncode != 0:
        raise PromotionError(
            "promote-git-failed", f"git {args[0]} failed in the working copy"
        )
    return done.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------- working copy


def working_copy(path: Path) -> dict[str, Any]:
    """The flake's working copy, fetched and checked: clean (tracked files;
    untracked ones are ignored, as flakes ignore them), on a branch with an
    upstream, neither behind, ahead of nor diverged from it.

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
    _git(top, "fetch", "--quiet", "--tags", remote)
    counts = _git(
        top, "rev-list", "--left-right", "--count", f"HEAD...{remote}/{upstream}"
    ).split()
    ahead, behind = (int(item) for item in counts)
    if ahead or behind:
        where = (
            "diverged from"
            if ahead and behind
            else "behind"
            if behind
            else "ahead of (the push would publish commits the plan does not show)"
        )
        raise PromotionError(
            "promote-not-in-sync",
            f"branch {branch} is {where} {remote}/{upstream}: "
            f"{ahead} ahead, {behind} behind",
        )
    return {
        "path": top,
        "head": _git(top, "rev-parse", "HEAD"),
        "branch": upstream,
        "local_branch": branch,
        "remote": remote,
    }


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


def lock_by_edit(lock: bytes, name: str, rev: str) -> bytes:
    """``flake.lock`` with the input's node pinned to ``rev`` (no nix)."""
    from piceli.infra.promotion import lock_node

    parsed = json.loads(lock)
    key, node = lock_node(parsed, name)
    locked = dict(node["locked"])
    url = _clone_url(node.get("original") or locked)
    with tempfile.TemporaryDirectory(prefix="piceli-promote-") as work:
        clone = Path(work) / "input.git"
        done = subprocess.run(
            ["git", "clone", "--quiet", "--bare", "--", url, str(clone)],
            capture_output=True, check=False, timeout=1800,
            env={**os.environ, **_GIT_ENV},
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
        found, method = lock_by_edit(lock, name, rev), "edit"
    if locked_rev(found, name) != rev:
        raise PromotionError(
            "promote-lock-failed", f"the new {LOCK_FILE} does not pin {rev[:12]}"
        )
    return found, method


# ---------------------------------------------------------------- plan


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

    :raises PromotionError: a refusal (``promotion-not-verified`` when the
        environment passed no commit, the working copy's refusals)."""
    from piceli.dev.nix import plan_digest
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
    copy = working_copy(repo)
    top, head = copy["path"], copy["head"]
    current = locked_rev(
        _git(top, "show", f"{head}:{LOCK_FILE}", check=False) or None, gate.input
    )
    tags = _git(top, "tag", "--list").split()
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
    if current == best["commit"]:
        found = tags_at(top, head, pattern)
        body.update(change=None, commit_message=None)
        if found:
            return {"state": "current", "plan": None, "tag": found[-1],
                    "pinned": body["pinned"], "base": head}  # fmt: skip
        return {"state": "current", "plan": body, "digest": plan_digest(body)}
    lock, method = pinned_lock(top, head, gate.input, best["commit"], nix=nix)
    body["change"] = {
        "file": LOCK_FILE,
        "input": gate.input,
        "from": current,
        "to": best["commit"],
        "method": method,
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
    }
    body["commit_message"] = (
        f"promote {gate.input} to {best['commit'][:12]} from {gate.environment}\n\n"
        f"{LOCK_FILE} pins {gate.input} to {best['commit']}, which "
        f"{gate.environment} ran with its release checks passed (run "
        f"{best['run_id']}, {best['verified_at']}). Released as {tag}.\n"
    )
    return {
        "state": "approval-required",
        "plan": body,
        "digest": plan_digest(body),
        "lock": lock,
    }


def apply(
    found: Mapping[str, Any],
    repo: Path,
    *,
    say: Callable[[str], None] = lambda _l: None,
) -> dict[str, Any]:
    """Write, commit, tag and push the (re-computed, approved) plan.

    :raises PromotionError: ``promote-push-failed`` (the commit and the tag
        stay local; ``git push --atomic`` again publishes them)."""
    body = found["plan"]
    top = Path(_git(repo, "rev-parse", "--show-toplevel"))
    commit = body["base"]
    if body.get("change") is not None:
        (top / LOCK_FILE).write_bytes(found["lock"])
        _git(top, "commit", "--quiet", "-m", body["commit_message"], "--", LOCK_FILE)
        commit = _git(top, "rev-parse", "HEAD")
        say(f"committed {LOCK_FILE} as {commit[:12]}")
    pinned = body["pinned"]
    _git(
        top, "tag", "-a", body["tag"], commit, "-m",
        f"{body['tag']}: {pinned['input']} {pinned['commit'][:12]} verified on "
        f"{pinned['environment']} (run {pinned['verified_run']})",
    )  # fmt: skip
    say(f"tagged {body['tag']}")
    refs = [f"refs/tags/{body['tag']}"]
    if body.get("change") is not None:
        refs.insert(0, f"{commit}:refs/heads/{body['branch']}")
    _git(top, "push", "--atomic", "--quiet", body["remote"], *refs, check=False)
    remote_tag = _git(
        top, "ls-remote", "--tags", body["remote"], f"refs/tags/{body['tag']}^{{}}",
        check=False,
    )  # fmt: skip
    result = {"commit": commit, "tag": body["tag"], "remote": body["remote"],
              "branch": body["branch"]}  # fmt: skip
    if commit not in remote_tag:
        raise PromotionError(
            "promote-push-failed",
            f"the push to {body['remote']} failed; the commit and tag {body['tag']} "
            "are local: git push --atomic "
            f"{body['remote']} {' '.join(refs)}",
            result,
        )
    say(f"pushed {', '.join(refs)} to {body['remote']}")
    return result
