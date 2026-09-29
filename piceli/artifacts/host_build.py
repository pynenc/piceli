"""Builds without a container VM: host toolchain, shared target dir, OCI layers.

A ``host-build.toml`` (revision ``piceli.host-build.v1``) declares the same
named contexts as a ``build.toml``, the host tools a build may run (``cargo``,
``cargo-zigbuild``, ``zig`` …), the commands, and the images to assemble from
their outputs. Nothing runs in a container, so on macOS no Docker Desktop VM
is needed:

* Contexts are staged, exactly their declared files with content-derived
  mtimes, into ``<cache>/stage/<name>`` (the same path every build, so the
  compiler's incremental state stays valid).
* Commands run on the host with a minimal environment: ``PATH`` holds only
  the directories of the declared tools (each pinned by sha256 in the plan)
  and the system default; ``CARGO_TARGET_DIR`` is ``<cache>/target``, shared
  by every host build that uses the same cache directory.
* Each image is a digest-pinned base pulled over OCI Distribution plus one
  deterministic layer per declared file mapping
  (:mod:`piceli.artifacts.oci_layers`), written as an OCI image-layout tar
  that delivery pushes by digest, skipping blobs the registry has.

The build compiles for one node. Its facts (:class:`~piceli.artifacts.node_facts.NodeFacts`:
platform, page size) are read at plan time; commands, ``env`` and image file
paths may use ``{platform}``, ``{arch}``, ``{rust_arch}``, ``{page_size}``
and ``{page_size_log2}``, and the plan hash covers the substituted values.

* `HostBuildSpec.from_toml` parses and validates; nothing runs.
* `HostBuildSpec.plan` scans the contexts and pins the tools (hashes them). It
  starts no process and contacts nothing.
* `HostBuildSpec.run` needs a `HostBuildGrant` for the exact plan hash.

Importing this module runs nothing.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import re
import shutil
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from piceli.artifacts.build_context import (
    BuildContextError,
    ContextManifest,
    ContextSelection,
)
from piceli.artifacts.build_spec import (
    _ENV_KEY,
    _ENV_VALUE,
    _NAME,
    _REPOSITORY,
    _USER,
    BUILD_RECEIPT_REVISION,
    MAX_SPEC_BYTES,
    BuildReceipt,
    BuildSpecError,
    ContextSpec,
    PinnedImage,
    Runner,
    _abs_path,
    _argv,
    _BuildLog,
    _changed_sources,
    _default_runner,
    _fail,
    _keys,
    _match,
    _now,
    _rel_path,
)
from piceli.artifacts.node_facts import PLACEHOLDERS, NodeFacts
from piceli.artifacts.plan import canonical, digest
from piceli.artifacts.process import ToolPin
from piceli.artifacts.source_identity import (
    InputsLock,
    InputsSpec,
    SourceDriftError,
    SourceIdentityError,
    open_sources,
)

HOST_BUILD_REVISION = "piceli.host-build.v1"
HOST_PREVIEW_REVISION = "piceli.host-build-preview.v1"
DEFAULT_TIMEOUT = 1800.0
_TOOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_PLACEHOLDER = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_DIR_SPEC = re.compile(r"([0-9]{1,10}):([0-9]{1,10}):(0?[0-7]{3,4})")
#: Variables passed from the caller's environment when set: locators only.
_PASSTHROUGH = (
    "HOME",
    "USER",
    "TMPDIR",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
)
#: Set by Piceli; a build's ``env`` cannot override them.
_RESERVED = frozenset({"PATH", "CARGO_TARGET_DIR", "SOURCE_DATE_EPOCH"})


def _template(value: str, what: str, check: Callable[[Any, str], Any]) -> str:
    """Validate ``value`` with placeholders standing in for plain text."""
    rendered = _PLACEHOLDER.sub("x", value)
    if "{" in rendered or "}" in rendered:
        raise _fail(f"{what}: only {{{'}, {'.join(PLACEHOLDERS)}}} may be in braces")
    check(rendered, what)
    return value


def substitute(value: str, facts: NodeFacts) -> str:
    values = facts.placeholders()
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], value)


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/piceli/host-build`` (``~/.cache/…`` without it)."""
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "piceli" / "host-build"


@dataclass(frozen=True)
class OwnedDir:
    path: str
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class HostImage:
    """One image: a pinned base (or scratch) plus one layer per file mapping."""

    name: str
    repository: str
    base: PinnedImage | None = None
    files: tuple[tuple[str, str], ...] = ()
    target_files: tuple[tuple[str, str], ...] = ()
    dirs: tuple[OwnedDir, ...] = ()
    entrypoint: tuple[str, ...] | None = None
    cmd: tuple[str, ...] | None = None
    user: str | None = None
    workdir: str | None = None
    env: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repository": self.repository,
            "base": self.base.to_dict() if self.base else None,
            "files": [list(item) for item in self.files],
            "target_files": [list(item) for item in self.target_files],
            "dirs": [dataclasses.asdict(item) for item in self.dirs],
            "entrypoint": list(self.entrypoint) if self.entrypoint else None,
            "cmd": list(self.cmd) if self.cmd else None,
            "user": self.user,
            "workdir": self.workdir,
            "env": dict(self.env),
        }


@dataclass(frozen=True)
class HostBuildGrant:
    """Authority to run exactly one host build plan on this machine."""

    plan_hash: str
    expires_at: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.plan_hash, str)
            or re.fullmatch(r"sha256:[a-f0-9]{64}", self.plan_hash) is None
            or type(self.expires_at) not in (int, float)
            or not 0 < self.expires_at < 10_000_000_000
        ):
            raise BuildSpecError("invalid-grant", "invalid build grant")


@dataclass(frozen=True)
class HostBuildSpec:
    name: str
    contexts: tuple[ContextSpec, ...]
    images: tuple[HostImage, ...]
    commands: tuple[tuple[str, ...], ...]
    tools: tuple[str, ...]
    platform: str | None = None
    workdir: str = "."
    env: Mapping[str, str] = field(default_factory=dict)
    source_date_epoch: int = 0
    timeout_seconds: float = DEFAULT_TIMEOUT
    inputs: str | None = None
    base: Path = field(default=Path("."), compare=False, repr=False)
    origin: Path | None = field(default=None, compare=False, repr=False)
    facts: NodeFacts | None = field(default=None, compare=False)
    """The target node's facts (`for_node`); required to plan."""
    cache_dir: Path | None = field(default=None, compare=False, repr=False)
    """Shared stage, target and blob directory; default `default_cache_dir`."""

    builder_kind: ClassVar[str] = "host"
    network: ClassVar[str] = "host"

    def __post_init__(self) -> None:
        _match(_NAME, self.name, "build name")
        if self.platform is not None and self.platform not in {
            "linux/amd64",
            "linux/arm64",
        }:
            raise _fail("build.platform must be linux/amd64 or linux/arm64")
        if not self.images:
            raise _fail("declare at least one [[output.image]]")
        if len({item.name for item in self.images}) != len(self.images):
            raise _fail("output image names must be unique")
        names = [item.name for item in self.contexts]
        if not names or len(set(names)) != len(names):
            raise _fail("declare uniquely named contexts")
        into = [item.target or item.name for item in self.contexts]
        if len(set(into)) != len(into):
            raise _fail("contexts must be staged into different directories")
        if not self.commands:
            raise _fail("declare build.commands")
        if not self.tools or len(set(self.tools)) != len(self.tools):
            raise _fail("declare build.tools, each once")
        for command in self.commands:
            if command[0] not in self.tools:
                raise _fail(
                    f"command {command[0]!r} is not a declared tool; add it to "
                    "build.tools"
                )
        if _RESERVED & set(self.env):
            raise _fail(f"build.env cannot set {sorted(_RESERVED & set(self.env))}")
        if (
            not isinstance(self.source_date_epoch, int)
            or isinstance(self.source_date_epoch, bool)
            or not 0 <= self.source_date_epoch < 2**32
        ):
            raise _fail("invalid source_date_epoch")
        if type(self.timeout_seconds) not in (int, float) or not (
            0 < self.timeout_seconds <= 3600
        ):
            raise _fail("timeout_seconds must be in (0, 3600]")

    # --- parsing ---------------------------------------------------------

    @classmethod
    def from_toml(cls, path: Path) -> HostBuildSpec:
        try:
            raw = path.read_bytes()
        except OSError:
            raise BuildSpecError("spec-unreadable", "cannot read build spec") from None
        if len(raw) > MAX_SPEC_BYTES:
            raise _fail("build spec exceeds its byte budget")
        try:
            document = tomllib.loads(raw.decode())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise _fail(f"invalid build spec TOML: {error}") from None
        spec = cls.from_dict(document, path.resolve().parent)
        return dataclasses.replace(spec, origin=path.resolve())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], base: Path) -> HostBuildSpec:
        document = dict(value)
        _keys(
            document,
            {"revision", "name", "build", "context", "output"},
            {"inputs"},
            "host build spec",
        )
        if document["revision"] != HOST_BUILD_REVISION:
            raise _fail(f"revision must be {HOST_BUILD_REVISION!r}")
        build = document["build"]
        _keys(
            build,
            {"tools", "commands"},
            {"platform", "workdir", "env", "source_date_epoch", "timeout_seconds"},
            "build",
        )
        tools = build["tools"]
        if not isinstance(tools, list) or not 0 < len(tools) <= 32:
            raise _fail("build.tools must list 1-32 host tool names")
        for tool in tools:
            _match(_TOOL, tool, "build.tools entry")
        items = build["commands"]
        if not isinstance(items, list) or not 0 < len(items) <= 64:
            raise _fail("build.commands must be a list of argv lists")
        commands = tuple(_argv(item, "build.commands") for item in items)
        for command in commands:
            for arg in command:
                _template(arg, "build.commands", lambda *_: None)
        env = build.get("env", {})
        if not isinstance(env, dict) or len(env) > 128:
            raise _fail("build.env must be a table")
        for key, item in env.items():
            _match(_ENV_KEY, key, "build.env name")
            _template(item, "build.env value", lambda v, w: _match(_ENV_VALUE, v, w))
        workdir = build.get("workdir", ".")
        if workdir != ".":
            _rel_path(workdir, "build.workdir")
        contexts = document["context"]
        if not isinstance(contexts, dict) or not 0 < len(contexts) <= 32:
            raise _fail("declare 1-32 [context.<name>] tables")
        context_specs = [_context(name, item) for name, item in contexts.items()]
        output = document["output"]
        _keys(output, {"image"}, set(), "output")
        if not isinstance(output["image"], list) or not 0 < len(output["image"]) <= 32:
            raise _fail("declare 1-32 [[output.image]] tables")
        inputs = document.get("inputs")
        if inputs is not None and (not isinstance(inputs, str) or "\x00" in inputs):
            raise _fail("inputs must be a path to an inputs.toml")
        return cls(
            name=document["name"] if isinstance(document["name"], str) else "",
            contexts=tuple(context_specs),
            images=tuple(_image(item) for item in output["image"]),
            commands=commands,
            tools=tuple(tools),
            platform=build.get("platform"),
            workdir=workdir,
            env=dict(env),
            source_date_epoch=build.get("source_date_epoch", 0),
            timeout_seconds=build.get("timeout_seconds", DEFAULT_TIMEOUT),
            inputs=inputs,
            base=base,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": HOST_BUILD_REVISION,
            "name": self.name,
            "platform": self.platform,
            "contexts": [item.to_dict() for item in self.contexts],
            "images": [item.to_dict() for item in self.images],
            "commands": [list(item) for item in self.commands],
            "tools": list(self.tools),
            "workdir": self.workdir,
            "env": dict(self.env),
            "source_date_epoch": self.source_date_epoch,
            "timeout_seconds": self.timeout_seconds,
            "inputs": self.inputs,
        }

    @property
    def spec_sha256(self) -> str:
        return digest(canonical(self.to_dict()))

    def for_node(self, facts: NodeFacts) -> HostBuildSpec:
        """This spec built for the node ``facts`` describe.

        :raises BuildSpecError: ``node-platform-mismatch`` when the spec
            declares another platform than the node's.
        """
        if self.platform is not None and self.platform != facts.platform:
            raise BuildSpecError(
                "node-platform-mismatch",
                f"build {self.name!r} declares {self.platform}; the node runs "
                f"{facts.platform}",
            )
        return dataclasses.replace(self, facts=facts)

    def with_cache_dir(self, path: Path) -> HostBuildSpec:
        return dataclasses.replace(self, cache_dir=path)

    @property
    def platforms(self) -> tuple[str, ...]:
        """The one platform (from the node facts, else the declaration)."""
        if self.facts is not None:
            return (self.facts.platform,)
        return (self.platform,) if self.platform else ()

    def base_images(self) -> dict[str, PinnedImage]:
        return {f"output.{item.name}": item.base for item in self.images if item.base}

    def load_inputs(self) -> InputsSpec | None:
        if self.inputs is None:
            return None
        path = Path(self.inputs).expanduser()
        return InputsSpec.from_toml(path if path.is_absolute() else self.base / path)

    def context_root(self, context: ContextSpec, inputs: InputsSpec | None) -> Path:
        if context.source is None:
            root = self.base
        else:
            if inputs is None:
                raise _fail(f"context {context.name!r} names a source; declare inputs")
            matches = [item for item in inputs.sources if item.name == context.source]
            if not matches:
                raise _fail(f"context {context.name!r}: unknown source")
            root = inputs.resolve(matches[0])
        if context.path in (None, "."):
            return root
        return root.joinpath(*PurePosixPath(str(context.path)).parts)

    # --- planning --------------------------------------------------------

    def plan(self, inputs: InputsSpec | None = None) -> HostBuildPlan:
        """Scan contexts and pin tools. Reads files; runs and writes nothing."""
        if self.facts is None:
            raise BuildSpecError(
                "node-facts-unavailable",
                f"host build {self.name!r} needs its target node's facts",
            )
        facts = self.facts
        inputs = inputs if inputs is not None else self.load_inputs()
        manifests: dict[str, ContextManifest] = {}
        roots: dict[str, Path] = {}
        for context in self.contexts:
            root = self.context_root(context, inputs)
            try:
                manifests[context.name] = context.selection.scan(root)
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
            roots[context.name] = root
        tools: dict[str, tuple[Path, ToolPin]] = {}
        for name in self.tools:
            found = shutil.which(name)
            if found is None:
                raise BuildSpecError(
                    "host-tool-missing", f"host tool {name!r} is not on PATH"
                )
            try:
                tools[name] = (Path(found).absolute(), ToolPin.capture(Path(found)))
            except (OSError, ValueError):
                raise BuildSpecError(
                    "host-tool-missing", f"host tool {name!r} is not a regular file"
                ) from None
        commands = tuple(
            tuple(substitute(arg, facts) for arg in command)
            for command in self.commands
        )
        env = {key: substitute(value, facts) for key, value in self.env.items()}
        images = tuple(
            dataclasses.replace(
                image,
                target_files=tuple(
                    (_rel_path(substitute(src, facts), "target_files key"), dest)
                    for src, dest in image.target_files
                ),
            )
            for image in self.images
        )
        return HostBuildPlan(
            self, facts, manifests, roots, tools, commands, env, images
        )

    # --- execution -------------------------------------------------------

    def run(
        self,
        grant: HostBuildGrant,
        output_dir: Path,
        *,
        inputs: InputsSpec | None = None,
        lock: InputsLock | None = None,
        runner: Runner | None = None,
        log: Path | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        raw_output: Callable[[bytes], None] | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> BuildReceipt:
        """Build under ``grant``; write the image archives; return the receipt."""
        from piceli.artifacts.host_execution import HostExecution

        inputs = inputs if inputs is not None else self.load_inputs()
        if lock is not None and inputs is None:
            raise _fail("an inputs lock needs an inputs spec")
        plan = self.plan(inputs)
        if grant.plan_hash != plan.plan_hash:
            raise BuildSpecError("plan-not-approved", "plan hash not approved")
        if grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        started_at = _now()
        start: InputsLock | None = None
        try:
            if inputs is not None:
                start = open_sources(inputs, lock)
        except SourceDriftError:
            raise BuildSpecError("source-drift", "a source changed") from None
        except SourceIdentityError:
            raise BuildSpecError("source-identity", "sources not verifiable") from None
        cache = (self.cache_dir or default_cache_dir()).absolute()
        with _BuildLog(log, progress, raw_output) as build_log:
            build_log.line(f"### host run {self.name} {started_at}")
            execution = HostExecution(
                plan,
                grant,
                cache,
                runner or _default_runner,
                build_log,
                cancel,
                client_factory,
            )
            outputs = execution.execute(output_dir)
        receipt = {
            "revision": BUILD_RECEIPT_REVISION,
            "state": "succeeded",
            "name": self.name,
            "spec_sha256": self.spec_sha256,
            "plan_hash": plan.plan_hash,
            "builder": plan.builder(),
            "node_facts": plan.facts.to_dict(),
            "base_images": {
                key: value.to_dict() for key, value in self.base_images().items()
            },
            "platforms": [plan.platform],
            "inputs_spec_sha256": inputs.spec_sha256 if inputs else None,
            "sources": start.to_dict()["sources"] if start is not None else [],
            "sources_changed_during_build": _changed_sources(inputs, start),
            "contexts": {name: item.summary() for name, item in plan.contexts.items()},
            "network": "host",
            "source_date_epoch": self.source_date_epoch,
            "outputs": outputs,
            "steps": list(execution.steps),
            "started_at": started_at,
            "finished_at": _now(),
        }
        return BuildReceipt(receipt)


def _context(name: Any, item: Any) -> ContextSpec:
    _match(_NAME, name, "context name")
    if name.startswith("piceli"):
        raise _fail("context names starting with 'piceli' are reserved")
    _keys(
        item,
        {"include"},
        {"path", "source", "into", "exclude", "max_files", "max_bytes"},
        f"context.{name}",
    )
    if not isinstance(item["include"], list) or not isinstance(
        item.get("exclude", []), list
    ):
        raise _fail(f"context.{name}: include/exclude must be lists")
    try:
        selection = ContextSelection(
            tuple(item["include"]),
            tuple(item.get("exclude", [])),
            item.get("max_files", 10_000),
            item.get("max_bytes", 64 * 1024 * 1024),
        )
    except BuildContextError as error:
        raise _fail(f"context.{name}: {error}") from None
    path = item.get("path")
    if path is not None and path != ".":
        _rel_path(path, f"context.{name}.path")
    source = item.get("source")
    if source is not None:
        _match(re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"), source, "source")
    into = item.get("into")
    if into is not None:
        _rel_path(into, f"context.{name}.into")
    # ``target`` holds the staging directory (relative) for host builds.
    return ContextSpec(name, selection, path, source, into)


def _image(item: Any) -> HostImage:
    _keys(
        item,
        {"name", "repository"},
        {
            "base",
            "files",
            "target_files",
            "dirs",
            "entrypoint",
            "cmd",
            "user",
            "workdir",
            "env",
        },
        "output.image",
    )
    base = None
    if item.get("base", "scratch") != "scratch":
        base = PinnedImage.from_dict(item["base"], "output.image.base")
    files = item.get("files", {})
    target_files = item.get("target_files", {})
    dirs = item.get("dirs", {})
    env = item.get("env", {})
    for table, what in (
        (files, "files"),
        (target_files, "target_files"),
        (dirs, "dirs"),
        (env, "env"),
    ):
        if not isinstance(table, dict) or len(table) > 256:
            raise _fail(f"output.image.{what} must be a table")
    if not files and not target_files and not dirs:
        raise _fail(f"image {item['name']!r} adds nothing to its base")
    owned = []
    for path, value in dirs.items():
        match = _DIR_SPEC.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise _fail(
                "output.image.dirs values are 'UID:GID:MODE' (10001:10001:0700)"
            )
        owned.append(
            OwnedDir(
                _abs_path(path, "output.image.dirs path").lstrip("/"),
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3), 8),
            )
        )
    for key, value in env.items():
        _match(_ENV_KEY, key, "output.image.env name")
        _match(_ENV_VALUE, value, "output.image.env value")
    destinations = [*files.values(), *target_files.values()]
    for dest in destinations:
        _abs_path(dest, "output.image file destination")
    if len(set(destinations)) != len(destinations):
        raise _fail(f"image {item['name']!r}: two files at one destination")
    return HostImage(
        _match(_NAME, item["name"], "output.image.name"),
        _match(_REPOSITORY, item["repository"], "output.image.repository"),
        base,
        tuple(
            sorted(
                (_rel_path(key, "output.image.files key"), value)
                for key, value in files.items()
            )
        ),
        tuple(
            sorted(
                (_template(key, "output.image.target_files key", _rel_path), value)
                for key, value in target_files.items()
            )
        ),
        tuple(sorted(owned, key=lambda entry: entry.path)),
        _argv(item["entrypoint"], "entrypoint") if "entrypoint" in item else None,
        _argv(item["cmd"], "cmd") if "cmd" in item else None,
        _match(_USER, item["user"], "user") if "user" in item else None,
        _abs_path(item["workdir"], "workdir") if "workdir" in item else None,
        tuple(sorted(env.items())),
    )


@dataclass(frozen=True)
class HostBuildPlan:
    """Everything a host build will do, computed without running anything."""

    spec: HostBuildSpec
    facts: NodeFacts
    contexts: Mapping[str, ContextManifest]
    context_roots: Mapping[str, Path] = field(repr=False)
    tools: Mapping[str, tuple[Path, ToolPin]] = field(repr=False)
    commands: tuple[tuple[str, ...], ...]
    env: Mapping[str, str]
    images: tuple[HostImage, ...]

    @property
    def platform(self) -> str:
        return self.facts.platform

    def builder(self) -> dict[str, Any]:
        """``{"kind": "host", "tools": {name: sha256}}``."""
        return {
            "kind": "host",
            "tools": {name: pin.sha256 for name, (_, pin) in self.tools.items()},
        }

    @property
    def plan_hash(self) -> str:
        return digest(
            canonical(
                {
                    "revision": HOST_PREVIEW_REVISION,
                    "spec_sha256": self.spec.spec_sha256,
                    "node": self.facts.placeholders(),
                    "contexts": {
                        name: item.sha256 for name, item in self.contexts.items()
                    },
                    "tools": self.builder()["tools"],
                    "commands": [list(item) for item in self.commands],
                    "env": dict(self.env),
                    "images": [item.to_dict() for item in self.images],
                }
            )
        )

    def preview(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "revision": HOST_PREVIEW_REVISION,
            "name": spec.name,
            "spec_sha256": spec.spec_sha256,
            "plan_hash": self.plan_hash,
            "builder": self.builder(),
            "node_facts": self.facts.to_dict(),
            "platform": self.platform,
            "base_images": {
                key: value.to_dict() for key, value in spec.base_images().items()
            },
            "contexts": {name: item.summary() for name, item in self.contexts.items()},
            "commands": [list(item) for item in self.commands],
            "env": dict(self.env),
            "images": {
                image.name: {
                    "repository": image.repository,
                    "base": image.base.to_dict() if image.base else None,
                    "layers": len(image.files)
                    + len(image.target_files)
                    + (1 if image.dirs else 0),
                }
                for image in self.images
            },
            "source_date_epoch": spec.source_date_epoch,
            "executes_code": True,
            "uses_container_engine": False,
            "push": False,
        }


def lock_file(path: Path) -> Any:
    """An exclusive ``flock`` on ``path`` (held while the returned file is open)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a+")  # noqa: SIM115 - closed by the caller
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream
