"""One journaled, resumable ``piceli deploy`` run: inputs → build → deliver → plan → apply → checks.

Planning (:meth:`PipelineRunner.plan`) computes every stage's plan without
changing anything outside the state directory and binds them into one
**combined hash**. Executing (:meth:`PipelineRunner.execute`) needs that hash
(the approval), journals each stage, and skips a stage whose content identity
is unchanged:

``inputs``
    Scans each build's contexts (the build plan hash covers every staged
    file) and records the sources' git identity as provenance.
``build``
    Skipped when the last receipt has the same build plan hash and its images
    are still in the local engine.
``deliver``
    Skipped per image when the registry (``HEAD`` of the manifest digest) or
    the node (config digest behind the content tag) already has it. Images
    are handed to the release by the delivery receipt's immutable reference,
    never by tag.
``plan``
    A release plan through :class:`~piceli.k8s.release_runner.ReleaseRunner`
    against live discovery.
``apply``
    Skipped when the planned release is the one already deployed and ready
    and its remaining ``apply`` actions remove no field and differ only in
    what a plan cannot confirm; ``--reapply`` forces it.
``checks``
    Runs the pipeline's checks; the run is ``ready`` only when they pass.
    With ``rollback_on_failed_checks`` the previous release is re-applied and
    the rollback is journaled.

:meth:`PipelineRunner.resume` continues the latest unfinished run at its first
unfinished stage, reusing the outputs of the finished ones.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.pipeline.backend import Backend, RegistryRoute, public
from piceli.pipeline.checks import CheckContext, describe_check, describe_result
from piceli.pipeline.compose import (
    canonical,
    digest,
    model_fingerprint,
    pinned_images,
    registry_release_spec,
    release_spec,
    used_handles,
)
from piceli.pipeline.errors import PipelineError
from piceli.pipeline.journal import FINISHED, Journal, Run, now, write_private
from piceli.pipeline.model import (
    STAGES,
    Build,
    NodeImport,
    NodeLoopbackRegistry,
    Pipeline,
    Registry,
)
from piceli.pipeline.operate import delivery_path, delivery_receipt

if TYPE_CHECKING:
    from piceli.artifacts.build_spec import BuildPlan, BuildSpec
    from piceli.artifacts.source_identity import InputsLock, InputsSpec
    from piceli.k8s.release_runner import PlanResult, ReleaseRunner
    from piceli.k8s.release_spec import ImageRef

PLAN_SCHEMA = "piceli.deploy-plan.v1"
EVENT_SCHEMA = "piceli.deploy-event.v1"
EventSink = Callable[[dict[str, Any]], None]


def _hex(value: Any) -> str:
    return digest(value).removeprefix("sha256:")


# -------------------------------------------------------------- work state


@dataclass
class _BuildWork:
    build: Build
    spec: BuildSpec
    plan: BuildPlan
    inputs: InputsSpec | None
    lock: InputsLock | None
    directory: Path
    sources: dict[str, Any] | None = None
    cached: bool = False
    images: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def receipt_path(self) -> Path:
        return self.directory / "receipt.json"


@dataclass
class _Work:
    """What planning learned; execution continues from it."""

    builds: list[_BuildWork] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    producer: dict[str, _BuildWork] = field(default_factory=dict)
    registry_runner: ReleaseRunner | None = None
    registry_plan: PlanResult | None = None
    delivered: dict[str, ImageRef] = field(default_factory=dict)
    present: dict[str, bool] = field(default_factory=dict)
    runner: ReleaseRunner | None = None
    release_plan: PlanResult | None = None
    release_images: dict[str, str] = field(default_factory=dict)
    noop: bool = False


@dataclass
class CombinedPlan:
    """Every stage's plan and the one hash that approves them together."""

    until: str
    stages: dict[str, dict[str, Any]]
    hashed: dict[str, Any]
    combined_hash: str
    work: _Work = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "combined_hash": self.combined_hash,
            "until": self.until,
            "stages": self.stages,
        }


def _compact(result: PlanResult) -> list[dict[str, Any]]:
    return [
        {
            "operation": item["operation"],
            "kind": item["kind"],
            "name": item["name"],
            "artifact_digest": item["artifact_digest"],
        }
        for item in result.to_dict()["actions"]
    ]


def _changes(result: PlanResult) -> list[dict[str, Any]]:
    return [
        {"operation": item["operation"], "kind": item["kind"], "name": item["name"]}
        for item in result.to_dict()["actions"]
        if item["operation"] != "no-op"
    ]


def _drift(result: PlanResult) -> list[dict[str, Any]]:
    return [
        {
            "kind": item["resource"]["kind"],
            "name": item["resource"]["name"],
            "managers": list(item["managers"]),
        }
        for item in result.drift
    ]


def unchanged(runner: ReleaseRunner, result: PlanResult) -> bool:
    """Whether applying ``result`` would only re-apply what is already deployed.

    The release name is the fingerprint of the desired state (images,
    rendered objects, secret settings), so it is the apply stage's content
    identity. The apply is skipped when that release is the one deployed and
    ready, the plan creates, deletes, adopts and replaces nothing, and no
    other field manager owns a desired field (``drift``: someone edited,
    patched or scaled an object). Remaining ``apply`` actions are objects
    whose live form differs only by server defaults a dry run could not
    confirm; an action that removes fields (three-way removal) always runs.
    """
    history = runner.history
    entries = history.entries()
    deployed = history.deployed()
    if not entries or entries[-1]["state"] != "ready" or not deployed:
        return False
    if deployed[-1] != result.release or result.drift:
        return False
    actions = result.to_dict()["actions"]
    return not any("removes" in item for item in actions) and all(
        item["operation"] == "apply" for item in _changes(result)
    )


def classify(error: BaseException) -> PipelineError:
    """Map an engine error to a registered code; messages are safe to print."""
    from piceli.errors import ERRORS

    if isinstance(error, PipelineError):
        return error
    from piceli.artifacts.build_spec import FAILED_CODES, BuildSpecError
    from piceli.artifacts.source_identity import SourceDriftError, SourceIdentityError

    if isinstance(error, BuildSpecError):
        return PipelineError(error.code, str(error), failed=error.code in FAILED_CODES)
    if isinstance(error, SourceDriftError):
        return PipelineError("source-drift", str(error))
    if isinstance(error, SourceIdentityError):
        return PipelineError("source-identity", str(error))
    from piceli.k8s.ops.kubernetes_provider import ProviderError

    if isinstance(error, ProviderError):
        code = error.category if error.category in ERRORS else "provider-error"
        return PipelineError(code, str(error))
    code = getattr(error, "code", None)
    details = getattr(error, "details", None) or {}
    if isinstance(code, str) and code in ERRORS:
        return PipelineError(code, str(error), details=details)
    if isinstance(error, ValueError):
        return PipelineError("pipeline-release-refused", str(error), details=details)
    return PipelineError("pipeline-stage-error", f"{type(error).__name__}: {error}")


# ------------------------------------------------------------------ runner


class PipelineRunner:
    """Plan, execute and resume one :class:`~piceli.pipeline.Pipeline`.

    :param pipeline: The pipeline.
    :param backend: Side effects of the build and deliver stages.
    :param on_event: Receives one event per stage change (JSON-safe dicts).
    :param say: Receives human progress lines.
    :param check_runner: Overrides the checks runner (tests, custom checks).
    """

    def __init__(
        self,
        pipeline: Pipeline,
        *,
        backend: Backend | None = None,
        on_event: EventSink | None = None,
        say: Callable[[str], None] | None = None,
        check_runner: Any = None,
    ) -> None:
        self.pipeline = pipeline
        self.backend = backend or Backend()
        self.on_event = on_event or (lambda _event: None)
        self.say = say or (lambda _line: None)
        self.check_runner = check_runner
        self.journal = Journal(pipeline.state_dir)
        self.run: Run | None = None

    # ------------------------------------------------------------ helpers
    def identity(self) -> dict[str, Any]:
        pipeline = self.pipeline
        return {
            "app": pipeline.app.name,
            "owner": pipeline.owner,
            "field_manager": pipeline.field_manager,
            "target": pipeline.target.identity(),
        }

    def _emit(
        self, stage: str, state: str, detail: Mapping[str, Any] | None = None
    ) -> None:
        event = {
            "schema": EVENT_SCHEMA,
            "event": "stage",
            "run_id": self.run.run_id if self.run else None,
            "stage": stage,
            "state": state,
            "at": now(),
        }
        if detail:
            event["detail"] = json.loads(canonical(detail))
        self.on_event(event)

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.journal.locked():
            yield

    def _route(self) -> RegistryRoute:
        strategy = self.pipeline.deliver
        target = self.pipeline.target
        if isinstance(strategy, NodeLoopbackRegistry):
            return RegistryRoute(
                push=None,
                node_registry=f"127.0.0.1:{strategy.port}",
                forward=f"deployment/{strategy.name}",
                namespace=target.namespace,
                remote_port=strategy.port,
                kubeconfig=target.kubeconfig,
                context=target.context,
            )
        assert isinstance(strategy, Registry)
        from piceli.artifacts.registry import RegistryTarget

        parsed = RegistryTarget.parse(strategy.url + "/x")
        return RegistryRoute(
            push=parsed.registry,
            node_registry=strategy.node_registry,
            tls=parsed.tls,
            credentials=strategy.credentials,
            ca_file=strategy.ca_file,
        )

    def _repository(self, image: str) -> str:
        strategy = self.pipeline.deliver
        if isinstance(strategy, NodeLoopbackRegistry):
            prefix = strategy.repository or self.pipeline.app.name
            return f"{prefix}/{image}"
        if isinstance(strategy, Registry):
            from piceli.artifacts.registry import RegistryTarget

            prefix = RegistryTarget.parse(strategy.url + "/x").repository[: -len("/x")]
            return f"{prefix}/{image}" if prefix else image
        work = self._work_producer(image)
        ref = work.images.get(image, {}).get("ref") or ""
        return ref.rpartition(":")[0] if ":" in ref.rsplit("/", 1)[-1] else ref

    def _work_producer(self, image: str) -> _BuildWork:
        assert self._work is not None
        return self._work.producer[image]

    def _node_url(self) -> str:
        strategy = self.pipeline.deliver
        assert isinstance(strategy, NodeImport)
        if strategy.target is not None:
            return strategy.target
        _, node = self.pipeline.target.node(strategy.node)
        return f"docker://{node.name}?runtime=containerd"

    def _delivery_path(self, image: str, config: str) -> Path:
        """One receipt per image and config digest, so earlier images stay known."""
        return delivery_path(self.pipeline.state_dir, image, config)

    _work: _Work | None = None

    def _shown(self, path: Path) -> str:
        """``path`` for human output: relative to the working directory when
        under it, else to the state directory; never an absolute local path."""
        for base, prefix in (
            (Path.cwd(), ""),
            (self.pipeline.state_dir, "<state_dir>/"),
        ):
            try:
                return prefix + str(path.resolve().relative_to(base.resolve()))
            except ValueError:
                continue
        return path.name

    # ----------------------------------------------------------- planning
    def plan(self, until: str = "checks", *, reapply: bool = False) -> CombinedPlan:
        """Plan every stage up to ``until``; nothing outside the state dir changes."""
        if until not in STAGES:
            raise PipelineError("deploy-stage-unknown", f"unknown stage {until!r}")
        limit = STAGES.index(until)
        work = _Work()
        self._work = work
        stages: dict[str, dict[str, Any]] = {}
        hashed: dict[str, Any] = {}
        for index, name in enumerate(STAGES):
            if index > limit:
                stages[name] = {"state": "not-planned"}
                continue
            stages[name], hashed[name] = getattr(self, f"_plan_{name}")(work, reapply)
            self._emit(name, "planned", stages[name])
        combined = _hex(
            {
                "schema": PLAN_SCHEMA,
                "pipeline": self.identity(),
                "until": until,
                "stages": hashed,
            }
        )
        return CombinedPlan(until, stages, hashed, combined, work)

    def _plan_inputs(
        self, work: _Work, _reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from piceli.artifacts.source_identity import InputsLock

        pipeline = self.pipeline
        builds: dict[str, Any] = {}
        sources: dict[str, Any] = {}
        for build in pipeline.builds:
            try:
                spec = build.load()
                inputs = spec.load_inputs()
                lock = (
                    InputsLock.from_json(build.lock.read_text())
                    if build.lock is not None
                    else None
                )
                plan = spec.plan(inputs)
            except OSError as error:
                raise PipelineError("pipeline-invalid", str(error)) from None
            except Exception as error:
                raise classify(error) from None
            if any(item.spec.name == spec.name for item in work.builds):
                raise PipelineError(
                    "pipeline-invalid", f"two builds are named {spec.name!r}"
                )
            item = _BuildWork(
                build,
                spec,
                plan,
                inputs,
                lock,
                pipeline.state_dir / "builds" / spec.name,
            )
            if inputs is not None:
                try:
                    item.sources = self.backend.record_sources(inputs, lock)
                except Exception as error:
                    raise classify(error) from None
                for source in item.sources.get("sources", []):
                    sources[source["name"]] = {
                        "commit": source["commit"],
                        "dirty": source["dirty"],
                        "diff_sha256": source["diff_sha256"],
                    }
            for image in spec.images:
                if image.name in work.producer:
                    raise PipelineError(
                        "pipeline-invalid",
                        f"image {image.name!r} is produced by two builds",
                    )
                work.producer[image.name] = item
            work.builds.append(item)
            builds[spec.name] = {
                "plan_hash": plan.plan_hash,
                "contexts": {
                    name: {"files": len(manifest.files), "sha256": manifest.sha256}
                    for name, manifest in plan.contexts.items()
                },
            }
        work.used = used_handles(pipeline)
        unknown = [name for name in work.used if name not in work.producer]
        if unknown:
            raise PipelineError(
                "pipeline-image-unknown",
                f"the app uses build images {unknown} that no build produces",
            )
        stage = {"builds": builds, "sources": sources, "images": work.used}
        return stage, {
            "builds": {name: value["plan_hash"] for name, value in builds.items()}
        }

    def _plan_build(
        self, work: _Work, _reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        stage: dict[str, Any] = {}
        hashed: dict[str, Any] = {}
        for item in work.builds:
            item.cached, item.images = self._cached_build(item)
            stage[item.spec.name] = {
                "action": "cached" if item.cached else "build",
                "builder": item.spec.builder.ref,
                "network": item.spec.network,
                "platform": item.spec.platforms[0],
                "images": {
                    image.name: item.images.get(image.name, {}).get("image_id")
                    for image in item.spec.images
                },
            }
            hashed[item.spec.name] = {
                "builder": item.spec.builder.ref,
                "network": item.spec.network,
                "platform": item.spec.platforms[0],
                "images": sorted(image.name for image in item.spec.images),
            }
        return {"builds": stage}, hashed

    def _cached_build(self, item: _BuildWork) -> tuple[bool, dict[str, dict[str, Any]]]:
        path = item.receipt_path
        if not path.is_file():
            return False, {}
        try:
            receipt = json.loads(path.read_text())
            images = dict(receipt["outputs"]["images"])
        except (OSError, ValueError, KeyError, TypeError):
            return False, {}
        if receipt.get("plan_hash") != item.plan.plan_hash:
            return False, {}
        for entry in images.values():
            image_id = entry.get("image_id") if isinstance(entry, dict) else None
            if not isinstance(image_id, str) or not self.backend.image_present(
                image_id
            ):
                return False, {}
        return True, images

    def _plan_deliver(
        self, work: _Work, _reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        strategy = self.pipeline.deliver
        if not work.used:
            return {"action": "none"}, {"action": "none"}
        assert strategy is not None
        stage: dict[str, Any] = {"strategy": strategy.describe()}
        hashed: dict[str, Any] = {"strategy": strategy.describe()}
        registry_ready = True
        if isinstance(strategy, NodeLoopbackRegistry):
            spec = registry_release_spec(self.pipeline)
            runner = self.backend.release_runner(spec)
            try:
                result = runner.plan()
            except Exception as error:
                raise classify(error) from None
            work.registry_runner, work.registry_plan = runner, result
            changes = _changes(result)
            registry_ready = not any(
                item["operation"] in {"create", "replace"} for item in changes
            )
            stage["registry"] = {
                "action": "unchanged" if unchanged(runner, result) else "apply",
                "release": result.release,
                "mode": result.mode,
                "summary": result.counts,
                "changes": changes,
            }
            hashed["registry"] = {
                "release": result.release,
                "actions": _hex(_compact(result)),
            }
        images: dict[str, Any] = {}
        for name in work.used:
            config = work.producer[name].images.get(name, {}).get("image_id")
            present = False
            if config is not None and registry_ready:
                present = self._present(name, config, work)
            work.present[name] = present
            images[name] = {
                "config_digest": config,
                "action": "present"
                if present
                else ("deliver" if config else "pending-build"),
            }
        stage["images"] = images
        hashed["images"] = {
            name: value["config_digest"] or "pending-build"
            for name, value in images.items()
        }
        return stage, hashed

    def _receipt(self, name: str, config: str) -> tuple[Path, dict[str, Any]] | None:
        return delivery_receipt(self.pipeline.state_dir, name, config)

    def _present(self, name: str, config: str, work: _Work) -> bool:
        """The last delivery of this exact image is still in the registry/node."""
        from piceli.k8s.release_spec import ImageHandoffError, load_delivery_receipt

        found = self._receipt(name, config)
        if found is None:
            return False
        path, receipt = found
        strategy = self.pipeline.deliver
        if isinstance(strategy, NodeImport):
            reference = (receipt.get("image") or {}).get("reference")
            if not isinstance(reference, str) or receipt.get("target") is None:
                return False
            if not self.backend.node_present(self._node_url(), reference, config):
                return False
        else:
            route = self._route()
            repository = self._repository(name)
            target = receipt.get("target") or {}
            manifest = (receipt.get("image") or {}).get("manifest_digest")
            if (
                target.get("repository") != repository
                or receipt.get("node_registry")
                != (route.node_registry or target.get("registry"))
                or not isinstance(manifest, str)
            ):
                return False
            if not self.backend.registry_present(route, [(repository, manifest)])[0]:
                return False
        try:
            work.delivered[name] = load_delivery_receipt(name, path)
        except ImageHandoffError:
            return False
        return True

    def _plan_plan(
        self, work: _Work, reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fingerprint = model_fingerprint(self.pipeline)
        if any(name not in work.delivered for name in work.used):
            return (
                {"state": "pending", "why": "images are not delivered yet"},
                {"model": fingerprint},
            )
        result = self._release_plan(work)
        drift = _drift(result)
        stage = {
            "state": "planned",
            "release": result.release,
            "mode": result.mode,
            "summary": result.counts,
            "changes": _changes(result),
            "drift": drift,
            "images": work.release_images,
        }
        return stage, {
            "model": fingerprint,
            "release": result.release,
            "actions": _hex(_compact(result)),
            "drift": drift,
        }

    def _release_images(self, work: _Work) -> dict[str, ImageRef]:
        images: dict[str, ImageRef] = dict(pinned_images(self.pipeline, work.producer))
        for name in work.used:
            images[name] = work.delivered[name]
        return images

    def _release_plan(self, work: _Work) -> PlanResult:
        images = self._release_images(work)
        spec = release_spec(self.pipeline, images)
        runner = self.backend.release_runner(spec)
        try:
            result = runner.plan()
        except Exception as error:
            raise classify(error) from None
        work.runner, work.release_plan = runner, result
        work.release_images = {name: ref.identity for name, ref in images.items()}
        return result

    def _unchanged(self, work: _Work, reapply: bool) -> bool:
        if reapply or work.runner is None or work.release_plan is None:
            return False
        return unchanged(work.runner, work.release_plan)

    def _plan_apply(
        self, work: _Work, reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if work.release_plan is None:
            return {"action": "pending"}, {"reapply": reapply}
        work.noop = self._unchanged(work, reapply)
        return (
            {
                "action": "skip" if work.noop else "apply",
                "release": work.release_plan.release,
            },
            {"reapply": reapply},
        )

    def _plan_checks(
        self, _work: _Work, _reapply: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        described = [describe_check(check) for check in self.pipeline.checks]
        value = {
            "checks": described,
            "rollback_on_failed_checks": self.pipeline.rollback_on_failed_checks,
        }
        return dict(value), value

    # ---------------------------------------------------------- execution
    def execute(
        self, plan: CombinedPlan, approval: str, *, reapply: bool = False
    ) -> dict[str, Any]:
        """Run an approved plan (``approval`` must equal its combined hash)."""
        if approval != plan.combined_hash:
            raise PipelineError(
                "pipeline-plan-changed",
                "the approved hash is not the current combined plan; plan again",
            )
        self._work = plan.work
        self.run = self.journal.create(
            pipeline=self.identity(),
            combined_hash=plan.combined_hash,
            until=plan.until,
            approval=approval,
            plan={"hashed": plan.hashed, "stages": plan.stages},
        )
        return self._continue(plan.until, reapply=reapply)

    def resume(self) -> dict[str, Any]:
        """Continue the latest unfinished run at its first unfinished stage."""
        run = self.journal.latest()
        if run is None or run.state in FINISHED:
            raise PipelineError(
                "pipeline-nothing-to-resume",
                "no interrupted or failed deploy run to resume",
            )
        if run.data["pipeline"] != self.identity():
            raise PipelineError(
                "pipeline-resume-changed",
                "the pipeline's app, owner or target differs from the run's",
            )
        self.run = run
        self._work = self._restore(run)
        run.set_state("running", resumed_at=now())
        return self._continue(str(run.data["until"]), reapply=False, resuming=True)

    def _restore(self, run: Run) -> _Work:
        """Rebuild the work state from the journal (finished stages keep outputs)."""
        from piceli.artifacts.source_identity import InputsLock
        from piceli.k8s.release_spec import load_delivery_receipt

        work = _Work()
        approved = run.data["plan"]["hashed"].get("inputs", {}).get("builds", {})
        build_out = run.output("build")
        for build in self.pipeline.builds:
            try:
                spec = build.load()
                inputs = spec.load_inputs()
                lock = (
                    InputsLock.from_json(build.lock.read_text()) if build.lock else None
                )
                plan = spec.plan(inputs)
            except Exception as error:
                raise classify(error) from None
            item = _BuildWork(
                build,
                spec,
                plan,
                inputs,
                lock,
                self.pipeline.state_dir / "builds" / spec.name,
            )
            done = build_out.get(spec.name)
            if done:
                item.cached = True
                item.images = dict(done["images"])
            elif approved.get(spec.name) != plan.plan_hash:
                raise PipelineError(
                    "pipeline-resume-changed",
                    f"build {spec.name!r} changed since the run was approved; "
                    "plan and approve a new run",
                )
            for image in spec.images:
                work.producer[image.name] = item
            work.builds.append(item)
        work.used = used_handles(self.pipeline)
        if run.stage("deliver").get("state") in {"done", "skipped"}:
            for name, entry in run.output("deliver").get("images", {}).items():
                work.delivered[name] = load_delivery_receipt(
                    name, Path(entry["receipt"])
                )
                work.present[name] = True
        return work

    def _continue(
        self, until: str, *, reapply: bool, resuming: bool = False
    ) -> dict[str, Any]:
        assert self.run is not None
        run = self.run
        limit = STAGES.index(until)
        for name in STAGES[: limit + 1]:
            if run.stage(name).get("state") in {"done", "skipped"}:
                continue
            started = time.monotonic()
            run.set_stage(name, state="running")
            self._emit(name, "running")
            try:
                state, output = getattr(self, f"_run_{name}")(reapply, resuming)
            except KeyboardInterrupt:
                run.set_stage(name, state="interrupted")
                run.set_state("interrupted")
                self._emit(name, "interrupted")
                raise
            except BaseException as error:
                failure = classify(error)
                result = "failed" if failure.failed else "rejected"
                run.set_stage(
                    name,
                    state=result,
                    reason=failure.code,
                    message=str(failure),
                    seconds=round(time.monotonic() - started, 3),
                    **(
                        {"output": failure.details.get("output")}
                        if "output" in failure.details
                        else {}
                    ),
                )
                final = failure.details.get("run_state", "failed")
                run.set_state(final, reason=failure.code)
                self._emit(name, result, {"reason": failure.code})
                raise failure from None
            run.set_stage(
                name,
                state=state,
                output=output,
                seconds=round(time.monotonic() - started, 3),
            )
            self._emit(name, state, output)
        final = "ready" if limit == len(STAGES) - 1 else "stopped"
        run.set_state(final)
        return self.result(final)

    def result(self, state: str) -> dict[str, Any]:
        assert self.run is not None
        run = self.run
        stages = {name: run.stage(name).get("state") for name in STAGES}
        plan = run.output("plan")
        apply = run.output("apply")
        return {
            "schema": EVENT_SCHEMA,
            "event": "result",
            "state": state,
            "run_id": run.run_id,
            "combined_hash": run.data["combined_hash"],
            "stages": stages,
            "release": apply.get("release") or plan.get("release"),
            "images": plan.get("images", {}),
        }

    # -------------------------------------------------------- stage: inputs
    def _run_inputs(
        self, _reapply: bool, _resuming: bool
    ) -> tuple[str, dict[str, Any]]:
        work = self._work
        assert work is not None
        if not work.builds:
            return "skipped", {"why": "no build"}
        return "done", {
            "builds": {item.spec.name: item.plan.plan_hash for item in work.builds},
            "sources": {
                source["name"]: {
                    key: source.get(key)
                    for key in ("commit", "dirty", "diff_sha256", "changed_paths")
                }
                for item in work.builds
                for source in (item.sources or {}).get("sources", [])
            },
        }

    # --------------------------------------------------------- stage: build
    def _run_build(self, _reapply: bool, _resuming: bool) -> tuple[str, dict[str, Any]]:
        from piceli.artifacts.build_spec import BuildGrant

        work = self._work
        assert work is not None
        if not work.builds:
            return "skipped", {"why": "no build"}
        output: dict[str, Any] = {}
        built = False
        for item in work.builds:
            name = item.spec.name
            if not item.cached:
                item.cached, item.images = self._cached_build(item)
            if item.cached:
                self.say(f"[build] {name}: cached (plan {item.plan.plan_hash[7:19]})")
            else:
                log = self._shown(item.directory / "build.log")
                self.say(f"[build] {name}: building (log: {log})")
                grant = BuildGrant(
                    item.spec.builder.digest,
                    time.time() + item.spec.timeout_seconds + 3600,
                    item.spec.network != "none",
                    item.plan.plan_hash,
                )
                item.directory.mkdir(parents=True, exist_ok=True)

                def progress(line: str, name: str = name) -> None:
                    self.say(f"[build] {name}: {line}")

                receipt = self.backend.build(
                    item.spec,
                    grant,
                    item.directory / "outputs",
                    inputs=item.inputs,
                    lock=item.lock,
                    log=item.directory / "build.log",
                    progress=progress,
                )
                write_private(
                    item.receipt_path, json.dumps(receipt, sort_keys=True, indent=2)
                )
                item.images = dict(receipt["outputs"]["images"])
                built = True
            output[name] = {
                "cached": item.cached,
                "plan_hash": item.plan.plan_hash,
                "receipt": str(item.receipt_path),
                "images": {
                    image: {
                        key: entry.get(key)
                        for key in ("image_id", "digest", "ref", "platform")
                    }
                    for image, entry in item.images.items()
                },
            }
        return ("done" if built else "skipped"), output

    # ------------------------------------------------------- stage: deliver
    def _run_deliver(
        self, _reapply: bool, resuming: bool
    ) -> tuple[str, dict[str, Any]]:
        from piceli.k8s.release_spec import load_delivery_receipt

        work = self._work
        assert work is not None
        if not work.used:
            return "skipped", {"why": "no built image is used"}
        strategy = self.pipeline.deliver
        output: dict[str, Any] = {"strategy": strategy.kind if strategy else None}
        acted = False
        if isinstance(strategy, NodeLoopbackRegistry):
            registry, changed = self._ensure_registry(work, resuming)
            output["registry"] = registry
            acted = acted or changed
        images: dict[str, Any] = {}
        for name in work.used:
            config = work.producer[name].images[name]["image_id"]
            ref = work.delivered.get(name)
            if ref is None or ref.image_id != config:
                work.delivered.pop(name, None)
                if not self._present(name, config, work):
                    result = self._deliver(name, config)
                    work.delivered[name] = load_delivery_receipt(
                        name, self._delivery_path(name, config)
                    )
                    acted = True
                    images[name] = {"action": result}
                else:
                    images[name] = {"action": "present"}
            else:
                images[name] = {"action": "present"}
            delivered = work.delivered[name]
            if delivered.image_id != config:
                raise PipelineError(
                    "image-digest-mismatch",
                    f"image {name!r}: the delivery receipt is not the built image",
                )
            images[name].update(
                reference=delivered.reference,
                identity=delivered.identity,
                config_digest=config,
                receipt=str(self._delivery_path(name, config)),
            )
            self.say(
                f"[deliver] {name}: {images[name]['action']} {delivered.reference}"
            )
        output["images"] = images
        return ("done" if acted else "skipped"), output

    def _ensure_registry(
        self, work: _Work, resuming: bool
    ) -> tuple[dict[str, Any], bool]:
        """Apply the node-loopback registry's release when its plan changes anything."""
        runner, result = work.registry_runner, work.registry_plan
        if runner is None or result is None or resuming:
            runner = self.backend.release_runner(registry_release_spec(self.pipeline))
            result = runner.plan()
            work.registry_runner, work.registry_plan = runner, result
        info: dict[str, Any] = {"release": result.release, "summary": result.counts}
        if unchanged(runner, result):
            info["action"] = "unchanged"
            return info, False
        self.say(f"[deliver] registry {result.release}: applying")
        outcome = runner.apply(result.plan_hash)
        info["action"] = "applied"
        info["execution"] = outcome["execution"]
        if outcome["execution"]["state"] != "ready":
            raise PipelineError(
                "pipeline-registry-not-ready",
                "the node-loopback registry did not become ready",
                failed=True,
                details={"output": {"registry": info}},
            )
        return info, True

    def _deliver(self, name: str, config: str) -> str:
        """Deliver one image; returns the receipt's result (pushed, imported …)."""
        strategy = self.pipeline.deliver
        if isinstance(strategy, NodeImport):
            from piceli.k8s.release_spec import content_tag

            reference = f"{self._repository(name)}:{content_tag(config)}"
            self.say(f"[deliver] {name}: importing into the node as {reference}")
            receipt = self.backend.node_deliver(self._node_url(), config, reference)
        else:
            route = self._route()
            repository = self._repository(name)
            self.say(f"[deliver] {name}: pushing to {repository}")
            receipt = self.backend.registry_deliver(route, config, repository)
        path = self._delivery_path(name, config)
        write_private(path, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        if receipt.get("state") != "succeeded":
            from piceli.errors import ERRORS

            reason = receipt.get("reason")
            failed = receipt.get("state") == "failed"
            details = {"output": {"images": {name: public(receipt)}}}
            message = f"delivery of image {name!r} did not succeed ({reason})"
            if not (isinstance(reason, str) and reason in ERRORS):
                raise PipelineError(
                    "pipeline-delivery-failed", message, failed=failed, details=details
                )
            raise PipelineError(reason, message, failed=failed, details=details)
        return str(receipt.get("result") or "delivered")

    # ---------------------------------------------------------- stage: plan
    def _run_plan(self, _reapply: bool, resuming: bool) -> tuple[str, dict[str, Any]]:
        work = self._work
        assert work is not None
        images = self._release_images(work)
        identities = {name: ref.identity for name, ref in images.items()}
        if work.release_plan is None or work.release_images != identities or resuming:
            result = self._release_plan(work)
        else:
            result = work.release_plan
        if resuming:
            approved = (
                self.run.data["plan"]["hashed"].get("plan", {}) if self.run else {}
            )
            if approved.get("release") not in (None, result.release):
                raise PipelineError(
                    "pipeline-resume-changed",
                    "the release differs from the approved one; plan again",
                )
        self.say(
            f"[plan] release {result.release} ({result.mode}): "
            + (
                ", ".join(f"{n} {op}" for op, n in result.counts.items())
                or "no actions"
            )
        )
        return "done", {
            "release": result.release,
            "mode": result.mode,
            "plan_hash": result.plan_hash,
            "summary": result.counts,
            "changes": _changes(result),
            "images": work.release_images,
            "source": result.source,
        }

    # --------------------------------------------------------- stage: apply
    def _run_apply(self, reapply: bool, resuming: bool) -> tuple[str, dict[str, Any]]:
        from piceli.k8s.release_runner import ReleaseError

        work = self._work
        assert work is not None and self.run is not None
        planned = self.run.output("plan")
        if work.runner is None or work.release_plan is None:
            self._release_plan(work)
        assert work.runner is not None and work.release_plan is not None
        runner = work.runner
        release = planned.get("release") or work.release_plan.release
        plan_hash = planned.get("plan_hash") or work.release_plan.plan_hash
        if self._unchanged(work, reapply):
            self.say(f"[apply] {release}: unchanged, already deployed and ready")
            return "skipped", {"release": release, "why": "unchanged"}
        outcome: dict[str, Any] | None = None
        if resuming:
            entry = next(
                (
                    item
                    for item in reversed(runner.history.entries())
                    if item.get("plan_hash") == plan_hash
                ),
                None,
            )
            if entry is not None and entry["state"] == "ready":
                return "done", {"release": release, "execution": {"state": "ready"}}
            if (
                entry is not None
                and entry["mode"] == "create"
                and entry["state"]
                # "refused" is the history state of releases before 0.4.1.
                not in {"cancelled", "rejected", "refused"}
            ):
                self.say(f"[apply] {release}: resuming the interrupted execution")
                outcome = runner.resume(release)
        if outcome is None:
            try:
                self.say(f"[apply] {release}: applying")
                outcome = runner.apply(plan_hash)
            except ReleaseError:
                if not resuming:
                    raise
                result = runner.plan()
                work.release_plan = result
                outcome = runner.apply(result.plan_hash)
        output = {
            "release": outcome.get("release", release),
            "execution": outcome["execution"],
            **({"source": outcome["source"]} if "source" in outcome else {}),
        }
        if outcome["execution"]["state"] != "ready":
            raise PipelineError(
                "pipeline-apply-not-ready",
                f"release {release} did not become ready "
                f"({outcome['execution'].get('failure_category', 'not ready')})",
                failed=True,
                details={"output": output},
            )
        return "done", output

    # -------------------------------------------------------- stage: checks
    def _run_checks(
        self, _reapply: bool, _resuming: bool
    ) -> tuple[str, dict[str, Any]]:
        work = self._work
        assert work is not None and self.run is not None
        pipeline = self.pipeline
        if not pipeline.checks:
            return "skipped", {"passed": True, "why": "no checks"}
        release = self.run.output("plan").get("release")
        if self.run.stage("apply").get(
            "state"
        ) == "skipped" and self.journal.last_verified(str(release)):
            self.say(f"[checks] {release}: already verified")
            return "skipped", {"passed": True, "why": "release already verified"}
        runner = self.check_runner or self.backend.check_runner()
        context = CheckContext(
            target=pipeline.target,
            kubeconfig=pipeline.target.kubeconfig.absolute(),
            context=pipeline.target.context,
            namespace=pipeline.target.namespace,
            release=str(release),
            images={name: ref.reference for name, ref in work.delivered.items()},
            app=pipeline.app,
            state_dir=pipeline.state_dir,
            base=pipeline.base,
        )
        report = runner(pipeline.checks, context)
        passed = bool(report.passed)
        output: dict[str, Any] = {
            "passed": passed,
            "results": [describe_result(item) for item in report.results],
        }
        self.say(f"[checks] {release}: {'passed' if passed else 'FAILED'}")
        if passed:
            return "done", output
        run_state = "failed"
        if pipeline.rollback_on_failed_checks:
            output["rollback"] = self._rollback(work)
            if output["rollback"].get("state") == "ready":
                run_state = "rolled-back"
        raise PipelineError(
            "pipeline-checks-failed",
            f"release {release} failed its checks",
            failed=True,
            details={"output": output, "run_state": run_state},
        )

    def _rollback(self, work: _Work) -> dict[str, Any]:
        """Re-apply the previous release (journaled in the run's checks stage)."""
        from piceli.k8s.release_runner import ReleaseError

        runner = work.runner
        if runner is None:
            # Resuming at the checks stage: the release was planned and applied
            # by an earlier invocation, so bind the runner without re-planning.
            runner = work.runner = self.backend.release_runner(
                release_spec(self.pipeline, self._release_images(work))
            )
        try:
            target = runner.resolve_rollback_target("previous")
        except ReleaseError as error:
            self.say(f"[checks] rollback: not possible ({error})")
            return {
                "state": "unavailable",
                "reason": "checks-rollback-unavailable",
                "message": str(error),
            }
        self.say(f"[checks] rolling back to {target}")
        try:
            result = runner.plan(rollback_to="previous")
            outcome = runner.apply(
                result.plan_hash, expected_intent="rollback", expected_release=target
            )
        except Exception as error:
            failure = classify(error)
            self.say(f"[checks] rollback to {target} rejected: {failure}")
            return {
                "state": "rejected",
                "release": target,
                "reason": failure.code,
                "message": str(failure),
            }
        return {
            "state": outcome["execution"]["state"],
            "release": target,
            "execution": outcome["execution"],
        }
