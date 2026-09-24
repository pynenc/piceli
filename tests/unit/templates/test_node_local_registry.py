"""Manifests of the node-local registry template and its garbage-collection Job."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from piceli.k8s import templates
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition
from piceli.k8s.release_spec import NodeRef, ReleaseSpec
from piceli.k8s.templates.deployable import node_local_registry as nlr

DIGEST = "sha256:" + "ab" * 32


def _by_kind(registry: templates.Deployable) -> dict[str, dict[str, Any]]:
    return {m["kind"]: m for m in registry.api_data()}


def _pod(manifest: dict[str, Any]) -> dict[str, Any]:
    return manifest["spec"]["template"]["spec"]


def _config(registry: templates.NodeLocalRegistry) -> dict[str, Any]:
    data = _by_kind(registry)["ConfigMap"]["data"]["config.yml"]
    return yaml.safe_load(data)


def test_host_network_pinned_to_node() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1")
    deployment = _by_kind(registry)["Deployment"]
    pod = _pod(deployment)
    assert pod["hostNetwork"] is True
    assert pod["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-1"}
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    # the port is declared so the scheduler reserves it on the node
    assert pod["containers"][0]["ports"] == [
        {"containerPort": 5000, "name": "registry", "protocol": "TCP"}
    ]


def test_listens_only_on_loopback() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", port=5123)
    config = _config(registry)
    assert config["http"]["addr"] == "127.0.0.1:5123"
    # the image default config opens a debug listener on :5001 (all interfaces)
    assert "debug" not in config["http"]
    container = _pod(_by_kind(registry)["Deployment"])["containers"][0]
    assert container["args"] == ["/etc/distribution/config.yml"]
    assert "env" not in container


def test_probes_target_loopback() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", port=5123)
    container = _pod(_by_kind(registry)["Deployment"])["containers"][0]
    for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
        assert container[probe]["httpGet"] == {
            "host": "127.0.0.1",
            "path": "/v2/",
            "port": 5123,
            "scheme": "HTTP",
        }


def test_delete_enabled_and_read_only_toggle() -> None:
    config = _config(templates.NodeLocalRegistry(node_name="node-1"))
    assert config["storage"]["delete"] == {"enabled": True}
    assert config["storage"]["maintenance"]["readonly"] == {"enabled": False}
    assert config["storage"]["filesystem"]["rootdirectory"] == "/var/lib/registry"
    read_only = _config(templates.NodeLocalRegistry(node_name="node-1", read_only=True))
    assert read_only["storage"]["maintenance"]["readonly"] == {"enabled": True}


def test_config_change_rolls_the_pod() -> None:
    def digest(registry: templates.NodeLocalRegistry) -> str:
        meta = _by_kind(registry)["Deployment"]["spec"]["template"]["metadata"]
        return meta["annotations"][nlr.CONFIG_DIGEST_ANNOTATION]

    base = templates.NodeLocalRegistry(node_name="node-1")
    assert digest(base) == digest(templates.NodeLocalRegistry(node_name="node-1"))
    assert digest(base) != digest(base.model_copy(update={"read_only": True}))


def test_default_image_is_digest_pinned() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1")
    image = _pod(_by_kind(registry)["Deployment"])["containers"][0]["image"]
    assert image == nlr.DEFAULT_REGISTRY_IMAGE
    assert image.startswith("docker.io/library/registry:3.1.1@sha256:")


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/library/registry:3",
        "registry@sha256:short",
        "registry@sha256:" + "AB" * 32,
    ],
)
def test_unpinned_image_rejected(image: str) -> None:
    with pytest.raises(ValidationError, match="pinned by digest"):
        templates.NodeLocalRegistry(node_name="node-1", image=image)


def test_retained_claim() -> None:
    registry = templates.NodeLocalRegistry(
        node_name="node-1", storage="20Gi", storage_class="local-path"
    )
    claim = _by_kind(registry)["PersistentVolumeClaim"]
    assert claim["metadata"]["name"] == "registry-storage"
    assert claim["metadata"]["annotations"] == {"piceli.io/retained": "true"}
    assert claim["spec"]["resources"]["requests"] == {"storage": "20Gi"}
    assert claim["spec"]["storageClassName"] == "local-path"
    volumes = _pod(_by_kind(registry)["Deployment"])["volumes"]
    assert volumes[0] == {
        "name": "storage",
        "persistentVolumeClaim": {"claimName": "registry-storage"},
    }


def test_host_path_storage_has_no_claim() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", host_path="/srv/reg")
    kinds = _by_kind(registry)
    assert "PersistentVolumeClaim" not in kinds
    pod = _pod(kinds["Deployment"])
    assert pod["volumes"][0] == {
        "name": "storage",
        "hostPath": {"path": "/srv/reg", "type": "DirectoryOrCreate"},
    }
    # fsGroup does not apply to hostPath: an init container hands the directory over
    (init,) = pod["initContainers"]
    assert init["securityContext"]["runAsUser"] == 0
    assert init["securityContext"]["capabilities"] == {
        "add": ["CHOWN"],
        "drop": ["ALL"],
    }
    assert "chown -h 65532:65532" in init["command"][-1]


def test_host_path_as_image_user_has_no_init_container() -> None:
    registry = templates.NodeLocalRegistry(
        node_name="node-1", host_path="/srv/reg", run_as_user=None
    )
    pod = _pod(_by_kind(registry)["Deployment"])
    assert "initContainers" not in pod
    assert "runAsUser" not in pod["containers"][0]["securityContext"]


@pytest.mark.parametrize("path", ["relative/dir", "/"])
def test_host_path_must_be_absolute_directory(path: str) -> None:
    with pytest.raises(ValidationError, match="absolute"):
        templates.NodeLocalRegistry(node_name="node-1", host_path=path)


def test_non_root_hardened_container() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1")
    pod = _pod(_by_kind(registry)["Deployment"])
    assert pod["securityContext"] == {
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
        "runAsGroup": 65532,
        "runAsNonRoot": True,
        "runAsUser": 65532,
    }
    assert pod["containers"][0]["resources"] == {
        "limits": {"memory": "256Mi"},
        "requests": {"cpu": "10m", "memory": "32Mi"},
    }
    assert "initContainers" not in pod


def test_stopped_scales_to_zero() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", stopped=True)
    assert _by_kind(registry)["Deployment"]["spec"]["replicas"] == 0


def test_name_leaves_room_for_suffixes() -> None:
    templates.NodeLocalRegistry(node_name="n", name="r" * 55)
    with pytest.raises(ValidationError, match="too long"):
        templates.NodeLocalRegistry(node_name="n", name="r" * 56)


def test_port_range() -> None:
    with pytest.raises(ValidationError):
        templates.NodeLocalRegistry(node_name="n", port=80)


def test_pull_reference() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", port=5001)
    assert (
        registry.pull_reference("team/app", DIGEST)
        == f"127.0.0.1:5001/team/app@{DIGEST}"
    )
    assert templates.pull_reference("app", DIGEST) == f"127.0.0.1:5000/app@{DIGEST}"
    with pytest.raises(ValueError, match="digest"):
        registry.pull_reference("app", "latest")
    with pytest.raises(ValueError, match="repository"):
        registry.pull_reference("Team/App", DIGEST)


def test_gc_job_claims_the_port_while_registry_is_writable() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", port=5002)
    (job,) = registry.garbage_collection().api_data()
    assert job["metadata"]["name"] == "registry-gc"
    assert job["spec"]["backoffLimit"] == 0
    pod = _pod(job)
    assert pod["restartPolicy"] == "Never"
    assert pod["hostNetwork"] is True
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-1"}
    container = pod["containers"][0]
    assert container["command"] == [
        "registry",
        "garbage-collect",
        "--delete-untagged",
        "/etc/distribution/config.yml",
    ]
    assert container["ports"][0]["containerPort"] == 5002
    # same storage and configuration as the registry
    deployment_pod = _pod(_by_kind(registry)["Deployment"])
    assert pod["volumes"] == deployment_pod["volumes"]
    assert container["volumeMounts"] == deployment_pod["containers"][0]["volumeMounts"]
    # the Deployment's selector must not match the GC pod
    selector = _by_kind(registry)["Deployment"]["spec"]["selector"]["matchLabels"]
    template_labels = job["spec"]["template"]["metadata"]["labels"]
    assert not selector.items() <= template_labels.items()


def test_gc_job_runs_beside_a_read_only_registry() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", read_only=True)
    gc = registry.garbage_collection(
        name="collect", delete_untagged=False, dry_run=True
    )
    (job,) = gc.api_data()
    pod = _pod(job)
    assert job["metadata"]["name"] == "collect"
    assert "hostNetwork" not in pod
    assert "ports" not in pod["containers"][0]
    assert pod["containers"][0]["command"] == [
        "registry",
        "garbage-collect",
        "--dry-run",
        "/etc/distribution/config.yml",
    ]


def test_release_component() -> None:
    registry = templates.NodeLocalRegistry(node_name="node-1", labels={"team": "ops"})
    component = registry.component("apps", dependencies=("base",))
    assert isinstance(component, DeploymentComponent)
    assert component.name == "registry"
    assert component.dependencies == ("base",)
    refs = {(r.ref.kind, r.ref.namespace, r.ref.name) for r in component.resources}
    assert refs == {
        ("ConfigMap", "apps", "registry-config"),
        ("PersistentVolumeClaim", "apps", "registry-storage"),
        ("Deployment", "apps", "registry"),
    }
    for intent in component.resources:
        assert intent.manifest["metadata"]["labels"]["team"] == "ops"
        # nothing in the manifests is redacted as sensitive by plan summaries
        assert intent.redacted_manifest() == intent.manifest
    DeploymentComposition(
        (component, DeploymentComponent("base", ())),
    )
    with pytest.raises(ValueError, match="namespace"):
        registry.resource_intents("")


def test_example_release_composition() -> None:
    root = Path(__file__).resolve().parents[3]
    spec = ReleaseSpec.from_toml(root / "examples" / "registry" / "release.toml")
    ctx = spec.context(
        spec.images(),
        {},
        {"primary": NodeRef("registry-demo-control-plane", "uid-1")},
    )
    composition = spec.load_composition()(ctx)
    components = {c.name: c for c in composition.components}
    assert components["app"].dependencies == ("registry",)
    (app,) = components["app"].resources
    pod = app.manifest["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {
        "kubernetes.io/hostname": "registry-demo-control-plane"
    }
    assert pod["containers"][0]["image"] == (
        "127.0.0.1:5000/demo/app@sha256:" + "0" * 64
    )
    kinds = sorted(r.ref.kind for r in components["registry"].resources)
    assert kinds == ["ConfigMap", "Deployment", "PersistentVolumeClaim"]
