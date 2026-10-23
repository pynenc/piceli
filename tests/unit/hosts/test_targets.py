"""``piceli targets``: one status view of every environment and NixHost (0.24.0)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import targets
from piceli.hosts.model import HostError

PRODUCT, ASSETS, OLD = "a" * 40, "b" * 40, "c" * 40
HOSTS_COMMIT, NEW = "d" * 40, "e" * 40
GATE = {"input": "product", "commit": PRODUCT, "environment": "main",
        "verified_run": "run-7", "verified_at": "2026-10-23T09:00:00Z"}  # fmt: skip


def _status(**releases: Any) -> dict[str, Any]:
    """A controller's ``gitops status`` body: three environments (one on
    two clusters, one unreachable) and the releases given."""
    checks = {"state": "passed", "passed": 3, "total": 3, "at": "2026-10-23T09:01:00Z"}
    return {
        "schema": "piceli.gitops-status.v1",
        "health": "healthy",
        "controller": {"heartbeat_at": "2026-10-23T09:30:00Z"},
        "envs": {
            "main": {"namespace": "shop", "state": "deployed", "health": "healthy",
                     "revision": {"product": PRODUCT, "assets": ASSETS},
                     "checks": checks},
            "rc": {"namespace": "shop-rc", "state": "failed",
                   "reason": "checks-failed-rolled-back",
                   "deployed_revision": {"product": OLD},
                   "checks": {**checks, "state": "failed", "passed": 2}},
            "edge": {"state": "deployed", "clusters": {
                "east": {"state": "deployed", "namespace": "shop",
                         "deployed_revision": {"product": PRODUCT}},
                "west": {"state": "unreachable", "namespace": "shop",
                         "reason": "cluster-unreachable",
                         "last_contact": "2026-10-23T08:00:00Z"}}},
        },
        "nix_releases": releases,
    }  # fmt: skip


def _agent(**changes: Any) -> dict[str, Any]:
    status = {
        "host": "web-1",
        "current": {"commit": HOSTS_COMMIT, "tag": "web-v1.2.0"},
        "poll": {"at": "2026-10-23T09:29:00Z", "state": "current"},
        "last_attempt": {"commit": HOSTS_COMMIT, "tag": "web-v1.2.0",
                         "state": "deployed", "ended_at": "2026-10-23T09:10:00Z",
                         "checks": [{"name": "http-1", "state": "passed"}]},
    }  # fmt: skip
    status.update(changes)
    return status


RELEASE = {"state": "published", "tag": "web-v1.2.0", "commit": HOSTS_COMMIT,
           "published_tag": "web-v1.2.0", "published_commit": HOSTS_COMMIT,
           "finished_at": "2026-10-23T09:05:00Z", "pinned": GATE}  # fmt: skip


def _rows(document: dict[str, Any]) -> dict[tuple[str, str | None], dict[str, Any]]:
    return {(row["name"], row["where"]): row for row in document["targets"]}


def test_every_environment_and_host_has_one_row_with_its_facts() -> None:
    document = targets.build(
        _status(**{"web-1": RELEASE}),
        hosts={"web-1": {"status": _agent()}},
        where={"web-1": "203.0.113.7"},
        now="2026-10-23T09:31:00Z",
    )
    assert document["schema"] == "piceli.targets.v1"
    rows = _rows(document)
    main = rows[("main", "shop")]
    assert main["kind"] == "environment" and main["state"] == "deployed"
    # The gate names the product source: the environment pins its commit.
    assert main["pinned"] == {"source": "product", "commit": PRODUCT,
                              "commits": {"assets": ASSETS, "product": PRODUCT}}  # fmt: skip
    assert main["revision"]["commit"] == PRODUCT
    assert main["checks"] == {"state": "passed", "at": "2026-10-23T09:01:00Z",
                              "passed": 3, "total": 3}  # fmt: skip
    assert main["as_of"] == "2026-10-23T09:30:00Z"
    assert set(main["facts"].values()) == {"controller"}
    rc = rows[("rc", "shop-rc")]
    assert rc["state"] == "rolled-back" and rc["checks"]["state"] == "failed"
    assert rc["pinned"]["commit"] == OLD  # one source: it is the product
    west = rows[("edge", "west/shop")]
    assert west["state"] == "unreachable" and west["reason"] == "cluster-unreachable"
    assert west["as_of"] == "2026-10-23T08:00:00Z"
    assert rows[("edge", "east/shop")]["state"] == "deployed"
    web = rows[("web-1", "203.0.113.7")]
    assert web["kind"] == "nix-host" and web["state"] == "deployed"
    assert web["revision"] == {"commit": HOSTS_COMMIT, "tag": "web-v1.2.0"}
    assert web["pinned"] == GATE  # the release of the commit it runs
    assert web["facts"] == {"revision": "host-agent", "pinned": "controller",
                            "checks": "host-agent", "state": "host-agent"}  # fmt: skip
    assert web["checks"]["state"] == "passed" and web["as_of"] == "2026-10-23T09:29:00Z"
    assert web["release"]["tag"] == "web-v1.2.0"
    assert document["state"] == "failed" and targets.exit_code(document) == 1
    assert document["counts"] == {"deployed": 3, "rolled-back": 1, "unreachable": 1}


def test_without_a_gate_every_source_commit_is_listed_and_pinned_is_null() -> None:
    release = {**RELEASE, "pinned": None}
    document = targets.build(
        _status(**{"web-1": release}), hosts={"web-1": {"status": _agent()}}
    )
    rows = _rows(document)
    main = rows[("main", "shop")]
    assert main["pinned"] == {"source": None, "commit": None,
                              "commits": {"assets": ASSETS, "product": PRODUCT}}  # fmt: skip
    assert rows[("web-1", None)]["pinned"] is None
    # A gate written into the signed manifest only (published.pinned) counts.
    published = {**release, "published": {"pinned": GATE}}
    document = targets.build(
        _status(**{"web-1": published}), hosts={"web-1": {"status": _agent()}}
    )
    assert _rows(document)[("web-1", None)]["pinned"]["verified_run"] == "run-7"


def test_a_host_running_another_build_does_not_borrow_the_release_pin() -> None:
    agent = _agent(current={"commit": NEW, "tag": "web-v1.3.0"})
    document = targets.build(
        _status(**{"web-1": RELEASE}), hosts={"web-1": {"status": agent}}
    )
    assert _rows(document)[("web-1", None)]["pinned"] is None


@pytest.mark.parametrize(
    ("changes", "state", "reason", "checks"),
    [
        ({"hold": {"kind": "pin", "tag": "web-v1.2.0"}}, "held", "host-pinned",
         "passed"),
        ({"in_progress": {"stages": [{"name": "checks", "state": "running",
                                      "started_at": "2026-10-23T09:30:00Z"}]}},
         "deploying", None, "running"),
        ({"last_attempt": {"commit": NEW, "state": "rolled-back",
                           "reason": "host-check-failed",
                           "checks": [{"name": "http-1", "state": "failed"}]}},
         "rolled-back", "host-check-failed", "failed"),
        ({"last_attempt": {"commit": NEW, "state": "rejected",
                           "reason": "host-manifest-invalid"}},
         "failed", "host-manifest-invalid", "none"),
    ],
    ids=["pinned", "deploying", "rolled-back", "refused"],
)  # fmt: skip
def test_host_states(
    changes: dict[str, Any], state: str, reason: Any, checks: str
) -> None:
    document = targets.build(None, hosts={"web-1": {"status": _agent(**changes)}},
                             targets=targets.declared([]))  # fmt: skip
    row = document["targets"][0]
    assert (row["state"], row["reason"], row["checks"]["state"]) == (
        state,
        reason,
        checks,
    )
    assert targets.exit_code(document) == (1 if state in targets.BAD_STATES else 0)


def test_with_the_home_cluster_down_host_rows_still_work() -> None:
    found = {"environments": {"main": {"cluster": "home", "namespace": "shop"}},
             "hosts": {"web-1": object(), "db-1": object()}, "releases": {"web-1"}}  # fmt: skip
    document = targets.build(
        None,
        controller_reason="gitops-cluster-failed",
        controller_detail="the API server is unreachable",
        targets=found,
        hosts={
            "web-1": {"status": _agent()},
            "db-1": {"reason": "host-unreachable", "detail": "ssh failed"},
        },
    )
    rows = {row["name"]: row for row in document["targets"]}
    assert rows["main"]["state"] == "unreachable"
    assert rows["main"]["reason"] == "gitops-cluster-failed"
    assert rows["main"]["where"] == "home/shop"
    assert rows["web-1"]["state"] == "deployed" and rows["web-1"]["pinned"] is None
    assert rows["db-1"]["state"] == "unreachable"
    assert rows["db-1"]["detail"] == "ssh failed"
    assert document["controller"] == {"read": False, "health": None, "as_of": None,
                                      "reason": "gitops-cluster-failed",
                                      "detail": "the API server is unreachable"}  # fmt: skip
    assert targets.exit_code(document) == 1


def test_without_host_reads_the_view_shows_the_published_release() -> None:
    """The in-cluster UI cannot SSH to hosts: the release as published."""
    document = targets.build(_status(**{"web-1": RELEASE}), hosts=None)
    row = _rows(document)[("web-1", None)]
    assert row["state"] == "unknown" and document["hosts_read"] is False
    assert row["revision"] == {"commit": HOSTS_COMMIT, "tag": "web-v1.2.0"}
    assert row["pinned"] == GATE and row["as_of"] == "2026-10-23T09:05:00Z"
    assert set(row["facts"].values()) == {"controller"}


def test_hosts_are_read_in_parallel_and_a_slow_one_times_out() -> None:
    release = threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5)
        return _agent()

    def refused() -> dict[str, Any]:
        raise HostError("host-status-missing", "no status file")

    def boom() -> dict[str, Any]:
        raise RuntimeError("unexpected")

    started = time.monotonic()
    try:
        found = targets.read_hosts(
            {"slow": slow, "ok": _agent, "refused": refused, "boom": boom}, 0.3
        )
    finally:
        release.set()
    assert time.monotonic() - started < 3
    assert found["ok"] == {"status": _agent()}
    assert found["slow"]["reason"] == "host-unreachable"
    assert found["refused"] == {"reason": "host-status-missing",
                                "detail": "no status file"}  # fmt: skip
    assert found["boom"]["reason"] == "host-unreachable"
    assert targets.read_hosts({}, 1) == {}


def test_the_table_is_compact() -> None:
    document = targets.build(
        _status(**{"web-1": RELEASE}), hosts={"web-1": {"status": _agent()}},
        where={"web-1": "203.0.113.7"},
    )  # fmt: skip
    lines = targets.table(document)
    assert lines[0].split() == ["KIND", "NAME", "WHERE", "REVISION", "PINNED",
                                "CHECKS", "STATE", "AS", "OF"]  # fmt: skip
    web = next(line for line in lines if "web-1" in line)
    assert "web-v1.2.0 dddddddddddd" in web and "product@aaaaaaa" in web
    rc = next(line for line in lines if line.split()[1] == "rc")
    assert "rolled-back (checks-failed-rolled-back)" in rc and "failed 2/3" in rc
    assert len(lines) == 1 + len(document["targets"])


# ------------------------------------------------------------------ the CLI

MODULE = """
from piceli.dev.nix import NixCache, generate_key, public_key
from piceli.envs import Environment
from piceli.infra import NixHost, NixRelease, Source, Tag

product = Source("https://git.example/shop/product.git", name="product")
hosts = Source("https://git.example/shop/hosts.git", name="hosts")
cache = NixCache(public_key(generate_key("cache-1")), url="file:///nonexistent")
environments = [Environment("main", namespace="shop", follow={product: "main"})]
web = NixHost("web-1", server="ssh://root@web-1.example.net", flake=".#web-1",
              follow=Tag("web-v*"), cache=cache)
db = NixHost("db-1", server="ssh://root@db-1.example.net", flake=".#db-1",
             follow=Tag("db-v*"), cache=cache)
web_release = NixRelease(web, source=hosts, builder="mini")
"""


def _module(tmp_path: Path) -> Path:
    path = tmp_path / "infra_targets.py"
    path.write_text(MODULE)
    return path


def _reads(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import piceli.hosts.remote as remote

    seen: list[Any] = []

    def read_status(target: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append((target.address, kwargs.get("timeout")))
        if target.address == "db-1.example.net":
            raise HostError("host-unreachable", "ssh to db-1.example.net failed")
        return _agent()

    monkeypatch.setattr(remote, "read_status", read_status)
    return seen


def test_cli_reads_the_controller_once_and_every_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.gitops.state import DirectoryChannel
    from piceli.k8s.cli import app

    seen = _reads(monkeypatch)
    DirectoryChannel(tmp_path / "state").publish(_status(**{"web-1": RELEASE}))
    done = CliRunner().invoke(app, [
        "targets", "--composition", str(_module(tmp_path)),
        "--state-dir", str(tmp_path / "state"), "--timeout", "5", "--json",
    ])  # fmt: skip
    assert done.exit_code == 1, done.stdout + done.stderr  # rc rolled back, db-1 down
    body = json.loads(done.stdout)
    rows = {(row["name"], row["where"]): row for row in body["targets"]}
    assert rows[("web-1", "web-1.example.net")]["pinned"] == GATE
    db = rows[("db-1", "db-1.example.net")]
    assert db["state"] == "unreachable" and db["reason"] == "host-unreachable"
    assert sorted(seen) == [("db-1.example.net", 5.0), ("web-1.example.net", 5.0)]
    assert "KIND" in done.stderr and "web-v1.2.0" in done.stderr


def test_cli_without_a_controller_shows_hosts_and_unreachable_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.cli import app

    _reads(monkeypatch)
    done = CliRunner().invoke(
        app, ["targets", "--infra", str(_module(tmp_path)), "--json"]
    )
    body = json.loads(done.stdout)
    rows = {row["name"]: row for row in body["targets"]}
    assert rows["main"]["state"] == "unreachable"
    assert rows["main"]["reason"] == "gitops-target-required"
    assert rows["web-1"]["state"] == "deployed"
    assert done.exit_code == 1
    assert "controller not read" in done.stderr


def test_cli_exits_0_when_every_target_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.gitops.state import DirectoryChannel
    from piceli.k8s.cli import app

    status = _status(**{"web-1": RELEASE})
    status["envs"] = {"main": status["envs"]["main"]}
    DirectoryChannel(tmp_path / "state").publish(status)
    done = CliRunner().invoke(
        app, ["targets", "--state-dir", str(tmp_path / "state"), "--json"]
    )
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert body["state"] == "ok" and body["hosts_read"] is True
    assert [row["state"] for row in body["targets"]] == ["deployed", "unknown"]


def test_cli_refuses_without_any_input() -> None:
    from piceli.k8s.cli import app

    done = CliRunner().invoke(app, ["targets", "--json"])
    assert done.exit_code == 2
    assert json.loads(done.stdout)["reason"] == "targets-input-required"


# ------------------------------------------- the controller of the home cluster

HOME = """
from piceli.envs import Environment
from piceli.infra import Cluster, Component, Source

product = Source("https://git.example/shop/product.git", name="product")
web = Component("web", source=product)
home = Cluster("my-cluster", api="https://203.0.113.9:6443", credentials="my-cluster")
environments = [Environment("main", namespace="shop", follow={product: "main"},
                            cluster=home)]
"""


def _home_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from piceli import profiles

    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("PICELI_IN_CLUSTER", raising=False)
    monkeypatch.setenv("PICELI_SERVICE_ACCOUNT_DIR", str(tmp_path / "no-sa"))
    profiles.set_override(None)
    path = tmp_path / "infra_home.py"
    path.write_text(HOME)
    return path


def _seen_controller(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from piceli.k8s.cli import targets as cli

    seen: list[Any] = []

    def controller(*args: Any) -> Any:
        seen.append(args)
        status = _status()
        status["envs"] = {"main": status["envs"]["main"]}
        return status, None, None

    monkeypatch.setattr(cli, "_controller", controller)
    return seen


def test_cli_reads_the_home_clusters_controller_with_its_login_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli import profiles
    from piceli.k8s.cli import app

    module = _home_module(tmp_path, monkeypatch)
    kubeconfig = tmp_path / "kc.yaml"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: "
        '{server: "https://203.0.113.9"}\nusers:\n- name: u\n  user: {token: t}\n'
        "contexts:\n- name: ctx\n  context: {cluster: c, user: u}\n"
    )
    profiles.save_profile("my-cluster", kubeconfig, "ctx")
    seen = _seen_controller(monkeypatch)
    done = CliRunner().invoke(app, ["targets", "--composition", str(module), "--json"])
    assert done.exit_code == 0, done.stdout + done.stderr
    assert [(a[1].resolve(), a[2]) for a in seen] == [(kubeconfig.resolve(), "ctx")]
    assert json.loads(done.stdout)["targets"][0]["state"] == "deployed"
    # Explicit flags win over the profile.
    other = tmp_path / "other.yaml"
    other.write_text(kubeconfig.read_text())
    seen.clear()
    done = CliRunner().invoke(app, ["targets", "--composition", str(module),
                                    "--kubeconfig", str(other), "--context", "ctx"])  # fmt: skip
    assert done.exit_code == 0, done.stdout + done.stderr
    assert seen[0][1] == other


def test_cli_without_the_home_profile_shows_environments_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.cli import app

    module = _home_module(tmp_path, monkeypatch)
    seen = _seen_controller(monkeypatch)
    done = CliRunner().invoke(app, ["targets", "--composition", str(module), "--json"])
    assert done.exit_code == 1, done.stdout + done.stderr
    body = json.loads(done.stdout)
    (main,) = body["targets"]
    assert main["state"] == "unreachable" and main["reason"] == "profile-not-found"
    assert main["where"] == "my-cluster/shop"
    assert "piceli login my-cluster" in main["detail"]
    assert "piceli login my-cluster" in done.stderr and seen == []
