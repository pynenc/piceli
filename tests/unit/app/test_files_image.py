"""``FilesImage``: an image's directory copied into the pod and mounted read-only."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from piceli import (
    App,
    Container,
    FilesImage,
    Mount,
    Resources,
    ScratchVolume,
    Security,
)
from piceli.app.render import empty_context
from piceli.k8s.release_spec import ImageRef, NodeRef, ReleaseContext
from piceli.pipeline import Build, NodeLoopbackRegistry, Pipeline, Target
from piceli.pipeline.compose import (
    composition_function,
    digest,
    pinned_images,
    resolve,
)

FILES = "registry.example/site-builds@sha256:" + "a" * 64
WEB = "registry.example/web@sha256:" + "b" * 64


def _pods(app: App) -> dict[str, dict[str, Any]]:
    composition = app.composition(empty_context("shop"))
    return {
        r.ref.name: r.manifest["spec"]["template"]["spec"]
        for c in composition.components
        for r in c.resources
        if r.ref.kind in {"Deployment", "StatefulSet"}
    }


def test_renders_a_disk_volume_an_init_copy_and_read_only_mounts() -> None:
    app = App("shop")
    # One name, one copy, even when mounted at different paths.
    builds = FilesImage(FILES, size_limit="2Gi", name="srv-builds")
    app.deployment(
        "web",
        image=WEB,
        volumes={"/srv/builds": builds},
        sidecars=[
            Container(
                name="sync", image=WEB, volumes={"/in": Mount(builds, sub_path="a")}
            )
        ],
        init=[Container(name="index", image=WEB, volumes={"/srv/builds": builds})],
    )
    pod = _pods(app)["web"]
    assert pod["volumes"] == [{"name": "srv-builds", "emptyDir": {"sizeLimit": "2Gi"}}]
    first, user = pod["initContainers"]
    assert first == {
        "name": "files-srv-builds",
        "image": FILES,
        "command": ["cp", "-a", "/data/.", "/piceli-files/"],
        "volumeMounts": [{"name": "srv-builds", "mountPath": "/piceli-files"}],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    # The workload's own init containers run after the copy, read-only.
    assert user["name"] == "index"
    mounts = [
        mount for item in (user, *pod["containers"]) for mount in item["volumeMounts"]
    ]
    assert all(mount["readOnly"] is True for mount in mounts)
    assert mounts[-1]["subPath"] == "a"


def test_security_and_resources_follow_the_workload() -> None:
    app = App("shop")
    app.deployment(
        "web",
        image=WEB,
        volumes={"/srv": FilesImage(FILES, path="/files/", name="site")},
        security=Security.restricted(user=10001, read_only_root_filesystem=False),
        resources=Resources(cpu="100m", memory="64Mi"),
    )
    app.stateful_set(
        "db",
        image=WEB,
        volumes={"/srv": FilesImage(FILES, resources=Resources(memory="16Mi"))},
    )
    pods = _pods(app)
    web = pods["web"]["initContainers"][0]
    assert web["name"] == "files-site"
    assert web["command"] == ["cp", "-a", "/files/.", "/piceli-files/"]
    # The workload's settings win field by field; the pod runs as 10001.
    assert web["securityContext"]["readOnlyRootFilesystem"] is False
    assert pods["web"]["securityContext"]["runAsUser"] == 10001
    assert web["resources"] == FilesImage.DEFAULT_RESOURCES.manifest()
    db = pods["db"]["initContainers"][0]
    assert db["resources"] == {"requests": {"memory": "16Mi"}}


def test_no_resources_when_the_pod_declares_none() -> None:
    app = App("shop")
    app.deployment("web", image=WEB, volumes={"/srv": FilesImage(FILES)})
    assert "resources" not in _pods(app)["web"]["initContainers"][0]


@pytest.mark.parametrize(
    "build",
    [
        lambda: FilesImage(FILES, path="/"),
        lambda: FilesImage(FILES, path="relative"),
        lambda: FilesImage("has space"),
        lambda: FilesImage(FILES, size_limit="big"),
    ],
)
def test_invalid_values_are_refused(build: Any) -> None:
    with pytest.raises(ValidationError):
        build()


def test_conflicts_are_refused_at_declaration() -> None:
    app = App("shop")
    other = FILES.replace("a" * 64, "c" * 64)
    with pytest.raises(ValueError, match="two different volumes"):
        app.deployment(
            "web",
            image=WEB,
            volumes={"/srv": FilesImage(FILES, name="site")},
            sidecars=[
                Container(
                    name="s", image=WEB, volumes={"/x": FilesImage(other, name="site")}
                )
            ],
        )
    with pytest.raises(ValueError, match="two different volumes"):
        app.deployment(
            "web",
            image=WEB,
            volumes={"/srv": FilesImage(FILES, name="site")},
            sidecars=[
                Container(
                    name="s", image=WEB, volumes={"/x": ScratchVolume(name="site")}
                )
            ],
        )
    with pytest.raises(ValueError, match="unique"):
        app.deployment(
            "web",
            image=WEB,
            volumes={"/srv": FilesImage(FILES, name="site")},
            init=[Container(name="files-site", image=WEB)],
        )


def _pipeline() -> Pipeline:
    app = App("shop")
    images = Build.spec("build.toml")
    app.deployment(
        "web",
        image=WEB,
        volumes={"/srv/builds": FilesImage(images["site-builds"], size_limit="2Gi")},
    )
    target = Target.kubeconfig(
        "cluster.kubeconfig",
        context="kind-shop",
        namespace="shop",
        nodes={"primary": ("shop-node", "uid-1")},
    )
    return Pipeline(app, target, build=images, deliver=NodeLoopbackRegistry())


def _resolved(pipeline: Pipeline, hexdigit: str) -> dict[str, Any]:
    ref = "sha256:" + hexdigit * 64
    image = ImageRef("site-builds", ref, "127.0.0.1:5000/shop/site-builds", None, ref)
    context = ReleaseContext(
        namespace="shop",
        images={"site-builds": image},
        secrets={},
        nodes={"primary": NodeRef("shop-node", "uid-1")},
    )
    composition = composition_function(pipeline)(context)
    return next(
        r.manifest
        for c in composition.components
        for r in c.resources
        if r.ref.kind == "Deployment"
    )


def test_a_build_handle_is_delivered_pinned_and_rolls_on_a_new_digest() -> None:
    pipeline = _pipeline()
    manifest = _resolved(pipeline, "e")
    pod = manifest["spec"]["template"]["spec"]
    copy = pod["initContainers"][0]
    assert copy["image"] == "127.0.0.1:5000/shop/site-builds@sha256:" + "e" * 64
    # A built image pins the pod to the delivery node, as for any container.
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "shop-node"}
    # The app container's pinned image is listed; the build handle is not.
    assert set(pinned_images(pipeline, {"site-builds": 1})) == {"web"}
    moved = _resolved(pipeline, "f")
    assert moved["spec"]["template"] != manifest["spec"]["template"]
    assert digest(moved) != digest(manifest)
    assert digest(_resolved(pipeline, "e")) == digest(manifest)


def test_a_mirrored_files_image_pulls_the_copy() -> None:
    app = App("shop")
    source = "docker.io/library/busybox:1.37@sha256:" + "d" * 64
    app.deployment("web", image=WEB, volumes={"/srv": FilesImage(source)})
    from piceli.artifacts.mirror import MirrorSource

    copy = "127.0.0.1:5000/mirror/busybox@sha256:" + "d" * 64
    resolved = resolve(
        app.composition(empty_context("shop")),
        images={},
        secrets={},
        pin_node="shop-node",
        mirrors={MirrorSource.parse(source).key: copy},
    )
    pod = resolved.components[0].resources[0].manifest["spec"]["template"]["spec"]
    assert pod["initContainers"][0]["image"] == copy
    assert pod["containers"][0]["image"] == WEB
