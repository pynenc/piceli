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
