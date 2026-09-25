"""Declarative, containerized builds with pinned builders and receipts.

A ``build.toml`` declares a builder image by digest, target platforms, named
build contexts (staged to exactly their declared files, see
`piceli.artifacts.build_context`), BuildKit cache mounts, a command (or a
Dockerfile) and the outputs: files extracted from the build and/or OCI
images loaded into the local Docker engine.

* `BuildSpec.from_toml` parses and validates; nothing runs.
* `BuildSpec.plan` reads the declared context files and renders the
  Dockerfiles and ``docker buildx`` argv. It writes nothing and starts no
  process, so ``preview`` is safe on untrusted specs.
* `BuildSpec.run` needs a `BuildGrant` for the exact builder digest. It
  records the sources (or verifies them against a lock), stages contexts into
  a private temporary directory, runs a pinned ``docker`` binary with explicit
  argv (no shell, bounded time and output, a minimal environment), streams
  the build log, resolves ``{image_id:N}`` tags, runs each image's smoke
  check, re-hashes every staged file (a change to one fails the build; other
  files of the checkout may change and are only recorded as provenance),
  extracts outputs and returns a `BuildReceipt` (``piceli.build-receipt.v1``).
  Receipts never contain process output, values of the caller's environment
  or absolute paths. They do record each smoke declaration, whose ``env``
  holds plain, non-secret values by contract.

Importing this module runs nothing.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from piceli.artifacts.build_context import (
    BuildContextError,
    ContextManifest,
    ContextSelection,
    stage_context,
    verify_staged,
)
from piceli.artifacts.build_dockerfile import (
    FILES_STAGE,
    IMAGE_STAGE_PREFIX,
    CacheMount,
    DockerfileError,
    FileCopy,
    ImageStage,
    check_user_dockerfile,
    export_stage,
    render_generated,
)
from piceli.artifacts.plan import canonical, digest, public_path, relative
from piceli.artifacts.process import ProcessLimits, ToolPin, _run_process
from piceli.artifacts.source_identity import (
    InputsLock,
    InputsSpec,
    SourceDriftError,
    SourceIdentityError,
    open_sources,
    verify_inputs,
)
from piceli.bounds import object_keys, strict_json
from piceli.tempfiles import temporary_directory

BUILD_SPEC_REVISION = "piceli.build-spec.v1"
BUILD_RECEIPT_REVISION = "piceli.build-receipt.v1"

MAX_SPEC_BYTES = 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_TIMEOUT = 1800.0
PROCESS_OUTPUT_BUDGET = 16 * 1024 * 1024

PLATFORMS = frozenset(
    {
        "linux/amd64",
        "linux/arm64",
        "linux/arm/v7",
        "linux/arm/v6",
        "linux/386",
        "linux/ppc64le",
        "linux/s390x",
        "linux/riscv64",
    }
)
_ARCH_ALIASES = {"aarch64": "arm64", "x86_64": "amd64", "armv7l": "arm/v7"}

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENV_VALUE = re.compile(r"[^\"\\$\x00-\x1f\x7f]{0,4096}")
_ABS_PATH = re.compile(r"/(?:[A-Za-z0-9._+@-]+/?)*")
_REL_PATH = re.compile(r"[A-Za-z0-9._+@-]+(?:/[A-Za-z0-9._+@-]+)*")
_IMAGE = re.compile(
    r"(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
)
_REPOSITORY = re.compile(
    r"(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}")
_TAG_PLACEHOLDER = re.compile(r"\{image_id(?::([0-9]{1,2}))?\}")
PENDING_TAG_PREFIX = "piceli-pending-"
MAX_SMOKE_SECONDS = 600.0
MAX_SMOKE_ENV = 64
MAX_SMOKE_PATTERN = 1024
SMOKE_CAPTURE_BYTES = 256 * 1024
"""Only the first bytes of each smoke output stream are matched (bounded)."""
SMOKE_EXCERPT_CHARS = 400
# Values that name secret material in a store rather than holding a plain
# value. Smoke env is recorded in plans and receipts, so it must stay plain.
_SECRET_REFERENCE = re.compile(
    r"(?i)(?:(?:secrets?|vault|op|sops|awssm|gcpsm|azkv|keyvault|k8s-secret)://"
    r"|secrets?:)"
)
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_USER = re.compile(r"[A-Za-z0-9_.-]{1,64}(?::[A-Za-z0-9_.-]{1,64})?")
_BUILDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ENV_PASSTHROUGH = (
    "HOME",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "BUILDX_CONFIG",
    "XDG_RUNTIME_DIR",
)


class BuildSpecError(ValueError):
    """A rejected spec, grant or build. ``code`` is fixed and path-free."""

    def __init__(
        self, code: str, message: str, *, steps: tuple[dict[str, Any], ...] = ()
    ) -> None:
        super().__init__(message)
        self.code = code
        self.steps = steps


def _fail(message: str) -> BuildSpecError:
    return BuildSpecError("invalid-spec", message)


def _keys(value: Any, required: set[str], optional: set[str], where: str) -> None:
    try:
        object_keys(value, required, frozenset(optional))
    except ValueError:
        allowed = sorted(required | optional)
        raise _fail(f"{where}: expected fields {allowed}") from None


def _match(pattern: re.Pattern[str], value: Any, what: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _fail(f"invalid {what}")
    return value


def _abs_path(value: Any, what: str) -> str:
    path = _match(_ABS_PATH, value, what)
    if any(part in {".", ".."} for part in path.split("/")):
        raise _fail(f"invalid {what}")
    return path


def _rel_path(value: Any, what: str) -> str:
    path = _match(_REL_PATH, value, what)
    try:
        relative(path)
    except ValueError:
        raise _fail(f"invalid {what}") from None
    return path


def _argv(value: Any, what: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 0 < len(value) <= 256
        or not all(
            isinstance(item, str)
            and len(item) <= 4096
            and "\x00" not in item
            and "\n" not in item
            for item in value
        )
    ):
        raise _fail(f"{what} must be a non-empty list of strings")
    return tuple(value)


def _tag(value: Any) -> str:
    """A literal tag, or a template with ``{image_id}``/``{image_id:N}``.

    ``{image_id:N}`` is the first ``N`` (4-64) hex digits of the built image's
    config digest; ``{image_id}`` is all 64. Nothing else may be in braces.
    """
    if not isinstance(value, str) or len(value) > 200:
        raise _fail("invalid output.image.tag")

    def width(match: re.Match[str]) -> str:
        count = int(match.group(1) or 64)
        if not 4 <= count <= 64:
            raise _fail("{image_id:N} needs 4 <= N <= 64")
        return "0" * count

    rendered = _TAG_PLACEHOLDER.sub(width, value)
    if "{" in rendered or "}" in rendered:
        raise _fail("output.image.tag supports only {image_id} and {image_id:N}")
    _match(_TAG, rendered, "output.image.tag")
    return value


def parse_smoke(value: Any) -> SmokeCheck:
    """Validate one ``smoke`` table (TOML or the Python pipeline API)."""
    _keys(
        value,
        {"command"},
        {
            "expect_exit",
            "timeout_seconds",
            "env",
            "entrypoint",
            "expect_stdout",
            "expect_stderr",
        },
        "smoke",
    )
    command = value["command"]
    if not isinstance(command, list) or (command and not _argv(command, "smoke")):
        raise _fail("smoke.command must be a list of strings")
    expect = value.get("expect_exit", 0)
    if not isinstance(expect, int) or isinstance(expect, bool) or not 0 <= expect < 256:
        raise _fail("smoke.expect_exit must be an exit code 0-255")
    seconds = value.get("timeout_seconds", 60)
    if type(seconds) not in (int, float) or not 0 < seconds <= MAX_SMOKE_SECONDS:
        raise _fail(f"smoke.timeout_seconds must be in (0, {MAX_SMOKE_SECONDS:g}]")
    env = value.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_SMOKE_ENV:
        raise _fail(f"smoke.env must be a table of at most {MAX_SMOKE_ENV} strings")
    entrypoint = value.get("entrypoint")
    return SmokeCheck(
        tuple(command),
        expect,
        float(seconds),
        env=tuple(env.items()),
        entrypoint=(
            _argv(entrypoint, "smoke.entrypoint") if entrypoint is not None else None
        ),
        expect_stdout=value.get("expect_stdout"),
        expect_stderr=value.get("expect_stderr"),
    )


def _smoke_pattern(value: Any, what: str) -> None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SMOKE_PATTERN:
        raise _fail(f"{what} must be a regular expression of 1-{MAX_SMOKE_PATTERN}")
    try:
        re.compile(value, re.MULTILINE)
    except re.error:
        raise _fail(f"{what} is not a valid regular expression") from None


def _excerpt(data: bytes) -> str:
    """A one-line, escaped, truncated view of captured output (human text only)."""
    text = data[: SMOKE_EXCERPT_CHARS * 4].decode(errors="replace")
    cut = text[:SMOKE_EXCERPT_CHARS]
    rendered = json.dumps(cut, ensure_ascii=True)
    return rendered + (" …" if len(data) > len(cut.encode()) else "")


def _mapping(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise _fail(f"{what} must be a table of strings")
    for key, item in value.items():
        _match(_ENV_KEY, key, f"{what} name")
        _match(_ENV_VALUE, item, f"{what} value")
    return dict(value)


def platform_slug(platform: str) -> str:
    return platform.replace("/", "-")


def _native_platform(raw: str) -> str | None:
    value = raw.strip()
    if "/" not in value:
        return None
    system, arch = value.split("/", 1)
    candidate = f"{system}/{_ARCH_ALIASES.get(arch, arch)}"
    return candidate if candidate in PLATFORMS else None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PinnedImage:
    """An image reference whose content is fixed by a manifest digest."""

    image: str
    digest: str

    def __post_init__(self) -> None:
        _match(_IMAGE, self.image, "image reference")
        _match(_DIGEST, self.digest, "image digest")

    @property
    def ref(self) -> str:
        return f"{self.image}@{self.digest}"

    def to_dict(self) -> dict[str, str]:
        return {"image": self.image, "digest": self.digest}

    @classmethod
    def from_dict(cls, value: Any, where: str) -> PinnedImage:
        _keys(value, {"image", "digest"}, set(), where)
        return cls(value["image"], value["digest"])


@dataclass(frozen=True)
class ContextSpec:
    """A named build context. ``path`` is relative to the source or spec dir."""

    name: str
    selection: ContextSelection
    path: str | None = None
    source: str | None = None
    target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "source": self.source,
            "target": self.target,
            **self.selection.to_dict(),
        }


@dataclass(frozen=True)
class CacheSpec:
    name: str
    target: str
    id: str | None = None
    sharing: str = "locked"

    def mount(self, spec_name: str, platform: str) -> CacheMount:
        return CacheMount(
            self.id or f"piceli-{spec_name}-{self.name}-{platform_slug(platform)}",
            self.target,
            self.sharing,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "id": self.id,
            "sharing": self.sharing,
        }


@dataclass(frozen=True)
class FileOutput:
    to: str
    source: str
    stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"to": self.to, "from": self.source, "stage": self.stage}


@dataclass(frozen=True)
class SmokeCheck:
    """A command run in the built image after the build; see `_Execution`.

    ``command`` is appended after the image (it replaces ``CMD`` and keeps the
    entrypoint; empty runs the image's default). ``entrypoint`` replaces the
    image's entrypoint (and, as with ``docker run --entrypoint``, its
    ``CMD``). ``env`` holds plain, non-secret values: they are part of the
    plan hash and are recorded in previews and receipts. ``expect_stdout`` and
    ``expect_stderr`` are regular expressions searched (``re.MULTILINE``) in
    the first `SMOKE_CAPTURE_BYTES` of each stream once the exit code matches.
    The container has no network, a read-only root filesystem, no
    capabilities, bounded memory and processes, and a bounded time.
    """

    command: tuple[str, ...]
    expect_exit: int = 0
    timeout_seconds: float = 60.0
    env: tuple[tuple[str, str], ...] = ()
    entrypoint: tuple[str, ...] | None = None
    expect_stdout: str | None = None
    expect_stderr: str | None = None

    def __post_init__(self) -> None:
        keys = [key for key, _ in self.env]
        if len(keys) > MAX_SMOKE_ENV or len(set(keys)) != len(keys):
            raise _fail("smoke.env must have unique names")
        for key, item in self.env:
            _match(_ENV_KEY, key, "smoke.env name")
            if isinstance(item, str) and _SECRET_REFERENCE.match(item):
                raise BuildSpecError(
                    "smoke-env-secret",
                    f"smoke.env {key} looks like a secret reference; smoke env is "
                    "recorded in plans and receipts and must hold plain values",
                )
            _match(_ENV_VALUE, item, "smoke.env value")
        if self.entrypoint is not None:
            _argv(list(self.entrypoint), "smoke.entrypoint")
        if self.expect_stdout is not None:
            _smoke_pattern(self.expect_stdout, "smoke.expect_stdout")
        if self.expect_stderr is not None:
            _smoke_pattern(self.expect_stderr, "smoke.expect_stderr")

    def unmatched(self, stdout: bytes, stderr: bytes) -> list[str]:
        """The streams whose expectation is not found in the bounded capture."""
        missing = []
        for stream, pattern, data in (
            ("stdout", self.expect_stdout, stdout),
            ("stderr", self.expect_stderr, stderr),
        ):
            if pattern is None:
                continue
            text = data[:SMOKE_CAPTURE_BYTES].decode(errors="replace")
            if re.search(pattern, text, re.MULTILINE) is None:
                missing.append(stream)
        return missing

    def to_dict(self) -> dict[str, Any]:
        # New fields appear only when set, so existing specs keep their digest.
        result: dict[str, Any] = {
            "command": list(self.command),
            "expect_exit": self.expect_exit,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.env:
            result["env"] = dict(self.env)
        if self.entrypoint is not None:
            result["entrypoint"] = list(self.entrypoint)
        if self.expect_stdout is not None:
            result["expect_stdout"] = self.expect_stdout
        if self.expect_stderr is not None:
            result["expect_stderr"] = self.expect_stderr
        return result


@dataclass(frozen=True)
class ImageOutput:
    name: str
    repository: str
    tag: str
    base: PinnedImage | None = None
    files: tuple[tuple[str, str], ...] = ()
    entrypoint: tuple[str, ...] | None = None
    cmd: tuple[str, ...] | None = None
    user: str | None = None
    workdir: str | None = None
    target: str | None = None
    smoke: SmokeCheck | None = None

    @property
    def templated(self) -> bool:
        """Whether the tag depends on the built image (``{image_id:N}``)."""
        return _TAG_PLACEHOLDER.search(self.tag) is not None

    def ref(
        self,
        platform: str,
        multi: bool,
        *,
        image_id: str | None = None,
        pending: str | None = None,
    ) -> str:
        """The image reference.

        A templated tag resolves with ``image_id``; before the build is known
        it is loaded under a private ``piceli-pending-<nonce>`` tag
        (``pending``). Without either, the template text is returned (preview).
        """
        tag = self.tag
        if self.templated and image_id is not None:
            hexdigits = image_id.removeprefix("sha256:")
            tag = _TAG_PLACEHOLDER.sub(
                lambda match: hexdigits[: int(match.group(1) or 64)], tag
            )
        elif self.templated and pending is not None:
            tag = f"{PENDING_TAG_PREFIX}{pending}"
        tag = f"{tag}-{platform_slug(platform)[6:]}" if multi else tag
        return f"{self.repository}:{tag}"

    def to_dict(self) -> dict[str, Any]:
        smoke = {"smoke": self.smoke.to_dict()} if self.smoke else {}
        return {
            **smoke,
            "name": self.name,
            "repository": self.repository,
            "tag": self.tag,
            "base": self.base.to_dict() if self.base else None,
            "files": [list(item) for item in self.files],
            "entrypoint": list(self.entrypoint) if self.entrypoint else None,
            "cmd": list(self.cmd) if self.cmd else None,
            "user": self.user,
            "workdir": self.workdir,
            "target": self.target,
        }


@dataclass(frozen=True)
class BuildGrant:
    """Authority to run one exact builder image, optionally one exact plan."""

    builder_digest: str
    expires_at: float
    allow_network: bool = False
    plan_hash: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.builder_digest, str)
            or _DIGEST.fullmatch(self.builder_digest) is None
            or (
                self.plan_hash is not None
                and (
                    not isinstance(self.plan_hash, str)
                    or _DIGEST.fullmatch(self.plan_hash) is None
                )
            )
            or type(self.expires_at) not in (int, float)
            or not 0 < self.expires_at < 10_000_000_000
            or not isinstance(self.allow_network, bool)
        ):
            raise BuildSpecError("invalid-grant", "invalid build grant")


OutputSink = Callable[[str, bytes], None]


class Runner(Protocol):
    """Runs one docker argv; returns ``(process receipt, stdout, stderr)``.

    ``on_output(stream, block)`` should receive output while it is produced
    (the build log streams from it); it is ``None`` for short probes.
    """

    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        limits: ProcessLimits,
        environment: dict[str, str],
        expires_at: float | None,
        /,
        *,
        on_output: OutputSink | None = None,
    ) -> tuple[dict[str, Any], bytes, bytes]: ...


def _default_runner(
    argv: list[str],
    cwd: Path,
    limits: ProcessLimits,
    environment: dict[str, str],
    expires_at: float | None,
    /,
    *,
    on_output: OutputSink | None = None,
) -> tuple[dict[str, Any], bytes, bytes]:
    return _run_process(
        argv,
        cwd,
        limits,
        expires_at=expires_at,
        environment=environment,
        on_output=on_output,
    )


@dataclass(frozen=True)
class DockerTool:
    """A pinned ``docker`` CLI plus the minimal environment it runs with.

    Only locator variables (``HOME``, ``DOCKER_CONFIG``, ``DOCKER_CONTEXT``,
    ``DOCKER_HOST`` ...) pass through; ``PATH`` is the docker binary's own
    directory (for credential helpers shipped beside it) plus the system
    default. Receipts record the tool digest, never the environment.
    """

    tool: ToolPin
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def discover(
        cls, path: Path | None = None, sha256: str | None = None
    ) -> DockerTool:
        if path is None:
            found = shutil.which("docker")
            if found is None:
                raise BuildSpecError("docker-unavailable", "docker CLI not found")
            path = Path(found)
        tool = ToolPin(path, sha256) if sha256 is not None else ToolPin.capture(path)
        environment = {
            key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ
        }
        environment["PATH"] = f"{tool.path.parent}{os.pathsep}{os.defpath}"
        environment["BUILDX_NO_DEFAULT_ATTESTATIONS"] = "1"
        return cls(tool, environment)


@dataclass(frozen=True)
class BuildPlan:
    """Everything a run will do, computed without writing or executing."""

    spec: BuildSpec
    contexts: Mapping[str, ContextManifest]
    context_roots: Mapping[str, Path] = field(repr=False)
    dockerfiles: Mapping[str, str]
    invocations: tuple[dict[str, Any], ...]

    @property
    def plan_hash(self) -> str:
        return digest(
            canonical(
                {
                    "spec_sha256": self.spec.spec_sha256,
                    "contexts": {
                        name: item.sha256 for name, item in self.contexts.items()
                    },
                    "dockerfiles": {
                        platform: digest(text.encode())
                        for platform, text in self.dockerfiles.items()
                    },
                    "invocations": list(self.invocations),
                }
            )
        )

    def preview(self) -> dict[str, Any]:
        spec = self.spec
        return {
            "revision": "piceli.build-preview.v1",
            "name": spec.name,
            "spec_sha256": spec.spec_sha256,
            "plan_hash": self.plan_hash,
            "builder": spec.builder.to_dict(),
            "base_images": {
                key: value.to_dict() for key, value in spec.base_images().items()
            },
            "platforms": list(spec.platforms),
            "mode": "dockerfile" if spec.dockerfile else "generated",
            "network": spec.network,
            "requires_network_grant": spec.network != "none",
            "source_date_epoch": spec.source_date_epoch,
            "contexts": {name: item.summary() for name, item in self.contexts.items()},
            "caches": {
                item.name: {"target": item.target, "sharing": item.sharing}
                for item in spec.caches
            },
            "dockerfiles_sha256": {
                platform: digest(text.encode())
                for platform, text in self.dockerfiles.items()
            },
            "dockerfiles": dict(self.dockerfiles),
            "outputs": {
                "files": [item.to for item in spec.files],
                "images": {
                    item.name: [
                        item.ref(platform, len(spec.platforms) > 1)
                        for platform in spec.platforms
                    ]
                    for item in spec.images
                },
                **(
                    {
                        "smoke": {
                            item.name: item.smoke.to_dict()
                            for item in spec.images
                            if item.smoke
                        }
                    }
                    if any(item.smoke for item in spec.images)
                    else {}
                ),
            },
            "invocations": list(self.invocations),
            "executes_code": True,
            "push": False,
        }


@dataclass(frozen=True)
class BuildReceipt:
    """The ``piceli.build-receipt.v1`` document for a successful build."""

    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data))

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True) + "\n"

    @property
    def files(self) -> dict[str, str]:
        return dict(self.data["outputs"]["files"])

    @property
    def images(self) -> dict[str, dict[str, Any]]:
        return dict(self.data["outputs"]["images"])

    @classmethod
    def from_json(cls, raw: str) -> BuildReceipt:
        value = strict_json(raw, 16 * 1024 * 1024)
        if (
            not isinstance(value, dict)
            or value.get("revision") != BUILD_RECEIPT_REVISION
        ):
            raise BuildSpecError("invalid-receipt", "not a build receipt")
        for key in (
            "spec_sha256",
            "builder",
            "platforms",
            "sources",
            "outputs",
            "started_at",
            "finished_at",
        ):
            if key not in value:
                raise BuildSpecError("invalid-receipt", "incomplete build receipt")
        return cls(value)


@dataclass(frozen=True)
class BuildSpec:
    name: str
    builder: PinnedImage
    platforms: tuple[str, ...]
    contexts: tuple[ContextSpec, ...]
    caches: tuple[CacheSpec, ...] = ()
    files: tuple[FileOutput, ...] = ()
    images: tuple[ImageOutput, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    workdir: str = "/work"
    env: Mapping[str, str] = field(default_factory=dict)
    dockerfile: str | None = None
    dockerfile_context: str | None = None
    builder_arg: str | None = None
    args: Mapping[str, str] = field(default_factory=dict)
    pinned_args: Mapping[str, PinnedImage] = field(default_factory=dict)
    network: str = "none"
    source_date_epoch: int = 0
    timeout_seconds: float = DEFAULT_TIMEOUT
    buildx_builder: str | None = None
    inputs: str | None = None
    base: Path = field(default=Path("."), compare=False, repr=False)
    origin: Path | None = field(default=None, compare=False, repr=False)
    """The ``build.toml`` this spec was read from; re-checked after a build."""
    platform_override: str | None = field(default=None, compare=False, repr=False)
    """A platform that replaced the file's ``platforms`` (``Build.spec(platform=)``)."""

    def __post_init__(self) -> None:
        _match(_NAME, self.name, "build name")
        if not self.platforms or len(set(self.platforms)) != len(self.platforms):
            raise _fail("platforms must be a non-empty list without duplicates")
        for platform in self.platforms:
            if platform not in PLATFORMS:
                raise _fail(f"unsupported platform {platform!r}")
        if not self.files and not self.images:
            raise _fail("declare at least one output file or image")
        names = [item.name for item in self.contexts]
        if not names or len(set(names)) != len(names):
            raise _fail("declare uniquely named contexts")
        if len({item.name for item in self.caches}) != len(self.caches):
            raise _fail("cache names must be unique")
        if len({item.to for item in self.files}) != len(self.files):
            raise _fail("output file destinations must be unique")
        if len({item.name for item in self.images}) != len(self.images):
            raise _fail("output image names must be unique")
        if self.network not in {"none", "default"}:
            raise _fail("network must be 'none' or 'default'")
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
        if self.buildx_builder is not None:
            _match(_BUILDER_NAME, self.buildx_builder, "buildx builder name")
        if self.dockerfile is None:
            self._check_generated()
        else:
            self._check_dockerfile_mode()

    def _check_generated(self) -> None:
        if not self.commands:
            raise _fail("declare build.command/commands or build.dockerfile")
        if self.args or self.pinned_args or self.builder_arg:
            raise _fail("args, images and builder_arg need build.dockerfile")
        declared = {item.to for item in self.files}
        for output in self.files:
            if output.stage is not None:
                raise _fail("output.file stage needs build.dockerfile")
        for image in self.images:
            if image.target is not None:
                raise _fail("output.image target needs build.dockerfile")
            if image.base is None and not image.files:
                raise _fail(f"image {image.name!r}: a scratch image needs files")
            for source, _ in image.files:
                if source not in declared:
                    raise _fail(f"image {image.name!r}: {source!r} is not an output")

    def _check_dockerfile_mode(self) -> None:
        if self.commands or self.caches or self.env:
            raise _fail("command, env and caches belong in the Dockerfile")
        if self.dockerfile_context not in {item.name for item in self.contexts}:
            raise _fail("build.context must name a declared context")
        if self.builder_arg is None:
            raise _fail("build.builder_arg is required with build.dockerfile")
        reserved = {"SOURCE_DATE_EPOCH", self.builder_arg, *self.pinned_args}
        if reserved & set(self.args) or self.builder_arg in self.pinned_args:
            raise _fail("build args cannot override pinned or reserved args")
        for output in self.files:
            if output.stage is None or not output.source.startswith("/"):
                raise _fail("output.file needs stage and an absolute from")
        for image in self.images:
            if image.target is None or image.base or image.files:
                raise _fail("output.image needs only target with build.dockerfile")

    # --- parsing ---------------------------------------------------------

    @classmethod
    def from_toml(cls, path: Path) -> BuildSpec:
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
    def from_dict(cls, value: Mapping[str, Any], base: Path) -> BuildSpec:
        document = dict(value)
        _keys(
            document,
            {"revision", "name", "builder", "build", "context", "output"},
            {"inputs", "cache", "images"},
            "build spec",
        )
        if document["revision"] != BUILD_SPEC_REVISION:
            raise _fail(f"revision must be {BUILD_SPEC_REVISION!r}")
        builder = PinnedImage.from_dict(document["builder"], "builder")
        build = document["build"]
        _keys(
            build,
            {"platforms"},
            {
                "command",
                "commands",
                "workdir",
                "env",
                "dockerfile",
                "context",
                "builder_arg",
                "args",
                "network",
                "source_date_epoch",
                "timeout_seconds",
                "buildx_builder",
            },
            "build",
        )
        if not isinstance(build["platforms"], list):
            raise _fail("build.platforms must be a list")
        if "command" in build and "commands" in build:
            raise _fail("use build.command or build.commands, not both")
        commands: tuple[tuple[str, ...], ...] = ()
        if "command" in build:
            commands = (_argv(build["command"], "build.command"),)
        elif "commands" in build:
            items = build["commands"]
            if not isinstance(items, list) or not 0 < len(items) <= 64:
                raise _fail("build.commands must be a list of argv lists")
            commands = tuple(_argv(item, "build.commands") for item in items)
        contexts = document["context"]
        if not isinstance(contexts, dict) or not 0 < len(contexts) <= 32:
            raise _fail("declare 1-32 [context.<name>] tables")
        context_specs = []
        for name, item in contexts.items():
            _match(_NAME, name, "context name")
            if name.startswith("piceli"):
                raise _fail("context names starting with 'piceli' are reserved")
            _keys(
                item,
                {"include"},
                {"path", "source", "target", "exclude", "max_files", "max_bytes"},
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
                _match(
                    re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"), source, "source"
                )
            target = item.get("target")
            if target is not None:
                _abs_path(target, f"context.{name}.target")
            context_specs.append(ContextSpec(name, selection, path, source, target))
        caches = document.get("cache", {})
        if not isinstance(caches, dict) or len(caches) > 32:
            raise _fail("declare at most 32 [cache.<name>] tables")
        cache_specs = []
        for name, item in caches.items():
            _match(_NAME, name, "cache name")
            _keys(item, {"target"}, {"id", "sharing"}, f"cache.{name}")
            if item.get("sharing", "locked") not in {"locked", "shared", "private"}:
                raise _fail("cache sharing must be locked, shared or private")
            cache_id = item.get("id")
            if cache_id is not None:
                _match(re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"), cache_id, "id")
            cache_specs.append(
                CacheSpec(
                    name,
                    _abs_path(item["target"], f"cache.{name}.target"),
                    cache_id,
                    item.get("sharing", "locked"),
                )
            )
        output = document["output"]
        _keys(output, set(), {"file", "image"}, "output")
        files = []
        for item in output.get("file", []):
            _keys(item, {"from", "to"}, {"stage"}, "output.file")
            to = _rel_path(item["to"], "output.file.to")
            try:
                public_path(to)
            except ValueError:
                raise _fail("output.file.to cannot be a private path") from None
            source = item["from"]
            source = (
                _abs_path(source, "output.file.from")
                if isinstance(source, str) and source.startswith("/")
                else _rel_path(source, "output.file.from")
            )
            stage = item.get("stage")
            if stage is not None:
                _match(_NAME, stage, "output.file.stage")
            files.append(FileOutput(to, source, stage))
        images = []
        for item in output.get("image", []):
            _keys(
                item,
                {"name", "repository", "tag"},
                {
                    "base",
                    "files",
                    "entrypoint",
                    "cmd",
                    "user",
                    "workdir",
                    "target",
                    "smoke",
                },
                "output.image",
            )
            files_map = item.get("files", {})
            if not isinstance(files_map, dict):
                raise _fail("output.image.files must map outputs to image paths")
            base_image = None
            if "base" in item and item["base"] != "scratch":
                base_image = PinnedImage.from_dict(item["base"], "output.image.base")
            images.append(
                ImageOutput(
                    _match(_NAME, item["name"], "output.image.name"),
                    _match(_REPOSITORY, item["repository"], "output.image.repository"),
                    _tag(item["tag"]),
                    base_image,
                    tuple(
                        (
                            _rel_path(key, "output.image.files key"),
                            _abs_path(target, "output.image.files path"),
                        )
                        for key, target in files_map.items()
                    ),
                    _argv(item["entrypoint"], "entrypoint")
                    if "entrypoint" in item
                    else None,
                    _argv(item["cmd"], "cmd") if "cmd" in item else None,
                    _match(_USER, item["user"], "user") if "user" in item else None,
                    _abs_path(item["workdir"], "workdir")
                    if "workdir" in item
                    else None,
                    _match(_NAME, item["target"], "target")
                    if "target" in item
                    else None,
                    parse_smoke(item["smoke"]) if "smoke" in item else None,
                )
            )
        pinned = document.get("images", {})
        if not isinstance(pinned, dict) or len(pinned) > 32:
            raise _fail("[images] must map build args to pinned images")
        pinned_args = {
            _match(_ENV_KEY, key, "image arg"): PinnedImage.from_dict(
                item, f"images.{key}"
            )
            for key, item in pinned.items()
        }
        inputs = document.get("inputs")
        if inputs is not None:
            text_path = inputs if isinstance(inputs, str) else ""
            if not text_path or "\x00" in text_path:
                raise _fail("inputs must be a path to an inputs.toml")
        builder_arg = build.get("builder_arg")
        if builder_arg is not None:
            _match(_ENV_KEY, builder_arg, "build.builder_arg")
        dockerfile = build.get("dockerfile")
        if dockerfile is not None:
            _rel_path(dockerfile, "build.dockerfile")
        return cls(
            name=document["name"] if isinstance(document["name"], str) else "",
            builder=builder,
            platforms=tuple(build["platforms"]),
            contexts=tuple(context_specs),
            caches=tuple(cache_specs),
            files=tuple(files),
            images=tuple(images),
            commands=commands,
            workdir=_abs_path(build.get("workdir", "/work"), "build.workdir"),
            env=_mapping(build.get("env", {}), "build.env"),
            dockerfile=dockerfile,
            dockerfile_context=build.get("context"),
            builder_arg=builder_arg,
            args=_mapping(build.get("args", {}), "build.args"),
            pinned_args=pinned_args,
            network=build.get("network", "none"),
            source_date_epoch=build.get("source_date_epoch", 0),
            timeout_seconds=build.get("timeout_seconds", DEFAULT_TIMEOUT),
            buildx_builder=build.get("buildx_builder"),
            inputs=inputs,
            base=base,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": BUILD_SPEC_REVISION,
            "name": self.name,
            "builder": self.builder.to_dict(),
            "platforms": list(self.platforms),
            "contexts": [item.to_dict() for item in self.contexts],
            "caches": [item.to_dict() for item in self.caches],
            "files": [item.to_dict() for item in self.files],
            "images": [item.to_dict() for item in self.images],
            "commands": [list(item) for item in self.commands],
            "workdir": self.workdir,
            "env": dict(self.env),
            "dockerfile": self.dockerfile,
            "dockerfile_context": self.dockerfile_context,
            "builder_arg": self.builder_arg,
            "args": dict(self.args),
            "pinned_args": {
                key: value.to_dict() for key, value in self.pinned_args.items()
            },
            "network": self.network,
            "source_date_epoch": self.source_date_epoch,
            "timeout_seconds": self.timeout_seconds,
            "buildx_builder": self.buildx_builder,
            "inputs": self.inputs,
        }

    def with_platform(self, platform: str) -> BuildSpec:
        """This spec built for one ``platform`` instead of the file's ``platforms``.

        The override is part of the spec digest (so of every plan hash) and is
        applied again when the file is re-checked after a build.
        """
        return dataclasses.replace(
            self, platforms=(platform,), platform_override=platform
        )

    @property
    def spec_sha256(self) -> str:
        """Digest of the normalised declaration (not of the file's bytes)."""
        return digest(canonical(self.to_dict()))

    def base_images(self) -> dict[str, PinnedImage]:
        result = dict(self.pinned_args)
        for image in self.images:
            if image.base is not None:
                result[f"output.{image.name}"] = image.base
        return result

    def load_inputs(self) -> InputsSpec | None:
        if self.inputs is None:
            return None
        path = Path(self.inputs).expanduser()
        return InputsSpec.from_toml(path if path.is_absolute() else self.base / path)

    # --- planning --------------------------------------------------------

    def context_root(self, context: ContextSpec, inputs: InputsSpec | None) -> Path:
        if context.source is None:
            base = self.base
        else:
            if inputs is None:
                raise _fail(f"context {context.name!r} names a source; declare inputs")
            matches = [item for item in inputs.sources if item.name == context.source]
            if not matches:
                raise _fail(f"context {context.name!r}: unknown source")
            base = inputs.resolve(matches[0])
        if context.path in (None, "."):
            return base
        return base.joinpath(*PurePosixPath(str(context.path)).parts)

    def _dockerfile_text(
        self, manifests: Mapping[str, ContextManifest], roots: Mapping[str, Path]
    ) -> str:
        assert self.dockerfile is not None and self.dockerfile_context is not None
        manifest = manifests[self.dockerfile_context]
        entry = [item for item in manifest.files if item.path == self.dockerfile]
        if not entry or entry[0].size > 1024 * 1024:
            raise _fail("build.dockerfile must be a small file in its context")
        raw = (roots[self.dockerfile_context] / self.dockerfile).read_bytes()
        if digest(raw) != entry[0].sha256:
            raise BuildSpecError("context-changed", "Dockerfile changed")
        return raw.decode()

    def render(
        self,
        platform: str,
        manifests: Mapping[str, ContextManifest],
        roots: Mapping[str, Path],
    ) -> str:
        if self.dockerfile is None:
            return render_generated(
                builder_ref=self.builder.ref,
                workdir=self.workdir,
                env=self.env,
                contexts=[
                    (item.name, item.target or self.workdir) for item in self.contexts
                ],
                caches=[item.mount(self.name, platform) for item in self.caches],
                commands=self.commands,
                files=tuple(
                    FileCopy(
                        item.to,
                        item.source
                        if item.source.startswith("/")
                        else f"{self.workdir.rstrip('/')}/{item.source}",
                    )
                    for item in self.files
                ),
                images=tuple(
                    ImageStage(
                        item.name,
                        item.base.ref if item.base else "scratch",
                        item.files,
                        item.entrypoint,
                        item.cmd,
                        item.user,
                        item.workdir,
                    )
                    for item in self.images
                ),
                network=self.network,
                epoch=self.source_date_epoch,
            )
        text = self._dockerfile_text(manifests, roots)
        assert self.builder_arg is not None
        try:
            stages = check_user_dockerfile(
                text,
                pinned_args=frozenset({self.builder_arg, *self.pinned_args}),
                contexts=frozenset(item.name for item in self.contexts),
                required_arg=self.builder_arg,
            )
        except DockerfileError as error:
            raise BuildSpecError(error.code, str(error)) from None
        for output in self.files:
            if str(output.stage) not in stages:
                raise _fail(f"output.file stage {output.stage!r} is not in the file")
        for image in self.images:
            if str(image.target) not in stages:
                raise _fail(f"output.image target {image.target!r} is not a stage")
        if any(stage.startswith("piceli-") for stage in stages):
            raise _fail("stage names starting with 'piceli-' are reserved")
        if not self.files:
            return text
        extra = export_stage(
            tuple(
                FileCopy(item.to, item.source, str(item.stage)) for item in self.files
            )
        )
        return text.rstrip("\n") + "\n\n" + "\n".join(extra) + "\n"

    def invocation(
        self,
        platform: str,
        kind: str,
        *,
        docker: str = "docker",
        builder: str = "<buildx-builder>",
        staging: str = "<staging>",
        pending: str = "<nonce>",
        image: ImageOutput | None = None,
    ) -> list[str]:
        slug = platform_slug(platform)
        argv = [
            docker,
            "buildx",
            "build",
            "--builder",
            builder,
            "--platform",
            platform,
            "--progress=plain",
            "--provenance=false",
            "--sbom=false",
            f"--network={self.network}",
            "--build-arg",
            f"SOURCE_DATE_EPOCH={self.source_date_epoch}",
        ]
        if self.dockerfile is not None:
            assert self.builder_arg is not None
            argv += ["--build-arg", f"{self.builder_arg}={self.builder.ref}"]
            for key, value in sorted(self.pinned_args.items()):
                argv += ["--build-arg", f"{key}={value.ref}"]
            for key, value in sorted(self.args.items()):
                argv += ["--build-arg", f"{key}={value}"]
        argv += ["--file", f"{staging}/dockerfiles/{slug}.Dockerfile"]
        for context in self.contexts:
            if context.name != self.dockerfile_context:
                argv += [
                    "--build-context",
                    f"{context.name}={staging}/contexts/{context.name}",
                ]
        if kind == "files":
            argv += [
                "--target",
                FILES_STAGE,
                "--metadata-file",
                f"{staging}/metadata/{slug}-files.json",
                "--output",
                f"type=local,dest={staging}/out/{slug}",
            ]
        else:
            assert image is not None
            target = image.target or f"{IMAGE_STAGE_PREFIX}{image.name}"
            argv += [
                "--target",
                target,
                "--metadata-file",
                f"{staging}/metadata/{slug}-image-{image.name}.json",
                "--load",
                "--tag",
                image.ref(platform, len(self.platforms) > 1, pending=pending),
            ]
        main = (
            f"{staging}/contexts/{self.dockerfile_context}"
            if self.dockerfile_context
            else f"{staging}/empty"
        )
        return [*argv, main]

    def _invocations(
        self, **kwargs: Any
    ) -> list[tuple[str, str, ImageOutput | None, list[str]]]:
        result: list[tuple[str, str, ImageOutput | None, list[str]]] = []
        for platform in self.platforms:
            if self.files:
                result.append(
                    (
                        platform,
                        "files",
                        None,
                        self.invocation(platform, "files", **kwargs),
                    )
                )
            for image in self.images:
                result.append(
                    (
                        platform,
                        f"image:{image.name}",
                        image,
                        self.invocation(platform, "image", image=image, **kwargs),
                    )
                )
        return result

    def plan(self, inputs: InputsSpec | None = None) -> BuildPlan:
        """Scan contexts and render the build. Reads files; writes nothing."""
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
        dockerfiles = {
            platform: self.render(platform, manifests, roots)
            for platform in self.platforms
        }
        invocations = tuple(
            {"platform": platform, "kind": kind, "argv": argv}
            for platform, kind, _, argv in self._invocations()
        )
        return BuildPlan(self, manifests, roots, dockerfiles, invocations)

    # --- execution -------------------------------------------------------

    def run(
        self,
        grant: BuildGrant,
        output_dir: Path,
        *,
        inputs: InputsSpec | None = None,
        lock: InputsLock | None = None,
        docker: DockerTool | None = None,
        runner: Runner | None = None,
        log: Path | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        raw_output: Callable[[bytes], None] | None = None,
    ) -> BuildReceipt:
        """Build under ``grant`` and return the receipt (outputs written).

        ``log`` is appended to while the build runs (complete lines, flushed).
        ``progress`` receives one short line per step; ``raw_output`` receives
        the build output as it arrives. Neither ever reaches the receipt.
        """
        inputs = inputs if inputs is not None else self.load_inputs()
        if lock is not None and inputs is None:
            raise _fail("an inputs lock needs an inputs spec")
        plan = self.plan(inputs)
        if grant.builder_digest != self.builder.digest:
            raise BuildSpecError("builder-not-approved", "builder digest not approved")
        if grant.plan_hash is not None and grant.plan_hash != plan.plan_hash:
            raise BuildSpecError("plan-not-approved", "plan hash not approved")
        if self.network != "none" and not grant.allow_network:
            raise BuildSpecError("network-not-granted", "the build needs network")
        if grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        docker = docker if docker is not None else DockerTool.discover()
        started_at = _now()
        start: InputsLock | None = None
        try:
            if inputs is not None:
                start = open_sources(inputs, lock)
        except SourceDriftError:
            raise BuildSpecError("source-drift", "a source changed") from None
        except SourceIdentityError:
            raise BuildSpecError("source-identity", "sources not verifiable") from None
        with _BuildLog(log, progress, raw_output) as build_log:
            build_log.line(f"### run {self.name} {started_at}")
            execution = _Execution(
                plan, grant, docker, runner or _default_runner, build_log, cancel
            )
            outputs = execution.execute(output_dir)
        receipt = {
            "revision": BUILD_RECEIPT_REVISION,
            "state": "succeeded",
            "name": self.name,
            "spec_sha256": self.spec_sha256,
            "plan_hash": plan.plan_hash,
            "builder": {
                **self.builder.to_dict(),
                "platform_manifests": execution.builder_manifests,
            },
            "base_images": {
                key: value.to_dict() for key, value in self.base_images().items()
            },
            "platforms": list(self.platforms),
            "native_platform": execution.native,
            "emulated_platforms": [
                item for item in self.platforms if item != execution.native
            ],
            "inputs_spec_sha256": inputs.spec_sha256 if inputs else None,
            "sources": start.to_dict()["sources"] if start is not None else [],
            "sources_changed_during_build": _changed_sources(inputs, start),
            "contexts": {name: item.summary() for name, item in plan.contexts.items()},
            "dockerfiles_sha256": {
                platform: digest(text.encode())
                for platform, text in plan.dockerfiles.items()
            },
            "network": self.network,
            "source_date_epoch": self.source_date_epoch,
            "tool": {
                "docker_sha256": docker.tool.sha256,
                "buildx_version": execution.buildx_version,
                "buildx_builder": execution.builder_name,
            },
            "outputs": outputs,
            **(
                {
                    "smoke": {
                        item.name: item.smoke.to_dict()
                        for item in self.images
                        if item.smoke
                    }
                }
                if any(item.smoke for item in self.images)
                else {}
            ),
            "steps": list(execution.steps),
            "started_at": started_at,
            "finished_at": _now(),
        }
        return BuildReceipt(receipt)

    def smoke_argv(
        self,
        image: ImageOutput,
        platform: str,
        image_id: str,
        *,
        docker: str = "docker",
        name: str = "piceli-smoke-<nonce>",
    ) -> list[str]:
        """``docker run`` for an image's smoke check: isolated and bounded.

        Declared options use the single-argument ``--flag=value`` form, so a
        value can never be read as another option; ``--env`` always carries
        ``NAME=value`` (never a bare name that would copy the caller's
        environment).
        """
        smoke = image.smoke
        assert smoke is not None
        declared = [f"--env={key}={value}" for key, value in smoke.env]
        entry: tuple[str, ...] = ()
        if smoke.entrypoint is not None:
            declared.append(f"--entrypoint={smoke.entrypoint[0]}")
            entry = smoke.entrypoint[1:]
        return [
            docker,
            "run",
            "--rm",
            "--name",
            name,
            "--pull",
            "never",
            "--platform",
            platform,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=67108864",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "512m",
            *declared,
            image_id,
            *entry,
            *smoke.command,
        ]


def _changed_sources(
    inputs: InputsSpec | None, start: InputsLock | None
) -> list[str] | None:
    """Provenance only: sources whose whole-checkout identity moved meanwhile.

    The build is judged by its staged files (`verify_staged`); a busy checkout
    whose unrelated files changed is recorded here, never rejected. ``None``
    means the sources could not be recaptured.
    """
    if inputs is None or start is None:
        return []
    try:
        verification = verify_inputs(inputs, start)
    except SourceIdentityError:
        return None
    return sorted({item.name for item in verification.drifts})


class _BuildLog:
    """Streams build output to ``--log`` (append, 0600) and progress sinks.

    Output is written in complete lines and flushed as it arrives, so the file
    can be followed during a long build. Partial lines are written when their
    step ends.
    """

    def __init__(
        self,
        path: Path | None,
        progress: Callable[[str], None] | None,
        raw: Callable[[bytes], None] | None,
    ) -> None:
        self.path = path
        self.progress = progress
        self.raw = raw
        self.stream: Any = None
        self.pending: dict[str, bytes] = {}

    def __enter__(self) -> _BuildLog:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
            )
            self.stream = os.fdopen(fd, "ab", buffering=0)
        return self

    def __exit__(self, *_: object) -> None:
        self.end_step()
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def _write(self, data: bytes) -> None:
        if self.stream is not None and data:
            self.stream.write(data)
            self.stream.flush()

    def line(self, text: str) -> None:
        self._write(text.encode() + b"\n")

    def say(self, text: str) -> None:
        if self.progress is not None:
            self.progress(text)

    def feed(self, stream: str, block: bytes) -> None:
        if self.raw is not None:
            self.raw(block)
        data = self.pending.pop(stream, b"") + block
        cut = data.rfind(b"\n") + 1
        self._write(data[:cut])
        if data[cut:]:
            self.pending[stream] = data[cut:]

    def end_step(self) -> None:
        for stream in sorted(self.pending):
            self._write(self.pending.pop(stream) + b"\n")


class _Execution:
    """One run of a plan: probes, staging, invocations, smoke, drift, outputs."""

    def __init__(
        self,
        plan: BuildPlan,
        grant: BuildGrant,
        docker: DockerTool,
        runner: Runner,
        log: _BuildLog,
        cancel: threading.Event | None,
    ) -> None:
        self.plan = plan
        self.spec = plan.spec
        self.grant = grant
        self.docker = docker
        self.runner = runner
        self.log = log
        self.cancel = cancel
        self.nonce = secrets.token_hex(8)
        self.steps: list[dict[str, Any]] = []
        self.native: str | None = None
        self.buildx_version: str | None = None
        self.builder_name: str | None = None
        self.builder_manifests: dict[str, str] = {}
        self.total = len(self.spec._invocations()) + sum(
            len(self.spec.platforms) for item in self.spec.images if item.smoke
        )

    def _run(
        self,
        argv: list[str],
        cwd: Path,
        seconds: float,
        *,
        step: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        if self.cancel is not None and self.cancel.is_set():
            raise BuildSpecError(
                "cancelled", "build cancelled", steps=tuple(self.steps)
            )
        if self.grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        self.docker.tool.verify()
        if step is not None:
            label = f"{step['platform']} {step['kind']}"
            self.log.line(f"### {label} started")
            self.log.say(
                f"[piceli] {len(self.steps) + 1}/{self.total} {label}: running"
            )
        receipt, stdout, stderr = self.runner(
            [str(self.docker.tool.path), *argv[1:]],
            cwd,
            ProcessLimits(seconds, PROCESS_OUTPUT_BUDGET),
            dict(self.docker.environment),
            self.grant.expires_at,
            on_output=self.log.feed if step is not None else None,
        )
        self.docker.tool.verify()
        return receipt, stdout, stderr

    def _record(
        self,
        step: dict[str, Any],
        receipt: Mapping[str, Any],
        state: str,
        **extra: Any,
    ) -> None:
        seconds = round(float(receipt["seconds"]), 3)
        self.steps.append(
            {
                **step,
                "state": state,
                "exit_code": receipt["exit_code"],
                "seconds": seconds,
                **extra,
            }
        )
        label = f"{step['platform']} {step['kind']}"
        self.log.end_step()
        self.log.line(f"### {label} {state} {seconds}s")
        self.log.say(
            f"[piceli] {len(self.steps)}/{self.total} {label}: {state} in {seconds}s"
        )

    def _call(
        self,
        argv: list[str],
        cwd: Path,
        seconds: float,
        *,
        step: dict[str, Any] | None = None,
    ) -> bytes:
        receipt, stdout, _ = self._run(argv, cwd, seconds, step=step)
        if step is not None:
            self._record(step, receipt, receipt["state"])
        if receipt["state"] != "succeeded":
            code = "build-failed" if step is not None else "docker-unavailable"
            if receipt["state"] == "timed-out":
                code = "build-timed-out"
            raise BuildSpecError(code, "docker step failed", steps=tuple(self.steps))
        return stdout

    def _probe(self, root: Path) -> None:
        version = self._call(["docker", "buildx", "version"], root, 30)
        self.buildx_version = version.decode(errors="replace").strip()[:200] or None
        if self.spec.buildx_builder is not None:
            self.builder_name = self.spec.buildx_builder
        else:
            name = self._call(["docker", "context", "show"], root, 30)
            text = name.decode(errors="replace").strip()
            if _BUILDER_NAME.fullmatch(text) is None:
                raise BuildSpecError("docker-unavailable", "unusable docker context")
            self.builder_name = text
        info = self._call(
            ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], root, 30
        )
        self.native = _native_platform(info.decode(errors="replace"))

    def execute(self, output_dir: Path) -> dict[str, Any]:
        spec = self.spec
        with temporary_directory("build") as directory:
            staging = directory
            self._probe(staging)
            assert self.builder_name is not None
            (staging / "empty").mkdir()
            (staging / "contexts").mkdir()
            (staging / "dockerfiles").mkdir()
            (staging / "metadata").mkdir()
            (staging / "out").mkdir()
            for context in spec.contexts:
                try:
                    stage_context(
                        self.plan.context_roots[context.name],
                        self.plan.contexts[context.name],
                        staging / "contexts" / context.name,
                        mtime=spec.source_date_epoch,
                    )
                except BuildContextError as error:
                    raise BuildSpecError(error.code, str(error)) from None
            for platform, text in self.plan.dockerfiles.items():
                path = staging / "dockerfiles" / f"{platform_slug(platform)}.Dockerfile"
                path.write_text(text)
            collected: dict[str, tuple[Path, str]] = {}
            images: dict[str, dict[str, Any]] = {}
            multi = len(spec.platforms) > 1
            for platform, kind, image, argv in spec._invocations(
                docker=str(self.docker.tool.path),
                builder=self.builder_name,
                staging=str(staging),
                pending=self.nonce,
            ):
                step = {"platform": platform, "kind": kind}
                self._call(argv, staging, spec.timeout_seconds, step=step)
                metadata = self._metadata(
                    staging
                    / "metadata"
                    / (
                        f"{platform_slug(platform)}-files.json"
                        if image is None
                        else f"{platform_slug(platform)}-image-{image.name}.json"
                    )
                )
                self._record_builder(platform, metadata)
                if image is None:
                    prefix = f"{platform_slug(platform)}/" if multi else ""
                    for to, (path, sha) in self._collect(
                        staging / "out" / platform_slug(platform)
                    ).items():
                        collected[prefix + to] = (path, sha)
                else:
                    key = (
                        f"{image.name}/{platform_slug(platform)}"
                        if multi
                        else image.name
                    )
                    images[key] = self._image(image, platform, multi, metadata, staging)
                    if image.smoke is not None:
                        self._smoke(image, platform, images[key]["image_id"], staging)
            self._verify_consumed()
            files = self._publish(collected, output_dir)
        return {"files": files, "images": images}

    def _image(
        self,
        image: ImageOutput,
        platform: str,
        multi: bool,
        metadata: Mapping[str, Any],
        staging: Path,
    ) -> dict[str, Any]:
        """Inspect a loaded image; move a templated tag to its resolved value."""
        loaded = image.ref(platform, multi, pending=self.nonce)
        result = self._inspect(loaded, platform, metadata, staging)
        if not image.templated:
            return result
        final = image.ref(platform, multi, image_id=result["image_id"])
        self._call(["docker", "tag", loaded, final], staging, 60)
        try:
            self._call(["docker", "image", "rm", "--no-prune", loaded], staging, 60)
        except BuildSpecError:
            pass  # a leftover pending tag is harmless; the final tag is set
        resolved = self._inspect(final, platform, metadata, staging)
        if resolved["image_id"] != result["image_id"]:
            raise BuildSpecError("image-mismatch", "tagged image differs from build")
        return resolved

    def _smoke(
        self, image: ImageOutput, platform: str, image_id: str, staging: Path
    ) -> None:
        """Run the image's smoke check; a wrong exit code or output rejects the build.

        The exit code is checked first; then ``expect_stdout``/``expect_stderr``
        against the bounded capture. A mismatch names the streams in the step
        (``unmatched``) and shows a short escaped excerpt on the progress sink
        and in the build log only, never in the error, JSON or receipt.
        """
        smoke = image.smoke
        assert smoke is not None
        name = f"piceli-smoke-{self.nonce}-{len(self.steps)}"
        step = {"platform": platform, "kind": f"smoke:{image.name}"}
        argv = self.spec.smoke_argv(image, platform, image_id, name=name)
        receipt, stdout, stderr = self._run(
            argv, staging, smoke.timeout_seconds, step=step
        )
        state = receipt["state"]
        finished = state in {"succeeded", "failed"}
        passed = finished and receipt["exit_code"] == smoke.expect_exit
        unmatched = smoke.unmatched(stdout, stderr) if passed else []
        if unmatched:
            self._record(step, receipt, "failed", unmatched=unmatched)
            for stream in unmatched:
                data = stdout if stream == "stdout" else stderr
                pattern = (
                    smoke.expect_stdout if stream == "stdout" else smoke.expect_stderr
                )
                text = (
                    f"[piceli] smoke:{image.name}: {stream} did not match "
                    f"{json.dumps(pattern)} ({len(data)} bytes); starts with "
                    f"{_excerpt(data)}"
                )
                self.log.line(text)
                self.log.say(text)
            raise BuildSpecError(
                "smoke-output-mismatch",
                f"smoke output did not match ({', '.join(unmatched)})",
                steps=tuple(self.steps),
            )
        self._record(
            step, receipt, "succeeded" if passed else "failed" if finished else state
        )
        if passed:
            return
        if not finished:
            # The docker CLI was killed; the container may still be running.
            try:
                self._call(["docker", "rm", "--force", name], staging, 30)
            except BuildSpecError:
                pass
        if state == "cancelled":
            raise BuildSpecError(
                "cancelled", "build cancelled", steps=tuple(self.steps)
            )
        code = "smoke-failed" if finished else "smoke-timed-out"
        raise BuildSpecError(code, "smoke check failed", steps=tuple(self.steps))

    def _verify_consumed(self) -> None:
        """Drift check over what the build consumed: staged files and spec."""
        for context in self.spec.contexts:
            try:
                verify_staged(
                    self.plan.context_roots[context.name],
                    self.plan.contexts[context.name],
                )
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
        origin = self.spec.origin
        if origin is not None:
            try:
                fresh = BuildSpec.from_toml(origin)
                if self.spec.platform_override is not None:
                    fresh = fresh.with_platform(self.spec.platform_override)
                same = fresh.spec_sha256 == self.spec.spec_sha256
            except BuildSpecError:
                same = False
            if not same:
                raise BuildSpecError("spec-changed", "build spec changed")
        self.log.say(
            "[piceli] drift check: "
            f"{sum(len(item.files) for item in self.plan.contexts.values())} "
            "staged file(s) unchanged"
        )

    def _metadata(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_text()
        except OSError:
            return {}
        try:
            value = strict_json(raw, 4 * 1024 * 1024)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _record_builder(self, platform: str, metadata: Mapping[str, Any]) -> None:
        info = metadata.get("containerimage.buildinfo")
        if not isinstance(info, dict):
            return
        for source in info.get("sources", []) or []:
            if (
                isinstance(source, dict)
                and isinstance(source.get("ref"), str)
                and source["ref"].endswith("@" + self.spec.builder.digest)
                and isinstance(source.get("pin"), str)
                and _DIGEST.fullmatch(source["pin"])
            ):
                self.builder_manifests[platform] = source["pin"]

    def _collect(self, root: Path) -> dict[str, tuple[Path, str]]:
        expected = {item.to for item in self.spec.files}
        found: dict[str, tuple[Path, str]] = {}
        if not root.is_dir() or root.is_symlink():
            raise BuildSpecError("output-invalid", "build produced no outputs")
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            for name in dirnames:
                if (base / name).is_symlink():
                    raise BuildSpecError("output-invalid", "output is a symlink")
            for name in filenames:
                path = base / name
                rel = path.relative_to(root).as_posix()
                metadata = os.lstat(path)
                if not stat.S_ISREG(metadata.st_mode) or rel not in expected:
                    raise BuildSpecError(
                        "output-invalid", "build produced an undeclared output"
                    )
                if metadata.st_size > MAX_OUTPUT_FILE_BYTES:
                    raise BuildSpecError("output-invalid", "output exceeds its budget")
                with path.open("rb") as stream:
                    found[rel] = (path, digest_stream(stream))
        if set(found) != expected:
            raise BuildSpecError("output-invalid", "a declared output is missing")
        return found

    def _inspect(
        self,
        ref: str,
        platform: str,
        metadata: Mapping[str, Any],
        staging: Path,
    ) -> dict[str, Any]:
        raw = self._call(["docker", "image", "inspect", ref], staging, 60)
        try:
            value = strict_json(raw.decode(), 16 * 1024 * 1024)
            image = value[0]
            image_id = image["Id"]
            actual = f"{image['Os']}/{image['Architecture']}"
            if image.get("Variant"):
                actual += f"/{image['Variant']}"
        except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError):
            raise BuildSpecError("image-invalid", "invalid image inspection") from None
        if not isinstance(image_id, str) or _DIGEST.fullmatch(image_id) is None:
            raise BuildSpecError("image-invalid", "invalid image id")
        if actual != platform:
            raise BuildSpecError("image-mismatch", "image platform mismatch")
        config = metadata.get("containerimage.config.digest")
        if config is not None and config != image_id:
            raise BuildSpecError("image-mismatch", "loaded image differs from build")
        manifest = metadata.get("containerimage.digest")
        if not isinstance(manifest, str) or _DIGEST.fullmatch(manifest) is None:
            manifest = None
        # Docker's classic image store keeps no manifest: its "digest" is the
        # config digest. Only a distinct value is a real manifest digest.
        if manifest == image_id:
            manifest = None
        size = image.get("Size")
        return {
            "image_id": image_id,
            "digest": manifest,
            "platform": platform,
            "ref": ref,
            # Added in 0.8.0: the engine's size of the image, for summaries.
            **({"size_bytes": size} if type(size) is int and size >= 0 else {}),
        }

    def _publish(
        self, collected: Mapping[str, tuple[Path, str]], output_dir: Path
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        if not collected:
            return result
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise BuildSpecError("output-invalid", "output directory is not usable")
        for rel, (source, sha) in sorted(collected.items()):
            target = output_dir.joinpath(*PurePosixPath(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.is_dir():
                raise BuildSpecError("output-invalid", "output path is occupied")
            temporary = target.with_name(f".{target.name}.piceli-partial")
            temporary.unlink(missing_ok=True)
            shutil.copyfile(source, temporary, follow_symlinks=False)
            os.chmod(temporary, 0o755 if os.access(source, os.X_OK) else 0o644)
            with temporary.open("rb") as stream:
                if digest_stream(stream) != sha:
                    temporary.unlink(missing_ok=True)
                    raise BuildSpecError("output-invalid", "output changed on copy")
            os.replace(temporary, target)
            result[rel] = sha
        return result


def digest_stream(stream: Any) -> str:
    return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


# --- CLI wiring ----------------------------------------------------------------


def add_build_spec_commands(subparsers: Any) -> None:
    """Register ``build-spec preview|run`` on an argparse subparsers object."""
    command = subparsers.add_parser(
        "build-spec", help="containerized builds from a declarative build.toml"
    )
    actions = command.add_subparsers(dest="build_spec_command", required=True)
    for name in ("preview", "run"):
        action = actions.add_parser(name)
        action.add_argument("--spec", type=Path, required=True)
        action.add_argument("--inputs", type=Path)
        if name == "run":
            action.add_argument("--lock", type=Path)
            action.add_argument("--approve-builder", required=True)
            action.add_argument("--approve-plan")
            action.add_argument("--allow-network", action="store_true")
            action.add_argument("--out", type=Path, required=True)
            action.add_argument("--output-dir", type=Path)
            action.add_argument("--docker", type=Path)
            action.add_argument("--docker-sha256")
            action.add_argument("--log", type=Path)
            action.add_argument(
                "--progress",
                choices=("steps", "plain", "quiet"),
                default="steps",
                help="stderr during the build: one line per step (default), "
                "plus the raw build output (plain), or nothing (quiet)",
            )
            action.add_argument("--max-seconds", type=float, default=7200.0)


FAILED_CODES = frozenset(
    {
        "build-failed",
        "build-timed-out",
        "smoke-failed",
        "smoke-timed-out",
        "smoke-output-mismatch",
    }
)


def _progress_line(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _raw_output(block: bytes) -> None:
    sys.stderr.flush()
    sys.stderr.buffer.write(block)
    sys.stderr.buffer.flush()


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.piceli-partial")
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run_build_spec_command(
    args: argparse.Namespace,
    *,
    runner: Runner | None = None,
    docker: DockerTool | None = None,
) -> int:
    """Run a parsed ``build-spec`` command; print JSON; return 0, 1 or 2.

    Exit 1 means a docker build step or a smoke check failed (the steps are
    printed, never their output). Exit 2 means the input or grant was
    rejected. The result object is printed on stdout in every case
    (``{"state": "failed"|"rejected", "reason": <code>, "message": …}`` on
    failure); stderr carries progress and a human summary only. Error output
    carries only a fixed reason code, never paths, output or secrets.
    """
    try:
        spec = BuildSpec.from_toml(args.spec)
        inputs = InputsSpec.from_toml(args.inputs) if args.inputs else None
        if args.build_spec_command == "preview":
            print(json.dumps(spec.plan(inputs).preview(), sort_keys=True))
            return 0
        lock = None
        if args.lock is not None:
            lock = InputsLock.from_json(args.lock.read_text())
        if args.max_seconds <= 0 or args.max_seconds > 86_400:
            raise BuildSpecError("invalid-grant", "invalid grant duration")
        grant = BuildGrant(
            args.approve_builder,
            time.time() + args.max_seconds,
            args.allow_network,
            args.approve_plan,
        )
        if docker is None and (args.docker or args.docker_sha256):
            if not (args.docker and args.docker_sha256):
                raise BuildSpecError(
                    "invalid-tool", "give --docker and --docker-sha256"
                )
            docker = DockerTool.discover(args.docker, args.docker_sha256)
        receipt = spec.run(
            grant,
            args.output_dir or args.out.resolve().parent,
            inputs=inputs,
            lock=lock,
            docker=docker,
            runner=runner,
            log=args.log,
            progress=None if args.progress == "quiet" else _progress_line,
            raw_output=_raw_output if args.progress == "plain" else None,
        )
        _write_atomic(args.out, receipt.to_json())
        print(json.dumps(receipt.to_dict(), sort_keys=True))
        return 0
    except BuildSpecError as error:
        failed = error.code in FAILED_CODES
        body: dict[str, Any] = {}
        if error.steps:
            body["steps"] = list(error.steps)
        return _result_error(error.code, failed=failed, **body)
    except SourceIdentityError:
        return _result_error("invalid-inputs")
    except (ValueError, KeyError, TypeError, OSError):
        # Never echo private paths, process output or attacker-controlled text.
        return _result_error("invalid-or-unavailable-build-input")


def _result_error(reason: str, *, failed: bool = False, **fields: Any) -> int:
    """Print the failure or rejection for ``reason``; return exit 1 or 2."""
    from piceli.cli_contract import Rejected, fail, reject

    try:
        if failed:
            fail(reason, **fields)
        reject(reason, **fields)
    except Rejected as rejected:
        return int(rejected.code or 2)
