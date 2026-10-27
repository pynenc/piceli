"""``piceli builder serve``: build what the GitOps controller waits for (0.21.0).

Runs on a build host (a laptop, a Mac mini, a Linux server) next to its
container engine, with the cluster owner's credential profile:

1. read the controller's status and take the oldest wanted build nobody
   holds (``builds_wanted``; see :mod:`piceli.infra.remote_builds`), claim it
   (``build-claim``) and wait until the controller confirms the claim;
2. fetch each source at its commit with this machine's own Git access into a
   mirror cache (``~/.cache/piceli/builder/git``): HTTPS with the user's
   credential helpers, else the same repository over SSH with the host's
   keys; check it out with its ``.git`` (``~/.cache/piceli/builder/checkouts``),
   so the build can verify its identity;
3. build it in a container of the builder image on the host's engine
   (``piceli build job-run --archives-only``: the same host-build code as the
   in-cluster Job, every platform; Rust for another architecture
   cross-compiles with ``cargo zigbuild``);
4. deliver the archives itself: push them to the cluster's registry through
   a port-forward (only the blobs it lacks) and one OCI index per image over
   its platforms; or (0.22.0, when the wanted build names nodes:
   ``Controller(delivery="nodes")``) import each image into every node's
   containerd over SSH (``ctr images import``), the archive of the node's
   platform, under the content-tagged name of
   :func:`piceli.infra.remote_builds.node_reference`;
5. answer ``built`` (each image's key, pull reference, digest and
   platforms, each node's import receipt when delivered into the nodes,
   and a receipt: host, times, image, commits) or ``build-failed`` with
   the output's tail.

0.25.0: a host started with ``--publish MODULE:ATTR`` (a ``NixCache`` whose
signing key, and S3 write keys, are in its own Piceli secrets) also takes
the ``nix-publish`` builds of the ``NixRelease`` s that name it as their
builder (:mod:`piceli.infra.nix_releases`): its claim carries the cache
key's signature over the claim, never a key; it fetches the flake's source
at the commit, checks that the tag points at it, builds the attribute,
signs and pushes the closure, uploads the manifest and the tag's pointer and
answers ``nix-published``. A publish to a cache it has no keys for is never
claimed. 0.25.0: with ``--publish-only`` it claims only those, whatever the
host's kind, never an image build (said once), and needs no registry.

0.22.0: each delivered image gets its provenance (host, builder image,
commits, times; :mod:`piceli.infra.provenance`) pushed as an OCI referrer
when the registry takes it. A build whose output shows a machine-health
signature (a crashed compiler, a segfault, a corrupt cached blob;
:mod:`piceli.dev.health`) records a health event on the host; a quarantined
host claims nothing and says why until ``piceli dev release-host``.

0.29.0: a host the controller's composition no longer declares claims
nothing and says so. A failure before the build's first command (a source
this host cannot fetch, an engine that cannot start the build) answers
``build-failed`` with ``early``: the controller hands the build to another
host or its fallback at once and quarantines this host for that source
until ``piceli gitops release-host``; this host skips the builds that need
it. :meth:`BuilderServe.take_next` (``--once --take-next``) stands by for
the next build and claims it before the in-cluster fallback.
:func:`check_sources` and :func:`scratch_check` are ``--check``'s
``git ls-remote`` of every source and write test inside the container.

0.33.0: the host claims only while it runs the controller's Piceli version
(``controller.version`` in its status). On a mismatch it claims nothing
(the controller falls back to its in-cluster build), tells the controller
its version (``build-host-version``: ``piceli builder status``, ``piceli
gitops hosts`` and the ``build-host-version`` alarm show it), writes the
controller's version into ``version_file`` when it has one, and raises
:class:`VersionMismatch`; ``builder serve`` then exits
:data:`EXIT_VERSION_MISMATCH` so its service upgrades and restarts it.
It builds in the controller's builder image (``controller.builder_image``,
the image of its own build Job), pulled again when it changes, unless
``--image`` pins another (said once).

Nothing here holds a Tailscale, registry or cluster credential of its own:
the profile reaches the API, the API reaches the registry; the nodes are
reached with this host's own SSH access (its keys, its agent).

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli import cli_contract
from piceli.infra import builds

#: The container engine's named volume of the build cache (cargo targets,
#: blobs, stage), per build host.
CACHE_VOLUME = "piceli-build-cache"
TAIL_LINES = 120
#: Lines of a failed publish's output kept for its answer.
TAIL_CHARS_LINES = 120
#: Where the builder container finds the sources: its own copy of the
#: read-only ``/work/src/<name>`` checkouts, in the build cache. The copy is
#: owned by the container's user, so Git verifies it without any exception
#: to its ownership check, and its stable path keeps the cargo targets valid
#: from one build to the next.
SOURCES = "/cache/builder-src"


#: ``builder serve``'s exit code when the controller runs another Piceli
#: version (0.33.0): ``EX_TEMPFAIL`` of ``sysexits.h``, "try again later",
#: here after installing the controller's version.
EXIT_VERSION_MISMATCH = cli_contract.EXIT_VERSION_MISMATCH


class VersionMismatch(Exception):
    """The controller runs another Piceli version than this host (0.33.0)."""

    def __init__(self, host: str, version: str, controller: str) -> None:
        super().__init__(
            f"build host {host} runs piceli {version}, the controller {controller}"
        )
        self.host = host
        self.version = version
        self.controller = controller


def _piceli_version() -> str:
    from piceli import __version__

    return str(__version__)


def write_version_file(path: Path, version: str) -> bool:
    """Write ``version`` (one line) into ``path`` unless it already holds it,
    through a temporary file and a rename; whether it changed (0.33.0)."""
    try:
        if path.read_text().strip() == version:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(version + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BuildFailed(Exception):
    """The build failed; ``tail`` is its output's end. ``early`` (0.29.0):
    before its first command (``source``: the one it could not fetch)."""

    def __init__(
        self,
        reason: str,
        tail: str = "",
        *,
        early: bool = False,
        source: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.tail = tail
        self.early = early
        self.source = source


#: ``docker run``'s own failure (the engine could not start the container).
ENGINE_FAILED = 125


def _slug(url: str) -> str:
    import hashlib

    name = re.sub(r"[^a-z0-9._-]+", "-", url.lower().rsplit("/", 1)[-1]).strip("-")
    return f"{name or 'source'}-{hashlib.sha256(url.encode()).hexdigest()[:10]}"


_HTTPS = re.compile(r"^https://(?:[^@/]+@)?([^/:]+)/(.+)$")


def fetch_urls(url: str) -> list[str]:
    """Where to fetch ``url`` from, in order: the URL itself, then, for an
    HTTPS URL, the same repository over SSH (``git@host:path``), so a private
    repository is fetched with the host's SSH keys when HTTPS has no
    credential. The rewrite applies to this process only."""
    found = [url]
    match = _HTTPS.match(url)
    if match:
        found.append(f"git@{match.group(1)}:{match.group(2)}")
    return found


def fetch_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Git's environment for fetches: never prompt (no terminal behind a build
    host), SSH in batch mode unless the user set their own SSH command. The
    user's Git config, credential helpers and SSH agent apply unchanged."""
    env = dict(os.environ if environ is None else environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _tail(result: subprocess.CompletedProcess[bytes]) -> str:
    raw = result.stderr if isinstance(result.stderr, bytes) else b""
    return raw.decode("utf-8", "replace").strip()[-500:]


def export_source(
    url: str,
    commit: str,
    into: Path,
    mirrors: Path,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """``commit`` of ``url`` as a Git checkout in ``into``.

    The build verifies each source's Git identity (its commit, a clean tree,
    its origin), so the checkout keeps its ``.git``: a shallow clone of the
    commit from this host's mirror (kept under ``mirrors``, so the next fetch
    is incremental), with ``origin`` the source's URL. ``into`` is kept
    between builds: files a commit does not change keep their times, so
    incremental builds (cargo) see exactly what changed. Everything is
    readable by others: the builder container's user reads it.

    :raises BuildFailed: the commit cannot be fetched or checked out.
    """
    mirror = mirrors / f"{_slug(url)}.git"
    env = fetch_env()
    if not (mirror / "HEAD").is_file():
        mirror.mkdir(parents=True, exist_ok=True)
        run(
            ["git", "init", "--quiet", "--bare", str(mirror)],
            check=False,
            capture_output=True,
        )
    # The checkout below asks the mirror for one commit by its id.
    run(
        ["git", "--git-dir", str(mirror), "config", "uploadpack.allowAnySHA1InWant", "true"],
        check=False, capture_output=True,
    )  # fmt: skip

    def present() -> bool:
        return run(
            ["git", "--git-dir", str(mirror), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False, capture_output=True,
        ).returncode == 0  # fmt: skip

    errors: list[str] = []
    for source in fetch_urls(url):
        if present():
            break
        # Every branch and tag first (later commits are then local), else the
        # commit alone (a server that serves unadvertised commits).
        for options, refs in (
            (["--prune"], ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]),
            ([], [commit]),
        ):
            if present():
                break
            fetched = run(
                ["git", "--git-dir", str(mirror), "fetch", "--quiet", *options, source,
                 *refs],
                check=False, capture_output=True, timeout=1800, env=env,
            )  # fmt: skip
            if fetched.returncode != 0 and _tail(fetched):
                errors.append(f"{source}: {_tail(fetched)}")
    if not present():
        detail = f" ({errors[-1]})" if errors else ""
        raise BuildFailed(
            f"commit {commit[:12]} of {url} cannot be fetched from this host"
            f" over HTTPS or SSH{detail}"
        )

    def git(*args: str) -> subprocess.CompletedProcess[bytes]:
        return run(
            ["git", "-C", str(into), *args], check=False, capture_output=True,
            timeout=1800,
        )  # fmt: skip

    if not (into / ".git").is_dir():
        shutil.rmtree(into, ignore_errors=True)
        into.mkdir(parents=True)
        git("init", "--quiet")
    git("remote", "remove", "origin")
    git("remote", "add", "origin", url)
    # Files whose recorded times went stale (the chmod below changes their
    # ctime) but whose content did not change are left as they are: without
    # the refresh, the forced checkout rewrites every file.
    git("update-index", "-q", "--refresh")
    steps = (
        ("fetch", "--quiet", "--depth", "1", "--no-tags", f"file://{mirror}", commit),
        ("checkout", "--quiet", "--force", "--detach", commit),
        ("clean", "-ffdxq"),
    )
    for step in steps:
        done = git(*step)
        if done.returncode != 0:
            raise BuildFailed(
                f"checkout of {url} at {commit[:12]} failed ({step[0]}: {_tail(done)})"
            )
    # Earlier commits' objects go: the checkout holds one commit.
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "--quiet", "--prune=now")
    readable = run(["chmod", "-R", "a+rX", str(into)], check=False, capture_output=True)
    if readable.returncode != 0:
        raise BuildFailed(f"checkout of {url} cannot be made readable")


def job_argv(wanted: Mapping[str, Any]) -> list[str]:
    """``piceli build job-run`` arguments for a wanted build (archives only)."""
    argv = [
        "build", "job-run", "--sources", SOURCES, "--cache", "/cache",
        "--out", "/work/out", "--registry-url", "oci://archive.local:5000",
        "--archives-only",
    ]  # fmt: skip
    for platform in wanted.get("platforms") or ("linux/amd64",):
        argv += ["--platform", str(platform)]
    if wanted.get("kind") == "spec":
        argv += ["--spec", f"{wanted['repo']}/{wanted['spec']}"]
        for name, item in sorted((wanted.get("keys") or {}).items()):
            argv += [
                "--image",
                json.dumps(
                    {
                        "image": name,
                        "repository": item["repository"],
                        "key": item["key"],
                    },
                    sort_keys=True,
                ),
            ]
        if wanted.get("facts"):
            argv += ["--facts", json.dumps(wanted["facts"], sort_keys=True)]
    else:
        for item in wanted.get("components") or ():
            argv += [
                "--component",
                json.dumps(
                    {
                        key: item[key]
                        for key in ("component", "source", "digest", "repository")
                    },
                    sort_keys=True,
                ),
            ]
    return argv


def _nodes(wanted: Mapping[str, Any]) -> dict[str, str]:
    """``{node: platform}`` the wanted build is imported into (0.22.0), else ``{}``."""
    deliver = wanted.get("deliver") or {}
    if deliver.get("mode") != "nodes":
        return {}
    return {str(k): str(v) for k, v in (deliver.get("nodes") or {}).items()}


def node_importer(
    targets: Mapping[str, str],
    backend: Any,
    ssh_agent_socket: Path | None = None,
) -> Callable[[str, str, str, Path], Mapping[str, Any]]:
    """``BuilderServe.node_import`` over the declared ``Node(target=...)``
    URLs (``{node: url}``): :meth:`piceli.pipeline.backend.Backend.node_deliver`
    (the validating relay into ``ctr images import -``, then the node's own
    report of the config digest), with this host's SSH agent when it runs one."""

    def run(
        node: str, image_id: str, reference: str, archive: Path
    ) -> Mapping[str, Any]:
        url = targets.get(node)
        if not url:
            raise BuildFailed(f"node {node} declares no target= on this host")
        return backend.node_deliver(
            url, image_id, reference, archive=archive, ssh_agent_socket=ssh_agent_socket
        )

    return run


def _commits(wanted: Mapping[str, Any]) -> dict[str, str]:
    if wanted.get("kind") == "spec":
        return {str(k): str(v) for k, v in (wanted.get("commits") or {}).items()}
    return {
        str(item["source"]): str(item["commit"])
        for item in wanted.get("components") or ()
    }


@dataclass
class BuilderServe:
    """One build host serving one controller (see the module doc)."""

    host: str
    image: str
    channel: Any
    #: Runs ``piceli`` with the argv in the builder image: the checkouts
    #: (``{name: path}``) at ``/work/src/<name>``, the archives out.
    container: Callable[
        [list[str], Mapping[str, Path], Path, Callable[[str], None]], int
    ]
    #: Push one archive to the registry; its digest-pinned pull reference
    #: (``None``: no registry to push to).
    push: Callable[[str, str, Path], str] | None
    index: Callable[[str, Mapping[str, str]], str] | None
    root: Path
    say: Callable[[str], None] = lambda _line: None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    claim_wait: float = 300.0
    git: Callable[..., subprocess.CompletedProcess[bytes]] = field(
        default=subprocess.run
    )
    #: 0.22.0: import one archive into one node, ``(node, config digest,
    #: reference, archive)``; returns the ``piceli.node-delivery.v1`` receipt
    #: (``None``: no node targets on this host).
    node_import: Callable[[str, str, str, Path], Mapping[str, Any]] | None = None
    #: 0.22.0: the host's health (:class:`piceli.dev.health.Health`), or None.
    health: Any = None
    #: 0.22.0: pushes a provenance document as a referrer of
    #: ``(repository, digest, body)``; ``None``: not pushed.
    attest: Callable[[str, str, bytes], Any] | None = None
    #: Machine-health signals seen in the current build's output.
    signals: list[dict[str, str]] = field(default_factory=list)
    #: Why this host claims nothing (its quarantine), once said.
    quarantine: dict[str, Any] | None = None
    #: The controller's health record of this host, from its status.
    controller_health: Mapping[str, Any] | None = None
    _said: Any = None
    #: Builds this host answered (built or failed) that the controller still
    #: lists: skipped until it drops them (0.22.0).
    answered: set[str] = field(default_factory=set)
    #: The current build's toolchain versions (``{tool: version}``), from
    #: its receipt.
    toolchains: dict[str, str] = field(default_factory=dict)
    #: 0.25.0: publishes NixReleases (:class:`piceli.infra.nix_releases.Publisher`)
    #: to the caches it holds the keys of; ``None``: claims no publish.
    publisher: Any = None
    #: 0.25.0: whether it builds images (a native host only publishes;
    #: 0.25.0: so does any host with ``--publish-only``).
    images: bool = True
    _refused: set[str] = field(default_factory=set)
    #: 0.25.0: whether it said that it leaves image builds to other hosts.
    _skipping_said: bool = False
    #: 0.26.0: each claimed controller-credential publish's one-claim
    #: private key and the sealed credential the controller answered with.
    _openers: dict[str, bytes] = field(default_factory=dict)
    _sealed: dict[str, Any] = field(default_factory=dict)
    #: 0.26.0: the build in hand's progress reports and cancel check.
    _progress: Any = None
    #: 0.27.1: why a wanted build is skipped, each said once.
    _skips: set[tuple[str, str]] = field(default_factory=set)
    #: 0.29.0: the hosts the controller's composition declares (empty: it
    #: declares none, any host may serve) and this host's quarantined
    #: sources, from its status.
    declared: list[str] = field(default_factory=list)
    blocked: dict[str, Any] = field(default_factory=dict)
    _undeclared_said: bool = False
    #: 0.33.0: this host's Piceli version (default: the running one), the
    #: controller's (``controller.version``) and the one the controller last
    #: saw from this host (``controller.build_hosts``).
    version: str = field(default_factory=_piceli_version)
    controller_version: str | None = None
    seen_version: str | None = None
    #: 0.33.0: where to write the controller's version on a mismatch (the
    #: file its service installs Piceli from); ``None``: written nowhere.
    version_file: Path | None = None
    _version_sent: str | None = None
    #: 0.33.0: the controller's builder image (``controller.builder_image``),
    #: whether ``--image`` fixed this host's, and how to switch to a new one
    #: (pull it, return its container runner); ``None``: never switched.
    controller_image: str | None = None
    image_pinned: bool = False
    switch_image: Callable[[str], Any] | None = None
    _image_said: str | None = None

    def _skip(self, build: str, why: str) -> None:
        """Say once, plainly, why this host leaves ``build`` (0.27.1)."""
        if (build, why) not in self._skips:
            self._skips.add((build, why))
            self.say(f"builder: not building {build}: {why}")

    # ------------------------------------------------------------ choosing
    def wanted(self) -> list[dict[str, Any]]:
        status = self.channel.read_status() or {}
        controller = status.get("controller") or {}
        # 0.22.0: the controller's own record of this host (it refuses the
        # claims of a host it quarantined).
        found = (controller.get("build_health") or {}).get(self.host)
        self.controller_health = found if isinstance(found, Mapping) else None
        self.declared = [str(h) for h in controller.get("build_hosts_declared") or ()]
        blocked = (controller.get("source_quarantine") or {}).get(self.host)
        self.blocked = dict(blocked) if isinstance(blocked, Mapping) else {}
        version = controller.get("version")
        self.controller_version = str(version) if version else None
        image = controller.get("builder_image")
        self.controller_image = str(image) if image else None
        seen = (controller.get("build_hosts") or {}).get(self.host)
        self.seen_version = (
            str(seen.get("version") or "") or None
            if isinstance(seen, Mapping)
            else None
        )
        return list(controller.get("builds_wanted") or ())

    def check_version(self) -> None:
        """0.33.0: raise :class:`VersionMismatch` when the controller runs
        another Piceli version (after the last :meth:`wanted`); first tell
        the controller this host's version when it last saw another one,
        and write the controller's into :attr:`version_file`. A controller
        that publishes no version (before 0.16) is not checked."""
        from piceli.gitops.state import build_host_version_request

        if self.seen_version != self.version and self._version_sent != self.version:
            try:
                self.channel.add_request(
                    *build_host_version_request(self.host, self.version)
                )
                self._version_sent = self.version
            except Exception as error:  # the claim refusal still stands
                self.say(f"builder: could not tell the controller ({error})")
        wanted = self.controller_version
        if not wanted or wanted in ("unknown", self.version):
            return
        if self.version_file is not None:
            if write_version_file(self.version_file, wanted):
                self.say(f"builder: wrote {wanted} into {self.version_file}")
        raise VersionMismatch(self.host, self.version, wanted)

    def undeclared(self) -> bool:
        """0.29.0: the controller's composition declares build hosts and not
        this one: it claims nothing (said once, and again when it changes)."""
        if self.declared and self.host not in self.declared:
            if not self._undeclared_said:
                self._undeclared_said = True
                self.say(
                    f"builder: the controller's composition does not declare build "
                    f"host {self.host} (it declares {', '.join(self.declared)}); "
                    "claiming nothing. Declare it in DevBuilds(hosts=[...]) or stop "
                    f"this service (piceli builder uninstall --host {self.host})"
                )
            return True
        if self._undeclared_said:
            self._undeclared_said = False
            self.say(f"builder: build host {self.host} is declared again")
        return False

    def follow_image(self) -> bool:
        """0.33.0: use the controller's builder image when it publishes
        another one (pulled first); ``--image`` keeps its own, said once.
        Whether this host may claim (``False``: the new image is not usable)."""
        wanted = self.controller_image
        if not wanted or wanted == self.image or not self.images:
            return True
        if self.image_pinned or self.switch_image is None:
            if self._image_said != wanted:
                self._image_said = wanted
                self.say(
                    f"builder: --image {self.image} differs from the controller's "
                    f"builder image {wanted}; builds may fail if their Piceli "
                    "versions differ (drop --image to follow the controller)"
                )
            return True
        try:
            container = self.switch_image(wanted)
        except Exception as error:
            if self._image_said != wanted:
                self._image_said = wanted
                self.say(
                    f"builder: cannot use the controller's builder image {wanted} "
                    f"({error}); claiming nothing"
                )
            return False
        self.say(f"builder: builder image {self.image} -> {wanted} (the controller's)")
        self.image, self.container, self._image_said = wanted, container, None
        return True

    def pick(self) -> dict[str, Any] | None:
        entries = self.wanted()
        self.check_version()  # 0.33.0: never claim on another version
        if not self.follow_image():
            return None
        held = self.controller_health or {}
        if held.get("quarantined"):
            if self._said != held.get("reason"):
                self.say(
                    f"builder: the controller quarantined {self.host} since "
                    f"{held.get('since')}: {held.get('reason')} (piceli gitops "
                    f"release-host {self.host}); claiming no builds"
                )
                self._said = held.get("reason")
            return None
        self._said = None
        if self.undeclared():
            return None
        # 0.22.0: what this host answered stays until the controller drops
        # it (its next poll); never build it again meanwhile.
        self.answered &= {str(entry.get("id")) for entry in entries}
        for entry in entries:
            if entry.get("failed"):
                # a failed build is retried by `piceli gitops sync`
                failure = entry["failed"]
                self._skip(
                    str(entry.get("id")),
                    f"it failed on {failure.get('host')} ({failure.get('reason')}); "
                    "piceli gitops sync offers it again",
                )
                continue
            if str(entry.get("id")) in self.answered:
                continue
            if entry.get("kind") == "nix-publish":
                if not self._can_publish(entry):
                    continue
            elif not self.images:
                if not self._skipping_said:
                    self._skipping_said = True
                    self.say(
                        f"builder: {self.host} only publishes NixReleases; "
                        "it leaves every image build to the other build hosts"
                    )
                continue
            blocked = sorted(set(self.blocked) & set(_commits(entry)))
            if blocked:  # 0.29.0: it could not fetch one of them
                record = self.blocked[blocked[0]] or {}
                self._skip(
                    str(entry.get("id")),
                    f"this host could not fetch source {blocked[0]} "
                    f"({record.get('reason')}); piceli gitops release-host "
                    f"{self.host} lets it try again",
                )
                continue
            claimed = entry.get("claimed") or {}
            if not claimed or claimed.get("host") == self.host:
                return entry
            self._skip(
                str(entry.get("id")),
                f"claimed by build host {claimed.get('host')} at {claimed.get('at')}",
            )
        return None

    def _can_publish(self, entry: Mapping[str, Any]) -> bool:
        """Whether this host publishes ``entry``: it is the release's builder
        and holds the keys of its cache (said once when it does not)."""
        if entry.get("builder") != self.host:
            return False
        if self.publisher is not None and self.publisher.cache_for(entry) is not None:
            return True
        build = str(entry.get("id"))
        if build not in self._refused:
            self._refused.add(build)
            cache = ((entry.get("plan") or {}).get("cache") or {}).get("store_url")
            self.say(
                f"builder: nix release {entry.get('release')} publishes to {cache}; "
                "this host has no keys for it (builder serve --publish "
                "MODULE:ATTR of that NixCache, with its keys in piceli secrets); "
                "not claimed"
            )
        return False

    def claim(self, entry: Mapping[str, Any]) -> bool:
        """Claim ``entry`` and wait until the controller confirms it (another
        host may win the same poll)."""
        from piceli.gitops.state import build_claim_request

        build = str(entry["id"])
        held = (entry.get("claimed") or {}).get("host") == self.host
        if held and (build in self._openers or not self._wants_seal(entry)):
            self._sealed[build] = entry.get("sealed")
            return True
        recipient = None
        if self._wants_seal(entry):  # 0.26.0: credentials="controller"
            from piceli.dev.nix_inputs import new_recipient

            self._openers[build], recipient = new_recipient()
        proof = (
            self.publisher.proof(entry, self.host, recipient)
            if entry.get("kind") == "nix-publish"
            else None
        )
        key, body = build_claim_request(build, self.host, proof, recipient)
        self.channel.add_request(key, body)
        deadline = self.clock() + self.claim_wait
        while self.clock() < deadline:
            self.sleep(5.0)
            for current in self.wanted():
                if current.get("id") == entry["id"]:
                    holder = (current.get("claimed") or {}).get("host")
                    if holder == self.host:
                        self._sealed[build] = current.get("sealed")
                        return True
                    if holder:
                        return self._refused_claim(
                            build, key, f"build host {holder} won it"
                        )
                    if self._rejection(key) is not None:
                        return self._refused_claim(build, key, "")
                    break
            else:
                # 0.27.1: said, not silent.
                return self._refused_claim(build, key, "no longer wanted")
        return self._refused_claim(
            build, key, f"the controller did not answer in {self.claim_wait:.0f}s"
        )

    def _rejection(self, key: str) -> str | None:
        """The controller's reason for refusing request ``key``, if it did."""
        try:
            status = self.channel.read_status() or {}
        except Exception:
            return None
        for item in status.get("rejected_requests") or ():
            if isinstance(item, Mapping) and item.get("request") == key:
                return str(item.get("message") or item.get("reason") or "refused")
        return None

    def _refused_claim(self, build: str, key: str, fallback: str) -> bool:
        why = self._rejection(key) or fallback
        self._skip(build, f"claim refused: {why}")
        return False

    @staticmethod
    def _wants_seal(entry: Mapping[str, Any]) -> bool:
        plan = entry.get("plan") or {}
        return (
            entry.get("kind") == "nix-publish"
            and plan.get("credentials") == "controller"
            and bool(plan.get("inputs"))
        )

    def _controller_credential(self, entry: Mapping[str, Any]) -> Any:
        """The controller's Git credential sealed for this build (0.26.0),
        opened with the claim's private key, which is then forgotten;
        ``None`` when none came."""
        from piceli.dev.nix_inputs import seal_aad, unseal

        build = str(entry["id"])
        private = self._openers.pop(build, None)
        sealed = self._sealed.pop(build, None)
        if not self._wants_seal(entry) or private is None:
            return None
        if not isinstance(sealed, Mapping) or "box" not in sealed:
            if isinstance(sealed, Mapping) and sealed.get("error"):
                self.say(f"builder: {sealed['error']}")
            return None
        try:
            found = unseal(private, sealed, seal_aad(build, self.host))
        except ValueError as error:
            self.say(f"builder: {error}")
            return None
        finally:
            del private
        return {
            "hosts": list((entry.get("plan") or {}).get("git_hosts") or ()),
            "username": str(found.get("username") or "git"),
            "token": str(found.get("token") or ""),
        }

    # ------------------------------------------------------------ building
    def quarantined(self) -> dict[str, Any] | None:
        """This host's quarantine (0.22.0), said once when it starts."""
        if self.health is None:
            return None
        try:
            found = self.health.quarantined()
        except Exception:  # an unreadable record never stops the loop
            return self.quarantine
        if found is not None and (self.quarantine or {}).get("reason") != found.get(
            "reason"
        ):
            from piceli.dev.health import describe_quarantine

            self.say(
                f"builder: {describe_quarantine(self.host, found)}; claiming no builds"
            )
        elif found is None and self.quarantine is not None:
            self.say(f"builder: {self.host} is released; claiming builds again")
        self.quarantine = found
        return found

    def _record_health(self, signal: Mapping[str, Any]) -> None:
        if self.health is None:
            return
        try:
            found = self.health.record(signal, "builder-serve")
        except Exception as error:
            self.say(f"builder: the health event was not recorded ({error})")
            return
        self.say(
            f"builder: health event on {self.host}: {signal.get('kind')}"
            + (f" ({signal.get('line')})" if signal.get("line") else "")
        )
        if found is not None:
            self.quarantined()

    def once(self) -> str | None:
        """Build one wanted build, if any; its id."""
        if self.quarantined() is not None:
            return None
        entry = self.pick()
        if entry is None or not self.claim(entry):
            return None
        return self.run(entry)

    def take_next(self, timeout: float = 1800.0, poll: float = 2.0) -> str | None:
        """0.29.0 (``--once --take-next``): stand by for the next build this
        host may take (one already waiting, or the next one asked for),
        claim it before the in-cluster fallback and build it; its id, or
        ``None`` after ``timeout`` seconds. The standby is renewed while it
        waits and ended when it returns."""
        from piceli.gitops.state import build_standby_request
        from piceli.infra.remote_builds import STANDBY_SECONDS

        deadline = self.clock() + timeout
        renewed = -STANDBY_SECONDS
        said = False
        try:
            while self.clock() < deadline:
                if self.clock() - renewed >= STANDBY_SECONDS / 2:
                    left = min(STANDBY_SECONDS, max(deadline - self.clock(), 1.0))
                    self.channel.add_request(*build_standby_request(self.host, left))
                    renewed = self.clock()
                if self.quarantined() is None:
                    entry = self.pick()
                    if entry is not None and self.claim(entry):
                        return self.run(entry)
                    if not said and not self.undeclared():
                        said = True
                        self.say(
                            f"builder: {self.host} stands by for the next build "
                            f"(at most {timeout:.0f}s); the in-cluster fallback "
                            "waits for it"
                        )
                self.sleep(poll)
            self.say(f"builder: no build to take in {timeout:.0f}s")
            return None
        finally:
            try:
                self.channel.add_request(*build_standby_request(self.host, 0))
            except Exception:  # the standby expires by itself
                pass

    def run(self, entry: Mapping[str, Any]) -> str:
        """Build (or publish) the claimed ``entry`` and answer; its id."""
        if entry.get("kind") == "nix-publish":
            return self.release(entry)
        build = str(entry["id"])
        self.say(f"builder: building {', '.join(entry.get('images') or ())} ({build})")
        began = self.clock()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.signals = []
        self._progress = self._reporter(build, tail)
        try:
            images = self.build(entry, tail)
        except builds.Cancelled:
            self.answered.add(build)
            self.say(f"builder: {build} was cancelled; stopped")
            return build
        except BuildFailed as error:
            from piceli.dev.pod import health_signal
            from piceli.gitops.state import build_failed_request

            text = error.tail or "\n".join(tail)
            self.channel.add_request(
                *build_failed_request(
                    build, self.host, str(error), text,
                    early=error.early, source=error.source,
                )
            )  # fmt: skip
            self.answered.add(build)
            self.say(
                f"builder: {build} failed: {error}"
                + (
                    "; handed to another host or the in-cluster fallback"
                    if error.early
                    else ""
                )
            )
            signal = (
                self.signals[0] if self.signals else health_signal(text.splitlines())
            )
            if signal is not None:
                self._record_health(signal)
            return build
        from piceli.gitops.state import built_request
        from piceli.infra import provenance

        receipt: dict[str, Any] = {
            "host": self.host,
            "image": self.image,
            "started_at": _iso(began),
            "finished_at": _iso(self.clock()),
            "seconds": round(self.clock() - began, 3),
            "platforms": list(entry.get("platforms") or ()),
            "commits": _commits(entry),
        }
        if self.toolchains:  # 0.22: what the build installed, for provenance
            receipt["toolchains"] = dict(self.toolchains)
        if _nodes(entry):
            receipt["delivery"] = "nodes"
        # 0.22: a passed build that met a corrupt cache still counts.
        cached = [s for s in self.signals if s["kind"].startswith("cache-")]
        if cached:
            receipt["health"] = cached
            self._record_health(cached[0])
        record = provenance.from_receipt(receipt, self.host)
        referrers = self.attach(entry, images, record)
        if referrers:
            receipt["referrers"] = referrers
        self.channel.add_request(*built_request(build, self.host, images, receipt))
        self.answered.add(build)
        self.say(f"builder: {build} delivered in {receipt['seconds']:.0f}s")
        return build

    def _reporter(self, build: str, tail: deque[str]) -> builds.Progress:
        """Progress reports of ``build`` (0.26.0): its stage, its log tail."""
        return builds.Progress(
            self.channel, build, self.host, clock=self.clock, tail=lambda: tail
        )

    def _stage(self, name: str) -> None:
        if self._progress is not None:
            self._progress.stage(name)

    def _report(self) -> None:
        if self._progress is not None:
            self._progress.report()

    def release(self, entry: Mapping[str, Any]) -> str:
        """Fetch, check the tag, build and publish a NixRelease (0.25.0);
        answer ``nix-published`` or ``build-failed`` with the output's tail."""
        import contextlib

        from piceli.gitops.state import build_failed_request, nix_published_request
        from piceli.infra import nix_releases

        build = str(entry["id"])
        plan = entry.get("plan") or {}
        self.say(
            f"builder: publishing {entry.get('release')} {plan.get('tag')} ({build})"
        )
        tail: deque[str] = deque(maxlen=TAIL_CHARS_LINES)
        marks: dict[str, tuple[float, float]] = {}

        def line(text: str) -> None:
            tail.append(text)
            self.say(text)
            self._report()

        self._progress = self._reporter(build, tail)

        @contextlib.contextmanager
        def stage(name: str) -> Any:
            self._stage(name)  # 0.26.0: reported; a cancelled build stops
            began = self.clock()
            try:
                yield
            finally:
                marks[name] = (began, self.clock())

        began = self.clock()
        try:
            nix_releases.check_plan(plan)
            source, commit = next(iter(sorted((entry.get("commits") or {}).items())))
            url = str((entry.get("urls") or {}).get(source) or "")
            if not url or commit != plan["commit"]:
                raise BuildFailed("the publish names no source at the plan's commit")
            checkout = self.root / "checkouts" / f"{source}-{_slug(url)}"
            with stage("fetch"):
                export_source(url, commit, checkout, self.root / "git", self.git)
                pointed = (
                    nix_releases.tag_commit(
                        url, str(plan["tag"]), self.root / "git", self.git
                    )
                    if plan.get("tag")
                    else commit  # 0.26.0: a branch's commit, or a commit alone
                )
                on_branch = not plan.get("branch") or builds.branch_contains(
                    url, str(plan["branch"]), commit, self.root / "git", self.git
                )
            if pointed != commit:
                raise BuildFailed(
                    f"tag {plan['tag']} of {source} points at "
                    f"{(pointed or 'nothing')[:12]}, not {commit[:12]}"
                )
            if not on_branch:
                raise BuildFailed(
                    f"commit {commit[:12]} is not on branch {plan['branch']} of {source}"
                )
            controller = self._controller_credential(entry)
            done = self.publisher.publish(
                entry, checkout, line, stage, controller=controller
            )
            controller = None
        except builds.Cancelled:
            self.answered.add(build)
            self.say(f"builder: {build} was cancelled; stopped")
            return build
        except Exception as error:  # every refusal answers, with its tail
            from piceli.errors import ERRORS

            code = getattr(error, "code", None)
            reason = (
                f"{code}: {error}" if isinstance(code, str) and code in ERRORS else
                str(error) if isinstance(error, BuildFailed | ValueError) else
                f"the publish failed ({type(error).__name__})"
            )  # fmt: skip
            self.channel.add_request(
                *build_failed_request(build, self.host, reason, "\n".join(tail))
            )
            self.answered.add(build)
            self.say(f"builder: {build} failed: {reason}")
            return build
        receipt = {
            "host": self.host,
            "started_at": _iso(began),
            "finished_at": _iso(self.clock()),
            "seconds": round(self.clock() - began, 3),
            "stages": nix_releases.receipt_stages(marks),
        }
        self.channel.add_request(
            *nix_published_request(
                build, self.host, done["manifest"], done["pointers"], receipt
            )
        )
        self.answered.add(build)
        self.say(f"builder: {build} published in {receipt['seconds']:.0f}s")
        return build

    def attach(
        self,
        entry: Mapping[str, Any],
        images: Mapping[str, Mapping[str, Any]],
        record: Mapping[str, Any],
    ) -> dict[str, str]:
        """Push each image's provenance as an OCI referrer (0.22.0); the
        referrer digest of each image the registry took."""
        if self.attest is None or _nodes(entry):
            return {}  # node deliveries have no registry to attach to
        from piceli.infra import provenance

        found: dict[str, str] = {}
        for name, image in sorted(images.items()):
            pull_ref = str(image.get("pull_ref") or "")
            digest = str(image.get("manifest_digest") or "")
            repository = pull_ref.split("@", 1)[0].split("/", 1)[-1]
            body = provenance.statement(repository, digest, record)
            try:
                result = self.attest(repository, digest, body) or {}
            except Exception as error:  # never fails the delivery
                result = {"state": "skipped", "reason": type(error).__name__}
            if result.get("state") == "attached" and result.get("digest"):
                found[name] = str(result["digest"])
            else:
                self.say(
                    f"builder: provenance of {name} not attached "
                    f"({result.get('reason') or 'refused'})"
                )
        return found

    def build(self, entry: Mapping[str, Any], tail: deque[str]) -> dict[str, Any]:
        from piceli.artifacts.cluster_build import RECEIPT_MARKER, decode_receipt
        from piceli.tempfiles import temporary_directory

        urls = dict(entry.get("urls") or {})
        with temporary_directory("builder") as scratch:
            out = Path(scratch) / "out"
            out.mkdir(parents=True)
            os.chmod(out, 0o777)  # the image's user writes here
            sources: dict[str, Path] = {}
            self._stage("fetch")
            for name, commit in sorted(_commits(entry).items()):
                url = urls.get(name)
                if not url:
                    raise BuildFailed(
                        f"no URL for source {name}", early=True, source=name
                    )
                self.say(f"builder: source {name} at {commit[:12]}")
                checkout = self.root / "checkouts" / f"{name}-{_slug(url)}"
                try:
                    export_source(url, commit, checkout, self.root / "git", self.git)
                except BuildFailed as error:  # 0.29.0: before the first command
                    raise BuildFailed(
                        str(error), error.tail, early=True, source=name
                    ) from None
                sources[name] = checkout
            receipt_line: list[str] = []

            from piceli.dev.pod import health_signal

            def line(text: str) -> None:
                if text.startswith(RECEIPT_MARKER):
                    receipt_line.append(text)
                    return
                tail.append(text)
                self.say(text)
                self._report()
                signal = health_signal((text,)) if len(self.signals) < 8 else None
                if signal is not None and all(
                    item["kind"] != signal["kind"] for item in self.signals
                ):
                    self.signals.append(signal)

            self._stage("build")
            code = self.container(job_argv(entry), sources, out, line)
            if code == ENGINE_FAILED and not receipt_line:
                # 0.29.0: the engine could not start the build on this host.
                raise BuildFailed(
                    f"the container engine could not start the build (exit {code})",
                    "\n".join(tail),
                    early=True,
                )
            if code != 0 or not receipt_line:
                raise BuildFailed(f"the build exited {code}", "\n".join(tail))
            receipt = decode_receipt(receipt_line[-1][len(RECEIPT_MARKER) :])
            self.toolchains = dict(receipt.get("toolchains") or {})
            self._stage("push")
            return self.deliver(entry, receipt, out)

    def deliver(
        self, entry: Mapping[str, Any], receipt: Mapping[str, Any], out: Path
    ) -> dict[str, Any]:
        archives = list(receipt.get("archives") or ())
        if entry.get("kind") == "spec":
            keys = {
                name: dict(item) for name, item in (entry.get("keys") or {}).items()
            }
        else:
            keys = {
                str(item["component"]): {
                    "key": item["digest"], "repository": item["repository"],
                }
                for item in entry.get("components") or ()
            }  # fmt: skip
        if _nodes(entry):
            return self.deliver_nodes(entry, keys, archives, out)
        if self.push is None or self.index is None:
            raise BuildFailed(
                "the controller delivers to its registry and this host has none "
                "to push to (Registry.in_cluster(...))"
            )
        by_repository = {item["repository"]: name for name, item in keys.items()}
        found: dict[str, dict[str, Any]] = {}
        per_platform: dict[str, dict[str, str]] = {}
        refs: dict[str, str] = {}
        for archive in archives:
            name = by_repository.get(str(archive["repository"]))
            if name is None:
                continue  # an image of the spec this build was not asked for
            self.say(f"builder: pushing {name} for {archive['platform']}")
            pull_ref = self.push(
                str(archive["image_id"]),
                str(archive["repository"]),
                out / archive["archive"],
            )
            digest = pull_ref.rsplit("@", 1)[-1]
            per_platform.setdefault(name, {})[str(archive["platform"])] = digest
            refs.setdefault(name, pull_ref)
        for name, item in keys.items():
            digests = per_platform.get(name)
            if not digests:
                raise BuildFailed(f"the build made no image {name}")
            if len(digests) > 1:
                index = self.index(str(item["repository"]), digests)
                base = refs[name].split("@", 1)[0]
                pull_ref, digest = f"{base}@{index}", index
            else:
                pull_ref, digest = refs[name], next(iter(digests.values()))
            found[name] = {
                "key": item["key"],
                "pull_ref": pull_ref,
                "manifest_digest": digest,
                "platforms": dict(sorted(digests.items())),
            }
        return found

    def deliver_nodes(
        self,
        entry: Mapping[str, Any],
        keys: Mapping[str, Mapping[str, Any]],
        archives: Sequence[Mapping[str, Any]],
        out: Path,
    ) -> dict[str, Any]:
        """Import every image into every node the wanted build names, the
        archive of each node's platform (0.22.0)."""
        from piceli.infra.remote_builds import node_identity, node_reference

        if self.node_import is None:
            raise BuildFailed(
                "the controller delivers into the nodes and this host knows no "
                "node targets (Node(target=...))"
            )
        nodes = _nodes(entry)
        found: dict[str, dict[str, Any]] = {}
        for name, item in sorted(keys.items()):
            repository = str(item["repository"])
            by_platform = {
                str(archive["platform"]): archive
                for archive in archives
                if str(archive["repository"]) == repository
            }
            if not by_platform:
                raise BuildFailed(f"the build made no image {name}")
            configs = {
                platform: str(archive["image_id"])
                for platform, archive in sorted(by_platform.items())
            }
            reference = node_reference(repository, configs)
            receipts: dict[str, dict[str, Any]] = {}
            for node, platform in sorted(nodes.items()):
                archive = by_platform.get(platform)
                if archive is None:
                    raise BuildFailed(
                        f"node {node} runs {platform}; the build made {name} for "
                        f"{', '.join(sorted(by_platform))}"
                    )
                self.say(f"builder: importing {name} ({platform}) into node {node}")
                try:
                    receipt = dict(
                        self.node_import(
                            node, configs[platform], reference, out / archive["archive"]
                        )
                    )
                except BuildFailed:
                    raise
                except Exception as error:  # a tool or target refused
                    raise BuildFailed(
                        f"import of {name} into node {node} failed: {error}"
                    ) from None
                image = receipt.get("image") or {}
                if receipt.get("state") != "succeeded" or receipt.get("result") not in {
                    "imported",
                    "already-present",
                }:
                    raise BuildFailed(
                        f"import of {name} into node {node} ended "
                        f"{receipt.get('result') or receipt.get('state')} "
                        f"({receipt.get('reason')})"
                    )
                if image.get("config_digest") != configs[platform]:
                    raise BuildFailed(
                        f"node {node} reports config {image.get('config_digest')} "
                        f"for {name}, not {configs[platform]}"
                    )
                receipts[node] = {
                    "platform": platform,
                    "result": receipt.get("result"),
                    "reference": image.get("reference"),
                    "config_digest": image.get("config_digest"),
                    "node_target_digest": image.get("node_target_digest"),
                    "target": receipt.get("target"),
                    "seconds": receipt.get("seconds"),
                }
            found[name] = {
                "key": item["key"],
                "delivery": "nodes",
                "pull_ref": reference,
                "manifest_digest": node_identity(configs),
                "platforms": configs,
                "nodes": receipts,
            }
        return found

    def serve(self, poll: float = 15.0, once: bool = False) -> None:
        while True:
            built = self.once()
            if once:
                return
            if built is None:
                self.sleep(poll)


#: The builder container's first step: copy every read-only checkout into
#: ``SOURCES`` (``tar`` keeps file times; the copy is the container user's),
#: run ``piceli``, remove the copy whatever the outcome.
_COPY_AND_RUN = f"""set -eu
dest={SOURCES}
rm -rf "$dest"
trap 'rm -rf "$dest"' EXIT
mkdir -p "$dest"
for src in /work/src/*/; do
  name=$(basename "$src")
  mkdir "$dest/$name"
  (cd "$src" && tar -cf - .) | (cd "$dest/$name" && tar -xf -)
done
piceli "$@"
"""


def docker_container(
    cli: Any, image: str, cache_volume: str = CACHE_VOLUME
) -> Callable[[list[str], Mapping[str, Path], Path, Callable[[str], None]], int]:
    """Run ``piceli`` with ``argv`` in the builder image on a docker build
    host: each checkout read-only at ``/work/src/<name>`` (copied to
    ``SOURCES`` first), the archives out, the build cache as a volume."""

    def run(
        argv: list[str],
        sources: Mapping[str, Path],
        out: Path,
        line: Callable[[str], None],
    ) -> int:
        mounts = [
            item
            for name, path in sorted(sources.items())
            for item in ("-v", f"{path}:/work/src/{name}:ro")
        ]
        command = cli.argv(
            "run", "--rm",
            "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            *mounts, "-v", f"{out}:/work/out",
            "-v", f"{cache_volume}:/cache", "-e", "HOME=/work/out/.home",
            "--entrypoint", "sh", image, "-c", _COPY_AND_RUN, "sh", *argv,
        )  # fmt: skip
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        assert process.stdout is not None
        for raw in process.stdout:
            line(raw.decode("utf-8", "replace").rstrip("\n"))
        return process.wait()

    return run


def check_sources(
    sources: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[dict[str, Any]]:
    """``builder serve --check`` (0.29.0): whether this host reaches every
    source (``{name: url}``) with ``git ls-remote``, as the build fetches it
    (the URL, then the same repository over SSH, never prompting). One row
    per source: ``state`` ``reachable`` (``via`` the URL that answered) or
    ``unreachable`` (``detail``, Git's last message)."""
    env = fetch_env()
    rows = []
    for name, url in sorted(sources.items()):
        errors: list[str] = []
        via = None
        for candidate in fetch_urls(url) if url else ():
            try:
                done = run(
                    ["git", "ls-remote", "--heads", candidate],
                    check=False, capture_output=True, timeout=60, env=env,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                errors.append(f"{candidate}: timed out")
                continue
            if done.returncode == 0:
                via = candidate
                break
            errors.append(f"{candidate}: {_tail(done) or f'exit {done.returncode}'}")
        row: dict[str, Any] = {"source": name, "url": url}
        if via is not None:
            row.update(state="reachable", via=via)
        else:
            row.update(state="unreachable", detail=errors[-1] if errors else "no URL")
        rows.append(row)
    return rows


#: The write test inside the builder container: the archive directory and
#: the build cache, as the build writes them.
_WRITE_TEST = f"""set -eu
touch /work/out/.piceli-write-test && rm /work/out/.piceli-write-test
mkdir -p {SOURCES}.write-test && rmdir {SOURCES}.write-test
"""


def scratch_check(
    cli: Any, image: str, cache_volume: str = CACHE_VOLUME
) -> dict[str, Any]:
    """``builder serve --check`` (0.29.0): run the builder image as the build
    does (its user, no capabilities, a scratch archive directory and the
    build cache mounted) and write into both. ``state`` ``writable``, or
    ``not-writable`` with the engine's last line."""
    from piceli.tempfiles import temporary_directory

    with temporary_directory("builder-check") as scratch:
        out = Path(scratch) / "out"
        out.mkdir()
        os.chmod(out, 0o777)  # the image's user writes here, as in a build
        done = cli.run(
            "run", "--rm", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "-v", f"{out}:/work/out",
            "-v", f"{cache_volume}:/cache", "--entrypoint", "sh", image,
            "-c", _WRITE_TEST, timeout=300,
        )  # fmt: skip
    if done.returncode == 0:
        return {"state": "writable", "cache": cache_volume}
    lines = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    return {
        "state": "not-writable",
        "cache": cache_volume,
        "detail": (lines[-1] if lines else f"exit {done.returncode}")[:300],
    }


def prepare_cache(cli: Any, image: str, cache_volume: str = CACHE_VOLUME) -> None:
    """The build cache volume, owned by the image's user (once)."""
    if cli.run("volume", "inspect", cache_volume, timeout=60).returncode == 0:
        return
    cli.run("volume", "create", cache_volume, timeout=60)
    cli.run(
        "run", "--rm", "--user", "0", "-v", f"{cache_volume}:/cache",
        "--entrypoint", "chown", image, "65532:65532", "/cache", timeout=600,
    )  # fmt: skip


def ensure_image(cli: Any, image: str, say: Callable[[str], None]) -> None:
    if cli.run("image", "inspect", image, timeout=60).returncode != 0:
        say(f"builder: pulling {image}")
        pulled = cli.run("pull", image, timeout=3600)
        if pulled.returncode != 0:
            raise BuildFailed("cannot pull the builder image")


def cleanup_root(root: Path, keep: Sequence[str] = ("git", "checkouts")) -> None:
    """Leave only the mirrors and checkouts in the builder's state directory."""
    for entry in root.iterdir() if root.is_dir() else ():
        if entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)
