"""The train's images handed to the GitOps controller (``Step.images``).

The cluster's push-to-running wait is mostly building: the controller sees
the push, waits for a build host (or starts a build Job) and only then
deploys. A train that just tested and promoted a commit can build those
images itself, before the push the controller reacts to:

1. Load the composition module the controller will load, and compute the
   **change key** the controller will compute once the promoted head is
   pushed. The composition lives in this repository (read from the promoted
   head) or in another one (``Step.images(repository=URL, branch=...)``:
   fetched at the tip of that branch, the commit the controller loads).
   Each source an environment follows is taken at the commit the
   controller will pick by the same rules (:meth:`Place.revision`):

   - this repository at the promoted head (``--push`` moves the branch the
     environment follows to it);
   - every other source at its current tip, read from the same remote with
     this machine's Git credentials (:class:`Remotes`).

   Then, with the controller's own functions:

   - for each component built from this repository, its contract
     (``piceli.toml``) and the Git object id of every path its build reads
     (:func:`piceli.infra.contract.contract_digest`);
   - for each build spec of a pipeline environment that deploys the
     promoted head (a host build: ``[build]`` commands, contexts, images,
     toolchains), each image's key from the spec, its contexts' files at
     those commits and the controller's platforms
     (:func:`piceli.infra.pipelines.image_keys`, over every source's Git
     objects with :class:`LocalSources`).
2. Build them from those commits with the same host-build code as
   ``piceli builder serve`` (a container of the builder image on a docker
   build host; every platform, one OCI index over several), push them to
   the cluster's registry and attach their provenance as OCI referrers.
3. Offer them to the controller: a ``prebuilt`` request with each image's
   component, key, source, commit, every source's commit the key was made
   with, reference and digest, and a receipt (host, builder image, times,
   commits, and the train run and tag).

The controller (``Controller(prebuilt=True)``) keeps the offer and uses it
only when a step's own change key for that component is the offered key and
the digest is in its registry (:mod:`piceli.infra.remote_builds`). So when
it sees the promoted commit, the build it wants exists: it deploys without
waiting for a host or a Job. A component whose key it computes differently
(another contract, another tree, another source that moved after the
offer) is built as before, and its status names the sources whose commits
differ.

A failure here (the controller does not take prebuilt images, no build
host, a failed build or push) fails the step (``train-images-failed``); the
promotion stands, and the controller builds the images itself.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.train.model import ImagesStep, TrainError
from piceli.train.runner import now_iso
from piceli.train.steps import StepContext, StepResult, log_path


def _failed(message: str) -> TrainError:
    return TrainError("train-images-failed", message)


def _iso(seconds: float) -> str:
    return now_iso(seconds)[:19] + "Z"


@dataclass
class ImageTools:
    """What an images step builds, pushes and offers with.

    :param build: ``(wanted build, tail) -> {component: {"key", "pull_ref",
        "manifest_digest", "platforms"}}``: build and push (the shape of
        :meth:`piceli.infra.builder_serve.BuilderServe.build`).
    :param offer: ``(request key, request body)``: write to the controller.
    :param attest: ``(wanted, images, provenance) -> {component: referrer}``.
    """

    host: str
    image: str
    build: Callable[[Mapping[str, Any], deque[str]], Mapping[str, Mapping[str, Any]]]
    offer: Callable[[str, Mapping[str, Any]], None]
    attest: Callable[..., Mapping[str, str]] = field(
        default=lambda *_args: {}  # type: ignore[assignment]
    )
    #: The toolchains the last build reported (for the receipt).
    toolchains: Callable[[], Mapping[str, Any]] = field(
        default=lambda: {}  # type: ignore[assignment]
    )


#: ``(composition, step, ctx) -> context manager of ImageTools``.
ToolsFactory = Callable[[Any, ImagesStep, StepContext], Any]


def _normalise(url: str) -> str:
    text = url.strip().rstrip("/")
    text = text[:-4] if text.endswith(".git") else text
    for prefix in ("https://", "http://", "ssh://", "git://", "file://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.split("@", 1)[-1] if "@" in text.split("/", 1)[0] else text
    return text.replace(":", "/", 1) if ":" in text.split("/", 1)[0] else text


def _remote_urls(checkout: Path) -> set[str]:
    done = subprocess.run(
        ["git", "remote", "-v"], cwd=checkout, capture_output=True, text=True
    )
    found = set()
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            found.add(_normalise(parts[1]))
    return found


def _object_id(checkout: Path, commit: str, path: str) -> str | None:
    spec = f"{commit}:{path}" if path not in ("", ".") else f"{commit}^{{tree}}"
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", spec],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    found = done.stdout.strip()
    return found if done.returncode == 0 and found else None


def _platforms(composition: Any, platforms: Sequence[str]) -> list[str]:
    """The step's platforms, else the controller's default: ``linux/<arch>``
    of every node of the cluster, sorted (``piceli gitops enable``)."""
    if platforms:
        return list(platforms)
    nodes = getattr(composition.cluster, "nodes", ()) or ()
    found = sorted(
        {
            f"linux/{node.arch}"
            for node in nodes
            if getattr(node, "arch", None) in {"amd64", "arm64"}
        }
    )
    return found or ["linux/amd64"]


def _prefix(composition: Any) -> str:
    registry = composition.registry
    return (getattr(registry, "repository", None) or composition.name).strip("/")


def component_build(
    composition: Any,
    checkout: Path,
    head: str,
    *,
    repo: Path,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The components build the controller will want for ``head`` (its
    ``components`` wanted-build shape, each with its change key), or
    ``None`` when no component is built from this repository.

    :param repo: The repository the build host fetches the commit from.
    :param names: Only these components (default: every one built from this
        repository's remotes).
    :raises TrainError: ``train-images-failed``.
    """
    from piceli.infra.contract import CONTRACT_FILE, contract_digest, parse_contracts

    here = _remote_urls(checkout) | {_normalise(str(repo))}
    chosen = [
        item
        for item in composition.components
        if item.source is not None
        and (item.name in names if names else _normalise(item.source.url) in here)
    ]
    if not chosen:
        return None
    contract_file = checkout / CONTRACT_FILE
    if not contract_file.is_file():
        raise _failed(f"the promoted head has no {CONTRACT_FILE}")
    contracts = parse_contracts(contract_file.read_text())
    items = []
    for component in chosen:
        contract = contracts.get(component.name)
        if contract is None or contract.build is None:
            raise _failed(f"{CONTRACT_FILE} has no build for {component.name}")
        trees = {
            path: _object_id(checkout, head, path) or "absent"
            for path in contract.build.paths
        }
        items.append(
            {
                "component": component.name,
                "source": component.source.key,
                "commit": head,
                "digest": contract_digest(contract, trees),
                "repository": f"{_prefix(composition)}/{component.name}",
            }
        )
    return {
        "id": "train",
        "kind": "components",
        "components": items,
        "urls": {item["source"]: str(repo) for item in items},
        "platforms": _platforms(composition, platforms),
    }


class LocalSources:
    """The controller's source reads (:class:`~piceli.infra.sources.SourceSet`:
    ``fetch``, ``tree``, ``read``, ``object_id``) answered from local Git
    repositories, one per source name: the train's checkout for its own
    repository (the promoted head is there before any push) and a fetched
    copy of each other source. A build that reads a source not here cannot
    have its key computed (``train-images-failed``)."""

    def __init__(self, repos: Mapping[str, Path]) -> None:
        self.repos = dict(repos)

    def _dir(self, key: str) -> Path:
        found = self.repos.get(key)
        if found is None:
            raise _failed(
                f"the build reads source {key!r}, which the train cannot read at "
                "the commit the controller will follow"
            )
        return found

    def _git(self, key: str, *args: str) -> bytes:
        done = subprocess.run(
            ["git", *args], cwd=self._dir(key), capture_output=True, timeout=600
        )
        if done.returncode != 0:
            raise _failed(f"git {args[0]} failed in source {key}")
        return done.stdout

    def fetch(self, key: str) -> None:
        self._dir(key)

    def object_id(self, key: str, commit: str, path: str) -> str | None:
        return _object_id(self._dir(key), commit, path)

    def read(self, key: str, commit: str, path: str) -> bytes | None:
        if self.object_id(key, commit, path) is None:
            return None
        return self._git(key, "show", f"{commit}:{path}")

    def tree(self, key: str, commit: str, prefix: str) -> list[tuple[str, str, str]]:
        """As :meth:`piceli.infra.sources.SourceSet.tree`."""
        prefix = prefix.strip("/")
        prefix = "" if prefix in ("", ".") else prefix
        args = ["ls-tree", "-r", "-z", "--full-tree", commit]
        if prefix:
            args += ["--", prefix + "/"]
        found: list[tuple[str, str, str]] = []
        for record in self._git(key, *args).split(b"\0"):
            if not record:
                continue
            meta, _, raw = record.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            if kind != "blob":
                continue
            path = raw.decode("utf-8", "surrogateescape")
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                path = path[len(prefix) + 1 :]
            found.append((path, mode, oid))
        return found


class Remotes:
    """Other repositories, read as the controller reads them: their refs
    (``git ls-remote``) and one commit each, fetched shallow into
    ``scratch`` (the step removes it).

    Git runs as a build host's fetch does (:func:`~piceli.infra.builder_serve.fetch_urls`,
    :func:`~piceli.infra.builder_serve.fetch_env`): this machine's own Git
    credentials (credential helpers, the SSH agent, ``insteadOf`` rewrites),
    never a prompt, and an HTTPS URL is tried over SSH too.
    """

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self._refs: dict[str, Any] = {}

    def _run(
        self, *args: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        from piceli.infra.builder_serve import fetch_env

        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=600,
            env=fetch_env(), stdin=subprocess.DEVNULL,
        )  # fmt: skip

    def refs(self, name: str, url: str) -> Any:
        """The remote's branches and tags (:class:`~piceli.gitops.repo.RemoteRefs`)."""
        from piceli.gitops.repo import parse_ls_remote
        from piceli.infra.builder_serve import fetch_urls

        if url not in self._refs:
            for where in fetch_urls(url):
                done = self._run("ls-remote", "--heads", "--tags", "--", where)
                if done.returncode == 0:
                    self._refs[url] = parse_ls_remote(
                        done.stdout.decode("utf-8", "replace")
                    )
                    break
            else:
                raise _failed(
                    f"source {name} cannot be read with this machine's Git "
                    "credentials (git ls-remote failed over HTTPS and SSH)"
                )
        return self._refs[url]

    def fetch(self, name: str, url: str, ref: str, commit: str) -> Path:
        """A Git directory holding ``commit`` (``ref`` of ``url``)."""
        from piceli.infra.builder_serve import fetch_urls

        directory = self.scratch / "sources" / name
        if not (directory / "HEAD").is_file():
            directory.mkdir(parents=True, exist_ok=True)
            self._run("init", "--quiet", "--bare", str(directory))

        def present() -> bool:
            return self._run(
                "cat-file", "-e", f"{commit}^{{commit}}", cwd=directory
            ).returncode == 0  # fmt: skip

        for where in fetch_urls(url):
            for wanted in (ref, commit):  # the ref moved since: the commit alone
                if present():
                    return directory
                self._run(
                    "fetch", "--quiet", "--depth", "1", "--no-tags", where, wanted,
                    cwd=directory,
                )  # fmt: skip
        if not present():
            raise _failed(
                f"commit {commit[:12]} of source {name} cannot be fetched with this "
                "machine's Git credentials"
            )
        return directory

    def export(self, directory: Path, commit: str, into: Path) -> None:
        """The files of ``commit`` into ``into`` (no ``.git``)."""
        import io
        import tarfile

        done = self._run("archive", "--format=tar", commit, cwd=directory)
        if done.returncode != 0:
            raise _failed(f"git archive failed at {commit[:12]}")
        into.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
            archive.extractall(into, filter="data")


def repo_source(composition: Any, checkout: Path) -> str:
    """The composition repository's source name, as ``piceli gitops enable``
    records it: the composition source with the URL of ``origin``, else the
    name of that URL."""
    done = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise _failed(
            "a pipeline environment's builds need the repository's origin remote "
            "(the controller names the composition repository by its URL)"
        )
    return _source_name(composition, done.stdout.strip(), "the origin remote")


def _source_name(composition: Any, url: str, what: str) -> str:
    """A repository's source name as ``piceli gitops enable`` names it: the
    composition source with that URL, else the name of the URL."""
    from piceli.infra import Source

    try:
        url = Source(url).url
    except Exception:
        raise _failed(f"{what} is not a Git URL without credentials") from None
    same = [source for source in composition.sources if source.url == url]
    return same[0].key if same else Source(url).key


@dataclass
class Place:
    """The composition the controller will load, and the commit of every
    source it will follow, as the train computes them.

    :param composition: The loaded composition module.
    :param tree: The files of its repository (the train's checkout, or the
        other repository's commit exported).
    :param repo: The composition repository's source name.
    :param own: The train repository's source name (``None``: the
        composition follows no source of it).
    :param head: The promoted head.
    :param base: The branch it is promoted to.
    :param sources: The source reads (:class:`LocalSources`).
    :param urls: Each readable source's URL, for the build host's fetch.
    :param commits: Each source's commit read so far.
    """

    composition: Any
    tree: Path
    repo: str
    own: str | None
    head: str
    base: str
    sources: LocalSources
    urls: dict[str, str]
    remotes: Remotes
    commits: dict[str, str] = field(default_factory=dict)

    def commit_of(self, source: Any, items: Sequence[Any], env: Any) -> str | None:
        """The commit the controller picks for ``source`` by its rules
        (``_resolve``), or ``None`` when the train cannot know it.

        The train's own repository: the promoted head, when the rules follow
        the branch it is promoted to. Another source: its current tip by the
        same rules, read from the same remote.
        """
        from piceli.envs.model import BRANCH, Branch, Promote, Tag
        from piceli.gitops.controller import _version_key

        key = source.key
        if key == self.own:
            for item in items:
                if item == BRANCH or (
                    isinstance(item, Branch) and item.name == self.base
                ):
                    return self.head
                return None  # follows another ref than the promoted branch
            return None
        if any(isinstance(item, Promote) for item in items):
            return None  # a promoted commit: the controller's state knows it
        url = self.urls.get(key) or source.url
        refs = self.remotes.refs(key, url)
        found: tuple[str, str] | None = None
        for item in items:
            if item == BRANCH:
                fallback = env.branches.fallback
                branch = self.base if self.base in refs.branches else fallback
                if branch in refs.branches:
                    found = (f"refs/heads/{branch}", refs.branches[branch])
            elif isinstance(item, Branch) and item.name in refs.branches:
                found = (f"refs/heads/{item.name}", refs.branches[item.name])
            elif isinstance(item, Tag):
                tags = sorted(
                    (n for n in refs.tags if fnmatch.fnmatchcase(n, item.pattern)),
                    key=_version_key,
                )
                if tags:
                    found = (f"refs/tags/{tags[-1]}", refs.tags[tags[-1]])
            if found is not None:
                break
        if found is None:
            return None
        ref, commit = found
        if self.commits.get(key) != commit:
            self.sources.repos[key] = self.remotes.fetch(key, url, ref, commit)
            self.urls[key] = url
            self.commits[key] = commit
        return commit

    def revision(self, env: Any) -> dict[str, str] | None:
        """``{source: commit}`` the controller will deploy ``env`` at once the
        promoted head is pushed, or ``None`` when it will not deploy the
        promoted head (or the train cannot know a commit of it)."""
        revision: dict[str, str] = {}
        for source, items in env.sources:
            commit = self.commit_of(source, tuple(items), env)
            if commit is None:
                return None
            revision[source.key] = commit
        if self.repo not in revision:  # the loaded module's commit
            revision[self.repo] = self.commits[self.repo]
        if self.own is None or revision.get(self.own) != self.head:
            return None
        return revision


@contextmanager
def place(
    step: ImagesStep, checkout: Path, head: str, base: str, *, repo: Path
) -> Iterator[Place]:
    """Load the composition the controller will load (see :class:`Place`).

    Without ``step.repository`` (or naming this repository), the composition
    is this repository's, read from the promoted head. With it, the other
    repository is fetched at the tip of ``step.branch`` (the commit the
    controller loads), the composition imported from that commit, and its
    imported modules forgotten afterwards.

    :raises TrainError: ``train-images-failed``.
    """
    from piceli.tempfiles import temporary_directory

    here = _remote_urls(checkout) | {_normalise(str(repo))}
    remote = bool(step.repository) and _normalise(step.repository) not in here
    with temporary_directory("images-step") as scratch:
        remotes = Remotes(scratch)
        if not remote:
            composition = _composition(step, checkout)
            # The name only keys pipeline builds (the controller's
            # composition source); components are keyed by their own source.
            name = (
                repo_source(composition, checkout)
                if _has_pipelines(composition)
                else "repository"
            )
            yield Place(
                composition, checkout, name, name, head, base,
                LocalSources({name: checkout}), {name: str(repo)}, remotes,
                {name: head},
            )  # fmt: skip
            return
        from piceli.infra import Source
        from piceli.infra.pipelines import _forget

        url = Source(step.repository).url
        refs = remotes.refs("the composition repository", url)
        commit = refs.branches.get(step.branch)
        if commit is None:
            raise _failed(f"the composition repository has no branch {step.branch}")
        directory = remotes.fetch(
            "_composition", url, f"refs/heads/{step.branch}", commit
        )
        tree = scratch / "composition"
        remotes.export(directory, commit, tree)
        try:
            composition = _composition(step, tree)
            name = _source_name(composition, url, "repository")
            own = next(
                (
                    source.key
                    for source in composition.sources
                    if _normalise(source.url) in here
                ),
                None,
            )
            repos: dict[str, Path] = {name: directory}
            urls = {name: url}
            if own is not None:
                repos[own] = checkout
                urls[own] = str(repo)
            found = Place(
                composition, tree, name, own, head, base, LocalSources(repos), urls,
                remotes, {name: commit, **({own: head} if own else {})},
            )  # fmt: skip
            yield found
        finally:
            _forget(tree)


def _has_pipelines(composition: Any) -> bool:
    return any(
        getattr(env, "pipeline", None) is not None for env in composition.environments
    )


def spec_builds(
    where: Place,
    *,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The spec builds the controller will want once the promoted head is
    pushed: one per build spec of each pipeline environment that deploys
    it, in its ``spec`` wanted-build shape, each image with the change key
    the controller computes (:func:`piceli.infra.pipelines.image_keys`, the
    same function, over every source at the commit the controller will
    follow: :meth:`Place.revision`).

    :raises TrainError: ``train-images-failed`` (a context reads a source
        the environment does not follow, a spec missing or invalid, two
        environments wanting one image under different keys).
    """
    from piceli.infra import CompositionError
    from piceli.infra.pipelines import image_keys, load_spec, spec_paths

    composition = where.composition
    chosen = _platforms(composition, platforms)
    platform = ",".join(chosen)
    found: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    keyed: dict[str, str] = {}
    name = where.repo
    for env in composition.environments:
        pipeline = getattr(env, "pipeline", None)
        if pipeline is None:
            continue
        revision = where.revision(env)
        if revision is None:
            continue
        try:
            paths = spec_paths(pipeline, where.tree)
        except CompositionError as error:
            raise _failed(str(error)) from None
        for path, build in paths:
            text = where.sources.read(name, revision[name], path)
            if text is None:
                raise _failed(f"the build spec {path} is not in {name}")
            facts = None if build.node_facts is None else build.node_facts.to_dict()
            try:
                spec = load_spec(text, path, repo=name, facts=facts)
                keys = image_keys(spec, revision, where.sources, platform=platform)
            except CompositionError as error:
                raise _failed(str(error)) from None
            commits = {source: revision[source] for source, _ in spec.contexts.values()}
            commits[name] = revision[name]
            identity = (path, tuple(sorted(commits.items())))
            if identity in found:
                continue
            images = {}
            for image, key in sorted(keys.items()):
                if names and image not in names:
                    continue
                if keyed.setdefault(image, key.key) != key.key:
                    raise _failed(
                        f"two environments want image {image} under different keys"
                    )
                images[image] = {
                    "repository": f"{_prefix(composition)}/{image}",
                    "key": key.key,
                }
            if images:
                found[identity] = {
                    "id": "train",
                    "kind": "spec",
                    "spec": path,
                    "repo": name,
                    "commits": dict(sorted(commits.items())),
                    "urls": {source: where.urls[source] for source in commits},
                    "facts": facts,
                    "keys": images,
                    "sources": {
                        image: dict(sorted(keys[image].sources.items()))
                        for image in images
                    },
                    "own": where.own,
                    "platforms": chosen,
                }
    return list(found.values())


def wanted_builds(
    where: Place,
    checkout: Path,
    *,
    repo: Path,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Every build the controller will want for the promoted head: its
    components built from this repository, and the build specs of its
    pipeline environments.

    :raises TrainError: ``train-images-failed`` (nothing to build, an
        unknown name, a key that cannot be computed here).
    """
    composition = where.composition
    known = {item.name for item in composition.components}
    builds = spec_builds(where, names=names, platforms=platforms)
    known |= {image for build in builds for image in build["keys"]}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise _failed(f"the composition builds no image {', '.join(unknown)}")
    third = [
        item.name
        for item in composition.components
        if item.source is None and item.name in names
    ]
    if third:
        raise _failed(f"{', '.join(third)}: a third-party image is mirrored, not built")
    components = component_build(
        composition, checkout, where.head, repo=repo, names=names,
        platforms=platforms,
    )  # fmt: skip
    found = ([components] if components is not None else []) + builds
    if not found:
        raise _failed(
            "nothing the composition builds comes from this repository (no "
            "component whose source is one of its remotes, no pipeline "
            "environment following its promoted branch): name the images with "
            "Step.images(components=[...])"
        )
    return found


def expected(build: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{image: {"key", "source", "commit", "sources"}}`` a wanted build
    makes: ``sources`` is every source's commit its key was made with;
    ``source`` and ``commit`` the train repository's when the image reads it
    (else the composition repository's)."""
    if build.get("kind") == "spec":
        found: dict[str, dict[str, Any]] = {}
        for name, item in build["keys"].items():
            sources = dict((build.get("sources") or {}).get(name) or {})
            primary = build.get("own") if build.get("own") in sources else build["repo"]
            commits = build["commits"]
            found[name] = {
                "key": str(item["key"]),
                "source": str(primary),
                "commit": str(sources.get(primary) or commits[build["repo"]]),
                "sources": sources or {build["repo"]: commits[build["repo"]]},
                "build": str(build["spec"]),
            }
        return found
    return {
        item["component"]: {
            "key": item["digest"],
            "source": item["source"],
            "commit": item["commit"],
            "sources": {item["source"]: item["commit"]},
        }
        for item in build["components"]
    }


def run_images_step(
    step: ImagesStep,
    ctx: StepContext,
    *,
    tag: str,
    tools: ToolsFactory | None = None,
) -> StepResult:
    """Build, push and offer the controller's images (see the module doc);
    never raises for a failure (verdict ``failed``)."""
    started, clock = time.time(), time.monotonic()
    log = log_path(ctx, step.name)
    log.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque(maxlen=120)
    detail: dict[str, Any] = {}
    hosts: list[str] = []

    def say(text: str) -> None:
        tail.append(text)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    try:
        repo = _repository(ctx.checkout)
        with place(step, ctx.checkout, ctx.head, ctx.base_branch, repo=repo) as where:
            _build_and_offer(
                step, ctx, where, repo, tag=tag, tools=tools, say=say,
                tail=tail, detail=detail, hosts=hosts,
            )  # fmt: skip
    except Exception as error:  # every failure fails the step, never the run
        code = getattr(error, "code", None) or "train-images-failed"
        say(f"images: {error} ({code})")
        host = hosts[-1] if hosts else "local"
        return StepResult(
            step.name, step.kind, "failed", started, time.monotonic() - clock, host,
            str(log), "\n".join(list(tail)[-20:]), {**detail, "reason": code},
        )  # fmt: skip
    return StepResult(
        step.name, step.kind, "passed", started, time.monotonic() - clock, hosts[-1],
        str(log), "", detail,
    )  # fmt: skip


def _build_and_offer(
    step: ImagesStep,
    ctx: StepContext,
    where: Place,
    repo: Path,
    *,
    tag: str,
    tools: ToolsFactory | None,
    say: Callable[[str], None],
    tail: deque[str],
    detail: dict[str, Any],
    hosts: list[str],
) -> None:
    """Compute the keys, build, push and offer (see the module doc); the
    build host's name goes to ``hosts``."""
    composition = where.composition
    builds = wanted_builds(
        where, ctx.checkout, repo=repo, names=step.components,
        platforms=step.platforms,
    )  # fmt: skip
    wanted_images = {
        name: item for build in builds for name, item in expected(build).items()
    }
    detail["keys"] = {name: item["key"] for name, item in wanted_images.items()}
    commits = {
        source: commit
        for item in wanted_images.values()
        for source, commit in item["sources"].items()
    }
    detail["sources"] = dict(sorted(commits.items()))
    say(
        "images: "
        + ", ".join(f"{k} {v[:19]}" for k, v in sorted(detail["keys"].items()))
        + " ("
        + ", ".join(f"{k}@{v[:12]}" for k, v in sorted(commits.items()))
        + ")"
    )
    factory = tools or cluster_tools
    with factory(composition, step, ctx) as found:
        hosts.append(found.host)
        began = time.time()
        images: dict[str, dict[str, Any]] = {}
        toolchains: dict[str, Any] = {}
        for build in builds:
            built = dict(found.build(build, tail))
            toolchains.update(found.toolchains() or {})
            for name, item in expected(build).items():
                image = built.get(name)
                if image is None:
                    raise _failed(f"the build made no image {name}")
                if str(image.get("key")) != item["key"]:
                    raise _failed(f"{name} was built from other inputs")
                images[name] = {**dict(image), **item}
        from piceli.gitops.state import prebuilt_request
        from piceli.infra import provenance

        platforms = sorted({p for build in builds for p in build["platforms"]})
        receipt: dict[str, Any] = {
            "host": found.host,
            "image": found.image,
            "started_at": _iso(began),
            "finished_at": _iso(time.time()),
            "seconds": round(time.time() - began, 3),
            "platforms": platforms,
            "commits": dict(sorted(commits.items())),
            "train": {
                "name": str(ctx.origin.get("train") or ""),
                "run": str(ctx.origin.get("run") or ""),
                "tag": tag,
            },
        }
        if toolchains:
            receipt["toolchains"] = toolchains
        record = provenance.from_receipt(receipt, found.host)
        referrers: dict[str, str] = {}
        for build in builds:
            mine = {name: images[name] for name in expected(build)}
            referrers.update(found.attest(build, mine, record) or {})
        if referrers:
            receipt["referrers"] = referrers
        key, body = prebuilt_request(found.host, images, receipt)
        found.offer(key, body)
        detail["images"] = {
            name: {
                "pull_ref": item["pull_ref"],
                "key": item["key"],
                "platforms": sorted(item.get("platforms") or ()),
            }
            for name, item in sorted(images.items())
        }
        detail["offered"] = key
        say(f"images: offered {', '.join(sorted(images))} ({key})")


def _composition(step: ImagesStep, checkout: Path) -> Any:
    from piceli.infra.composition import load_composition

    path = checkout / step.composition
    if not path.is_file():
        raise _failed(f"{step.composition} is not in the composition's commit")
    try:
        return load_composition(path)
    except Exception as error:
        raise _failed(f"{step.composition}: {error}") from None


def _repository(checkout: Path) -> Path:
    """The repository a build host fetches the commit from: the checkout's
    common Git directory (the promoted head is there before any push)."""
    done = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise _failed("the checkout is not in a Git repository")
    return Path(done.stdout.strip())


@contextmanager
def cluster_tools(
    composition: Any, step: ImagesStep, ctx: StepContext
) -> Iterator[ImageTools]:
    """The real tools: a docker build host of the composition's cluster
    (``Cluster(dev=DevBuilds(hosts=[...]))``; the step's ``hosts`` pick one),
    its registry through a port-forward and its controller's request
    channel, with the cluster's credential profile (as ``piceli builder
    serve`` uses them)."""
    from piceli.dev.hosts import ContainerCli
    from piceli.gitops.state import ConfigMapChannel
    from piceli.infra.builder_serve import (
        BuilderServe,
        docker_container,
        ensure_image,
        prepare_cache,
    )
    from piceli.k8s.cli.cluster import connect
    from piceli.pipeline.backend import Backend, RegistryRoute
    from piceli.pipeline.model import ClusterRegistry
    from piceli.profiles import override, resolve

    cluster = composition.cluster
    controller = getattr(cluster, "controller", None)
    if cluster is None or controller is None:
        raise _failed("the composition's cluster declares no controller")
    if not controller.prebuilt:
        raise _failed(
            "the controller takes no prebuilt images: declare Controller(prebuilt=True)"
        )
    if controller.delivery == "nodes":
        raise _failed("prebuilt images go through the registry, not into the nodes")
    registry = cluster.registry
    if not isinstance(registry, ClusterRegistry):
        raise _failed("the cluster needs registry=Registry.in_cluster(...)")
    dev = cluster.dev
    hosts = [
        item
        for item in (dev.hosts if dev is not None else ())
        if item.kind == "docker" and item.runs
    ]
    if step.hosts:
        hosts = [item for item in hosts if item.name in step.hosts] or hosts
    if not hosts or dev is None:
        raise _failed("the cluster declares no docker build host to build on")
    chosen = hosts[0]
    image = str(chosen.image or dev.image)
    cli = ContainerCli(chosen)
    lines: list[str] = []
    cli.platform()
    ensure_image(cli, image, lines.append)
    prepare_cache(cli, image)
    profile = resolve(override() or cluster.credentials)
    backend = Backend()
    route = RegistryRoute(
        push=None,
        node_registry=registry.host,
        forward=f"service/{registry.name}",
        namespace=registry.namespace,
        remote_port=registry.port,
        kubeconfig=profile.kubeconfig,
        context=profile.context,
    )

    def push(image_id: str, repository: str, archive: Path) -> str:
        delivered: dict[str, Any] = backend.registry_deliver(
            route, image_id, repository, archive=archive
        )
        if delivered.get("state") != "succeeded" or not delivered.get("pull_ref"):
            raise _failed(f"push of {repository} ended {delivered.get('state')}")
        return str(delivered["pull_ref"])

    def index(repository: str, digests: Any) -> str:
        return str(backend.registry_index(route, repository, digests))

    def attest(repository: str, digest: str, body: bytes) -> Any:
        return backend.registry_attach(route, repository, digest, body)

    from piceli.train.local import cache_root

    with connect(cluster, "https", False, None) as api:
        channel = ConfigMapChannel(api.client, "piceli-system")
        worker = BuilderServe(
            host=chosen.name,
            image=image,
            channel=channel,
            container=docker_container(cli, image),
            push=push,
            index=index,
            root=cache_root(None).parent / "builder",
            attest=attest,
        )
        yield ImageTools(
            host=chosen.name,
            image=image,
            build=worker.build,
            offer=channel.add_request,
            attest=worker.attach,
            toolchains=lambda: dict(worker.toolchains),
        )
