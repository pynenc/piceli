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
- ``none``: the host's own access (a public input, or its own Git
  credential helpers and SSH agent on a native host).

Nix's fetcher cache (``XDG_CACHE_HOME``) is in the temporary directory
too, so no clone of a private repository stays on the host; the directory is
removed on every exit. The fetched trees land in the host's store (through
its daemon), where the build finds them by their ``narHash`` without
fetching. Output lines are masked; the one result line
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
MODES = ("askpass", "access-token", "none")
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


def fetch_expr(locked: Mapping[str, Any]) -> str:
    """The expression whose value is the store path of ``locked``'s tree."""
    body = json.dumps(dict(locked), sort_keys=True)
    return f"(builtins.fetchTree (builtins.fromJSON {nix_string(body)})).outPath"


def eval_argv(nix: str, locked: Mapping[str, Any]) -> list[str]:
    return [nix, *FEATURES, "eval", "--raw", "--expr", fetch_expr(locked)]


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
    elif mode == "access-token":
        line = f"access-tokens = {item.get('host')}={item.get('token')}"
        env["NIX_CONFIG"] = "\n".join(
            part for part in (environ.get("NIX_CONFIG"), line) if part
        )
    return env


def reason(lines: Sequence[str]) -> str:
    """The line that says why: Git's ``fatal:``, else Nix's last ``error:``."""
    stripped = [line.strip() for line in lines]
    fatal = [line for line in stripped if line.startswith("fatal:")]
    errors = [line for line in stripped if line.startswith("error:") and line[6:]]
    found = (fatal or errors or stripped or ["nix printed nothing"])[-1]
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
    secrets = [str(item.get("token") or "") for item in items]
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
            done.append({"name": name, "store_path": path})
    return {"state": "fetched", "inputs": done}


def main() -> int:
    def stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(143)  # the temporary directory still goes

    signal.signal(signal.SIGTERM, stop)
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        request = {}
    outcome = fetch(request if isinstance(request, dict) else {})
    print(RESULT_MARKER + json.dumps(outcome, sort_keys=True), flush=True)
    return 0 if outcome.get("state") == "fetched" else 1


if __name__ == "__main__":  # pragma: no cover - runs on the host
    raise SystemExit(main())
