"""The component contract: ``[component.<name>]`` in a source's ``piceli.toml``.

A component's repository says how it is built and run; the composition says
where it runs. The format (every key but ``build`` optional; any other key is
refused with ``component-contract-invalid``)::

    [component.api]
    build = { rust = "crates/api", bin = "api" }    # or python / files / dockerfile
    image = { base = "<image>@sha256:<64 hex>", user = 10001, dirs = { "/var/lib/api" = "0700" } }
    ports = { http = 8080 }
    health = { ready = "GET /health", check = ["api", "check-config"] }
    upgrade_check = ["api", "store", "verify", "--read-only", "/var/lib/api"]
    volumes = { data = { path = "/var/lib/api", size = "8Gi", retained = true } }
    needs = ["cache?", "secret:api-token", "component:db"]
    settings = { log_level = "info" }
    emits = ["otlp:metrics"]

See ``docs/components.md``. Parsing reads no file (:func:`parse_contracts`
takes the TOML text) and runs nothing.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from piceli.infra import CompositionError

CONTRACT_FILE = "piceli.toml"
MAX_CONTRACT_BYTES = 256 * 1024

_KEYS = frozenset(
    {
        "build",
        "image",
        "ports",
        "health",
        "upgrade_check",
        "volumes",
        "needs",
        "settings",
        "emits",
    }
)
_BUILD_KINDS = ("rust", "python", "files", "dockerfile")
_BUILD_KEYS: Mapping[str, frozenset[str]] = {
    "rust": frozenset({"rust", "bin", "page_size", "features", "paths"}),
    "python": frozenset({"python", "module", "paths"}),
    "files": frozenset({"files", "paths"}),
    "dockerfile": frozenset({"dockerfile", "context", "target", "paths"}),
}
_IMAGE_KEYS = frozenset({"base", "user", "dirs", "cmd", "workdir"})
_HEALTH_KEYS = frozenset({"ready", "check"})
_VOLUME_KEYS = frozenset({"path", "size", "retained"})
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PORT_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,13}[a-z0-9])?")
_SETTING = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}")
_SIZE = re.compile(r"[0-9]+(?:Ki|Mi|Gi|Ti)")
_BASE = re.compile(r"([a-z0-9][a-z0-9./:_-]*?)@(sha256:[0-9a-f]{64})")
_MODE = re.compile(r"0?[0-7]{3}")
_USER = re.compile(r"([0-9]{1,10})(?::([0-9]{1,10}))?")
_READY = re.compile(r"(GET|TCP)\s+(\S+)")
_EMIT = re.compile(r"[a-z0-9][a-z0-9:._/-]{0,62}")
_BIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _invalid(component: str, message: str) -> CompositionError:
    return CompositionError(
        "component-contract-invalid", f"component {component!r}: {message}"
    )


def _rel(value: Any, component: str, what: str) -> str:
    """A path relative to the source root, without ``..``."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _invalid(component, f"{what} must be a relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise _invalid(component, f"{what} must stay inside the repository")
    return path.as_posix()


def _abs(value: Any, component: str, what: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise _invalid(component, f"{what} must be an absolute path")
    path = PurePosixPath(value)
    if ".." in path.parts or path.as_posix() == "/":
        raise _invalid(component, f"{what} must be an absolute path below /")
    return path.as_posix()


def _argv(value: Any, component: str, what: str) -> tuple[str, ...]:
    if (
        isinstance(value, str)
        or not isinstance(value, Sequence)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise _invalid(component, f"{what} must be a non-empty list of strings")
    return tuple(value)


def _table(value: Any, component: str, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(component, f"{what} must be a table")
    return dict(value)


def _only(
    table: Mapping[str, Any], allowed: frozenset[str], component: str, what: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise _invalid(component, f"unknown key {unknown[0]!r} in {what}")


@dataclass(frozen=True)
class BuildRecipe:
    """How a component's image is built (one of :data:`_BUILD_KINDS`).

    :param kind: ``rust``, ``python``, ``files`` or ``dockerfile``.
    :param path: The crate (``rust``), package directory (``python``),
        Dockerfile (``dockerfile``); ``""`` for ``files``.
    :param bin: The Rust binary.
    :param module: The Python module run with ``python3 -m``.
    :param files: ``files``: source path → absolute path in the image.
    :param paths: The paths the build reads (its cache key); defaults per kind.
    """

    kind: str
    path: str = ""
    bin: str | None = None
    module: str | None = None
    features: tuple[str, ...] = ()
    files: tuple[tuple[str, str], ...] = ()
    context: str = "."
    target: str | None = None
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "bin": self.bin,
            "module": self.module,
            "features": list(self.features),
            "files": dict(self.files),
            "context": self.context,
            "target": self.target,
            "paths": list(self.paths),
        }


def _build(value: Any, component: str) -> BuildRecipe:
    table = _table(value, component, "build")
    kinds = [kind for kind in _BUILD_KINDS if kind in table]
    if len(kinds) != 1:
        raise _invalid(
            component, "build names exactly one of rust, python, files, dockerfile"
        )
    kind = kinds[0]
    _only(table, _BUILD_KEYS[kind], component, "build")
    declared = table.get("paths")
    paths: tuple[str, ...] = ()
    if declared is not None:
        if (
            isinstance(declared, str)
            or not isinstance(declared, Sequence)
            or not declared
        ):
            raise _invalid(component, "build.paths must be a non-empty list")
        paths = tuple(
            dict.fromkeys(
                _rel(item, component, "build.paths entry") for item in declared
            )
        )
    if kind == "rust":
        crate = _rel(table["rust"], component, "build.rust")
        binary = table.get("bin", component)
        if not isinstance(binary, str) or not _BIN.fullmatch(binary):
            raise _invalid(component, "build.bin must be a binary name")
        if table.get("page_size", "from node") != "from node":
            raise _invalid(component, 'build.page_size is "from node" (the default)')
        features = table.get("features", [])
        if (
            isinstance(features, str)
            or not isinstance(features, Sequence)
            or not all(
                isinstance(item, str) and _BIN.fullmatch(item) for item in features
            )
        ):
            raise _invalid(component, "build.features must be a list of names")
        return BuildRecipe(
            kind,
            crate,
            bin=binary,
            features=tuple(features),
            paths=paths or tuple(dict.fromkeys((crate, "Cargo.toml", "Cargo.lock"))),
        )
    if kind == "python":
        package = _rel(table["python"], component, "build.python")
        module = table.get("module")
        if not isinstance(module, str) or not _MODULE.fullmatch(module):
            raise _invalid(component, "build.module names the module python -m runs")
        return BuildRecipe(kind, package, module=module, paths=paths or (package,))
    if kind == "files":
        files = _table(table["files"], component, "build.files")
        if not files or len(files) > 64:
            raise _invalid(component, "build.files maps 1-64 paths into the image")
        pairs = tuple(
            sorted(
                (
                    _rel(src, component, "build.files source"),
                    _abs(dest, component, "build.files destination"),
                )
                for src, dest in files.items()
            )
        )
        return BuildRecipe(
            kind, files=pairs, paths=paths or tuple(src for src, _ in pairs)
        )
    dockerfile = _rel(table["dockerfile"], component, "build.dockerfile")
    context = _rel(table.get("context", "."), component, "build.context")
    target = table.get("target")
    if target is not None and (
        not isinstance(target, str) or not _LABEL.fullmatch(target)
    ):
        raise _invalid(component, "build.target must be a stage name")
    return BuildRecipe(
        kind,
        dockerfile,
        context=context,
        target=target,
        paths=paths or tuple(dict.fromkeys((context, dockerfile))),
    )


@dataclass(frozen=True)
class ImageSpec:
    """The runtime image: a base pinned by digest, the user, owned directories."""

    base: str | None = None
    digest: str | None = None
    uid: int = 65532
    gid: int = 65532
    dirs: tuple[tuple[str, str], ...] = ()
    cmd: tuple[str, ...] | None = None
    workdir: str | None = None

    @property
    def base_ref(self) -> str | None:
        return None if self.base is None else f"{self.base}@{self.digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base_ref,
            "user": f"{self.uid}:{self.gid}",
            "dirs": dict(self.dirs),
            "cmd": None if self.cmd is None else list(self.cmd),
            "workdir": self.workdir,
        }


def _image(value: Any, component: str) -> ImageSpec:
    table = _table(value, component, "image")
    _only(table, _IMAGE_KEYS, component, "image")
    base = digest = None
    if "base" in table:
        match = _BASE.fullmatch(str(table["base"]))
        if not isinstance(table["base"], str) or match is None:
            raise _invalid(
                component,
                "image.base must be pinned by digest: <image>@sha256:<64 hex>",
            )
        base, digest = match.group(1), match.group(2)
    user = table.get("user", 65532)
    if isinstance(user, bool):
        raise _invalid(component, "image.user is a uid or 'uid:gid'")
    match = _USER.fullmatch(str(user))
    if match is None:
        raise _invalid(component, "image.user is a uid or 'uid:gid'")
    uid = int(match.group(1))
    gid = int(match.group(2) or uid)
    dirs = _table(table.get("dirs", {}), component, "image.dirs")
    pairs: list[tuple[str, str]] = []
    for path, mode in dirs.items():
        if not isinstance(mode, str) or not _MODE.fullmatch(mode):
            raise _invalid(component, "image.dirs values are modes like '0700'")
        pairs.append((_abs(path, component, "image.dirs path"), mode.rjust(4, "0")))
    cmd = _argv(table["cmd"], component, "image.cmd") if "cmd" in table else None
    workdir = (
        _abs(table["workdir"], component, "image.workdir")
        if "workdir" in table
        else None
    )
    return ImageSpec(base, digest, uid, gid, tuple(sorted(pairs)), cmd, workdir)


@dataclass(frozen=True)
class Volume:
    """A volume of the component: retained data, or scratch space."""

    name: str
    path: str
    size: str | None = None
    retained: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "retained": self.retained,
        }


@dataclass(frozen=True)
class Need:
    """One ``needs`` entry: ``component:NAME``, ``secret:NAME``, ``NAME`` (a component)."""

    kind: str  # component | secret
    name: str
    optional: bool = False

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}{'?' if self.optional else ''}"


def _need(value: Any, component: str) -> Need:
    if not isinstance(value, str) or not value:
        raise _invalid(component, "needs holds strings")
    text, optional = (value[:-1], True) if value.endswith("?") else (value, False)
    kind, sep, name = text.partition(":")
    if not sep:
        kind, name = "component", text
    if kind not in {"component", "secret"} or not _LABEL.fullmatch(name):
        raise _invalid(
            component, f"need {value!r} is not NAME, component:NAME or secret:NAME"
        )
    return Need(kind, name, optional)


@dataclass(frozen=True)
class Health:
    """``ready``: an http or tcp probe, or a command; ``check``: a pre-rollout command."""

    ready: tuple[str, str] | None = None  # ("GET", "/path") | ("TCP", "port")
    ready_command: tuple[str, ...] | None = None
    check: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": None if self.ready is None else " ".join(self.ready),
            "ready_command": None
            if self.ready_command is None
            else list(self.ready_command),
            "check": None if self.check is None else list(self.check),
        }


def _health(value: Any, component: str, ports: Mapping[str, int]) -> Health:
    table = _table(value, component, "health")
    _only(table, _HEALTH_KEYS, component, "health")
    ready: tuple[str, str] | None = None
    ready_command: tuple[str, ...] | None = None
    if "ready" in table:
        item = table["ready"]
        if isinstance(item, str):
            match = _READY.fullmatch(item.strip())
            if match is None:
                raise _invalid(
                    component,
                    "health.ready is 'GET /path', 'TCP <port name>' or a command",
                )
            method, target = match.group(1), match.group(2)
            if not ports:
                raise _invalid(component, "health.ready needs a port in ports")
            if method == "GET" and not target.startswith("/"):
                raise _invalid(
                    component, "health.ready 'GET' takes a path starting '/'"
                )
            if method == "TCP" and target not in ports:
                raise _invalid(component, f"health.ready names no port {target!r}")
            ready = (method, target)
        else:
            ready_command = _argv(item, component, "health.ready")
    check = (
        _argv(table["check"], component, "health.check") if "check" in table else None
    )
    return Health(ready, ready_command, check)


@dataclass(frozen=True)
class ComponentContract:
    """One ``[component.<name>]`` table, validated."""

    name: str
    build: BuildRecipe | None
    image: ImageSpec = field(default_factory=ImageSpec)
    ports: tuple[tuple[str, int], ...] = ()
    health: Health = field(default_factory=Health)
    upgrade_check: tuple[str, ...] | None = None
    volumes: tuple[Volume, ...] = ()
    needs: tuple[Need, ...] = ()
    settings: tuple[tuple[str, str], ...] = ()
    emits: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "build": None if self.build is None else self.build.to_dict(),
            "image": self.image.to_dict(),
            "ports": dict(self.ports),
            "health": self.health.to_dict(),
            "upgrade_check": None
            if self.upgrade_check is None
            else list(self.upgrade_check),
            "volumes": {item.name: item.to_dict() for item in self.volumes},
            "needs": [str(item) for item in self.needs],
            "settings": dict(self.settings),
            "emits": list(self.emits),
        }

    def build_identity(self) -> dict[str, Any]:
        """What decides the image besides the source files: ``build`` and ``image``."""
        return {
            "build": None if self.build is None else self.build.to_dict(),
            "image": self.image.to_dict(),
        }

    @property
    def retained(self) -> tuple[Volume, ...]:
        return tuple(item for item in self.volumes if item.retained)


def parse_component(name: str, value: Any, *, built: bool = True) -> ComponentContract:
    """Validate one component table (``built=False``: a third-party image, no ``build``)."""
    table = _table(value, name, "the component")
    _only(table, _KEYS, name, "the component")
    recipe: BuildRecipe | None = None
    if built:
        if "build" not in table:
            raise _invalid(name, "build is required")
        recipe = _build(table["build"], name)
    elif "build" in table:
        raise _invalid(name, "a Component.image(...) has no build")
    image = _image(table.get("image", {}), name)
    if not built and (image.base or image.dirs):
        raise _invalid(name, "a Component.image(...) takes no image.base or image.dirs")
    if recipe is not None and recipe.kind in {"rust", "python"} and image.base is None:
        raise _invalid(name, "image.base (pinned by digest) is required for this build")
    ports_table = _table(table.get("ports", {}), name, "ports")
    ports: dict[str, int] = {}
    for port_name, number in ports_table.items():
        if not isinstance(port_name, str) or not _PORT_NAME.fullmatch(port_name):
            raise _invalid(
                name, f"port name {port_name!r} is not 1-15 lowercase characters"
            )
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 0 < number < 65536
        ):
            raise _invalid(name, f"port {port_name!r} is not 1-65535")
        ports[port_name] = number
    if len(set(ports.values())) != len(ports):
        raise _invalid(name, "two port names share one number")
    health = _health(table.get("health", {}), name, ports)
    upgrade = (
        _argv(table["upgrade_check"], name, "upgrade_check")
        if "upgrade_check" in table
        else None
    )
    volumes: list[Volume] = []
    for volume_name, item in _table(table.get("volumes", {}), name, "volumes").items():
        if not isinstance(volume_name, str) or not _LABEL.fullmatch(volume_name):
            raise _invalid(name, f"volume name {volume_name!r} is not a DNS label")
        spec = _table(item, name, f"volumes.{volume_name}")
        _only(spec, _VOLUME_KEYS, name, f"volumes.{volume_name}")
        if "path" not in spec:
            raise _invalid(name, f"volumes.{volume_name}.path is required")
        size = spec.get("size")
        if size is not None and (
            not isinstance(size, str) or not _SIZE.fullmatch(size)
        ):
            raise _invalid(name, f"volumes.{volume_name}.size is like 8Gi")
        retained = spec.get("retained", False)
        if not isinstance(retained, bool):
            raise _invalid(name, f"volumes.{volume_name}.retained is true or false")
        if retained and size is None:
            raise _invalid(
                name, f"volumes.{volume_name}: a retained volume needs a size"
            )
        volumes.append(
            Volume(
                volume_name,
                _abs(spec["path"], name, f"volumes.{volume_name}.path"),
                size,
                retained,
            )
        )
    if len({item.path for item in volumes}) != len(volumes):
        raise _invalid(name, "two volumes share one path")
    if upgrade is not None and not any(item.retained for item in volumes):
        raise _invalid(name, "upgrade_check needs a retained volume to open")
    needs_value = table.get("needs", [])
    if isinstance(needs_value, str) or not isinstance(needs_value, Sequence):
        raise _invalid(name, "needs must be a list")
    needs = tuple(dict.fromkeys(_need(item, name) for item in needs_value))
    settings_table = _table(table.get("settings", {}), name, "settings")
    for key, item in settings_table.items():
        if not isinstance(key, str) or not _SETTING.fullmatch(key):
            raise _invalid(name, f"setting name {key!r} is invalid")
        if not isinstance(item, str):
            raise _invalid(name, f"setting {key!r} must be a string")
    emits_value = table.get("emits", [])
    if (
        isinstance(emits_value, str)
        or not isinstance(emits_value, Sequence)
        or not all(
            isinstance(item, str) and _EMIT.fullmatch(item) for item in emits_value
        )
    ):
        raise _invalid(name, "emits must be a list like ['otlp:metrics']")
    return ComponentContract(
        name=name,
        build=recipe,
        image=image,
        ports=tuple(ports.items()),
        health=health,
        upgrade_check=upgrade,
        volumes=tuple(volumes),
        needs=needs,
        settings=tuple(sorted(settings_table.items())),
        emits=tuple(emits_value),
    )


def parse_contracts(text: str | bytes) -> dict[str, ComponentContract]:
    """Every ``[component.<name>]`` of a ``piceli.toml``.

    :raises CompositionError: ``component-contract-invalid`` for a malformed
        file, an unknown key or a bad value.
    """
    raw = text.encode() if isinstance(text, str) else text
    if len(raw) > MAX_CONTRACT_BYTES:
        raise CompositionError("component-contract-invalid", "piceli.toml is too large")
    try:
        document = tomllib.loads(raw.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise CompositionError(
            "component-contract-invalid", "piceli.toml is not valid TOML"
        ) from None
    unknown = sorted(set(document) - {"component"})
    if unknown:
        raise CompositionError(
            "component-contract-invalid",
            f"piceli.toml: unknown table {unknown[0]!r} (only [component.<name>])",
        )
    components = document.get("component", {})
    if not isinstance(components, Mapping):
        raise CompositionError(
            "component-contract-invalid", "piceli.toml: [component.<name>] tables"
        )
    found: dict[str, ComponentContract] = {}
    for name, value in components.items():
        if not _LABEL.fullmatch(name):
            raise _invalid(name, "the name is not a DNS label")
        found[name] = parse_component(name, value)
    return found


def image_contract(name: str, value: Mapping[str, Any]) -> ComponentContract:
    """The contract of a ``Component.image(...)`` (its ``contract=`` table)."""
    return parse_component(name, dict(value), built=False)


def contract_digest(contract: ComponentContract, trees: Mapping[str, str]) -> str:
    """The component's source digest: its build identity and the trees it reads.

    ``trees`` maps each path of ``build.paths`` to its Git object id at the
    environment's commit (``"absent"`` when the path does not exist). Two
    commits that leave those paths and the recipe unchanged give the same
    digest: the component is not rebuilt and its rollout is a no-op.
    """
    body = {
        "schema": "piceli.component-digest.v1",
        "identity": contract.build_identity(),
        "trees": dict(sorted(trees.items())),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()
