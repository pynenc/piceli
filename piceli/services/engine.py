"""Adapters to the existing release engine; no service planner or executor."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.checks import PythonCheck
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.provider_factory import build_provider
from piceli.k8s.release_runner import ReleaseRunner
from piceli.k8s.release_secrets import input_names
from piceli.k8s.release_spec import NodeRef, ReleaseSpec
from piceli.services.contracts import (
    EvaluationPreview,
    ExecutionJournalRecord,
    PlanRecord,
    ReleaseSummary,
    ResourceDiff,
    ResourceIdentity,
)
from piceli.services.plan_evidence import desired_resources, plan_steps
from piceli.services.query import QueryError
from piceli.services.registration import Registration
from piceli.services.store import digest
from piceli.state import session
from piceli.state.scopes import release_scope

if TYPE_CHECKING:
    from piceli.services.evaluation import RenderedComposition, RenderInputs


def freeze_spec(spec: ReleaseSpec) -> dict[str, Any]:
    return {
        "model": spec.model.model_dump(mode="json", exclude_unset=True),
        "base": str(spec.base),
    }


def thaw_spec(value: Mapping[str, Any]) -> ReleaseSpec:
    return ReleaseSpec.from_dict(value["model"], Path(value["base"]))


def unexpired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(UTC)
    except (ValueError, TypeError):
        return False


class EngineAdapter:
    def __init__(
        self, *, runner_factory: Callable[[ReleaseSpec], ReleaseRunner] = ReleaseRunner
    ) -> None:
        self.runner_factory = runner_factory

    def inputs(self, registration: Registration) -> tuple[ReleaseSpec, RenderInputs]:
        from piceli.services.evaluation import RenderInputs

        spec = registration.release_spec
        if (
            spec is None
            or registration.definition_kind != "release"
            or registration.ownership != "native"
        ):
            raise QueryError("ui-operation-unavailable", 409)
        if any(isinstance(check, PythonCheck) for check in spec.model.checks):
            raise QueryError("ui-operation-unavailable", 409)
        binding = build_provider(
            registration.target,
            field_manager=spec.model.release.field_manager,
            owner_id=spec.model.release.owner,
        )
        try:
            target = spec.model.target.model_copy(
                update={
                    "cluster_uid": binding.identity.cluster_uid,
                    "namespace_uid": binding.identity.namespace_uid,
                }
            )
            pinned = replace(
                spec, model=spec.model.model_copy(update={"target": target})
            )
            inputs = RenderInputs(
                namespace=target.namespace,
                images=pinned.images(),
                values=dict(pinned.model.values),
                secret_names=tuple(
                    name
                    for group, generator in pinned.model.secrets.items()
                    for name in input_names(group, generator)
                ),
                nodes={
                    alias: NodeRef(node.name, str(node.uid))
                    for alias, node in binding.identity.nodes.items()
                },
            )
            return pinned, inputs
        finally:
            binding.close()

    @staticmethod
    def _prerollout_stage(
        spec: ReleaseSpec, rendered: RenderedComposition
    ) -> tuple[Any, Any]:
        """Reuse the pipeline check Job runner for validated renderer declarations."""
        from piceli import App, Pipeline, Target
        from piceli.app.prerollout import PreRollout
        from piceli.app.render import placeholder_inputs
        from piceli.pipeline.backend import Backend
        from piceli.pipeline.prerollout import _collect
        from piceli.pipeline.prerollout_stage import PreRolloutStage

        app = App(spec.model.release.name)
        app._pre_rollouts = [
            PreRollout.model_validate(item) for item in rendered.pre_rollouts
        ]
        target = spec.kubeconfig_target()
        pipeline = Pipeline(
            app,
            Target.kubeconfig(
                target.kubeconfig,
                context=target.context,
                namespace=target.namespace,
                cluster_uid=target.cluster_uid,
                namespace_uid=target.namespace_uid,
                transport=target.transport,
            ),
            state_dir=spec.state_dir,
        )
        backend = Backend()
        stage = PreRolloutStage(
            pipeline,
            lambda: backend.prerollout_cluster(pipeline.target),
            lambda _message: None,
        )
        context = rendered.inputs.context(
            placeholder_inputs(list(rendered.inputs.secret_names))
        )
        return stage, _collect(rendered.composition(context))

    def plan(
        self,
        registration: Registration,
        spec: ReleaseSpec,
        rendered: RenderedComposition,
        preview: EvaluationPreview,
        evaluation_id: str,
    ) -> tuple[PlanRecord, dict[str, Any]]:
        from piceli.services.evaluation import materialize_spec

        materialized = materialize_spec(spec, rendered)
        runner = self.runner_factory(materialized)
        with session(release_scope(spec), write=True) as held:
            runner.checkpoint = lambda: held.checkpoint(force=True)
            result = runner.plan(
                rollback_to=preview.release if preview.intent == "rollback" else None
            )
            pending = runner.pending_plan(result.plan_hash)
        precheck: dict[str, Any] | None = None
        if rendered.pre_rollouts:
            stage, desired = self._prerollout_stage(spec, rendered)
            precheck, _ = stage.plan(desired)
        target = replace(registration, target=spec.kubeconfig_target()).public_target()
        frozen = {
            "spec": freeze_spec(spec),
            "rendered": rendered.to_dict(),
            "pending_digest": digest(pending),
            "pending": pending,
            "mode": result.mode,
        }
        if precheck is not None:
            frozen["pre_rollout"] = precheck
        envelope = {
            "application_id": registration.id,
            "engine_digest": result.plan_hash,
            "target": target.model_dump(mode="json"),
            "evaluation_digest": preview.digest,
            "frozen_digest": digest(frozen),
            "intent": preview.intent,
            "release": result.release,
        }
        authorization_digest = digest(envelope)
        diffs = []
        for value in result.diffs:
            resource = ResourceIdentity(target_id=target.id, **value["resource"])
            diffs.append(
                ResourceDiff(
                    resource=resource,
                    **{key: item for key, item in value.items() if key != "resource"},
                )
            )
        desired, desired_complete = desired_resources(result.plan, target.id)
        record = PlanRecord(
            id=authorization_digest,
            application_id=registration.id,
            digest=authorization_digest,
            engine_digest=result.plan_hash,
            target=target,
            source=preview.source,
            intent=preview.intent,
            release=result.release,
            expires_at=result.expires_at,
            summary=result.counts,
            diffs=diffs,
            actions=result.to_dict()["actions"],
            checks={**result.checks, **({"pre_rollout": precheck} if precheck else {})},
            evaluation_id=evaluation_id,
            precondition_digest=digest(result.plan),
            warnings=["dry-run-unavailable"] if result.dry_run_unavailable else [],
            steps=plan_steps(result.plan, target.id),
            desired_resources=desired,
            desired_resources_complete=desired_complete,
        )
        return record, {"frozen": frozen, "envelope": envelope}

    def validate(
        self, plan: PlanRecord, private: Mapping[str, Any], *, pending: bool = True
    ) -> ReleaseSpec:
        frozen = private["frozen"]
        envelope = private["envelope"]
        if (
            digest(envelope) != plan.digest
            or envelope["frozen_digest"] != digest(frozen)
            or envelope["engine_digest"] != plan.engine_digest
            or envelope["target"] != plan.target.model_dump(mode="json")
        ):
            raise QueryError("ui-state-invalid", 409)
        if not unexpired(plan.expires_at):
            raise QueryError("ui-plan-stale", 409)
        spec = thaw_spec(frozen["spec"])
        if pending:
            runner = self.runner_factory(spec)
            try:
                current = runner.pending_plan(str(plan.engine_digest))
            except (ValueError, OSError):
                raise QueryError("ui-plan-stale", 409) from None
            if digest(current) != frozen["pending_digest"]:
                raise QueryError("ui-plan-stale", 409)
        return spec

    def execute(
        self,
        plan: PlanRecord,
        private: Mapping[str, Any],
        *,
        on_start: Callable[[dict[str, Any]], None],
        on_progress: Callable[[str], None],
        resume: bool = False,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        from piceli.services.evaluation import RenderedComposition, materialize_spec

        spec = self.validate(plan, private, pending=False)
        frozen = private["frozen"]
        rendered = RenderedComposition.from_dict(frozen["rendered"])
        runner = self.runner_factory(materialize_spec(spec, rendered))
        with session(release_scope(spec), write=True) as held:
            runner.checkpoint = lambda: held.checkpoint(force=True)
            runner.before_execution = on_start
            runner.progress = on_progress
            # Refresh shared state under its lease, then recheck reviewed metadata.
            self.validate(plan, private, pending=not resume)
            if rendered.pre_rollouts and not resume:
                stage, desired = self._prerollout_stage(spec, rendered)
                current, _ = stage.plan(desired)
                if current != frozen.get("pre_rollout"):
                    raise QueryError("ui-plan-stale", 409)
                changed = {
                    (str(action["kind"]), str(action["name"]))
                    for action in plan.actions
                    if action.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}
                    and action.get("operation") != "noop"
                }
                stage.run(desired, changed=changed, run_id=plan.id[:12])
            if resume:
                if frozen["mode"] != "create" or execution_id is None:
                    raise QueryError("ui-operation-unavailable", 409)
                latest = runner._latest(plan.release)
                if (
                    latest["execution_id"] != execution_id
                    or latest["plan_hash"] != plan.engine_digest
                ):
                    raise QueryError("ui-plan-stale", 409)
                return runner.resume(plan.release)
            return runner.apply(
                str(plan.engine_digest),
                expected_intent="rollback" if plan.intent == "rollback" else "apply",
                expected_release=plan.release,
            )

    def inspect(
        self, plan: PlanRecord, private: Mapping[str, Any], execution_id: str
    ) -> dict[str, Any]:
        spec = thaw_spec(private["frozen"]["spec"])
        runner = self.runner_factory(spec)
        with session(release_scope(spec), write=False):
            result = runner.run(execution_id)
            path = spec.state_dir / "checks" / f"{execution_id}.json"
            if path.is_file():
                checked = json.loads(path.read_text())
                if checked.get("execution_id") == execution_id:
                    result["checks"] = {"passed": checked.get("passed")}
            return result

    def journal(
        self,
        registration: Registration,
        execution_id: str,
        engine_digest: str,
        private: Mapping[str, Any] | None = None,
    ) -> ExecutionJournalRecord | None:
        """Recorded evidence only, bound to this execution and its frozen plan."""
        from piceli.services.execution_evidence import read_journal

        spec = (
            thaw_spec(private["frozen"]["spec"])
            if private is not None
            else registration.release_spec
        )
        if spec is None:
            return None
        target = replace(registration, target=spec.kubeconfig_target()).public_target()
        with session(release_scope(spec), write=False):
            return read_journal(
                spec.journal_path, execution_id, engine_digest, target.id
            )

    def cancel(self, private: Mapping[str, Any], execution_id: str) -> None:
        """Persist cancellation without waiting for the active runner's lease.

        Only the owning server's local working journal is touched. Its runner
        checkpoints cancellation through the held state session before exit.
        """
        spec = thaw_spec(private["frozen"]["spec"])
        if not spec.journal_path.exists():
            return
        journal = ExecutionJournal(spec.journal_path)
        try:
            journal.cancel(execution_id)
        finally:
            journal.close()

    def history(self, registration: Registration) -> dict[str, Any]:
        if registration.release_spec is None:
            return {"history": [], "releases": []}
        spec = registration.release_spec
        with session(release_scope(spec), write=False):
            runner = self.runner_factory(spec)
            result = runner.status()
            result["history"] = (
                runner.history.entries() if spec.state_dir.exists() else []
            )
            return result

    def policy_plan(
        self, parent: PlanRecord, private: Mapping[str, Any], entry: Mapping[str, Any]
    ) -> tuple[PlanRecord, dict[str, Any]]:
        """Capture the actual automatic rollback before its first write."""
        if entry.get("trigger") != "checks-failed" or not private["frozen"][
            "pending"
        ].get("rollback_on_failed_checks"):
            raise QueryError("ui-approval-mismatch", 409)
        frozen = dict(private["frozen"])
        pending = self.runner_factory(thaw_spec(frozen["spec"])).pending_plan(
            entry["plan_hash"]
        )
        frozen.update(
            pending=pending, pending_digest=digest(pending), mode=pending["mode"]
        )
        envelope = {
            **private["envelope"],
            "engine_digest": entry["plan_hash"],
            "frozen_digest": digest(frozen),
            "intent": "rollback",
            "release": entry["release"],
            "standing_policy": parent.digest,
        }
        identity = digest(envelope)
        reviewed = entry["reviewed"]
        actions = reviewed["plan"]["actions"]
        counts: dict[str, int] = {}
        for action in actions:
            counts[action["operation"]] = counts.get(action["operation"], 0) + 1
        diffs = [
            ResourceDiff(
                resource=ResourceIdentity(
                    target_id=parent.target.id, **value["resource"]
                ),
                **{key: value for key, value in value.items() if key != "resource"},
            )
            for value in reviewed["diffs"]
        ]
        desired, desired_complete = desired_resources(
            reviewed["plan"], parent.target.id
        )
        plan = parent.model_copy(
            update={
                "id": identity,
                "digest": identity,
                "engine_digest": entry["plan_hash"],
                "intent": "rollback",
                "release": entry["release"],
                "expires_at": reviewed["expires_at"],
                "summary": counts,
                "diffs": diffs,
                "actions": actions,
                "checks": reviewed["checks"],
                "authorization": "policy",
                "policy_digest": parent.digest,
                "precondition_digest": digest(reviewed["plan"]),
                "steps": plan_steps(reviewed["plan"], parent.target.id),
                "desired_resources": desired,
                "desired_resources_complete": desired_complete,
            }
        )
        return plan, {"frozen": frozen, "envelope": envelope}

    def releases(self, registration: Registration) -> list[ReleaseSummary]:
        report = self.history(registration)
        result = []
        for release in report.get("releases", []):
            entries = [
                entry
                for entry in report.get("history", [])
                if entry["release"] == release["name"]
            ]
            result.append(
                ReleaseSummary(
                    name=release["name"],
                    selected=release["name"] == report.get("selected"),
                    state=entries[-1]["state"] if entries else "not-started",
                    created_at=release.get("created_at"),
                )
            )
        return result
