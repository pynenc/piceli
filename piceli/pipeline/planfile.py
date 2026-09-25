"""Portable approved plans: ``piceli deploy --plan --out FILE`` and ``--apply FILE``.

A plan file lets one runner plan and **another** runner apply, with no shared
disk and no shared build cache. It holds what the reviewer approved and what
the applying runner needs to reproduce the same plan:

* the combined hash, the ``until`` stage and ``--reapply``;
* the pipeline identity (app, owner, field manager, declared target) and the
  **observed** target (the kube-system and namespace UIDs the plan read);
* the ``--ref`` commits, every stage's plan (including a placeholder
  preview's adopt/replace/delete set);
* the receipts the plan relied on: build receipts (image config digests),
  delivery receipts (registry or node references by manifest digest) and
  mirror receipts. They are never secret and never hold local paths.

``--apply FILE --approve HASH`` never trusts the file for what to do. It
refuses a file whose hash is not the approved one, a pipeline or observed
cluster that differs, then writes the receipts into the working state
directory (content-addressed; a receipt only lets a stage be skipped when
the registry or node still has that exact digest) and **plans again**
against live state. Only when the recomputed combined hash equals the
approved one does anything run (``pipeline-plan-changed`` otherwise), so a
plan file can never widen an approval.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.journal import now, write_private
from piceli.pipeline.model import STAGES

if TYPE_CHECKING:
    from piceli.pipeline.model import Pipeline
    from piceli.pipeline.runner import CombinedPlan, PipelineRunner

PLAN_FILE_SCHEMA = "piceli.deploy-plan-file.v1"
MAX_PLAN_FILE_BYTES = 10_000_000
_HASH = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def observed_target(pipeline: Pipeline) -> dict[str, str]:
    """The kube-system and namespace UIDs of the pipeline's target (read-only)."""
    from piceli.k8s.ops.provider_factory import (
        api_client_from_kubeconfig,
        read_cluster_identity,
    )
    from piceli.state.scopes import kubeconfig_target

    target = kubeconfig_target(pipeline.target)
    client = api_client_from_kubeconfig(
        target.kubeconfig,
        target.context,
        transport=target.transport,
        exec_policy=target.exec_policy,
    )
    try:
        identity = read_cluster_identity(client, target)
    finally:
        client.close()
    return {
        "cluster_uid": identity.cluster_uid,
        "namespace_uid": identity.namespace_uid,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def receipts(runner: PipelineRunner) -> dict[str, Any]:
    """The build, delivery and mirror receipts the runner's last plan used."""
    from piceli.pipeline.compose import mirror_repository
    from piceli.pipeline.operate import delivery_receipt, mirror_receipt

    work = runner._work
    state_dir = runner.pipeline.state_dir
    builds: dict[str, Any] = {}
    deliveries: dict[str, Any] = {}
    mirrors: dict[str, Any] = {}
    if work is None:
        return {"builds": builds, "deliveries": deliveries, "mirrors": mirrors}
    for item in work.builds:
        receipt = _read_json(item.receipt_path)
        images = ((receipt or {}).get("outputs") or {}).get("images")
        if receipt is None or not isinstance(images, dict) or not item.images:
            continue
        builds[item.spec.name] = {
            "revision": receipt.get("revision"),
            "state": receipt.get("state"),
            "plan_hash": receipt.get("plan_hash"),
            "outputs": {
                "images": {
                    name: {
                        key: entry.get(key)
                        for key in ("image_id", "digest", "ref", "platform")
                    }
                    for name, entry in images.items()
                    if isinstance(entry, dict)
                }
            },
        }
    for name in work.used:
        config = work.producer[name].images.get(name, {}).get("image_id")
        if not isinstance(config, str):
            continue
        found = delivery_receipt(state_dir, name, config)
        if found is not None:
            deliveries[f"{name}@{config}"] = found[1]
    for key in work.mirrors:
        repository = mirror_repository(runner.pipeline, key)
        found = mirror_receipt(state_dir, key, repository)
        if found is not None:
            mirrors[key] = found[1]
    return {"builds": builds, "deliveries": deliveries, "mirrors": mirrors}


def plan_document(
    runner: PipelineRunner,
    combined: CombinedPlan,
    *,
    entry: str,
    reapply: bool,
    observed: dict[str, str] | None,
) -> dict[str, Any]:
    """The portable plan of ``combined`` (see the module docstring)."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("piceli")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        version = "unknown"
    refs = combined.stages.get("inputs", {}).get("refs") or {}
    return json.loads(
        json.dumps(
            {
                "schema": PLAN_FILE_SCHEMA,
                "piceli_version": version,
                "created_at": now(),
                "entry": entry,
                "combined_hash": combined.combined_hash,
                "until": combined.until,
                "reapply": reapply,
                "pipeline": runner.identity(),
                "observed_target": observed,
                "state": {"backend": runner.pipeline.state.backend},
                "refs": {name: value["commit"] for name, value in refs.items()},
                "stages": combined.stages,
                "receipts": receipts(runner),
            },
            sort_keys=True,
            default=str,
        )
    )


def write_plan_document(path: Path, document: dict[str, Any]) -> None:
    """Write the plan file atomically (mode 0600)."""
    write_private(path, json.dumps(document, sort_keys=True, indent=2) + "\n")


def _invalid(message: str) -> PipelineError:
    return PipelineError("deploy-plan-file-invalid", message)


def load_plan_document(path: Path) -> dict[str, Any]:
    """Read and validate a plan file; the content is checked, never executed."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise _invalid(f"cannot read the plan file ({type(error).__name__})") from None
    if len(raw) > MAX_PLAN_FILE_BYTES:
        raise _invalid("the plan file is larger than the limit")
    try:
        document = json.loads(raw)
    except ValueError:
        raise _invalid("the plan file is not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != PLAN_FILE_SCHEMA:
        raise _invalid(f"the plan file is not a {PLAN_FILE_SCHEMA} document")
    checks = (
        isinstance(document.get("combined_hash"), str)
        and bool(_HASH.fullmatch(document["combined_hash"])),
        isinstance(document.get("entry"), str) and bool(document["entry"]),
        document.get("until") in STAGES,
        isinstance(document.get("reapply"), bool),
        isinstance(document.get("pipeline"), dict),
        isinstance(document.get("stages"), dict),
        document.get("observed_target") is None
        or isinstance(document.get("observed_target"), dict),
    )
    if not all(checks):
        raise _invalid("the plan file is missing a required field")
    refs = document.get("refs") or {}
    if not isinstance(refs, dict) or not all(
        isinstance(name, str)
        and _NAME.fullmatch(name)
        and isinstance(commit, str)
        and _COMMIT.fullmatch(commit)
        for name, commit in refs.items()
    ):
        raise _invalid("the plan file's refs are not commit SHAs")
    found = document.get("receipts") or {}
    if not isinstance(found, dict) or not all(
        isinstance(found.get(key, {}), dict)
        for key in ("builds", "deliveries", "mirrors")
    ):
        raise _invalid("the plan file's receipts are malformed")
    for name in found.get("builds", {}):
        if not _NAME.fullmatch(str(name)):
            raise _invalid("the plan file names an invalid build")
    for key in found.get("deliveries", {}):
        image, _, config = str(key).partition("@")
        if not _NAME.fullmatch(image) or not _DIGEST.fullmatch(config):
            raise _invalid("the plan file names an invalid delivery")
    return document


def seed_receipts(pipeline: Pipeline, document: dict[str, Any]) -> list[str]:
    """Write the plan file's receipts into the working state directory.

    Build receipts replace a differing local one (the approved plan saw that
    build); delivery and mirror receipts are content-addressed and only
    added. Returns what was written (``builds/<name>``, ``deliveries/<image>``,
    ``mirrors/<source>``).
    """
    from piceli.pipeline.operate import delivery_path, mirror_path

    found = document.get("receipts") or {}
    written: list[str] = []
    state_dir = pipeline.state_dir
    for name, receipt in sorted((found.get("builds") or {}).items()):
        if not isinstance(receipt, dict) or receipt.get("state") != "succeeded":
            continue
        path = state_dir / "builds" / name / "receipt.json"
        if _read_json(path) != receipt:
            write_private(path, json.dumps(receipt, sort_keys=True, indent=2))
            written.append(f"builds/{name}")
    for key, receipt in sorted((found.get("deliveries") or {}).items()):
        image, _, config = key.partition("@")
        if (
            not isinstance(receipt, dict)
            or receipt.get("approved_digest") != config
            or receipt.get("state") != "succeeded"
        ):
            continue
        path = delivery_path(state_dir, image, config)
        if not path.exists():
            write_private(path, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
            written.append(f"deliveries/{image}")
    for key, receipt in sorted((found.get("mirrors") or {}).items()):
        repository = ((receipt or {}).get("target") or {}).get("repository")
        if (
            not isinstance(receipt, dict)
            or not isinstance(repository, str)
            or receipt.get("state") != "succeeded"
            or (receipt.get("source") or {}).get("reference") != key
        ):
            continue
        path = mirror_path(state_dir, key, repository)
        if not path.exists():
            write_private(path, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
            written.append(f"mirrors/{key}")
    return written
