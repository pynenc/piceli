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


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BuildFailed(Exception):
    """The build failed; ``tail`` is its output's end."""

    def __init__(self, reason: str, tail: str = "") -> None:
        super().__init__(reason)
        self.tail = tail


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

    # ------------------------------------------------------------ choosing
    def wanted(self) -> list[dict[str, Any]]:
        status = self.channel.read_status() or {}
        controller = status.get("controller") or {}
        # 0.22.0: the controller's own record of this host (it refuses the
        # claims of a host it quarantined).
        found = (controller.get("build_health") or {}).get(self.host)
        self.controller_health = found if isinstance(found, Mapping) else None
        return list(controller.get("builds_wanted") or ())

    def pick(self) -> dict[str, Any] | None:
        entries = self.wanted()
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
        # 0.22.0: what this host answered stays until the controller drops
        # it (its next poll); never build it again meanwhile.
        self.answered &= {str(entry.get("id")) for entry in entries}
        for entry in entries:
            if entry.get("failed") or str(entry.get("id")) in self.answered:
                continue  # a failed build is retried by `piceli gitops sync`
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
            claimed = entry.get("claimed") or {}
            if not claimed or claimed.get("host") == self.host:
                return entry
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

        if (entry.get("claimed") or {}).get("host") == self.host:
            return True
        proof = (
            self.publisher.proof(entry, self.host)
            if entry.get("kind") == "nix-publish"
            else None
        )
        self.channel.add_request(
            *build_claim_request(str(entry["id"]), self.host, proof)
        )
        deadline = self.clock() + self.claim_wait
        while self.clock() < deadline:
            self.sleep(5.0)
            for current in self.wanted():
                if current.get("id") == entry["id"]:
                    holder = (current.get("claimed") or {}).get("host")
                    if holder == self.host:
                        return True
                    if holder:
                        return False
                    break
            else:
                return False  # no longer wanted
        return False

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
        if entry.get("kind") == "nix-publish":
            return self.release(entry)
        build = str(entry["id"])
        self.say(f"builder: building {', '.join(entry.get('images') or ())} ({build})")
        began = self.clock()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.signals = []
        try:
            images = self.build(entry, tail)
        except BuildFailed as error:
            from piceli.dev.pod import health_signal
            from piceli.gitops.state import build_failed_request

            text = error.tail or "\n".join(tail)
            self.channel.add_request(
                *build_failed_request(build, self.host, str(error), text)
            )
            self.answered.add(build)
            self.say(f"builder: {build} failed: {error}")
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

        @contextlib.contextmanager
        def stage(name: str) -> Any:
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
                pointed = nix_releases.tag_commit(
                    url, str(plan["tag"]), self.root / "git", self.git
                )
            if pointed != commit:
                raise BuildFailed(
                    f"tag {plan['tag']} of {source} points at "
                    f"{(pointed or 'nothing')[:12]}, not {commit[:12]}"
                )
            done = self.publisher.publish(entry, checkout, line, stage)
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
            for name, commit in sorted(_commits(entry).items()):
                url = urls.get(name)
                if not url:
                    raise BuildFailed(f"no URL for source {name}")
                self.say(f"builder: source {name} at {commit[:12]}")
                checkout = self.root / "checkouts" / f"{name}-{_slug(url)}"
                export_source(url, commit, checkout, self.root / "git", self.git)
                sources[name] = checkout
            receipt_line: list[str] = []

            from piceli.dev.pod import health_signal

            def line(text: str) -> None:
                if text.startswith(RECEIPT_MARKER):
                    receipt_line.append(text)
                    return
                tail.append(text)
                self.say(text)
                signal = health_signal((text,)) if len(self.signals) < 8 else None
                if signal is not None and all(
                    item["kind"] != signal["kind"] for item in self.signals
                ):
                    self.signals.append(signal)

            code = self.container(job_argv(entry), sources, out, line)
            if code != 0 or not receipt_line:
                raise BuildFailed(f"the build exited {code}", "\n".join(tail))
            receipt = decode_receipt(receipt_line[-1][len(RECEIPT_MARKER) :])
            self.toolchains = dict(receipt.get("toolchains") or {})
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
