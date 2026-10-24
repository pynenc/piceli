"""Train speed: build hosts, local slots and warm targets, content-hash reuse,
parallel halves, the kept head and ``--promote-run``, dev-target impact.

Build hosts are faked two ways: :class:`FakeHost` (a placed runner whose
state and outcome a test sets) for placement, and the real ``piceli dev run``
client and wrapper over :class:`tests.unit.dev.local_port.LocalPort` (a host
whose queue, upload, lineage and artifacts are the real code, run in
threads on directories under ``tmp_path``).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.hosts import HostState
from piceli.dev.model import BuildHost, DevBuilds
from piceli.k8s.cli import app
from piceli.train import Step, Train, TrainError
from piceli.train.engine import TrainRun, locate
from piceli.train.hosts import HostRunner, build_runner
from piceli.train.impact import cargo_test_arguments, select
from piceli.train.local import CargoCache, Slots
from piceli.train.reuse import Store, step_key
from piceli.train.runner import Command, Completed
from piceli.train.state import TrainState
from tests.train_toy import Toy, make_toy
from tests.unit.dev.local_port import LocalPort

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TOY_COUNTER", str(tmp_path / "counter"))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    return make_toy(tmp_path)


# --------------------------------------------------------------- placement


class FakeHost:
    """A placed runner: ``up``, ``free`` and the ``error`` of its runs."""

    def __init__(
        self, name: str, *, up: bool = True, free: int = 1, error: str | None = None
    ) -> None:
        self.host = name
        self.up = up
        self.free = free
        self.error = error
        self.ran: list[tuple[str, ...]] = []

    def state(self) -> tuple[bool, int, str]:
        return self.up, self.free, "" if self.up else "engine does not answer"

    def run(self, command: Command) -> Completed:
        self.ran.append(tuple(command.argv))
        if self.error:
            return Completed(None, 0.0, 0.0, self.host, "gone", error=self.error)
        return Completed(0, 0.0, 0.0, self.host, "ok")


def command(
    tmp_path: Path, *, hosts: tuple[str, ...] = (), stdout: bool = False
) -> Command:
    return Command(
        ("make",), tmp_path, tmp_path / "log", {}, 10, stdout=stdout, hosts=hosts
    )


def test_a_command_goes_to_the_first_free_host_and_names_it(tmp_path: Path) -> None:
    laptop, server = FakeHost("local", free=0), FakeHost("server", free=2)
    runner = HostRunner(
        {"local": laptop, "server": server}, default=("local", "server")
    )
    done = runner.run(command(tmp_path))
    assert done.host == "server" and server.ran and not laptop.ran
    # Every host busy: the first one that is up takes it (it queues there).
    server.free = 0
    assert runner.run(command(tmp_path)).host == "local"
    # A step's own hosts win over the train's.
    assert runner.run(command(tmp_path, hosts=("server",))).host == "server"
    # Machine output (cargo metadata) is read here, in no slot.
    assert runner.run(command(tmp_path, hosts=("server",), stdout=True)).host == "local"


def test_a_down_host_is_skipped_and_the_laptop_is_the_fallback(tmp_path: Path) -> None:
    laptop, server = FakeHost("local"), FakeHost("server", up=False)
    runner = HostRunner({"local": laptop, "server": server}, default=("server",))
    done = runner.run(command(tmp_path))
    assert done.host == "local"
    assert any("server: engine does not answer" in note for note in done.notes)
    assert any("fallback local" in note for note in done.notes)
    strict = HostRunner(
        {"local": laptop, "server": server}, default=("server",), fallback=None
    )
    with pytest.raises(TrainError) as caught:
        strict.run(command(tmp_path))
    assert caught.value.code == "train-host-unavailable"


def test_a_host_unavailable_at_start_hands_the_command_on(tmp_path: Path) -> None:
    one, two = FakeHost("one", error="unavailable"), FakeHost("two")
    runner = HostRunner(
        {"local": FakeHost("local"), "one": one, "two": two}, default=("one", "two")
    )
    done = runner.run(command(tmp_path))
    assert done.host == "two" and one.ran and two.ran
    assert any("one: unavailable at start" in note for note in done.notes)


def test_unknown_step_hosts_are_noted_and_the_default_used(tmp_path: Path) -> None:
    runner = HostRunner({"local": FakeHost("local")}, default=("local",))
    done = runner.run(command(tmp_path, hosts=("mini",)))
    assert done.host == "local"
    assert any("mini: not a build host" in note for note in done.notes)


def test_train_hosts_must_name_declared_build_hosts() -> None:
    dev = DevBuilds(node=None, image=IMAGE, hosts=[BuildHost.docker("server")])
    Train("shop", steps=[Step.command("a", ["x"])], builds=dev, hosts=("server",))
    with pytest.raises(TrainError):
        Train("shop", steps=[Step.command("a", ["x"])], builds=dev, hosts=("mini",))
    with pytest.raises(TrainError):
        Train("shop", steps=[Step.command("a", ["x"])], local_slots=0)
    with pytest.raises(TrainError):
        Step.command("a", ["x"], outputs=["dist"])  # outputs need inputs


# ------------------------------------------------------- slots and targets


def test_local_slots_hold_across_threads(tmp_path: Path) -> None:
    slots = Slots(tmp_path / "slots", 1)
    order: list[str] = []
    with slots.take() as slot:
        assert slot == 0 and slots.free() == 0

        def second() -> None:
            with slots.take():
                order.append("second")

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)
        order.append("first-done")
    thread.join(5)
    assert order == ["first-done", "second"]
    assert slots.free() == 1


def test_cargo_cache_gives_each_slot_its_target_and_finds_sccache(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "sccache").write_text('#!/bin/sh\nexec "$@"\n')
    (tools / "sccache").chmod(0o755)
    cache = CargoCache(tmp_path / "cache", "shop", tmp_path)
    env, target = cache.environment(0, {"PATH": str(tools)})
    other, _ = cache.environment(1, {"PATH": str(tools)})
    assert target is not None and target.is_dir()
    assert env["CARGO_TARGET_DIR"] != other["CARGO_TARGET_DIR"]
    assert env["RUSTC_WRAPPER"] == str(tools / "sccache")
    # The caller's own choices win.
    mine, none = cache.environment(0, {"CARGO_TARGET_DIR": "/t", "RUSTC_WRAPPER": "x"})
    assert mine == {} and none is None
    off = CargoCache(tmp_path / "cache", "shop", tmp_path, target=None, sccache=False)
    assert off.environment(0, {"PATH": str(tools)}) == ({}, None)


# ------------------------------------------------------------------ reuse


def test_a_step_key_follows_its_inputs_only(toy: Toy) -> None:
    definition = Step.command("unit", ["x"], inputs=["src/**"]).to_dict()
    first, count = step_key(definition, ["src/**"], toy.repo)
    toy.commit({"README.md": "# changed\n"}, "docs")
    assert step_key(definition, ["src/**"], toy.repo)[0] == first and count == 1
    toy.commit({"src/shared.txt": "four\n"}, "src")
    assert step_key(definition, ["src/**"], toy.repo)[0] != first
    other = Step.command("unit", ["y"], inputs=["src/**"]).to_dict()
    assert step_key(other, ["src/**"], toy.repo)[0] != first


def test_the_store_keeps_and_restores_outputs(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "dist" / "bin").mkdir(parents=True)
    (tree / "dist" / "bin" / "app").write_text("binary")
    (tree / "dist" / "bin" / "app").chmod(0o755)
    store = Store(tmp_path / "store")
    assert store.save("sha256:" + "1" * 64, tree, ["dist/missing"], {}) is None
    record = store.save("sha256:" + "1" * 64, tree, ["dist"], {"run": "r1"})
    assert record is not None and record["outputs"][0]["path"] == "dist/bin/app"
    found = store.lookup("sha256:" + "1" * 64)
    assert found is not None and found["run"] == "r1"
    other = tmp_path / "other"
    assert store.restore(found, other) == ["dist/bin/app"]
    assert (other / "dist/bin/app").read_text() == "binary"
    assert os.access(other / "dist/bin/app", os.X_OK)


COUNTING = """\
import os, pathlib, sys
with open(os.environ["TOY_COUNTER"], "a") as out:
    out.write(sys.argv[1] + "\\n")
pathlib.Path("dist").mkdir(exist_ok=True)
pathlib.Path("dist/app.txt").write_text(pathlib.Path("src/shared.txt").read_text())
"""

PUBLISH_BUILT = """\
import os, pathlib
out = pathlib.Path(os.environ["TOY_PUBLISH_OUT"])
out.mkdir(parents=True, exist_ok=True)
(out / "app.txt").write_text(pathlib.Path("dist/app.txt").read_text())
"""


def reuse_spec(extra: str = "", train_options: str = "") -> str:
    return f"""\
import sys

from piceli.train import Step, Train

train = Train(
    "toy",
    base="main",
    tested="tested",{train_options}
    steps=[
        Step.command(
            "build",
            [sys.executable, "scripts/build.py", "build"],
            inputs=["src/**", "scripts/build.py"],
            outputs=["dist/app.txt"],
        ),
        Step.command("fitness", [sys.executable, "scripts/fitness.py"]),{extra}
        Step.publish("publish", [sys.executable, "scripts/publish_built.py"]),
    ],
)
"""


def counted(toy: Toy) -> list[str]:
    path = Path(os.environ["TOY_COUNTER"])
    return path.read_text().split() if path.exists() else []


def cli(toy: Toy, *args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(
        app,
        ["train", toy.spec, *args, "--json", "--state-dir", str(toy.root / "state")],
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    return result.exit_code, json.loads(lines[-1]) if lines else {}


def step_of(body: dict[str, Any], branch: str, name: str) -> dict[str, Any]:
    found = next(b for b in body["branches"] if b["branch"] == branch)
    return next(s for s in found["steps"] if s["name"] == name)


def test_reuse_survives_bisect_and_later_runs_and_feeds_publish(toy: Toy) -> None:
    toy.commit(
        {
            "train_spec.py": reuse_spec(),
            "scripts/build.py": COUNTING,
            "scripts/publish_built.py": PUBLISH_BUILT,
        },
        "spec: a build step with inputs",
    )
    toy.branch("docs", {"README.md": "# toy, documented\n"})
    toy.branch("bad-docs", {"docs/x.md": "forbidden\n"}, base="main")
    toy.write({"rules/forbidden.txt": "forbidden\n"})
    # bad-docs breaks fitness only when docs are scanned: make fitness read them.
    toy.commit(
        {
            "scripts/fitness.py": Path(toy.repo / "scripts/fitness.py")
            .read_text()
            .replace(
                'for root in ("src", "crates"):',
                'for root in ("src", "crates", "docs"):',
            )
        },
        "fitness reads docs",
    )
    status, body = cli(toy, "--branches", "docs", "bad-docs")
    assert status == 1
    assert {b["branch"]: b["verdict"] for b in body["branches"]} == {
        "docs": "joined",
        "bad-docs": "ejected",
    }
    # Three trials (joint, docs, bad-docs), one build: src/ never changed.
    assert len(body["trials"]) == 3 and counted(toy) == ["build"]
    joined = step_of(body, "docs", "build")
    assert joined["verdict"] == "reused"
    assert joined["detail"]["reused"]["run"] == body["run"]
    assert joined["detail"]["reused"]["trial"] == 1
    first_run = body["run"]
    # A later run, on a branch that leaves src/ alone, reuses it again, and
    # the publish step gets the tested output restored (no rebuild).
    toy.branch("docs-2", {"README.md": "# toy, again\n"})
    status, body = cli(toy, "--branches", "docs-2", "--promote")
    assert status == 0, body
    assert step_of(body, "docs-2", "build")["detail"]["reused"]["run"] == first_run
    assert counted(toy) == ["build"]
    published = Path(os.environ["TOY_PUBLISH_OUT"]) / "app.txt"
    assert published.read_text() == "one\ntwo\nthree\n"
    # A change to an input runs the step.
    toy.branch("src", {"src/shared.txt": "changed\n"})
    status, body = cli(toy, "--branches", "src")
    assert status == 0 and counted(toy) == ["build", "build"]
    assert step_of(body, "src", "build")["verdict"] == "passed"


# ---------------------------------------------------------- build hosts


def host_spec() -> str:
    return f'''\
import sys

from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.train import Step, Train

train = Train(
    "toy",
    base="main",
    tested="tested",
    builds=DevBuilds(
        node=None,
        image="{IMAGE}",
        hosts=[BuildHost.docker("server", slots=2)],
        profiles=[DevProfile("sh", tools=["sh"], toolchain=None)],
    ),
    hosts=["server"],
    steps=[
        Step.command(
            "remote",
            [sys.executable, "scripts/remote.py"],
            env={{"STEP_OWN": "yes"}},
            inputs=["src/**", "scripts/remote.py"],
            outputs=["dist/remote.json"],
        ),
        Step.command("fitness", [sys.executable, "scripts/fitness.py"], hosts=["local"]),
        Step.publish("publish", [sys.executable, "scripts/publish.py"], hosts=["local"]),
    ],
)
'''


REMOTE = """\
import json, os, pathlib
pathlib.Path("dist").mkdir(exist_ok=True)
seen = {k: os.environ.get(k) for k in ("PICELI_TRAIN", "PICELI_TRAIN_BRANCHES", "STEP_OWN", "TOY_SECRET")}
pathlib.Path("dist/remote.json").write_text(json.dumps(seen))
print("remote ran", json.dumps(seen))
"""


def host_run(
    toy: Toy,
    tmp_path: Path,
    *branches: str,
    up: bool = True,
    ports: list[LocalPort] | None = None,
) -> dict[str, Any]:
    loaded, repo, spec_path = locate(toy.spec, toy.repo)
    made = ports if ports is not None else []

    def port(host: Any, _dev: Any) -> LocalPort:
        made.append(LocalPort(tmp_path / "hosts" / host.name))
        return made[-1]

    def probe(host: Any) -> HostState:
        return HostState(host, up, "" if up else "engine does not answer",
                         platforms=("linux/amd64",))  # fmt: skip

    runner = build_runner(
        loaded.train,
        repo,
        port=port,
        probe=probe,
    )
    run = TrainRun(
        loaded, repo=repo, spec_path=spec_path, branches=list(branches),
        state=TrainState.for_train("toy", toy.root / "state"), runner=runner,
    )  # fmt: skip
    return run.run()


def test_a_step_runs_on_its_build_host_with_only_the_train_variables(
    toy: Toy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOY_SECRET", "never-sent")
    toy.commit({"train_spec.py": host_spec(), "scripts/remote.py": REMOTE}, "hosts")
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    ports: list[LocalPort] = []
    body = host_run(toy, tmp_path, "feat-a", ports=ports)
    assert body["ok"], body
    remote = step_of(body, "feat-a", "remote")
    assert remote["host"] == "server" and remote["verdict"] == "passed"
    assert step_of(body, "feat-a", "fitness")["host"] == "local"
    log = Path(remote["log"]).read_text()
    assert '"STEP_OWN": "yes"' in log and '"PICELI_TRAIN": "toy"' in log
    assert '"PICELI_TRAIN_BRANCHES": "feat-a"' in log
    # The run the host got carries the train's and the step's variables,
    # never the caller's environment.
    (job,) = [job for port in ports for job in port.jobs.values()]
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    sent = json.loads(next(e["value"] for e in env if e["name"] == "PICELI_DEV_SPEC"))
    assert sent["command"][0] == "env" and "STEP_OWN=yes" in sent["command"]
    assert "PICELI_TRAIN=toy" in sent["command"]
    assert "never-sent" not in json.dumps(job)
    assert sent["lineage_key"].endswith("-sh")  # the host's warm lineage
    # The output came back from the host and is in the content store.
    assert remote["detail"]["stored"] == 1
    # The same inputs on another run: reused, naming the host that made it.
    # feat-b builds on feat-a: its merge has the same src/ as feat-a's had.
    toy.branch("feat-b", {"README.md": "# b\n"}, base="feat-a")
    again = host_run(toy, tmp_path, "feat-b")
    reused = step_of(again, "feat-b", "remote")
    assert reused["verdict"] == "reused"
    assert reused["detail"]["reused"]["host"] == "server"


def test_a_down_build_host_falls_back_to_the_laptop(toy: Toy, tmp_path: Path) -> None:
    toy.commit({"train_spec.py": host_spec(), "scripts/remote.py": REMOTE}, "hosts")
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    body = host_run(toy, tmp_path, "feat-a", up=False)
    assert body["ok"], body
    remote = step_of(body, "feat-a", "remote")
    assert remote["host"] == "local"
    assert any("fallback local" in note for note in remote["detail"]["placement"])


# --------------------------------------------------------- parallel halves


# Two whole trains of six branches (25 trials, each starting Python steps):
# about 15 s alone, 90 s beside two suites, past the suite's 120 s hang
# guard on a machine three times as busy. The guard stays, sized to the work.
@pytest.mark.timeout(480)
def test_parallel_halves_reach_the_sequential_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    results = {}
    for mode in ("off", "on"):
        (tmp_path / mode).mkdir()
        toy = make_toy(tmp_path / mode)
        toy.commit(
            {
                "train_spec.py": Path(toy.repo / "train_spec.py")
                .read_text()
                .replace(
                    'tested="tested",',
                    f'tested="tested", parallel="{mode}", local_slots=2,',
                )
            },
            f"parallel {mode}",
        )
        for name in ("a", "b", "c", "d", "e", "f"):
            broken = name in ("b", "e")
            toy.branch(name, {f"src/{name}.txt": "BROKEN\n" if broken else f"{name}\n"})
        status, body = cli(toy, "--branches", "a", "b", "c", "d", "e", "f")
        assert status == 1
        results[mode] = body
    verdict = {
        mode: {b["branch"]: b["verdict"] for b in body["branches"]}
        for mode, body in results.items()
    }
    assert verdict["on"] == verdict["off"] == {
        "a": "joined", "b": "ejected", "c": "joined",
        "d": "joined", "e": "ejected", "f": "joined",
    }  # fmt: skip
    assert any(t.get("speculative") for t in results["on"]["trials"])
    assert not any(t.get("speculative") for t in results["off"]["trials"])
    # Joined branches keep the order given.
    head = results["on"]["head"]
    log = make_log(tmp_path / "on" / "repo", head)
    assert [line for line in log if line in "acdf"] == ["a", "c", "d", "f"]


def make_log(repo: Path, head: str) -> list[str]:
    from tests.train_toy import git

    out = git(repo, "log", "--reverse", "--merges", "--format=%s", head)
    return [line.split("'")[1] for line in out.splitlines() if "'" in line]


# ------------------------------------------------- kept head, promote-run


def test_the_last_head_is_kept_and_promote_run_promotes_it(toy: Toy) -> None:
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    status, body = cli(toy, "--branches", "feat-a")
    assert status == 0
    assert toy.rev("refs/piceli/train/toy/last") == body["head"]
    assert body["last"]["ref"] == "refs/piceli/train/toy/last"
    assert toy.rev("main") == body["start"]  # nothing moved
    status, promoted = cli(toy, "--promote-run", body["run"])
    assert status == 0, promoted
    assert promoted["promoted_from"] == body["run"]
    assert promoted["promote"]["state"] == "promoted"
    assert toy.rev("main") == body["head"] == toy.rev("tested")
    assert (
        Path(os.environ["TOY_PUBLISH_OUT"])
        / promoted["promote"]["tag"].replace("/", "_")
    ).exists()


def test_promote_run_refuses_a_base_that_moved_elsewhere(toy: Toy) -> None:
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    status, body = cli(toy, "--branches", "feat-a")
    assert status == 0
    toy.commit({"src/direct.txt": "pushed by hand\n"}, "outside the train")
    status, refused = cli(toy, "--promote-run", "last")
    assert status == 2 and refused["reason"] == "train-not-fast-forward"
    status, unknown = cli(toy, "--promote-run", "20990101T000000Z-abcdef")
    assert status == 2 and unknown["reason"] == "train-run-unknown"


# ----------------------------------------------------- dev-target impact


def _metadata(root: Path) -> dict[str, Any]:
    def package(
        name: str, deps: list[tuple[str, str | None]], tests: list[str]
    ) -> dict[str, Any]:
        base = root / "crates" / name
        return {
            "id": f"{name} 0.1.0",
            "name": name,
            "manifest_path": str(base / "Cargo.toml"),
            "dependencies": [
                {"name": dep, "kind": kind, "path": str(root / "crates" / dep)}
                for dep, kind in deps
            ],
            "targets": [
                {"name": name, "kind": ["lib"], "src_path": str(base / "src/lib.rs")},
                *(
                    {
                        "name": t,
                        "kind": ["test"],
                        "src_path": str(base / "tests" / f"{t}.rs"),
                    }
                    for t in tests
                ),
            ],
        }

    packages = [
        package("core-lib", [], []),
        package("api", [("core-lib", "dev")], ["uses_core", "other"]),
        package("web", [("core-lib", "dev")], ["e2e"]),
    ]
    return {
        "workspace_root": str(root),
        "workspace_members": [p["id"] for p in packages],
        "packages": packages,
    }


def test_dev_targets_narrow_dev_dependency_users_to_tests_naming_the_library(
    tmp_path: Path,
) -> None:
    files = {
        "crates/core-lib/src/lib.rs": "pub fn f() {}\n",
        "crates/api/src/lib.rs": "pub fn g() {}\n",
        "crates/api/tests/uses_core.rs": "use core_lib::f;\n",
        "crates/api/tests/other.rs": "#[test] fn t() {}\n",
        "crates/web/src/lib.rs": "#[cfg(test)] mod t { use core_lib::f; }\n",
        "crates/web/tests/e2e.rs": "use core_lib::f;\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    meta = _metadata(tmp_path)
    changed = ["crates/core-lib/src/lib.rs"]
    assert select(changed, meta, ()) == ["api", "core-lib", "web"]
    reasons: list[str] = []
    narrow = select(changed, meta, (), reasons.append, dev_targets=True)
    # web's own unit tests name the library: the whole member stays.
    assert narrow == ["api:uses_core", "core-lib", "web"]
    assert any("api --test uses_core" in line for line in reasons)
    assert cargo_test_arguments(narrow) == [
        ["-p", "core-lib", "-p", "web"],
        ["-p", "api", "--test", "uses_core"],
    ]
