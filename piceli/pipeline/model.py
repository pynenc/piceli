"""Typed declarations of a deployment pipeline: target, build, delivery, pipeline.

Everything here is a declaration: constructing these objects reads no file,
contacts no cluster and runs no tool. ``piceli deploy`` (see
:mod:`piceli.pipeline.runner`) turns a :class:`Pipeline` into one journaled run
of the stages ``inputs → build → deliver → plan → apply → checks``.

Relative paths (a kubeconfig, a build spec, the state directory) resolve from
the directory of the Python file that declares them, so
``piceli deploy deploy/app.py:pipeline`` works from any working directory.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, overload

from piceli.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from piceli.app import App
    from piceli.artifacts.build_spec import BuildSpec
    from piceli.k8s.ops.exec_credentials import ExecPolicy
    from piceli.pipeline.secrets import Secrets

#: Host of the placeholder a build image handle renders to before delivery.
HANDLE_HOST = "pipeline.piceli.invalid"
_HANDLE = re.compile(re.escape(HANDLE_HOST) + r"/([a-z0-9][a-z0-9_-]{0,62}):unresolved")
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PINNED = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
STAGES = ("inputs", "build", "deliver", "plan", "apply", "checks")


def _caller_dir() -> Path | None:
    """Directory of the first calling file outside this package, if any."""
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if not str(name).startswith("piceli.pipeline"):
            path = frame.f_globals.get("__file__")
            return Path(path).resolve().parent if isinstance(path, str) else None
        frame = frame.f_back  # type: ignore[assignment]
    return None  # pragma: no cover


def _resolve(value: str | Path, base: Path | None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or base is None:
        return path
    return base / path


# ------------------------------------------------------------------ target


class TargetNode(NamedTuple):
    """A node the target must have: its name and, optionally, its UID."""

    name: str
    uid: str | None = None


class _KubeconfigAccessor:
    """``Target.kubeconfig(...)`` builds a target; ``target.kubeconfig`` is its path."""

    @overload
    def __get__(self, instance: None, owner: type[Target]) -> Callable[..., Target]: ...

    @overload
    def __get__(self, instance: Target, owner: type[Target]) -> Path: ...

    def __get__(
        self, instance: Target | None, owner: type[Target]
    ) -> Callable[..., Target] | Path:
        if instance is None:
            return owner.from_kubeconfig
        return instance._kubeconfig


class Target:
    """The cluster and namespace a pipeline deploys to, reached by an explicit kubeconfig.

    Build one with :meth:`Target.kubeconfig`. Piceli never falls back to
    ``~/.kube/config``, ``KUBECONFIG`` or the current context.

    Attributes: ``kubeconfig`` (path), ``context``, ``namespace``,
    ``cluster_uid``, ``namespace_uid``, ``nodes`` (alias →
    :class:`TargetNode`), ``transport``, ``request_seconds`` and the exec
    credential plugin opt-in ``allow_exec``, ``exec_sha256``,
    ``exec_pass_env`` and ``exec_timeout_seconds`` (the same keys and
    semantics as ``[target]`` in ``release.toml``; see
    ``docs/managed_clusters.md``).

    Invariants: every node alias is a lowercase identifier; with
    ``cluster_uid``/node UIDs set, the release verifies them against the
    server before planning; a context whose user runs an exec plugin is
    refused unless ``allow_exec=True``, and the ``exec_*`` options need it.

    Example::

        target = Target.kubeconfig(
            "cluster.kubeconfig", context="kind-shop", namespace="shop",
            nodes={"primary": ("shop-control-plane", None)},
        )
    """

    kubeconfig = _KubeconfigAccessor()

    def __init__(
        self,
        kubeconfig: str | Path,
        *,
        context: str,
        namespace: str,
        cluster_uid: str | None = None,
        namespace_uid: str | None = None,
        nodes: Mapping[str, TargetNode | tuple[str, str | None] | str] | None = None,
        transport: str = "https",
        request_seconds: float = 10.0,
        allow_exec: bool = False,
        exec_sha256: str | None = None,
        exec_pass_env: Sequence[str] = (),
        exec_timeout_seconds: float | None = None,
        base: Path | None = None,
    ) -> None:
        if not isinstance(context, str) or not context:
            raise PipelineError("pipeline-invalid", "target context is required")
        if not isinstance(namespace, str) or not _LABEL.fullmatch(namespace):
            raise PipelineError(
                "pipeline-invalid", f"invalid target namespace {namespace!r}"
            )
        if transport not in {"https", "loopback-http"}:
            raise PipelineError(
                "pipeline-invalid", "target transport must be https or loopback-http"
            )
        parsed: dict[str, TargetNode] = {}
        for alias, value in (nodes or {}).items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise PipelineError("pipeline-invalid", f"invalid node alias {alias!r}")
            if isinstance(value, str):
                node = TargetNode(value)
            else:
                name, uid = tuple(value)
                node = TargetNode(str(name), None if uid is None else str(uid))
            parsed[alias] = node
        if not allow_exec and (
            exec_sha256 is not None or exec_pass_env or exec_timeout_seconds is not None
        ):
            raise PipelineError(
                "pipeline-invalid", "exec_* target options require allow_exec=True"
            )
        if isinstance(exec_pass_env, str):
            raise PipelineError(
                "pipeline-invalid", "exec_pass_env must be a list of variable names"
            )
        from piceli.k8s.ops.exec_credentials import ExecPolicy

        try:
            ExecPolicy(
                allow_exec,
                exec_sha256,
                tuple(exec_pass_env),
                60.0 if exec_timeout_seconds is None else float(exec_timeout_seconds),
            )
        except (TypeError, ValueError) as error:
            raise PipelineError("pipeline-invalid", str(error)) from None
        values = {
            "_kubeconfig": _resolve(kubeconfig, base),
            "context": context,
            "namespace": namespace,
            "cluster_uid": cluster_uid,
            "namespace_uid": namespace_uid,
            "nodes": dict(sorted(parsed.items())),
            "transport": transport,
            "request_seconds": float(request_seconds),
            "allow_exec": allow_exec,
            "exec_sha256": exec_sha256,
            "exec_pass_env": tuple(exec_pass_env),
            "exec_timeout_seconds": exec_timeout_seconds,
        }
        for key, item in values.items():
            object.__setattr__(self, key, item)

    _kubeconfig: Path
    context: str
    namespace: str
    cluster_uid: str | None
    namespace_uid: str | None
    nodes: dict[str, TargetNode]
    transport: str
    request_seconds: float
    allow_exec: bool
    exec_sha256: str | None
    exec_pass_env: tuple[str, ...]
    exec_timeout_seconds: float | None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Target is immutable")

    @classmethod
    def from_kubeconfig(
        cls,
        kubeconfig: str | Path,
        *,
        context: str,
        namespace: str,
        cluster_uid: str | None = None,
        namespace_uid: str | None = None,
        nodes: Mapping[str, TargetNode | tuple[str, str | None] | str] | None = None,
        transport: str = "https",
        request_seconds: float = 10.0,
        allow_exec: bool = False,
        exec_sha256: str | None = None,
        exec_pass_env: Sequence[str] = (),
        exec_timeout_seconds: float | None = None,
    ) -> Target:
        """A target reached with ``kubeconfig`` and ``context`` (both explicit).

        :param kubeconfig: The kubeconfig file; relative to the declaring file.
        :param context: The context in it (never ``current-context``).
        :param namespace: The namespace; it must already exist.
        :param cluster_uid: Expected ``kube-system`` namespace UID, if pinned.
        :param namespace_uid: Expected namespace UID, if pinned.
        :param nodes: Alias → ``(node name, uid or None)`` the target must have.
        :param transport: ``https``, or ``loopback-http`` for a local test API.
        :param request_seconds: Per-request timeout.
        :param allow_exec: Allow the context's exec credential plugin (GKE,
            EKS, AKS, OIDC); without it an exec user is refused.
        :param exec_sha256: Expected ``sha256:<hex>`` of the resolved plugin.
        :param exec_pass_env: Extra variables passed to the plugin
            (``PATH`` and ``HOME`` always are).
        :param exec_timeout_seconds: Limit for one plugin run (default 60).
        """
        return cls(
            kubeconfig,
            context=context,
            namespace=namespace,
            cluster_uid=cluster_uid,
            namespace_uid=namespace_uid,
            nodes=nodes,
            transport=transport,
            request_seconds=request_seconds,
            allow_exec=allow_exec,
            exec_sha256=exec_sha256,
            exec_pass_env=exec_pass_env,
            exec_timeout_seconds=exec_timeout_seconds,
            base=_caller_dir(),
        )

    def node(self, alias: str | None) -> tuple[str, TargetNode]:
        """The node named by ``alias``, or the only declared node."""
        if alias is None:
            if len(self.nodes) != 1:
                raise PipelineError(
                    "pipeline-invalid",
                    "this delivery needs a node: declare exactly one node in the "
                    "target or name its alias with node=",
                )
            return next(iter(self.nodes.items()))
        if alias not in self.nodes:
            raise PipelineError(
                "pipeline-invalid",
                f"node alias {alias!r} is not declared by the target; declared: "
                f"{sorted(self.nodes)}",
            )
        return alias, self.nodes[alias]

    def exec_policy(self) -> ExecPolicy:
        """The explicit authority to run the context's exec plugin (if any)."""
        from piceli.k8s.ops.exec_credentials import ExecPolicy

        return ExecPolicy(
            self.allow_exec,
            self.exec_sha256,
            self.exec_pass_env,
            60.0 if self.exec_timeout_seconds is None else self.exec_timeout_seconds,
        )

    def exec_table(self) -> dict[str, Any]:
        """The ``[target]`` exec keys of a release spec (empty without allow_exec)."""
        if not self.allow_exec:
            return {}
        table: dict[str, Any] = {
            "allow_exec": True,
            "exec_pass_env": list(self.exec_pass_env),
        }
        if self.exec_sha256 is not None:
            table["exec_sha256"] = self.exec_sha256
        if self.exec_timeout_seconds is not None:
            table["exec_timeout_seconds"] = self.exec_timeout_seconds
        return table

    def identity(self) -> dict[str, Any]:
        """What an approval binds to (never the kubeconfig path)."""
        return {
            "context": self.context,
            "namespace": self.namespace,
            "cluster_uid": self.cluster_uid,
            "namespace_uid": self.namespace_uid,
            "nodes": {alias: list(node) for alias, node in self.nodes.items()},
        }

    def __repr__(self) -> str:
        return (
            f"Target(context={self.context!r}, namespace={self.namespace!r}, "
            f"nodes={sorted(self.nodes)})"
        )


# ------------------------------------------------------------------- build


class ImageHandle(str):
    """A built image a workload can use before it exists.

    ``build["api"]`` returns one. It renders as a placeholder
    (``pipeline.piceli.invalid/api:unresolved``) and ``piceli deploy``
    replaces it with the immutable reference from the delivery receipt
    (``registry@sha256:…`` or a node content tag), never a movable tag.
    """

    image: str

    def __new__(cls, image: str) -> ImageHandle:
        if not isinstance(image, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,62}", image
        ):
            raise PipelineError("pipeline-invalid", f"invalid image name {image!r}")
        value = super().__new__(cls, f"{HANDLE_HOST}/{image}:unresolved")
        value.image = image
        return value


def handle_image(value: Any) -> str | None:
    """The image name of a rendered handle placeholder, else ``None``."""
    if not isinstance(value, str):
        return None
    match = _HANDLE.fullmatch(value)
    return match[1] if match else None


@dataclass(frozen=True, init=False)
class Smoke:
    r"""A smoke check run in a built image before its receipt is written.

    The same fields as a ``smoke = {…}`` table in ``build.toml`` (see
    :doc:`containerized_builds`). The container is isolated: no network, a
    read-only root filesystem, no capabilities, bounded memory, processes and
    time.

    :param command: Arguments after the image (replace ``CMD``); empty runs the
        image's default command.
    :param env: Plain environment values for the check. They are part of the
        plan hash and are recorded in previews and receipts, so they must not
        be secrets (values that look like secret references are refused).
    :param entrypoint: Replaces the image's entrypoint (``["/bin/sh", "-c"]``
        for a shell check); the image's ``CMD`` is then not used.
    :param expect_exit: The exit code that passes (default 0).
    :param expect_stdout: A regular expression searched (``re.MULTILINE``) in
        the first 256 KiB of stdout.
    :param expect_stderr: The same for stderr.
    :param timeout_seconds: Time limit, at most 600 (default 60).

    Invalid values raise :class:`PipelineError` when the check is declared.

    Example::

        Smoke(["--version"], expect_stdout=r"^api \d+\.\d+")
    """

    command: tuple[str, ...]
    env: Mapping[str, str]
    entrypoint: tuple[str, ...] | None
    expect_exit: int
    expect_stdout: str | None
    expect_stderr: str | None
    timeout_seconds: float

    def __init__(
        self,
        command: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        entrypoint: Sequence[str] | None = None,
        expect_exit: int = 0,
        expect_stdout: str | None = None,
        expect_stderr: str | None = None,
        timeout_seconds: float = 60,
    ) -> None:
        if isinstance(command, str) or isinstance(entrypoint, str):
            raise PipelineError(
                "invalid-spec", "smoke command and entrypoint are lists of strings"
            )
        values = {
            "command": tuple(command),
            "env": dict(env or {}),
            "entrypoint": tuple(entrypoint) if entrypoint is not None else None,
            "expect_exit": expect_exit,
            "expect_stdout": expect_stdout,
            "expect_stderr": expect_stderr,
            "timeout_seconds": timeout_seconds,
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)
        _check_smoke(self.to_table())

    def to_table(self) -> dict[str, Any]:
        """The ``smoke`` table of a build spec (only the fields that are set)."""
        table: dict[str, Any] = {
            "command": list(self.command),
            "expect_exit": self.expect_exit,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.env:
            table["env"] = dict(self.env)
        if self.entrypoint is not None:
            table["entrypoint"] = list(self.entrypoint)
        if self.expect_stdout is not None:
            table["expect_stdout"] = self.expect_stdout
        if self.expect_stderr is not None:
            table["expect_stderr"] = self.expect_stderr
        return table


def _check_smoke(table: Mapping[str, Any]) -> None:
    from piceli.artifacts.build_spec import BuildSpecError, parse_smoke

    try:
        parse_smoke(dict(table))
    except BuildSpecError as error:
        raise PipelineError(error.code, str(error)) from None


class Build:
    """A containerized build (``piceli artifacts build-spec``) whose images a pipeline deploys.

    Create it with :meth:`Build.spec` (an existing ``build.toml``) or
    :meth:`Build.dockerfile` (a Dockerfile/Containerfile with one image per
    target stage). ``build["name"]`` is an :class:`ImageHandle` for one output
    image. Declaring a build reads nothing; ``piceli deploy`` plans it (a hash
    over the staged files) and skips it when its last receipt has the same
    plan hash and the images are still in the local engine.

    Only single-platform builds can feed a pipeline (a workload runs one
    image).
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        document: Mapping[str, Any] | None = None,
        base: Path | None = None,
        images: Sequence[str] | None = None,
        lock: Path | None = None,
    ) -> None:
        if (path is None) == (document is None):
            raise PipelineError("pipeline-invalid", "a build needs a path or document")
        self.path = path
        self.document = dict(document) if document is not None else None
        self.base = base
        self.declared_images = tuple(images) if images is not None else None
        self.lock = lock
        self._spec: BuildSpec | None = None

    @classmethod
    def spec(cls, path: str | Path, *, lock: str | Path | None = None) -> Build:
        """A build described by a ``build.toml`` (relative to the declaring file).

        :param path: The build spec.
        :param lock: Optional ``inputs`` lock the sources must match.
        """
        base = _caller_dir()
        return cls(
            path=_resolve(path, base),
            base=base,
            lock=_resolve(lock, base) if lock is not None else None,
        )

    @classmethod
    def dockerfile(
        cls,
        dockerfile: str,
        *,
        builder: str,
        targets: Sequence[str],
        context: str = ".",
        include: Sequence[str] = ("**",),
        exclude: Sequence[str] = (),
        sources: str | Path | None = None,
        source: str | None = None,
        platform: str = "linux/amd64",
        builder_arg: str = "BUILDER",
        args: Mapping[str, str] | None = None,
        repository: str | None = None,
        name: str | None = None,
        network: str = "none",
        timeout_seconds: float = 1800,
        smoke: Mapping[str, Smoke | Mapping[str, Any]] | None = None,
    ) -> Build:
        """One image per Dockerfile target stage, built in a pinned builder.

        :param dockerfile: Path of the Dockerfile inside ``context``. It must
            start ``ARG <builder_arg>`` / ``FROM ${<builder_arg>}`` (see
            :doc:`containerized_builds`).
        :param builder: The builder image pinned by digest (``image@sha256:…``).
        :param targets: Stage names; each becomes an image of the same name.
        :param context: Build context directory (relative to the declaring
            file, or to ``source`` when given).
        :param include: Files of the context that are staged (globs).
        :param exclude: Globs removed from ``include``.
        :param sources: An ``inputs.toml`` whose source identity is recorded.
        :param source: The source (from ``sources``) the context lives in.
        :param platform: The one platform to build.
        :param builder_arg: The ``ARG`` naming the builder image.
        :param args: Extra build args.
        :param repository: Local repository prefix; default ``piceli-build/<name>``.
        :param name: Build name; default the first target.
        :param network: ``none`` (default) or ``default``.
        :param timeout_seconds: Build time limit.
        :param smoke: Smoke checks by target name, each a :class:`Smoke` (or a
            mapping with the ``build.toml`` smoke keys). A failing check fails
            the build; the checks are part of the build's plan hash.
        """
        from piceli.artifacts.build_spec import BUILD_SPEC_REVISION

        if not targets:
            raise PipelineError("pipeline-invalid", "declare at least one target")
        smoke_tables: dict[str, dict[str, Any]] = {}
        for target, check in (smoke or {}).items():
            if target not in targets:
                raise PipelineError(
                    "pipeline-invalid", f"smoke names unknown target {target!r}"
                )
            table = check.to_table() if isinstance(check, Smoke) else dict(check)
            _check_smoke(table)
            smoke_tables[target] = table
        builder_image, sep, digest = builder.partition("@")
        if not sep or not digest.startswith("sha256:"):
            raise PipelineError(
                "pipeline-invalid", "builder must be pinned as image@sha256:<digest>"
            )
        name = name or targets[0]
        prefix = repository or f"piceli-build/{name}"
        context_table: dict[str, Any] = {
            "include": list(include),
            "exclude": list(exclude),
        }
        if context not in ("", "."):
            context_table["path"] = context
        if source is not None:
            context_table["source"] = source
        document: dict[str, Any] = {
            "revision": BUILD_SPEC_REVISION,
            "name": name,
            "builder": {"image": builder_image, "digest": digest},
            "build": {
                "platforms": [platform],
                "dockerfile": dockerfile,
                "context": "src",
                "builder_arg": builder_arg,
                "args": dict(args or {}),
                "network": network,
                "timeout_seconds": timeout_seconds,
            },
            "context": {"src": context_table},
            "output": {
                "image": [
                    {
                        "name": target,
                        "repository": f"{prefix}/{target}",
                        "tag": "{image_id:12}",
                        "target": target,
                        **(
                            {"smoke": smoke_tables[target]}
                            if target in smoke_tables
                            else {}
                        ),
                    }
                    for target in targets
                ]
            },
        }
        base = _caller_dir()
        if sources is not None:
            document["inputs"] = str(_resolve(sources, base))
        build = cls(document=document, base=base, images=list(targets))
        for target in targets:
            ImageHandle(target)  # validates the name
        return build

    def __getitem__(self, image: str) -> ImageHandle:
        if self.declared_images is not None and image not in self.declared_images:
            raise PipelineError(
                "pipeline-image-unknown",
                f"build has no image {image!r}; images: {list(self.declared_images)}",
            )
        return ImageHandle(image)

    def load(self) -> BuildSpec:
        """Parse and validate the build spec (reads the spec file once)."""
        if self._spec is None:
            from piceli.artifacts.build_spec import BuildSpec

            if self.path is not None:
                spec = BuildSpec.from_toml(self.path)
            else:
                assert self.document is not None
                spec = BuildSpec.from_dict(self.document, self.base or Path.cwd())
            if len(spec.platforms) != 1:
                raise PipelineError(
                    "pipeline-invalid",
                    f"build {spec.name!r} targets {len(spec.platforms)} platforms; a "
                    "pipeline build must target exactly one",
                )
            self._spec = spec
        return self._spec

    def image_names(self) -> tuple[str, ...]:
        """The images this build produces (loads the spec)."""
        return tuple(item.name for item in self.load().images)

    def __repr__(self) -> str:
        where = self.path.name if self.path else "dockerfile"
        return f"Build({where})"


# ---------------------------------------------------------------- delivery


def _mirrors(value: Any) -> tuple[str, ...]:
    """Validate ``mirror=`` entries: digest-pinned references, canonical, unique."""
    from piceli.artifacts.mirror import MirrorError, MirrorSource

    if isinstance(value, str):
        raise PipelineError(
            "pipeline-invalid", "mirror must be a list of image references"
        )
    keys: dict[str, None] = {}
    for item in value or ():
        try:
            keys[MirrorSource.parse(item).key] = None
        except MirrorError as error:
            raise PipelineError(error.code, str(error)) from None
    return tuple(keys)


def _mirror_credentials(value: Any, base: Path | None) -> dict[str, Path]:
    """``{registry: credentials file}``; files resolve from the declaring file."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PipelineError(
            "pipeline-invalid",
            "mirror_credentials maps a registry (docker.io, ghcr.io:443 …) to a "
            "private credentials file",
        )
    result: dict[str, Path] = {}
    for registry, path in value.items():
        if not isinstance(registry, str) or not re.fullmatch(
            r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", registry
        ):
            raise PipelineError(
                "pipeline-invalid", f"invalid mirror_credentials registry {registry!r}"
            )
        result[registry.lower()] = _resolve(path, base)
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class NodeLoopbackRegistry:
    """Deliver built images to a registry on one node's loopback (no public registry).

    ``piceli deploy`` releases a
    :class:`~piceli.k8s.templates.NodeLocalRegistry` (its own release and
    owner, never part of the app's release), pushes each image through a
    supervised ``kubectl port-forward``, and the app pulls
    ``127.0.0.1:<port>/<repository>@sha256:…``. Workloads that use a built
    or mirrored image and have no node selection of their own are pinned to
    that node.

    :param port: Registry port on the node loopback.
    :param node: Target node alias; default the only declared node.
    :param name: Base name of the registry objects.
    :param storage: Size of the registry's retained volume claim.
    :param image: Registry image pinned by digest; default the template's.
    :param repository: Repository prefix; default the app name.
    :param host_path: Keep the registry data in this node directory instead
        of a volume claim.
    :param existing_claim: Keep the registry data on this existing claim
        (never created, changed or deleted) instead of ``<name>-storage``.
    :param mirror: Third-party images to copy by digest into the registry
        (``docker.io/library/redis@sha256:…``); the app's references to them
        are rewritten to ``127.0.0.1:<port>/mirror/<registry>/<repository>``
        with the same digest. A tag without a digest is refused.
    :param mirror_credentials: ``{registry: credentials file}`` for mirror
        sources that need a login (private ``0600`` JSON file, as for
        :class:`Registry`); other sources are pulled anonymously.
    :param adopt: Name of an existing loopback registry Deployment in the
        namespace to take over (its objects are adopted by the registry
        release; its data stays). The registry objects take this name.
    :param replace: Like ``adopt``, but the existing Deployment is deleted
        and recreated (a backup is written first); for a registry whose
        selector, port or storage cannot be taken over in place.
    :param inherited_owners: Earlier owners of the registry objects whose
        retained objects (the claim) this registry release may take over.
    """

    port: int = 5000
    node: str | None = None
    name: str = "registry"
    storage: str = "10Gi"
    image: str | None = None
    repository: str | None = None
    host_path: str | None = None
    existing_claim: str | None = None
    mirror: Sequence[str] = ()
    mirror_credentials: Mapping[str, Path] | None = field(default=None, repr=False)
    adopt: str | None = None
    replace: str | None = None
    inherited_owners: Sequence[str] = ()

    kind = "node-loopback-registry"

    def __post_init__(self) -> None:
        object.__setattr__(self, "mirror", _mirrors(self.mirror))
        object.__setattr__(
            self,
            "mirror_credentials",
            _mirror_credentials(self.mirror_credentials, _caller_dir()),
        )
        if isinstance(self.inherited_owners, str):
            raise PipelineError(
                "pipeline-invalid", "inherited_owners must be a list of owners"
            )
        object.__setattr__(self, "inherited_owners", tuple(self.inherited_owners))
        if self.adopt is not None and self.replace is not None:
            raise PipelineError(
                "pipeline-invalid", "give adopt= or replace= for the registry, not both"
            )
        existing = self.adopt or self.replace
        if existing is not None:
            if not isinstance(existing, str) or not _LABEL.fullmatch(existing):
                raise PipelineError(
                    "pipeline-invalid", f"invalid registry name {existing!r}"
                )
            if self.name not in ("registry", existing):
                raise PipelineError(
                    "pipeline-invalid",
                    "adopt=/replace= name the registry objects; drop name= or "
                    "set it to the same value",
                )
        if self.host_path is not None and self.existing_claim is not None:
            raise PipelineError(
                "pipeline-invalid", "give host_path or existing_claim, not both"
            )
        if self.host_path is not None and (
            not isinstance(self.host_path, str)
            or not self.host_path.startswith("/")
            or self.host_path == "/"
        ):
            raise PipelineError(
                "pipeline-invalid", "host_path must be an absolute node directory"
            )

    @property
    def registry_name(self) -> str:
        """The registry objects' base name (``adopt``/``replace`` when given)."""
        return self.adopt or self.replace or self.name

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {
            "strategy": self.kind,
            "port": self.port,
            "node": self.node,
            "name": self.registry_name,
            "storage": self.storage,
            "image": self.image,
            "repository": self.repository,
        }
        # Keys added in 0.5.0 appear only when used, so earlier plans keep
        # their hashes.
        for key in ("host_path", "existing_claim", "adopt", "replace"):
            if getattr(self, key) is not None:
                described[key] = getattr(self, key)
        if self.inherited_owners:
            described["inherited_owners"] = list(self.inherited_owners)
        if self.mirror:
            described["mirror"] = list(self.mirror)
        if self.mirror_credentials:
            described["mirror_credentials"] = sorted(self.mirror_credentials)
        return described


@dataclass(frozen=True)
class NodeImport:
    """Import built images straight into one node's containerd (no registry).

    The node reference is a content tag of the config digest
    (``repository:sha256-<12 hex>``), so it can never move. Every layer is
    sent on every import; prefer a registry for large images. Workloads using
    a built image are pinned to the node.

    :param node: Target node alias; default the only declared node.
    :param target: ``ssh://user@host?runtime=…`` or ``docker://name?runtime=…``;
        default ``docker://<node name>?runtime=containerd`` (a kind node).
    """

    node: str | None = None
    target: str | None = None

    kind = "node-import"

    def describe(self) -> dict[str, Any]:
        return {"strategy": self.kind, "node": self.node, "target": self.target}


@dataclass(frozen=True)
class Registry:
    """Push built images to an OCI registry the nodes can pull from.

    :param url: ``oci://host[:port]/prefix``; each image goes to
        ``prefix/<image>`` and is pulled by manifest digest.
    :param node_registry: ``host[:port]`` the nodes pull from, when it differs.
    :param credentials: A private (``0600``) credentials file.
    :param ca_file: A CA bundle for the registry's TLS certificate.
    :param mirror: Third-party images to copy by digest into
        ``prefix/mirror/<registry>/<repository>`` (every platform of a
        multi-arch index); the app's references are rewritten to the copy.
    :param mirror_credentials: ``{registry: credentials file}`` for mirror
        sources that need a login.
    """

    url: str
    node_registry: str | None = None
    credentials: Path | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    mirror: Sequence[str] = ()
    mirror_credentials: Mapping[str, Path] | None = field(default=None, repr=False)

    kind = "registry"

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith("oci://"):
            raise PipelineError(
                "pipeline-invalid", "Registry url must be oci://host[:port]/prefix"
            )
        base = _caller_dir()
        for name in ("credentials", "ca_file"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _resolve(value, base))
        object.__setattr__(self, "mirror", _mirrors(self.mirror))
        object.__setattr__(
            self,
            "mirror_credentials",
            _mirror_credentials(self.mirror_credentials, base),
        )

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {
            "strategy": self.kind,
            "url": self.url,
            "node_registry": self.node_registry,
        }
        if self.mirror:
            described["mirror"] = list(self.mirror)
        if self.mirror_credentials:
            described["mirror_credentials"] = sorted(self.mirror_credentials)
        return described


Delivery = NodeLoopbackRegistry | NodeImport | Registry


# ---------------------------------------------------------------- pipeline


def _checks(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(value)
    return (value,)


class Pipeline:
    """One application deployed from source by ``piceli deploy``.

    The run has six stages, each journaled and skipped when its content
    identity is unchanged: ``inputs`` (identify the sources and staged
    files), ``build`` (containerized builds), ``deliver`` (images to the
    node or registry, handed off by digest), ``plan`` (a release plan against
    live discovery), ``apply`` (the release engine) and ``checks``.

    :param app: The typed :class:`~piceli.app.App`.
    :param target: Where it runs (:meth:`Target.kubeconfig`).
    :param build: One :class:`Build` or several; their images are used as
        ``build["name"]`` in the app.
    :param deliver: :class:`NodeLoopbackRegistry`, :class:`NodeImport` or
        :class:`Registry`; required with a build.
    :param checks: Post-deploy checks (``piceli.checks``), one or several.
    :param rollback_on_failed_checks: Re-apply the previous release when a
        check fails (journaled).
    :param secrets: Secret generators (:class:`~piceli.pipeline.Secrets` or
        a mapping of name → generator spec).
    :param adopt: ``Kind/name`` objects the release may adopt.
    :param replace: ``Kind/name`` unmanaged objects it may delete and recreate
        (a backup is written first).
    :param owner: Release owner; default ``app.owner`` or the app name.
    :param field_manager: Server-side apply field manager; default the owner.
    :param state_dir: Local state (journal, receipts, release catalog),
        relative to the declaring file. With ``state="cluster"`` it is the
        runner's working copy of the shared state.
    :param state: ``"local"`` (default): the state directory is the state;
        ``"cluster"``: the state lives in the release namespace behind a
        release-scoped Lease lock, so any runner can plan and any other can
        apply or resume (see ``docs/state.md``).
    :param state_lease_seconds: With ``state="cluster"``, how long a runner
        that stopped renewing its lock keeps it before another may take it
        over (5-3600, default 60).
    :param execution: ``[execution]`` limits of the release engine
        (``max_seconds``, ``readiness_seconds``, ``poll_seconds`` …).
    :param approval_window_seconds: How long a release plan stays valid.

    Invariants: every image the app uses is a build handle or pinned by
    digest; the release never manages the node-loopback registry.

    Example::

        pipeline = Pipeline(
            app, target, build=images, deliver=NodeLoopbackRegistry(),
            checks=Checks.http(web, "/", expect=200),
            rollback_on_failed_checks=True,
        )
    """

    def __init__(
        self,
        app: App,
        target: Target,
        *,
        build: Build | Sequence[Build] | None = None,
        deliver: Delivery | None = None,
        checks: Any = (),
        rollback_on_failed_checks: bool = False,
        secrets: Secrets | Mapping[str, Any] | None = None,
        adopt: Sequence[str] = (),
        replace: Sequence[str] = (),
        owner: str | None = None,
        field_manager: str | None = None,
        state_dir: str | Path = ".piceli-deploy",
        state: str = "local",
        state_lease_seconds: int = 60,
        execution: Mapping[str, Any] | None = None,
        approval_window_seconds: int = 900,
        inherited_owners: Sequence[str] = (),
    ) -> None:
        from piceli.app import App

        if not isinstance(app, App):
            raise PipelineError("pipeline-invalid", "app must be a piceli App")
        if not isinstance(target, Target):
            raise PipelineError("pipeline-invalid", "target must be a Target")
        builds: tuple[Build, ...]
        if build is None:
            builds = ()
        elif isinstance(build, Build):
            builds = (build,)
        else:
            builds = tuple(build)
        if not all(isinstance(item, Build) for item in builds):
            raise PipelineError("pipeline-invalid", "build must be Build objects")
        if builds and deliver is None:
            raise PipelineError(
                "pipeline-invalid",
                "a pipeline with a build needs deliver= (NodeLoopbackRegistry(), "
                "NodeImport() or Registry(url))",
            )
        if deliver is not None and not isinstance(
            deliver, NodeLoopbackRegistry | NodeImport | Registry
        ):
            raise PipelineError("pipeline-invalid", "unknown delivery strategy")
        if len(app.name) > 42:
            raise PipelineError(
                "pipeline-invalid", "a deployed app name has at most 42 characters"
            )
        self.app = app
        self.target = target
        self.builds = builds
        self.deliver = deliver
        self.checks = _checks(checks)
        self.rollback_on_failed_checks = bool(rollback_on_failed_checks)
        from piceli.pipeline.secrets import Secrets

        if secrets is None:
            self.secrets: dict[str, Any] = {}
        elif isinstance(secrets, Secrets):
            self.secrets = dict(secrets.generators)
        else:
            self.secrets = dict(secrets)
        self.adopt = tuple(adopt)
        self.replace = tuple(replace)
        self.owner = owner or app.owner or app.name
        self.field_manager = field_manager or self.owner
        self.base = _caller_dir()
        self.state_dir = _resolve(state_dir, self.base)
        from piceli.state.backend import StateSettings

        try:
            self.state = StateSettings(state, state_lease_seconds)
        except ValueError as error:
            raise PipelineError("pipeline-invalid", str(error)) from None
        self.execution = dict(execution or {})
        self.approval_window_seconds = approval_window_seconds
        self.inherited_owners = tuple(inherited_owners)

    @property
    def name(self) -> str:
        return self.app.name

    def handles(self) -> Iterator[str]:
        """Image names the app uses through build handles (renders the app)."""
        from piceli.pipeline.compose import used_handles

        yield from used_handles(self)

    def __repr__(self) -> str:
        return (
            f"Pipeline(app={self.app.name!r}, target={self.target!r}, "
            f"builds={len(self.builds)}, deliver={self.deliver!r})"
        )


def pinned(reference: str) -> bool:
    """Whether ``reference`` is ``repository@sha256:<64 hex>``."""
    return bool(_PINNED.fullmatch(reference))
