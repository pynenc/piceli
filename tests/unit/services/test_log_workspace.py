"""The Logs workspace: scoped aggregation, bounded redacted reads, profile scopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.profiles import save_profile
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import Principal
from piceli.services.log_workspace import (
    LogWorkspace,
    ProfileScopes,
    detect_level,
    split_time,
)
from piceli.services.logs import LogService
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration


def pod(
    namespace: str, name: str, uid: str, owner: tuple[str, str] | None
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": uid,
            "ownerReferences": [
                {"kind": owner[0], "name": owner[1], "uid": "o", "controller": True}
            ]
            if owner
            else [],
        },
        "spec": {"containers": [{"name": "api", "image": "example/api:1"}]},
        "status": {
            "phase": "Running",
            "startTime": "2026-10-03T10:00:00Z",
            "containerStatuses": [{"name": "api", "restartCount": 2}],
        },
    }


LOGS = {
    ("shop", "api-1"): (
        "2026-10-03T10:00:01.000000001Z INFO started\n"
        "2026-10-03T10:00:03Z ERROR request failed password=hunter2\n"
        "    at handler (trace line)\n"
    ),
    ("shop-preview", "web-1"): (
        "2026-10-03T10:00:02Z level=warn slow\n2026-10-03T10:00:04Z plain line\n"
    ),
}


class Reader:
    reads: list[tuple[str, str, bool, int]] = []

    def __init__(self, registration: Registration) -> None:
        self.namespace = registration.target.namespace

    def list(self, _version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        if kind == "ReplicaSet" and namespace == "shop":
            return [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": "api-abc",
                        "namespace": "shop",
                        "ownerReferences": [
                            {"kind": "Deployment", "name": "api", "controller": True}
                        ],
                    },
                }
            ]
        if kind != "Pod":
            return []
        if namespace == "shop":
            return [pod("shop", "api-1", "uid-api-1", ("ReplicaSet", "api-abc"))]
        if namespace == "shop-preview":
            return [pod("shop-preview", "web-1", "uid-web-1", ("StatefulSet", "web"))]
        return []

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        self.reads.append((namespace, pod, previous, tail_lines))
        return LOGS[(namespace, pod)]

    def close(self) -> None:
        pass


def registrations(tmp_path: Path) -> list[Registration]:
    return [
        Registration(
            "shop", "Shop", KubeconfigTarget(tmp_path / "kc", "explicit", "shop")
        ),
        Registration(
            "env-preview",
            "preview",
            KubeconfigTarget(tmp_path / "kc", "explicit", "shop-preview"),
        ),
    ]


def workspace(query: QueryService) -> LogWorkspace:
    LogService(query)
    return LogWorkspace(query)


def test_time_and_level_detection() -> None:
    assert split_time("2026-10-03T10:00:01.123456789Z hello") == (
        "2026-10-03T10:00:01.123456789Z",
        "hello",
    )
    assert (
        split_time("2026-10-03T12:00:00+02:00 x")[0] == "2026-10-03T10:00:00.000000000Z"
    )
    assert split_time("no time here") == (None, "no time here")
    assert detect_level('{"level":"error","msg":"x"}') == "error"
    assert detect_level("2026 WARNING disk") == "warn"
    assert detect_level("[info] ready") == "info"
    assert detect_level("severity=DEBUG x") == "debug"
    assert detect_level("an information line") is None


def test_aggregates_scopes_merges_by_time_and_redacts(tmp_path: Path) -> None:
    Reader.reads = []
    query = QueryService(registrations(tmp_path), reader_factory=Reader)
    logs = workspace(query)
    sources = logs.sources()
    assert [scope.kind for scope in sources.scopes] == ["application", "environment"]
    by_pod = {item.pod.name: item for item in sources.items}
    assert by_pod["api-1"].workload is not None
    assert (by_pod["api-1"].workload.kind, by_pod["api-1"].workload.name) == (
        "Deployment",
        "api",
    )
    assert by_pod["web-1"].workload.kind == "StatefulSet"  # type: ignore[union-attr]
    assert by_pod["api-1"].restarts == 2
    assert [item.pod.name for item in logs.sources(["env-preview"]).items] == ["web-1"]

    batch = logs.read(
        ["shop/api-1/uid-api-1/api", "env-preview/web-1/uid-web-1/api"],
        tail_lines=100,
    )
    texts = [line.text for line in batch.lines]
    assert texts == [
        "INFO started",
        "level=warn slow",
        "ERROR request failed password=[REDACTED]",
        "    at handler (trace line)",
        "plain line",
    ]
    assert [line.level for line in batch.lines] == ["info", "warn", "error", None, None]
    # The continuation line keeps its first line's time and stream.
    assert batch.lines[3].at == batch.lines[2].at and batch.lines[3].stream == 0
    assert [stream.state for stream in batch.streams] == ["ok", "ok"]
    assert "hunter2" not in batch.model_dump_json()

    only = logs.read(
        ["shop/api-1/uid-api-1/api", "env-preview/web-1/uid-web-1/api"],
        levels=["error", "warn"],
        query="FAILED",
    )
    assert [line.text for line in only.lines] == [
        "ERROR request failed password=[REDACTED]"
    ]
    # Old lines fall outside a recent time range; the reads stay bounded.
    assert logs.read(["shop/api-1/uid-api-1/api"], since_seconds=60).lines == []
    assert all(tail == 500 for *_, tail in Reader.reads[2:])


def test_unknown_or_replaced_pods_and_bounds_stay_explicit(tmp_path: Path) -> None:
    query = QueryService(registrations(tmp_path), reader_factory=Reader)
    logs = workspace(query)
    batch = logs.read(
        ["shop/api-1/old-uid/api", "missing/x/u/api", "shop/api-1/uid-api-1/db"]
    )
    assert [(item.state, item.code) for item in batch.streams] == [
        ("not-found", "ui-not-found"),
        ("not-found", "ui-not-found"),
        ("not-found", "ui-not-found"),
    ]
    assert batch.lines == []
    for streams, options in (
        ([], {}),
        ([f"shop/api-{n}/u/api" for n in range(13)], {}),
        (["shop/api-1"], {}),
        (["../etc/x/y/z"], {}),
        (["shop/api-1/uid-api-1/api"], {"tail_lines": 5001}),
        (["shop/api-1/uid-api-1/api"], {"since_seconds": 0}),
        (["shop/api-1/uid-api-1/api"], {"levels": ["loud"]}),
        (["shop/api-1/uid-api-1/api"], {"query": "x" * 201}),
    ):
        with pytest.raises(QueryError) as raised:
            logs.read(streams, **options)  # type: ignore[arg-type]
        assert raised.value.code == "ui-invalid-request"


def test_only_scopes_with_the_logs_grant_are_listed_or_read(tmp_path: Path) -> None:
    Reader.reads = []
    query = QueryService(
        registrations(tmp_path),
        reader_factory=Reader,
        scope_policy=ScopePolicy(
            {
                "alice": {
                    "shop": frozenset({"inspect", "logs"}),
                    "env-preview": frozenset({"inspect"}),
                }
            }
        ),
    )
    logs = workspace(query)
    with request_principal(Principal(id="alice", name="Alice", kind="oidc")):
        assert [scope.id for scope in logs.scopes()] == ["shop"]
        assert {item.scope_id for item in logs.sources().items} == {"shop"}
        assert logs.sources(["env-preview"]).items == []
        batch = logs.read(["env-preview/web-1/uid-web-1/api"])
        assert batch.streams[0].state == "not-found" and batch.lines == []
    assert not any(namespace == "shop-preview" for namespace, *_ in Reader.reads)
    with pytest.raises(QueryError):
        logs.scopes()  # no principal: nothing


def test_profile_scopes_use_the_profile_kubeconfig_and_stay_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("PICELI_IN_CLUSTER", raising=False)
    monkeypatch.setenv("PICELI_SERVICE_ACCOUNT_DIR", str(tmp_path / "no-sa"))
    kubeconfig = tmp_path / "west.kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\nclusters: [{name: c, cluster: {server: 'https://192.0.2.1'}}]\n"
        "users: [{name: u, user: {}}]\ncontexts: [{name: west, context: {cluster: c, user: u}}]\n"
    )
    save_profile("west", kubeconfig, "west")
    query = QueryService(registrations(tmp_path), reader_factory=Reader)
    profiles = ProfileScopes(query)
    logs = LogWorkspace(query, profiles=profiles, active_profile="east")
    LogService(query)
    added = profiles.add("west", "shop")
    assert added.target.kubeconfig == kubeconfig.absolute()
    assert added.target.context == "west" and added.target.namespace == "shop"
    assert all(kind not in {"Secret", "ConfigMap"} for _, kind in added.kinds)
    assert profiles.add("west", "shop").id == added.id  # idempotent
    scope = logs.scope(added)
    assert (scope.kind, scope.profile, scope.cluster) == ("profile", "west", "west")
    assert logs.scope(query.registrations["shop"]).profile == "east"
    with pytest.raises(QueryError) as missing:
        profiles.add("absent", "shop")
    assert missing.value.code == "profile-not-found"
    profiles.limit = 1
    with pytest.raises(QueryError) as limited:
        profiles.add("west", "other")
    assert limited.value.code == "ui-operation-conflict"
    profiles.remove(added.id)
    assert added.id not in query.registrations
    monkeypatch.setenv("PICELI_IN_CLUSTER", "1")
    with pytest.raises(QueryError) as inside:
        profiles.add("west", "shop")
    assert inside.value.code == "ui-operation-unavailable"
    scoped = QueryService(
        registrations(tmp_path), reader_factory=Reader, scope_policy=ScopePolicy({})
    )
    monkeypatch.delenv("PICELI_IN_CLUSTER")
    with pytest.raises(QueryError):
        ProfileScopes(scoped).add("west", "shop")
