"""0.20: development runs on declared build hosts (docker, native, cluster).

The native port runs the real wrapper as a process of this machine; the
docker port is checked through its container arguments (a real engine is
the opt-in ``tests/integration/test_dev_docker_host.py``); the queue and
the choice of host are pure.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from piceli.dev.client import RunRequest, run
from piceli.dev.hosts import (
    DockerPort,
    Entry,
    FileQueue,
    HostState,
    NativePort,
    acquire,
    candidates,
    queue_position,
)
from piceli.dev.model import BuildHost, DevBuilds, DevError, DevProfile
from piceli.dev.pack import SourceRequest
from piceli.infra.cluster import ClusterError
from tests.unit.dev.test_pack import repo

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
PROFILES = [DevProfile("sh", tools=["sh"], toolchain=None)]


def _dev(*hosts: BuildHost, node: str | None = None) -> DevBuilds:
    return DevBuilds(node=node, image=IMAGE, profiles=PROFILES, hosts=list(hosts))


# ------------------------------------------------------------------ model


def test_hosts_are_declared_in_order_and_the_node_is_the_first_by_default() -> None:
    laptop = BuildHost.docker("laptop", slots=2, memory="6Gi")
    mini = BuildHost.docker("mini", context="ssh://builder@mini.tail1.ts.net")
    dev = _dev(laptop, mini, node="builder-1")
    assert [(h.name, h.kind) for h in dev.hosts] == [
        ("builder-1", "cluster"), ("laptop", "docker"), ("mini", "docker"),
    ]  # fmt: skip
    # Placed explicitly, the cluster host keeps its place.
    later = _dev(laptop, BuildHost.cluster("builder-1"), node="builder-1")
    assert [h.name for h in later.hosts] == ["laptop", "builder-1"]
    # Round trip through the cluster's config.
    assert DevBuilds.from_dict(dev.describe()).hosts == dev.hosts
    hostless = _dev(laptop)
    assert hostless.node is None and hostless.hosts == (laptop,)


@pytest.mark.parametrize(
    "build",
    [
        lambda: BuildHost.docker("Bad_Name"),
        lambda: BuildHost.docker("x", context="not a url"),
        lambda: BuildHost.native("x", context="ssh://a@b"),
        lambda: BuildHost.docker("x", slots=0),
        lambda: BuildHost.docker("x", platforms=["darwin/arm64"]),
        lambda: BuildHost.docker("x", image="example/builder:latest"),
        lambda: BuildHost("x", "cluster"),
        lambda: _dev(),
        lambda: _dev(BuildHost.cluster("other"), node="builder-1"),
        lambda: _dev(BuildHost.docker("a"), BuildHost.native("a")),
    ],
)
def test_bad_hosts_are_refused(build: Any) -> None:
    with pytest.raises(ClusterError):
        build()


# ------------------------------------------------------------------ queue


def _entry(
    run: str, requester: str = "a", priority: str = "agent", at: str = ""
) -> Entry:
    return Entry(run, requester, priority, at or f"2026-10-17T10:00:00.{run[-3:]}Z")


def test_the_host_queue_keeps_the_schedulers_order() -> None:
    waiting = [
        _entry("r-001", "agent-1"),
        _entry("r-002", "agent-1"),
        _entry("r-003", "agent-2"),
        _entry("r-004", "coord", "round"),
    ]
    running = [Entry("r-000", "agent-1", "agent", "", 0)]
    order = [queue_position(waiting, running, e.run) for e in waiting]
    # The round first; then agent-2 (agent-1 already holds a slot); then age.
    assert order == [2, 3, 1, 0]


def test_file_slots_are_exclusive_and_a_dead_waiter_is_dropped(tmp_path: Path) -> None:
    queue = FileQueue(tmp_path / "q")
    first, second = _entry("r-001"), _entry("r-002")
    queue.register(first)
    assert queue.claim(0, first) and not FileQueue(tmp_path / "q").claim(0, second)
    assert [e.run for e in FileQueue(tmp_path / "q").running()] == ["r-001"]
    (tmp_path / "q" / "waiting" / "r-009.json").write_text(
        json.dumps({**_entry("r-009").__dict__, "pid": 2**22 + 7})
    )
    assert "r-009" not in [e.run for e in queue.waiting()]
    queue.release(0)
    assert FileQueue(tmp_path / "q").running() == []


def test_acquire_waits_its_turn_and_times_out(tmp_path: Path) -> None:
    queue = FileQueue(tmp_path / "q")
    holder = _entry("r-001")
    assert queue.claim(0, holder)
    late = _entry("r-002")
    queue.register(late)
    clock = {"now": 0.0}
    seen: list[str] = []

    def tick(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] >= 3:
            queue.release(0)

    slot = acquire(
        queue, late, 1, lambda n: queue.claim(n, late),
        timeout=60, on_wait=seen.append, clock=lambda: clock["now"], sleep=tick,
    )  # fmt: skip
    assert slot == 0 and seen and seen[0].startswith("queued #1 of 1")
    with pytest.raises(DevError) as raised:
        acquire(
            queue, _entry("r-003"), 1, lambda n: False,
            timeout=2, on_wait=lambda _s: None, clock=lambda: clock["now"], sleep=tick,
        )  # fmt: skip
    assert raised.value.code == "dev-queue-timeout"


# ------------------------------------------------------------------ choosing


def _state(
    host: BuildHost, up: bool = True, running: int = 0, waiting: int = 0
) -> HostState:
    return HostState(
        host, up, "down" if not up else "", ("linux/arm64",), running, waiting
    )


def test_candidates_skip_down_hosts_and_prefer_a_free_slot() -> None:
    builder = BuildHost.cluster("builder-1")
    laptop = BuildHost.docker("laptop")
    mini = BuildHost.docker("mini", context="ssh://builder@mini")
    dev = _dev(builder, laptop, mini, node="builder-1")
    states = {
        "builder-1": _state(builder, up=False),
        "laptop": _state(laptop, running=1),
        "mini": _state(mini),
    }
    chosen = candidates(dev, probe=lambda h: states[h.name])
    assert [s.host.name for s in chosen] == ["mini", "laptop"]
    assert [
        s.host.name
        for s in candidates(dev, requested="laptop", probe=lambda h: states[h.name])
    ] == ["laptop"]
    with pytest.raises(DevError) as unknown:
        candidates(dev, requested="nope", probe=lambda h: states[h.name])
    assert unknown.value.code == "dev-host-unknown"
    with pytest.raises(DevError) as none:
        candidates(dev, probe=lambda h: _state(h, up=False))
    assert none.value.code == "dev-host-unavailable" and "builder-1: down" in str(
        none.value
    )


def test_a_publish_only_host_takes_no_run_unless_named() -> None:
    from piceli.infra.cluster import ClusterError
    from piceli.k8s.cli.dev_nix import _host

    publisher = BuildHost.docker(
        "nix-builder", context="ssh://builder@builder-1.example.org", runs=False
    )
    laptop = BuildHost.docker("laptop")
    dev = _dev(publisher, laptop)
    states = {"nix-builder": _state(publisher), "laptop": _state(laptop, running=1)}
    probe = lambda h: states[h.name]  # noqa: E731
    assert [s.host.name for s in candidates(dev, probe=probe)] == ["laptop"]
    named = candidates(dev, requested="nix-builder", probe=probe)
    assert [s.host.name for s in named] == ["nix-builder"]
    with pytest.raises(DevError) as none:
        candidates(_dev(publisher), probe=probe)
    assert none.value.code == "dev-host-unavailable"
    # dev nix build's default host skips it too; --host still names it.
    assert _host(dev, None).name == "laptop"
    assert _host(dev, "nix-builder").name == "nix-builder"
    # Declared: in the plan (only when False, so other hashes stay) and in
    # dev hosts.
    assert publisher.describe()["runs"] is False
    assert "runs" not in laptop.describe()
    assert _state(publisher).describe()["runs"] is False
    assert _state(laptop).describe()["runs"] is True
    with pytest.raises(ClusterError):
        BuildHost.cluster("builder-1", runs=False)
    with pytest.raises(ClusterError):
        BuildHost.docker("laptop", runs="no")  # type: ignore[arg-type]


# ------------------------------------------------------------------ native


def test_a_native_host_runs_the_wrapper_here_and_queues_on_files(
    tmp_path: Path,
) -> None:
    app = repo(tmp_path / "shop", {"src/a.txt": "hello\n", "t.sh": "cat src/a.txt"})
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    dev = _dev(host)
    lines: list[str] = []
    result = run(
        NativePort(host, dev), dev,
        RunRequest(SourceRequest("shop", app, None), ["sh", "t.sh"], requester="agent-1"),
        log=lines.append, host=host,
    )  # fmt: skip
    assert result["state"] == "passed", result
    assert "hello" in lines
    assert result["host"] == {"name": "laptop", "kind": "native", "slots": 1,
                              "state_dir": str(tmp_path / "state")}  # fmt: skip
    assert result["node"] is None
    # The scratch goes; the cache (a lineage) stays for the next run.
    assert not list((tmp_path / "state" / "work").glob("*"))
    assert list((tmp_path / "state" / "cache" / "lineages").glob("shop-sh*/*"))
    again = run(
        NativePort(host, dev), dev,
        RunRequest(SourceRequest("shop", app, None), ["true"]), host=host,
    )  # fmt: skip
    assert again["cache"]["warm"] is True


def test_native_runs_share_the_hosts_slots(tmp_path: Path) -> None:
    app = repo(tmp_path / "shop", {"t.sh": "sleep 1"})
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"), slots=1)
    dev = _dev(host)
    waits: list[str] = []
    results: list[dict[str, Any]] = []

    def one(name: str) -> None:
        results.append(
            run(
                NativePort(host, dev, poll=0.05),
                dev,
                RunRequest(
                    SourceRequest("shop", app, "HEAD"), ["sh", "t.sh"], requester=name
                ),
                say=waits.append,
                host=host,
            )
        )

    threads = [threading.Thread(target=one, args=(f"agent-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert [r["state"] for r in results] == ["passed", "passed"]
    assert any("queued #1 of 1 on laptop (1/1 busy)" in line for line in waits)


# ------------------------------------------------------------------ docker


class FakeCli:
    """Records the container CLI's argv; answers like an engine with nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_the_docker_port_starts_a_bounded_container_in_its_slot() -> None:
    from piceli.dev.hosts import ContainerCli
    from piceli.dev.jobs import run_job

    host = BuildHost.docker("mini", context="ssh://builder@mini", memory="6Gi", cpu="4")
    dev = _dev(host)
    fake = FakeCli()
    port = DockerPort(
        host, dev, cli=ContainerCli(host, runner=fake), platform="linux/amd64"
    )
    job = run_job(
        dev, dev.profile(None), run="r-1",
        spec={"command": ["true"], "cwd": "shop", "lineage_key": "shop-sh", "sources": []},
        requester="agent-1", priority="round",
    )  # fmt: skip
    port.create(job)
    entry = Entry("r-1", "agent-1", "round", "2026-10-17T10:00:00.000Z")
    assert port._claim(2, entry, job, IMAGE)
    argv = fake.calls[-1]
    assert argv[:3] == ["docker", "--host", "ssh://builder@mini"]
    assert argv[3:7] == ["run", "-d", "--name", "piceli-dev-mini-2"]
    joined = " ".join(argv)
    for flag in (
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 65532:65532",
        "-v piceli-dev-cache:/cache",
        "--platform linux/amd64",
        "--cpus 4",
        f"--memory {6 * 2**30}",
        "piceli.io/dev-slot=2",
        "piceli.io/dev-priority=round",
    ):
        assert flag in joined, flag  # fmt: skip
    spec = json.loads(next(a for a in argv if a.startswith("PICELI_DEV_SPEC="))[16:])
    assert spec["isolation_check"] is False
    assert (
        argv[-4:-1] == ["sh", IMAGE, "-c"] and "PICELI_DEV_ROLE=fetch" not in argv[-1]
    )


def test_a_cli_missing_makes_the_host_unavailable() -> None:
    from piceli.dev.hosts import ContainerCli, probe

    host = BuildHost.docker("ghost", cli="no-such-container-cli-xyz")
    state = probe(host)
    assert state.up is False and "not installed" in state.detail
    with pytest.raises(DevError) as raised:
        ContainerCli(host).platform()
    assert raised.value.code == "dev-host-unavailable"


def test_cli_runs_on_a_native_host_without_any_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import piceli.k8s.cli.dev as dev_cli
    from piceli.infra import Cluster, Node
    from piceli.k8s.cli import app

    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("pi00", arch="arm64", roles=["workloads"])],
        dev=_dev(host),
    )  # fmt: skip
    monkeypatch.setattr(dev_cli, "load_cluster", lambda ref: cluster)
    shop = repo(tmp_path / "shop", {"t.sh": "echo on-the-laptop"})
    monkeypatch.chdir(shop)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = CliRunner().invoke(
        app, ["dev", "run", "--cluster", "infra.py:c", "--json", "--", "sh", "t.sh"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "passed" and body["host"]["name"] == "laptop"
    assert "on-the-laptop" in result.stderr
    assert os.environ.get("PICELI_DEV_HOST") is None


def test_cache_clear_on_a_native_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import piceli.k8s.cli.dev as dev_cli
    from piceli.infra import Cluster, Node
    from piceli.k8s.cli import app

    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("pi00", arch="arm64", roles=["workloads"])], dev=_dev(host),
    )  # fmt: skip
    monkeypatch.setattr(dev_cli, "load_cluster", lambda ref: cluster)
    target = tmp_path / "state" / "cache" / "lineages" / "shop-sh" / "0" / "target"
    (target / "debug" / "deps").mkdir(parents=True)
    (target / "debug" / "deps" / "libshop-1.rlib").write_text("x")
    result = CliRunner().invoke(
        app,
        [
            "dev",
            "cache",
            "clear",
            "--cluster",
            "infra.py:c",
            "--host",
            "laptop",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["cleared"][0]["lineage"] == "shop-sh/0"
    assert not target.exists()


def test_a_native_run_keeps_the_users_cargo_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its crates are already there, and ``cargo --offline`` keeps working."""
    app = repo(tmp_path / "shop", {"t.sh": 'echo "home=${CARGO_HOME:-unset}"'})
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    dev = _dev(host)
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "my-cargo"))
    lines: list[str] = []
    result = run(
        NativePort(host, dev), dev,
        RunRequest(SourceRequest("shop", app, None), ["sh", "t.sh"]),
        log=lines.append, host=host,
    )  # fmt: skip
    assert result["state"] == "passed"
    assert f"home={tmp_path / 'my-cargo'}" in lines
