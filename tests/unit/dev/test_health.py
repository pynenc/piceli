"""0.22: build host health events and quarantine (no cluster, no engine).

A native host keeps its events in a file, a docker host as labelled volumes
(checked here through a fake CLI). After N events within the window the
host is quarantined: ``candidates`` skips it, ``dev run`` says why and
``dev release-host`` clears it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import pod
from piceli.dev.health import (
    DockerHealth,
    FileHealth,
    Health,
    HealthEvent,
    quarantine,
)
from piceli.dev.hosts import ContainerCli, HostState, candidates
from piceli.dev.model import BuildHost, DevBuilds, DevError, DevProfile
from piceli.infra.cluster import ClusterError

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
PROFILES = [DevProfile("sh", tools=["sh"], toolchain=None)]
DAY = 86400.0


def _dev(*hosts: BuildHost) -> DevBuilds:
    return DevBuilds(node=None, image=IMAGE, profiles=PROFILES, hosts=list(hosts))


# ------------------------------------------------------------------ signals


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("error: internal compiler error: unexpected panic", "internal-compiler-error"),
        ("thread 'rustc' panicked at compiler/rustc_middle", "internal-compiler-error"),
        ("rust-lld: error: undefined symbol: _ZN4core3fmt", "link-symbol"),
        ("ld: undefined hidden symbol `foo' in libbar.a", "link-symbol"),
        (
            "collect2: error: ld terminated with signal 11 [Segmentation fault]",
            "segfault",
        ),
        ("process didn't exit successfully (signal: 11, SIGSEGV)", "segfault"),
        (
            "sh: line 1: 4242 Illegal instruction (core dumped) ./a",
            "illegal-instruction",
        ),
        (
            "error: could not compile (signal: 7, SIGBUS: access to undefined memory)",
            "bus-error",
        ),
        ("[ 12.3] mce: [Hardware Error]: Machine check events logged", "machine-check"),
        ("[piceli] cache-corrupt: blob sha256:ab discarded", "cache-corrupt"),
        (
            "[piceli] linux/amd64 command 1/2 (cargo): corrupted cache (link): cleared",
            "cache-repaired",
        ),
    ],
)
def test_machine_health_signatures(line: str, kind: str) -> None:
    found = pod.health_signal(["Compiling serde v1.0", line])
    assert found is not None and found["kind"] == kind
    assert found["line"] == line.strip()


def test_ordinary_failures_have_no_signature() -> None:
    lines = ["error[E0308]: mismatched types", "test result: FAILED. 1 failed"]
    assert pod.health_signal(lines) is None


# ------------------------------------------------------------------ policy


def _events(*ats: float, kind: str = "segfault") -> list[HealthEvent]:
    return [HealthEvent(at, kind, f"event at {at}") for at in ats]


def test_two_events_within_a_day_quarantine_and_older_ones_do_not() -> None:
    now = 10 * DAY
    assert quarantine(_events(now - 100), after=2, window=DAY, now=now) is None
    found = quarantine(_events(now - 2 * DAY, now - 100), after=2, window=DAY, now=now)
    assert found is None  # the first one is out of the window
    found = quarantine(_events(now - 3000, now - 100), after=2, window=DAY, now=now)
    assert found is not None
    assert found["events"] == 2 and found["kinds"] == ["segfault"]
    assert "2 health events within 24h" in found["reason"]
    assert (
        quarantine(_events(now - 1, now - 2), after=None, window=DAY, now=now) is None
    )


def test_the_policy_is_declared_on_the_host() -> None:
    host = BuildHost.docker("mini", quarantine_after=3, quarantine_window="2h")
    assert host.quarantine_policy() == (3, 7200)
    assert host.describe()["quarantine_after"] == 3
    assert BuildHost.from_dict(host.describe()) == host
    # The defaults keep earlier declarations' descriptions (and hashes).
    assert "quarantine_after" not in BuildHost.docker("mini").describe()
    off = BuildHost.docker("mini", quarantine_after=None)
    assert BuildHost.from_dict(off.describe()).quarantine_after is None
    for bad in ({"quarantine_after": 0}, {"quarantine_window": "soon"}):
        with pytest.raises(ClusterError):
            BuildHost.docker("mini", **bad)


# ------------------------------------------------------------------ stores


def test_a_native_hosts_events_live_in_its_state_dir(tmp_path: Path) -> None:
    host = BuildHost.native("mac", state_dir=str(tmp_path / "state"))
    clock = {"now": 5 * DAY}
    health = Health.of(host, clock=lambda: clock["now"])
    assert health is not None and isinstance(health.store, FileHealth)
    assert (
        health.record({"kind": "segfault", "line": "Segmentation fault"}, "dev-run")
        is None
    )
    clock["now"] += 600
    found = health.record({"kind": "internal-compiler-error", "line": "ICE"}, "dev-run")
    assert found is not None and found["kinds"] == [
        "internal-compiler-error",
        "segfault",
    ]
    saved = json.loads((tmp_path / "state" / "health.json").read_text())
    assert [item["source"] for item in saved["events"]] == ["dev-run", "dev-run"]
    # Another client of the same host sees the same record.
    assert Health.of(host, clock=lambda: clock["now"]).quarantined() is not None  # type: ignore[union-attr]
    clock["now"] += DAY + 1
    assert health.quarantined() is None  # both left the window
    assert health.release() == 2 and health.store.events() == []


class FakeEngine:
    """A container CLI that keeps volumes (name, labels) like an engine."""

    def __init__(self) -> None:
        self.volumes: dict[str, dict[str, str]] = {}
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        args = argv[argv.index("volume") + 1 :] if "volume" in argv else []
        out = b""
        if args[:1] == ["create"]:
            labels = {}
            for index, value in enumerate(args):
                if value == "--label":
                    key, _, text = args[index + 1].partition("=")
                    labels[key] = text
            self.volumes[args[-1]] = labels
        elif args[:1] == ["ls"]:
            wanted = [args[i + 1][6:] for i, a in enumerate(args) if a == "--filter"]
            lines = []
            for name, labels in sorted(self.volumes.items()):
                ok = all(
                    (labels.get(w.partition("=")[0]) == w.partition("=")[2])
                    if "=" in w
                    else w in labels
                    for w in wanted
                )
                if ok:
                    text = ",".join(f"{k}={v}" for k, v in labels.items())
                    lines.append(f"{name}\t{text}")
            out = "\n".join(lines).encode()
        elif args[:1] == ["rm"]:
            for name in args[1:]:
                self.volumes.pop(name, None)
        return subprocess.CompletedProcess(argv, 0, out, b"")


def test_a_docker_hosts_events_are_labelled_volumes_on_its_engine() -> None:
    host = BuildHost.docker("mini", context="ssh://builder@mini")
    engine = FakeEngine()
    cli = ContainerCli(host, runner=engine)
    clock = {"now": 3 * DAY}
    health = Health.of(host, cli, clock=lambda: clock["now"])
    assert health is not None and isinstance(health.store, DockerHealth)
    health.record(
        {"kind": "segfault", "line": "cc1plus: Segmentation fault, again"},
        "builder-serve",
    )
    clock["now"] += 60
    found = health.record({"kind": "machine-check", "line": "mce: CPU 3"}, "dev-run")
    assert found is not None
    create = next(c for c in engine.calls if "create" in c)
    assert create[:3] == ["docker", "--host", "ssh://builder@mini"]
    assert all(name.startswith("piceli-health-mini-") for name in engine.volumes)
    events = sorted(health.store.events(), key=lambda e: e.at)
    # The comma could not survive the CLI's k=v,k=v listing: it is replaced.
    assert events[0].detail == "cc1plus: Segmentation fault; again"
    assert [e.source for e in events] == ["builder-serve", "dev-run"]
    # Another host's volumes are not this host's events.
    engine.volumes["piceli-health-other-1"] = {
        "piceli.io/dev-host": "other", "piceli.io/build-health": "segfault",
        "piceli.io/build-health-at": str(clock["now"]),
    }  # fmt: skip
    assert len(health.store.events()) == 2
    assert health.release() == 2
    assert list(engine.volumes) == ["piceli-health-other-1"]


# ------------------------------------------------------------------ choosing


def _state(host: BuildHost, quarantined: bool = False) -> HostState:
    health = {"quarantined": quarantined, "reason": "2 health events within 24h"}
    return HostState(host, True, "", ("linux/amd64",), health=health)


def test_candidates_skip_a_quarantined_host_with_its_reason() -> None:
    bad = BuildHost.docker("bad")
    good = BuildHost.docker("good")
    dev = _dev(bad, good)
    states = {"bad": _state(bad, True), "good": _state(good)}
    chosen = candidates(dev, probe=lambda h: states[h.name])
    assert [s.host.name for s in chosen] == ["good"]
    assert chosen[0].describe()["quarantined"] is False
    with pytest.raises(DevError) as refused:
        candidates(dev, requested="bad", probe=lambda h: states[h.name])
    assert refused.value.code == "dev-host-unavailable"
    assert "bad: quarantined: 2 health events" in str(refused.value)
    assert "piceli dev release-host bad" in str(refused.value)


# ------------------------------------------------------------------ the wrapper


def _run_failing(tmp_path: Path, script: str) -> dict[str, Any]:
    from tests.unit.dev.test_pod import _archive, _run, _spec

    data = _archive({"app/a.txt": "x"})
    result, _ = _run(tmp_path, data, _spec(data, ["sh", "-c", script]))
    return result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_a_failed_run_with_a_segfault_reports_a_health_signal(tmp_path: Path) -> None:
    result = _run_failing(
        tmp_path,
        "echo 'collect2: fatal error: ld terminated with signal 11 [Segmentation fault]'; exit 1",
    )
    assert result["health"]["kind"] == "segfault" and not result["health"]["repaired"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")
def test_a_link_error_alone_is_the_codes_not_the_hosts(tmp_path: Path) -> None:
    result = _run_failing(
        tmp_path, "echo 'error: undefined symbol: my_extern_fn'; exit 1"
    )
    assert result["state"] == "failed" and "health" not in result


# ------------------------------------------------------------------ the CLI


def _cluster(host: BuildHost) -> Any:
    from piceli.infra import Cluster, Node

    return Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("pi00", arch="arm64", roles=["workloads"])],
        dev=_dev(host),
    )  # fmt: skip


def test_dev_run_skips_a_quarantined_host_and_release_host_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import piceli.k8s.cli.dev as dev_cli
    from piceli.k8s.cli import app
    from tests.unit.dev.test_pack import repo

    host = BuildHost.native("mac", state_dir=str(tmp_path / "state"))
    monkeypatch.setattr(dev_cli, "load_cluster", lambda ref: _cluster(host))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    shop = repo(tmp_path / "shop", {"t.sh": "echo 'Illegal instruction'; exit 4"})
    monkeypatch.chdir(shop)
    runner = CliRunner()
    argv = ["dev", "run", "--cluster", "infra.py:c", "--json", "--", "sh", "t.sh"]
    first = runner.invoke(app, argv)
    assert first.exit_code == 1
    assert json.loads(first.stdout)["health"]["kind"] == "illegal-instruction"
    assert "health event on build host mac: illegal-instruction" in first.stderr
    second = runner.invoke(app, argv)
    assert json.loads(second.stdout)["host_quarantined"]["events"] == 2
    assert "build host mac is quarantined" in second.stderr
    third = runner.invoke(app, argv)
    assert third.exit_code == 2  # no host can take it
    assert "quarantined" in third.stderr and "release-host mac" in third.stderr
    hosts = runner.invoke(app, ["dev", "hosts", "--cluster", "infra.py:c", "--json"])
    (shown,) = json.loads(hosts.stdout)["hosts"]
    assert shown["quarantined"] is True and shown["health"]["recent"] == 2
    assert "QUARANTINED" in hosts.stderr
    released = runner.invoke(
        app, ["dev", "release-host", "mac", "--cluster", "infra.py:c", "--json"]
    )
    assert released.exit_code == 0, released.stderr
    assert json.loads(released.stdout) == {
        "schema": "piceli.dev-host-release.v1", "host": "mac",
        "cleared": 2, "was_quarantined": True,
    }  # fmt: skip
    again = runner.invoke(app, argv)
    assert again.exit_code == 1 and "health" in json.loads(again.stdout)


def test_an_event_checked_at_once_or_from_a_clock_a_little_ahead_counts(
    tmp_path: Path,
) -> None:
    """Rounding up made a just-written event look like the future, and a
    fast check did not count it; another client's clock may run ahead too."""
    at = 1_790_000_000.0006  # rounds up to ...0.001 at 3 decimals
    stored = HealthEvent.from_dict(HealthEvent(at, "segfault").to_dict())
    assert stored.at <= at
    store = FileHealth(tmp_path / "health.json")
    store.record(HealthEvent(at, "segfault"))
    store.record(HealthEvent(at + 120, "machine-check"))  # a clock 2 min ahead
    found = quarantine(store.events(), after=2, window=86400, now=at)
    assert found is not None and found["events"] == 2
