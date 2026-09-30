"""Multi-platform images: one spec built for several platforms, one image index.

An image published for other people's clusters must run on their nodes,
whatever their CPU. One spec is built once per platform and the per-platform
manifests are joined in an OCI image index; a registry tag names the index,
and each node pulls the manifest of its own platform.

* :func:`image_index` writes the index bytes. They are canonical (sorted keys,
  manifests sorted by platform), so the same manifests always give the same
  index digest.
* :class:`MultiPlatformHostBuild` runs a host build
  (:mod:`piceli.artifacts.host_build`) once per platform, with the facts of
  the platform alone (:meth:`~piceli.artifacts.node_facts.NodeFacts.for_platform`:
  no node, 4 KiB pages), into one output directory per platform. Its plan
  hash covers every platform's plan; one approval runs them all, one after
  another (the cache and its target directory are shared: `cargo zigbuild
  --target {rust_arch}-…` keeps each platform's output apart). The receipt
  lists each image once per platform (``<image>/<platform-slug>``, as a
  multi-platform ``build.toml`` does) and each image's index under
  ``outputs.indexes``.
* A Docker ``build.toml`` already builds every platform of its ``platforms``
  list; its per-platform images are joined in an index when they are
  published (:mod:`piceli.artifacts.publish`).

Importing this module runs nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.artifacts.build_spec import (
    BUILD_RECEIPT_REVISION,
    BuildReceipt,
    BuildSpecError,
    Runner,
    _now,
    platform_slug,
)
from piceli.artifacts.host_build import (
    HOST_PLATFORMS,
    HostBuildGrant,
    HostBuildPlan,
    HostBuildSpec,
)
from piceli.artifacts.image_manifest import OCI_INDEX, OCI_MANIFEST
from piceli.artifacts.node_facts import NodeFacts, NodeFactsError
from piceli.artifacts.plan import canonical, digest
from piceli.artifacts.source_identity import InputsLock, InputsSpec

MULTI_PLATFORM_REVISION = "piceli.multi-platform-build.v1"


@dataclass(frozen=True)
class IndexEntry:
    """One platform manifest of an image index."""

    platform: str
    digest: str
    size: int
    media_type: str = OCI_MANIFEST

    def to_oci(self) -> dict[str, Any]:
        os_name, _, rest = self.platform.partition("/")
        arch, _, variant = rest.partition("/")
        platform = {"architecture": arch, "os": os_name}
        if variant:
            platform["variant"] = variant
        return {
            "mediaType": self.media_type,
            "digest": self.digest,
            "size": self.size,
            "platform": platform,
        }


def image_index(entries: Iterable[IndexEntry]) -> bytes:
    """The canonical OCI image index of ``entries`` (one per platform)."""
    items = sorted(entries, key=lambda item: item.platform)
    platforms = [item.platform for item in items]
    if not items or len(set(platforms)) != len(platforms):
        raise ValueError("an image index needs one manifest per platform")
    return canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [item.to_oci() for item in items],
        }
    )


def index_summary(body: bytes) -> dict[str, Any]:
    """``{"digest", "media_type", "size", "manifests": [...]}`` of index bytes."""
    import json

    document = json.loads(body)
    return {
        "digest": digest(body),
        "media_type": OCI_INDEX,
        "size": len(body),
        "manifests": [
            {
                "platform": "/".join(
                    part
                    for part in (
                        item["platform"]["os"],
                        item["platform"]["architecture"],
                        item["platform"].get("variant"),
                    )
                    if part
                ),
                "digest": item["digest"],
                "size": item["size"],
            }
            for item in document["manifests"]
        ],
    }


def platform_specs(
    spec: HostBuildSpec, platforms: Sequence[str] | None = None
) -> tuple[HostBuildSpec, ...]:
    """``spec`` once per platform (``platforms``, else ``build.platforms``).

    :raises BuildSpecError: ``build-platforms-invalid`` for an empty,
        repeated or unsupported list, or one the spec's ``build.platform``
        or ``build.platforms`` does not allow.
    """
    chosen = tuple(platforms) if platforms else spec.platforms
    if (
        not chosen
        or len(set(chosen)) != len(chosen)
        or not set(chosen) <= HOST_PLATFORMS
    ):
        raise BuildSpecError(
            "build-platforms-invalid",
            "declare the platforms to build: linux/amd64 and/or linux/arm64, each "
            "once (build.platforms or --platform)",
        )
    result = []
    for platform in chosen:
        try:
            facts = NodeFacts.for_platform(platform)
        except NodeFactsError as error:
            raise BuildSpecError("build-platforms-invalid", str(error)) from None
        try:
            result.append(spec.for_node(facts))
        except BuildSpecError as error:
            raise BuildSpecError("build-platforms-invalid", str(error)) from None
    return tuple(result)


@dataclass(frozen=True)
class MultiPlatformHostPlan:
    """The per-platform plans of one host build, and their combined hash."""

    name: str
    plans: tuple[HostBuildPlan, ...]

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(plan.platform for plan in self.plans)

    @property
    def plan_hash(self) -> str:
        """One platform: that plan's own hash. Several: a hash over all of them."""
        if len(self.plans) == 1:
            return self.plans[0].plan_hash
        return digest(
            canonical(
                {
                    "revision": MULTI_PLATFORM_REVISION,
                    "plans": {plan.platform: plan.plan_hash for plan in self.plans},
                }
            )
        )

    def preview(self) -> dict[str, Any]:
        if len(self.plans) == 1:
            return self.plans[0].preview()
        return {
            "revision": MULTI_PLATFORM_REVISION,
            "name": self.name,
            "plan_hash": self.plan_hash,
            "platforms": list(self.platforms),
            "builds": {plan.platform: plan.preview() for plan in self.plans},
            "executes_code": True,
            "uses_container_engine": False,
            "push": False,
        }


@dataclass(frozen=True)
class MultiPlatformHostBuild:
    """A host build for several platforms (see the module docstring)."""

    spec: HostBuildSpec
    platforms: tuple[str, ...] = ()

    def specs(self) -> tuple[HostBuildSpec, ...]:
        return platform_specs(self.spec, self.platforms or None)

    def plan(self, inputs: InputsSpec | None = None) -> MultiPlatformHostPlan:
        """Plan every platform. Reads files and hashes tools; runs nothing."""
        inputs = inputs if inputs is not None else self.spec.load_inputs()
        return MultiPlatformHostPlan(
            self.spec.name, tuple(item.plan(inputs) for item in self.specs())
        )

    def run(
        self,
        grant: HostBuildGrant,
        output_dir: Path,
        *,
        inputs: InputsSpec | None = None,
        lock: InputsLock | None = None,
        runner: Runner | None = None,
        log: Path | None = None,
        cancel: threading.Event | None = None,
        progress: Callable[[str], None] | None = None,
        raw_output: Callable[[bytes], None] | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> BuildReceipt:
        """Build every platform under ``grant``; return one combined receipt.

        One platform keeps the single-platform receipt and output layout.
        """
        inputs = inputs if inputs is not None else self.spec.load_inputs()
        plan = self.plan(inputs)
        if grant.plan_hash != plan.plan_hash:
            raise BuildSpecError("plan-not-approved", "plan hash not approved")
        if grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        options: dict[str, Any] = {
            "inputs": inputs,
            "lock": lock,
            "runner": runner,
            "log": log,
            "cancel": cancel,
            "progress": progress,
            "raw_output": raw_output,
            "client_factory": client_factory,
        }
        specs = self.specs()
        if len(specs) == 1:
            return specs[0].run(grant, output_dir, **options)
        started_at = _now()
        receipts: dict[str, dict[str, Any]] = {}
        for item, item_plan in zip(specs, plan.plans, strict=True):
            slug = platform_slug(item_plan.platform)
            # The combined approval covers this plan; `run` re-plans and
            # refuses a drift since.
            one = HostBuildGrant(item_plan.plan_hash, grant.expires_at)
            receipts[item_plan.platform] = item.run(
                one, output_dir / slug, **options
            ).to_dict()
        return BuildReceipt(
            combine_receipts(self.spec, plan, receipts, started_at, output_dir)
        )


def combine_receipts(
    spec: HostBuildSpec,
    plan: MultiPlatformHostPlan,
    receipts: Mapping[str, Mapping[str, Any]],
    started_at: str,
    output_dir: Path,
) -> dict[str, Any]:
    """One receipt for every platform; writes each image's index JSON."""
    images: dict[str, Any] = {}
    indexes: dict[str, Any] = {}
    for image in spec.images:
        entries = []
        for platform, receipt in receipts.items():
            slug = platform_slug(platform)
            entry = dict(receipt["outputs"]["images"][image.name])
            for key in ("archive", "sbom", "provenance"):
                value = entry.get(key)
                if isinstance(value, str):
                    entry[key] = f"{slug}/{value}"
                elif isinstance(value, Mapping):
                    entry[key] = {**value, "path": f"{slug}/{value['path']}"}
            images[f"{image.name}/{slug}"] = entry
            entries.append(
                IndexEntry(platform, entry["digest"], entry["manifest_size"])
            )
        body = image_index(entries)
        path = output_dir / "indexes" / f"{image.name}.index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        indexes[image.name] = {
            **index_summary(body),
            "repository": image.repository,
            "path": f"indexes/{image.name}.index.json",
        }
    first = next(iter(receipts.values()))
    return {
        "revision": BUILD_RECEIPT_REVISION,
        "state": "succeeded",
        "name": spec.name,
        "spec_sha256": spec.spec_sha256,
        "plan_hash": plan.plan_hash,
        "builder": first["builder"],
        "platforms": list(plan.platforms),
        "node_facts": {
            platform: receipt["node_facts"] for platform, receipt in receipts.items()
        },
        "base_images": first["base_images"],
        "inputs_spec_sha256": first["inputs_spec_sha256"],
        "sources": first["sources"],
        "sources_changed_during_build": _changed(receipts.values()),
        "contexts": first["contexts"],
        "network": "host",
        "source_date_epoch": spec.source_date_epoch,
        "outputs": {"files": {}, "images": images, "indexes": indexes},
        "builds": {
            platform: {
                "plan_hash": receipt["plan_hash"],
                "steps": receipt["steps"],
                "started_at": receipt["started_at"],
                "finished_at": receipt["finished_at"],
            }
            for platform, receipt in receipts.items()
        },
        "steps": [step for receipt in receipts.values() for step in receipt["steps"]],
        "started_at": started_at,
        "finished_at": _now(),
    }


def _changed(receipts: Iterable[Mapping[str, Any]]) -> list[str] | None:
    """The union of every platform's changed sources (``None``: unknown)."""
    names: set[str] = set()
    for receipt in receipts:
        value = receipt["sources_changed_during_build"]
        if value is None:
            return None
        names.update(value)
    return sorted(names)
