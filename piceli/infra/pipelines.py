"""Pipeline environments of a composition: images from a host build spec, change-aware.

An ``Environment(pipeline=...)`` deploys a :class:`piceli.Pipeline`'s app.
The composition module and the pipeline live in the composition's own
repository, which the controller follows like a source and imports at the
followed commit (:class:`CompositionRepo`). Its images come from the
pipeline's host builds (``Build.spec("host-build.toml", builder="host")``):

- a ``[context.X] source = "X"`` reads the composition's :class:`Source`
  named ``X`` at the environment's revision; a context without ``source``
  reads the composition repository (paths relative to the spec's directory);
- each ``[[output.image]]`` has a **change key** (:func:`image_keys`): its
  output table, the spec's build table, and the Git blob id of every file the
  contexts it reads include (``contexts = [...]``, default all) at their
  commits. No changed key: nothing is built. Otherwise one build runs, and
  only the images whose key changed take the new image; the others keep their
  cached image and apply as no-op;
- third-party images (``Registry.in_cluster(mirror=[ref@digest])`` of the
  cluster, and the pipeline's own delivery ``mirror=``) are copied into the
  in-cluster registry when the environment syncs; nodes pull them from there.

:func:`environment_pipeline` turns the pipeline into the one that deploys the
environment (its namespace, stack, nodes, quota, the controller's kubeconfig,
the in-cluster registry), so ``env_up`` plans, approves and applies it like
any 0.14 environment, with the pipeline's checks, rollback and approval policy.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import posixpath
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.infra import CompositionError, Source
from piceli.infra.composition import Composition, EnvItem, load_composition

IMAGE_KEY_REVISION = "piceli.image-key.v1"


def _invalid(message: str) -> CompositionError:
    return CompositionError("composition-invalid", message)


# ---------------------------------------------------------------- the repo


@dataclass(frozen=True)
class RepoSettings:
    """The composition's own repository, as the controller config records it.

    :param name: Its source name (a composition :class:`Source` with the same
        URL keeps its name).
    :param url: The remote.
    :param branch: The branch the controller follows (default ``main``).
    :param entry: The composition module, relative to the repository root.
    """

    name: str
    url: str
    branch: str = "main"
    entry: str = "infra.py"

    def __post_init__(self) -> None:
        from piceli.envs.model import Branch, EnvError

        Source(self.url, name=self.name)  # validates both
        try:
            Branch(self.branch)
        except EnvError:
            raise _invalid("the composition branch is one exact branch name") from None
        path = PurePosixPath(self.entry)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".py"
            or "\\" in self.entry
        ):
            raise _invalid("the composition entry is a .py path inside its repository")

    @property
    def source(self) -> Source:
        return Source(self.url, name=self.name)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "url": self.url,
            "branch": self.branch,
            "entry": self.entry,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RepoSettings:
        try:
            return cls(
                name=str(value["name"]),
                url=str(value["url"]),
                branch=str(value.get("branch") or "main"),
                entry=str(value.get("entry") or "infra.py"),
            )
        except (KeyError, TypeError, AttributeError):
            raise _invalid("invalid composition repository settings") from None


class CompositionRepo:
    """Import the composition module from its repository at a commit.

    One working tree is kept per loaded commit under ``root`` (the pipeline's
    spec, checks and module files live there while it is in use); loading a
    new commit removes the others and forgets the modules imported from them,
    so a changed helper module next to ``infra.py`` is imported afresh.

    :param settings: :class:`RepoSettings`.
    :param sources: The :class:`~piceli.infra.sources.SourceSet` holding its
        mirror (the repository is one of its sources).
    :param root: The controller's directory for these checkouts.
    """

    def __init__(self, settings: RepoSettings, sources: Any, root: Path) -> None:
        self.settings = settings
        self.sources = sources
        self.root = root
        self.commit: str | None = None
        self.checkout: Path | None = None
        self.composition: Composition | None = None

    def load(self, commit: str) -> Composition:
        """The composition at ``commit`` (kept when it is already loaded).

        :raises CompositionError: ``composition-invalid`` (the module fails to
            import or is not a valid composition); the previous one stays.
        """
        if commit == self.commit and self.composition is not None:
            return self.composition
        key = self.settings.name
        self.sources.fetch(key)
        tree = self.root / commit[:20] / "src"
        if not (tree / ".git").exists():
            if tree.parent.exists():
                shutil.rmtree(tree.parent)
            tree.parent.mkdir(parents=True)
            remote = self.sources.remotes[key]
            remote._git(
                "clone", "--quiet", "--shared", "--no-checkout",
                str(remote.mirror_dir), str(tree), what="clone",
            )  # fmt: skip
            remote._git(
                "checkout", "--quiet", "--detach", commit, what="checkout", cwd=tree
            )
        try:
            composition = load_composition(self.settings.entry, tree)
        except CompositionError:
            _forget(tree)
            if commit != self.commit:
                shutil.rmtree(tree.parent, ignore_errors=True)
            raise
        previous = self.checkout
        self.commit, self.checkout, self.composition = commit, tree, composition
        for item in self.root.iterdir():
            if item != tree.parent and item.is_dir():
                if previous is not None and item == previous.parent:
                    _forget(previous)
                shutil.rmtree(item, ignore_errors=True)
        return composition


def _forget(tree: Path) -> None:
    """Drop the modules imported from ``tree`` and its ``sys.path`` entries."""
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if isinstance(location, str) and Path(location).is_relative_to(tree):
            del sys.modules[name]
    sys.path[:] = [item for item in sys.path if not Path(item).is_relative_to(tree)]


# ---------------------------------------------------------------- builds


@dataclass(frozen=True)
class SpecBuild:
    """One host build of a pipeline environment.

    :param name: The spec's name.
    :param path: The spec file, relative to the composition repository root.
    :param spec: The parsed spec (read at the environment's commit).
    :param contexts: Context name → ``(source, directory in it)``.
    :param facts: The ``Build(node_facts=)`` the owner declared, if any.
    """

    name: str
    path: str
    spec: Any
    contexts: Mapping[str, tuple[str, str]]
    facts: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ImageKey:
    """An output image's change key and the sources it reads."""

    build: str
    image: str
    key: str
    sources: Mapping[str, str] = field(default_factory=dict)


def spec_paths(pipeline: Any, checkout: Path) -> list[tuple[str, Any]]:
    """``(spec path relative to the composition repository, Build)`` per build.

    :raises CompositionError: ``component-build-unsupported`` for a Docker
        build (it needs a Docker engine); ``composition-invalid`` for a spec
        outside the repository.
    """
    found: list[tuple[str, Any]] = []
    for build in pipeline.builds:
        if build.builder != "host" or build.path is None:
            raise CompositionError(
                "component-build-unsupported",
                f"pipeline {pipeline.name!r}: a composition builds host builds "
                '(Build.spec("host-build.toml", builder="host")); a Docker build '
                "needs a Docker engine",
            )
        try:
            relative = build.path.resolve().relative_to(checkout.resolve())
        except ValueError:
            raise _invalid(
                f"pipeline {pipeline.name!r}: its build spec is outside the "
                "composition repository"
            ) from None
        found.append((relative.as_posix(), build))
    return found


def _norm(path: str) -> str:
    value = posixpath.normpath(path or ".")
    if value.startswith("..") or value.startswith("/"):
        raise _invalid("a build context points outside its repository")
    return "" if value == "." else value


def load_spec(
    text: bytes, path: str, *, repo: str, facts: Mapping[str, Any] | None = None
) -> SpecBuild:
    """Parse a host build spec read from the composition repository at ``path``.

    A context with ``source = "X"`` reads source ``X`` (``path`` inside it);
    one without reads the composition repository ``repo``, relative to the
    spec's directory.

    :raises CompositionError: ``composition-invalid``.
    """
    from piceli.artifacts.build_spec import BuildSpecError
    from piceli.artifacts.host_build import HOST_BUILD_REVISION, HostBuildSpec

    try:
        document = tomllib.loads(text.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise _invalid(f"the build spec {path} is not valid TOML") from None
    if document.get("revision") != HOST_BUILD_REVISION:
        raise _invalid(f"the build spec {path} is not a {HOST_BUILD_REVISION} spec")
    directory = posixpath.dirname(path)
    try:
        spec = HostBuildSpec.from_dict(document, Path("/") / directory)
    except BuildSpecError as error:
        raise _invalid(f"the build spec {path} is invalid ({error.code})") from None
    contexts: dict[str, tuple[str, str]] = {}
    for context in spec.contexts:
        if context.source is None:
            contexts[context.name] = (
                repo,
                _norm(posixpath.join(directory, context.path or ".")),
            )
        else:
            contexts[context.name] = (context.source, _norm(context.path or "."))
    return SpecBuild(spec.name, path, spec, contexts, facts)


def context_files(
    sources: Any, source: str, commit: str, prefix: str, selection: Any
) -> list[list[str]]:
    """``[path, mode, blob id]`` of the files a context includes at ``commit``.

    The same selection a staged build makes (include and exclude globs, pruned
    directories, private files), read from Git without a working tree.
    """
    from piceli.artifacts.build_context import PRUNED_DIRECTORIES, Glob, _is_private

    includes = [Glob(item) for item in selection.include]
    patterns = [item.regex() for item in includes]
    excludes = [Glob(item).regex() for item in selection.exclude]
    files: list[list[str]] = []
    for path, mode, oid in sources.tree(source, commit, prefix):
        parts = tuple(path.split("/"))
        if any(
            parts[depth] in PRUNED_DIRECTORIES
            and not any(item.names_literally(parts[: depth + 1]) for item in includes)
            for depth in range(len(parts) - 1)
        ):
            continue
        if not any(item.fullmatch(path) for item in patterns) or any(
            item.fullmatch(path) for item in excludes
        ):
            continue
        if _is_private(path):
            continue
        files.append([path, mode, oid])
    return sorted(files)


def _digest(value: Any) -> str:
    from piceli.artifacts.plan import canonical, digest

    return digest(canonical(value))


def build_table(spec: Any) -> dict[str, Any]:
    """The spec's ``[build]`` (tools, commands, env, platforms …), without contexts and images."""
    return {
        key: value
        for key, value in spec.to_dict().items()
        if key not in ("contexts", "images", "inputs")
    }


def image_keys(
    build: SpecBuild,
    revision: Mapping[str, str],
    sources: Any,
    *,
    platform: str,
) -> dict[str, ImageKey]:
    """Each output image's change key at ``revision`` (``{source: commit}``).

    :raises CompositionError: ``composition-invalid`` when a context reads a
        source the environment does not follow.
    """
    spec = build.spec
    files: dict[str, Any] = {}
    for context in spec.contexts:
        source, prefix = build.contexts[context.name]
        if source not in revision:
            raise _invalid(
                f"build {build.name!r}: context {context.name!r} reads source "
                f"{source!r}, which the environment does not follow"
            )
        sources.fetch(source)
        files[context.name] = {
            "context": context.to_dict(),
            "files": context_files(
                sources, source, revision[source], prefix, context.selection
            ),
        }
    table = build_table(spec)
    names = tuple(item.name for item in spec.contexts)
    keys: dict[str, ImageKey] = {}
    for image in spec.images:
        reads = image.reads(names)
        body = {
            "revision": IMAGE_KEY_REVISION,
            "platform": platform,
            "facts": dict(build.facts) if build.facts else None,
            "build": table,
            "image": image.to_dict(),
            "contexts": {name: files[name] for name in reads},
        }
        keys[image.name] = ImageKey(
            build.name,
            image.name,
            _digest(body),
            {
                build.contexts[name][0]: revision[build.contexts[name][0]]
                for name in reads
            },
        )
    return keys


@dataclass(frozen=True)
class SpecBuildRequest:
    """One build of a host spec: the sources at their commits, the images to push.

    :param spec: The spec path relative to the composition repository.
    :param repo: The composition repository's source name.
    :param commits: Every source the build reads, at its commit (the
        composition repository included).
    :param images: Image name → ``{"repository", "key"}`` of the images to
        push (those whose key changed); the build compiles every image.
    :param facts: Declared node facts (``Build(node_facts=)``), if any.
    :param urls: Each source's URL (the build Job fetches them).
    """

    spec: str
    repo: str
    commits: Mapping[str, str]
    images: Mapping[str, Mapping[str, str]]
    facts: Mapping[str, Any] | None = None
    urls: Mapping[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "spec": self.spec,
            "repo": self.repo,
            "commits": dict(sorted(self.commits.items())),
            "images": {k: dict(v) for k, v in sorted(self.images.items())},
        }


def inputs_for(build: Any, roots: Mapping[str, Path]) -> Any:
    """The ``InputsSpec`` that maps each context source to its checkout."""
    from piceli.artifacts.source_identity import InputsSpec, SourceSpec

    names = sorted({c.source for c in build.contexts if c.source is not None})
    if not names:
        return None
    missing = [name for name in names if name not in roots]
    if missing:
        raise _invalid(f"no checkout of source {missing[0]!r}")
    return InputsSpec(
        tuple(SourceSpec(name, str(roots[name])) for name in names),
        roots={name: roots[name] for name in names},
    )


def run_spec_build(
    request: SpecBuildRequest,
    roots: Mapping[str, Path],
    *,
    platforms: Sequence[str],
    cache: Path,
    out: Path,
    deliver: Any,
    say: Any = lambda _: None,
) -> dict[str, Any]:
    """Build ``request``'s spec from checkouts (``roots``) and push its changed images.

    ``deliver(image, archive, image_id, repository)`` pushes one image and
    returns its delivery receipt. Returns ``{"images": {name: {"pull_ref",
    "manifest_digest", "key", "platform"}}}`` for the first platform.
    """
    import time

    from piceli.artifacts.build_spec import BuildSpecError
    from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
    from piceli.artifacts.node_facts import NodeFacts
    from piceli.infra.builders import _pinned

    root = roots[request.repo]
    path = root.joinpath(*PurePosixPath(request.spec).parts)
    results: dict[str, Any] = {}
    declared = NodeFacts.from_dict(request.facts) if request.facts else None
    for platform in platforms:
        facts = (
            declared
            if declared is not None and declared.platform == platform
            else NodeFacts.for_platform(platform)
        )
        try:
            spec = HostBuildSpec.from_toml(path)
            inputs = inputs_for(spec, roots)
            spec = spec.for_node(facts).with_cache_dir(cache)
            plan = spec.plan(inputs)
            grant = HostBuildGrant(plan.plan_hash, time.time() + 7200)
            directory = out / platform.replace("/", "-") / spec.name
            say(f"[build] {spec.name} for {platform}")
            receipt = spec.run(grant, directory, inputs=inputs, progress=say).to_dict()
        except BuildSpecError as error:
            from piceli.infra.builders import build_failed

            raise build_failed(
                f"build {request.spec}: host build failed ({error.code})", error
            ) from None
        for name, wanted in sorted(request.images.items()):
            entry = receipt["outputs"]["images"].get(name)
            if entry is None:
                raise CompositionError(
                    "component-build-failed", f"the build made no image {name!r}"
                )
            delivered = deliver(
                name,
                (directory / entry["archive"]).absolute(),
                entry["image_id"],
                wanted["repository"],
            )
            image = _pinned(delivered, name)
            results.setdefault(
                name, {**image.to_dict(), "platform": platform, "key": wanted["key"]}
            )
    return {"images": results}


# ---------------------------------------------------------------- render


def mirror_list(composition: Composition, pipeline: Any) -> tuple[str, ...]:
    """Third-party images of the environment: the cluster registry's and the pipeline's."""
    from piceli.pipeline.model import NodeLoopbackRegistry, Registry

    found = list(getattr(composition.registry, "mirror", ()) or ())
    deliver = getattr(pipeline, "deliver", None)
    if isinstance(deliver, NodeLoopbackRegistry | Registry):
        found += list(deliver.mirror)
    return tuple(dict.fromkeys(found))


def delivery(composition: Composition, pipeline: Any) -> Any:
    """The in-cluster registry as the pipeline's delivery (its repository, every mirror)."""
    registry = composition.registry
    return dataclasses.replace(
        registry,
        repository=registry.repository or composition.name,
        mirror=mirror_list(composition, pipeline),
    )


def env_config_of(composition: Composition, env: EnvItem) -> Any:
    """The ``EnvConfig`` holding exactly ``env`` (its stack, nodes and quota)."""
    from piceli.envs.model import BranchEnvironments, EnvConfig, Environment

    if isinstance(env, BranchEnvironments):
        return dataclasses.replace(env.env_config(), branch_stack=env.stack)
    single = Environment(
        env.name,
        namespace=env.namespace,
        stack=env.stack,
        on_nodes=env.on_nodes,
        quota=env.quota,
        auto_approve=env.auto_approve,
    )
    prefix = (composition.name + "-")[:39].rstrip("-") + "-"
    if not prefix[0].isalpha():
        prefix = "env-" + prefix
    return EnvConfig(
        prefix=prefix[:40],
        main_branch=env.name,
        branches=(env.name,),
        environments=[single],
    )


def environment_pipeline(
    composition: Composition,
    env: EnvItem,
    *,
    kubeconfig: Path,
    context: str,
    state_dir: Path,
    transport: str = "https",
) -> Any:
    """The pipeline that deploys ``env``: the declared one, in the controller's hands.

    The app, checks, rollback, restore points and approval policy are the
    pipeline's; the target is the controller's kubeconfig and the
    environment's namespace (the declared cluster UID and node aliases stay),
    the delivery is the in-cluster registry (``env_up`` is given the built
    images by digest), and ``envs`` is this one environment.

    :raises CompositionError: ``composition-invalid`` for a pipeline with one
        target per app environment (select one: ``pipeline.for_environment``).
    """
    from piceli.approval_policy import ApprovalPolicy
    from piceli.envs.model import Environment
    from piceli.pipeline.model import Target

    pipeline = env.pipeline
    if pipeline.needs_environment:
        raise _invalid(
            f"environment {env.name!r}: its pipeline has one target per app "
            "environment; pass pipeline.for_environment(NAME)"
        )
    declared = pipeline.target
    namespace = env.namespace if isinstance(env, Environment) else env.prefix + "main"
    derived = copy.copy(pipeline)
    derived._target = Target(
        kubeconfig,
        context=context,
        namespace=namespace,
        cluster_uid=declared.cluster_uid,
        nodes=declared.nodes,
        transport=transport,
        request_seconds=declared.request_seconds,
    )
    derived.state_dir = state_dir / env.name
    derived.deliver = delivery(composition, pipeline)
    derived.envs = env_config_of(composition, env)
    if derived.auto_approve is None and env.auto_approve:
        derived.auto_approve = ApprovalPolicy()
    return derived


def mirror_target(composition: Composition, pipeline: Any, reference: str) -> str:
    """The in-cluster repository a third-party image is copied to."""
    from piceli.pipeline.compose import mirror_repository

    holder = copy.copy(pipeline)
    holder.deliver = delivery(composition, pipeline)
    return mirror_repository(holder, reference)


def key_of(reference: str) -> str:
    """A cache key for a mirrored reference."""
    return "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
