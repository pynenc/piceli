"""Only bounded recorded transitions and already-redacted diagnosis leave journals."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import Operation, Principal
from piceli.services.evaluation import DockerEvaluator
from piceli.services.execution_evidence import read_journal
from piceli.services.operations import OperationService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store
from tests.unit.services.test_plan_evidence import fixture_plan


def journal(path: Path) -> None:
    value = ExecutionJournal(path)
    plan = fixture_plan() | {"plan_hash": "engine-plan"}
    value.start(
        "execution",
        {"revision": {"desired_state": plan}},
        [char * 32 for char in "abc"],
    )
    value.record(
        "execution",
        0,
        "intent",
        {
            "before": {"store_id": "private-store", "version": "private-version"},
            "error": "private-error",
        },
    )
    value.record(
        "execution",
        0,
        "ready",
        {
            "uid": "pod-id",
            "written_at": "2026-10-02T10:00:00Z",
            "after": {"store_id": "private-store", "version": "private-version"},
        },
    )
    value.record("execution", 1, "failed", {})
    value.record_failure("execution", "apply-crashloop")
    value.record_diagnosis(
        "execution",
        {
            "schema": "piceli.diagnosis.v1",
            "code": "apply-crashloop",
            "workloads": [
                {
                    "kind": "Pod",
                    "name": "api",
                    "causes": [
                        {
                            "pod": "api",
                            "container": "web",
                            "logs": ["startup failed", "token=must-be-redacted"],
                            "message": "private-provider-message",
                        }
                    ],
                }
            ],
        },
    )
    value.set_state("execution", "failed")
    value.close()


def test_projection_reads_durable_ordinals_and_events_without_private_payloads_or_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    journal(path)
    before = path.read_bytes()
    result = read_journal(path, "execution", "engine-plan", "target")
    assert result is not None and not result.truncated
    assert [
        (action.ordinal, action.resource.name, action.state)
        for action in result.actions
    ] == [(0, "settings", "ready"), (1, "api", "failed"), (2, "old", "pending")]
    assert result.actions[0].written_at == "2026-10-02T10:00:00Z"
    assert [event.state for event in result.events] == [
        "intent",
        "ready",
        "failed",
        "error:apply-crashloop",
    ]
    assert [event.sequence for event in result.events] == sorted(
        event.sequence for event in result.events
    )
    assert result.logs[0].lines == ["startup failed", "token=[REDACTED]"]
    public = result.model_dump_json()
    for forbidden in (
        "private-store",
        "private-version",
        "private-error",
        "never-public",
        "private-provider-message",
        "must-be-redacted",
    ):
        assert forbidden not in public
    assert path.read_bytes() == before


def test_missing_wrong_identity_and_bounded_history_are_explicit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing.sqlite"
    assert read_journal(path, "execution", "engine-plan", "target") is None
    assert not path.exists()
    journal(path)
    assert read_journal(path, "execution", "wrong-plan", "target") is None
    assert read_journal(path, "another-execution", "engine-plan", "target") is None
    result = read_journal(
        path, "execution", "engine-plan", "target", max_actions=1, max_events=2
    )
    assert result is not None and result.truncated
    assert len(result.actions) == 1 and len(result.events) == 2
    assert [event.state for event in result.events] == [
        "failed",
        "error:apply-crashloop",
    ]


def test_binding_mismatch_or_unrecognized_states_cannot_be_presented_as_execution_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite"
    journal(path)
    value = ExecutionJournal(path)
    with value.connection:
        value.connection.execute(
            "UPDATE actions SET state='secret-state-value' WHERE ordinal=0"
        )
    value.close()
    result = read_journal(path, "execution", "engine-plan", "target")
    assert result is not None and result.truncated
    assert "secret-state-value" not in json.dumps(result.model_dump())


@pytest.mark.parametrize("saved", [True, False], ids=["saved", "projected"])
def test_recorded_logs_require_logs_grant_for_detail_and_saved_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: bool
) -> None:
    path = tmp_path / "journal.sqlite"
    journal(path)
    evidence = read_journal(path, "execution", "engine-plan", "target")
    assert evidence is not None and evidence.logs
    policy = ScopePolicy({"viewer": {"shop": frozenset({"activity", "logs"})}})
    query = QueryService(
        [
            Registration(
                "shop", "Shop", KubeconfigTarget(tmp_path / "unused", "test", "test")
            )
        ],
        scope_policy=policy,
    )
    operations = OperationService(
        query,
        Store(tmp_path / "control.sqlite"),
        Mock(spec=DockerEvaluator),
        {},
        principal=None,
    )
    operation = Operation(
        id="run",
        application_id="shop",
        plan_id="",
        approved_digest="engine-plan",
        actor="operator",
        trigger="cli",
        state="failed",
        created_at="2026-10-02T10:00:00Z",
        updated_at="2026-10-02T10:00:00Z",
        engine_execution_id="execution",
        journal=evidence if saved else None,
    )
    operations.store.put("operation", operation.model_dump(mode="json"), private={})
    stored = operations.store.get("operation", "run")
    monkeypatch.setattr(
        operations.engine, "journal", lambda *_args: None if saved else evidence
    )
    # Historical logs need an authorization grant, not a currently available
    # live-pod log reader: no logs capability provider is configured here.
    assert query.logs_capability is None
    with request_principal(Principal(id="viewer", name="Viewer", kind="oidc")):
        allowed = operations.operation("run")
        assert allowed.journal is not None and allowed.journal.logs == evidence.logs
        policy.replace({"viewer": {"shop": frozenset({"activity"})}})
        denied = operations.operation("run")
        assert denied.journal is not None and denied.journal.logs == []
        assert denied.journal.actions == evidence.actions
        assert denied.journal.events == evidence.events
        history = operations.operations("shop").items[0]
        if saved:
            assert history.journal is not None and history.journal.logs == []
            assert history.journal.actions == evidence.actions
        else:
            assert history.journal is None
        assert "startup failed" not in denied.model_dump_json()
        assert "startup failed" not in history.model_dump_json()
        policy.replace({"viewer": {"shop": frozenset({"activity", "logs"})}})
        restored = operations.operation("run")
        assert restored.journal is not None and restored.journal.logs == evidence.logs
    assert operations.store.get("operation", "run") == stored
