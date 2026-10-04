"""Workload log selection stays scoped through pod replacement and denied reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.logs import LogService
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration


def row(kind: str, name: str, uid: str, owner: str | None = None) -> dict[str, Any]:
    pod_spec = {"containers": [{"name": "api", "image": "example/api:1"}]}
    return {
        "apiVersion": "apps/v1" if kind in {"Deployment", "ReplicaSet"} else "v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "shop",
            "uid": uid,
            "ownerReferences": [{"uid": owner}] if owner else [],
        },
        "spec": {"template": {"spec": pod_spec}} if kind == "Deployment" else pod_spec,
        "status": {"phase": "Running", "startTime": "2026-09-29T10:00:00Z"},
    }


class Reader:
    pods = [row("Pod", "api-old", "pod-old", "replica-1")]
    denied: str | None = None
    fail_log = False
    calls: list[tuple[str, str, str, bool, int]] = []

    def __init__(self, _registration: Registration) -> None:
        pass

    def list(self, _version: str, kind: str, _namespace: str) -> list[dict[str, Any]]:
        if kind == self.denied:
            raise RuntimeError("private permission detail")
        if kind == "Deployment":
            return [row("Deployment", "api", "deployment-1")]
        if kind == "ReplicaSet":
            return [row("ReplicaSet", "api-abc", "replica-1", "deployment-1")]
        if kind == "Pod":
            return self.pods
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
        if self.fail_log:
            raise RuntimeError("private token")
        self.calls.append((pod, namespace, container, previous, tail_lines))
        return "2026-09-29T10:00:00Z first\n2026-09-29T10:00:01Z second\n"

    def close(self) -> None:
        pass


@pytest.fixture
def service() -> tuple[QueryService, LogService]:
    Reader.pods = [row("Pod", "api-old", "pod-old", "replica-1")]
    Reader.denied = None
    Reader.fail_log = False
    Reader.calls = []
    query = QueryService(
        [
            Registration(
                "shop",
                "Shop",
                KubeconfigTarget(Path("/explicit/config"), "local", "shop"),
                kinds=(("apps/v1", "Deployment"),),
            )
        ],
        reader_factory=Reader,
    )
    return query, LogService(query)


def test_deployment_selection_survives_pod_replacement(
    service: tuple[QueryService, LogService],
) -> None:
    query, logs = service
    selected = query.resources("shop").items[0]
    assert selected.capabilities["logs"].allowed
    with pytest.raises(QueryError, match="ui-observation-unavailable"):
        logs.sources("shop", selected.id, "stale")
    sources = logs.sources("shop", selected.id, selected.identity.uid or "")
    assert [(item.pod.name, item.containers) for item in sources.items] == [
        ("api-old", ["api"])
    ]
    batch = logs.read(
        "shop",
        selected.id,
        resource_uid=selected.identity.uid or "",
        pod_name="api-old",
        pod_uid="pod-old",
        container="api",
        previous=True,
    )
    assert batch.lines[-1].endswith("second")
    assert Reader.calls == [("api-old", "shop", "api", True, 2000)]
    Reader.pods = [row("Pod", "api-new", "pod-new", "replica-1")]
    assert (
        logs.sources("shop", selected.id, selected.identity.uid or "").items[0].pod.name
        == "api-new"
    )
    with pytest.raises(QueryError, match="ui-not-found"):
        logs.read(
            "shop",
            selected.id,
            resource_uid=selected.identity.uid or "",
            pod_name="api-old",
            pod_uid="pod-old",
            container="api",
        )
    with pytest.raises(QueryError, match="ui-invalid-request"):
        logs.read(
            "shop",
            selected.id,
            resource_uid=selected.identity.uid or "",
            pod_name="api-new",
            pod_uid="pod-new",
            container="other",
        )


def test_partial_rbac_and_provider_errors_are_safe(
    service: tuple[QueryService, LogService],
) -> None:
    query, logs = service
    selected = query.resources("shop").items[0]
    Reader.denied = "ReplicaSet"
    page = logs.sources("shop", selected.id, selected.identity.uid or "")
    assert not page.items and page.partial[0].scope == "apps/v1/ReplicaSet"
    assert "private" not in page.model_dump_json()
    Reader.denied = None
    Reader.fail_log = True
    with pytest.raises(QueryError, match="ui-logs-unavailable"):
        logs.read(
            "shop",
            selected.id,
            resource_uid=selected.identity.uid or "",
            pod_name="api-old",
            pod_uid="pod-old",
            container="api",
        )


def test_log_lines_are_redacted_before_they_leave_the_service(
    service: tuple[QueryService, LogService], monkeypatch: pytest.MonkeyPatch
) -> None:
    query, logs = service
    monkeypatch.setattr(
        Reader,
        "logs",
        lambda *_args, **_kwargs: (
            "2026-09-29T10:00:00Z store: upstream password=hunter2-value\n"
            "2026-09-29T10:00:01Z Authorization: Bearer abcdefghijkl123\n"
            "2026-09-29T10:00:02Z plain\n"
        ),
    )
    selected = query.resources("shop").items[0]
    batch = logs.read(
        "shop",
        selected.id,
        resource_uid=selected.identity.uid or "",
        pod_name="api-old",
        pod_uid="pod-old",
        container="api",
    )
    text = "\n".join(batch.lines)
    assert "hunter2-value" not in text and "abcdefghijkl123" not in text
    assert batch.lines[0].endswith("store: upstream password=[REDACTED]")
    assert batch.lines[2] == "2026-09-29T10:00:02Z plain"
