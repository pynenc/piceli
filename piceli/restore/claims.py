"""Grow or move a retained claim when a release changes its size or class.

Maturity: **preview** (the API may change before 1.0).

A release that asks for a larger claim gets one of two steps in the deploy's
``backup`` stage, after the restore point is taken and while every writer is
stopped:

- **expand** when the claim's StorageClass has ``allowVolumeExpansion:
  true``: the claim's requested size is patched and Piceli waits until the
  volume has grown (or only the node-side file system resize is left, which
  completes when the pod mounts it);
- **migrate** otherwise, or to change the storage class: a new claim is
  created, the restore point's verified archive of the old claim is
  extracted into it, its content digest is checked in the cluster and the
  app's ``restore_verify`` command (when declared) runs against it
  read-only. The release then switches the workload to the new claim. The
  old claim is kept, annotated ``piceli.io/migrated-to``, and never deleted.

A smaller size is refused (``claim-shrink-refused``); a larger size or a new
class that cannot be applied in place is refused with the declaration that
moves the data (``claim-migration-required``).

:func:`plan_claims` is pure (manifests in, steps out); :func:`grow` runs the
steps against a :class:`~piceli.restore.cluster.RestoreCluster`.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from piceli.restore.model import RestorePointError
from piceli.restore.plan import WORKLOAD_KINDS, _pod, _ref, _template_settings

#: Label of both claims of a move: ``source`` (kept) or ``target`` (new).
MIGRATION_LABEL = "piceli.io/migration"
#: Annotation on the new claim: the claim its data came from.
MIGRATED_FROM = "piceli.io/migrated-from"
#: Annotation on the old claim: the claim its data moved to.
MIGRATED_TO = "piceli.io/migrated-to"

_FAILED_RESIZE = {"ControllerResizeInfeasible", "NodeResizeInfeasible"}


def quantity(value: Any) -> Decimal:
    """A Kubernetes quantity (``512Mi``, ``8Gi``) as bytes."""
    from kubernetes.utils import parse_quantity

    return Decimal(parse_quantity(str(value)))


def _meta(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return value if isinstance(value, dict) else {}


def _spec(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("spec")
    return value if isinstance(value, dict) else {}


def requested(claim: Mapping[str, Any]) -> str | None:
    """A claim's requested storage (``spec.resources.requests.storage``)."""
    value = ((_spec(claim).get("resources") or {}).get("requests") or {}).get("storage")
    return str(value) if value else None


def _mounted_claims(manifest: Mapping[str, Any] | None) -> set[str]:
    return {
        str(volume["persistentVolumeClaim"].get("claimName"))
        for volume in _pod(manifest or {}).get("volumes") or ()
        if isinstance(volume, dict)
        and isinstance(volume.get("persistentVolumeClaim"), dict)
    }


def _replica_claims(
    template: str, workload: str, existing: Iterable[str]
) -> list[tuple[str, int]]:
    pattern = re.compile(re.escape(f"{template}-{workload}-") + r"(0|[1-9][0-9]*)")
    found = []
    for name in sorted(existing):
        match = pattern.fullmatch(name)
        if match:
            found.append((name, int(match.group(1))))
    return sorted(found, key=lambda item: item[1])


@dataclass
class ClaimPlan:
    """The claims a release grows in place or moves to a new claim."""

    #: One entry per claim: ``action`` (``expand`` or ``migrate``), ``claim``
    #: (the live claim), ``workload``, ``ordinal``, ``from`` and ``to``
    #: (sizes), ``storage_class``; a move also has ``target``,
    #: ``access_modes`` and ``resume`` (the new claim exists from an earlier,
    #: unfinished run and is filled again).
    steps: list[dict[str, Any]] = field(default_factory=list)
    #: Declarations with nothing to do, and why (evidence for the plan).
    notes: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.steps

    def identity(self) -> list[dict[str, Any]]:
        """What an approval covers (``resume`` is live state, not intent)."""
        return [
            {key: value for key, value in step.items() if key != "resume"}
            for step in self.steps
        ]

    def touched(self) -> dict[str, dict[str, Any]]:
        """Claims the restore point must cover first, with the reason."""
        found: dict[str, dict[str, Any]] = {}
        for step in self.steps:
            why = (
                f"claim grows from {step['from']} to {step['to']}"
                if step["action"] == "expand"
                else f"claim moves to {step['target']} ({step['to']})"
            )
            found[str(step["claim"])] = {
                "workload": step["workload"],
                "ordinal": step.get("ordinal"),
                "why": why,
            }
        return found


def _refuse_shrink(claim: str, have: str, want: str) -> RestorePointError:
    return RestorePointError(
        "claim-shrink-refused",
        f"claim {claim} holds {have} and the release asks for {want}: a claim "
        "never shrinks; keep the size, or move the data to a new, smaller "
        "claim by hand",
    )


def _needs_migration(claim: str, why: str, hint: str) -> RestorePointError:
    return RestorePointError(
        "claim-migration-required",
        f"claim {claim}: {why}; move its data to a new claim instead: {hint}",
    )


def _expand_or_refuse(
    claim: str,
    live: Mapping[str, Any],
    want: str,
    *,
    workload: str,
    ordinal: int | None,
    expandable: Callable[[str | None], bool],
    hint: str,
) -> dict[str, Any] | None:
    have = requested(live) or "0"
    if quantity(want) < quantity(have):
        raise _refuse_shrink(claim, have, want)
    if quantity(want) == quantity(have):
        return None
    storage_class = _spec(live).get("storageClassName")
    if not expandable(storage_class):
        raise _needs_migration(
            claim,
            f"storage class {storage_class or '(none)'} does not allow volume "
            f"expansion (allowVolumeExpansion), so it cannot grow from {have} "
            f"to {want} in place",
            hint,
        )
    return {
        "action": "expand",
        "claim": claim,
        "workload": workload,
        "ordinal": ordinal,
        "from": have,
        "to": want,
        "storage_class": storage_class,
    }


def _migration(
    source: str,
    target: str,
    live: Mapping[str, Mapping[str, Any]],
    *,
    workload: str,
    ordinal: int | None,
    size: str,
    storage_class: str | None,
    access_modes: Sequence[str] | None,
) -> dict[str, Any]:
    claim = live[source]
    have = requested(claim) or "0"
    if quantity(size) < quantity(have):
        raise _refuse_shrink(source, have, size)
    existing = live.get(target)
    resume = False
    if existing is not None:
        annotations = _meta(existing).get("annotations") or {}
        if annotations.get(MIGRATED_FROM) != source:
            raise RestorePointError(
                "claim-migration-target-exists",
                f"claim {target} already exists and was not made by Piceli to "
                f"take the data of {source}; pick another name for the new "
                "claim",
            )
        resume = True
    spec = _spec(claim)
    return {
        "action": "migrate",
        "claim": source,
        "target": target,
        "workload": workload,
        "ordinal": ordinal,
        "from": have,
        "to": size,
        "storage_class": storage_class
        if storage_class is not None
        else spec.get("storageClassName"),
        "access_modes": sorted(
            access_modes or spec.get("accessModes") or ["ReadWriteOnce"]
        ),
        "resume": resume,
    }


def plan_claims(
    desired: Iterable[Mapping[str, Any]],
    live: Iterable[Mapping[str, Any]],
    claims: Iterable[Mapping[str, Any]],
    *,
    declared: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    expandable: Callable[[str | None], bool] = lambda _class: False,
) -> ClaimPlan:
    """The claims a release grows or moves (see the module doc).

    :param desired: The release's workload manifests (other kinds ignored).
    :param live: The namespace's live workloads.
    :param claims: The namespace's live PersistentVolumeClaims.
    :param declared: :meth:`piceli.app.App.claim_growth`.
    :param expandable: Whether a storage class allows volume expansion.
    :raises RestorePointError: ``claim-shrink-refused``,
        ``claim-migration-required``, ``claim-migration-target-exists``.
    """
    declared = declared or {}
    wanted = {
        _ref(item): item for item in desired if item.get("kind") in WORKLOAD_KINDS
    }
    current = {_ref(item): item for item in live if item.get("kind") in WORKLOAD_KINDS}
    every = {
        str(_meta(item).get("name")): item
        for item in claims
        if not _meta(item).get("deletionTimestamp")
    }
    bound = {
        name: item
        for name, item in every.items()
        if (item.get("status") or {}).get("phase", "Bound") == "Bound"
    }
    result = ClaimPlan()
    for ref, manifest in sorted(wanted.items()):
        kind, _, name = ref.partition("/")
        live_manifest = current.get(ref)
        growth = list(declared.get(ref, ()))
        moves = {
            str(item["template"]): str(item["migrate_from"])
            for item in growth
            if item.get("template")
        }
        if kind == "StatefulSet":
            before = {
                str(item["name"]): item
                for item in (_template_settings(live_manifest) if live_manifest else [])
            }
            for template in _template_settings(manifest):
                tpl = str(template["name"])
                size = str(template["storage"])
                source = moves.get(tpl)
                if source is not None:
                    if tpl in before:
                        result.notes.append(
                            f"{ref} already mounts template {tpl}: nothing to move"
                        )
                        continue
                    replicas = _replica_claims(source, name, bound)
                    if not replicas:
                        result.notes.append(
                            f"{ref}: no claim of template {source} to move"
                        )
                    for claim, ordinal in replicas:
                        result.steps.append(
                            _migration(
                                claim,
                                f"{tpl}-{name}-{ordinal}",
                                every,
                                workload=ref,
                                ordinal=ordinal,
                                size=size,
                                storage_class=template["storageClassName"],
                                access_modes=template["accessModes"],
                            )
                        )
                    continue
                old = before.get(tpl)
                if old is None or old == template:
                    continue
                if old.get("storage") and quantity(size) < quantity(old["storage"]):
                    raise _refuse_shrink(
                        f"template {tpl} of {ref}", old["storage"], size
                    )
                hint = (
                    f'ClaimTemplate("{tpl}-2", size="{size}", '
                    f'migrate_from="{tpl}") and replace=["{ref}"]'
                )
                for claim, ordinal in _replica_claims(tpl, name, bound):
                    have_class = _spec(bound[claim]).get("storageClassName")
                    want_class = template["storageClassName"]
                    if want_class is not None and want_class != have_class:
                        raise _needs_migration(
                            claim,
                            f"its storage class {have_class or '(none)'} cannot "
                            f"change to {want_class} in place",
                            hint,
                        )
                    step = _expand_or_refuse(
                        claim,
                        bound[claim],
                        size,
                        workload=ref,
                        ordinal=ordinal,
                        expandable=expandable,
                        hint=hint,
                    )
                    if step is not None:
                        result.steps.append(step)
        for item in growth:
            if not item.get("claim"):
                continue
            target, size = str(item["claim"]), str(item["size"])
            source = item.get("migrate_from")
            if source is not None:
                if target in _mounted_claims(live_manifest):
                    result.notes.append(
                        f"{ref} already mounts claim {target}: nothing to move"
                    )
                    continue
                if source not in bound:
                    result.notes.append(
                        f"{ref}: claim {source} does not exist; nothing to move"
                    )
                    continue
                result.steps.append(
                    _migration(
                        str(source),
                        target,
                        every,
                        workload=ref,
                        ordinal=None,
                        size=size,
                        storage_class=item.get("storage_class"),
                        access_modes=None,
                    )
                )
                continue
            if target not in bound:
                result.notes.append(f"{ref}: claim {target} does not exist yet")
                continue
            step = _expand_or_refuse(
                target,
                bound[target],
                size,
                workload=ref,
                ordinal=None,
                expandable=expandable,
                hint=(
                    f'ExistingClaim("{target}-2", size="{size}", '
                    f'migrate_from="{target}")'
                ),
            )
            if step is not None:
                result.steps.append(step)
    targets = [step["target"] for step in result.steps if step.get("target")]
    doubled = sorted({name for name in targets if targets.count(name) > 1})
    if doubled:
        raise RestorePointError(
            "claim-migration-target-exists",
            f"two claims would move into claim {doubled[0]}",
        )
    result.steps.sort(key=lambda step: (str(step["workload"]), str(step["claim"])))
    return result


# ------------------------------------------------------------------ run


def migration_claim(step: Mapping[str, Any]) -> dict[str, Any]:
    """The new claim of a move: shaped by the step, labelled and annotated."""
    body: dict[str, Any] = {
        "accessModes": list(step.get("access_modes") or ["ReadWriteOnce"]),
        "resources": {"requests": {"storage": str(step["to"])}},
    }
    if step.get("storage_class") is not None:
        body["storageClassName"] = step["storage_class"]
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": step["target"],
            "labels": {
                "app.kubernetes.io/managed-by": "piceli",
                MIGRATION_LABEL: "target",
            },
            "annotations": {MIGRATED_FROM: step["claim"]},
        },
        "spec": body,
    }


def _entry(record: Mapping[str, Any], claim: str) -> dict[str, Any]:
    for item in record.get("claims") or ():
        if item.get("claim") == claim:
            return dict(item)
    raise RestorePointError(
        "claim-migration-failed",
        f"the restore point holds no copy of claim {claim}",
        failed=True,
    )


def _grown(claim: Mapping[str, Any], want: str) -> str | None:
    """``done``, ``filesystem-resize-pending``, ``failed`` or ``None`` (wait)."""
    status = claim.get("status") or {}
    statuses = status.get("allocatedResourceStatuses") or {}
    if str(statuses.get("storage") or "") in _FAILED_RESIZE:
        return "failed"
    capacity = (status.get("capacity") or {}).get("storage")
    conditions = {
        str(item.get("type"))
        for item in status.get("conditions") or ()
        if isinstance(item, dict) and str(item.get("status", "True")) == "True"
    }
    if (
        capacity
        and quantity(capacity) >= quantity(want)
        and "Resizing" not in (conditions)
    ):
        return "done"
    if "FileSystemResizePending" in conditions:
        return "filesystem-resize-pending"
    return None


def grow(
    cluster: Any,
    steps: Sequence[Mapping[str, Any]],
    *,
    record: Mapping[str, Any],
    directory: Path,
    workloads: Mapping[str, Mapping[str, Any] | None],
    verifies: Mapping[str, Mapping[str, Any]],
    jobs: Any,
    app: str,
    image: str | None,
    timeout_seconds: int,
    run_as_user: int = 0,
    say: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Carry out planned steps while the writers are stopped.

    The caller took the restore point ``record`` (in ``directory``) and
    starts the writers again when this raises.

    :param workloads: The live (stopped) workloads by ``Kind/name``.
    :param verifies: :meth:`piceli.app.App.restore_verifies`.
    :param jobs: Runs a verify Job (``run_job``; see
        :class:`piceli.pipeline.prerollout_cluster.PreRolloutCluster`).
    :raises RestorePointError: ``claim-expansion-failed``,
        ``claim-migration-target-exists``, ``claim-migration-failed``,
        ``claim-migration-mismatch``, ``claim-migration-verify-failed``.
    """
    import time

    from piceli.restore.cluster import helper_job, restore_command
    from piceli.restore.runner import _chunks, _digest_in_pod, _main_image, _template
    from piceli.restore.verify import _mount_path, _public_outcome, verify_job

    say = say or (lambda _line: None)
    timeout = float(timeout_seconds)
    point = str(record["id"])
    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        claim = str(step["claim"])
        if step["action"] == "expand":
            say(f"growing claim {claim} from {step['from']} to {step['to']}")
            cluster.expand_claim(claim, str(step["to"]))
            deadline = time.monotonic() + timeout
            while True:
                live = cluster.claim(claim) or {}
                state = _grown(live, str(step["to"]))
                if state == "failed":
                    raise RestorePointError(
                        "claim-expansion-failed",
                        f"the storage driver could not grow claim {claim} to "
                        f"{step['to']}",
                        failed=True,
                    )
                if state is not None:
                    break
                if time.monotonic() >= deadline:
                    raise RestorePointError(
                        "claim-expansion-failed",
                        f"claim {claim} did not grow to {step['to']} within "
                        f"{int(timeout)}s",
                        failed=True,
                    )
                time.sleep(cluster.poll_seconds)
            results.append(
                {"action": "expand", "claim": claim, "to": step["to"], "state": state}
            )
            say(f"claim {claim}: {state}")
            continue
        target = str(step["target"])
        entry = _entry(record, claim)
        existing = cluster.claim(target)
        if existing is None:
            say(f"creating claim {target} ({step['to']}) for the data of {claim}")
            cluster.create_claim(migration_claim(step))
        elif (_meta(existing).get("annotations") or {}).get(MIGRATED_FROM) != claim:
            raise RestorePointError(
                "claim-migration-target-exists",
                f"claim {target} already exists and was not made by Piceli to "
                f"take the data of {claim}",
                failed=True,
            )
        else:
            say(f"claim {target} exists from an earlier run; filling it again")
        owner = workloads.get(str(step["workload"]))
        helper_image = image or _main_image(owner)
        if helper_image is None:
            raise RestorePointError(
                "restore-point-helper-failed",
                f"no image for the helper of claim {target}: declare "
                "RestorePoints(image=...)",
                failed=True,
            )
        source = cluster.claim(claim) or {}
        volume = _spec(source).get("volumeName")
        affinity = cluster.volume_affinity(str(volume)) if volume else None
        path = directory / str(entry["archive"])
        size = path.stat().st_size
        name = f"piceli-move-{point[-6:]}-{index:02d}"
        manifest = helper_job(
            name,
            point=point,
            claim=target,
            image=helper_image,
            read_only=False,
            seconds=int(timeout * 4),
            run_as_user=run_as_user,
            template=_template(owner),
            node_affinity=affinity,
        )
        say(f"copying claim {claim} into {target} ({size} bytes)")
        with cluster.helper(name, manifest, timeout) as pod:
            code = cluster.exec(
                pod,
                restore_command(size),
                timeout=timeout,
                stdin=_chunks(path),
                code="claim-migration-failed",
            )
            if code != 0:
                raise RestorePointError(
                    "claim-migration-failed",
                    f"copying claim {claim} into {target} exited {code}",
                    failed=True,
                )
            content = _digest_in_pod(cluster, pod, timeout)
        if content != entry.get("content_sha256"):
            raise RestorePointError(
                "claim-migration-mismatch",
                f"claim {target} does not hold the content of {claim} after the copy",
                failed=True,
            )
        result: dict[str, Any] = {
            "action": "migrate",
            "claim": claim,
            "target": target,
            "to": step["to"],
            "content_sha256": content,
            "verify": "not-declared",
        }
        declared = verifies.get(str(step["workload"]))
        if declared is not None and owner is not None:
            mount = _mount_path(owner, claim, step.get("ordinal"))
            if mount is None:
                raise RestorePointError(
                    "claim-migration-verify-failed",
                    f"{step['workload']} does not mount claim {claim}",
                    failed=True,
                )
            check = {
                **dict(declared),
                "ordinal": step.get("ordinal"),
                "mounts": {mount: target},
                "job": f"piceli-mvck-{point[-6:]}-{index:02d}",
            }
            job = verify_job(
                owner,
                check,
                point=point,
                namespace=cluster.namespace,
                app=app,
                node_affinity=affinity,
            )
            say(f"verify {step['workload']} on {target}: running Job {check['job']}")
            try:
                outcome = _public_outcome(jobs.run_job(job))
            except Exception as error:  # the API refused the Job
                outcome = {"state": "not-startable", "reason": type(error).__name__}
            result["verify"] = outcome.get("state")
            if outcome.get("state") != "passed":
                raise RestorePointError(
                    "claim-migration-verify-failed",
                    f"the restore verify command of {step['workload']} did not "
                    f"pass on {target} ({outcome.get('state')})",
                    failed=True,
                )
        cluster.annotate_claim(
            claim,
            labels={MIGRATION_LABEL: "source"},
            annotations={MIGRATED_TO: target},
        )
        results.append(result)
        say(f"claim {claim}: copied into {target}, verified ({result['verify']})")
    return results


__all__ = [
    "MIGRATED_FROM",
    "MIGRATED_TO",
    "MIGRATION_LABEL",
    "ClaimPlan",
    "grow",
    "migration_claim",
    "plan_claims",
    "quantity",
    "requested",
]
