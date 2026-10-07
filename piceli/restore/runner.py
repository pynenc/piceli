"""Take a restore point, and put one back.

:func:`take` runs a :class:`~piceli.restore.plan.RestorePlan`: quiesce hooks,
stop every writer, wait until their pods are gone, archive each claim through
a read-only helper Job, read every archive back and verify it, then start
the writers again (or leave them for the release to start). :func:`plan_restore`
and :func:`restore` put a verified restore point back: the plan is
read-only and has a hash; the restore needs that hash, stops the writers,
empties each claim, extracts the archive in a read-write helper Job, checks
the content digest in the cluster, and starts the writers with their
previous replica counts.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.restore.cluster import (
    BACKUP,
    DIGEST,
    RestoreCluster,
    helper_job,
    restore_command,
)
from piceli.restore.model import RestorePointError, RestorePoints
from piceli.restore.plan import STOPPABLE, RestorePlan, _claim_names
from piceli.restore.store import (
    RECORD,
    RECORD_SCHEMA,
    ArchiveWriter,
    archive_name,
    close_interrupted,
    file_sha256,
    load_record,
    new_point_dir,
    new_point_id,
    private_dir,
    take_lock,
    verify_claim,
    write_record,
)

RESTORE_PLAN_SCHEMA = "piceli.restore-plan.v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _main_image(workload: Mapping[str, Any] | None) -> str | None:
    pod = (((workload or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    for container in pod.get("containers") or ():
        if isinstance(container, dict) and container.get("image"):
            return str(container["image"])
    return None


def _template(workload: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(((workload or {}).get("spec") or {}).get("template") or {})


def _job_name(point: str, verb: str, index: int) -> str:
    return f"piceli-{verb}-{point[-6:]}-{index:02d}"


class _Writers:
    """Stops writers, remembers their replica counts, starts them again."""

    def __init__(self, cluster: RestoreCluster, say: Callable[[str], None]) -> None:
        self.cluster = cluster
        self.say = say
        self.stopped: list[dict[str, Any]] = []

    def stop(
        self,
        writers: Iterable[Mapping[str, Any]],
        prior: Mapping[str, int] | None = None,
        recorded: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        """Remember each writer's replicas, call ``recorded`` with them, then
        scale every writer to zero.

        :param prior: Replica counts an interrupted restore point recorded
            (:func:`~piceli.restore.store.close_interrupted`): a writer found at
            zero replicas that one of them stopped is remembered at that
            count, so it is started again at it.
        :param recorded: Persists the counts before anything is scaled (a
            process killed after a scale still knows them).
        """
        prior = prior or {}
        for writer in writers:
            kind, name = str(writer["kind"]), str(writer["name"])
            live = self.cluster.workload(kind, name)
            if live is None:
                continue
            replicas = (live.get("spec") or {}).get("replicas")
            replicas = 1 if replicas is None else int(replicas)
            workload = f"{kind}/{name}"
            if not replicas and prior.get(workload):
                replicas = int(prior[workload])
                self.say(
                    f"{workload} was stopped by an interrupted restore point; "
                    f"it had {replicas} replicas"
                )
            self.stopped.append(
                {"workload": workload, "kind": kind, "name": name, "replicas": replicas}
            )
        if recorded is not None:
            recorded(self.stopped)
        for item in self.stopped:
            if item["replicas"]:
                live = self.cluster.workload(item["kind"], item["name"])
                if live is not None and not (live.get("spec") or {}).get("replicas"):
                    continue  # already at zero (an interrupted attempt)
                self.say(
                    f"stopping {item['workload']} ({item['replicas']} -> 0 replicas)"
                )
                self.cluster.scale(item["kind"], item["name"], 0)

    def start(self, keep_stopped: Iterable[str] = ()) -> list[str]:
        """Scale every stopped writer back unless listed; errors are reported,
        the first one raised after all were tried."""
        kept = set(keep_stopped)
        started: list[str] = []
        first: BaseException | None = None
        for item in self.stopped:
            if item["workload"] in kept or not item["replicas"]:
                continue
            try:
                live = self.cluster.workload(item["kind"], item["name"])
                if live is not None and not (live.get("spec") or {}).get("replicas"):
                    self.cluster.scale(item["kind"], item["name"], item["replicas"])
                    self.say(
                        f"started {item['workload']} again "
                        f"({item['replicas']} replicas)"
                    )
                started.append(item["workload"])
            except Exception as error:  # keep starting the others
                self.say(f"could not start {item['workload']} ({type(error).__name__})")
                first = first or error
        if first is not None:
            raise RestorePointError(
                "restore-point-restart-failed",
                "a writer stopped for the restore point could not be started "
                "again; scale it back by hand",
                failed=True,
            ) from None
        return started


def _digest_in_pod(cluster: RestoreCluster, pod: str, timeout: float) -> str:
    output = bytearray()
    code = cluster.exec(pod, DIGEST, timeout=timeout, stdout=output.extend)
    value = bytes(output[:128]).decode(errors="replace").strip()
    if code != 0 or not _HEX64.fullmatch(value):
        raise RestorePointError(
            "restore-point-copy-failed",
            f"the content digest could not be computed in pod {pod} (exit {code})",
            failed=True,
        )
    return value


def take(
    cluster: RestoreCluster,
    plan: RestorePlan,
    settings: RestorePoints,
    root: Path,
    *,
    context: Mapping[str, Any],
    keep_stopped: Iterable[str] = (),
    say: Callable[[str], None] | None = None,
    point: str | None = None,
) -> dict[str, Any]:
    """Take and verify one restore point of ``plan``; return its record.

    :param root: The restore point directory (one subdirectory per point).
    :param context: Recorded as is (app, namespace, target, run, release).
    :param keep_stopped: Writers (``Kind/name``) left at zero replicas on
        success, for the release to start; every writer is started again
        when anything fails.
    :raises RestorePointError: with a registered code; the writers are
        started again and the partial point is recorded as ``failed``.
        ``restore-point-busy`` (another process takes into ``root``) and
        ``restore-point-exists`` (``point`` was taken before) stop nothing.

    Every attempt writes a new point directory. Points of earlier attempts
    that died mid-copy are closed first (:func:`close_interrupted`), and
    writers they left at zero replicas are remembered at their counts.
    """
    with take_lock(root):
        prior = close_interrupted(root)
        return _take(
            cluster,
            plan,
            settings,
            new_point_dir(root, point or new_point_id()),
            prior,
            context=context,
            keep_stopped=keep_stopped,
            say=say or (lambda _line: None),
        )


def _take(
    cluster: RestoreCluster,
    plan: RestorePlan,
    settings: RestorePoints,
    directory: Path,
    prior: Mapping[str, int],
    *,
    context: Mapping[str, Any],
    keep_stopped: Iterable[str],
    say: Callable[[str], None],
) -> dict[str, Any]:
    point = directory.name
    record: dict[str, Any] = {
        "schema": RECORD_SCHEMA,
        "id": point,
        "created_at": _now(),
        "state": "running",
        **dict(context),
        "settings": settings.describe(),
        "writers": [],
        "claims": [],
    }
    writers = _Writers(cluster, say)
    timeout = float(settings.timeout_seconds)
    claims = [str(item["claim"]) for item in plan.claims]
    live = {
        str(item["workload"]): cluster.workload(str(item["kind"]), str(item["name"]))
        for item in plan.writers
    }
    try:
        for writer in plan.writers:
            for hook in writer["quiesce"]:
                say(f"quiesce {writer['workload']}: {hook['type']} hook")
                cluster.run_hook(writer, hook)

        def recorded(stopped: list[dict[str, Any]]) -> None:
            record["writers"] = stopped
            write_record(directory, record)

        writers.stop(plan.writers, prior, recorded)
        cluster.wait_stopped(plan.writers, claims, timeout)
        say(f"writers stopped; no pod mounts {len(claims)} claim(s) writably")
        for index, item in enumerate(plan.claims):
            claim = str(item["claim"])
            owner = live.get(str(item["workload"])) or next(
                (value for value in live.values() if value is not None), None
            )
            image = settings.image or _main_image(owner)
            if image is None:
                raise RestorePointError(
                    "restore-point-helper-failed",
                    f"no image for the helper of claim {claim}: declare "
                    "RestorePoints(image=...)",
                )
            name = _job_name(point, "backup", index)
            manifest = helper_job(
                name,
                point=point,
                claim=claim,
                image=image,
                read_only=True,
                seconds=int(timeout * 4),
                run_as_user=settings.run_as_user,
                template=_template(owner),
            )
            archive = archive_name(index, claim)
            say(f"copying claim {claim}")
            with cluster.helper(name, manifest, timeout) as pod:
                with ArchiveWriter(directory / archive) as stream:
                    code = cluster.exec(
                        pod, BACKUP, timeout=timeout, stdout=stream.write
                    )
                    if code != 0:
                        raise RestorePointError(
                            "restore-point-copy-failed",
                            f"archiving claim {claim} exited {code}",
                            failed=True,
                        )
                    sha256 = stream.commit()
                    size = stream.bytes
                content = _digest_in_pod(cluster, pod, timeout)
            entry = {
                "claim": claim,
                "workload": item["workload"],
                "ordinal": item["ordinal"],
                "why": list(item["why"]),
                "archive": archive,
                "sha256": sha256,
                "bytes": size,
                "content_sha256": content,
            }
            listing = verify_claim(directory, entry)
            entry.update(
                entries=listing["entries"],
                files=listing["files"],
                file_bytes=listing["file_bytes"],
                verified=True,
            )
            record["claims"].append(entry)
            write_record(directory, record)
            say(f"claim {claim}: {size} bytes, {listing['files']} files, verified")
        record["state"] = "verified"
        record["verified_at"] = _now()
        write_record(directory, record)
    except BaseException as error:
        record["state"] = "failed"
        record["reason"] = getattr(error, "code", type(error).__name__)
        if directory.exists():
            try:
                write_record(directory, record)
            except OSError:
                pass
        started = writers.start()
        if isinstance(error, RestorePointError):
            # Shown in the deploy's status: the release before keeps running.
            error.details["writers_started"] = started
        raise
    record["started"] = writers.start(keep_stopped)
    record["left_stopped"] = [
        item["workload"]
        for item in writers.stopped
        if item["workload"] not in record["started"] and item["replicas"]
    ]
    write_record(directory, record)
    return record


# ------------------------------------------------------------------ restore


def _selected(record: Mapping[str, Any], claims: Sequence[str]) -> list[dict[str, Any]]:
    entries = [dict(item) for item in record.get("claims") or ()]
    if not claims:
        return entries
    known = {item["claim"] for item in entries}
    unknown = sorted(set(claims) - known)
    if unknown:
        raise RestorePointError(
            "restore-point-claim-unknown",
            f"restore point {record['id']} has no claim {unknown[0]}",
        )
    return [item for item in entries if item["claim"] in set(claims)]


def writers_of(
    live: Iterable[Mapping[str, Any]],
    claims: Sequence[str],
    existing: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Live workloads that mount any of ``claims`` writably.

    :raises RestorePointError: ``restore-point-writer-unsupported`` for a
        writer Piceli cannot stop (a DaemonSet).
    """
    wanted = set(claims)
    found = []
    for workload in live:
        written = sorted(
            claim
            for claim, _ordinal in _claim_names(workload, existing)
            if claim in wanted
        )
        if not written:
            continue
        kind = str(workload.get("kind"))
        name = str((workload.get("metadata") or {}).get("name"))
        if kind not in STOPPABLE:
            raise RestorePointError(
                "restore-point-writer-unsupported",
                f"{kind}/{name} writes claim {written[0]} and cannot be stopped "
                "for a restore; stop it first",
            )
        found.append(
            {
                "workload": f"{kind}/{name}",
                "kind": kind,
                "name": name,
                "claims": written,
            }
        )
    return sorted(found, key=lambda item: item["workload"])


def plan_restore(
    cluster: RestoreCluster,
    root: Path,
    point: str,
    *,
    claims: Sequence[str] = (),
    target: Mapping[str, Any],
    source_namespace: str | None = None,
) -> dict[str, Any]:
    """The read-only plan to put ``point`` back, with its hash.

    Verifies every selected archive (SHA-256, listing, content digest),
    checks that the claims exist in the namespace, and lists the writers
    that will be stopped. ``source_namespace`` restores a point taken in
    that namespace into the cluster's (a branch environment seeded from
    main); the plan then records it.

    :raises RestorePointError: ``restore-point-unknown``,
        ``restore-point-claim-unknown``, ``restore-point-target-mismatch``,
        ``restore-point-claim-missing``, ``restore-point-not-verified``,
        archive errors, ``restore-point-writer-unsupported``.
    """
    directory, record = load_record(root, point)
    if record.get("state") != "verified":
        raise RestorePointError(
            "restore-point-not-verified",
            f"restore point {point} is {record.get('state')}, not verified",
        )
    expected = source_namespace or cluster.namespace
    if record.get("namespace") != expected:
        raise RestorePointError(
            "restore-point-target-mismatch",
            f"restore point {point} was taken in namespace "
            f"{record.get('namespace')}, not {expected}",
        )
    entries = _selected(record, claims)
    for entry in entries:
        verify_claim(directory, entry)
    existing = {
        str((item.get("metadata") or {}).get("name")): item for item in cluster.claims()
    }
    missing = sorted(
        entry["claim"] for entry in entries if entry["claim"] not in existing
    )
    if missing:
        raise RestorePointError(
            "restore-point-claim-missing",
            f"claim {missing[0]} does not exist in namespace {cluster.namespace}",
        )
    names = [entry["claim"] for entry in entries]
    writers = writers_of(cluster.workloads(), names, existing)
    body = {
        "schema": RESTORE_PLAN_SCHEMA,
        "point": point,
        "record_sha256": file_sha256(directory / RECORD),
        "namespace": cluster.namespace,
        "target": dict(target),
        "claims": [
            {
                key: entry[key]
                for key in ("claim", "archive", "sha256", "content_sha256")
            }
            for entry in entries
        ],
        "writers": [item["workload"] for item in writers],
        **({"source_namespace": source_namespace} if source_namespace else {}),
    }
    return {
        **body,
        "restore_hash": "sha256:"
        + hashlib.sha256(_canonical(body).encode()).hexdigest(),
        "created_at": record.get("created_at"),
        "bytes": sum(int(entry.get("bytes") or 0) for entry in entries),
        "writer_details": writers,
    }


def _chunks(path: Path, size: int = 256 * 1024) -> Iterator[bytes]:
    with path.open("rb") as stream:
        yield from iter(lambda: stream.read(size), b"")


def restore(
    cluster: RestoreCluster,
    root: Path,
    planned: Mapping[str, Any],
    *,
    image: str | None,
    timeout_seconds: int,
    run_as_user: int = 0,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Put a planned restore point back (the caller checked the approval).

    :raises RestorePointError: ``restore-point-restore-failed`` or
        ``restore-point-restore-mismatch``, after starting the writers again.
    """
    say = say or (lambda _line: None)
    point = str(planned["point"])
    directory, record = load_record(root, point)
    entries = {entry["claim"]: entry for entry in record["claims"]}
    timeout = float(timeout_seconds)
    writers = _Writers(cluster, say)
    live = {
        item["workload"]: cluster.workload(item["kind"], item["name"])
        for item in planned["writer_details"]
    }
    results = []
    started_at = _now()
    try:
        writers.stop(planned["writer_details"])
        names = [item["claim"] for item in planned["claims"]]
        cluster.wait_stopped(planned["writer_details"], names, timeout)
        for index, item in enumerate(planned["claims"]):
            entry = entries[item["claim"]]
            owner = live.get(str(entry.get("workload"))) or next(
                (value for value in live.values() if value is not None), None
            )
            helper_image = image or _main_image(owner)
            if helper_image is None:
                raise RestorePointError(
                    "restore-point-helper-failed",
                    f"no image for the helper of claim {entry['claim']}: pass "
                    "--image or declare RestorePoints(image=...)",
                )
            path = directory / str(entry["archive"])
            size = path.stat().st_size
            name = _job_name(point, "restore", index)
            manifest = helper_job(
                name,
                point=point,
                claim=str(entry["claim"]),
                image=helper_image,
                read_only=False,
                seconds=int(timeout * 4),
                run_as_user=run_as_user,
                template=_template(owner),
            )
            say(f"restoring claim {entry['claim']} ({size} bytes)")
            with cluster.helper(name, manifest, timeout) as pod:
                code = cluster.exec(
                    pod,
                    restore_command(size),
                    timeout=timeout,
                    stdin=_chunks(path),
                    code="restore-point-restore-failed",
                )
                if code != 0:
                    raise RestorePointError(
                        "restore-point-restore-failed",
                        f"extracting the archive into claim {entry['claim']} "
                        f"exited {code}",
                        failed=True,
                    )
                content = _digest_in_pod(cluster, pod, timeout)
            if content != entry.get("content_sha256"):
                raise RestorePointError(
                    "restore-point-restore-mismatch",
                    f"claim {entry['claim']} does not hold the restore point's "
                    "content after the restore",
                    failed=True,
                )
            results.append(
                {"claim": entry["claim"], "content_sha256": content, "verified": True}
            )
            say(f"claim {entry['claim']}: restored and verified")
    except BaseException:
        writers.start()
        raise
    started = writers.start()
    receipt = {
        "schema": "piceli.restore-receipt.v1",
        "point": point,
        "restore_hash": planned["restore_hash"],
        "started_at": started_at,
        "finished_at": _now(),
        "claims": results,
        "writers": writers.stopped,
        "started": started,
    }
    receipts = private_dir(directory / "restores")
    path = receipts / f"{receipt['finished_at'].replace(':', '')}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    receipt["receipt"] = str(path)
    return receipt
