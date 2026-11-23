"""0.19: corrupted cargo artifacts are found, cleared, retried.

Four corrupted artifacts in ten hours on a faulty node: a truncated object
breaking the link of ``geometry``, ``rust-lld: undefined hidden symbol`` in its
incremental objects, a proc-macro ``.so`` the loader refused. Each is
recognised from the build's output; only the affected crates' artifacts are
removed, and the command runs once more.
"""

from __future__ import annotations

from pathlib import Path

from piceli.dev.pod import FirstError, clear_crates, corrupt_crates

TARGET = "/cache/lineages/shop-rust-1/0/target"

LOADER = [
    "   Compiling geometry v0.4.0 (/work/tree/crates/geometry)",
    f"error: {TARGET}/debug/deps/libserde_derive-1a2b3c.so: error while loading "
    "shared libraries: unexpected reloc type R_X86_64_RELATIVE",
    "error: could not compile `geometry` (lib) due to 1 previous error",
]
LLD = [
    "   Compiling geometry v0.4.0 (/work/tree/crates/geometry)",
    "error: linking with `cc` failed: exit status: 1",
    "  = note: rust-lld: error: undefined hidden symbol: geometry::thing::h12",
    f"          >>> referenced by {TARGET}/debug/incremental/geometry-3k2j1/s-abc/x.o",
    'error: could not compile `geometry` (bin "geometry") due to 1 previous error',
]
METADATA = [
    "error[E0786]: found invalid metadata files for crate `tokio_util`",
    f"  = note: invalid metadata version found: {TARGET}/debug/deps/libtokio_util-9f.rmeta",
    "error: could not compile `app` (lib) due to 1 previous error",
]
PLAIN = [
    "error[E0308]: mismatched types",
    "error: could not compile `geometry` (lib) due to 1 previous error",
]


def test_loader_link_and_metadata_failures_name_their_crates() -> None:
    assert corrupt_crates(LOADER) == (["serde_derive"], "loader")
    assert corrupt_crates(LLD) == (["geometry"], "link")
    assert corrupt_crates(METADATA) == (["tokio_util"], "metadata")


def test_an_ordinary_compile_error_is_no_corruption() -> None:
    assert corrupt_crates(PLAIN) == ([], None)
    assert corrupt_crates([]) == ([], None)


def test_clearing_removes_only_the_named_crates_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "target"
    doomed = [
        "debug/deps/libgeometry-1a.rlib",
        "debug/deps/libgeometry-1a.rmeta",
        "debug/deps/geometry-1a.d",
        "debug/.fingerprint/geometry-1a/lib-geometry",
        "debug/build/geometry-77/out/x",
        "debug/incremental/geometry-3k2j1/s-abc/x.o",
        "wasm32-unknown-unknown/release/deps/libgeometry-2b.rlib",
        "debug/deps/libserde_derive-1a2b3c.so",
    ]
    kept = [
        "debug/deps/libgeometryx-1a.rlib",  # another crate whose name starts alike
        "debug/deps/libapp-1.rlib",
        "debug/.fingerprint/app-1/lib-app",
    ]
    for name in doomed + kept:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text("x")
    removed = clear_crates(target, ["geometry", "serde-derive"])
    assert removed == len(doomed)
    assert all(not (target / name).exists() for name in doomed)
    assert all((target / name).exists() for name in kept)


def test_the_first_error_and_what_follows_it_are_kept() -> None:
    first = FirstError(lines=3)
    for line in ["   Compiling a", "warning: unused", *PLAIN, "more", "and more"]:
        first.feed(line)
    assert first.lines == [
        "error[E0308]: mismatched types",
        "error: could not compile `geometry` (lib) due to 1 previous error",
        "more",
    ]


def _lineage(cache: Path, key: str, index: int) -> Path:
    path = cache / "lineages" / key / str(index)
    (path / "target" / "debug" / "deps").mkdir(parents=True)
    (path / "target" / "debug" / "deps" / "libgeometry-1.rlib").write_bytes(b"x" * 4096)
    (path / "target" / "debug" / "deps" / "libapp-1.rlib").write_bytes(b"x" * 4096)
    (path / "tree").mkdir()
    (path / "meta.json").write_text('{"runs": 3, "hint": "main"}')
    return path


def test_cache_clear_empties_free_lineages_and_skips_busy_ones(tmp_path: Path) -> None:
    """``piceli dev cache clear [--lineage]``: no root pod on
    the claim; a lineage a run holds is left alone and reported."""
    import fcntl
    import os

    from piceli.dev.pod import clear_cache

    cache = tmp_path / "cache"
    a0 = _lineage(cache, "shop-rust-1a2b", 0)
    a1 = _lineage(cache, "shop-rust-1a2b", 1)
    other = _lineage(cache, "sdk-rust-9f", 0)
    held = os.open(a1 / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = clear_cache(cache, lineage="shop")
    finally:
        os.close(held)
    assert result["schema"] == "piceli.dev-cache-clear.v1"
    assert [item["lineage"] for item in result["cleared"]] == ["shop-rust-1a2b/0"]
    assert result["cleared"][0]["bytes"] >= 8192
    assert result["busy"] == ["shop-rust-1a2b/1"]
    assert not (a0 / "target").exists() and (a0 / "tree").is_dir()
    assert '"runs": 0' in (a0 / "meta.json").read_text()  # the next run is cold
    assert (a1 / "target").exists() and (other / "target").exists()
    # Only some crates, in every lineage of one key.
    only = clear_cache(cache, lineage="sdk-rust-9f/0", crates=["geometry"])
    assert only["cleared"] == [{"lineage": "sdk-rust-9f/0", "entries": 1}]
    assert (other / "target" / "debug" / "deps" / "libapp-1.rlib").exists()
    assert clear_cache(cache, lineage="nothing")["cleared"] == []


ICE = [
    "   Compiling core v0.4.0 (/work/tree/crates/core)",
    "thread 'rustc' panicked at compiler/rustc_query_system/src/dep_graph/serialized.rs:233:9:",
    "error: the compiler unexpectedly panicked. this is a bug.",
    "note: we would appreciate a bug report",
    "error: internal compiler error: unexpected panic",
    "error: could not compile `core` (lib)",
]
UNDEFINED = [
    "   Compiling core v0.4.0 (/x)",
    "error: linking with `cc` failed: exit status: 1",
    "  = note: rust-lld: error: undefined symbol: "
    "alloc::collections::btree::navigate::LazyLeafRange::init_front",
    f"          >>> referenced by {TARGET}/debug/deps/libsignal_hook_registry-7a1b.rlib(x.o)",
    "error: could not compile `core` (lib test) due to 1 previous error",
]


def test_an_ice_in_incremental_state_and_a_plain_undefined_symbol_are_corruption() -> (
    None
):
    """0.20."""
    assert corrupt_crates(ICE)[1] == "incremental"
    crates, kind = corrupt_crates(UNDEFINED)
    assert kind == "link" and "signal_hook_registry" in crates and "core" in crates


def test_clearing_incremental_state_keeps_the_rest_of_the_target(
    tmp_path: Path,
) -> None:
    from piceli.dev.pod import clear_incremental

    target = tmp_path / "target"
    for name in (
        "debug/incremental/core-1/s-1/x.o",
        "wasm32-unknown-unknown/debug/incremental/core-2/s/x.o",
        "debug/deps/libcore-1.rlib",
    ):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        (target / name).write_text("x")
    assert clear_incremental(target) == 2
    assert not (target / "debug/incremental").exists()
    assert not (target / "wasm32-unknown-unknown/debug/incremental").exists()
    assert (target / "debug/deps/libcore-1.rlib").exists()


def test_a_damaged_lineage_is_told_from_the_codes_own_errors() -> None:
    """0.69.0: real lines from a lineage whose deps were damaged."""
    from piceli.dev.pod import lineage_damage

    target = Path(TARGET)
    rlib = f"{TARGET}/debug/deps/libarrow_ord-05e60e528b0ab6ce.rlib"
    link = f"rust-lld: error: {rlib}(arrow_ord-05e60e528b0ab6ce.arrow_ord.o): bad"
    assert lineage_damage(["error: linking with `cc` failed", link], target) == link
    segv = "error: rustc interrupted by SIGSEGV, printing backtrace"
    assert lineage_damage([segv], target) == segv
    killed = (
        "  process didn't exit successfully: `/root/.rustup/toolchains/1.97-x/bin/"
        "rustc --crate-name syn --edition=2021 src/lib.rs` (signal: 11, SIGSEGV: "
        "invalid memory reference)"
    )
    assert lineage_damage([killed], target) == killed.strip()
    # The code's own: an undefined symbol; a crash or file outside this target.
    assert lineage_damage(UNDEFINED, target) is None
    assert lineage_damage(LLD, target) is None
    elsewhere = (
        "  process didn't exit successfully: `/tmp/other/debug/deps/shop-1a` "
        "(signal: 11, SIGSEGV: invalid memory reference)"
    )
    assert lineage_damage([elsewhere], target) is None
    other = link.replace(TARGET, "/cache/lineages/shop-rust-1/1/target")
    assert lineage_damage([other], target) is None
    # An ICE or invalid metadata counts only once a repair did not cure it.
    assert lineage_damage(ICE, target) is None
    assert lineage_damage(ICE, target, persisted=True)
    assert lineage_damage(METADATA, target, persisted=True)
    assert lineage_damage(UNDEFINED, target, persisted=True) is None


# ---------------------------------------- damaged registry sources (0.77.0)
HOME = "/cache/cargo-home/ac31499b59"
INDEX = "index.crates.io-1949cf8c6b5b557f"
NOT_UTF8 = [
    "   Compiling chrono v0.4.45",
    f"error: couldn't read `{HOME}/registry/src/{INDEX}/chrono-0.4.45/src/naive/"
    "date/mod.rs`: stream did not contain valid UTF-8",
    "error: could not compile `chrono` (lib) due to 1 previous error",
]
MANIFEST = [
    "error: failed to download `base16ct v0.2.0`",
    "Caused by:",
    f"  failed to parse manifest at `{HOME}/registry/src/{INDEX}/base16ct-0.2.0/Cargo.toml`",
]
TOKEN = [
    "error: unknown start of token: \\u{14}",
    f"  --> {HOME}/registry/src/{INDEX}/serde-json-1.0.140/src/de.rs:12:3",
]


def test_unreadable_registry_sources_name_their_crates() -> None:
    from piceli.dev.pod import registry_src_damage

    home = Path(HOME)
    found = registry_src_damage(NOT_UTF8 + MANIFEST + TOKEN, home)
    assert [(item["index"], item["crate"]) for item in found] == [
        (INDEX, "base16ct-0.2.0"),
        (INDEX, "chrono-0.4.45"),
        (INDEX, "serde-json-1.0.140"),
    ]
    assert "stream did not contain valid UTF-8" in found[1]["line"]
    # The code's own errors, another cargo home, a path that leaves src/.
    assert registry_src_damage(PLAIN, home) == []
    assert registry_src_damage(NOT_UTF8, Path("/cache/cargo-home/other")) == []
    assert (
        registry_src_damage(
            [
                f"error: couldn't read `{HOME}/registry/src/../cache/{INDEX}/x-1.0.0.crate`",
                f"error: couldn't read `{HOME}/registry/src/{INDEX}/../../index`",
                f"error: couldn't read `/evil{HOME}/registry/src/{INDEX}/chrono-0.4.45/x`",
            ],
            home,
        )
        == []
    )
    # A registry path in an ordinary error is no damage.
    assert (
        registry_src_damage(
            [
                "error[E0658]: use of unstable library feature",
                f"  --> {HOME}/registry/src/{INDEX}/chrono-0.4.45/src/lib.rs:1:1",
            ],
            home,
        )
        == []
    )


def _cargo_home(cache: Path, key: str = "ac31499b59") -> Path:
    home = cache / "cargo-home" / key
    for crate in ("chrono-0.4.44", "chrono-0.4.45", "base16ct-0.2.0", "serde-1.0.0"):
        (home / "registry" / "src" / INDEX / crate / "src").mkdir(parents=True)
        (home / "registry" / "src" / INDEX / crate / "src" / "lib.rs").write_text("x")
        (home / "registry" / "cache" / INDEX).mkdir(parents=True, exist_ok=True)
        (home / "registry" / "cache" / INDEX / f"{crate}.crate").write_text("c")
    (home / "registry" / "index" / INDEX).mkdir(parents=True)
    (home / "registry" / "index" / INDEX / "config.json").write_text("{}")
    return home


def _left(home: Path) -> list[str]:
    return sorted(
        str(path.relative_to(home))
        for path in home.rglob("*")
        if path.is_file() and not path.name.startswith(".package-cache")
    )


def test_cache_clear_removes_the_chosen_registry_sources_and_nothing_else(
    tmp_path: Path,
) -> None:
    from piceli.dev.pod import clear_cache

    cache = tmp_path / "cache"
    home = _cargo_home(cache)
    lineage = _lineage(cache, "shop-rust-1a2b", 0)
    before = _left(home)
    # --crate NAME: every version of it, and its artifacts as before.
    result = clear_cache(cache, crates=["chrono"], lock_wait=0)
    assert result["sources"] == [
        {
            "cargo_home": "ac31499b59",
            "removed": [f"{INDEX}/chrono-0.4.44", f"{INDEX}/chrono-0.4.45"],
        }
    ]
    assert result["sources_busy"] == []
    assert [
        line for line in before if not line.startswith(f"registry/src/{INDEX}/chrono-")
    ] == _left(home)
    assert (home / "registry" / "cache" / INDEX / "chrono-0.4.45.crate").is_file()
    # NAME-VERSION: that one; --registry-src: no lineage's target.
    result = clear_cache(
        cache, crates=["base16ct-0.2.0"], registry_src=True, lock_wait=0
    )
    assert result["sources"][0]["removed"] == [f"{INDEX}/base16ct-0.2.0"]
    assert result["cleared"] == []
    assert (lineage / "target" / "debug" / "deps" / "libgeometry-1.rlib").exists()
    # --registry-src alone: every extracted crate; .crate files and index stay.
    result = clear_cache(cache, registry_src=True, lock_wait=0)
    assert result["sources"][0]["removed"] == [f"{INDEX}/serde-1.0.0"]
    assert not list((home / "registry" / "src" / INDEX).iterdir())
    assert len(list((home / "registry" / "cache" / INDEX).iterdir())) == 4
    assert (home / "registry" / "index" / INDEX / "config.json").is_file()
    assert (lineage / "target").is_dir()


def test_a_cargo_home_a_build_uses_is_not_cleared(tmp_path: Path) -> None:
    import fcntl
    import os

    from piceli.dev.pod import clear_cache

    cache = tmp_path / "cache"
    home = _cargo_home(cache)
    held = os.open(home / ".package-cache-mutate", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_SH)  # what cargo holds through a build
    try:
        result = clear_cache(cache, registry_src=True, lock_wait=0)
    finally:
        os.close(held)
    assert result["sources"] == [] and result["sources_busy"] == ["ac31499b59"]
    assert (home / "registry" / "src" / INDEX / "chrono-0.4.45").is_dir()


def test_only_a_real_directory_inside_registry_src_is_removed(tmp_path: Path) -> None:
    from piceli.dev.pod import remove_registry_src

    cache = tmp_path / "cache"
    home = _cargo_home(cache)
    outside = tmp_path / "outside" / "chrono-9.9.9"
    outside.mkdir(parents=True)
    (outside / "keep").write_text("x")
    src = home / "registry" / "src"
    (src / INDEX / "linked-1.0.0").symlink_to(outside)
    (src / "linked-index").symlink_to(outside.parent)
    assert not remove_registry_src(home, INDEX, "linked-1.0.0")
    assert not remove_registry_src(home, "linked-index", "chrono-9.9.9")
    assert not remove_registry_src(home, "..", "cache")
    assert not remove_registry_src(home, INDEX, "..")
    assert not remove_registry_src(home, f"{INDEX}/../..", "outside")
    assert (outside / "keep").is_file()
    # registry/src itself a link elsewhere: nothing goes.
    other = _cargo_home(tmp_path / "other")
    moved = tmp_path / "elsewhere-src"
    (other / "registry" / "src").rename(moved)
    (other / "registry" / "src").symlink_to(moved)
    assert not remove_registry_src(other, INDEX, "chrono-0.4.45")
    assert (moved / INDEX / "chrono-0.4.45").is_dir()
    # The real one goes; one already gone counts as gone.
    assert remove_registry_src(home, INDEX, "chrono-0.4.45")
    assert not (src / INDEX / "chrono-0.4.45").exists()
    assert remove_registry_src(home, INDEX, "chrono-0.4.45")
