"""Private flake inputs of a Nix build, fetched on the build host first (0.26.0).

A flake whose ``flake.lock`` pins a private input (``app =
git+ssh://git@github.com/org/app``) cannot be built where nothing gives Nix
a Git credential: the build run on a build host holds none, by design. So
before the build, the build host fetches those inputs itself, into its own
Nix store, with a Git credential it holds in its own Piceli secrets; the
build then finds each one in the store by its ``narHash`` and fetches
nothing. The credential never reaches the build run, the image, the build
log, the cache or the published closure.

**Which inputs** (:func:`covered`): the input of the release's promotion
gate (``Promotion(input=)``); every input whose locked URL is the
repository of a :class:`~piceli.infra.Source` of the composition (by host
and path: ``git+ssh://git@github.com/org/app``,
``git+https://github.com/org/app.git``, ``github:org/app`` and
``git@github.com:org/app`` are one repository); and the inputs the release
declares, ``NixRelease(inputs={"lib": "lib-token"})``. Each uses the build
host's credential named in ``inputs=``, else :data:`DEFAULT_CREDENTIAL`.
Other inputs are public: the build fetches them as before.

**The credential** is a ``git-token`` in the build host's Piceli secrets
(``piceli secrets git-token NAME --host github.com --prompt``; it reaches
the host as the cache's keys do, ``piceli secrets backup --only NAME`` and
``piceli secrets restore``). It is only used for an input on its own host:
over HTTPS through ``GIT_ASKPASS`` (a ``git+ssh`` input is fetched from the
same repository over HTTPS), or as Nix's ``access-tokens`` for
``github:``/``gitlab:``/``sourcehut:``. An input without a stored
credential is fetched with the host's own access (a public input, or a
native host's own credential helpers and SSH agent).

**Where:** :mod:`piceli.dev.nix_fetch` runs on the build host, as the
publisher does, with the request on its stdin. An input it cannot fetch
refuses before any build: ``infra-nix-input-unreachable: app
git+ssh://git@github.com/org/app (credential git): <Git's reason>``.

The controller never sends a credential: its plan names the inputs and the
credential names (:func:`covered`), the build host checks them against the
``flake.lock`` of the commit it fetched (:func:`resolve`) and loads the
credentials itself.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from piceli.dev.model import DevError

LOCK_FILE = "flake.lock"
#: The build host's credential of an input ``inputs=`` does not name.
DEFAULT_CREDENTIAL = "git"
CODE = "infra-nix-input-unreachable"
KIND = "git-token"
_INPUT = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]{0,99}")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_SCP = re.compile(r"^(?:[^@/]+@)?([^/:]+):(?!//)(.+)$")
#: Each forge's default host, for ``github:``/``gitlab:``/``sourcehut:``.
FORGES = {"github": "github.com", "gitlab": "gitlab.com", "sourcehut": "git.sr.ht"}


def _refuse(message: str) -> DevError:
    return DevError("infra-nix-input-unreachable", message)


# ---------------------------------------------------------------- flake.lock


def lock_nodes(lock: bytes | str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """``(nodes, root inputs)`` of a ``flake.lock`` (``{}`` for none):
    every node with ``locked`` attributes by its key, and the root's
    inputs by name (``{name: node key}``)."""
    if lock is None:
        return {}, {}
    try:
        parsed = json.loads(lock)
    except ValueError:
        return {}, {}
    nodes = parsed.get("nodes") if isinstance(parsed, Mapping) else None
    if not isinstance(nodes, Mapping):
        return {}, {}
    root = nodes.get(str(parsed.get("root") or "root")) or {}
    inputs = root.get("inputs") if isinstance(root, Mapping) else None
    found = {
        str(key): dict(node)
        for key, node in nodes.items()
        if isinstance(node, Mapping) and isinstance(node.get("locked"), Mapping)
    }
    names = {
        str(name): str(key)
        for name, key in (inputs or {}).items()
        if isinstance(key, str) and key in found
    }
    return found, names


def _hostname(text: str) -> str:
    return text.rsplit("@", 1)[-1].split(":", 1)[0].lower()


def url_location(url: str) -> tuple[str, str] | None:
    """``(host, path)`` of a Git URL, normalised: lower case, without the
    user, the port, ``.git`` and slashes around the path. A local path or
    ``file://`` URL has host ``""``."""
    text = url.strip()
    for prefix in ("git+", "hg+"):
        text = text.removeprefix(prefix)
    scp = _SCP.match(text) if "://" not in text else None
    if scp is not None and not text.startswith("/"):
        host, path = scp.group(1).lower(), scp.group(2)
    else:
        parsed = urllib.parse.urlsplit(text)
        if parsed.scheme in ("", "file"):
            host, path = "", parsed.path or text
        elif parsed.scheme in ("http", "https", "ssh", "git") and parsed.hostname:
            host, path = parsed.hostname.lower(), parsed.path
        else:
            return None
    path = urllib.parse.unquote(path).strip("/").lower().removesuffix(".git")
    if not path:
        return None
    return host, ("/" + path if host == "" else path)


def location(locked: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(host, path)`` of a locked input, as :func:`url_location` gives a URL."""
    kind = locked.get("type")
    if kind in ("git", "hg") and isinstance(locked.get("url"), str):
        return url_location(str(locked["url"]))
    if kind in FORGES and locked.get("owner") and locked.get("repo"):
        host = str(locked.get("host") or FORGES[str(kind)]).lower()
        owner = urllib.parse.unquote(str(locked["owner"])).lower()
        repo = str(locked["repo"]).lower().removesuffix(".git")
        return host, f"{owner}/{repo}"
    return None


def safe_url(text: str) -> str:
    """``text`` without a password or a token in it (a user name before
    ``@`` stays for ``ssh``), and without a query."""
    parsed = urllib.parse.urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text.split("?", 1)[0]
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    user = parsed.username if parsed.scheme == "ssh" else None
    if user:
        netloc = f"{user}@{netloc}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def display(locked: Mapping[str, Any]) -> str:
    """The input as a flake reference without its pin or any credential:
    ``git+ssh://git@github.com/org/app``, ``github:org/app``."""
    kind = str(locked.get("type") or "?")
    if kind in ("git", "hg", "tarball", "file") and isinstance(locked.get("url"), str):
        url = safe_url(str(locked["url"]))
        return url if kind in ("tarball", "file") else f"{kind}+{url}"
    if kind in FORGES:
        host = locked.get("host")
        suffix = f"?host={host}" if host else ""
        return f"{kind}:{locked.get('owner')}/{locked.get('repo')}{suffix}"
    return kind


# ---------------------------------------------------------------- coverage


def check_declared(inputs: Any, what: str = "inputs=") -> dict[str, str]:
    """``{input: credential name}`` as declared (``NixRelease(inputs=)``,
    ``--input``). :raises ValueError: a name that is no input or no
    credential name."""
    from piceli.profiles import ProfileError, check_name

    if inputs is None:
        return {}
    if not isinstance(inputs, Mapping):
        raise ValueError(f"{what} maps flake inputs to credential names")
    found: dict[str, str] = {}
    for name, credential in inputs.items():
        if not isinstance(name, str) or not _INPUT.fullmatch(name):
            raise ValueError(f"{what}: {name!r} is not a flake input's name")
        try:
            check_name(str(credential))
        except ProfileError:
            raise ValueError(
                f"{what}: {credential!r} is not a credential name"
            ) from None
        found[name] = str(credential)
    return dict(sorted(found.items()))


def covered(
    lock: bytes | str | None,
    *,
    gate: str | None = None,
    sources: Iterable[tuple[str, str]] = (),
    declared: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """The inputs of ``lock`` the build host fetches first, in order (the
    gate's, then by input): ``input`` (the node's key in ``flake.lock``),
    ``url`` (:func:`display`), ``credential`` (a credential name on the
    build host) and ``why`` (``gate``, ``declared`` or ``source:KEY``).

    ``sources``: the composition's ``(key, url)``; ``declared``:
    ``{input: credential}``, a root input's name or a node's key. Names
    ``flake.lock`` does not lock are left out (nothing to fetch)."""
    nodes, names = lock_nodes(lock)
    declared = dict(declared or {})
    wanted: dict[str, tuple[str, str]] = {}
    if gate is not None and (key := names.get(gate, gate)) in nodes:
        wanted[key] = ("gate", declared.get(gate, DEFAULT_CREDENTIAL))
    places = {
        place: key
        for key, url in sources
        if (place := url_location(url)) is not None and place[0]
    }
    for key, node in sorted(nodes.items()):
        place = location(node["locked"])
        if key not in wanted and place is not None and place in places:
            wanted[key] = (f"source:{places[place]}", DEFAULT_CREDENTIAL)
    for name, credential in declared.items():
        key = names.get(name, name)
        if key in nodes:
            why = wanted.get(key, ("declared", ""))[0]
            wanted[key] = (why, credential)
    order = sorted(wanted, key=lambda key: (wanted[key][0] != "gate", key))
    return [
        {
            "input": key,
            "url": display(nodes[key]["locked"]),
            "credential": wanted[key][1],
            "why": wanted[key][0],
        }
        for key in order
    ]


def resolve(
    entries: Sequence[Mapping[str, Any]], lock: bytes | str | None
) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """``(entry, locked)`` of each covered input, its locked attributes read
    from ``lock`` (the build host's own checkout of the commit): an entry
    whose input ``flake.lock`` does not lock, or locks at another URL, is
    refused (``infra-nix-input-unreachable``), never fetched."""
    from piceli.profiles import ProfileError, check_name

    nodes, _ = lock_nodes(lock)
    found = []
    for entry in entries:
        name, url = str(entry.get("input") or ""), str(entry.get("url") or "")
        node = nodes.get(name)
        if node is None:
            raise _refuse(f"{name} {url}: {LOCK_FILE} at the commit does not lock it")
        locked = dict(node["locked"])
        if display(locked) != url:
            raise _refuse(
                f"{name} {url}: {LOCK_FILE} at the commit locks {display(locked)}"
            )
        try:
            check_name(str(entry.get("credential")))
        except ProfileError:
            raise _refuse(f"{name} {url}: no credential name") from None
        found.append(
            (
                {
                    "input": name,
                    "url": url,
                    "credential": str(entry["credential"]),
                    "why": str(entry.get("why") or "declared"),
                },
                locked,
            )
        )
    return found


def check_entries(value: Any) -> list[dict[str, str]]:
    """A plan's ``inputs`` as a build host receives it.

    :raises ValueError: a malformed one."""
    if value is None:
        return []
    keys = {"input", "url", "credential", "why"}
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping)
        and set(item) == keys
        and all(isinstance(item[k], str) for k in keys)
        for item in value
    ):
        raise ValueError("the plan's inputs field is malformed")
    return [dict(item) for item in value]


# ---------------------------------------------------------------- credentials


def save_git_token(name: str, host: str, username: str, token: str) -> Any:
    """Store a Git token for ``host`` under ``name`` (``0600``, never printed)."""
    from piceli.infra.machines.credentials import _write, check_value
    from piceli.infra.machines.model import InfraError

    check_value(KIND, token)
    if not _HOST.fullmatch(host.lower()):
        raise InfraError("infra-credential-invalid", "--host is a host name")
    if (
        not username
        or len(username) > 256
        or any(c.isspace() or not c.isprintable() for c in username)
    ):
        raise InfraError(
            "infra-credential-invalid", "--username is printable, without spaces"
        )
    return _write(
        name,
        {
            "schema": "piceli.credential.v1",
            "kind": KIND,
            "value": token,
            "host": host.lower().split(":", 1)[0],
            "username": username,
        },
    )


def load_git_token(name: str) -> dict[str, str]:
    """``{host, username, token}`` of the ``git-token`` ``name``.

    :raises InfraError: missing, unsafe or another kind."""
    from piceli.infra.machines.credentials import _path, load_credential

    token = load_credential(name, KIND)
    body = json.loads(_path(name).read_text())
    return {
        "host": str(body.get("host") or ""),
        "username": str(body.get("username") or "git"),
        "token": token,
    }


def _https(locked: Mapping[str, Any], host: str) -> str | None:
    """The HTTPS URL of a ``git+ssh`` (or scp-like, ``git://``) input."""
    url = str(locked.get("url") or "")
    scp = _SCP.match(url) if "://" not in url else None
    if scp is not None:
        path = scp.group(2)
    else:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("ssh", "git"):
            return None
        path = parsed.path
    return f"https://{host}/{path.lstrip('/')}"


def plan_fetch(
    pairs: Sequence[tuple[Mapping[str, str], Mapping[str, Any]]],
    *,
    load: Callable[[str], dict[str, str]] = load_git_token,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The fetcher's request (with the tokens: only for its stdin) and what
    each input tried (no secret): ``input``, ``url``, ``credential`` and
    ``tried`` (how it is fetched, said in a failure)."""
    from piceli.infra.machines.model import InfraError

    items: list[dict[str, Any]] = []
    tried: list[dict[str, str]] = []
    cache: dict[str, Any] = {}
    for entry, locked in pairs:
        name = entry["credential"]
        if name not in cache:
            try:
                cache[name] = load(name)
            except InfraError as error:
                cache[name] = error
        credential = cache[name]
        place = location(locked)
        kind = str(locked.get("type"))
        item: dict[str, Any] = {"name": entry["input"], "locked": dict(locked),
                                "mode": "none"}  # fmt: skip
        if isinstance(credential, InfraError):
            how = (
                f"credential {name} is not stored on this host"
                if credential.code == "infra-credential-missing"
                else f"credential {name} is unusable ({credential.code})"
            ) + ", so the host's own access"
        elif place is None or place[0] != credential["host"]:
            how = (
                f"credential {name} is for {credential['host']}, not "
                f"{(place[0] or 'a local path') if place else 'this input'}, so "
                "the host's own access"
            )
        elif kind in FORGES:
            token = credential["token"]
            if kind == "gitlab" and ":" not in token:
                token = f"PAT:{token}"
            item.update(mode="access-token", host=place[0], token=token)
            how = f"credential {name}"
        elif kind == "git":
            url = str(locked.get("url") or "")
            rewritten = not url.startswith(("https://", "http://"))
            https = _https(locked, place[0]) if rewritten else url
            if https is None:
                how = f"credential {name} applies to no {url.split(':', 1)[0]} URL"
            else:
                item["locked"]["url"] = https
                item.update(
                    mode="askpass",
                    host=place[0],
                    username=credential["username"],
                    token=credential["token"],
                )
                how = f"credential {name}" + (" over HTTPS" if rewritten else "")
        else:
            how = f"credential {name} applies to no {kind} input"
        items.append(item)
        tried.append({"input": entry["input"], "url": entry["url"],
                      "credential": name, "tried": how})  # fmt: skip
    return {"inputs": items}, tried


# ---------------------------------------------------------------- run


def fetcher_source() -> str:
    """The stdlib fetcher (:mod:`piceli.dev.nix_fetch`) as one script."""
    from pathlib import Path

    from piceli.dev import nix_fetch

    return Path(nix_fetch.__file__).read_text()


def fetcher_argv(host: Any, image: str) -> list[str]:
    """How the fetcher runs on ``host`` before the build: a native host's
    Python, or a short container of the run image with the host's Nix
    mounted as for the run (never on a cluster node)."""
    import sys

    source = fetcher_source()
    if host.kind == "native":
        return [sys.executable, "-I", "-c", source]
    if host.kind == "docker":
        from piceli.dev.hosts import NIX_MOUNT, ContainerCli

        return ContainerCli(host).argv(
            "run", "--rm", "-i", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", *NIX_MOUNT, "-e", "HOME=/tmp",
            "--entrypoint", "python3", host.image or image, "-I", "-c", source,
        )  # fmt: skip
    raise DevError(
        "dev-nix-host-unsupported",
        f"build host {host.name} is a cluster node; Nix builds run on docker "
        "or native hosts",
    )


def run_fetcher(
    argv: list[str],
    request: Mapping[str, Any],
    tried: Sequence[Mapping[str, str]],
    *,
    runner: Callable[..., Any] | None = None,
    say: Callable[[str], None] = lambda _line: None,
) -> dict[str, Any]:
    """Run the fetcher with ``request`` on its stdin; its outcome.

    Its output is masked (no token reaches ``say``). :raises DevError:
    ``infra-nix-input-unreachable`` naming the input, its URL and the
    credential tried, when an input could not be fetched."""
    import subprocess

    from piceli.dev.nix_fetch import FETCH_SECONDS, RESULT_MARKER, mask

    items = list(request.get("inputs") or ())
    secrets = [str(item.get("token") or "") for item in items]
    try:
        done = (runner or subprocess.run)(
            argv,
            input=json.dumps(dict(request)).encode(),
            capture_output=True,
            timeout=FETCH_SECONDS * max(1, len(items)) + 600,
        )
    except (OSError, subprocess.TimeoutExpired):
        first = tried[0] if tried else {}
        raise _refuse(
            f"{first.get('input')} {first.get('url')}: the fetcher could not run "
            "on the build host"
        ) from None
    for line in (done.stderr or b"").decode("utf-8", "replace").splitlines()[-60:]:
        say(f"  | {mask(line, secrets)}")
    outcome: dict[str, Any] = {}
    for line in (done.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                outcome = json.loads(line[len(RESULT_MARKER) :])
            except ValueError:
                outcome = {}
    if outcome.get("state") == "fetched":
        return outcome
    failed = str(outcome.get("input") or "")
    entry = next(
        (item for item in tried if item["input"] == failed),
        tried[0] if tried else {"input": "?", "url": "?", "tried": "?"},
    )
    reason = outcome.get("reason")
    detail = (
        "no nix on the build host"
        if reason == "dev-tool-missing"
        else mask(str(outcome.get("detail") or "the fetch failed"), secrets)
    )
    raise _refuse(f"{entry['input']} {entry['url']} ({entry['tried']}): {detail}")


def fetch_inputs(host: Any, image: str) -> Callable[..., dict[str, Any]]:
    """``(pairs, say) -> outcome``: fetch the covered inputs (:func:`resolve`)
    on ``host`` with this machine's credentials (:func:`plan_fetch`)."""

    def run(
        pairs: Sequence[tuple[Mapping[str, str], Mapping[str, Any]]],
        say: Callable[[str], None],
    ) -> dict[str, Any]:
        request, tried = plan_fetch(pairs)
        for item in tried:
            say(f"input {item['input']} {item['url']} ({item['tried']})")
        outcome = run_fetcher(fetcher_argv(host, image), request, tried, say=say)
        del request
        return outcome

    return run
