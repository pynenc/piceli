"""The images a plan uploads, and their upload during ``piceli infra apply`` (0.25.3).

**In the plan.** Each image the plan creates (or replaces) is an entry of
the plan's ``images``: its name and address, ``upload`` or ``replace``,
where it is sent from (``build host NAME`` or ``this machine``), its size
and sha256, and for a replacement why (``a failed upload left it in
saving``, ``its sha256 changed``). The plan hash covers them. An image in
the state that a failed upload left unfinished, or whose sha256 tag is not
the declared one, is planned again with ``tofu plan -replace=ADDRESS``. The
images at the cloud that carry this image's ownership tags but are not in
the state (left by an upload that was killed) are listed as ``stale``
(outside the hash: the apply looks again and deletes them).

**In the apply,** after the approved plan's hash matched and before
``tofu apply``, for each entry: the stale images are deleted; a replaced
image that never became active is deleted and dropped from the state; the
image is uploaded (:func:`piceli.infra.machines.glance.upload_image`) and
adopted with ``tofu import``. A replaced active image is dropped from the
state then, and deleted after the apply succeeded (until then it is listed
in ``images.json`` as ``retired``). OpenTofu then plans again: that plan
must have the approved plan's changes less the uploads
(``infra-plan-changed`` otherwise), and only it is applied. The upload's
whole log is in ``image-upload-<name>.log`` in the state directory, whose
path is printed when it starts.

Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.infra.machines.model import (
    INFRA_LABEL,
    MANAGED_BY,
    RESOURCE_LABEL,
    Image,
    InfraError,
)
from piceli.infra.machines.state import read_record, write_record

if TYPE_CHECKING:
    from piceli.infra.machines.glance import Uploaded
    from piceli.infra.machines.plan import Workspace
    from piceli.infra.machines.provider import Provider

__all__ = [
    "IMAGES_RECORD",
    "UPLOAD_LOG",
    "delete_retired",
    "image_entries",
    "replacements",
    "retired",
    "stale_images",
    "upload_images",
]

IMAGES_RECORD = "images.json"
UPLOAD_LOG = "image-upload-{}.log"
Progress = Callable[[Mapping[str, Any]], None]


def _images(ws: Workspace) -> dict[str, tuple[Image, Provider]]:
    """Rendered image address -> the declared image and its provider, for
    providers whose images Piceli uploads itself."""
    found: dict[str, tuple[Image, Provider]] = {}
    for image, provider in ws.infra.images():
        for address, owner in ws.rendered.resources.items():
            if owner[1] == "image" and owner[2] == image.name:
                found[address] = (image, provider)
    return found


def _changes(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for item in document.get("resource_changes") or ():
        if isinstance(item, dict) and item.get("mode") == "managed":
            found[str(item.get("address"))] = item.get("change") or {}
    return found


def replacements(ws: Workspace, document: Mapping[str, Any]) -> list[str]:
    """Image addresses OpenTofu keeps but Piceli must upload again: a
    half-made upload, a different sha256 (``-replace=`` them)."""
    changes = _changes(document)
    found = []
    for address, (image, provider) in sorted(_images(ws).items()):
        change = changes.get(address) or {}
        before = change.get("before")
        if not isinstance(before, Mapping) or "create" in (change.get("actions") or ()):
            continue
        if provider.image_replace_reason(before, image) is not None:
            found.append(address)
    return found


def _ownership(ws: Workspace, image: Image) -> list[str]:
    """The ownership tags every image record of ``image`` carries."""
    from piceli.infra.machines.render import labels

    wanted = labels(ws.infra, "image", image.name)
    keys = (MANAGED_BY, INFRA_LABEL, RESOURCE_LABEL)
    return sorted(f"{k.replace('/', ':')}={wanted[k]}" for k in keys)


def _size(image: Image) -> int | None:
    if image.host is not None:
        return image.size
    if image.file is not None:
        try:
            return Path(image.file).stat().st_size
        except OSError:
            return image.size
    return None


def _where(image: Image) -> str:
    if image.host is not None:
        return f"build host {image.host.name}"
    return "this machine" if image.file is not None else "this machine (from its url)"


def image_entries(
    ws: Workspace, document: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """The plan's ``images``: what each upload sends, from where (hashed)."""
    changes = _changes(document)
    entries = []
    for address, (image, provider) in sorted(_images(ws).items()):
        if not provider.uploads_images:
            continue
        change = changes.get(address) or {}
        actions = tuple(change.get("actions") or ())
        if "create" not in actions:
            continue
        entry: dict[str, Any] = {
            "name": image.name,
            "address": address,
            "action": "upload" if actions == ("create",) else "replace",
            "from": _where(image),
            "bytes": _size(image),
            "sha256": image.sha256,
        }
        before = change.get("before")
        if isinstance(before, Mapping):
            reason = provider.image_replace_reason(before, image)
            entry["reason"] = reason or "replaced with its server"
        entries.append(entry)
    return tuple(entries)


def _state_id(document: Mapping[str, Any], address: str) -> str | None:
    before = (_changes(document).get(address) or {}).get("before")
    if isinstance(before, Mapping) and before.get("id"):
        return str(before["id"])
    return None


def stale_images(
    ws: Workspace,
    document: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Images left by an upload that was killed (the cloud has them with
    this image's tags, the state does not): ``(check, stale)`` for the plan."""
    from piceli.infra.machines.glance import GlanceError

    if not entries:
        return {}, []
    images = _images(ws)
    stale: list[dict[str, Any]] = []
    try:
        for entry in entries:
            image, provider = images[entry["address"]]
            glance = provider.image_service(ws.env)
            keep = _state_id(document, entry["address"])
            for record in glance.find(_ownership(ws, image)):
                if record.get("id") and record["id"] != keep:
                    stale.append(
                        {"image": image.name, "id": str(record["id"]),
                         "status": str(record.get("status"))}
                    )  # fmt: skip
    except GlanceError as error:
        return {"check": "stale image uploads", "ran": False,
                "why": f"Glance did not answer ({error}); the apply looks again"}, []  # fmt: skip
    return {"check": "stale image uploads", "ran": True, "found": len(stale)}, stale


def _retire(ws: Workspace, image_id: str, image: Image) -> None:
    record = read_record(ws.directory, IMAGES_RECORD)
    retired = [r for r in record.get("retired") or () if isinstance(r, dict)]
    if image_id not in {r.get("id") for r in retired}:
        retired.append(
            {"id": image_id, "image": image.name, "tags": _ownership(ws, image)}
        )
    record["retired"] = retired
    write_record(ws.directory, IMAGES_RECORD, record)


def retired(ws: Workspace) -> bool:
    """Whether replaced images wait to be deleted."""
    return bool(read_record(ws.directory, IMAGES_RECORD).get("retired"))


def delete_retired(ws: Workspace, in_use: set[str]) -> list[str]:
    """Delete the replaced images no server in the state uses any more."""
    from piceli.infra.machines.glance import GlanceError, ours

    record = read_record(ws.directory, IMAGES_RECORD)
    retired = [r for r in record.get("retired") or () if isinstance(r, dict)]
    if not retired:
        return []
    services = {
        provider.name: provider.image_service(ws.env)
        for _image, provider in ws.infra.images()
    }
    service = next((s for s in services.values() if s is not None), None)
    kept, deleted = [], []
    for item in retired:
        image_id = str(item.get("id"))
        try:
            if image_id in in_use or service is None:
                kept.append(item)
                continue
            found = service.get(image_id)
            if found is None:
                continue
            if not ours(found, item.get("tags") or ()):
                continue
            service.delete(image_id)
            deleted.append(image_id)
        except GlanceError:
            kept.append(item)
    record["retired"] = kept
    write_record(ws.directory, IMAGES_RECORD, record)
    return deleted


class _Log:
    """The upload's log file in the state directory (never a secret)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.write_text("")
        path.chmod(0o600)

    def __call__(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self.path.open("a") as handle:
            handle.write(f"{stamp} {line}\n")


def upload_images(
    ws: Workspace,
    document: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    *,
    say: Callable[[str], None],
    progress: Progress,
    sender_for: Callable[[Image], Any] | None = None,
    done: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Upload each entry's image and adopt it with ``tofu import``.

    What each upload did (id, bytes, seconds, rate, attempts, from, deleted
    stale ids); :class:`InfraError` when one fails (the image it made is
    deleted; the earlier ones are adopted already)."""
    from piceli.infra.machines.glance import (
        HostSender,
        LocalSender,
        remove_stale,
        upload_image,
    )
    from piceli.infra.machines.images import stage_image
    from piceli.infra.machines.plan import _check

    images = _images(ws)
    done = [] if done is None else done
    for entry in entries:
        address = str(entry["address"])
        image, provider = images[address]
        glance = provider.image_service(ws.env)
        tags = _ownership(ws, image)
        log = _Log(ws.directory / UPLOAD_LOG.format(image.name))
        size = entry.get("bytes")
        from piceli.infra.machines.glance import format_bytes

        shown = f"{format_bytes(size)}, " if isinstance(size, int) else ""
        say(f"uploading image {image.name} from {entry['from']} "
            f"({shown}sha256 {image.sha256[:12]}…); full log: {log.path}")  # fmt: skip
        log(
            f"image {image.name} ({address}) from {entry['from']}, sha256 {image.sha256}"
        )
        state_id = _state_id(document, address)
        removed = remove_stale(glance, tags, keep=[state_id] if state_id else [])
        for item in removed:
            say(
                f"  deleted a stale upload of {image.name} ({item['id']}, {item['status']})"
            )
            log(f"deleted stale {item['id']} ({item['status']})")
        old = glance.get(state_id) if state_id else None
        if state_id and (old is None or old.get("status") != "active"):
            # A half-made image: deleted before the new upload.
            if old is not None:
                glance.delete(state_id)
                say(f"  deleted the unfinished image {state_id} ({old.get('status')})")
                log(f"deleted unfinished {state_id} ({old.get('status')})")
            _check(ws.run("state", "rm", address), "infra-apply-failed", "state-rm", ws)
            state_id = None
        if sender_for is not None:
            sender = sender_for(image)
        elif image.host is not None:
            sender = HostSender(image.host)
        elif image.file is not None:
            sender = LocalSender(Path(image.file))
        else:
            sender = LocalSender(stage_image(image, ws.directory))
        body = provider.image_record(
            ws.rendered.config["resource"][address.split(".", 1)[0]][
                address.split(".", 1)[1]
            ]
        )
        uploaded: Uploaded = upload_image(
            glance, name=image.name, sha256=image.sha256, body=body, sender=sender,
            tags=tags, progress=progress, log=log,
        )  # fmt: skip
        try:
            if state_id:
                # The active image it replaces: dropped from the state now,
                # deleted once the servers that use it are replaced.
                _retire(ws, state_id, image)
                _check(
                    ws.run("state", "rm", address), "infra-apply-failed", "state-rm", ws
                )
            _check(
                ws.run("import", "-input=false", "-no-color", address, uploaded.id),
                "infra-apply-failed",
                "import",
                ws,
                known=False,
            )
        except BaseException:
            from piceli.infra.machines.glance import _delete_ours

            if _delete_ours(glance, uploaded.id, tags):
                log(f"adoption failed: deleted {uploaded.id}")
            raise
        log(f"adopted {uploaded.id} as {address}")
        rate = uploaded.to_dict()
        say(f"  uploaded image {image.name}: {format_bytes(uploaded.bytes)} in "
            f"{uploaded.seconds:.0f}s ({format_bytes(rate['bytes_per_second'] or 0)}/s, "
            f"{uploaded.attempts} attempt(s)), verified and adopted")  # fmt: skip
        done.append({"name": image.name, "address": address, **rate,
                     "stale_deleted": [item["id"] for item in removed]})  # fmt: skip
    return done


def check_unchanged(
    approved: Sequence[Any], adopted: set[str], fresh: Sequence[Any]
) -> None:
    """The plan after the uploads must be the approved one less them."""
    want = {(c.address, c.action) for c in approved if c.address not in adopted}
    have = {(c.address, c.action) for c in fresh}
    if want != have:
        raise InfraError(
            "infra-plan-changed",
            "after the image upload the plan is not the approved one less the "
            "upload; the image is uploaded and adopted, review the new plan",
            fields={
                "unexpected": sorted(f"{a} {b}" for a, b in have - want),
                "missing": sorted(f"{a} {b}" for a, b in want - have),
            },
        )


def in_use(document: Mapping[str, Any]) -> set[str]:
    """Image ids the servers in a state document boot from."""
    found: set[str] = set()
    values = (document.get("values") or {}).get("root_module") or {}
    for item in values.get("resources") or ():
        if (
            not isinstance(item, dict)
            or item.get("type") == "openstack_images_image_v2"
        ):
            continue
        image_id = (item.get("values") or {}).get("image_id")
        if isinstance(image_id, str) and image_id:
            found.add(image_id)
    return found
