"""Pure parts of the train: the spec model, impact selection, cargo output,
the step runner and the flaky registry."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli.errors import ERRORS
from piceli.train import CargoCheck, CargoTests, Impact, Step, Train, TrainError
from piceli.train import cargo as cargo_output
from piceli.train.impact import cargo_arguments, select
from piceli.train.runner import LocalRunner, no_events
from piceli.train.spec import split_entry
from piceli.train.state import TrainState
from piceli.train.steps import StepContext, run_step

# ------------------------------------------------------------------ model


def test_spec_round_trips_through_json_data() -> None:
    train = Train(
        "shop",
        base="main",
        steps=[
            CargoCheck(select="all"),
            CargoTests(select=Impact(roots=["Cargo.toml"]), retry=2, args=["--", "-q"]),
            Step.command("fitness", ["scripts/fitness.sh"], env={"A": "1"}, timeout=60),
            Step.publish("prebuilt", ["scripts/publish.sh"]),
        ],
    )
    assert Train.from_dict(train.to_dict()) == train
    assert [s.name for s in train.test_steps] == [
        "cargo-check",
        "cargo-test",
        "fitness",
    ]
    assert [s.name for s in train.publish_steps] == ["prebuilt"]
    assert train.tag_name("2026-10-23", 3) == "tested/2026-10-23.3"


@pytest.mark.parametrize(
    "build",
    [
        lambda: Train("Not A Label", steps=[Step.command("a", ["true"])]),
        lambda: Train("shop", steps=[]),
        lambda: Train("shop", steps=[Step.publish("p", ["true"])]),
        lambda: Train(
            "shop", steps=[Step.command("a", ["x"]), Step.command("a", ["y"])]
        ),
        lambda: Train("shop", tag="tested/{date}", steps=[Step.command("a", ["x"])]),
        lambda: Train(
            "shop", base="main", tested="main", steps=[Step.command("a", ["x"])]
        ),
        lambda: Step.command("merge", ["x"]),
        lambda: Step.command("a", []),
        lambda: CargoTests(retry=-1),
        lambda: CargoTests(select="some"),  # type: ignore[arg-type]
    ],
)
def test_invalid_declarations_are_refused(build: Any) -> None:
    with pytest.raises(TrainError) as caught:
        build()
    assert caught.value.code == "train-invalid"


def test_every_train_code_is_registered() -> None:
    import piceli.train

    package = Path(piceli.train.__file__).parent
    source = "\n".join(path.read_text() for path in sorted(package.glob("*.py")))

    used = set(re.findall(r'"(train-[a-z-]+)"', source))
    assert used and used <= set(ERRORS), used - set(ERRORS)


@pytest.mark.parametrize(
    ("entry", "parts"),
    [
        ("pkg.module", ("pkg.module", None)),
        ("pkg.module:train", ("pkg.module", "train")),
        ("dir/spec.py", ("dir/spec.py", None)),
        ("dir/spec.py:trains.main", ("dir/spec.py", "trains.main")),
    ],
)
def test_entries_split_into_module_and_attribute(
    entry: str, parts: tuple[str, str | None]
) -> None:
    assert split_entry(entry) == parts


# ----------------------------------------------------------------- impact


def _metadata(root: str = "/w") -> dict[str, Any]:
    def package(name: str, deps: list[tuple[str, str | None]]) -> dict[str, Any]:
        return {
            "id": f"{name}-id",
            "name": name,
            "manifest_path": f"{root}/crates/{name}/Cargo.toml",
            "dependencies": [
                {"name": dep, "kind": kind, "path": f"{root}/crates/{dep}"}
                for dep, kind in deps
            ]
            + [{"name": "serde", "kind": None}],
        }

    packages = [
        package("core", []),
        package("api", [("core", None)]),
        package("cli", [("api", None)]),
        package("bench", [("core", "dev")]),
        package("other", []),
    ]
    return {
        "workspace_root": root,
        "workspace_members": [p["id"] for p in packages],
        "packages": packages,
    }


def test_impact_selects_changed_packages_and_their_dependents() -> None:
    why: list[str] = []
    chosen = select(
        ["crates/core/src/lib.rs"], _metadata(), ("Cargo.toml",), why.append
    )
    assert chosen == ["api", "bench", "cli", "core"]
    assert "impact: bench: its tests use core" in why


def test_impact_dev_dependents_do_not_propagate() -> None:
    meta = _metadata()
    meta["packages"].append(
        {
            "id": "x-id",
            "name": "x",
            "manifest_path": "/w/crates/x/Cargo.toml",
            "dependencies": [
                {"name": "bench", "kind": None, "path": "/w/crates/bench"}
            ],
        }
    )
    meta["workspace_members"].append("x-id")
    assert select(["crates/core/src/a.rs"], meta, ()) == ["api", "bench", "cli", "core"]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["Cargo.lock"], None),
        (["rust-toolchain.toml", "crates/other/x.rs"], None),
        ([".cargo/config.toml"], None),
        (["README.md", "docs/x.md", "scripts/fitness.sh"], []),
        (["crates/other/src/lib.rs"], ["other"]),
        (["crates/api/Cargo.toml"], ["api", "cli"]),
    ],
)
def test_impact_root_files_select_everything(
    files: list[str], expected: list[str] | None
) -> None:
    assert select(files, _metadata(), Impact().roots) == expected


def test_cargo_arguments() -> None:
    assert cargo_arguments(None) == ["--workspace"]
    assert cargo_arguments(["a", "b"]) == ["-p", "a", "-p", "b"]


# ------------------------------------------------------------ cargo output

CARGO_OUTPUT = """\
   Compiling a v0.1.0 (/w/crates/a)
     Running unittests src/lib.rs (target/debug/deps/a-3fef0e11b2f043ea)

running 2 tests
test tests::ok ... ok
test tests::bad ... FAILED

failures:

---- tests::bad stdout ----

thread 'tests::bad' panicked at crates/a/src/lib.rs:5:24:
assertion `left == right` failed

failures:
    tests::bad

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out

error: test failed, to rerun pass `-p a --lib`
     Running tests/it.rs (target/debug/deps/it-29b9dfabeebf4550)

running 2 tests
test it_ok ... ok
test it_bad ... FAILED

failures:

---- it_bad stdout ----

boom

failures:
    it_bad

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out

error: test failed, to rerun pass `-p b --test it`
   Doc-tests a

running 0 tests

error: 2 targets failed:
    `-p a --lib`
    `-p b --test it`
"""


def test_cargo_output_names_failed_tests_per_target() -> None:
    report = cargo_output.parse(CARGO_OUTPUT)
    assert [(f.target, f.tests) for f in report.failed] == [
        ("-p a --lib", ["tests::bad"]),
        ("-p b --test it", ["it_bad"]),
    ]
    assert report.retryable
    assert (
        "assertion `left == right` failed"
        in report.output[("-p a --lib", "tests::bad")]
    )
    assert report.output[("-p b --test it", "it_bad")] == "boom"


def test_a_crashed_target_is_not_retryable() -> None:
    text = (
        "     Running tests/x.rs (target/debug/deps/x-1)\n\nrunning 1 test\n"
        "error: test failed, to rerun pass `-p b --test x`\n"
    )
    report = cargo_output.parse(text)
    assert [(f.target, f.tests) for f in report.failed] == [("-p b --test x", [])]
    assert not report.retryable


def test_retry_runs_only_the_failed_tests_by_exact_name() -> None:
    (target, _) = cargo_output.parse(CARGO_OUTPUT).failed
    argv = cargo_output.retry_argv(
        target, ("--features", "x", "--", "-q"), ["tests::bad"]
    )
    assert argv == [
        "cargo", "test", "-p", "a", "--lib", "--features", "x",
        "--", "-q", "--exact", "tests::bad",
    ]  # fmt: skip


# ------------------------------------------------------------- the runner


def _context(tmp_path: Path) -> StepContext:
    return StepContext(
        checkout=tmp_path,
        base="0" * 40,
        head="1" * 40,
        branches=["a"],
        changed=[],
        env={"PATH": "/usr/bin:/bin", "PICELI_TRAIN": "toy"},
        log_dir=tmp_path / "logs",
        prefix="01-",
        runner=LocalRunner(),
        events=no_events,
    )


def test_a_command_step_passes_or_fails_with_its_tail(tmp_path: Path) -> None:
    ok = run_step(
        Step.command("ok", [sys.executable, "-c", "print('fine')"]), _context(tmp_path)
    )
    assert ok.verdict == "passed" and ok.host == "local"
    assert "fine" in Path(ok.log or "").read_text()
    failed = run_step(
        Step.command(
            "bad", [sys.executable, "-c", "import sys; print('no'); sys.exit(3)"]
        ),
        _context(tmp_path),
    )
    assert failed.verdict == "failed"
    assert failed.detail == {"exit_code": 3} and failed.tail.endswith("no")


def test_a_step_over_its_timeout_is_stopped(tmp_path: Path) -> None:
    slow = Step.command(
        "slow", [sys.executable, "-c", "import time; time.sleep(600)"], timeout=1
    )
    result = run_step(slow, _context(tmp_path))
    assert result.verdict == "timeout"
    assert "(timeout)" in Path(result.log or "").read_text()


def test_a_missing_program_fails_the_step(tmp_path: Path) -> None:
    result = run_step(Step.command("gone", ["no-such-program-x"]), _context(tmp_path))
    assert result.verdict == "failed"


# ------------------------------------------------------------------ state


def test_flaky_registry_is_append_only_with_running_totals(tmp_path: Path) -> None:
    state = TrainState(tmp_path / "toy")
    seen = {"target": "-p a --lib", "test": "t", "tail": "boom", "branches": ["x"]}
    first = state.record_flakes([seen, seen], "run-1", "2026-10-23T10:00:00Z")
    assert len(first) == 1 and first[0]["count"] == 1
    state.record_flakes([{**seen, "tail": "again"}], "run-2", "2026-10-23T11:00:00Z")
    entry = state.flaky()["-p a --lib t"]
    assert entry["count"] == 2
    assert entry["first_seen"] == "2026-10-23T10:00:00Z"
    assert entry["last_seen"] == "2026-10-23T11:00:00Z"
    assert entry["last_tail"] == "again"
    assert len(state.flaky_path.read_text().splitlines()) == 2


def test_a_second_run_of_the_same_train_is_busy(tmp_path: Path) -> None:
    state = TrainState(tmp_path / "toy")
    with state.lock():
        with pytest.raises(TrainError) as caught:
            with state.lock():
                pass
    assert caught.value.code == "train-busy"
