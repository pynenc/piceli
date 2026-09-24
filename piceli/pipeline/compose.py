"""Turn a :class:`~piceli.pipeline.Pipeline` into release specs for the release engine.

The pipeline never writes a ``release.toml``: it builds the same typed
:class:`~piceli.k8s.release_spec.ReleaseSpec` in memory, with a composition
function that renders the app and then

* replaces every build handle with the delivered, immutable reference
  (``registry@sha256:…`` or a node content tag) from the delivery receipt;
* pins workloads that use a node-delivered image to that node, unless they
  choose their node themselves;
* rebinds the app's value-free secret references to the release's versions.

Every other image must already be pinned by digest. The image set recorded as
the release source is every image of the app (component → digest).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef
from piceli.k8s.release_spec import (
    ImageRef,
    NodeRef,
    ReleaseContext,
    ReleaseSpec,
    ReleaseSpecModel,
)
from piceli.pipeline.errors import PipelineError
from piceli.pipeline.model import (
    NodeImport,
    NodeLoopbackRegistry,
    handle_image,
    pinned,
)
from piceli.pipeline.secrets import placeholder

if TYPE_CHECKING:
    from piceli.pipeline.model import Pipeline

_IMAGE_KEY = re.compile(r"[a-z][a-z0-9_-]{0,62}")
HOSTNAME_LABEL = "kubernetes.io/hostname"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


# ------------------------------------------------------------ manifests


def pod_specs(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The pod specs inside a workload manifest (mutable views)."""
    spec = manifest.get("spec")
    if not isinstance(spec, dict):
        return []
    if manifest.get("kind") == "Pod":
        return [spec]
    if manifest.get("kind") == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    template = spec.get("template") if isinstance(spec, dict) else None
    pod = template.get("spec") if isinstance(template, dict) else None
    return [pod] if isinstance(pod, dict) else []


def containers(pod: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    for key in ("initContainers", "containers"):
        for item in pod.get(key) or ():
            if isinstance(item, dict):
                yield item


def _secret_inputs(pipeline: Pipeline) -> list[str]:
    from piceli.k8s.release_secrets import input_names

    return [
        item
        for name, spec in pipeline.secrets.items()
        for item in input_names(name, spec)
    ]


def declared_nodes(pipeline: Pipeline) -> dict[str, NodeRef]:
    return {
        alias: NodeRef(node.name, node.uid or "")
        for alias, node in pipeline.target.nodes.items()
    }


def preview_context(pipeline: Pipeline) -> ReleaseContext:
    """A context with value-free secret references and the declared nodes."""
    return ReleaseContext(
        namespace=pipeline.target.namespace,
        images=MappingProxyType({}),
        secrets=MappingProxyType(
            {name: placeholder(name) for name in _secret_inputs(pipeline)}
        ),
        nodes=MappingProxyType(declared_nodes(pipeline)),
    )


def render_app(pipeline: Pipeline, ctx: ReleaseContext) -> DeploymentComposition:
    try:
        return pipeline.app.composition(ctx)
    except PipelineError:
        raise
    except ValueError as error:
        raise PipelineError("render-model-invalid", str(error)) from None


def used_handles(pipeline: Pipeline) -> list[str]:
    """Build image names the app uses, in first-use order."""
    names: dict[str, None] = {}
    for resource in _resources(render_app(pipeline, preview_context(pipeline))):
        for pod in pod_specs(resource.manifest):
            for container in containers(pod):
                name = handle_image(container.get("image"))
                if name is not None:
                    names[name] = None
    return list(names)


def _resources(composition: DeploymentComposition) -> Iterator[ResourceIntent]:
    for component in composition.components:
        yield from component.resources


def model_fingerprint(pipeline: Pipeline) -> str:
    """Digest of the app as rendered before delivery (handles unresolved)."""
    from piceli.app.render import rendered
    from piceli.k8s.release_secrets import config_digest

    ctx = preview_context(pipeline)
    composition = render_app(pipeline, ctx)
    return digest(
        {
            "components": rendered(composition, ctx.secrets),
            "secrets": {
                name: config_digest(spec) for name, spec in pipeline.secrets.items()
            },
            "owner": pipeline.owner,
            "field_manager": pipeline.field_manager,
        }
    )


def pinned_images(pipeline: Pipeline, built: Mapping[str, Any]) -> dict[str, ImageRef]:
    """Digest-pinned images the app uses directly, keyed by container name.

    A reference that is neither a build handle nor ``repository@sha256:…`` is
    refused: a release never references a tag another push can move.
    """
    result: dict[str, ImageRef] = {}
    composition = render_app(pipeline, preview_context(pipeline))
    for resource in _resources(composition):
        for pod in pod_specs(resource.manifest):
            for container in containers(pod):
                image = container.get("image")
                if not isinstance(image, str) or handle_image(image) is not None:
                    continue
                if not pinned(image):
                    raise PipelineError(
                        "pipeline-image-not-pinned",
                        f"{resource.ref.kind}/{resource.ref.name} container "
                        f"{container.get('name')!r} uses {image!r}, which is not "
                        "pinned by digest; use repository@sha256:… or a build handle",
                    )
                key = _image_key(str(container.get("name", "")), resource.ref.name)
                ref = _pinned_ref(key, image)
                if key in built or (key in result and result[key].ref != image):
                    key = _image_key(f"{resource.ref.name}-{key}", resource.ref.name)
                    ref = _pinned_ref(key, image)
                result[key] = ref
    return result


def _image_key(name: str, fallback: str) -> str:
    key = re.sub(r"[^a-z0-9_-]", "-", (name or fallback).lower())[:63]
    return key if _IMAGE_KEY.fullmatch(key) else ("img-" + key)[:63]


def _pinned_ref(name: str, reference: str) -> ImageRef:
    repository, _, image_digest = reference.partition("@")
    tag = None
    slash, colon = repository.rfind("/"), repository.rfind(":")
    if colon > slash:
        repository, tag = repository[:colon], repository[colon + 1 :]
    return ImageRef(
        name,
        image_digest,
        repository,
        tag,
        image_digest,
        ref=reference,
    )


# ------------------------------------------------------------ resolution


def resolve(
    composition: DeploymentComposition,
    *,
    images: Mapping[str, ImageRef],
    secrets: Mapping[str, SecretVersionRef],
    pin_node: str | None,
) -> DeploymentComposition:
    """Delivered references for handles, node pins, and real secret versions."""
    rebind = {placeholder(name): ref for name, ref in secrets.items()}
    components = []
    for component in composition.components:
        resources = []
        for resource in component.resources:
            manifest = resource.manifest
            changed = False
            for pod in pod_specs(manifest):
                uses_handle = False
                for container in containers(pod):
                    name = handle_image(container.get("image"))
                    if name is None:
                        continue
                    if name not in images:
                        raise PipelineError(
                            "pipeline-image-unknown",
                            f"{resource.ref.kind}/{resource.ref.name} uses build "
                            f"image {name!r}, which no build of the pipeline produces",
                        )
                    container["image"] = images[name].reference
                    uses_handle = changed = True
                if (
                    uses_handle
                    and pin_node is not None
                    and not pod.get("nodeSelector")
                    and not pod.get("nodeName")
                    and not pod.get("affinity")
                ):
                    pod["nodeSelector"] = {HOSTNAME_LABEL: pin_node}
            bindings = resource.secret_bindings
            if not changed and not any(item.reference in rebind for item in bindings):
                resources.append(resource)
                continue
            rebuilt = ResourceIntent.from_manifest(manifest, resource.dependencies)
            for binding in bindings:
                rebuilt = rebuilt.with_secret(
                    binding.json_pointer,
                    rebind.get(binding.reference, binding.reference),
                )
            resources.append(rebuilt)
        components.append(
            DeploymentComponent(
                component.name, tuple(resources), component.dependencies
            )
        )
    return DeploymentComposition(tuple(components))


def pin_alias(pipeline: Pipeline) -> str | None:
    """The node alias built images are delivered to (node strategies only)."""
    strategy = pipeline.deliver
    if isinstance(strategy, NodeLoopbackRegistry | NodeImport):
        alias, _ = pipeline.target.node(strategy.node)
        return alias
    return None


def composition_function(
    pipeline: Pipeline,
) -> Callable[[ReleaseContext], DeploymentComposition]:
    alias = pin_alias(pipeline)

    def compose(ctx: ReleaseContext) -> DeploymentComposition:
        node = ctx.nodes.get(alias) if alias is not None else None
        return resolve(
            render_app(pipeline, ctx),
            images=ctx.images,
            secrets=ctx.secrets,
            pin_node=node.name if node is not None else None,
        )

    return compose


# ---------------------------------------------------------------- specs


@dataclass(frozen=True)
class PipelineReleaseSpec(ReleaseSpec):
    """A :class:`ReleaseSpec` built in memory: composition and images are given.

    ``load_composition`` returns ``function`` instead of importing a module
    and ``images`` returns the delivered references instead of reading
    receipts; everything else (state layout, target, secrets, approvals) is
    the release engine's.
    """

    function: Callable[[ReleaseContext], DeploymentComposition] | None = field(
        default=None, compare=False
    )
    resolved: Mapping[str, ImageRef] = field(default_factory=dict, compare=False)

    def images(self) -> dict[str, ImageRef]:
        return dict(sorted(self.resolved.items()))

    def load_composition(self) -> Callable[[ReleaseContext], DeploymentComposition]:
        assert self.function is not None
        return self.function


def _target_table(pipeline: Pipeline) -> dict[str, Any]:
    target = pipeline.target
    return {
        "kubeconfig": str(target.kubeconfig),
        "context": target.context,
        "namespace": target.namespace,
        "cluster_uid": target.cluster_uid,
        "namespace_uid": target.namespace_uid,
        "transport": target.transport,
        "request_seconds": target.request_seconds,
        "nodes": {
            alias: {"name": node.name, "uid": node.uid}
            for alias, node in target.nodes.items()
        },
    }


def _spec(
    pipeline: Pipeline,
    *,
    name: str,
    owner: str,
    field_manager: str,
    state_dir: Path,
    images: Mapping[str, ImageRef],
    function: Callable[[ReleaseContext], DeploymentComposition],
    secrets: Mapping[str, Any],
    adopt: tuple[str, ...] = (),
    replace: tuple[str, ...] = (),
    inherited: tuple[str, ...] = (),
) -> PipelineReleaseSpec:
    from pydantic import ValidationError

    from piceli.k8s.release_secret_spec import check_secrets

    if not images:
        raise PipelineError("pipeline-invalid", "the app declares no image")
    document = {
        "target": _target_table(pipeline),
        "release": {
            "name": name,
            "owner": owner,
            "field_manager": field_manager,
            "composition": "piceli.pipeline:compose",
            "state_dir": str(state_dir),
            "approval_window_seconds": pipeline.approval_window_seconds,
            "inherited_owners": list(inherited),
            "adopt": list(adopt),
            "replace": list(replace),
        },
        "execution": dict(pipeline.execution),
        "images": {key: {"digest": ref.identity} for key, ref in images.items()},
        "secrets": dict(secrets),
    }
    try:
        model = ReleaseSpecModel.model_validate(document)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        raise PipelineError("pipeline-invalid", problems) from None
    check_secrets(model.secrets)
    base = pipeline.base or Path.cwd()
    return PipelineReleaseSpec(
        model, base.resolve(), None, function=function, resolved=dict(images)
    )


def release_spec(
    pipeline: Pipeline, images: Mapping[str, ImageRef]
) -> PipelineReleaseSpec:
    """The app's release: delivered images plus the app's pinned images."""
    return _spec(
        pipeline,
        name=pipeline.app.name,
        owner=pipeline.owner,
        field_manager=pipeline.field_manager,
        state_dir=pipeline.state_dir / "release",
        images=images,
        function=composition_function(pipeline),
        secrets=pipeline.secrets,
        adopt=pipeline.adopt,
        replace=pipeline.replace,
        inherited=pipeline.inherited_owners,
    )


def registry_release_spec(pipeline: Pipeline) -> PipelineReleaseSpec:
    """The node-loopback registry's own release (its own name and owner)."""
    from piceli.k8s.templates.deployable.node_local_registry import (
        DEFAULT_REGISTRY_IMAGE,
        NodeLocalRegistry,
    )

    strategy = pipeline.deliver
    assert isinstance(strategy, NodeLoopbackRegistry)
    alias, _ = pipeline.target.node(strategy.node)
    image = strategy.image or DEFAULT_REGISTRY_IMAGE
    if not pinned(image):
        raise PipelineError(
            "pipeline-image-not-pinned", "the registry image must be pinned by digest"
        )

    def compose(ctx: ReleaseContext) -> DeploymentComposition:
        registry = NodeLocalRegistry(
            node_name=ctx.nodes[alias].name,
            name=strategy.name,
            port=strategy.port,
            storage=strategy.storage,
            image=ctx.image("registry"),
        )
        return DeploymentComposition((registry.component(ctx.namespace),))

    owner = f"{pipeline.owner}-registry"[:128]
    return _spec(
        pipeline,
        name=f"{pipeline.app.name[:33]}-registry",
        owner=owner,
        field_manager=owner,
        state_dir=pipeline.state_dir / "registry",
        images={"registry": _pinned_ref("registry", image)},
        function=compose,
        secrets={},
    )
