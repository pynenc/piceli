"""Sign a Nix closure and push it to a binary cache, on the build host (0.24.0).

Standard library only: ``piceli dev nix build --publish`` runs this file's
source with ``python3 -I -c`` on the host that built the store path (a
native host's own Python, or a short ``docker run --rm -i`` of the run
image on a docker host), *after* the build run ended. It executes no code
of the project: only ``nix copy`` and ``nix path-info``.

Its input is one JSON object on **stdin** (never an argument or the
environment of the client): the store path, the cache's store URL, the
signing key and, for an ``s3://`` cache, the access keys. The key goes to a
``0600`` file in a ``0700`` temporary directory, named by the store URL's
``secret-key=`` parameter (``nix copy`` signs every path it uploads, so the
host's own store is never changed and its daemon need not trust the user),
and the directory is removed on every exit. The access keys reach ``nix``
through its child environment only. Output lines are masked; the one result
line (``PICELI-NIX-PUBLISH {json}``) carries hashes and names, never a
secret.

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

RESULT_MARKER = "PICELI-NIX-PUBLISH "
#: Where ``nix`` is on a multi-user install and on NixOS (also with only
#: ``/nix`` mounted into a container).
NIX_PATHS = (
    "/nix/var/nix/profiles/default/bin",
    "/nix/var/nix/profiles/system/sw/bin",
    "/run/current-system/sw/bin",
)
FEATURES = ("--extra-experimental-features", "nix-command flakes")
COPY_SECONDS = 6 * 3600


def nix_argv(nix: str, *args: str) -> list[str]:
    """``nix`` with the flake and ``nix`` command features on."""
    return [nix, *FEATURES, *args]


def with_secret_key(store_url: str, key_file: str) -> str:
    """The store URL that signs each path ``nix copy`` uploads with ``key_file``."""
    return store_url + ("&" if "?" in store_url else "?") + f"secret-key={key_file}"


def copy_argv(nix: str, store_url: str, key_file: str, store_path: str) -> list[str]:
    """``nix copy`` of ``store_path``'s closure, signed on upload."""
    return nix_argv(
        nix, "copy", "--to", with_secret_key(store_url, key_file), store_path
    )


def path_info_argv(
    nix: str, store_path: str, store: str | None = None, closure: bool = False
) -> list[str]:
    """``nix path-info --json`` (``-S``: with the closure's size in bytes)."""
    return nix_argv(
        nix, "path-info", "--json", *(("-S",) if closure else ()),
        *(("--store", store) if store else ()), store_path,
    )  # fmt: skip


def derivation_argv(nix: str, drv: str) -> list[str]:
    return nix_argv(nix, "derivation", "show", drv)


def path_info(text: str, store_path: str) -> dict[str, Any]:
    """The entry of ``store_path`` in ``nix path-info --json`` (a list before
    Nix 2.19, an object keyed by path since)."""
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    if isinstance(value, dict):
        entry = value.get(store_path)
        return dict(entry) if isinstance(entry, dict) else {}
    for entry in value if isinstance(value, list) else ():
        if isinstance(entry, dict) and entry.get("path") in (None, store_path):
            return dict(entry)
    return {}


def derivation_system(text: str) -> str | None:
    """``system`` of the first derivation in ``nix derivation show``."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("derivations"), dict):
        value = value["derivations"]
    for entry in value.values() if isinstance(value, dict) else ():
        if isinstance(entry, dict) and isinstance(entry.get("system"), str):
            return str(entry["system"])
    return None


def mask(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def find_nix(environ: Mapping[str, str]) -> str | None:
    path = os.pathsep.join([environ.get("PATH") or os.defpath, *NIX_PATHS])
    return shutil.which("nix", path=path)


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def publish(
    request: Mapping[str, Any],
    *,
    runner: Runner = subprocess.run,
    emit: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
    environ: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Sign and push ``request["store_path"]``; the result (never a secret)."""
    store_path = str(request.get("store_path") or "")
    store_url = str(request.get("store_url") or "")
    signing_key = str(request.get("signing_key") or "")
    s3 = request.get("s3") or {}
    secrets = [signing_key, str(s3.get("secret_access_key") or "")]
    result: dict[str, Any] = {"state": "failed", "store_path": store_path}
    nix = find_nix(environ)
    if nix is None:
        return {**result, "reason": "dev-tool-missing"}
    env = {k: v for k, v in environ.items() if not k.startswith("AWS_")}
    if s3:
        env["AWS_ACCESS_KEY_ID"] = str(s3.get("access_key_id") or "")
        env["AWS_SECRET_ACCESS_KEY"] = str(s3.get("secret_access_key") or "")
        env["AWS_EC2_METADATA_DISABLED"] = "true"

    def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        done = runner(argv, env=env, capture_output=True, timeout=timeout)
        for raw in (done.stderr or b"").decode("utf-8", "replace").splitlines()[-40:]:
            emit(mask(raw, secrets))
        return done

    with tempfile.TemporaryDirectory(prefix="piceli-nix-") as scratch:
        os.chmod(scratch, 0o700)
        key_file = os.path.join(scratch, "signing.key")
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(signing_key)
        del signing_key
        try:
            copied = run(copy_argv(nix, store_url, key_file, store_path), COPY_SECONDS)
        except subprocess.TimeoutExpired:
            return {**result, "reason": "dev-nix-publish-failed"}
        finally:
            os.unlink(key_file)
    if copied.returncode != 0:
        return {**result, "reason": "dev-nix-publish-failed"}
    # What the cache now serves: its signatures and the NAR's hash.
    remote = run(path_info_argv(nix, store_path, store_url), 600)
    info = path_info(remote.stdout.decode("utf-8", "replace"), store_path)
    if remote.returncode != 0 or not info.get("narHash"):
        return {**result, "reason": "dev-nix-publish-failed"}
    system = None
    local = path_info(
        run(path_info_argv(nix, store_path, closure=True), 600).stdout.decode(
            "utf-8", "replace"
        ),
        store_path,
    )
    if isinstance(local.get("deriver"), str):
        shown = run(derivation_argv(nix, local["deriver"]), 600)
        if shown.returncode == 0:
            system = derivation_system(shown.stdout.decode("utf-8", "replace"))
    return {
        "state": "published",
        "store_path": store_path,
        "nar_hash": str(info["narHash"]),
        "nar_size": int(info.get("narSize") or 0),
        "signatures": [str(s) for s in info.get("signatures") or ()],
        "system": system,
        "closure_size": int(local["closureSize"])
        if isinstance(local.get("closureSize"), int)
        else None,
    }


def main() -> int:
    def stop(_signum: int, _frame: Any) -> None:
        raise SystemExit(143)  # the temporary directory still goes

    signal.signal(signal.SIGTERM, stop)
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        request = {}
    outcome = publish(request if isinstance(request, dict) else {})
    print(RESULT_MARKER + json.dumps(outcome, sort_keys=True), flush=True)
    return 0 if outcome.get("state") == "published" else 1


if __name__ == "__main__":  # pragma: no cover - runs on the host
    raise SystemExit(main())
