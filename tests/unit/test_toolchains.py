"""0.22: the toolchains a source pins, read and installed once into a cache.

Shared by host builds and development runs (:mod:`piceli.dev.toolchains`).
No network: downloads come from a fake opener, installers from a fake runner.
"""

from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import toolchains
from piceli.dev.toolchains import ToolchainError

NODE = "24.11.0"
NODE_ASSET = f"node-v{NODE}-linux-x64.tar.xz"
NODE_BASE = f"https://nodejs.org/dist/v{NODE}"
BINARYEN_ASSET = "binaryen-version_123-x86_64-linux.tar.gz"
BINARYEN_URL = (
    "https://github.com/WebAssembly/binaryen/releases/download/version_123/"
    + BINARYEN_ASSET
)
LOCK = """version = 4

[[package]]
name = "wasm-bindgen"
version = "0.2.114"

[[package]]
name = "wasm-bindgen-macro"
version = "0.2.114"
"""


def tarball(top: str, files: dict[str, str], mode: str, links: Any = ()) -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode=mode) as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(body.encode())
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(body.encode()))
        for name, target in links:
            info = tarfile.TarInfo(f"{top}/{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return data.getvalue()


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class Opener:
    """Serves fixed URLs; counts each request."""

    def __init__(self, bodies: dict[str, bytes], delay: float = 0.0) -> None:
        self.bodies = bodies
        self.calls: list[str] = []
        self.delay = delay

    def __call__(self, url: str) -> io.BytesIO:
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if url not in self.bodies:
            raise OSError(f"404 {url}")
        return io.BytesIO(self.bodies[url])


def releases(node_sha: str | None = None) -> Opener:
    node = tarball(
        f"node-v{NODE}-linux-x64",
        {"bin/node": "#!/bin/sh\necho v24.11.0\n", "lib/npm-cli.js": ""},
        "w:xz",
        [("bin/npm", "../lib/npm-cli.js")],
    )
    wasm_opt = tarball(
        "binaryen-version_123",
        {"bin/wasm-opt": "#!/bin/sh\necho 'wasm-opt version 123'\n"},
        "w:gz",
    )
    return Opener(
        {
            f"{NODE_BASE}/SHASUMS256.txt": (
                f"{'0' * 64}  node-v{NODE}-darwin-arm64.tar.xz\n"
                f"{node_sha or sha(node)}  {NODE_ASSET}\n"
            ).encode(),
            f"{NODE_BASE}/{NODE_ASSET}": node,
            BINARYEN_URL + ".sha256": f"{sha(wasm_opt)}  {BINARYEN_ASSET}\n".encode(),
            BINARYEN_URL: wasm_opt,
        }
    )


class Runner:
    """Stands in for rustup and cargo: records argv and env, creates bin/."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), dict(kwargs["env"])))
        if "install" in argv and "--root" in argv:
            root = Path(argv[argv.index("--root") + 1])
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "wasm-bindgen").write_text("#!/bin/sh\n")

        class Done:
            returncode = 0
            stdout = stderr = ""

        return Done()


def tools_on_path(tmp_path: Path) -> str:
    bin_dir = tmp_path / "host-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("rustup", "cargo"):
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexit 1\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return str(bin_dir)


# ------------------------------------------------------------------ parsing


def test_each_pin_kind_is_read_from_a_file_or_a_literal() -> None:
    files = {
        "app/rust-toolchain.toml": b'[toolchain]\nchannel = "1.97.0"\n'
        b'targets = ["wasm32-unknown-unknown", "x86_64-unknown-linux-gnu"]\n'
        b'components = ["rustfmt", "clippy"]\n',
        "app/.nvmrc": b"v24.11.0\n",
        "app/Cargo.lock": LOCK.encode(),
        "app/binaryen-version": b"version_123\n",
    }
    pins = toolchains.resolve(
        {
            "rust": "app/rust-toolchain.toml",
            "node": "app/.nvmrc",
            "wasm-bindgen": "app/Cargo.lock",
            "binaryen": "app/binaryen-version",
        },
        files.__getitem__,
    )
    assert pins == {
        "rust": {
            "file": "app/rust-toolchain.toml",
            "channel": "1.97.0",
            "targets": ["wasm32-unknown-unknown", "x86_64-unknown-linux-gnu"],
            "components": ["clippy", "rustfmt"],
            "profile": "minimal",
        },
        "node": {"version": "24.11.0", "file": "app/.nvmrc"},
        "binaryen": {"version": "123", "file": "app/binaryen-version"},
        "wasm-bindgen": {"version": "0.2.114", "file": "app/Cargo.lock"},
    }
    assert toolchains.versions(pins) == {
        "rust": "1.97.0",
        "node": "24.11.0",
        "binaryen": "123",
        "wasm-bindgen": "0.2.114",
    }

    def nothing(path: str) -> bytes:
        raise AssertionError(f"a literal reads no file ({path})")

    literal = toolchains.resolve(
        {"node": "v24.11.0", "binaryen": "123", "wasm-bindgen": "0.2.114"}, nothing
    )
    assert literal == {
        "node": {"version": "24.11.0", "file": None},
        "binaryen": {"version": "123", "file": None},
        "wasm-bindgen": {"version": "0.2.114", "file": None},
    }
    # A legacy one-line rust-toolchain and a .node-version.
    assert (
        toolchains.parse_rust("nightly-2026-10-01\n", "rust-toolchain", "x")["channel"]
        == "nightly-2026-10-01"
    )
    assert toolchains.parse_node("# pinned\n24.11.0\n", ".node-version") == "24.11.0"


@pytest.mark.parametrize(
    ("declared", "files", "names"),
    [
        ({"node": "app/.nvmrc"}, {"app/.nvmrc": b"lts/*\n"}, "app/.nvmrc"),
        ({"node": "app/.nvmrc"}, {"app/.nvmrc": b"24\n"}, "app/.nvmrc"),
        (
            {"wasm-bindgen": "app/Cargo.lock"},
            {"app/Cargo.lock": b'[[package]]\nname = "serde"\nversion = "1.0.0"\n'},
            "app/Cargo.lock",
        ),
        (
            {"wasm-bindgen": "app/Cargo.lock"},
            {
                "app/Cargo.lock": LOCK.encode()
                + b'\n[[package]]\nname = "wasm-bindgen"\nversion = "0.2.99"\n'
            },
            "app/Cargo.lock",
        ),
        ({"binaryen": "app/v"}, {"app/v": b"one-two-three"}, "app/v"),
        (
            {"rust": "app/rust-toolchain.toml"},
            {"app/rust-toolchain.toml": b'[toolchain]\nchannel = "1.97 ; rm"\n'},
            "app/rust-toolchain.toml",
        ),
    ],
)
def test_an_unreadable_pin_names_its_file(
    declared: dict[str, str], files: dict[str, bytes], names: str
) -> None:
    with pytest.raises(ToolchainError) as raised:
        toolchains.resolve(declared, files.__getitem__)
    assert raised.value.kind == "invalid"
    assert names in str(raised.value)


def test_a_declaration_names_known_tools_and_relative_paths() -> None:
    assert toolchains.check_declared({"node": "a/.nvmrc", "rust": "a/r.toml"}, "t") == {
        "rust": "a/r.toml",
        "node": "a/.nvmrc",
    }
    for bad in (
        {"python": "3.12"},
        {"node": "../.nvmrc"},
        {"node": "/etc/.nvmrc"},
        {"node": 24},
        {},
    ):
        with pytest.raises(ToolchainError) as raised:
            toolchains.check_declared(bad, "build.toolchains")
        assert raised.value.kind == "invalid"


# ------------------------------------------------------------------ provisioning


def test_every_tool_is_installed_once_and_selected(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    opener, runner = releases(), Runner()
    pins = toolchains.resolve(
        {"node": NODE, "binaryen": "123", "wasm-bindgen": "0.2.114"},
        lambda _p: b"",
    )
    pins["rust"] = toolchains.parse_rust(
        '[toolchain]\nchannel = "1.97.0"\ntargets = ["wasm32-unknown-unknown"]\n',
        "rust-toolchain.toml",
        "app/rust-toolchain.toml",
    )
    lines: list[str] = []
    env, paths, report = toolchains.provision(
        cache,
        pins,
        lines.append,
        env={"PATH": tools_on_path(tmp_path)},
        platform=("linux", "amd64"),
        opener=opener,
        run=runner,
    )
    root = cache / "toolchains"
    assert env == {"RUSTUP_HOME": str(root / "rustup"), "RUSTUP_TOOLCHAIN": "1.97.0"}
    assert paths == [
        str(root / "node" / "24.11.0-linux-amd64" / "bin"),
        str(root / "binaryen" / "123-linux-amd64" / "bin"),
        str(root / "wasm-bindgen" / "0.2.114-linux-amd64" / "bin"),
    ]
    assert (Path(paths[0]) / "node").read_text().startswith("#!/bin/sh")
    assert (Path(paths[0]) / "npm").is_symlink()
    assert (Path(paths[1]) / "wasm-opt").is_file()
    assert (Path(paths[2]) / "wasm-bindgen").is_file()
    assert [item for item in lines if item.startswith("installing")] == [
        "installing Rust 1.97.0 (app/rust-toolchain.toml)",
        "installing node 24.11.0 (literal)",
        "installing binaryen 123 (literal)",
        "installing wasm-bindgen-cli 0.2.114 (literal)",
    ]
    rustup, cargo = runner.calls
    assert rustup[0][1:4] == ["toolchain", "install", "1.97.0"]
    assert rustup[1]["RUSTUP_HOME"] == str(root / "rustup")
    assert "RUSTUP_TOOLCHAIN" not in rustup[1]
    # wasm-bindgen-cli is built with the pinned Rust, into its own root.
    assert cargo[0][1:] == [
        "install", "--locked", "--root", cargo[0][4],
        "wasm-bindgen-cli", "--version", "0.2.114",
    ]  # fmt: skip
    assert cargo[1]["RUSTUP_TOOLCHAIN"] == "1.97.0"
    assert report["node"]["url"] == f"{NODE_BASE}/{NODE_ASSET}"
    assert len(report["node"]["sha256"]) == 64
    assert report["binaryen"]["url"] == BINARYEN_URL
    assert all(item["installed"] for item in report.values())
    # Next time: done-markers, no download, no installer.
    downloads = len(opener.calls)
    env_again, paths_again, again = toolchains.provision(
        cache,
        pins,
        lines.append,
        env={"PATH": tools_on_path(tmp_path)},
        platform=("linux", "amd64"),
        opener=opener,
        run=runner,
    )
    assert (env_again, paths_again) == (env, paths)
    assert len(opener.calls) == downloads and len(runner.calls) == 2
    assert not any(item["installed"] for item in again.values())
    assert again["node"]["sha256"] == report["node"]["sha256"]  # from the marker
    assert sorted(path.name for path in root.glob("*.done")) == sorted(
        [
            f"rust-{toolchains._key(['1.97.0', ['wasm32-unknown-unknown'], [], 'minimal'])}.done",
            "node-24.11.0-linux-amd64.done",
            "binaryen-123-linux-amd64.done",
            "wasm-bindgen-0.2.114-linux-amd64.done",
        ]
    )


def test_a_download_that_does_not_match_its_checksum_is_refused(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    with pytest.raises(ToolchainError) as raised:
        toolchains.provision(
            cache,
            {"node": {"version": NODE, "file": None}},
            lambda _l: None,
            platform=("linux", "amd64"),
            opener=releases(node_sha="f" * 64),
        )
    assert raised.value.kind == "checksum"
    assert NODE_ASSET in str(raised.value) and "f" * 64 in str(raised.value)
    root = cache / "toolchains"
    assert not list(root.glob("*.done"))
    assert not (root / "node" / "24.11.0-linux-amd64").exists()
    assert [p.name for p in (root / "node").iterdir()] == []  # no scratch left


def test_two_concurrent_provisioners_install_once(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    opener = releases()
    opener.delay = 0.05
    reports: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def provision() -> None:
        try:
            _, _, report = toolchains.provision(
                cache,
                {"binaryen": {"version": "123", "file": None}},
                lambda _l: None,
                platform=("linux", "amd64"),
                opener=opener,
            )
            reports.append(report["binaryen"])
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=provision) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert opener.calls.count(BINARYEN_URL) == 1
    assert sorted(item["installed"] for item in reports) == [False, True]


def test_a_tarball_member_leaving_its_directory_is_refused(tmp_path: Path) -> None:
    evil = tarball("binaryen-version_123", {"../../escape": "x"}, "w:gz")
    opener = Opener(
        {
            BINARYEN_URL + ".sha256": f"{sha(evil)}  {BINARYEN_ASSET}\n".encode(),
            BINARYEN_URL: evil,
        }
    )
    with pytest.raises(ToolchainError) as raised:
        toolchains.provision(
            tmp_path / "cache",
            {"binaryen": {"version": "123", "file": None}},
            lambda _l: None,
            platform=("linux", "amd64"),
            opener=opener,
        )
    assert raised.value.kind == "failed"
    assert not (tmp_path / "escape").exists()


def test_a_failed_install_and_a_missing_release_say_why(tmp_path: Path) -> None:
    class Failing(Runner):
        def __call__(self, argv: list[str], **kwargs: Any) -> Any:
            class Done:
                returncode = 101
                stdout = ""
                stderr = "error: could not find `wasm-bindgen-cli` 9.9.9\n"

            return Done()

    with pytest.raises(ToolchainError) as raised:
        toolchains.provision(
            tmp_path / "cache",
            {"wasm-bindgen": {"version": "9.9.9", "file": None}},
            lambda _l: None,
            env={"PATH": tools_on_path(tmp_path)},
            platform=("linux", "amd64"),
            run=Failing(),
        )
    assert raised.value.kind == "failed" and "9.9.9" in str(raised.value)
    with pytest.raises(ToolchainError) as raised:
        toolchains.provision(
            tmp_path / "cache",
            {"node": {"version": "99.0.0", "file": None}},
            lambda _l: None,
            platform=("linux", "amd64"),
            opener=Opener({}),
        )
    assert raised.value.kind == "failed" and "99.0.0" in str(raised.value)


def test_assets_follow_the_machine_that_runs_the_commands() -> None:
    assert toolchains.node_asset(NODE, "linux", "arm64") == (
        "node-v24.11.0-linux-arm64.tar.xz"
    )
    assert toolchains.node_asset(NODE, "darwin", "arm64") == (
        "node-v24.11.0-darwin-arm64.tar.xz"
    )
    assert toolchains.binaryen_asset("123", "linux", "arm64") == (
        "binaryen-version_123-aarch64-linux.tar.gz"
    )
    assert toolchains.binaryen_asset("123", "darwin", "arm64") == (
        "binaryen-version_123-arm64-macos.tar.gz"
    )
