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
   its platforms;
5. answer ``built`` (each image's key, pull reference, digest and
   platforms, and a receipt: host, times, image, commits) or
   ``build-failed`` with the output's tail.

Nothing here holds a Tailscale, registry or cluster credential of its own:
the profile reaches the API, the API reaches the registry.

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
    push: Callable[[str, str, Path], str]
    index: Callable[[str, Mapping[str, str]], str]
    root: Path
    say: Callable[[str], None] = lambda _line: None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    claim_wait: float = 300.0
    git: Callable[..., subprocess.CompletedProcess[bytes]] = field(
        default=subprocess.run
    )

    # ------------------------------------------------------------ choosing
    def wanted(self) -> list[dict[str, Any]]:
        status = self.channel.read_status() or {}
        return list((status.get("controller") or {}).get("builds_wanted") or ())

    def pick(self) -> dict[str, Any] | None:
        for entry in self.wanted():
            claimed = entry.get("claimed") or {}
            if not claimed or claimed.get("host") == self.host:
                return entry
        return None

    def claim(self, entry: Mapping[str, Any]) -> bool:
        """Claim ``entry`` and wait until the controller confirms it (another
        host may win the same poll)."""
        from piceli.gitops.state import build_claim_request

        if (entry.get("claimed") or {}).get("host") == self.host:
            return True
        self.channel.add_request(*build_claim_request(str(entry["id"]), self.host))
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
    def once(self) -> str | None:
        """Build one wanted build, if any; its id."""
        entry = self.pick()
        if entry is None or not self.claim(entry):
            return None
        build = str(entry["id"])
        self.say(f"builder: building {', '.join(entry.get('images') or ())} ({build})")
        began = self.clock()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        try:
            images = self.build(entry, tail)
        except BuildFailed as error:
            from piceli.gitops.state import build_failed_request

            text = error.tail or "\n".join(tail)
            self.channel.add_request(
                *build_failed_request(build, self.host, str(error), text)
            )
            self.say(f"builder: {build} failed: {error}")
            return build
        from piceli.gitops.state import built_request

        receipt = {
            "host": self.host,
            "image": self.image,
            "started_at": _iso(began),
            "finished_at": _iso(self.clock()),
            "seconds": round(self.clock() - began, 3),
            "platforms": list(entry.get("platforms") or ()),
            "commits": _commits(entry),
        }
        self.channel.add_request(*built_request(build, self.host, images, receipt))
        self.say(f"builder: {build} delivered in {receipt['seconds']:.0f}s")
        return build

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

            def line(text: str) -> None:
                if text.startswith(RECEIPT_MARKER):
                    receipt_line.append(text)
                    return
                tail.append(text)
                self.say(text)

            code = self.container(job_argv(entry), sources, out, line)
            if code != 0 or not receipt_line:
                raise BuildFailed(f"the build exited {code}", "\n".join(tail))
            receipt = decode_receipt(receipt_line[-1][len(RECEIPT_MARKER) :])
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
