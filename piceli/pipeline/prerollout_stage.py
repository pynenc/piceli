"""The ``prerollout`` stage: plan the declared checks, run them before any change.

The stage exists only for a pipeline whose app declares a pre-rollout check
(:meth:`piceli.app.App.pre_rollout`). Planning reads the cluster (never writes)
and refuses a mount that cannot work. Running creates one Job per check with
the new image and the workload's real pod settings, waits for it, and removes
it again; the release proceeds only if every check passed, and nothing of the
release has been applied when one fails.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from typing import TYPE_CHECKING, Any

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.prerollout import (
    Reference,
    Rendered,
    check_job,
    claim_names,
    describe_checks,
    references,
    replicas,
    staged_copy,
    staged_name,
    staged_values,
)
from piceli.pipeline.prerollout_cluster import (
    Outcome,
    PreRolloutCluster,
    Unreadable,
)

if TYPE_CHECKING:
    from piceli.app.prerollout import PreRollout
    from piceli.pipeline.model import Pipeline

ClusterFactory = Callable[[], PreRolloutCluster]
#: ``release_config(wanted)`` → the full manifests (values resolved) of the
#: release's own Secrets and ConfigMaps named in ``wanted``.
ReleaseConfig = Callable[[set[tuple[str, str]]], Mapping[tuple[str, str], Any]]
EXCLUSIVE = "ReadWriteOncePod"


def _problems(
    cluster: PreRolloutCluster, refs: list[Reference], rendered: Rendered
) -> tuple[list[str], list[tuple[str, str]]]:
    """``(missing, unverified)``: what an external Secret/ConfigMap lacks, and
    the release-owned ones that do not exist yet."""
    missing: list[str] = []
    unverified: list[tuple[str, str]] = []
    seen: dict[tuple[str, str], set[str] | None] = {}
    for ref in refs:
        pair = (ref.kind, ref.name)
        if pair not in seen:
            try:
                seen[pair] = cluster.read_object(ref.kind, ref.name)
            except Unreadable:
                seen[pair] = set()  # cannot read: the Job itself decides
                continue
        keys = seen[pair]
        owned = pair in rendered.declared
        if keys is None:
            if owned:
                if pair not in unverified:
                    unverified.append(pair)
            elif not ref.optional:
                text = f"{ref.kind}/{ref.name} does not exist"
                if text not in missing:
                    missing.append(text)
        elif (
            ref.key and keys and ref.key not in keys and not ref.optional and not owned
        ):
            missing.append(f"{ref.kind}/{ref.name} has no key {ref.key}")
    return missing, unverified


def _exclusive(
    cluster: PreRolloutCluster, manifest: Mapping[str, Any], item: PreRollout
) -> list[str]:
    """Claims the upgrade check cannot share with the running pod."""
    problems: list[str] = []
    for claim in _upgrade_claims(manifest, item):
        state = cluster.read_claim(claim)
        if state is not None and EXCLUSIVE in state["modes"]:
            if cluster.pods_using(claim):
                problems.append(claim)
    return problems


def _upgrade_claims(manifest: Mapping[str, Any], item: PreRollout) -> list[str]:
    """Every claim an upgrade check would open."""
    return [
        claim
        for ordinal in _ordinals(manifest)
        for path, claim in claim_names(manifest, ordinal).items()
        if _wanted(item, path)
    ]


def _ordinals(manifest: Mapping[str, Any]) -> list[int | None]:
    if manifest.get("kind") == "StatefulSet":
        return list(range(replicas(manifest)))
    return [None]


def _wanted(item: PreRollout, path: str) -> bool:
    assert item.upgrade is not None
    return not item.upgrade.volumes or path in item.upgrade.volumes


class PreRolloutStage:
    """Plans and runs a pipeline's pre-rollout checks.

    :param pipeline: The pipeline (its app declares the checks).
    :param cluster: Opens a :class:`PreRolloutCluster` for the target.
    :param say: Human progress lines.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        cluster: ClusterFactory,
        say: Callable[[str], None],
    ) -> None:
        self.pipeline = pipeline
        self.open = cluster
        self.say = say
        self.release_config: ReleaseConfig | None = None

    # -------------------------------------------------------------- plan
    def plan(self, rendered: Rendered) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(stage, hashed)``; refuses a check that cannot work (exit 2)."""
        declared = self.pipeline.app.pre_rollouts
        described = describe_checks(declared, rendered)
        hashed = {"checks": described}
        problems: list[str] = []
        exclusive: list[str] = []
        with closing(self.open()) as cluster:
            for item in declared:
                manifest = rendered.workloads.get(item.workload)
                if manifest is None:
                    continue
                missing, _ = _problems(cluster, references(manifest), rendered)
                problems += [f"{item.workload}: {text}" for text in missing]
                if item.upgrade is not None:
                    exclusive += _exclusive(cluster, manifest, item)
        if exclusive:
            raise PipelineError(
                "prerollout-claim-exclusive",
                "the upgrade check cannot open a ReadWriteOncePod claim that a "
                "running pod holds: " + ", ".join(sorted(set(exclusive))),
                details={"stage": "prerollout", "claims": sorted(set(exclusive))},
            )
        if problems:
            raise PipelineError(
                "prerollout-mount-missing",
                "a pre-rollout check would fail to start: " + "; ".join(problems),
                details={"stage": "prerollout", "problems": problems},
            )
        stage = {
            "state": "planned",
            "runs": "before the workload changes; a failing check stops the release",
            "checks": described,
        }
        return stage, hashed

    # --------------------------------------------------------------- run
    def run(
        self,
        rendered: Rendered,
        *,
        changed: set[tuple[str, str]],
        run_id: str,
        release_config: ReleaseConfig | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Run the checks of every changing workload; raise on the first failure.

        :param release_config: Reads the release's own Secrets and ConfigMaps
            with their values. A check that reads one that does not exist yet
            (a first install) gets a labelled copy of it under another name,
            created just before its Job and deleted with it, so the check
            sees the values the pods will. The release's objects themselves
            are created only by ``apply``, which keeps the approved plan
            exact. Without it (or when a value cannot be read) the reference
            is made optional and listed as ``unverified``.
        """
        self.release_config = release_config
        declared = self.pipeline.app.pre_rollouts
        output: dict[str, Any] = {"checks": [], "skipped": []}
        with closing(self.open()) as cluster:
            output["removed_stale"] = cluster.remove_stale(self.pipeline.app.name)
            for item in declared:
                manifest = rendered.workloads.get(item.workload)
                if manifest is None or (manifest["kind"], item.workload) not in changed:
                    output["skipped"].append(
                        {"workload": item.workload, "why": "workload-unchanged"}
                    )
                    continue
                self._check(cluster, rendered, manifest, item, run_id, output)
        ran = len(output["checks"])
        return ("done" if ran else "skipped"), output

    def _check(
        self,
        cluster: PreRolloutCluster,
        rendered: Rendered,
        manifest: dict[str, Any],
        item: PreRollout,
        run_id: str,
        output: dict[str, Any],
    ) -> None:
        refs = references(manifest)
        missing, unverified = _problems(cluster, refs, rendered)
        label = f"{manifest['kind']}/{item.workload}"
        if missing:
            self._fail(
                "prerollout-mount-missing",
                f"{label}: " + "; ".join(missing),
                output,
            )
        redact = cluster.secret_values(
            sorted({ref.name for ref in refs if ref.kind == "Secret"})
        )
        staged = self._stage(cluster, item, unverified, run_id, redact)
        try:
            self._jobs(
                cluster,
                manifest,
                item,
                run_id,
                output,
                label,
                redact,
                staged,
                unverified,
            )
        finally:
            for (kind, _), copy in staged.items():
                cluster.delete_object(kind, copy)

    def _stage(
        self,
        cluster: PreRolloutCluster,
        item: PreRollout,
        unverified: list[tuple[str, str]],
        run_id: str,
        redact: list[str],
    ) -> dict[tuple[str, str], str]:
        """Copies of the absent release-owned objects ``item`` reads: ``{ref: copy}``."""
        if not unverified or self.release_config is None:
            return {}
        try:
            found = self.release_config(set(unverified))
        except Exception:  # unreadable state: fall back to optional references
            return {}
        staged: dict[tuple[str, str], str] = {}
        try:
            for index, pair in enumerate(sorted(found)):
                name = staged_name(item.workload, run_id, pair[0], index)
                body = staged_copy(
                    found[pair],
                    name=name,
                    namespace=self.pipeline.target.namespace,
                    app=self.pipeline.app.name,
                    run=run_id,
                )
                redact.extend(staged_values(body))
                cluster.create_object(body)
                staged[pair] = name
        except BaseException:
            for (kind, _), copy in staged.items():
                cluster.delete_object(kind, copy)
            raise
        if staged:
            self.say(
                f"[prerollout] {item.workload}: staged "
                + ", ".join(f"{k}/{n}" for k, n in sorted(staged))
                + " for the check (this release creates them)"
            )
        return staged

    def _jobs(
        self,
        cluster: PreRolloutCluster,
        manifest: dict[str, Any],
        item: PreRollout,
        run_id: str,
        output: dict[str, Any],
        label: str,
        redact: list[str],
        staged: dict[tuple[str, str], str],
        unverified: list[tuple[str, str]],
    ) -> None:
        optional = [pair for pair in unverified if pair not in staged]
        common: dict[str, Any] = {
            "app": self.pipeline.app.name,
            "namespace": self.pipeline.target.namespace,
            "run": run_id,
            "optional": optional,
            "renamed": staged,
        }
        note: dict[str, Any] = {}
        if optional:
            note["unverified"] = [f"{k}/{n}" for k, n in optional]
        if staged:
            note["staged"] = [f"{k}/{n}" for k, n in sorted(staged)]
        if item.command is not None:
            job = check_job(
                manifest,
                kind="config",
                command=item.command,
                timeout_seconds=item.timeout_seconds,
                **common,
            )
            self._execute(cluster, job, label, "config", redact, output, dict(note))
        if item.upgrade is None:
            return
        upgrade = item.upgrade
        opened = 0
        for ordinal in _ordinals(manifest):
            claims: dict[str, str] = {}
            node: str | None = None
            skipped: list[str] = []
            for path, claim in claim_names(manifest, ordinal).items():
                if not _wanted(item, path):
                    continue
                state = cluster.read_claim(claim)
                if state is None or state["phase"] != "Bound":
                    skipped.append(claim)
                    continue
                if EXCLUSIVE in state["modes"] and cluster.pods_using(claim):
                    self._fail(
                        "prerollout-claim-exclusive",
                        f"{label}: claim {claim} is ReadWriteOncePod and in use",
                        output,
                    )
                claims[path] = claim
                if "ReadWriteOnce" in state["modes"]:
                    holders = [
                        p["node"] for p in cluster.pods_using(claim) if p["node"]
                    ]
                    node = node or (holders[0] if holders else None)
            if skipped:
                output["skipped"].append(
                    {
                        "workload": item.workload,
                        "why": "claim-not-bound-or-absent",
                        "claims": skipped,
                    }
                )
            if not claims:
                continue
            job = check_job(
                manifest,
                kind="upgrade",
                command=upgrade.command,
                timeout_seconds=upgrade.timeout_seconds,
                ordinal=ordinal,
                claims=claims,
                node=node,
                **common,
            )
            extra: dict[str, Any] = {"claims": sorted(claims.values())}
            if node:
                extra["node"] = node
            if staged:
                extra["staged"] = note["staged"]
            self._execute(cluster, job, label, "upgrade", redact, output, extra)
            opened += 1
        if not opened and not any(
            s.get("workload") == item.workload for s in output["skipped"]
        ):
            output["skipped"].append(
                {"workload": item.workload, "why": "no-retained-claim"}
            )

    def _execute(
        self,
        cluster: PreRolloutCluster,
        job: dict[str, Any],
        label: str,
        kind: str,
        redact: list[str],
        output: dict[str, Any],
        extra: dict[str, Any],
    ) -> None:
        name = job["metadata"]["name"]
        self.say(f"[prerollout] {label}: {kind} check running as Job {name}")
        outcome = cluster.run_job(job, redact=redact)
        entry = {
            "workload": label.split("/", 1)[1],
            "kind": kind,
            "job": name,
            **outcome.public(),
            **extra,
        }
        output["checks"].append(entry)
        self.say(
            f"[prerollout] {label}: {kind} check {outcome.state} "
            f"({outcome.seconds:.1f}s)"
        )
        if outcome.state == "passed":
            return
        for line in outcome.log_tail.splitlines():
            self.say(f"[prerollout]   | {line}")
        message = (
            f"{label}: the {kind} check {_phrase(outcome)}; "
            "nothing of the release was applied"
        )
        details = {"output": output}
        if outcome.state == "timeout":
            raise PipelineError(
                "prerollout-timeout", message, failed=True, details=details
            )
        if outcome.state == "not-startable":
            raise PipelineError(
                "prerollout-not-startable", message, failed=True, details=details
            )
        raise PipelineError("prerollout-failed", message, failed=True, details=details)

    def _fail(self, code: str, message: str, output: dict[str, Any]) -> None:
        raise PipelineError(
            code,
            message + "; nothing of the release was applied",
            failed=True,
            details={"output": output},
        )


def _phrase(outcome: Outcome) -> str:
    if outcome.state == "timeout":
        return "did not finish in time"
    if outcome.state == "not-startable":
        return f"could not start ({outcome.category}: {outcome.reason})"
    code = "" if outcome.exit_code is None else f" (exit {outcome.exit_code})"
    return f"failed{code}"


def changing(actions: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """``(kind, name)`` of every object a release plan does not leave as it is."""
    return {
        (item["kind"], item["name"]) for item in actions if item["operation"] != "no-op"
    }
