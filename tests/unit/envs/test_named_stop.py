"""Stopping and starting a named environment for the GitOps controller (0.14.7).

``env_stop(..., named=True)`` (a declared ``Environment(stopped=True)`` or a
``piceli env stop`` request) scales the named environment's workloads to zero
and records them; ``env_start`` scales them back. Claims are never touched.
Env commands without ``named=True`` still never stop a named environment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from piceli.envs import EnvError, env_start, env_stop
from piceli.envs.model import ENV_NAME_LABEL
from tests.unit.envs.test_named_envs import _named, _pipeline
from tests.unit.envs.test_ops import Cluster

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def _rc(cluster: Cluster, *, labelled: bool = True) -> None:
    labels = {ENV_NAME_LABEL: "rc"} if labelled else {}
    cluster.ns["shop-rc"] = {"metadata": {"name": "shop-rc", "labels": labels}}
    cluster.records["shop-rc"] = {"branch": "rc", "state": "running"}
    cluster.live["shop-rc"] = [
        {"kind": "Deployment", "metadata": {"name": "web"}, "spec": {"replicas": 2}},
        {"kind": "StatefulSet", "metadata": {"name": "db"}, "spec": {"replicas": 1}},
    ]
    cluster.pvcs["shop-rc"] = ["data-db-0"]


def test_a_named_environment_stops_and_starts_on_the_owners_request(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, environments=_named())
    cluster = Cluster()
    _rc(cluster)
    with pytest.raises(EnvError) as protected:
        env_stop(pipeline, "rc", approve_if_policy=True, cluster=cluster)
    assert protected.value.code == "env-main-protected"

    stopped = env_stop(
        pipeline,
        "rc",
        approve_if_policy=True,
        reason="requested",
        named=True,
        cluster=cluster,
        now=NOW,
    )
    assert stopped["state"] == "stopped"
    assert cluster.calls == [
        "scale shop-rc Deployment/web 0",
        "scale shop-rc StatefulSet/db 0",
    ]
    record = cluster.records["shop-rc"]
    assert record["state"] == "stopped" and record["stop_reason"] == "requested"
    assert not any("delete" in call for call in cluster.calls)  # claims kept

    started = env_start(pipeline, "rc", cluster=cluster, now=NOW)
    assert started["state"] == "started"
    assert sorted(started["started"]) == ["Deployment/web", "StatefulSet/db"]
    assert cluster.calls[-2:] == [
        "scale shop-rc Deployment/web 2",
        "scale shop-rc StatefulSet/db 1",
    ]
    record = cluster.records["shop-rc"]
    assert record["state"] == "running" and record["stopped"] == []
    assert env_start(pipeline, "rc", cluster=cluster)["state"] == "running"


def test_a_named_stop_only_touches_the_namespace_piceli_deployed_it_to(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, environments=_named())
    cluster = Cluster()
    _rc(cluster, labelled=False)
    cluster.records["shop-rc"] = {"branch": "other"}  # not this environment's record
    with pytest.raises(EnvError) as error:
        env_stop(
            pipeline, "rc", approve_if_policy=True, named=True, cluster=cluster
        )
    assert error.value.code == "env-namespace-not-managed"
    assert cluster.calls == []
    # Its environment record is enough when the namespace was not labelled.
    cluster.records["shop-rc"] = {"branch": "rc", "state": "running"}
    done = env_stop(pipeline, "rc", approve_if_policy=True, named=True, cluster=cluster)
    assert done["state"] == "stopped"


def test_a_never_deployed_named_environment_has_nothing_to_stop(
    tmp_path: Path,
) -> None:
    pipeline = _pipeline(tmp_path, environments=_named())
    cluster = Cluster()
    result = env_stop(pipeline, "rc", approve_if_policy=True, named=True, cluster=cluster)
    assert result["state"] == "absent"
    assert env_start(pipeline, "rc", cluster=cluster)["state"] == "absent"
