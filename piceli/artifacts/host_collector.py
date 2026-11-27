"""OpenTelemetry Collector images, built with OCB (0.94.0).

An ``[[output.image]]`` of a host build may hold a ``collector`` table::

    [[output.image]]
    name = "collector"
    repository = "shop/collector"
    base = "scratch"
    user = "10001:10001"
    [output.image.collector]
    version = "0.166.0"
    receivers = ["otlp", "statsd"]
    processors = ["memory_limiter", "resource"]
    exporters = ["otlp_http"]
    extensions = ["health_check"]

Each entry is a short name from :data:`COMPONENTS` (its module at
``v<version>``) or a full ``"<module> v<semver>"`` pin. Piceli writes the
OCB (OpenTelemetry Collector Builder) manifest with only those components,
installs OCB ``v<version>`` with the Go that ``build.toolchains.go`` pins,
and runs it for the image's platform (``CGO_ENABLED=0``, ``GOOS=linux``,
``GOARCH`` of the platform). Everything lives under the platform's target
directory::

    <target>/piceli-collector/<image>/manifest.yaml
    <target>/piceli-collector/<image>/build/otelcol        the binary
    <target>/piceli-collector/<image>/THIRD-PARTY-NOTICES

After the build, ``go version -m`` lists the modules linked into the
binary; :func:`notices` writes each one's licence and notice files (from
the module cache) and Go's own licence into ``THIRD-PARTY-NOTICES``, and the
SBOM (software bill of materials) lists each module
(:func:`piceli.artifacts.attestations.go_packages`).

Errors: ``invalid-spec`` for a table that cannot be read,
``collector-notices-failed`` when the linked modules cannot be listed or
found, ``collector-licence-missing`` when a module has no licence file and
no parent module that has one.

Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.artifacts.build_spec import BuildSpecError, _abs_path, _fail, _keys

if TYPE_CHECKING:
    from piceli.artifacts.host_build import HostImage
    from piceli.artifacts.host_execution import HostExecution

BUILDER = "go.opentelemetry.io/collector/cmd/builder"
CORE = "go.opentelemetry.io/collector"
CONTRIB = "github.com/open-telemetry/opentelemetry-collector-contrib"
#: The component lists of a collector table, in the manifest's order.
KINDS = ("extensions", "receivers", "processors", "exporters", "connectors")
DIST_NAME = "otelcol"
DIST_MODULE = "piceli.dev/otelcol"
#: The collector's directory under the platform's target directory.
WORK = "piceli-collector"
MANIFEST = "manifest.yaml"
NOTICES = "THIRD-PARTY-NOTICES"
DEFAULT_BINARY = "/otelcol"
DEFAULT_NOTICES = "/" + NOTICES
#: The keys of ``[output.image.collector]``.
TABLE_KEYS = frozenset({"version", "binary", "notices", *KINDS})
#: Keys of ``[[output.image]]`` a collector image cannot set: the collector
#: table makes its files and its entrypoint.
REFUSED_IMAGE_KEYS = ("target_files", "shared_files", "entrypoint")
MAX_COMPONENTS = 64
#: A licence or notice file larger than this is refused (not a licence).
MAX_NOTICE_BYTES = 1024 * 1024

#: Short names, per list, of components whose module is versioned with the
#: collector (``v<version>``). Checked against the otelcol-contrib manifest
#: of collector 0.166.0.
COMPONENTS: Mapping[str, Mapping[str, str]] = {
    "receivers": {
        "otlp": f"{CORE}/receiver/otlpreceiver",
        "statsd": f"{CONTRIB}/receiver/statsdreceiver",
        "mongodb": f"{CONTRIB}/receiver/mongodbreceiver",
        "rabbitmq": f"{CONTRIB}/receiver/rabbitmqreceiver",
        "prometheus": f"{CONTRIB}/receiver/prometheusreceiver",
        "hostmetrics": f"{CONTRIB}/receiver/hostmetricsreceiver",
    },
    "processors": {
        "batch": f"{CORE}/processor/batchprocessor",
        "memory_limiter": f"{CORE}/processor/memorylimiterprocessor",
        "resource": f"{CONTRIB}/processor/resourceprocessor",
        "attributes": f"{CONTRIB}/processor/attributesprocessor",
        "filter": f"{CONTRIB}/processor/filterprocessor",
        "transform": f"{CONTRIB}/processor/transformprocessor",
    },
    "exporters": {
        "otlp": f"{CORE}/exporter/otlpexporter",
        "otlp_http": f"{CORE}/exporter/otlphttpexporter",
        "debug": f"{CORE}/exporter/debugexporter",
    },
    "extensions": {
        "health_check": f"{CONTRIB}/extension/healthcheckextension",
        "file_storage": f"{CONTRIB}/extension/storage/filestorage",
        "zpages": f"{CORE}/extension/zpagesextension",
    },
    "connectors": {},
}
#: The other spelling of a renamed component type.
ALIASES: Mapping[str, str] = {
    "otlphttp": "otlp_http",
    "memorylimiter": "memory_limiter",
    "healthcheck": "health_check",
    "filestorage": "file_storage",
    "k8s_attributes": "k8sattributes",
}
#: Stable modules (v1.x): their version does not follow the collector's, so
#: a short name is refused and the message asks for the full pin.
STABLE: Mapping[str, Mapping[str, tuple[str, str]]] = {
    "processors": {
        "k8sattributes": (
            f"{CONTRIB}/processor/k8sattributesprocessor",
            "v1.5.0 at collector 0.166.0",
        ),
    },
}

_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
_SHORT = re.compile(r"[a-z0-9][a-z0-9_]{0,63}")
_PIN = re.compile(
    r"([a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[A-Za-z0-9._~-]+)+)"
    r" (v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?)"
)
_NOTICE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
_RULE = "=" * 72


@dataclass(frozen=True)
class CollectorSpec:
    """``[output.image.collector]``: the collector version and its modules."""

    version: str
    #: Per kind of :data:`KINDS`: ``"<module> v<semver>"`` lines, as declared.
    components: tuple[tuple[str, tuple[str, ...]], ...]
    binary: str = DEFAULT_BINARY
    notices: str = DEFAULT_NOTICES

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "binary": self.binary,
            "notices": self.notices,
            "components": {kind: list(items) for kind, items in self.components},
        }

    def gomods(self, kind: str) -> tuple[str, ...]:
        return dict(self.components).get(kind, ())

    @property
    def manifest(self) -> str:
        return manifest(self)

    @property
    def manifest_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.manifest.encode()).hexdigest()


def known(kind: str) -> list[str]:
    """The short names of ``kind``, with their other spellings."""
    names = set(COMPONENTS[kind])
    names |= {alias for alias, name in ALIASES.items() if name in COMPONENTS[kind]}
    return sorted(names)


def _component(kind: str, entry: Any, version: str, image: str) -> str:
    where = f"image {image!r}: collector.{kind}"
    if not isinstance(entry, str):
        raise _fail(f"{where}: entries are short names or '<module> v<semver>' pins")
    if " " in entry:
        if _PIN.fullmatch(entry) is None:
            raise _fail(
                f"{where}: {entry[:120]!r} is not '<module path> v<semver>' "
                f"(for example '{COMPONENTS['receivers']['otlp']} v{version}')"
            )
        return entry
    name = ALIASES.get(entry, entry)
    if _SHORT.fullmatch(entry) is None:
        raise _fail(f"{where}: {entry[:120]!r} is not a component name")
    stable = STABLE.get(kind, {}).get(name)
    if stable is not None:
        module, pinned = stable
        raise _fail(
            f"{where}: {entry!r} is a stable module whose version does not "
            f"follow the collector's ({pinned}); write the full pin, for "
            f'example "{module} v1.5.0"'
        )
    module = COMPONENTS[kind].get(name)
    if module is None:
        listed = ", ".join(known(kind)) or "none"
        raise _fail(
            f"{where}: unknown component {entry!r}; known short names: {listed}; "
            "any other component takes a full pin '<module path> v<semver>'"
        )
    return f"{module} v{version}"


def parse(value: Any, image: str) -> CollectorSpec:
    """``[output.image.collector]`` of image ``image``.

    :raises BuildSpecError: ``invalid-spec``.
    """
    _keys(value, {"version"}, set(TABLE_KEYS) - {"version"}, "output.image.collector")
    version = value["version"]
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise _fail(
            f"image {image!r}: collector.version must be an exact collector "
            'release such as "0.166.0" (OCB and the distribution use it)'
        )
    components: list[tuple[str, tuple[str, ...]]] = []
    modules: set[str] = set()
    for kind in KINDS:
        items = value.get(kind, [])
        if not isinstance(items, list) or len(items) > MAX_COMPONENTS:
            raise _fail(
                f"image {image!r}: collector.{kind} must list at most "
                f"{MAX_COMPONENTS} components"
            )
        lines = []
        for entry in items:
            line = _component(kind, entry, version, image)
            module = line.split(" ", 1)[0]
            if module in modules:
                raise _fail(
                    f"image {image!r}: collector lists {module} twice; name each "
                    "component once"
                )
            modules.add(module)
            lines.append(line)
        if lines:
            components.append((kind, tuple(lines)))
    found = dict(components)
    if not found.get("receivers") or not found.get("exporters"):
        raise _fail(
            f"image {image!r}: a collector needs at least one receiver and one exporter"
        )
    binary = _abs_path(value.get("binary", DEFAULT_BINARY), "collector.binary")
    notices = _abs_path(value.get("notices", DEFAULT_NOTICES), "collector.notices")
    if binary == notices:
        raise _fail(f"image {image!r}: collector.binary and notices are one path")
    return CollectorSpec(version, tuple(components), binary, notices)


def check_image(item: Mapping[str, Any], spec: CollectorSpec) -> None:
    """A collector image sets none of :data:`REFUSED_IMAGE_KEYS`, and its
    ``files`` do not land on the binary or the notices."""
    name = item.get("name")
    for key in REFUSED_IMAGE_KEYS:
        if key in item:
            raise _fail(
                f"image {name!r} has a collector table, so it cannot set {key!r}: "
                f"the collector makes its binary ({spec.binary}, the entrypoint) "
                f"and its notices ({spec.notices}); add other files with files "
                "and arguments with cmd"
            )
    files = item.get("files", {})
    taken = {spec.binary, spec.notices}
    for dest in files.values() if isinstance(files, dict) else ():
        if dest in taken:
            raise _fail(
                f"image {name!r}: files puts a file at {dest}, where the "
                "collector writes its binary or notices"
            )


def mappings(image: str, spec: CollectorSpec) -> tuple[tuple[str, str], ...]:
    """The image's files the collector build makes, as ``target_files``
    entries (paths under the platform's target directory)."""
    return (
        (f"{WORK}/{image}/build/{DIST_NAME}", spec.binary),
        (f"{WORK}/{image}/{NOTICES}", spec.notices),
    )


def manifest(spec: CollectorSpec) -> str:
    """The OCB manifest: the distribution and only the listed components.

    ``output_path`` is relative (OCB runs in the image's collector
    directory), so the text, and its sha256, is the same on every machine.
    OCB adds its default configuration providers (env, file, http, https,
    yaml) when the manifest names none.
    """
    lines = [
        "# Written by piceli for a host build; changes are overwritten.",
        "dist:",
        f"  name: {json.dumps(DIST_NAME)}",
        f"  module: {json.dumps(DIST_MODULE)}",
        f"  description: {json.dumps('OpenTelemetry Collector')}",
        f"  version: {json.dumps(spec.version)}",
        f"  output_path: {json.dumps('./build')}",
    ]
    for kind, items in spec.components:
        lines.append(f"{kind}:")
        lines.extend(f"  - gomod: {json.dumps(item)}" for item in items)
    return "\n".join(lines) + "\n"


def summary(spec: CollectorSpec, built: Mapping[str, Any]) -> dict[str, Any]:
    """What the receipt and the provenance record of one collector."""
    return {
        "version": spec.version,
        "manifest_sha256": spec.manifest_sha256,
        "components": {kind: list(items) for kind, items in spec.components},
        "go": built.get("go"),
        "go_modules": len(built.get("modules") or ()),
    }


# ------------------------------------------------------------------ build


def build(
    execution: HostExecution, image: HostImage, environment: Mapping[str, str]
) -> dict[str, Any]:
    """Write the manifest, install OCB, build the binary, write the notices.

    Returns ``{"go": "go1.X.Y", "modules": [{"path", "version", "sum"?}]}``.
    """
    from piceli.dev import toolchains

    spec = image.collector
    assert spec is not None
    platform = execution.plan.platform
    go = shutil.which("go", path=environment.get("PATH"))
    if go is None:
        raise BuildSpecError(
            "host-toolchain-failed",
            f"image {image.name!r}: the pinned go is not on the commands' PATH",
            steps=tuple(execution.steps),
        )
    work = execution.target / WORK / image.name
    work.mkdir(parents=True, exist_ok=True)
    (work / MANIFEST).write_text(spec.manifest)
    binary = work / "build" / DIST_NAME
    written = work / NOTICES
    for stale in (binary, written):  # never package an earlier build's file
        stale.unlink(missing_ok=True)
    system, arch = toolchains.host_platform()
    gobin = execution.cache / "go" / "ocb" / f"{spec.version}-{system}-{arch}"
    execution._command(
        1,
        ("go", "install", f"{BUILDER}@v{spec.version}"),
        work,
        {**environment, "GOBIN": str(gobin)},
        program=Path(go),
        kind=f"collector-{image.name}-install",
        title=f"{platform} collector {image.name}: OCB v{spec.version}",
    )
    execution._command(
        2,
        ("builder", "--config", MANIFEST),
        work,
        {
            **environment,
            "CGO_ENABLED": "0",
            "GOOS": "linux",
            "GOARCH": execution.plan.facts.architecture,
        },
        program=gobin / "builder",
        kind=f"collector-{image.name}-build",
        title=f"{platform} collector {image.name}: build",
    )
    if binary.is_symlink() or not binary.is_file():
        raise BuildSpecError(
            "host-output-missing",
            f"image {image.name!r}: OCB wrote no {DIST_NAME} binary",
            steps=tuple(execution.steps),
        )
    start = execution._offset()
    began = time.monotonic()
    try:
        go_version, modules = buildinfo(Path(go), binary, environment)
        text = notices(
            modules,
            go_version,
            Path(go).resolve().parent.parent,
            Path(
                environment.get("GOMODCACHE")
                or toolchains.go_env(execution.cache)["GOMODCACHE"]
            ),
            image.repository,
        )
    except BuildSpecError as error:
        raise BuildSpecError(
            error.code, str(error), steps=tuple(execution.steps)
        ) from None
    written.write_text(text)
    execution.steps.append(
        {
            "platform": platform,
            "kind": f"collector-{image.name}-notices",
            "state": "succeeded",
            "seconds": round(time.monotonic() - began, 3),
            "start_seconds": start,
        }
    )
    execution.log.say(
        f"[piceli] collector {image.name}: {len(modules)} Go module(s), notices written"
    )
    return {
        "go": go_version,
        "modules": [module.to_dict() for module in modules],
    }


# ------------------------------------------------------------------ notices


@dataclass(frozen=True)
class GoModule:
    """A module linked into a Go binary (``go version -m``)."""

    path: str
    version: str
    sum: str | None = None

    def to_dict(self) -> dict[str, str]:
        found = {"path": self.path, "version": self.version}
        if self.sum:
            found["sum"] = self.sum
        return found


def _run(*args: Any, **kwargs: Any) -> Any:
    return subprocess.run(*args, **kwargs)


def buildinfo(
    go: Path, binary: Path, environment: Mapping[str, str]
) -> tuple[str, list[GoModule]]:
    """``go version -m binary``: the Go release and the linked modules.

    :raises BuildSpecError: ``collector-notices-failed``.
    """
    try:
        completed = _run(
            [str(go), "version", "-m", str(binary)],
            # GOFLAGS holds build flags, which go version does not take.
            env={k: v for k, v in environment.items() if k != "GOFLAGS"},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BuildSpecError(
            "collector-notices-failed", f"cannot run go version -m ({error})"
        ) from None
    if completed.returncode != 0:
        tail = " | ".join((completed.stderr or "").strip().splitlines()[-5:])[:600]
        raise BuildSpecError(
            "collector-notices-failed", f"go version -m failed: {tail}"
        )
    return parse_buildinfo(completed.stdout or "")


def parse_buildinfo(text: str) -> tuple[str, list[GoModule]]:
    """The Go release and the ``dep`` modules of ``go version -m`` output; a
    ``=>`` line replaces the module before it. Sorted by path and version.

    :raises BuildSpecError: ``collector-notices-failed``.
    """
    lines = text.splitlines()
    head = lines[0].rsplit(":", 1) if lines else []
    go_version = head[1].strip() if len(head) == 2 else ""
    if not re.fullmatch(r"go[0-9][0-9A-Za-z.+-]*", go_version):
        raise BuildSpecError(
            "collector-notices-failed",
            "go version -m printed no Go release for the collector binary",
        )
    found: list[GoModule] = []
    for line in lines[1:]:
        fields = line.strip().split("\t")
        if fields[0] not in {"dep", "=>"}:
            continue
        if len(fields) < 3 or not fields[2].startswith("v"):
            raise BuildSpecError(
                "collector-notices-failed",
                f"go version -m: {line.strip()[:200]!r} names no module version "
                "(a module replaced by a local directory has no licence to copy)",
            )
        module = GoModule(fields[1], fields[2], fields[3] if len(fields) > 3 else None)
        if fields[0] == "=>":
            if not found:
                raise BuildSpecError(
                    "collector-notices-failed", "go version -m: '=>' before a dep"
                )
            found[-1] = module
        else:
            found.append(module)
    unique = {(item.path, item.version): item for item in found}
    return go_version, [unique[key] for key in sorted(unique)]


def escape(value: str) -> str:
    """A module path or version as the module cache spells it: each upper-case
    letter is ``!`` and its lower case."""
    return "".join("!" + char.lower() if char.isupper() else char for char in value)


def module_dir(modcache: Path, module: GoModule) -> Path:
    return modcache / f"{escape(module.path)}@{escape(module.version)}"


def licence_files(directory: Path) -> list[Path]:
    """The licence and notice files at the top of ``directory``, by name."""
    found = []
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.name.upper().startswith(_NOTICE_PREFIXES):
            found.append(entry)
    return sorted(found, key=lambda item: item.name)


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_NOTICE_BYTES:
        raise BuildSpecError(
            "collector-notices-failed", f"{path.name}: larger than a licence file"
        )
    return path.read_bytes().decode("utf-8", "replace").rstrip("\n")


def _section(title: str, note: str | None, files: Sequence[Path]) -> list[str]:
    lines = ["", _RULE, title, _RULE]
    if note:
        lines.append(note)
    for path in files:
        lines += ["", f"--- {path.name} ---", "", _read(path)]
    return lines


def notices(
    modules: Sequence[GoModule],
    go_version: str,
    goroot: Path,
    modcache: Path,
    title: str,
) -> str:
    """``THIRD-PARTY-NOTICES``: Go's licence, then per linked module (by path
    and version) its licence and notice files. A module without one uses
    the nearest parent module of the list that has one, and says so.

    :raises BuildSpecError: ``collector-notices-failed`` (a module missing
        from the cache), ``collector-licence-missing``.
    """
    go_licence = goroot / "LICENSE"
    if not go_licence.is_file():
        raise BuildSpecError(
            "collector-licence-missing", f"{go_version}: no LICENSE in its GOROOT"
        )
    own: dict[str, list[Path]] = {}
    for module in modules:
        directory = module_dir(modcache, module)
        if not directory.is_dir():
            raise BuildSpecError(
                "collector-notices-failed",
                f"{module.path} {module.version}: not in the module cache "
                f"({escape(module.path)}@{escape(module.version)})",
            )
        own[module.path] = licence_files(directory)
    ordered = sorted(modules, key=lambda item: (item.path, item.version))
    by_path = {item.path: item for item in ordered}
    lines = [
        f"Third-party notices for {title}",
        "",
        "The licences and notices of the Go modules linked into this image's",
        f"OpenTelemetry Collector (from 'go version -m'), and of {go_version}.",
    ]
    lines += _section(
        f"{go_version} (Go standard library and runtime)", None, [go_licence]
    )
    for module in ordered:
        files = own[module.path]
        note = None
        if not files:
            parent = _parent(module.path, own)
            if parent is None:
                raise BuildSpecError(
                    "collector-licence-missing",
                    f"{module.path} {module.version}: no LICENSE, LICENCE, "
                    "COPYING or NOTICE file, and no parent module linked into "
                    "the collector has one",
                )
            files = own[parent]
            note = (
                f"This module has no licence file of its own; these are the "
                f"files of its parent module {parent} {by_path[parent].version}."
            )
        lines += _section(f"{module.path} {module.version}", note, files)
    return "\n".join(lines) + "\n"


def _parent(path: str, own: Mapping[str, Sequence[Path]]) -> str | None:
    """The longest module path of ``own`` that holds ``path`` and has files."""
    parts = path.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:cut])
        if own.get(candidate):
            return candidate
    return None
