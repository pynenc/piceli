"""The toolchains a source pins, read and installed once into a cache (0.22.0).

Standard library only and self-contained: development runs ship this file
into their pod next to :mod:`piceli.dev.pod` (:func:`piceli.dev.jobs.wrapper_source`),
and host builds (:mod:`piceli.artifacts.host_toolchains`) import it.

Four tools, each pinned by a file of the source or by a literal version:

* ``rust``: a ``rust-toolchain.toml`` (or legacy ``rust-toolchain``):
  channel, targets, components, profile. Installed with ``rustup toolchain
  install`` into ``<cache>/toolchains/rustup``.
* ``node``: a ``.nvmrc`` / ``.node-version`` file or a version
  (``24.11.0``). The official ``nodejs.org`` tarball, checked against that
  release's ``SHASUMS256.txt``.
* ``binaryen``: a file holding a version or a version (``123``). The
  official GitHub release tarball, checked against its published
  ``.sha256``.
* ``wasm-bindgen``: the ``wasm-bindgen`` version of a ``Cargo.lock`` or a
  version (``0.2.114``). ``cargo install --locked wasm-bindgen-cli``.

Each is installed once per cache, keyed by its version (and for Rust the
parsed pin), under a file lock so concurrent installs do not race, with a
done-marker so the next use skips it::

    <cache>/toolchains/rustup/                    RUSTUP_HOME
    <cache>/toolchains/rust-<key>.done
    <cache>/toolchains/node/<version>-<os>-<arch>/bin
    <cache>/toolchains/binaryen/<version>-<os>-<arch>/bin
    <cache>/toolchains/wasm-bindgen/<version>-<os>-<arch>/bin
    <cache>/toolchains/<tool>-<version>-<os>-<arch>.done

Errors are :class:`ToolchainError` with a ``kind``: ``invalid`` (a pin that
cannot be read), ``checksum`` (a download that does not match its published
checksum) or ``failed`` (an install that failed). Callers map the kind to
their own fixed error code.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform as _platform
import posixpath
import re
import shutil
import subprocess
import tarfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

#: The tools a pin may name, in the order they are installed (wasm-bindgen
#: is built with the pinned Rust).
TOOLS = ("rust", "node", "binaryen", "wasm-bindgen")
#: A download larger than this is refused (a wrong URL, not a toolchain).
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
#: A pin file larger than this is refused.
MAX_PIN_BYTES = 4 * 1024 * 1024
NODE_DIST = "https://nodejs.org/dist"
BINARYEN_RELEASES = "https://github.com/WebAssembly/binaryen/releases/download"

_CHANNEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ITEM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SEMVER = re.compile(r"v?([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})")
_BINARYEN = re.compile(r"(?:version_)?([0-9]{1,4})")
_PATH = re.compile(r"[A-Za-z0-9._@+-][A-Za-z0-9._@+/-]{0,511}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

Opener = Callable[[str], Any]
Runner = Callable[..., Any]


class ToolchainError(Exception):
    """A pin or an install failed; ``kind`` is ``invalid``, ``checksum`` or ``failed``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------------ declarations


def is_literal(tool: str, value: str) -> bool:
    """``value`` is a version, not a path (``node = "24.11.0"``)."""
    if tool == "rust":
        return False
    if tool == "binaryen":
        return _BINARYEN.fullmatch(value) is not None
    return _SEMVER.fullmatch(value) is not None


def check_declared(declared: Mapping[str, Any], where: str) -> dict[str, str]:
    """Validate a ``{tool: path or version}`` table; returns it as plain strings.

    Paths are relative (no ``..``, no absolute path).

    :raises ToolchainError: ``invalid``.
    """
    if not isinstance(declared, Mapping) or not declared:
        raise ToolchainError("invalid", f"{where} must be a table of {list(TOOLS)}")
    found: dict[str, str] = {}
    for tool, value in declared.items():
        if tool not in TOOLS:
            raise ToolchainError(
                "invalid", f"{where}: unknown tool {tool!r}; known: {list(TOOLS)}"
            )
        if not isinstance(value, str) or not value:
            raise ToolchainError("invalid", f"{where}.{tool} must be a string")
        if not is_literal(tool, value):
            normal = posixpath.normpath(value)
            if (
                not _PATH.fullmatch(value)
                or normal != value.rstrip("/")
                or normal.startswith("..")
                or normal in {".", ""}
            ):
                raise ToolchainError(
                    "invalid", f"{where}.{tool}: {value!r} is not a relative file path"
                )
        found[str(tool)] = value
    return {tool: found[tool] for tool in TOOLS if tool in found}


# ------------------------------------------------------------------ parsing


def parse_rust(text: str, name: str, where: str) -> dict[str, Any]:
    """A ``rust-toolchain.toml`` (or legacy one-line ``rust-toolchain``).

    :raises ToolchainError: ``invalid``, naming ``where``.
    """
    if not name.endswith(".toml") and "[toolchain]" not in text:
        table: dict[str, Any] = {"channel": text.strip()}
    else:
        try:
            import tomllib

            table = dict(tomllib.loads(text).get("toolchain") or {})
        except Exception as error:
            raise ToolchainError(
                "invalid", f"{where}: not valid TOML ({error})"
            ) from None
    channel = str(table.get("channel") or "")
    if not _CHANNEL.fullmatch(channel):
        raise ToolchainError("invalid", f"{where}: [toolchain] channel {channel!r}")
    found: dict[str, Any] = {"file": where, "channel": channel}
    for key in ("targets", "components"):
        items = [str(item) for item in table.get(key) or ()]
        if not all(_ITEM.fullmatch(item) for item in items):
            raise ToolchainError("invalid", f"{where}: [toolchain] {key}")
        found[key] = sorted(items)
    profile = str(table.get("profile") or "minimal")
    found["profile"] = (
        profile if profile in {"minimal", "default", "complete"} else "minimal"
    )
    return found


def parse_node(text: str, where: str) -> str:
    """The exact version of a ``.nvmrc`` / ``.node-version`` (``v24.11.0``)."""
    lines = [
        line.split("#", 1)[0].strip() for line in text.splitlines() if line.strip()
    ]
    value = next((line for line in lines if line), "")
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ToolchainError(
            "invalid",
            f"{where}: node {value[:40]!r} is not an exact version (24.11.0); "
            "aliases and majors alone are not reproducible",
        )
    return match.group(1)


def parse_binaryen(text: str, where: str) -> str:
    """A binaryen version (``123`` or ``version_123``)."""
    value = text.strip()
    match = _BINARYEN.fullmatch(value)
    if match is None:
        raise ToolchainError(
            "invalid", f"{where}: binaryen {value[:40]!r} is not a version (123)"
        )
    return str(int(match.group(1)))


def parse_wasm_bindgen(text: str, where: str) -> str:
    """The ``wasm-bindgen`` version a ``Cargo.lock`` pins (exactly one)."""
    try:
        import tomllib

        packages = tomllib.loads(text).get("package") or []
    except Exception as error:
        raise ToolchainError("invalid", f"{where}: not valid TOML ({error})") from None
    versions = sorted(
        {
            str(item.get("version"))
            for item in packages
            if isinstance(item, dict) and item.get("name") == "wasm-bindgen"
        }
    )
    if len(versions) != 1 or _SEMVER.fullmatch(versions[0]) is None:
        raise ToolchainError(
            "invalid",
            f"{where}: expected one wasm-bindgen version, found {versions[:4]}",
        )
    return versions[0]


def resolve(
    declared: Mapping[str, str], read: Callable[[str], bytes]
) -> dict[str, dict[str, Any]]:
    """Each declared tool's pin: ``{tool: {"version"|"channel"…, "file"}}``.

    ``read(path)`` returns a pin file's bytes (raising :class:`ToolchainError`
    when it is missing); literal versions read nothing.
    """
    pins: dict[str, dict[str, Any]] = {}
    for tool in TOOLS:
        value = declared.get(tool)
        if value is None:
            continue
        if is_literal(tool, value):
            text, where = value, None
        else:
            raw = read(value)
            if len(raw) > MAX_PIN_BYTES:
                raise ToolchainError("invalid", f"{value}: larger than a pin file")
            text, where = raw.decode("utf-8", "replace"), value
        if tool == "rust":
            pins[tool] = parse_rust(text, posixpath.basename(value), value)
            continue
        label = where or "literal"
        if tool == "node":
            version = parse_node(text, label)
        elif tool == "binaryen":
            version = parse_binaryen(text, label)
        else:
            version = (
                parse_wasm_bindgen(text, label)
                if where is not None
                else _SEMVER.fullmatch(value).group(1)  # type: ignore[union-attr]
            )
        pins[tool] = {"version": version, "file": where}
    return pins


def versions(pins: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """``{tool: version}`` (Rust: its channel)."""
    return {
        tool: str(pin.get("version") or pin.get("channel"))
        for tool, pin in pins.items()
    }


# ------------------------------------------------------------------ platform


def host_platform() -> tuple[str, str]:
    """The machine that runs the commands: ``(linux|darwin, amd64|arm64)``."""
    system = _platform.system().lower()
    machine = _platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return system, arch


def node_asset(version: str, system: str, arch: str) -> str:
    names = {"amd64": "x64", "arm64": "arm64"}
    if system not in {"linux", "darwin"} or arch not in names:
        raise ToolchainError("failed", f"node has no build for {system}/{arch}")
    return f"node-v{version}-{system}-{names[arch]}.tar.xz"


def binaryen_asset(version: str, system: str, arch: str) -> str:
    if system == "linux":
        names = {"amd64": "x86_64", "arm64": "aarch64"}
        label = "linux"
    elif system == "darwin":
        names = {"amd64": "x86_64", "arm64": "arm64"}
        label = "macos"
    else:
        names, label = {}, system
    if arch not in names:
        raise ToolchainError("failed", f"binaryen has no build for {system}/{arch}")
    return f"binaryen-version_{version}-{names[arch]}-{label}.tar.gz"


# ------------------------------------------------------------------ once


def _once(
    cache: Path,
    name: str,
    key: str,
    install: Callable[[], dict[str, Any]],
    present: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Run ``install`` once per ``(name, key)``; returns (installed here, marker).

    The done-marker ``<cache>/toolchains/<name>-<key>.done`` holds what
    ``install`` returned; the lock ``<cache>/toolchains/.<name>.lock``
    serialises installers of one tool.
    """
    root = cache / "toolchains"
    root.mkdir(parents=True, exist_ok=True)
    done = root / f"{name}-{key}.done"

    def finished() -> dict[str, Any] | None:
        if not done.is_file() or (present is not None and not present.is_dir()):
            return None
        try:
            value = json.loads(done.read_text())
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    marker = finished()
    if marker is not None:
        return False, marker
    lock = os.open(root / f".{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = finished()
        if marker is not None:
            return False, marker
        marker = install()
        partial = done.with_name(done.name + f".{os.getpid()}")
        partial.write_text(json.dumps(marker, sort_keys=True))
        os.replace(partial, done)
        return True, marker
    finally:
        os.close(lock)


def _key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ downloads


def _run(*args: Any, **kwargs: Any) -> Any:
    return subprocess.run(*args, **kwargs)


def _open_url(url: str) -> Any:
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "piceli"})
    return urllib.request.urlopen(request, timeout=120)


def _fetch(
    opener: Opener | None, url: str, into: Path | None = None
) -> tuple[bytes, str]:
    """``url``'s body (or written to ``into``) and its sha256."""
    opener = opener or _open_url
    hasher = hashlib.sha256()
    size = 0
    kept = bytearray()
    try:
        with opener(url) as response:
            sink = into.open("wb") if into is not None else None
            try:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ToolchainError("failed", f"{url}: larger than expected")
                    hasher.update(block)
                    if sink is not None:
                        sink.write(block)
                    else:
                        kept += block
            finally:
                if sink is not None:
                    sink.close()
    except ToolchainError:
        raise
    except Exception as error:
        raise ToolchainError("failed", f"cannot download {url} ({error})") from None
    return bytes(kept), hasher.hexdigest()


def _extract(archive: Path, into: Path) -> None:
    """Extract ``archive`` into ``into`` without its top directory; no member
    may leave ``into`` (absolute paths, ``..``, escaping links)."""
    with tarfile.open(archive) as tar:
        members = []
        for member in tar.getmembers():
            parts = [p for p in member.name.split("/") if p not in ("", ".")]
            if len(parts) < 2:
                continue
            name = "/".join(parts[1:])
            if member.name.startswith("/") or ".." in parts:
                raise ToolchainError("failed", f"{archive.name}: unsafe member")
            if member.issym():
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), member.linkname)
                )
                if member.linkname.startswith("/") or target.startswith(".."):
                    raise ToolchainError("failed", f"{archive.name}: unsafe link")
            elif member.islnk():
                link = [p for p in member.linkname.split("/") if p not in ("", ".")]
                if member.linkname.startswith("/") or ".." in link or len(link) < 2:
                    raise ToolchainError("failed", f"{archive.name}: unsafe link")
                member.linkname = "/".join(link[1:])
            elif not (member.isfile() or member.isdir()):
                continue
            member.name = name
            members.append(member)
        extra: dict[str, Any] = (
            {"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}
        )
        tar.extractall(into, members=members, **extra)


def _install_tarball(
    cache: Path,
    tool: str,
    version: str,
    system: str,
    arch: str,
    url: str,
    expected: str,
    opener: Opener | None,
) -> dict[str, Any]:
    final = cache / "toolchains" / tool / f"{version}-{system}-{arch}"
    scratch = final.with_name(f".{final.name}.{os.getpid()}")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        archive = scratch / "archive"
        _, got = _fetch(opener, url, archive)
        if got != expected:
            raise ToolchainError(
                "checksum",
                f"{posixpath.basename(url)}: sha256 {got} does not match the "
                f"published {expected}",
            )
        tree = scratch / "tree"
        tree.mkdir()
        _extract(archive, tree)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tree, final)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"version": version, "url": url, "sha256": expected}


def provision_node(
    cache: Path,
    pin: Mapping[str, Any],
    emit: Callable[[str], None],
    *,
    system: str,
    arch: str,
    opener: Opener | None = None,
) -> tuple[bool, dict[str, Any], Path]:
    version = str(pin["version"])
    asset = node_asset(version, system, arch)
    final = cache / "toolchains" / "node" / f"{version}-{system}-{arch}"

    def install() -> dict[str, Any]:
        emit(f"installing node {version} ({pin.get('file') or 'literal'})")
        base = f"{NODE_DIST}/v{version}"
        sums, _ = _fetch(opener, f"{base}/SHASUMS256.txt")
        expected = None
        for line in sums.decode("utf-8", "replace").splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == asset:
                expected = fields[0].lower()
        if expected is None or not _SHA256.fullmatch(expected):
            raise ToolchainError(
                "failed", f"node {version}: no {asset} in its SHASUMS256.txt"
            )
        return _install_tarball(
            cache, "node", version, system, arch, f"{base}/{asset}", expected, opener
        )

    installed, marker = _once(
        cache, "node", f"{version}-{system}-{arch}", install, final
    )
    return installed, marker, final / "bin"


def provision_binaryen(
    cache: Path,
    pin: Mapping[str, Any],
    emit: Callable[[str], None],
    *,
    system: str,
    arch: str,
    opener: Opener | None = None,
) -> tuple[bool, dict[str, Any], Path]:
    version = str(pin["version"])
    asset = binaryen_asset(version, system, arch)
    final = cache / "toolchains" / "binaryen" / f"{version}-{system}-{arch}"

    def install() -> dict[str, Any]:
        emit(f"installing binaryen {version} ({pin.get('file') or 'literal'})")
        url = f"{BINARYEN_RELEASES}/version_{version}/{asset}"
        published, _ = _fetch(opener, url + ".sha256")
        fields = published.decode("utf-8", "replace").split()
        expected = fields[0].lower() if fields else ""
        if not _SHA256.fullmatch(expected):
            raise ToolchainError(
                "failed", f"binaryen {version}: {asset}.sha256 holds no sha256"
            )
        return _install_tarball(
            cache, "binaryen", version, system, arch, url, expected, opener
        )

    installed, marker = _once(
        cache, "binaryen", f"{version}-{system}-{arch}", install, final
    )
    return installed, marker, final / "bin"


def _tail(completed: Any) -> str:
    text = (completed.stderr or completed.stdout or "").strip().splitlines()[-5:]
    return " | ".join(text)[:600]


def provision_wasm_bindgen(
    cache: Path,
    pin: Mapping[str, Any],
    emit: Callable[[str], None],
    *,
    system: str,
    arch: str,
    env: Mapping[str, str],
    run: Runner | None = None,
) -> tuple[bool, dict[str, Any], Path]:
    version = str(pin["version"])
    final = cache / "toolchains" / "wasm-bindgen" / f"{version}-{system}-{arch}"

    def install() -> dict[str, Any]:
        emit(f"installing wasm-bindgen-cli {version} ({pin.get('file') or 'literal'})")
        cargo = shutil.which("cargo", path=env.get("PATH"))
        if cargo is None:
            raise ToolchainError(
                "failed", f"wasm-bindgen-cli {version}: cargo is not on PATH"
            )
        scratch = final.with_name(f".{final.name}.{os.getpid()}")
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        try:
            completed = (run or _run)(
                [
                    cargo,
                    "install",
                    "--locked",
                    "--root",
                    str(scratch / "root"),
                    "wasm-bindgen-cli",
                    "--version",
                    version,
                ],
                env={**env, "CARGO_TARGET_DIR": str(scratch / "target")},
                capture_output=True,
                text=True,
                timeout=3600,
                check=False,
            )
            if completed.returncode != 0:
                raise ToolchainError(
                    "failed",
                    f"cargo could not install wasm-bindgen-cli {version}: "
                    + _tail(completed),
                )
            if not (scratch / "root" / "bin").is_dir():
                raise ToolchainError(
                    "failed", f"wasm-bindgen-cli {version}: cargo installed no bin/"
                )
            shutil.rmtree(final, ignore_errors=True)
            os.replace(scratch / "root", final)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return {"version": version, "crate": "wasm-bindgen-cli"}

    installed, marker = _once(
        cache, "wasm-bindgen", f"{version}-{system}-{arch}", install, final
    )
    return installed, marker, final / "bin"


def provision_rust(
    cache: Path,
    pin: Mapping[str, Any],
    emit: Callable[[str], None],
    *,
    env: Mapping[str, str] | None = None,
    run: Runner | None = None,
) -> tuple[bool, dict[str, str]]:
    """Install the pinned toolchain once into ``<cache>/toolchains/rustup``
    (``rustup``, keyed by channel, targets, components and profile); returns
    whether it ran and the environment that selects it."""
    home = cache / "toolchains" / "rustup"
    home.mkdir(parents=True, exist_ok=True)
    selected = {"RUSTUP_HOME": str(home), "RUSTUP_TOOLCHAIN": str(pin["channel"])}
    key = _key([pin["channel"], pin["targets"], pin["components"], pin["profile"]])
    base = dict(os.environ if env is None else env)

    def install() -> dict[str, Any]:
        emit(f"installing Rust {pin['channel']} ({pin['file']})")
        rustup = shutil.which("rustup", path=base.get("PATH"))
        if rustup is None:
            raise ToolchainError(
                "failed",
                f"rustup is not on PATH to install {pin['channel']} ({pin['file']})",
            )
        command = [
            rustup, "toolchain", "install", str(pin["channel"]),
            "--profile", str(pin["profile"]), "--no-self-update",
        ]  # fmt: skip
        for item in pin["components"]:
            command += ["--component", item]
        for item in pin["targets"]:
            command += ["--target", item]
        completed = (run or _run)(
            command,
            env={
                k: v for k, v in {**base, **selected}.items() if k != "RUSTUP_TOOLCHAIN"
            },
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if completed.returncode != 0:
            raise ToolchainError(
                "failed",
                f"rustup could not install {pin['channel']} ({pin['file']}): "
                + _tail(completed),
            )
        return dict(pin)

    installed, _ = _once(cache, "rust", key, install)
    return installed, selected


def provision(
    cache: Path,
    pins: Mapping[str, Mapping[str, Any]],
    emit: Callable[[str], None],
    *,
    env: Mapping[str, str] | None = None,
    platform: tuple[str, str] | None = None,
    opener: Opener | None = None,
    run: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, str], list[str], dict[str, dict[str, Any]]]:
    """Install every pinned tool once; returns the environment to set
    (``RUSTUP_HOME``, ``RUSTUP_TOOLCHAIN``), the ``bin`` directories to put
    first on ``PATH`` and a report per tool (its pin, whether this call
    installed it, seconds, and the download's URL and sha256).

    ``env`` is the commands' environment (``PATH`` finds ``rustup`` and
    ``cargo``); ``platform`` is ``(os, arch)`` of the machine that runs the
    commands (default: this one).
    """
    system, arch = platform or host_platform()
    base = dict(os.environ if env is None else env)
    selected: dict[str, str] = {}
    paths: list[str] = []
    report: dict[str, dict[str, Any]] = {}
    for tool in TOOLS:
        pin = pins.get(tool)
        if pin is None:
            continue
        began = clock()
        marker: dict[str, Any] = {}
        if tool == "rust":
            installed, selected = provision_rust(cache, pin, emit, env=base, run=run)
        else:
            if tool == "node":
                installed, marker, bin_dir = provision_node(
                    cache, pin, emit, system=system, arch=arch, opener=opener
                )
            elif tool == "binaryen":
                installed, marker, bin_dir = provision_binaryen(
                    cache, pin, emit, system=system, arch=arch, opener=opener
                )
            else:
                installed, marker, bin_dir = provision_wasm_bindgen(
                    cache,
                    pin,
                    emit,
                    system=system,
                    arch=arch,
                    env={**base, **selected},
                    run=run,
                )
            paths.append(str(bin_dir))
        report[tool] = {
            **pin,
            **{k: marker[k] for k in ("url", "sha256") if k in marker},
            "installed": installed,
            "seconds": round(clock() - began, 3),
        }
    return selected, paths, report
