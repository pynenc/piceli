"""0.94.0: ``build.toolchains.go`` pins Go by version or by a go.mod toolchain line.

The go.dev tarball is installed once into ``<cache>/toolchains/go``, checked
against the sha256 of go.dev's release list, and selected with
``GOTOOLCHAIN=local`` and caches inside the build cache. No network: the
downloads come from a fake opener.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildSpec
from piceli.artifacts.host_toolchains import provenance
from piceli.dev import toolchains
from piceli.dev.toolchains import ToolchainError
from tests.unit.test_toolchains import Opener, sha, tarball

GO = "1.26.11"
GO_ASSET = f"go{GO}.linux-amd64.tar.gz"
GO_URL = f"https://go.dev/dl/{GO_ASSET}"
GO_LIST = "https://go.dev/dl/?mode=json&include=all"


def go_release(body: bytes, published: str | None = None) -> bytes:
    """go.dev's release list (``?mode=json&include=all``) for ``GO``."""
    return json.dumps(
        [
            {"version": "go1.27.0", "stable": True, "files": []},
            {
                "version": f"go{GO}",
                "stable": True,
                "files": [
                    {
                        "filename": f"go{GO}.src.tar.gz",
                        "os": "",
                        "arch": "",
                        "sha256": "1" * 64,
                        "kind": "source",
                    },
                    {
                        "filename": f"go{GO}.darwin-arm64.tar.gz",
                        "os": "darwin",
                        "arch": "arm64",
                        "sha256": "2" * 64,
                        "kind": "archive",
                    },
                    {
                        "filename": GO_ASSET,
                        "os": "linux",
                        "arch": "amd64",
                        "sha256": published or sha(body),
                        "kind": "archive",
                    },
                ],
            },
        ]
    ).encode()


def go_opener(published: str | None = None) -> Opener:
    body = tarball(
        "go",
        {"bin/go": "#!/bin/sh\necho go1.26.11\n", "LICENSE": "Go licence\n"},
        "w:gz",
    )
    return Opener({GO_LIST: go_release(body, published), GO_URL: body})


# ------------------------------------------------------------------ pins


def test_a_go_pin_is_a_version_or_a_go_mod_toolchain_line() -> None:
    assert toolchains.is_literal("go", "1.26.11")
    assert not toolchains.is_literal("go", "app/go.mod")
    assert toolchains.check_declared({"go": "app/go.mod"}, "t") == {"go": "app/go.mod"}
    pins = toolchains.resolve({"go": "1.26.11"}, lambda _p: b"")
    assert pins == {"go": {"version": "1.26.11", "file": None}}
    text = b"module example.org/app\n\ngo 1.26\n\ntoolchain go1.26.11 // pinned\n"
    pins = toolchains.resolve({"go": "app/go.mod"}, lambda _p: text)
    assert pins == {"go": {"version": "1.26.11", "file": "app/go.mod"}}


@pytest.mark.parametrize(
    "text",
    [
        "module example.org/app\n\ngo 1.26\n",
        "module example.org/app\n\ngo 1.26.11\n",
        "module example.org/app\n\ntoolchain go1.27rc1\n",
        "module example.org/app\n\ntoolchain default\n",
    ],
)
def test_a_go_mod_without_an_exact_toolchain_line_is_refused(text: str) -> None:
    with pytest.raises(ToolchainError) as raised:
        toolchains.resolve({"go": "app/go.mod"}, lambda _p: text.encode())
    assert raised.value.kind == "invalid"
    assert "app/go.mod" in str(raised.value)


def test_a_minor_alone_is_not_a_go_version() -> None:
    with pytest.raises(ToolchainError) as raised:
        toolchains.check_declared({"go": "1.26"}, "build.toolchains")
    assert raised.value.kind == "invalid"


def test_go_asset_names_follow_go_dev() -> None:
    assert toolchains.go_asset(GO, "linux", "amd64") == GO_ASSET
    assert toolchains.go_asset(GO, "darwin", "arm64") == f"go{GO}.darwin-arm64.tar.gz"
    with pytest.raises(ToolchainError) as raised:
        toolchains.go_asset(GO, "windows", "amd64")
    assert raised.value.kind == "failed"


def test_the_checksum_is_the_archive_of_that_release() -> None:
    listing = go_release(b"x", published="a" * 64)
    assert toolchains.go_checksum(listing, GO, GO_ASSET) == "a" * 64
    with pytest.raises(ToolchainError) as raised:
        toolchains.go_checksum(listing, "1.26.10", GO_ASSET)
    assert str(raised.value) == (
        f"go 1.26.10: no {GO_ASSET} in the go.dev release list"
    )
    with pytest.raises(ToolchainError):
        toolchains.go_checksum(b"<html>", GO, GO_ASSET)


# ------------------------------------------------------------------ install


def test_go_is_installed_once_verified_and_selected(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    opener = go_opener()
    pins = {"go": {"version": GO, "file": None}}
    selected, paths, report = toolchains.provision(
        cache, pins, lambda _line: None, platform=("linux", "amd64"), opener=opener
    )
    root = cache / "toolchains" / "go" / f"{GO}-linux-amd64"
    assert paths == [str(root / "bin")]
    assert (root / "bin" / "go").is_file() and (root / "LICENSE").is_file()
    assert selected == {
        "GOTOOLCHAIN": "local",
        "GOMODCACHE": str(cache / "go" / "mod"),
        "GOCACHE": str(cache / "go" / "build"),
        "GOPATH": str(cache / "go" / "path"),
        "GOFLAGS": "-modcacherw -trimpath",
    }
    assert report["go"]["installed"] is True
    assert report["go"]["url"] == GO_URL
    assert report["go"]["sha256"] == sha(opener.bodies[GO_URL])
    assert (cache / "toolchains" / f"go-{GO}-linux-amd64.done").is_file()
    # The next build finds the done-marker and downloads nothing.
    calls = len(opener.calls)
    _, _, again = toolchains.provision(
        cache, pins, lambda _line: None, platform=("linux", "amd64"), opener=opener
    )
    assert again["go"]["installed"] is False and len(opener.calls) == calls
    # The provenance records the pin and the download.
    pinned, downloads = provenance(pins, report)
    assert pinned == {"go": {"version": GO, "file": None}}
    assert downloads == [
        {
            "name": "toolchain:go",
            "uri": GO_URL,
            "digest": {"sha256": report["go"]["sha256"]},
        }
    ]


def test_a_tarball_that_does_not_match_go_dev_is_not_installed(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    with pytest.raises(ToolchainError) as raised:
        toolchains.provision(
            cache,
            {"go": {"version": GO, "file": None}},
            lambda _line: None,
            platform=("linux", "amd64"),
            opener=go_opener(published="f" * 64),
        )
    assert raised.value.kind == "checksum"
    assert GO_ASSET in str(raised.value)
    assert not (cache / "toolchains" / "go" / f"{GO}-linux-amd64").exists()
    assert not (cache / "toolchains" / f"go-{GO}-linux-amd64.done").exists()


# ------------------------------------------------------------------ spec

SPEC = """
revision = "piceli.host-build.v1"
name = "app"

[build]
tools = ["sh"]
commands = [["sh", "-c", "go build ./..."]]
toolchains = { go = "1.26.11" }
@EXTRA@

[context.app]
path = "app"
include = ["*.go", "go.mod"]

[[output.files]]
name = "bin"
target_files = { "app" = "app" }
"""


def load(tmp_path: Path, extra: str = "", tools: str = '["sh"]') -> HostBuildSpec:
    path = tmp_path / "host-build.toml"
    path.write_text(
        SPEC.replace("@EXTRA@", extra).replace('tools = ["sh"]', f"tools = {tools}")
    )
    return HostBuildSpec.from_toml(path)


def test_a_spec_pins_go_and_records_it_in_its_digest(tmp_path: Path) -> None:
    spec = load(tmp_path)
    assert dict(spec.toolchains) == {"go": "1.26.11"}
    assert spec.to_dict()["toolchains"] == {"go": "1.26.11"}


def test_build_env_cannot_override_the_pinned_go(tmp_path: Path) -> None:
    with pytest.raises(BuildSpecError) as raised:
        load(tmp_path, 'env = { GOTOOLCHAIN = "auto" }')
    assert raised.value.code == "invalid-spec"
    assert "GOTOOLCHAIN" in str(raised.value)


def test_a_declared_go_tool_beside_a_pinned_go_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BuildSpecError) as raised:
        load(tmp_path, tools='["sh", "go"]')
    assert raised.value.code == "invalid-spec"
    assert "build.tools lists go" in str(raised.value)


def test_provisioning_puts_the_pinned_go_first_for_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.artifacts import host_toolchains

    opener = go_opener()
    monkeypatch.setattr(toolchains, "_open_url", opener)
    monkeypatch.setattr(toolchains, "host_platform", lambda: ("linux", "amd64"))
    cache = tmp_path / "cache"
    environment: dict[str, Any] = {"PATH": "/usr/bin:/bin"}
    report = host_toolchains.provision(
        cache, {"go": {"version": GO, "file": None}}, environment, lambda _l: None
    )
    root = cache / "toolchains" / "go" / f"{GO}-linux-amd64"
    assert environment["PATH"].split(":")[0] == str(root / "bin")
    assert environment["GOTOOLCHAIN"] == "local"
    assert environment["GOMODCACHE"] == str(cache / "go" / "mod")
    assert report["go"]["installed"] is True
