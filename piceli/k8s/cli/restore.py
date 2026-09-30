"""``piceli restore-points`` and ``piceli restore``: list and put back restore points.

``restore-points MODULE:ATTR`` is read-only and offline: it lists the
pipeline's restore points (see :mod:`piceli.restore`) newest first and, with
``--verify``, reads every archive back (SHA-256, listing, content digest).

``restore MODULE:ATTR --point ID`` changes the cluster, so it plans first:
without ``--approve`` it verifies the archives, checks that the claims exist
and lists the writers it will stop, prints the plan with its
``restore_hash`` and exits ``3``. ``--approve HASH`` plans again, refuses a
different hash (``restore-plan-changed``) and restores: writers stopped and
gone, each claim emptied and extracted by a helper Job, its content digest
checked in the cluster, writers started again. It holds the pipeline's
state lock, so it never runs beside a deploy.

Output follows the CLI contract: one JSON object on stdout, human text on
stderr; nothing a claim holds is ever printed.

Importing this module is side-effect free.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, EXIT_FAILED, emit_json, fail, reject, say
from piceli.k8s.cli.maintenance import JsonOption, _pipeline

POINTS_SCHEMA = "piceli.restore-points.v1"
RESULT_SCHEMA = "piceli.restore-result.v1"

TargetArgument = Annotated[
    str,
    typer.Argument(
        help="The pipeline, MODULE:ATTR or path/to/file.py:ATTR", show_default=False
    ),
]
EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="The pipeline's environment", show_default=False),
]


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "state": record.get("state"),
        "created_at": record.get("created_at"),
        "namespace": record.get("namespace"),
        "run_id": record.get("run_id"),
        "release": record.get("release"),
        "claims": [
            {
                key: item.get(key)
                for key in (
                    "claim",
                    "workload",
                    "ordinal",
                    "why",
                    "bytes",
                    "files",
                    "sha256",
                    "content_sha256",
                )
            }
            for item in record.get("claims") or ()
        ],
    }


def restore_points(
    target: TargetArgument,
    env: EnvOption = None,
    verify: Annotated[
        bool,
        typer.Option("--verify", help="Read every archive back and check its digests"),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """List a pipeline's restore points, newest first (read-only, offline)."""
    from piceli.restore.model import RestorePointError
    from piceli.restore.store import points, verify_claim

    pipeline = _pipeline(target, env)
    directory = pipeline.restore_point_directory
    found = []
    failed = False
    for folder, record in points(directory):
        summary = _summary(record)
        if verify:
            for entry, item in zip(
                record.get("claims") or (), summary["claims"], strict=True
            ):
                try:
                    verify_claim(folder, entry)
                    item["check"] = "verified"
                except RestorePointError as error:
                    item["check"] = error.code
                    failed = True
        found.append(summary)
        say(
            f"{summary['id']}  {summary['state']}  "
            f"{len(summary['claims'])} claim(s)  {summary['created_at']}"
        )
        for item in summary["claims"]:
            say(
                f"  {item['claim']} ({item['workload']}) {item['bytes']} bytes"
                + (f" [{item['check']}]" if "check" in item else "")
            )
    if not found:
        say(f"no restore points in {directory}")
    emit_json(
        {
            "schema": POINTS_SCHEMA,
            "directory": str(directory),
            "points": found,
            **({"verified": not failed} if verify else {}),
        }
    )
    if failed:
        raise typer.Exit(EXIT_FAILED)
    _ = as_json  # the one JSON object is always printed


def restore(
    target: TargetArgument,
    point: Annotated[
        str | None,
        typer.Option("--point", help="The restore point id (rp-…)", show_default=False),
    ] = None,
    claim: Annotated[
        list[str] | None,
        typer.Option("--claim", help="Only this claim (repeatable; default: all)"),
    ] = None,
    env: EnvOption = None,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="Restore; HASH is the plan's restore_hash"),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option(
            "--image",
            help="Helper image pinned by digest (default: RestorePoints(image=) "
            "or each writer's image)",
        ),
    ] = None,
    to_new_claim: Annotated[
        bool,
        typer.Option(
            "--to-new-claim",
            help="Restore into scratch claims and run the app's restore_verify "
            "checks; live claims and writers are not touched",
        ),
    ] = False,
    all_points: Annotated[
        bool,
        typer.Option("--all", help="With --to-new-claim: every verified restore point"),
    ] = False,
    keep: Annotated[
        bool,
        typer.Option(
            "--keep", help="With --to-new-claim: keep the scratch claims afterwards"
        ),
    ] = False,
    as_json: JsonOption = False,
) -> None:
    """Put a restore point back into its claims (plan, then --approve HASH).

    With --to-new-claim, restore it into scratch claims instead and verify
    the copy (plan, then --approve HASH).
    """
    from piceli.pipeline.backend import Backend
    from piceli.restore.model import RestorePointError, RestorePoints
    from piceli.restore.runner import plan_restore
    from piceli.restore.runner import restore as run_restore
    from piceli.state import session
    from piceli.state.scopes import pipeline_scope

    if (point is None) == (not all_points) or (
        (all_points or keep) and not to_new_claim
    ):
        reject(
            "restore-options-invalid",
            "give --point ID or --all; --all and --keep need --to-new-claim",
        )
    pipeline = _pipeline(target, env)
    settings = pipeline.restore_points or RestorePoints()
    if image is not None:
        try:
            settings = RestorePoints(**{**settings.describe(), "image": image})
        except ValueError:
            reject(
                "restore-point-helper-failed",
                "--image must be pinned by digest (repository@sha256:<64 hex>)",
            )
    directory = pipeline.restore_point_directory
    if to_new_claim:
        _verify(
            target,
            pipeline,
            settings,
            [point] if point is not None else None,
            tuple(claim or ()),
            image,
            keep,
            approve,
        )
        return
    assert point is not None
    cluster = Backend().restore_cluster(pipeline.target, say=say)
    try:
        try:
            planned = plan_restore(
                cluster,
                directory,
                point,
                claims=tuple(claim or ()),
                target=pipeline.target.identity(),
            )
        except RestorePointError as error:
            reject(error.code, str(error))
        public = {
            key: value for key, value in planned.items() if key != "writer_details"
        }
        say(
            f"restore point {point}: {len(planned['claims'])} claim(s), "
            f"{planned['bytes']} bytes; stops "
            + (", ".join(planned["writers"]) or "no writer")
        )
        for item in planned["claims"]:
            say(f"  {item['claim']} <- {item['archive']}")
        if approve is None:
            say("every file in these claims is replaced; approve with:")
            say(
                f"  piceli restore {target} --point {point} --approve {planned['restore_hash']}"
            )
            emit_json(
                {"schema": RESULT_SCHEMA, "state": "approval-required", "plan": public}
            )
            raise typer.Exit(EXIT_APPROVAL)
        if approve != planned["restore_hash"]:
            reject(
                "restore-plan-changed",
                "the approved hash is not the restore plan's current hash; plan again",
                plan=public,
            )
        with session(pipeline_scope(pipeline), write=True, say=say):
            try:
                receipt = run_restore(
                    cluster,
                    directory,
                    planned,
                    image=settings.image,
                    timeout_seconds=settings.timeout_seconds,
                    run_as_user=settings.run_as_user,
                    say=say,
                )
            except RestorePointError as error:
                fail(error.code, str(error), plan=public)
        say(f"restored {len(receipt['claims'])} claim(s) from {point}")
        emit_json({"schema": RESULT_SCHEMA, "state": "restored", "receipt": receipt})
        _ = as_json
    finally:
        cluster.close()


VERIFY_SCHEMA = "piceli.restore-verify-result.v1"


def _verify(
    target: str,
    pipeline: Any,
    settings: Any,
    points: list[str] | None,
    claims: tuple[str, ...],
    image: str | None,
    keep: bool,
    approve: str | None,
) -> None:
    """``restore --to-new-claim``: plan (exit 3), then restore into scratch
    claims and verify; exit 1 when any claim is FAIL."""
    import hashlib

    from piceli.pipeline.backend import Backend
    from piceli.restore.model import RestorePointError
    from piceli.restore.runner import _canonical
    from piceli.restore.verify import plan_verify, selected_points, verify

    directory = pipeline.restore_point_directory
    backend = Backend()
    cluster = backend.restore_cluster(pipeline.target, say=say)
    try:
        wanted = points if points is not None else list(selected_points(directory))
        try:
            plans = [
                plan_verify(
                    cluster,
                    directory,
                    item,
                    declared=pipeline.app.restore_verifies(),
                    claims=claims,
                    target=pipeline.target.identity(),
                    image=settings.image,
                    keep=keep,
                )
                for item in wanted
            ]
        except RestorePointError as error:
            reject(error.code, str(error))
        digest = (
            plans[0]["verify_hash"]
            if points is not None
            else "sha256:"
            + hashlib.sha256(
                _canonical([item["verify_hash"] for item in plans]).encode()
            ).hexdigest()
        )
        for planned in plans:
            say(
                f"restore point {planned['point']}: {len(planned['claims'])} "
                f"claim(s), {planned['bytes']} bytes into scratch claims"
                + (" (kept afterwards)" if keep else "")
            )
            for item in planned["claims"]:
                say(f"  {item['claim']} -> {item['scratch_claim']} ({item['size']})")
            for check in planned["checks"]:
                say(f"  verify {check['workload']}: {' '.join(check['command'])}")
        if not plans:
            say(f"no verified restore points in {directory}")
        if approve is None:
            say("live claims and writers are not touched; approve with:")
            which = "--all" if points is None else f"--point {points[0]}"
            say(
                f"  piceli restore {target} {which} --to-new-claim "
                f"{'--keep ' if keep else ''}--approve {digest}"
            )
            emit_json(
                {
                    "schema": VERIFY_SCHEMA,
                    "state": "approval-required",
                    "verify_hash": digest,
                    "plans": plans,
                }
            )
            raise typer.Exit(EXIT_APPROVAL)
        if approve != digest:
            reject(
                "restore-verify-plan-changed",
                "the approved hash is not the verify plan's current hash; plan again",
                verify_hash=digest,
            )
        jobs = backend.prerollout_cluster(pipeline.target)
        receipts = []
        try:
            for planned in plans:
                try:
                    receipts.append(
                        verify(
                            cluster,
                            jobs,
                            directory,
                            planned,
                            app=pipeline.app.name,
                            timeout_seconds=settings.timeout_seconds,
                            run_as_user=settings.run_as_user,
                            say=say,
                        )
                    )
                except RestorePointError as error:
                    fail(error.code, str(error), **error.details)
        finally:
            jobs.close()
        passed = all(item["state"] == "passed" for item in receipts)
        for receipt in receipts:
            for item in receipt["claims"]:
                say(
                    f"{receipt['point']} {item['claim']}: {item['result']}"
                    + (f" ({item['reason']})" if item.get("reason") else "")
                )
        emit_json(
            {
                "schema": VERIFY_SCHEMA,
                "state": "passed" if passed else "failed",
                **({} if passed else {"reason": "restore-verify-failed"}),
                "verify_hash": digest,
                "receipts": receipts,
            }
        )
        if not passed:
            raise typer.Exit(EXIT_FAILED)
    finally:
        cluster.close()


def register(root: typer.Typer) -> None:
    """Add ``restore-points`` and ``restore`` to the root application."""
    root.command("restore-points")(restore_points)
    root.command("restore")(restore)
