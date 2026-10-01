"""Independent regressions for the legacy UI's observation/action claims."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from piceli.artifacts.gc import ImageSpaceInventory
from piceli.k8s.observe import ObservationRef, ObservedObject, PreferenceStore
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.operator import build_operator_report


class _Archive:
    session_id = "a" * 32

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition": [
                {
                    "resources": [
                        {
                            "resource": {
                                "api_version": "apps/v1",
                                "kind": "Deployment",
                                "namespace": "shop",
                                "name": "web",
                            }
                        }
                    ]
                }
            ]
        }


class _Reader:
    def get(self, ref: ObservationRef) -> ObservedObject:
        return ObservedObject(ref, images=("registry.example/web:v2",))

    def list(self, api_version: str, kind: str, namespace: str) -> list[ObservedObject]:
        return []


@contextmanager
def _server(
    tmp_path: Path, *, reader: Any = None, **kwargs: Any
) -> Iterator[LocalObserveServer]:
    reader = reader or _Reader()
    server = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: build_operator_report(
            reader, "shop", session_archive=_Archive(), include_common_types=True
        ),
        PreferenceStore(tmp_path / "preferences.json"),
        **kwargs,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(
    server: LocalObserveServer, path: str, payload: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "GET" if payload is None else "POST",
            path,
            body=None if payload is None else json.dumps(payload),
            headers={
                "X-Piceli-Local-Token": server.local_token,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_unconfigured_artifacts_are_unknown_instead_of_measured_zero(
    tmp_path: Path,
) -> None:
    with _server(tmp_path) as server:
        status, result = _request(server, "/v1/artifacts")
    assert status == 200
    assert result["available"] is False
    for key in ("total_images", "total_bytes", "protected_bytes", "reclaimable_bytes"):
        assert result[key] is None


def test_configured_empty_artifact_scan_still_reports_real_zero(tmp_path: Path) -> None:
    with _server(
        tmp_path, artifact_inventory=ImageSpaceInventory(entries=(), scan_complete=True)
    ) as server:
        status, result = _request(server, "/v1/artifacts")
    assert status == 200
    assert result["total_images"] == 0
    assert result["total_bytes"] == 0


def test_legacy_rollback_route_reports_catalog_selection_without_deployment(
    tmp_path: Path,
) -> None:
    catalog = Mock()
    execute = Mock(
        side_effect=AssertionError("catalog selection must not apply a release")
    )
    workflow = SimpleNamespace(catalog=catalog, apply=execute)
    with _server(tmp_path, workflow=workflow) as server:
        status, result = _request(
            server, "/v1/releases/rollback", {"target_release_name": "previous"}
        )
    assert status == 200
    catalog.select.assert_called_once_with("previous")
    execute.assert_not_called()
    assert result["action"] == "catalog-selection"
    assert result["selected"] == "previous"
    assert result["deployed"] is False
    assert "rolled_back_to" not in result


def test_unconfigured_workflow_cannot_claim_recovery(tmp_path: Path) -> None:
    with _server(tmp_path) as server:
        status, result = _request(
            server, "/v1/releases/rollback", {"target_release_name": "previous"}
        )
    assert status >= 400
    assert result["error"] == "workflow-not-configured"
    assert "rolled_back_to" not in result


@pytest.mark.parametrize("failure", [PermissionError, RuntimeError])
def test_declared_resource_failure_never_discloses_provider_detail(
    tmp_path: Path, failure: type[Exception]
) -> None:
    private = "token=private-provider-value /private/cluster/credentials"

    class DeniedReader(_Reader):
        def get(self, ref: ObservationRef) -> ObservedObject:
            raise failure(private)

    with _server(tmp_path, reader=DeniedReader()) as server:
        status, result = _request(server, "/v1/status")
    assert status == 200
    assert result["unknown"][0]["state"] == "unknown"
    assert result["unknown"][0]["error"]
    assert "private-provider-value" not in json.dumps(result)
    assert "/private/cluster" not in json.dumps(result)


def test_denied_kind_preserves_observed_resources_and_partial_warning(
    tmp_path: Path,
) -> None:
    class PartialReader(_Reader):
        def list(
            self, api_version: str, kind: str, namespace: str
        ) -> list[ObservedObject]:
            if kind == "Secret":
                raise PermissionError("private-rbac-detail")
            return []

    with _server(tmp_path, reader=PartialReader()) as server:
        status, result = _request(server, "/v1/status")
    assert status == 200
    assert result["managed"][0]["ref"]["name"] == "web"
    assert result["managed"][0]["state"] == "present"
    assert result["scan_errors"]
    assert "private-rbac-detail" not in json.dumps(result)
