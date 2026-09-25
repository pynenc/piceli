"""Operate the release of a pipeline with ``piceli release … --spec MODULE:ATTR``.

``piceli deploy`` never writes a ``release.toml``: it builds its release spec in
memory (:func:`~piceli.pipeline.compose.release_spec`). :func:`operations_spec`
builds the **same** spec from the pipeline declaration and its state
directory, so every ``piceli release`` subcommand works on the releases a
pipeline deployed: same state directory (``<state_dir>/release``), release
name, owner, field manager, target (kubeconfig, context, namespace, exec
policy), secrets and composition.

Images are never built here. They come from the pipeline's receipts:

* commands that plan a **new** release from the current model (``plan``,
  ``preview``, ``diff``, ``apply``) need every build image the app uses to
  have been built from the *current* build inputs and delivered; otherwise
  they are refused with ``pipeline-not-delivered`` (run ``piceli deploy``);
* commands that operate on **catalogued** releases (``rollback``, ``resume``,
  ``stop``, ``check``, ``status``, ``secret show``) use the releases' own
  records: a rollback re-applies the archived composition with the image
  digests recorded in it (the ``oci-set`` source), never a rebuild.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.pipeline.compose import pinned_images, release_spec, used_handles
from piceli.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from piceli.k8s.release_spec import ImageRef
    from piceli.pipeline.compose import PipelineReleaseSpec
    from piceli.pipeline.model import Pipeline


def delivery_path(state_dir: Path, image: str, config: str) -> Path:
    """The delivery receipt of one image and config digest (earlier ones stay)."""
    return state_dir / "deliveries" / f"{image}-{config.removeprefix('sha256:')}.json"


def delivery_receipt(
    state_dir: Path, image: str, config: str
) -> tuple[Path, dict[str, Any]] | None:
    """The succeeded delivery receipt of exactly this image, if any."""
    path = delivery_path(state_dir, image, config)
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("approved_digest") != config:
        return None
    if receipt.get("state") != "succeeded":
        return None
    return path, receipt


def _build_receipt(pipeline: Pipeline, name: str) -> dict[str, Any] | None:
    path = pipeline.state_dir / "builds" / name / "receipt.json"
    try:
        receipt = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) else None


def delivered_images(
    pipeline: Pipeline, *, current: bool
) -> tuple[dict[str, ImageRef], list[str]]:
    """``(images, missing)``: the release's images from the pipeline's receipts.

    ``images`` holds every digest-pinned image of the app and each build
    image whose last build was delivered; ``missing`` names the build images
    the app uses that have none. With ``current`` a build's images count only
    when its receipt was built from the current build inputs (the staged
    files' plan hash), as ``piceli deploy`` would use them.
    """
    from piceli.k8s.release_spec import ImageHandoffError, load_delivery_receipt
    from piceli.pipeline.runner import classify

    configs: dict[str, str] = {}
    produced: set[str] = set()
    for build in pipeline.builds:
        try:
            spec = build.load()
            names = [image.name for image in spec.images]
            produced.update(names)
            receipt = _build_receipt(pipeline, spec.name)
            if receipt is None:
                continue
            if (
                current
                and receipt.get("plan_hash") != spec.plan(spec.load_inputs()).plan_hash
            ):
                continue
            outputs = (receipt.get("outputs") or {}).get("images") or {}
        except PipelineError:
            raise
        except Exception as error:
            raise classify(error) from None
        for name in names:
            entry = outputs.get(name)
            image_id = entry.get("image_id") if isinstance(entry, dict) else None
            if isinstance(image_id, str):
                configs[name] = image_id
    images: dict[str, ImageRef] = dict(pinned_images(pipeline, dict.fromkeys(produced)))
    missing: list[str] = []
    for name in used_handles(pipeline):
        config = configs.get(name)
        found = (
            delivery_receipt(pipeline.state_dir, name, config)
            if config is not None
            else None
        )
        if found is None:
            missing.append(name)
            continue
        try:
            images[name] = load_delivery_receipt(name, found[0])
        except ImageHandoffError:
            missing.append(name)
    return images, missing


def operations_spec(pipeline: Pipeline, *, current: bool) -> PipelineReleaseSpec:
    """The release spec ``piceli deploy`` uses for ``pipeline``, for release commands.

    :param current: The command plans a new release from the current model
        (``plan``/``preview``/``diff``/``apply``): every build image must be
        delivered from the current build inputs.
    :raises PipelineError: ``pipeline-not-delivered`` when the images are not
        available, or the codes of an invalid pipeline.
    """
    images, missing = delivered_images(pipeline, current=current)
    if current and missing:
        raise PipelineError(
            "pipeline-not-delivered",
            f"build image(s) {', '.join(missing)} of the current sources were "
            "not delivered yet; run `piceli deploy` (it builds and delivers only "
            "what changed)",
        )
    if not images:
        raise PipelineError(
            "pipeline-not-delivered",
            "no image of this pipeline was delivered yet, so it has no release; "
            "run `piceli deploy` first",
        )
    return release_spec(pipeline, images, with_checks=True)
