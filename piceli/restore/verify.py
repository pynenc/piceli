"""Prove a restore point without touching live data.

:func:`plan_verify` is read-only: it verifies the archives, reads each
source claim (storage class, size, access modes, the nodes its volume is
bound to), names one scratch claim per restored claim, and the read-only
verify Job of every workload that declared ``app.restore_verify``; the plan
has a ``verify_hash``. :func:`verify` (the caller checked the approval)
creates the scratch claims, extracts each archive into its scratch claim in a
helper Job and checks the content digest in the cluster, runs each verify Job
with the workload's current image and the scratch copies mounted read-only,
writes a receipt with ``PASS`` or ``FAIL`` per claim, and deletes the scratch
claims unless ``keep``. No writer is stopped and no live claim is mounted.

Nothing a claim holds, and nothing a verify command prints, is ever printed
or stored.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from piceli.restore.cluster import (
    SCRATCH_LABEL,
    VERIFY_LABEL,
    RestoreCluster,
    helper_job,
    pin,
    restore_command,
    scratch_claim,
)
from piceli.restore.model import RestorePointError
from piceli.restore.runner import (
    _canonical,
    _chunks,
    _digest_in_pod,
    _main_image,
    _now,
    _selected,
    _template,
)
from piceli.restore.store import file_sha256, load_record, private_dir, verify_claim

VERIFY_PLAN_SCHEMA = "piceli.restore-verify-plan.v1"
VERIFY_RECEIPT_SCHEMA = "piceli.restore-verify-receipt.v1"


class JobRunner(Protocol):
    """Runs one Job to its end and always removes it
    (:class:`piceli.pipeline.prerollout_cluster.PreRolloutCluster`)."""

    def run_job(self, job: Mapping[str, Any], *, redact: Any = ()) -> Any: ...


def scratch_name(point: str, index: int) -> str:
    return f"piceli-verify-{point[-6:]}-{index:02d}"


def _job_name(point: str, verb: str, index: int) -> str:
    return f"piceli-{verb}-{point[-6:]}-{index:02d}"


def _source_spec(
    cluster: RestoreCluster, claim: str, live: Mapping[str, Any]
) -> dict[str, Any]:
    spec = live.get("spec") or {}
    status = live.get("status") or {}
    size = ((spec.get("resources") or {}).get("requests") or {}).get("storage") or (
        (status.get("capacity") or {}).get("storage")
    )
    storage_class = spec.get("storageClassName")
    if storage_class == "" or not size:
        raise RestorePointError(
            "restore-verify-scratch-unsupported",
            f"claim {claim} is bound to a static volume (no storage class or "
            "size), so Piceli cannot create a scratch claim like it",
        )
    volume = spec.get("volumeName")
    affinity = cluster.volume_affinity(str(volume)) if volume else None
    return {
        "storage_class": storage_class,
        "size": str(size),
        "access_modes": list(spec.get("accessModes") or ["ReadWriteOnce"]),
        **({"volume_mode": spec["volumeMode"]} if spec.get("volumeMode") else {}),
        **({"node_affinity": affinity} if affinity else {}),
    }


def _mount_path(manifest: Mapping[str, Any], claim: str, ordinal: Any) -> str | None:
    from piceli.pipeline.prerollout import claim_names

    names = claim_names(manifest, ordinal if isinstance(ordinal, int) else None)
    return next((path for path, name in names.items() if name == claim), None)


def plan_verify(
    cluster: RestoreCluster,
    root: Path,
    point: str,
    *,
    declared: Mapping[str, Any],
    claims: Sequence[str] = (),
    target: Mapping[str, Any],
    image: str | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    """The read-only plan to restore ``point`` into scratch claims and verify it.

    :param declared: ``Kind/name`` -> a described :class:`~piceli.restore.RestoreVerify`
        (:meth:`piceli.app.App.restore_verifies`).
    :param image: The helper image (default: each workload's current image).
    :raises RestorePointError: the codes of :func:`~piceli.restore.runner.plan_restore`,
        ``restore-verify-scratch-exists``, ``restore-verify-scratch-unsupported``,
        ``restore-verify-workload-missing``.
    """
    directory, record = load_record(root, point)
    if record.get("state") != "verified":
        raise RestorePointError(
            "restore-point-not-verified",
            f"restore point {point} is {record.get('state')}, not verified",
        )
    if record.get("namespace") != cluster.namespace:
        raise RestorePointError(
            "restore-point-target-mismatch",
            f"restore point {point} was taken in namespace "
            f"{record.get('namespace')}, not {cluster.namespace}",
        )
    entries = _selected(record, claims)
    for entry in entries:
        verify_claim(directory, entry)
    workloads: dict[str, dict[str, Any] | None] = {}
    planned_claims: list[dict[str, Any]] = []
    groups: dict[tuple[str, Any], dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        claim = str(entry["claim"])
        live_claim = cluster.claim(claim)
        if live_claim is None:
            raise RestorePointError(
                "restore-point-claim-missing",
                f"claim {claim} does not exist in namespace {cluster.namespace}: "
                "its storage class and size shape the scratch claim",
            )
        scratch = scratch_name(point, index)
        if cluster.claim(scratch) is not None:
            raise RestorePointError(
                "restore-verify-scratch-exists",
                f"scratch claim {scratch} already exists (kept by an earlier "
                "--keep run?); delete it first",
            )
        workload = str(entry.get("workload") or "")
        if workload not in workloads:
            kind, _, name = workload.partition("/")
            workloads[workload] = (
                cluster.workload(kind, name)
                if kind in {"Deployment", "StatefulSet"}
                else None
            )
        live = workloads[workload]
        item = {
            "claim": claim,
            "archive": entry["archive"],
            "sha256": entry["sha256"],
            "content_sha256": entry["content_sha256"],
            "workload": workload,
            "ordinal": entry.get("ordinal"),
            "scratch_claim": scratch,
            **_source_spec(cluster, claim, live_claim),
            "helper_image": image or _main_image(live),
        }
        verify = declared.get(workload)
        if verify is not None:
            if live is None:
                raise RestorePointError(
                    "restore-verify-workload-missing",
                    f"{workload} declares a restore verify command but does not "
                    f"exist in namespace {cluster.namespace}",
                )
            path = _mount_path(live, claim, entry.get("ordinal"))
            if path is None:
                raise RestorePointError(
                    "restore-verify-workload-missing",
                    f"{workload} no longer mounts claim {claim}",
                )
            item["path"] = path
            key = (workload, entry.get("ordinal"))
            group = groups.setdefault(
                key,
                {
                    "workload": workload,
                    "ordinal": entry.get("ordinal"),
                    "image": _main_image(live),
                    **dict(verify),
                    "mounts": {},
                },
            )
            group["mounts"][path] = scratch
        if item["helper_image"] is None:
            raise RestorePointError(
                "restore-point-helper-failed",
                f"no image for the helper of claim {claim}: pass --image or "
                "declare RestorePoints(image=...)",
            )
        planned_claims.append(item)
    checks = [
        {**group, "job": _job_name(point, "verify", index)}
        for index, group in enumerate(groups.values())
    ]
    body = {
        "schema": VERIFY_PLAN_SCHEMA,
        "point": point,
        "record_sha256": file_sha256(directory / "record.json"),
        "namespace": cluster.namespace,
        "target": dict(target),
        "keep": keep,
        "claims": planned_claims,
        "checks": checks,
    }
    return {
        **body,
        "verify_hash": "sha256:"
        + hashlib.sha256(_canonical(body).encode()).hexdigest(),
        "created_at": record.get("created_at"),
        "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
    }


def verify_job(
    manifest: Mapping[str, Any],
    check: Mapping[str, Any],
    *,
    point: str,
    namespace: str,
    app: str,
    node_affinity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The verify Job: the workload's pod settings and current image, the
    scratch claims mounted read-only at the workload's paths, the declared
    command as entrypoint (see :func:`piceli.pipeline.prerollout.check_job`)."""
    from piceli.pipeline.prerollout import check_job

    job = check_job(
        manifest,
        app=app,
        namespace=namespace,
        kind="upgrade",
        command=list(check["command"]),
        timeout_seconds=int(check["timeout_seconds"]),
        run=point,
        ordinal=check.get("ordinal"),
        claims=dict(check["mounts"]),
    )
    labels = {"app.kubernetes.io/managed-by": "piceli", VERIFY_LABEL: point}
    job["metadata"]["name"] = check["job"]
    job["metadata"]["labels"] = dict(labels)
    job["spec"]["template"]["metadata"]["labels"] = dict(labels)
    if node_affinity:
        job["spec"]["template"]["spec"]["affinity"] = pin(node_affinity)
    return job


def _public_outcome(outcome: Any) -> dict[str, Any]:
    """The Job outcome without its log (a verify command may print data)."""
    value = dict(outcome.public())
    value.pop("log_tail", None)
    return value


def verify(
    cluster: RestoreCluster,
    jobs: JobRunner,
    root: Path,
    planned: Mapping[str, Any],
    *,
    app: str,
    timeout_seconds: int,
    run_as_user: int = 0,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore a planned point into scratch claims, verify it, clean up.

    Returns the receipt (also written to ``<point>/verifies/``); its
    ``state`` is ``passed`` when every claim is ``PASS``. Scratch claims are
    deleted in every case (failure and interrupt included) unless the plan
    says ``keep``.

    :raises RestorePointError: ``restore-verify-cleanup-failed`` when a
        scratch claim could not be deleted (the receipt names it); API errors
        propagate after the cleanup.
    """
    say = say or (lambda _line: None)
    point = str(planned["point"])
    directory, _record = load_record(root, point)
    timeout = float(timeout_seconds)
    created: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    checks: list[dict[str, Any]] = []
    started_at = _now()
    live: dict[str, dict[str, Any] | None] = {}
    left: list[str] = []
    try:
        for index, item in enumerate(planned["claims"]):
            claim, scratch = str(item["claim"]), str(item["scratch_claim"])
            workload = str(item["workload"])
            if workload not in live:
                kind, _, name = workload.partition("/")
                live[workload] = (
                    cluster.workload(kind, name)
                    if kind in {"Deployment", "StatefulSet"}
                    else None
                )
            result: dict[str, Any] = {
                "claim": claim,
                "scratch_claim": scratch,
                "workload": workload,
                "restored": False,
            }
            results[claim] = result
            say(f"creating scratch claim {scratch} for {claim}")
            cluster.create_claim(
                scratch_claim(scratch, point=point, source=claim, spec=item)
            )
            created.append(scratch)
            path = directory / str(item["archive"])
            size = path.stat().st_size
            name = _job_name(point, "rv", index)
            manifest = helper_job(
                name,
                point=point,
                claim=scratch,
                image=str(item["helper_image"]),
                read_only=False,
                seconds=int(timeout * 4),
                run_as_user=run_as_user,
                template=_template(live[workload]),
                node_affinity=item.get("node_affinity"),
                label=VERIFY_LABEL,
            )
            say(f"restoring {claim} into {scratch} ({size} bytes)")
            try:
                with cluster.helper(name, manifest, timeout) as pod:
                    code = cluster.exec(
                        pod,
                        restore_command(size),
                        timeout=timeout,
                        stdin=_chunks(path),
                        code="restore-verify-scratch-failed",
                    )
                    if code != 0:
                        raise RestorePointError(
                            "restore-verify-scratch-failed",
                            f"extracting the archive into {scratch} exited {code}",
                            failed=True,
                        )
                    content = _digest_in_pod(cluster, pod, timeout)
                result["content_sha256"] = content
                if content != item["content_sha256"]:
                    raise RestorePointError(
                        "restore-verify-mismatch",
                        f"{scratch} does not hold the restore point's content",
                        failed=True,
                    )
            except RestorePointError as error:
                result.update(result="FAIL", reason=error.code)
                say(f"claim {claim}: FAIL ({error.code})")
                continue
            result["restored"] = True
            say(f"claim {claim}: restored into {scratch}, content digest matches")
        for check in planned["checks"]:
            claims = [
                item["claim"]
                for item in planned["claims"]
                if item["scratch_claim"] in set(check["mounts"].values())
            ]
            entry: dict[str, Any] = {
                "workload": check["workload"],
                "ordinal": check.get("ordinal"),
                "job": check["job"],
                "claims": claims,
            }
            checks.append(entry)
            if not all(results[claim]["restored"] for claim in claims):
                entry["state"] = "skipped"
                for claim in claims:
                    results[claim]["verify"] = "skipped"
                    results[claim].setdefault("result", "FAIL")
                    results[claim].setdefault("reason", "restore-verify-failed")
                continue
            manifest_live = live.get(str(check["workload"]))
            affinity = next(
                (
                    item.get("node_affinity")
                    for item in planned["claims"]
                    if item["claim"] in claims and item.get("node_affinity")
                ),
                None,
            )
            job = verify_job(
                manifest_live or {},
                check,
                point=point,
                namespace=cluster.namespace,
                app=app,
                node_affinity=affinity,
            )
            say(f"verify {check['workload']}: running Job {check['job']}")
            try:
                outcome = jobs.run_job(job)
            except Exception as error:  # the API refused the Job
                entry.update(state="not-startable", reason=type(error).__name__)
            else:
                entry.update(_public_outcome(outcome))
            say(f"verify {check['workload']}: {entry['state']}")
            for claim in claims:
                results[claim]["verify"] = entry["state"]
                if entry["state"] != "passed":
                    results[claim].update(result="FAIL", reason="restore-verify-failed")
        for result in results.values():
            result.setdefault("verify", "not-declared")
            result.setdefault("result", "PASS")
    finally:
        if not planned.get("keep"):
            for scratch in created:
                if cluster.delete_claim(scratch, timeout):
                    say(f"deleted scratch claim {scratch}")
                else:
                    left.append(scratch)
    ordered = [results[str(item["claim"])] for item in planned["claims"]]
    receipt: dict[str, Any] = {
        "schema": VERIFY_RECEIPT_SCHEMA,
        "point": point,
        "verify_hash": planned["verify_hash"],
        "started_at": started_at,
        "finished_at": _now(),
        "state": "passed"
        if ordered and all(item["result"] == "PASS" for item in ordered)
        else "failed",
        "claims": ordered,
        "checks": checks,
        "kept": list(created) if planned.get("keep") else [],
        "left": left,
    }
    receipts = private_dir(directory / "verifies")
    path = receipts / f"{receipt['finished_at'].replace(':', '')}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    receipt["receipt"] = str(path)
    if left:
        raise RestorePointError(
            "restore-verify-cleanup-failed",
            "scratch claim(s) could not be deleted: " + ", ".join(left),
            failed=True,
            details={"receipt": receipt},
        )
    return receipt


def selected_points(root: Path) -> Iterator[str]:
    """Ids of every verified restore point under ``root``, newest first."""
    from piceli.restore.store import points

    for _folder, record in points(root):
        if record.get("state") == "verified":
            yield str(record["id"])


__all__ = [
    "SCRATCH_LABEL",
    "VERIFY_LABEL",
    "plan_verify",
    "scratch_name",
    "selected_points",
    "verify",
    "verify_job",
]
