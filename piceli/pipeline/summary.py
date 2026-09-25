"""Run summaries: ``<state_dir>/runs/<run id>/summary.json`` and ``summary.md``.

Every ``piceli deploy`` run that starts executing writes both when it ends,
whatever the outcome (ready, stopped, failed, rejected, rolled back or
interrupted): the JSON (``piceli.run-summary.v1``,
``docs/schemas/piceli-run-summary-v1.schema.json``) is for agents, the
Markdown for people (CI posts it as the job summary or a pull request
comment).

A summary is derived only from the run journal and the receipts it names:
commits and refs, image digests and sizes, delivery transfer counts, the
release plan's action classes and changed objects with their changed field
paths (never values), check results, the failure's registered code and
message, and stage timings. It never contains a secret value, a kubeconfig
path or process output.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from piceli.pipeline.journal import Run, write_private
from piceli.pipeline.model import STAGES

SCHEMA = "piceli.run-summary.v1"
#: Changed field paths listed per object (the count is always complete).
MAX_FIELDS = 20
#: Objects listed in the Markdown plan section (the JSON lists all).
MAX_MARKDOWN_CHANGES = 50
_FAILED = ("failed", "rejected", "interrupted")


def summary_paths(run_path: Path) -> tuple[Path, Path]:
    """``(summary.json, summary.md)`` of the run journaled at ``run_path``."""
    directory = run_path.with_suffix("")
    return directory / "summary.json", directory / "summary.md"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read(path: Any) -> dict[str, Any]:
    if not isinstance(path, str):
        return {}
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return _dict(value)


def _images(run: Run, state_dir: Path) -> dict[str, Any]:
    from piceli.pipeline.operate import delivery_path

    stages = run.data.get("stages") or {}
    built: dict[str, dict[str, Any]] = {}
    for output in _dict(_dict(stages.get("build")).get("output")).values():
        for name, value in _dict(_dict(output).get("images")).items():
            built[name] = _dict(value)
    delivered = _dict(_dict(_dict(stages.get("deliver")).get("output")).get("images"))
    planned = _dict(_dict(_dict(stages.get("plan")).get("output")).get("images"))
    images: dict[str, Any] = {}
    for name in sorted(set(built) | set(delivered) | set(planned)):
        entry: dict[str, Any] = {}
        made = built.get(name, {})
        delivery = _dict(delivered.get(name))
        config = delivery.get("config_digest") or made.get("image_id")
        if isinstance(config, str):
            entry["config_digest"] = config
        if isinstance(delivery.get("reference"), str):
            entry["reference"] = delivery["reference"]
        elif isinstance(planned.get(name), str):
            entry["reference"] = planned[name]
        if type(made.get("size_bytes")) is int:
            entry["size_bytes"] = made["size_bytes"]
        if delivery.get("action"):
            entry["delivery"] = {"action": delivery["action"]}
            receipt = (
                _read(str(delivery_path(state_dir, name, config)))
                if isinstance(config, str)
                else {}
            )
            blobs = _dict(receipt.get("blobs"))
            if blobs:
                keys = ("uploaded", "skipped", "uploaded_bytes", "skipped_bytes")
                entry["delivery"].update(
                    {key: blobs[key] for key in keys if type(blobs.get(key)) is int}
                )
                total = blobs.get("uploaded_bytes", 0) + blobs.get("skipped_bytes", 0)
                if isinstance(total, int) and total > 0:
                    entry["size_bytes"] = total
            transfer = _dict(receipt.get("transfer"))
            if type(transfer.get("bytes")) is int and transfer["bytes"] > 0:
                entry["delivery"]["transferred_bytes"] = transfer["bytes"]
        images[name] = entry
    return images


def _plan(run: Run) -> dict[str, Any] | None:
    stages = run.data.get("stages") or {}
    output = _dict(_dict(stages.get("plan")).get("output"))
    if not output:
        planned = _dict(_dict(run.data.get("plan")).get("stages")).get("plan")
        output = _dict(planned)
        if output.get("state") != "planned":
            return None
    counts = {
        str(key): int(value)
        for key, value in _dict(output.get("summary")).items()
        if type(value) is int
    }
    changes: list[dict[str, Any]] = [
        {
            "operation": str(item.get("operation")),
            "kind": str(item.get("kind")),
            "name": str(item.get("name")),
        }
        for item in output.get("changes") or []
        if isinstance(item, Mapping)
    ]
    fields = {
        (str(item.get("kind")), str(item.get("name"))): item
        for item in output.get("diff") or []
        if isinstance(item, Mapping)
    }
    for change in changes:
        diff = fields.get((change["kind"], change["name"]))
        if diff is not None:
            change["fields"] = list(diff.get("fields") or [])[:MAX_FIELDS]
            change["changed_fields"] = int(diff.get("changed") or 0)
    return {
        "release": output.get("release"),
        "mode": output.get("mode"),
        "counts": counts,
        "classes": sorted(
            key for key, value in counts.items() if key != "no-op" and value
        ),
        "changes": changes,
        "drift": [
            {"kind": item.get("kind"), "name": item.get("name")}
            for item in output.get("drift") or []
            if isinstance(item, Mapping)
        ],
    }


def _checks(run: Run) -> dict[str, Any] | None:
    stage = _dict((run.data.get("stages") or {}).get("checks"))
    output = _dict(stage.get("output"))
    if not output:
        return None
    results = [
        {
            key: item[key]
            for key in ("name", "type", "passed", "code", "detail", "duration")
            if key in item
        }
        for item in output.get("results") or []
        if isinstance(item, Mapping)
    ]
    value: dict[str, Any] = {
        "passed": output.get("passed"),
        "results": results,
    }
    if "why" in output:
        value["why"] = output["why"]
    rollback = _dict(output.get("rollback"))
    if rollback:
        value["rollback"] = {
            key: rollback.get(key)
            for key in ("state", "release", "reason")
            if rollback.get(key) is not None
        }
    return value


def _failure(run: Run) -> dict[str, Any] | None:
    stages = run.data.get("stages") or {}
    for name in STAGES:
        stage = _dict(stages.get(name))
        if stage.get("state") not in _FAILED:
            continue
        failure: dict[str, Any] = {"stage": name, "state": stage["state"]}
        reason = stage.get("reason") or run.data.get("reason")
        if isinstance(reason, str):
            failure["reason"] = reason
            failure["explain"] = f"piceli explain {reason}"
        if isinstance(stage.get("message"), str):
            failure["message"] = stage["message"]
        execution = _dict(_dict(stage.get("output")).get("execution"))
        if execution.get("failure_category"):
            failure["category"] = execution["failure_category"]
        causes = execution.get("causes")
        if isinstance(causes, list) and causes:
            failure["causes"] = causes
        if name == "checks":
            failure["failed_checks"] = [
                {"name": item.get("name"), "code": item.get("code")}
                for item in _dict(stage.get("output")).get("results") or []
                if isinstance(item, Mapping) and not item.get("passed")
            ]
        return failure
    return None


def build(run: Run, state_dir: Path) -> dict[str, Any]:
    """The ``piceli.run-summary.v1`` document of ``run``."""
    data = run.data
    identity = _dict(data.get("pipeline"))
    target = _dict(identity.get("target"))
    stages_data = data.get("stages") or {}
    stages: dict[str, Any] = {}
    total = 0.0
    for name in STAGES:
        stage = _dict(stages_data.get(name))
        entry: dict[str, Any] = {"state": stage.get("state", "pending")}
        seconds = stage.get("seconds")
        if isinstance(seconds, int | float):
            entry["seconds"] = seconds
            total += float(seconds)
        if isinstance(stage.get("reason"), str):
            entry["reason"] = stage["reason"]
        stages[name] = entry
    inputs = _dict(data.get("plan")).get("stages", {}).get("inputs", {})
    sources = {}
    refs = _dict(data.get("refs"))
    for name, value in _dict(_dict(inputs).get("sources")).items():
        source = _dict(value)
        sources[name] = {
            "commit": source.get("commit"),
            "dirty": bool(source.get("dirty")),
            **({"ref": _dict(refs.get(name)).get("ref")} if name in refs else {}),
        }
    for name, value in refs.items():
        sources.setdefault(
            name,
            {
                "commit": _dict(value).get("commit"),
                "dirty": False,
                "ref": _dict(value).get("ref"),
            },
        )
    apply = _dict(_dict(stages_data.get("apply")).get("output"))
    plan = _plan(run)
    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run.run_id,
        "state": data.get("state"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "until": data.get("until"),
        "combined_hash": data.get("combined_hash"),
        "pipeline": {
            "app": identity.get("app"),
            "owner": identity.get("owner"),
            "namespace": target.get("namespace"),
            "context": target.get("context"),
            **(
                {"environment": _dict(identity.get("environment")).get("name")}
                if identity.get("environment")
                else {}
            ),
        },
        "release": apply.get("release") or (plan or {}).get("release"),
        "sources": sources,
        "images": _images(run, state_dir),
        "plan": plan,
        "checks": _checks(run),
        "failure": _failure(run),
        "stages": stages,
        "seconds": round(total, 3),
    }
    if isinstance(data.get("reason"), str):
        summary["reason"] = data["reason"]
    return summary


# -------------------------------------------------------------- markdown


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _code(value: Any) -> str:
    text = _cell(value).replace("`", "'")
    return f"`{text}`" if text else ""


def _short(digest: Any) -> str:
    if not isinstance(digest, str):
        return ""
    name, sep, hex_digest = digest.partition("sha256:")
    return f"{name}sha256:{hex_digest[:12]}" if sep else digest


def _size(value: Any) -> str:
    from piceli.maintenance.cache import format_size

    return format_size(value) if type(value) is int else ""


def _seconds(value: Any) -> str:
    if not isinstance(value, int | float):
        return ""
    if value >= 60:
        return f"{int(value // 60)}m {value % 60:.0f}s"
    return f"{value:.1f}s"


def _cause(cause: Any) -> str:
    """One compact diagnosis cause (``piceli.k8s.ops.diagnosis.compact``)."""
    item = _dict(cause)
    if not {"kind", "name", "reason"} <= set(item):
        return _cell(json.dumps(cause, sort_keys=True))
    text = _code(f"{item['kind']}/{item['name']}")
    if item.get("container"):
        text += f" container {_code(item['container'])}"
    text += f": {_code(item['reason'])}"
    if type(item.get("exit_code")) is int:
        text += f", exit {item['exit_code']}"
    if type(item.get("restarts")) is int and item["restarts"]:
        text += f", {item['restarts']} restart(s)"
    return text


def markdown(summary: Mapping[str, Any]) -> str:
    """The Markdown view of a summary (GitHub-flavoured; no secret values)."""
    pipeline = _dict(summary.get("pipeline"))
    lines = [
        f"### piceli deploy {_code(pipeline.get('app'))}: {summary.get('state')}",
        "",
        "| | |",
        "| --- | --- |",
        f"| Run | {_code(summary.get('run_id'))} |",
    ]
    if summary.get("release"):
        lines.append(f"| Release | {_code(summary['release'])} |")
    if pipeline.get("environment"):
        lines.append(f"| Environment | {_code(pipeline['environment'])} |")
    lines.append(
        f"| Target | {_code(pipeline.get('context'))} namespace "
        f"{_code(pipeline.get('namespace'))} |"
    )
    lines.append(f"| Combined hash | {_code(summary.get('combined_hash'))} |")
    lines.append(f"| Duration | {_seconds(summary.get('seconds'))} |")
    failure = summary.get("failure")
    if isinstance(failure, Mapping):
        lines += ["", "#### Failure", ""]
        head = f"Stage {_code(failure.get('stage'))} {failure.get('state')}"
        if failure.get("reason"):
            head += f": {_code(failure['reason'])}"
        lines.append(head)
        if failure.get("message"):
            lines += ["", f"> {_cell(failure['message'])}", ""]
        for cause in failure.get("causes") or []:
            lines.append(f"- {_cause(cause)}")
        for check in failure.get("failed_checks") or []:
            lines.append(
                f"- check {_code(check.get('name') or '?')}: "
                f"{_code(check.get('code') or 'failed')}"
            )
        if failure.get("explain"):
            lines += ["", f"Explain with {_code(failure['explain'])}."]
    sources = _dict(summary.get("sources"))
    if sources:
        lines += ["", "#### Sources", ""]
        for name, value in sources.items():
            source = _dict(value)
            commit = str(source.get("commit") or "")[:12]
            ref = f" (ref {_code(source['ref'])})" if source.get("ref") else ""
            dirty = " (dirty working tree)" if source.get("dirty") else ""
            lines.append(f"- {_code(name)}: {_code(commit)}{ref}{dirty}")
    images = _dict(summary.get("images"))
    if images:
        lines += [
            "",
            "#### Images",
            "",
            "| Image | Digest | Size | Delivery |",
            "| --- | --- | --- | --- |",
        ]
        for name, value in images.items():
            image = _dict(value)
            reference = image.get("reference") or image.get("config_digest")
            digest = _short(str(reference).rpartition("@")[2] if reference else None)
            delivery = _dict(image.get("delivery"))
            text = str(delivery.get("action") or "")
            if "uploaded" in delivery:
                text += (
                    f": {delivery['uploaded']} blob(s) uploaded "
                    f"({_size(delivery.get('uploaded_bytes'))}), "
                    f"{delivery.get('skipped', 0)} reused "
                    f"({_size(delivery.get('skipped_bytes'))})"
                )
            elif "transferred_bytes" in delivery:
                text += f": {_size(delivery['transferred_bytes'])} transferred"
            lines.append(
                f"| {_code(name)} | {_code(digest)} | {_size(image.get('size_bytes'))} "
                f"| {_cell(text)} |"
            )
    plan = summary.get("plan")
    if isinstance(plan, Mapping):
        counts = _dict(plan.get("counts"))
        counted = ", ".join(f"{value} {key}" for key, value in counts.items())
        lines += [
            "",
            "#### Plan",
            "",
            f"Release {_code(plan.get('release'))} ({plan.get('mode')}): "
            f"{counted or 'no actions'}",
        ]
        changes = list(plan.get("changes") or [])
        if changes:
            lines.append("")
        for change in changes[:MAX_MARKDOWN_CHANGES]:
            line = f"- {change['operation']} {_code(change['kind'] + '/' + change['name'])}"
            fields = change.get("fields") or []
            if fields:
                more = int(change.get("changed_fields") or len(fields)) - len(fields)
                line += ": " + ", ".join(_code(item) for item in fields)
                if more > 0:
                    line += f" and {more} more"
            lines.append(line)
        if len(changes) > MAX_MARKDOWN_CHANGES:
            lines.append(
                f"- … and {len(changes) - MAX_MARKDOWN_CHANGES} more (see summary.json)"
            )
        for item in plan.get("drift") or []:
            lines.append(
                f"- drift {_code(str(item.get('kind')) + '/' + str(item.get('name')))}: "
                "edited by another field manager (re-applied)"
            )
    checks = summary.get("checks")
    if isinstance(checks, Mapping):
        results = list(checks.get("results") or [])
        passed = sum(1 for item in results if item.get("passed"))
        verdict = "passed" if checks.get("passed") else "FAILED"
        lines += ["", "#### Checks", ""]
        if not results and checks.get("why"):
            lines.append(f"{verdict} ({_cell(checks['why'])})")
        else:
            lines.append(f"{verdict}: {passed} of {len(results)} passed")
        for item in results:
            mark = "passed" if item.get("passed") else "FAILED"
            code = f" {_code(item['code'])}" if item.get("code") else ""
            detail = f": {_cell(item['detail'])}" if item.get("detail") else ""
            lines.append(f"- {_code(item.get('name') or '?')} {mark}{code}{detail}")
        rollback = _dict(checks.get("rollback"))
        if rollback:
            lines.append(
                f"- rollback to {_code(rollback.get('release'))}: {rollback.get('state')}"
            )
    lines += ["", "#### Stages", "", "| Stage | State | Time |", "| --- | --- | --- |"]
    for name, value in _dict(summary.get("stages")).items():
        stage = _dict(value)
        state = str(stage.get("state"))
        if stage.get("reason"):
            state += f" ({stage['reason']})"
        lines.append(f"| {name} | {_cell(state)} | {_seconds(stage.get('seconds'))} |")
    return "\n".join(lines) + "\n"


def write(run: Run, state_dir: Path) -> dict[str, str]:
    """Write ``summary.json`` and ``summary.md`` next to the run journal."""
    document = build(run, state_dir)
    json_path, markdown_path = summary_paths(run.path)
    write_private(json_path, json.dumps(document, sort_keys=True, indent=2) + "\n")
    write_private(markdown_path, markdown(document))
    return {"json": str(json_path), "markdown": str(markdown_path)}


def read(run_path: Path) -> dict[str, Any] | None:
    """The summary of the run journaled at ``run_path``, if written."""
    json_path, _ = summary_paths(run_path)
    try:
        value = json.loads(json_path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == SCHEMA else None
