"""Acceptance: the node-loopback registry mirrors third-party images and takes
over a live registry (``NodeLoopbackRegistry(mirror=…, adopt=…, replace=…)``).

The registry release and the app release run through the real release
engine against the in-process fake API (which serves the registry node).
Builds are faked; mirrors are real OCI copies between two in-process
registries (the "public" source and the node registry), so no network or
cluster is needed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.pipeline.backend import Backend
from tests.acceptance.fake_api import TARGET, serve
from tests.acceptance.test_deploy_pipeline import BUILD_TOML, FakeBackend, deploy
from tests.oci_registry import OciRegistry, oci_registry

NODE = "node-1"
OLD = "old-registry"
OLD_PATH = "/var/lib/old-registry"


class RegistryBackend(FakeBackend):
    """Fake builds; the node registry is an in-process OCI registry."""

    node_registry: OciRegistry | None = None

    def _local(self, route: Any) -> Any:
        assert self.node_registry is not None
        return dataclasses.replace(
            route, push=self.node_registry.host, forward=None, tls=False
        )

    def registry_present(self, route, manifests):  # type: ignore[no-untyped-def]
        self.calls.append("head")
        assert self.node_registry is not None
        return [
            digest in self.registry.get(repo, set())
            or (repo, digest) in self.node_registry.manifests
            for repo, digest in manifests
        ]

    def registry_deliver(self, route, image_id, repository):  # type: ignore[no-untyped-def]
        self.calls.append("deliver")
        manifest = "sha256:" + hashlib.sha256(image_id.encode()).hexdigest()
        self.registry.setdefault(repository, set()).add(manifest)
        return {
            "schema": "piceli.registry-delivery.v1",
            "result": "pushed",
            "state": "succeeded",
            "approved_digest": image_id,
            "image": {"config_digest": image_id, "manifest_digest": manifest},
            "target": {"registry": None, "repository": repository},
            "node_registry": route.node_registry,
            "pull_ref": f"{route.node_registry}/{repository}@{manifest}",
        }

    def mirror_deliver(self, route, reference, repository, *, platform, credentials):  # type: ignore[no-untyped-def]
        self.calls.append("mirror")
        return Backend.mirror_deliver(
            self,
            self._local(route),
            reference,
            repository,
            platform=platform,
            credentials=credentials,
        )


def _module(
    tmp_path: Path,
    *,
    cache: str,
    registry: str,
    build: bool = True,
) -> None:
    # A rewritten module must be imported again by the next command.
    # (and a same-size rewrite in the same second must not reuse its .pyc).
    for name in [key for key in sys.modules if key.startswith("_piceli_render_")]:
        del sys.modules[name]
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)
    web = 'app.deployment("web", image=images["web"], ports=[8080])' if build else ""
    built = "build=images," if build else ""
    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            f"""
            from piceli import App, Build, NodeLoopbackRegistry, Pipeline, Registry, Target

            target = Target.kubeconfig(
                "kubeconfig", context="fake", namespace="{TARGET.namespace}",
                transport="loopback-http", nodes={{"primary": "{NODE}"}},
            )
            app = App("shop")
            images = Build.spec("build.toml")
            app.deployment("cache", image="{cache}")
            {web}
            pipeline = Pipeline(
                app, target, {built}
                deliver={registry if registry.startswith("Registry(") else f"NodeLoopbackRegistry({registry})"},
                state_dir="state",
                execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
            )
            """
        )
    )


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    FakeBackend.reset()
    monkeypatch.setattr("piceli.pipeline.runner.Backend", RegistryBackend)
    with (
        serve() as (api, url),
        oci_registry(auth="bearer") as source,
        oci_registry() as node,
    ):
        api.add_node(NODE, architecture="arm64")
        RegistryBackend.node_registry = node
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                users: [{{name: nobody, user: {{}}}}]
                contexts:
                - {{name: fake, context: {{cluster: fake, user: nobody}}}}
                """
            )
        )
        (tmp_path / "build.toml").write_text(BUILD_TOML)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.txt").write_text("v1\n")
        yield api, source, node, tmp_path
        RegistryBackend.node_registry = None


def _pod(api: Any, name: str) -> dict[str, Any]:
    return api.objects[("Deployment", name)]["spec"]["template"]["spec"]


# ------------------------------------------------------------------- item 14


def test_mirrored_image_is_copied_by_digest_and_the_release_pulls_the_copy(
    cluster,
) -> None:
    api, source, node, tmp_path = cluster
    index, children = source.add_index("library/cache")
    # Like the registry's validation config that the plan below declares.
    node.index_platforms = {("linux", "arm64")}
    # The app writes the image with a tag; the mirror list without one.
    app_ref = f"{source.host}/library/cache:7.2@{index}"
    mirror = f"{source.host}/library/cache@{index}"
    _module(tmp_path, cache=app_ref, registry=f"port=5000, mirror=[{mirror!r}]")

    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    planned = events[-1]
    deliver = planned["stages"]["deliver"]
    key = f"{source.host}/library/cache@{index}"
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert deliver["mirrors"] == {
        key: {
            "action": "mirror",
            "repository": repository,
            "reference": f"127.0.0.1:5000/{repository}@{index}",
            "platform": "linux/arm64",
            "used": True,
        }
    }
    assert deliver["registry"]["index_platforms"] == ["linux/arm64"]
    assert "mirror" in result.stderr  # the human plan lists the mirror
    assert not node.mutations() and FakeBackend.calls == []

    code, events, result = deploy(
        tmp_path, "--approve", planned["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    # The index keeps its digest; only the node's platform was copied.
    assert (repository, index) in node.manifests
    assert (repository, children["arm64"]) in node.manifests
    assert (repository, children["amd64"]) not in node.manifests
    pod = _pod(api, "cache")
    assert pod["containers"][0]["image"] == f"127.0.0.1:5000/{repository}@{index}"
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": NODE}
    # The registry accepts partial indexes for the node platform only.
    config = yaml.safe_load(
        api.objects[("ConfigMap", "registry-config")]["data"]["config.yml"]
    )
    assert config["validation"]["manifests"]["indexes"] == {
        "platforms": "list",
        "platformlist": [{"os": "linux", "architecture": "arm64"}],
    }
    receipts = list((tmp_path / "state" / "mirrors").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["schema"] == "piceli.mirror-delivery.v1"
    assert (receipt["state"], receipt["result"]) == ("succeeded", "mirrored")
    assert receipt["source"]["digest"] == index
    catalog = json.loads((tmp_path / "state" / "release" / "catalog.json").read_text())
    assert catalog["releases"][0]["source"]["images"]["cache"] == index
    assert FakeBackend.calls.count("mirror") == 1

    # Unchanged rerun: present by content, nothing copied again.
    before = len(node.mutations())
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["deliver"] == "skipped"
    assert events[-1]["stages"]["apply"] == "skipped"
    assert len(node.mutations()) == before
    assert FakeBackend.calls.count("mirror") == 1

    # The mirror list is part of the combined hash.
    code, events, _ = deploy(tmp_path, "--plan", "--json")
    first = events[-1]["combined_hash"]
    other, _ = source.add_image("library/other")
    _module(
        tmp_path,
        cache=app_ref,
        registry=f"port=5000, mirror=[{mirror!r}, '{source.host}/library/other@{other}']",
    )
    code, events, _ = deploy(tmp_path, "--plan", "--json")
    assert code == 0
    assert events[-1]["combined_hash"] != first
    assert (
        events[-1]["stages"]["deliver"]["mirrors"][
            f"{source.host}/library/other@{other}"
        ]["used"]
        is False
    )


def test_mirror_without_a_build_and_tags_are_refused(cluster) -> None:
    api, source, node, tmp_path = cluster
    digest, _ = source.add_image("library/cache")
    reference = f"{source.host}/library/cache@{digest}"
    _module(tmp_path, cache=reference, registry=f"mirror=[{reference!r}]", build=False)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    stages = events[-1]["stages"]
    assert (stages["build"], stages["deliver"]) == ("skipped", "done")
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert _pod(api, "cache")["containers"][0]["image"] == (
        f"127.0.0.1:5000/{repository}@{digest}"
    )
    # The release commands plan with the mirror while its copy is recorded.
    spec = str(tmp_path / "app.py:pipeline")
    planned = CliRunner().invoke(cli, ["release", "plan", "--spec", spec])
    assert planned.exit_code == 0, planned.stdout + planned.stderr
    for receipt in (tmp_path / "state" / "mirrors").glob("*.json"):
        receipt.unlink()
    refused = CliRunner().invoke(cli, ["release", "plan", "--spec", spec])
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "pipeline-not-delivered"
    assert "mirror " in refused.stderr

    _module(
        tmp_path,
        cache=reference,
        registry=f"mirror=['{source.host}/library/cache:7']",
        build=False,
    )
    code, events, _ = deploy(tmp_path, "--plan", "--json")
    assert code == 2
    assert events[-1]["reason"] == "pipeline-mirror-not-pinned"


def test_failed_mirror_is_a_registered_code_and_resumes(cluster) -> None:
    api, source, node, tmp_path = cluster
    index, _ = source.add_index("library/cache", arches=("amd64",))
    reference = f"{source.host}/library/cache@{index}"
    _module(tmp_path, cache=reference, registry=f"mirror=[{reference!r}]")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert (events[-1]["stage"], events[-1]["reason"]) == (
        "deliver",
        "mirror-platform-unavailable",
    )
    assert ("Deployment", "cache") not in api.objects

    # A transient source failure: fix it and resume the same run.
    digest, _ = source.add_image("library/other")
    reference = f"{source.host}/library/other@{digest}"
    _module(tmp_path, cache=reference, registry=f"mirror=[{reference!r}]")
    source.anonymous_token = False
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["reason"] == "registry-unauthorized"
    run_id = events[-1]["run_id"]
    source.anonymous_token = True
    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["run_id"] == run_id and events[-1]["state"] == "ready"
    assert _pod(api, "cache")["containers"][0]["image"].startswith("127.0.0.1:5000/")


def test_registry_strategy_mirrors_every_platform_under_its_prefix(cluster) -> None:
    api, source, node, tmp_path = cluster
    index, children = source.add_index("library/cache")
    reference = f"{source.host}/library/cache@{index}"
    _module(
        tmp_path,
        cache=reference,
        registry=(
            "Registry('oci://registry.example:5000/team', "
            f"node_registry='10.0.0.5:5000', mirror=[{reference!r}])"
        ),
    )
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    repository = f"team/mirror/127.0.0.1-{source.port}/library/cache"
    assert all((repository, child) in node.manifests for child in children.values())
    pod = _pod(api, "cache")
    assert pod["containers"][0]["image"] == f"10.0.0.5:5000/{repository}@{index}"
    assert "nodeSelector" not in pod  # a shared registry needs no node pin
    assert ("Deployment", "registry") not in api.objects


# ------------------------------------------------------------------- item 15


def _old_registry(api: Any, *, storage: dict[str, Any] | None = None) -> None:
    """A registry created with kubectl: hostNetwork, port 5000, hostPath data."""
    api.field_ownership = True  # takeovers transfer field managers
    value = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": OLD, "namespace": TARGET.namespace},
        "spec": {
            "replicas": 1,
            # What the API server defaults for a Deployment created without a
            # strategy; Kubernetes refuses to switch it to Recreate in place.
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
            },
            "selector": {"matchLabels": {"app": OLD}},
            "template": {
                "metadata": {"labels": {"app": OLD}},
                "spec": {
                    "hostNetwork": True,
                    "nodeSelector": {"kubernetes.io/hostname": NODE},
                    "containers": [
                        {
                            "name": "registry",
                            "image": "docker.io/library/registry:3",
                            "env": [
                                {
                                    "name": "REGISTRY_HTTP_ADDR",
                                    "value": "127.0.0.1:5000",
                                }
                            ],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/var/lib/registry"}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            **(storage or {"hostPath": {"path": OLD_PATH}}),
                        }
                    ],
                },
            },
        },
    }
    api.put(value, managers=[("kubectl-client-side-apply", "Update", value)])


def test_live_registry_on_the_port_needs_an_explicit_takeover(cluster) -> None:
    api, source, node, tmp_path = cluster
    _old_registry(api)
    digest, _ = source.add_image("library/cache")
    _module(
        tmp_path, cache=f"{source.host}/library/cache@{digest}", registry="port=5000"
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-registry-takeover-required"
    assert f"adopt='{OLD}'" in result.stderr
    assert not [r for r in api.requests if r["method"] != "GET"]

    # Adopting needs the declared storage to be the live one.
    _module(
        tmp_path,
        cache=f"{source.host}/library/cache@{digest}",
        registry=f"port=5000, adopt={OLD!r}",
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-registry-incompatible"
    assert OLD_PATH in result.stderr and f"replace='{OLD}'" in result.stderr
    assert not [r for r in api.requests if r["method"] != "GET"]


def test_adopt_takes_over_a_live_registry_and_keeps_its_data(cluster) -> None:
    api, source, node, tmp_path = cluster
    _old_registry(api)
    digest, _ = source.add_image("library/cache")
    _module(
        tmp_path,
        cache=f"{source.host}/library/cache@{digest}",
        registry=f"port=5000, adopt={OLD!r}, host_path={OLD_PATH!r}",
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    registry = events[-1]["stages"]["deliver"]["registry"]
    assert registry["existing"]["action"] == "adopt"
    assert registry["existing"]["data"] == "kept"
    assert registry["existing"]["storage"] == {"host_path": OLD_PATH}
    changes = {(c["operation"], c["kind"], c["name"]) for c in registry["changes"]}
    assert changes == {
        ("adopt", "Deployment", OLD),
        ("create", "ConfigMap", f"{OLD}-config"),
    }
    assert f"adopt Deployment/{OLD}" in result.stderr
    uid = api.objects[("Deployment", OLD)]["metadata"]["uid"]

    code, events, result = deploy(
        tmp_path, "--approve", events[-1]["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    live = api.objects[("Deployment", OLD)]
    assert live["metadata"]["uid"] == uid  # never deleted
    assert live["metadata"]["annotations"]["piceli.io/owner"] == "shop-registry"
    assert live["spec"]["selector"] == {"matchLabels": {"app": OLD}}
    # One pod at a time, like Recreate, without the forbidden strategy switch.
    assert live["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 0, "maxUnavailable": 1},
    }
    pod = live["spec"]["template"]["spec"]
    assert pod["volumes"][0]["hostPath"]["path"] == OLD_PATH
    assert pod["hostNetwork"] is True
    assert not [r for r in api.requests if r["method"] == "DELETE"]
    assert ("PersistentVolumeClaim", f"{OLD}-storage") not in api.objects

    # The standing adopt= keeps working: the registry is now the release's own.
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    registry = events[-1]["stages"]["deliver"]["registry"]
    assert registry["existing"]["action"] == "managed"
    assert registry["action"] == "unchanged"


def test_replace_recreates_the_deployment_but_never_its_data(cluster) -> None:
    api, source, node, tmp_path = cluster
    _old_registry(api)
    digest, _ = source.add_image("library/cache")
    _module(
        tmp_path,
        cache=f"{source.host}/library/cache@{digest}",
        registry=f"port=5000, replace={OLD!r}, host_path={OLD_PATH!r}",
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    registry = events[-1]["stages"]["deliver"]["registry"]
    assert (registry["existing"]["action"], registry["existing"]["data"]) == (
        "replace",
        "kept",
    )
    changes = {(c["operation"], c["kind"], c["name"]) for c in registry["changes"]}
    assert ("replace", "Deployment", OLD) in changes
    code, events, result = deploy(
        tmp_path, "--approve", events[-1]["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    deleted = [r["path"] for r in api.requests if r["method"] == "DELETE"]
    assert deleted and all(path.endswith(f"/deployments/{OLD}") for path in deleted)
    live = api.objects[("Deployment", OLD)]
    assert live["spec"]["selector"]["matchLabels"]["app.kubernetes.io/instance"] == OLD
    assert (
        live["spec"]["template"]["spec"]["volumes"][0]["hostPath"]["path"] == OLD_PATH
    )
    backups = list((tmp_path / "state").rglob("*backup*"))
    assert backups, "a replace writes a backup first"

    # Replace of an object the release now manages is not repeated.
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    assert (
        events[-1]["stages"]["deliver"]["registry"]["existing"]["action"] == "managed"
    )
