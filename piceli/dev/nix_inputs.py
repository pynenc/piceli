"""Private flake inputs of a Nix build, fetched on the build host first (0.26.0).

A flake whose ``flake.lock`` pins a private input (``app =
git+ssh://git@github.com/org/app``) cannot be built where nothing gives Nix
a Git credential: the build run on a build host holds none, by design. So
before the build, the build host fetches those inputs itself, into its own
Nix store, with a Git credential it holds in its own Piceli secrets; the
build then reads them from a reference lock pinning their trees in the
store (0.26.3, :mod:`piceli.dev.nix_fetch`) and fetches nothing. The
credential never reaches the build run, the image, the build
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
import subprocess
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


SSH_KIND = "git-ssh-key"


def save_git_ssh_key(
    name: str, host: str, key: str, known_hosts: str | None = None
) -> Any:
    """Store a deploy key (an OpenSSH private key) for ``host`` under
    ``name``; ``known_hosts``: that host's known_hosts lines (else its key is
    taken on first use, in a temporary file, every time)."""
    import base64

    from piceli.infra.machines.credentials import _write
    from piceli.infra.machines.model import InfraError

    if not _HOST.fullmatch(host.lower()):
        raise InfraError("infra-credential-invalid", "--host is a host name")
    if "PRIVATE KEY-----" not in key or len(key) > 16384:
        raise InfraError(
            "infra-credential-invalid", "the file is not an OpenSSH private key"
        )
    body: dict[str, object] = {
        "schema": "piceli.credential.v1",
        "kind": SSH_KIND,
        "value": base64.b64encode(key.encode()).decode(),
        "host": host.lower().split(":", 1)[0],
    }
    if known_hosts:
        body["known_hosts"] = known_hosts
    return _write(name, body)


def load_git_credential(name: str) -> dict[str, Any]:
    """A Git credential of this machine by name: a ``git-token`` (``kind``
    ``token``) or a ``git-ssh-key`` (``kind`` ``ssh-key``), with its host.

    :raises InfraError: missing, unsafe or another kind."""
    import base64

    from piceli.infra.machines.credentials import _path, load_credential, stored_kind

    _, kind = stored_kind(name)
    if kind == SSH_KIND:
        value = load_credential(name, SSH_KIND)
        body = json.loads(_path(name).read_text())
        return {
            "kind": "ssh-key",
            "host": str(body.get("host") or ""),
            "key": base64.b64decode(value).decode(),
            "known_hosts": str(body.get("known_hosts") or ""),
        }
    return {"kind": "token", **load_git_token(name)}


def credential_state(name: str) -> str:
    """``present``, ``missing`` or ``unusable`` (never the value)."""
    from piceli.infra.machines.model import InfraError

    try:
        load_git_credential(name)
    except InfraError as error:
        return "missing" if error.code == "infra-credential-missing" else "unusable"
    return "present"


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
    load: Callable[[str], dict[str, Any]] | None = None,
    controller: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The fetcher's request (with the secrets: only for its stdin) and what
    each input tried (no secret): ``input``, ``url``, ``credential`` and
    ``tried`` (how it is fetched, said in a failure).

    ``load``: a credential of this host by name (:func:`load_git_credential`);
    ``controller``: the credential the controller sealed for this build
    (``{"hosts", "username", "token"}``), for inputs named
    :data:`CONTROLLER`, only on its hosts."""
    from piceli.infra.machines.model import InfraError

    load = load or load_git_credential
    items: list[dict[str, Any]] = []
    tried: list[dict[str, str]] = []
    cache: dict[str, Any] = {}
    for entry, locked in pairs:
        name = entry["credential"]
        if name not in cache:
            if name == CONTROLLER:
                cache[name] = (
                    {"kind": "token", **dict(controller)}
                    if controller is not None
                    else "the controller sent no credential for this build"
                )
            else:
                try:
                    found = dict(load(name))
                    found.setdefault("kind", "token")
                    found.setdefault("hosts", [found.get("host")])
                    cache[name] = found
                except InfraError as error:
                    cache[name] = (
                        f"credential {name} is not stored on this host"
                        if error.code == "infra-credential-missing"
                        else f"credential {name} is unusable ({error.code})"
                    )
        credential = cache[name]
        place = location(locked)
        kind = str(locked.get("type"))
        url = str(locked.get("url") or "")
        item: dict[str, Any] = {"name": entry["input"], "locked": dict(locked),
                                "mode": "none"}  # fmt: skip
        label = (
            "the controller's credential"
            if name == CONTROLLER
            else (f"credential {name}")
        )
        if isinstance(credential, str):
            how = f"{credential}, so the host's own access"
        elif place is None or place[0] not in credential["hosts"]:
            how = (
                f"{label} is for {', '.join(map(str, credential['hosts']))}, not "
                f"{(place[0] or 'a local path') if place else 'this input'}, so "
                "the host's own access"
            )
        elif credential["kind"] == "ssh-key":
            if kind == "git" and not url.startswith(("https://", "http://")):
                item.update(
                    mode="ssh-key",
                    host=place[0],
                    key=credential["key"],
                    known_hosts=credential.get("known_hosts") or "",
                )
                how = f"{label} (a deploy key over SSH)"
            else:
                how = f"{label} is an SSH key; it applies to no {kind} input over HTTPS"
        elif kind in FORGES:
            token = credential["token"]
            if kind == "gitlab" and ":" not in token:
                token = f"PAT:{token}"
            item.update(mode="access-token", host=place[0], token=token)
            how = label
        elif kind == "git":
            rewritten = not url.startswith(("https://", "http://"))
            https = _https(locked, place[0]) if rewritten else url
            if https is None:
                how = f"{label} applies to no {url.split(':', 1)[0]} URL"
            else:
                item["locked"]["url"] = https
                item.update(
                    mode="askpass",
                    host=place[0],
                    username=credential.get("username") or "git",
                    token=credential["token"],
                )
                how = label + (" over HTTPS" if rewritten else "")
        else:
            how = f"{label} applies to no {kind} input"
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


def fetcher_argv(host: Any, image: str | None) -> list[str]:
    """How the fetcher runs on ``host`` before the build: a native host's
    Python, or a short container of the run image (``image``, unless the
    host names its own) with the host's Nix mounted as for the run, as the
    run's user (:mod:`piceli.dev.nix_step`; never on a cluster node).

    :raises DevError: ``dev-nix-invalid``, a docker host without an image."""
    from piceli.dev.nix_step import step_argv

    return step_argv(host, image, fetcher_source())


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
    from piceli.dev.nix_step import failure_tail

    items = list(request.get("inputs") or ())
    secrets = [str(item.get(k) or "") for item in items for k in ("token", "key")]
    try:
        done = (runner or subprocess.run)(
            argv,
            input=json.dumps(dict(request)).encode(),
            capture_output=True,
            timeout=FETCH_SECONDS * max(1, len(items)) + 600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        first = tried[0] if tried else {}
        why = (
            "it timed out"
            if isinstance(error, subprocess.TimeoutExpired)
            else f"{type(error).__name__}: {mask(str(error), secrets)}"
        )
        raise _refuse(
            f"{first.get('input')} {first.get('url')}: the fetcher could not run "
            f"on the build host ({why})"
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
    if not outcome:
        # 0.26.1: it never reported (docker refused the container, the step
        # or Nix aborted): say what docker or Nix said.
        detail = f"the fetch step ended without a result (exit {done.returncode}): " + (
            failure_tail(done.stderr, secrets) or "it printed nothing"
        )
    elif reason == "dev-tool-missing":
        detail = "no nix on the build host"
    else:
        detail = mask(
            str(outcome.get("detail") or "")
            or "the fetch step failed: "
            + (failure_tail(done.stderr, secrets) or "it printed nothing"),
            secrets,
        )
    raise _refuse(f"{entry['input']} {entry['url']} ({entry['tried']}): {detail}")


class InputFetch:
    """``fetch(pairs, say, controller=None, roots=None) -> outcome``: fetch
    the covered inputs (:func:`resolve`) on the host with this machine's
    credentials (:func:`plan_fetch`); with ``roots`` (from :meth:`roots`),
    each tree gets a GC root there, held until the build ended."""

    def __init__(
        self, host: Any, image: str | None, runner: Callable[..., Any] | None = None
    ) -> None:
        self.host, self.image, self.runner = host, image, runner
        self.argv = fetcher_argv(host, image)

    def roots(self) -> Any:
        """A context of where this build's input roots live, removed when it
        exits: a temporary directory on a native host, a directory under
        ``BuildHost.roots_dir`` on a docker host (0.26.2; removed by a step
        there), else ``None`` (no root)."""
        import contextlib
        import secrets
        from pathlib import Path

        @contextlib.contextmanager
        def held() -> Any:
            if self.host.kind == "native":
                from piceli.tempfiles import temporary_directory

                with temporary_directory("nix-input-roots") as directory:
                    yield Path(directory)
                return
            roots_dir = getattr(self.host, "roots_dir", None)
            if self.host.kind != "docker" or not roots_dir:
                yield None
                return
            path = f"{roots_dir.rstrip('/')}/inputs-{secrets.token_hex(8)}"
            try:
                yield path
            finally:
                self._step({"release": path}, lambda _line: None)

        return held()

    def _step(
        self, request: Mapping[str, Any], say: Callable[[str], None]
    ) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return (self.runner or subprocess.run)(
                self.argv, input=json.dumps(dict(request)).encode(),
                capture_output=True, timeout=600,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as error:
            say(f"warning: the GC root step did not run ({type(error).__name__})")
            return None

    def __call__(
        self,
        pairs: Sequence[tuple[Mapping[str, str], Mapping[str, Any]]],
        say: Callable[[str], None],
        controller: Mapping[str, Any] | None = None,
        roots: Any = None,
        lock: bytes | str | None = None,
    ) -> dict[str, Any]:
        """0.26.3: with ``lock`` (the commit's ``flake.lock``), the outcome's
        ``lock`` is a reference lock in the host's store pinning each
        fetched tree (:func:`reference_lock_of`)."""
        request, tried = plan_fetch(pairs, controller=controller)
        if roots is not None:
            request["roots"] = str(roots)
        if lock is not None:
            request["lock"] = lock.decode() if isinstance(lock, bytes) else str(lock)
        for item in tried:
            say(f"input {item['input']} {item['url']} ({item['tried']})")
        outcome = run_fetcher(self.argv, request, tried, say=say, runner=self.runner)
        del request
        return outcome


def reference_lock_of(outcome: Mapping[str, Any] | None) -> str | None:
    """The reference lock of a fetch's outcome (0.26.3): the store path the
    build reads with ``--reference-lock-file``, or ``None``."""
    found = (outcome or {}).get("lock")
    if (
        isinstance(found, str)
        and found.startswith("/nix/store/")
        and found.endswith("-flake.lock")
        and "/" not in found[len("/nix/store/") :]
        and not any(char.isspace() for char in found)
    ):
        return found
    return None


def fetch_inputs(
    host: Any, image: str | None, runner: Callable[..., Any] | None = None
) -> InputFetch:
    """The fetch of private inputs on ``host`` (:class:`InputFetch`).
    ``image``: the build run's (:func:`piceli.dev.nix_step.step_image`).

    :raises DevError: ``dev-nix-invalid``, now, for a docker host without
        an image (not a docker error at run time)."""
    return InputFetch(host, image, runner)


def hold_root(
    host: Any,
    image: str | None,
    name: str,
    store_path: str,
    say: Callable[[str], None],
    *,
    runner: Callable[..., Any] | None = None,
) -> str | None:
    """Root ``store_path`` (an ``--image`` build's) as ``ROOTS_DIR/images/NAME``
    on a docker host with ``roots_dir`` (0.26.2), until the next build of
    ``name`` replaces it, so a garbage collection before ``infra apply``'s
    upload cannot take it. The link, or ``None`` (no ``roots_dir``, or Nix
    refused: said)."""
    from piceli.dev.nix_fetch import RESULT_MARKER

    roots_dir = getattr(host, "roots_dir", None)
    if getattr(host, "kind", None) != "docker" or not roots_dir:
        return None
    link = f"{roots_dir.rstrip('/')}/images/{name}"
    done = InputFetch(host, image, runner)._step(
        {"hold": {"link": link, "path": store_path}}, say
    )
    lines = (done.stdout or b"").decode("utf-8", "replace") if done else ""
    for line in lines.splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                if json.loads(line[len(RESULT_MARKER) :]).get("state") == "held":
                    return link
            except (ValueError, AttributeError):
                pass
    say(f"warning: no GC root for image {name} on {host.name}; a garbage "
        "collection before infra apply's upload would take it")  # fmt: skip
    return None


# ---------------------------------------------------------------- per job

#: ``NixRelease(credentials=)``: whose Git credential fetches the inputs.
MODES = ("host", "controller")
#: The credential name of every input a ``controller`` release covers.
CONTROLLER = "controller"
SEAL_SCHEMA = "piceli.sealed-git-credential.v1"


def new_recipient() -> tuple[bytes, str]:
    """A one-claim X25519 key pair of a build host: the private key (kept in
    memory for that build only) and the public key it claims with."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519

    private = x25519.X25519PrivateKey.generate()
    raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    import base64

    return raw, base64.b64encode(public).decode()


def _key(shared: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=SEAL_SCHEMA.encode() + aad
    ).derive(shared)


def seal(recipient: str, payload: Mapping[str, Any], aad: bytes) -> dict[str, str]:
    """``payload`` encrypted to ``recipient`` (X25519, HKDF-SHA256,
    ChaCha20-Poly1305; ``aad``: the build and the host it is for)."""
    import base64
    import os

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    peer = x25519.X25519PublicKey.from_public_bytes(base64.b64decode(recipient))
    mine = x25519.X25519PrivateKey.generate()
    nonce = os.urandom(12)
    body = ChaCha20Poly1305(_key(mine.exchange(peer), aad)).encrypt(
        nonce, json.dumps(dict(payload), sort_keys=True).encode(), aad
    )
    sender = mine.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {
        "schema": SEAL_SCHEMA,
        "sender": base64.b64encode(sender).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "box": base64.b64encode(body).decode(),
    }


def unseal(private: bytes, sealed: Mapping[str, Any], aad: bytes) -> dict[str, Any]:
    """The payload of :func:`seal`. :raises ValueError: not for this key or
    this build, or tampered with."""
    import base64

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    try:
        if sealed.get("schema") != SEAL_SCHEMA:
            raise ValueError("not a sealed credential")
        mine = x25519.X25519PrivateKey.from_private_bytes(private)
        peer = x25519.X25519PublicKey.from_public_bytes(
            base64.b64decode(str(sealed["sender"]))
        )
        text = ChaCha20Poly1305(_key(mine.exchange(peer), aad)).decrypt(
            base64.b64decode(str(sealed["nonce"])),
            base64.b64decode(str(sealed["box"])),
            aad,
        )
        found = json.loads(text)
    except (KeyError, TypeError, ValueError, InvalidTag):
        raise ValueError("the sealed credential does not open here") from None
    if not isinstance(found, dict):
        raise ValueError("the sealed credential does not open here")
    return found


def seal_aad(build: str, host: str) -> bytes:
    return f"{build}\n{host}".encode()


def git_hosts(sources: Iterable[tuple[str, str]]) -> list[str]:
    """The hosts of the composition's sources: where the controller's Git
    credential is sent from a ``controller`` release (and nowhere else)."""
    return sorted(
        {
            place[0]
            for _key_, url in sources
            if (place := url_location(url)) is not None and place[0]
        }
    )


def read_controller_credential(directory: Any) -> dict[str, str] | None:
    """The username and HTTPS token of the controller's mounted Git Secret
    (``piceli secrets git``), ``None`` when it holds none."""
    from pathlib import Path

    if directory is None:
        return None
    root = Path(directory)
    token_file, user_file = root / "password", root / "username"
    if not token_file.is_file():
        return None
    token = token_file.read_text().strip()
    user = user_file.read_text().strip() if user_file.is_file() else "git"
    return {"username": user or "git", "token": token} if token else None


def host_check(
    status: Mapping[str, Any],
    host: str,
    state: Callable[[str], str] = credential_state,
) -> list[dict[str, Any]]:
    """What a build host needs for the NixReleases it publishes (``builder
    serve --check``): each release's credential mode and, per private input
    of its last plan, the credential and whether this host holds it
    (``present``, ``missing``, ``unusable``; ``per-build`` for the
    controller's). Never a value."""
    found = []
    releases = status.get("nix_releases") or {}
    for name, record in sorted(releases.items()):
        if not isinstance(record, Mapping) or record.get("builder") != host:
            continue
        plan = record.get("plan") or {}
        mode = str(plan.get("credentials") or "host")
        inputs = []
        for item in plan.get("inputs") or ():
            if not isinstance(item, Mapping):
                continue
            credential = str(item.get("credential") or "")
            inputs.append(
                {
                    "input": item.get("input"),
                    "url": item.get("url"),
                    "credential": credential,
                    "state": "per-build"
                    if credential == CONTROLLER
                    else state(credential),
                }
            )
        found.append(
            {
                "release": name,
                "credentials": mode,
                "tag": record.get("tag"),
                "planned": bool(plan),
                "inputs": inputs,
            }
        )
    return found
