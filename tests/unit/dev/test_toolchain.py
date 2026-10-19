"""0.20: the run's Rust toolchain is the one the source pins.

The wrapper reads the nearest ``rust-toolchain.toml`` (or legacy
``rust-toolchain``) and installs that toolchain once into the cache with
``rustup`` (a fake one here), instead of using the image's Rust.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from piceli.dev.pod import RunError, provision_rust, rust_toolchain

PIN = """[toolchain]
channel = "1.97.0"
targets = ["wasm32-unknown-unknown"]
components = ["rustfmt", "clippy"]
"""


def test_the_nearest_pin_from_the_run_directory_up_counts(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "shop" / "crates" / "poet").mkdir(parents=True)
    (tree / "shop" / "rust-toolchain.toml").write_text(PIN)
    found = rust_toolchain(tree, "shop/crates/poet")
    assert found == {
        "file": "shop/rust-toolchain.toml",
        "channel": "1.97.0",
        "targets": ["wasm32-unknown-unknown"],
        "components": ["clippy", "rustfmt"],
        "profile": "minimal",
    }
    (tree / "shop" / "crates" / "poet" / "rust-toolchain").write_text(
        "nightly-2026-10-01\n"
    )
    assert rust_toolchain(tree, "shop/crates/poet")["channel"] == "nightly-2026-10-01"
    assert rust_toolchain(tree, ".") is None  # above the pin: none


def test_a_bad_pin_names_its_file(tmp_path: Path) -> None:
    (tmp_path / "rust-toolchain.toml").write_text("[toolchain\nchannel=")
    with pytest.raises(RunError) as raised:
        rust_toolchain(tmp_path, ".")
    assert raised.value.reason == "dev-toolchain-invalid"
    assert "rust-toolchain.toml" in str(raised.value)
    (tmp_path / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.97 ; rm"\n'
    )
    with pytest.raises(RunError):
        rust_toolchain(tmp_path, ".")


def _fake_rustup(bin_dir: Path, log: Path, fail: bool = False) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "rustup"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s\\n" "$RUSTUP_HOME" "$*" >> "{log}"\n'
        + ("echo 'error: no such toolchain' >&2; exit 1\n" if fail else "exit 0\n")
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def test_the_pinned_toolchain_is_installed_once_into_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "rustup.log"
    _fake_rustup(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    (tmp_path / "rust-toolchain.toml").write_text(PIN)
    pin = rust_toolchain(tmp_path, ".")
    assert pin is not None
    cache = tmp_path / "cache"
    lines: list[str] = []
    env, report = provision_rust(cache, pin, lines.append)
    assert env == {
        "RUSTUP_HOME": str(cache / "toolchains" / "rustup"),
        "RUSTUP_TOOLCHAIN": "1.97.0",
    }
    assert report["installed"] is True and lines == [
        "[piceli-dev] installing Rust 1.97.0 (rust-toolchain.toml)"
    ]
    home, args = log.read_text().splitlines()[0].split("|")
    assert home == str(cache / "toolchains" / "rustup")
    assert args == (
        "toolchain install 1.97.0 --profile minimal --no-self-update "
        "--component clippy --component rustfmt --target wasm32-unknown-unknown"
    )
    again_env, again = provision_rust(cache, pin, lines.append)
    assert again_env == env and again["installed"] is False
    assert len(log.read_text().splitlines()) == 1  # cached: rustup not run again


def test_a_failed_install_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_rustup(tmp_path / "bin", tmp_path / "log", fail=True)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    (tmp_path / "rust-toolchain.toml").write_text(PIN)
    with pytest.raises(RunError) as raised:
        provision_rust(
            tmp_path / "cache", rust_toolchain(tmp_path, ".") or {}, lambda _l: None
        )
    assert raised.value.reason == "dev-toolchain-failed"
    assert "no such toolchain" in str(raised.value)
    assert not list((tmp_path / "cache" / "toolchains").glob("*.done"))
    assert json is not None


# --------------------------------------------- 0.22: the profile's other tools


def test_a_profile_pins_node_binaryen_and_wasm_bindgen() -> None:
    from piceli.dev import jobs
    from piceli.dev.model import DevBuilds, DevProfile
    from piceli.infra.cluster import ClusterError

    profile = DevProfile(
        "web", toolchains={"wasm-bindgen": "shop/Cargo.lock", "node": "shop/.nvmrc"}
    )
    assert profile.toolchains == {
        "node": "shop/.nvmrc",
        "wasm-bindgen": "shop/Cargo.lock",
    }
    dev = DevBuilds(
        node="builder-1",
        image="ghcr.io/example/builder@sha256:" + "a" * 64,
        profiles=[profile],
    )
    assert DevBuilds.from_dict(dev.describe()).profile("web").toolchains == (
        profile.toolchains
    )
    job = jobs.run_job(
        dev, dev.profile("web"), run="r1", spec={}, requester="me", priority="agent"
    )
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    spec = json.loads(next(e["value"] for e in env if e["name"] == "PICELI_DEV_SPEC"))
    assert spec["toolchains"] == profile.toolchains
    # Without pins the profile and the run spec are as before.
    assert "toolchains" not in DevProfile("plain").describe()
    for bad in ({"rust": "rust-toolchain.toml"}, {"node": "../.nvmrc"}, {"go": "1"}):
        with pytest.raises(ClusterError):
            DevProfile("web", toolchains=bad)


def test_a_run_installs_the_profiles_tools_and_puts_them_first_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.dev import toolchains
    from tests.unit.dev.test_pod import _archive, _run, _spec
    from tests.unit.test_toolchains import releases

    opener = releases()
    monkeypatch.setattr(toolchains, "_open_url", opener)
    monkeypatch.setattr(toolchains, "host_platform", lambda: ("linux", "amd64"))
    data = _archive({"app/a.txt": "x", "app/.nvmrc": "24.11.0\n"})
    spec = _spec(
        data,
        ["sh", "-c", "node; wasm-opt"],
        toolchains={"node": "app/.nvmrc", "binaryen": "123"},
        isolation_check=False,
    )
    result, lines = _run(tmp_path, data, spec)
    assert result["state"] == "passed", result
    assert "v24.11.0" in lines and "wasm-opt version 123" in lines
    assert result["toolchain"]["node"]["version"] == "24.11.0"
    assert result["toolchain"]["binaryen"]["installed"] is True
    assert "[piceli-dev] installing node 24.11.0 (app/.nvmrc)" in lines
    # A bad checksum is the run's reason.
    monkeypatch.setattr(toolchains, "_open_url", releases(node_sha="0" * 64))
    spec = _spec(
        data, ["true"], toolchains={"node": "app/.nvmrc"}, isolation_check=False
    )
    result, _ = _run(tmp_path / "again", data, spec)
    assert result["reason"] == "dev-toolchain-checksum"
