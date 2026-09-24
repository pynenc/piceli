from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

import pytest

from piceli.app.render import placeholder_inputs
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ImageRef, NodeRef, ReleaseContext

DIGEST = "sha256:" + "0" * 64


def image(name: str, repository: str) -> ImageRef:
    return ImageRef(name=name, identity=DIGEST, repository=repository, digest=DIGEST)


def make_context(
    *,
    images: dict[str, str],
    secrets: list[str],
    values: dict[str, Any] | None = None,
    nodes: dict[str, NodeRef] | None = None,
    namespace: str = "demo",
) -> ReleaseContext:
    return ReleaseContext(
        namespace=namespace,
        images=MappingProxyType({n: image(n, r) for n, r in images.items()}),
        secrets=MappingProxyType(placeholder_inputs(secrets)),
        values=MappingProxyType(values or {}),
        nodes=MappingProxyType(nodes or {}),
    )


def canonical(composition: DeploymentComposition) -> str:
    """Everything a release reads from a composition, as canonical JSON."""
    return json.dumps(
        [
            {
                "name": component.name,
                "dependencies": list(component.dependencies),
                "resources": [
                    {
                        "ref": resource.ref.__dict__,
                        "manifest": resource.manifest,
                        "dependencies": [d.__dict__ for d in resource.dependencies],
                        "bindings": [
                            [b.json_pointer, b.reference.store_id, b.reference.version]
                            for b in resource.secret_bindings
                        ],
                    }
                    for resource in component.resources
                ],
            }
            for component in composition.components
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.fixture
def release_ctx() -> ReleaseContext:
    return make_context(
        images={"web": "docker.io/library/nginx"},
        secrets=["api-token", "web-tls.crt", "web-tls.key"],
        values={"greeting": "hi there", "replicas": 3},
        namespace="release-demo",
    )


@pytest.fixture
def shop_ctx() -> ReleaseContext:
    return make_context(
        images={
            "cache": "docker.io/library/redis",
            "tools": "docker.io/library/busybox",
            "exporter": "docker.io/oliver006/redis_exporter",
            "api": "registry.example/shop/api",
        },
        secrets=["cache-password", "cache-tls.crt", "cache-tls.key"],
        nodes={"primary": NodeRef("node-a", "uid-a")},
        namespace="shop",
    )
