"""Opt-in: the published builder image builds Rust crates with C builds.

Crates such as ``jemalloc-sys`` (make) and ``aws-lc-sys`` (cmake) compile C
from their build scripts. This builds ``images/Dockerfile``'s ``builder``
target from this checkout's wheel and, as the image's non-root user, builds
a crate whose ``build.rs`` runs ``cmake`` and ``make`` and links the result.
Skipped unless ``PICELI_IMAGE_TESTS=1`` (it needs ``docker`` with
``buildx``, ``uv`` and a few minutes); it removes the image it builds::

    PICELI_IMAGE_TESTS=1 uv run pytest tests/integration/test_builder_image.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get("PICELI_IMAGE_TESTS") != "1"
        or not (shutil.which("docker") and shutil.which("uv")),
        reason="set PICELI_IMAGE_TESTS=1 (needs docker with buildx and uv)",
    ),
]

CRATE = {
    "Cargo.toml": (
        '[package]\nname = "cbuild"\nversion = "0.1.0"\nedition = "2021"\n'
        'build = "build.rs"\n'
    ),
    "build.rs": """\
use std::{env, path::PathBuf, process::Command};

fn main() {
    let build = PathBuf::from(env::var("OUT_DIR").unwrap()).join("c");
    let configured = Command::new("cmake").args(["-S", "c", "-B"]).arg(&build).status();
    assert!(configured.unwrap().success(), "cmake failed");
    let made = Command::new("make").arg("-C").arg(&build).status();
    assert!(made.unwrap().success(), "make failed");
    println!("cargo:rustc-link-search=native={}", build.display());
    println!("cargo:rustc-link-lib=static=answer");
}
""",
    "c/CMakeLists.txt": (
        "cmake_minimum_required(VERSION 3.13)\nproject(answer C)\n"
        "add_library(answer STATIC answer.c)\n"
    ),
    "c/answer.c": "int answer(void) { return 42; }\n",
    "src/main.rs": (
        'extern "C" {\n    fn answer() -> i32;\n}\n\n'
        'fn main() {\n    println!("answer={}", unsafe { answer() });\n}\n'
    ),
}


def _run(*command: str, timeout: int = 1500) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]
    return result.stdout


@pytest.fixture(scope="module")
def builder_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tag = f"piceli-builder:test-{uuid.uuid4().hex[:10]}"
    context = tmp_path_factory.mktemp("builder-context")
    _run("uv", "build", "-q", "--wheel", "--out-dir", str(context / "dist"), str(ROOT))
    _run(
        "docker", "buildx", "build", "-q", "--load", "--target", "builder",
        "-f", str(ROOT / "images" / "Dockerfile"), "-t", tag, str(context),
    )  # fmt: skip
    try:
        yield tag
    finally:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True)


def test_builder_has_the_tools_c_builds_need(builder_image: str) -> None:
    tools = "make cmake pkg-config curl xz gcc cargo cargo-zigbuild zig uv python3"
    output = _run(
        "docker", "run", "--rm", builder_image, "sh", "-c",
        f"for t in {tools}; do command -v $t >/dev/null || echo missing $t; done",
    )  # fmt: skip
    assert output.strip() == ""


def test_builder_builds_a_crate_whose_build_script_runs_cmake_and_make(
    builder_image: str, tmp_path: Path
) -> None:
    crate = tmp_path / "crate"
    for name, text in CRATE.items():
        (crate / name).parent.mkdir(parents=True, exist_ok=True)
        (crate / name).write_text(text)
    output = _run(
        "docker", "run", "--rm", "-e", "HOME=/tmp/home",
        "-v", f"{crate}:/src:ro", builder_image, "sh", "-ec",
        "mkdir -p $HOME && cp -r /src $HOME/crate && cd $HOME/crate "
        "&& cargo run -q --offline",
    )  # fmt: skip
    assert output.strip().endswith("answer=42")
