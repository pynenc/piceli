"""Fetch a flake's private inputs into the build host's Nix store (0.26.0).

Standard library only: before a Nix build, the client
(:mod:`piceli.dev.nix_inputs`) runs this file's source with ``python3 -I -c``
on the build host, as it runs the publisher (:mod:`piceli.dev.nix_publish`):
a native host's own Python, or a short ``docker run --rm -i`` of the run
image with the host's ``/nix`` (its daemon) mounted. It executes no code of
the project: only ``nix eval`` of ``builtins.fetchTree`` on each input's
locked attributes, as ``flake.lock`` pins them (``narHash`` included, so
Nix checks what it fetched).

Its input is one JSON object on **stdin** (never an argument or the
environment of the client): the inputs in order, each with its locked
attributes and, when a credential applies, the credential's host, user name
and token. Each input is fetched by its own ``nix`` process, so a token only
ever reaches the input it was given for:

- ``askpass`` (a ``git+https`` input, or a ``git+ssh`` one rewritten to
  HTTPS by the client): Git reads the user name and the token through
  ``GIT_ASKPASS``, a script in a ``0700`` temporary directory reading two
  ``0600`` files there; ``credential.helper`` is reset for that process, so
  no helper stores the token or answers with another one;
- ``access-token`` (``github:``, ``gitlab:``, ``sourcehut:``): the token
  reaches ``nix`` as ``access-tokens = HOST=TOKEN`` in its child's
  ``NIX_CONFIG`` only;
- ``ssh-key`` (a ``git+ssh`` input and a deploy key): ``GIT_SSH_COMMAND``
  with the key in a ``0600`` file there, ``IdentitiesOnly``, and the host's
  known_hosts lines when stored (else its key is taken on first use, in a
  temporary file);
- ``none``: the host's own access (a public input, or its own Git
  credential helpers and SSH agent on a native host).

With ``roots`` (a directory of the build host the client removes when the
build ended; a native host's), each fetched tree also gets an indirect GC
root there, so a garbage collection between the fetch and the build cannot
take it (on a docker host, 0.26.2: a directory under its
``BuildHost.roots_dir``, mounted at the same path, so its daemon sees the
links; ``{"release": DIR}`` removes it after the build, and ``{"hold":
{"link", "path"}}`` roots one store path, the kept image of an ``--image``
build). Nix's fetcher cache (``XDG_CACHE_HOME``) is in the temporary directory
too, so no clone of a private repository stays on the host; the directory is
removed on every exit. The fetched trees land in the host's store (through
its daemon).

0.26.3: each tree is fetched with exactly the lock's attributes (``shallow``
included) and its ``narHash`` is recomputed (``nix hash path``) and checked
against the lock's; a different tree stops the fetch (``mismatch``, naming
both hashes). With ``lock`` (the commit's ``flake.lock``), the step then
stores a *reference lock*: the same lock with each fetched node locked as a
``path`` input of its tree (``narHash``, ``rev``, ``revCount``,
``lastModified`` kept), rooted like the trees. The build reads it with
``nix build --reference-lock-file``, so it never resolves a private input
itself; its result is the same, as the trees are. Output lines are masked; the one result line
(``PICELI-NIX-INPUTS {json}``) carries names and store paths, never a
secret. It stops at the first input it cannot fetch.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

RESULT_MARKER = "PICELI-NIX-INPUTS "
#: The store name of the reference lock (0.26.3).
LOCK_NAME = "flake.lock"
#: Where ``nix`` is on a multi-user install and on NixOS (also with only
#: ``/nix`` mounted into a container).
NIX_PATHS = (
    "/nix/var/nix/profiles/default/bin",
    "/nix/var/nix/profiles/system/sw/bin",
    "/run/current-system/sw/bin",
)
FEATURES = ("--extra-experimental-features", "nix-command flakes")
FETCH_SECONDS = 1800
#: The locked input types it fetches.
TYPES = ("git", "github", "gitlab", "sourcehut", "tarball", "file", "hg")
MODES = ("askpass", "access-token", "ssh-key", "none")
#: Lines of a failed fetch's output kept (masked) for its reason.
TAIL_LINES = 20
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*|username*) file=$PICELI_ASKPASS_USER ;;
  *) file=$PICELI_ASKPASS_TOKEN ;;
esac
IFS= read -r value < "$file" || true
printf '%s\\n' "$value"
"""


def nix_string(text: str) -> str:
    """``text`` as a Nix string literal (no interpolation)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def fetch_attrs(locked: Mapping[str, Any]) -> dict[str, Any]:
    """The ``fetchTree`` attributes of a locked node: exactly as
    ``flake.lock`` locks it, ``shallow`` included (0.26.3; 0.26.2 forced
    ``shallow = false``). The tree, and so its ``narHash``, is the same
    either way; the lock's own attributes are what the build would use."""
    return dict(locked)


#: The attributes a locked node keeps when the build gets its fetched tree
#: as a ``path`` input (0.26.3): what the flake can read of an input
#: (``rev``, ``shortRev``, ``revCount``, ``lastModified``, ``narHash``).
PATH_KEEPS = ("narHash", "lastModified", "rev", "revCount")


def sri(text: str) -> str:
    """A ``sha256-…`` hash with its base-64 padding (old locks lack it)."""
    text = text.strip()
    if text.startswith("sha256-"):
        body = text[len("sha256-") :]
        return "sha256-" + body + "=" * (-len(body) % 4)
    return text


def reference_lock(lock: str, fetched: Mapping[str, str]) -> str:
    """``lock`` with each fetched node (``{node key: store path}``) locked
    as a ``path`` input of its tree in the store (0.26.3): the build reads
    it with ``--reference-lock-file`` and never resolves the input itself.
    The node's ``original``, its inputs and every other node stay as they
    are, so nothing is relocked and the build's result is the same.

    :raises ValueError: ``lock`` is not a lock, or lacks a fetched node."""
    parsed = json.loads(lock)
    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(nodes, dict):
        raise ValueError("flake.lock has no nodes")
    for key, path in fetched.items():
        node = nodes.get(key)
        if not isinstance(node, dict) or not isinstance(node.get("locked"), dict):
            raise ValueError(f"flake.lock does not lock {key}")
        locked = node["locked"]
        node["locked"] = {
            "type": "path",
            "path": path,
            **{name: locked[name] for name in PATH_KEEPS if name in locked},
        }
    return json.dumps(parsed, indent=2, sort_keys=True) + "\n"


def fetch_expr(locked: Mapping[str, Any]) -> str:
    """The expression whose value is the store path of ``locked``'s tree."""
    body = json.dumps(fetch_attrs(locked), sort_keys=True)
    return f"(builtins.fetchTree (builtins.fromJSON {nix_string(body)})).outPath"


def eval_argv(nix: str, locked: Mapping[str, Any]) -> list[str]:
    return [nix, *FEATURES, "eval", "--raw", "--expr", fetch_expr(locked)]


def hash_argv(nix: str, path: str) -> list[str]:
    """The ``narHash`` of the tree at ``path``, recomputed from its files."""
    return [nix, *FEATURES, "hash", "path", path]


def lock_argv(nix: str, file: str) -> list[str]:
    """Adds ``file`` (the reference lock) to the store as ``…-flake.lock``."""
    expr = f"builtins.toFile {nix_string(LOCK_NAME)} (builtins.readFile {nix_string(file)})"
    return [nix, *FEATURES, "eval", "--impure", "--raw", "--expr", expr]


def root_argv(nix: str, link: str, path: str) -> list[str]:
    """An indirect GC root ``link`` of the fetched tree ``path`` (registered
    with the daemon; it holds while ``link`` exists)."""
    return [nix, *FEATURES, "build", "--out-link", link, path]


def check_locked(locked: Any) -> dict[str, Any] | None:
    """``locked`` when it is a flat object of a known type, else ``None``."""
    if not isinstance(locked, Mapping) or locked.get("type") not in TYPES:
        return None
    if not all(
        isinstance(key, str) and isinstance(value, str | int | bool)
        for key, value in locked.items()
    ):
        return None
    return dict(locked)


def mask(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def find_nix(environ: Mapping[str, str]) -> str | None:
    path = os.pathsep.join([environ.get("PATH") or os.defpath, *NIX_PATHS])
    return shutil.which("nix", path=path)


def _private_file(path: str, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def input_env(
    item: Mapping[str, Any], scratch: str, environ: Mapping[str, str]
) -> dict[str, str]:
    """The environment of ``item``'s ``nix`` process (its credential files
    written under ``scratch``)."""
    env = {
        key: value
        for key, value in environ.items()
        if not key.startswith(("GIT_ASKPASS", "PICELI_ASKPASS", "GIT_CONFIG_"))
    }
    env["XDG_CACHE_HOME"] = os.path.join(scratch, "cache")
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    mode = item.get("mode")
    if mode == "askpass":
        own = os.path.join(scratch, f"credential-{item['index']}")
        os.mkdir(own, 0o700)
        user, token, script = (os.path.join(own, n) for n in ("user", "token", "ask"))
        _private_file(user, str(item.get("username") or "git"))
        _private_file(token, str(item.get("token") or ""))
        _private_file(script, _ASKPASS)
        os.chmod(script, 0o700)
        env.update(
            GIT_ASKPASS=script,
            PICELI_ASKPASS_USER=user,
            PICELI_ASKPASS_TOKEN=token,
            # No helper stores the token, or answers with another credential.
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="credential.helper",
            GIT_CONFIG_VALUE_0="",
        )
    elif mode == "ssh-key":
        own = os.path.join(scratch, f"credential-{item['index']}")
        os.mkdir(own, 0o700)
        key, known = os.path.join(own, "id"), os.path.join(own, "known_hosts")
        _private_file(key, str(item.get("key") or "").rstrip("\n") + "\n")
        pinned = str(item.get("known_hosts") or "")
        _private_file(known, pinned)
        check = "yes" if pinned.strip() else "accept-new"
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
            f"-o UserKnownHostsFile={known} -o StrictHostKeyChecking={check}"
        )
    elif mode == "access-token":
        line = f"access-tokens = {item.get('host')}={item.get('token')}"
        env["NIX_CONFIG"] = "\n".join(
            part for part in (environ.get("NIX_CONFIG"), line) if part
        )
    return env


def reason(lines: Sequence[str]) -> str:
    """The line that says why: Git's ``fatal:``, else Nix's last ``error:``
    (also inside an abort's ``what():``), else docker's, else the last."""
    stripped = [line.strip() for line in lines if line.strip()]
    fatal = [line for line in stripped if line.startswith("fatal:")]
    errors = [
        line[line.index("error:") :]
        for line in stripped
        if "error:" in line and line[line.index("error:") + 6 :].strip()
    ]
    docker = [line for line in stripped if line.startswith("docker:")]
    found = (fatal or errors or docker or stripped or ["nix printed nothing"])[-1]
    return found[:300]


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def fetch(
    request: Mapping[str, Any],
    *,
    runner: Runner = subprocess.run,
    emit: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
    environ: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Fetch every input of ``request`` in order; the result (never a secret)."""
    items = [item for item in request.get("inputs") or () if isinstance(item, dict)]
    secrets = [str(item.get(k) or "") for item in items for k in ("token", "key")]
    nix = find_nix(environ)
    if nix is None:
        return {"state": "failed", "reason": "dev-tool-missing", "inputs": []}
    done: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="piceli-nix-inputs-") as scratch:
        os.chmod(scratch, 0o700)
        for index, item in enumerate(items):
            name = str(item.get("name") or "")
            locked = check_locked(item.get("locked"))
            if locked is None or item.get("mode") not in MODES:
                return {"state": "failed", "input": name, "failure": "invalid",
                        "detail": "not a locked input it fetches", "inputs": done}  # fmt: skip
            env = input_env({**item, "index": index}, scratch, environ)
            try:
                result = runner(
                    eval_argv(nix, locked), env=env, capture_output=True,
                    timeout=FETCH_SECONDS,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                return {"state": "failed", "input": name, "failure": "timeout",
                        "detail": f"no answer in {FETCH_SECONDS}s", "inputs": done}  # fmt: skip
            finally:
                shutil.rmtree(os.path.join(scratch, f"credential-{index}"), True)
            lines = [
                mask(raw, secrets)
                for raw in (result.stderr or b"")
                .decode("utf-8", "replace")
                .splitlines()
                if raw.strip()
            ][-TAIL_LINES:]
            for line in lines:
                emit(line)
            path = (result.stdout or b"").decode("utf-8", "replace").strip()
            if result.returncode != 0 or not path.startswith("/nix/store/"):
                return {"state": "failed", "input": name, "failure": "unreachable",
                        "detail": reason(lines), "inputs": done}  # fmt: skip
            # 0.26.3: the tree is the lock's, checked here by its own files.
            expected = sri(str(locked.get("narHash") or ""))
            hashed = runner(hash_argv(nix, path), env=env, capture_output=True,
                            timeout=FETCH_SECONDS)  # fmt: skip
            actual = sri((hashed.stdout or b"").decode("utf-8", "replace"))
            if hashed.returncode != 0 or not actual.startswith("sha256-"):
                return {"state": "failed", "input": name, "failure": "unreachable",
                        "detail": "nix could not hash the fetched tree: " + reason(
                            (hashed.stderr or b"").decode("utf-8", "replace").splitlines()
                        ), "inputs": done}  # fmt: skip
            if actual != expected:
                return {"state": "failed", "input": name, "failure": "mismatch",
                        "detail": f"the fetched tree is not the one flake.lock "
                        f"locks: narHash expected {expected or 'none'}, got {actual}",
                        "expected": expected, "actual": actual, "inputs": done}  # fmt: skip
            entry: dict[str, Any] = {"name": name, "store_path": path,
                                     "nar_hash": actual}  # fmt: skip
            rooted = _root(
                runner,
                nix,
                request.get("roots"),
                f"{index}-{name or 'input'}",
                path,
                env,
            )
            if rooted is not None:
                entry["rooted"] = rooted
                if not rooted:
                    emit(f"warning: no GC root for input {name}; a garbage "
                         "collection before the build would take it")  # fmt: skip
            done.append(entry)
        lock = request.get("lock")
        if not isinstance(lock, str) or not lock.strip():
            return {"state": "fetched", "inputs": done}
        # 0.26.3: the build gets the fetched trees in a reference lock in the
        # store, so it never resolves (or refetches) the inputs itself.
        try:
            text = reference_lock(lock, {e["name"]: e["store_path"] for e in done})
        except (ValueError, TypeError) as error:
            return {"state": "failed", "input": done[0]["name"] if done else "",
                    "failure": "invalid", "detail": f"no reference lock: {error}",
                    "inputs": done}  # fmt: skip
        file = os.path.join(scratch, LOCK_NAME)
        _private_file(file, text)
        added = runner(lock_argv(nix, file), env=dict(environ), capture_output=True,
                       timeout=600)  # fmt: skip
        stored = (added.stdout or b"").decode("utf-8", "replace").strip()
        if added.returncode != 0 or not stored.startswith("/nix/store/"):
            return {"state": "failed", "input": done[0]["name"] if done else "",
                    "failure": "invalid", "detail": "nix did not store the reference "
                    "lock: " + reason((added.stderr or b"").decode(
                        "utf-8", "replace").splitlines()),
                    "inputs": done}  # fmt: skip
        outcome: dict[str, Any] = {"state": "fetched", "inputs": done, "lock": stored}
        rooted = _root(runner, nix, request.get("roots"), "lock", stored, dict(environ))
        if rooted is not None:
            outcome["lock_rooted"] = rooted
    return outcome


def _root(
    runner: Runner, nix: str, roots: Any, name: str, path: str,
    env: Mapping[str, str],
) -> bool | None:  # fmt: skip
    """An indirect GC root ``ROOTS/NAME`` of ``path``, held until the client
    removes ``roots`` after the build: ``None`` without ``roots``."""
    if not isinstance(roots, str) or not roots:
        return None
    link = os.path.join(roots, name)
    os.makedirs(roots, mode=0o700, exist_ok=True)
    rooted = runner(root_argv(nix, link, path), env=dict(env), capture_output=True,
                    timeout=600)  # fmt: skip
    return rooted.returncode == 0


def run(
    request: Mapping[str, Any],
    *,
    runner: Runner = subprocess.run,
    emit: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
    environ: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """One request: ``release`` a roots directory, ``hold`` one root, else
    :func:`fetch` the inputs (0.26.2)."""
    released = request.get("release")
    if isinstance(released, str) and os.path.isabs(released):
        shutil.rmtree(released, ignore_errors=True)
        return {"state": "released"}
    hold = request.get("hold")
    if isinstance(hold, Mapping):
        link, path = str(hold.get("link") or ""), str(hold.get("path") or "")
        nix = find_nix(environ)
        if not (os.path.isabs(link) and path.startswith("/nix/store/")) or not nix:
            return {"state": "failed", "detail": "nothing to hold, or no nix"}
        os.makedirs(os.path.dirname(link), mode=0o700, exist_ok=True)
        done = runner(root_argv(nix, link, path), capture_output=True, timeout=600)
        if done.returncode != 0:
            for line in (done.stderr or b"").decode("utf-8", "replace").splitlines():
                emit(line)
            return {"state": "failed", "detail": "nix refused the root"}
        return {"state": "held"}
    return fetch(request, runner=runner, emit=emit, environ=environ)


def main() -> int:
    def stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(143)  # the temporary directory still goes

    signal.signal(signal.SIGTERM, stop)
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        request = {}
    outcome = run(request if isinstance(request, dict) else {})
    print(RESULT_MARKER + json.dumps(outcome, sort_keys=True), flush=True)
    return 0 if outcome.get("state") in ("fetched", "held", "released") else 1


if __name__ == "__main__":  # pragma: no cover - runs on the host
    raise SystemExit(main())
