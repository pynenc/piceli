"""The cluster renderer stages only frozen public input into a no-token Job."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.cluster_evaluation import (
    KubernetesJobEvaluator,
    KubernetesRendererConfig,
)
from piceli.services.evaluation import EvaluationError, RenderInputs, SourceSelection
from piceli.services.render_worker import _copy_approved_source


def _renderer(tmp_path: Path) -> KubernetesJobEvaluator:
    config = KubernetesRendererConfig(
        image_id="example.invalid/piceli-renderer@sha256:" + "a" * 64,
        platform="linux/amd64",
        target=KubeconfigTarget(
            tmp_path / "explicit-kubeconfig",
            "explicit-context",
            "sandbox",
            cluster_uid="cluster-uid",
            namespace_uid="namespace-uid",
        ),
    )
    return KubernetesJobEvaluator(
        tmp_path / "evaluation-store", config, client_factory=lambda: None
    )


def test_corrupt_staging_record_does_not_prevent_renderer_start(tmp_path: Path) -> None:
    stage = tmp_path / "evaluation-store" / ("a" * 32)
    stage.mkdir(parents=True)
    stage.parent.chmod(0o700)
    (stage / "record.json").write_text("not JSON")
    evaluator = _renderer(tmp_path)
    assert evaluator.store.exists()
    assert not stage.exists()


def test_renderer_job_has_no_credential_or_egress_escape(tmp_path: Path) -> None:
    evaluator = _renderer(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("def build(context): pass\n")
    preview = evaluator.preview(
        "app",
        SourceSelection(source, ("module.py",), "module:build"),
        RenderInputs("sandbox", {}, {}, ("private-name",), {}),
    )
    stage = evaluator.store / preview.id
    hashes = json.loads((stage / "record.json").read_text())["hashes"]
    job = evaluator._job(preview, hashes)
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "piceli-renderer"
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["image"] == evaluator.kube_renderer.image_id
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert json.loads(environment["PICELI_RENDER_SOURCE_HASHES"]) == hashes
    assert environment["PICELI_RENDER_REQUEST_SHA256"] == preview.input_digest
    assert (
        job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"]
        == "renderer"
    )
    assert all(
        volume.get("configMap") or volume.get("emptyDir") for volume in pod["volumes"]
    )
    staged_map = evaluator._config_map(stage, preview)
    assert staged_map["immutable"] is True
    encoded: dict[str, str] = staged_map["binaryData"]
    assert base64.b64decode(encoded["file000"]) == b"def build(context): pass\n"
    request = base64.b64decode(encoded["request"])
    assert b"private-name" in request
    assert b"explicit-kubeconfig" not in request
    assert b"cluster-uid" not in request
    assert preview.renderer_digest == evaluator.kube_renderer.image_id


def test_worker_rejects_changed_source_before_import_and_uses_a_private_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    module = source / "module.py"
    module.write_text("def build(context): return 'approved'\n")
    hashes = {"module.py": hashlib.sha256(module.read_bytes()).hexdigest()}
    approved = tmp_path / "approved"
    _copy_approved_source(source, approved, hashes)
    module.write_text("def build(context): return 'changed'\n")
    assert (approved / "module.py").read_text().endswith("'approved'\n")
    with pytest.raises(ValueError, match="source digest mismatch"):
        _copy_approved_source(source, tmp_path / "refused", hashes)


@pytest.mark.parametrize(
    "image",
    ["example.invalid/renderer:latest", "sha256:" + "a" * 64, "image@sha256:bad"],
)
def test_renderer_refuses_unpinned_image(tmp_path: Path, image: str) -> None:
    with pytest.raises(EvaluationError, match="evaluation-config"):
        KubernetesRendererConfig(
            image,
            "linux/amd64",
            KubeconfigTarget(
                tmp_path / "config",
                "explicit",
                "sandbox",
                cluster_uid="cluster-uid",
                namespace_uid="namespace-uid",
            ),
        )


def test_preview_tampering_is_rejected_before_contacting_cluster(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    evaluator = _renderer(tmp_path)
    evaluator._client_factory = lambda: calls.append("client")  # type: ignore[assignment]
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("def build(context): pass\n")
    preview = evaluator.preview(
        "app",
        SourceSelection(source, ("module.py",), "module:build"),
        RenderInputs("sandbox", {}, {}, (), {}),
    )
    staged = evaluator.store / preview.id / "source" / "module.py"
    staged.chmod(0o600)
    staged.write_text("def build(context): return 1\n")
    with pytest.raises(EvaluationError, match="evaluation-approval"):
        evaluator.render(preview.id, preview.digest)
    assert not calls


def test_network_policy_must_select_renderer_and_deny_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluator = _renderer(tmp_path)
    from types import SimpleNamespace

    account = SimpleNamespace(metadata=SimpleNamespace(name="piceli-renderer"))
    policy = SimpleNamespace(
        metadata=SimpleNamespace(name="piceli-renderer-deny-egress"),
        spec=SimpleNamespace(
            pod_selector=SimpleNamespace(
                match_labels={"app.kubernetes.io/component": "renderer"}
            ),
            policy_types=["Egress"],
            egress=[],
        ),
    )

    class Core:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_service_account(self, _name: str, _namespace: str) -> Any:
            return account

    class Network:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_network_policy(self, _name: str, _namespace: str) -> Any:
            return policy

        def list_namespaced_network_policy(self, _namespace: str) -> Any:
            return SimpleNamespace(items=[policy])

    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.read_cluster_identity",
        lambda _api, _target: None,
    )
    monkeypatch.setattr("piceli.services.cluster_evaluation.client.CoreV1Api", Core)
    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.client.NetworkingV1Api", Network
    )
    evaluator._boundary_ready(object())
    policy.spec.egress = [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]
    with pytest.raises(EvaluationError, match="evaluation-config"):
        evaluator._boundary_ready(object())
    policy.spec.egress = []
    policy.spec.pod_selector.match_labels["piceli.evaluation"] = "other-id"
    with pytest.raises(EvaluationError, match="evaluation-config"):
        evaluator._boundary_ready(object())
    del policy.spec.pod_selector.match_labels["piceli.evaluation"]
    policy.spec.pod_selector.match_labels["app.kubernetes.io/name"] = "another-app"
    with pytest.raises(EvaluationError, match="evaluation-config"):
        evaluator._boundary_ready(object())
    del policy.spec.pod_selector.match_labels["app.kubernetes.io/name"]
    policy.spec.pod_selector.match_expressions = [
        SimpleNamespace(
            key="app.kubernetes.io/component", operator="NotIn", values=["renderer"]
        )
    ]
    with pytest.raises(EvaluationError, match="evaluation-config"):
        evaluator._boundary_ready(object())


def test_other_egress_policy_must_not_select_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    evaluator = _renderer(tmp_path)
    denied = SimpleNamespace(
        metadata=SimpleNamespace(name="piceli-renderer-deny-egress"),
        spec=SimpleNamespace(
            pod_selector=SimpleNamespace(
                match_labels={"app.kubernetes.io/component": "renderer"},
                match_expressions=[],
            ),
            policy_types=["Egress"],
            egress=[],
        ),
    )
    permitted = SimpleNamespace(
        metadata=SimpleNamespace(name="allow-all"),
        spec=SimpleNamespace(
            pod_selector=SimpleNamespace(match_labels={}, match_expressions=[]),
            policy_types=["Egress"],
            egress=[{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
        ),
    )

    class Core:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_service_account(self, _name: str, _namespace: str) -> Any:
            return SimpleNamespace(metadata=SimpleNamespace(name="piceli-renderer"))

    class Network:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_network_policy(self, _name: str, _namespace: str) -> Any:
            return denied

        def list_namespaced_network_policy(self, _namespace: str) -> Any:
            return SimpleNamespace(items=[denied, permitted])

    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.read_cluster_identity",
        lambda _api, _target: None,
    )
    monkeypatch.setattr("piceli.services.cluster_evaluation.client.CoreV1Api", Core)
    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.client.NetworkingV1Api", Network
    )
    with pytest.raises(EvaluationError, match="evaluation-config"):
        evaluator._boundary_ready(object())
    permitted.spec.pod_selector.match_labels = {"app.kubernetes.io/component": "ui"}
    evaluator._boundary_ready(object())


@pytest.mark.parametrize("pod_owner", ["job-uid", "foreign-job"])
@pytest.mark.parametrize("log_kind", ["utf8", "invalid-utf8"])
def test_approved_render_stages_job_reads_result_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pod_owner: str, log_kind: str
) -> None:
    from types import SimpleNamespace

    from kubernetes.client.exceptions import ApiException

    evaluator = _renderer(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("def build(context): pass\n")
    preview = evaluator.preview(
        "app",
        SourceSelection(source, ("module.py",), "module:build"),
        RenderInputs("sandbox", {}, {}, (), {}),
    )
    staged: dict[str, Any] = {"configmap": None, "job": None, "closed": False}
    output = {
        "components": [
            {
                "name": "settings",
                "dependencies": [],
                "resources": [
                    {
                        "manifest": {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {"name": "settings", "namespace": "sandbox"},
                            "data": {"mode": "test"},
                        },
                        "dependencies": [],
                        "bindings": [],
                    }
                ],
            }
        ],
        "pre_rollout_checks": 0,
    }

    class API:
        def close(self) -> None:
            staged["closed"] = True

    class Core:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_service_account(self, _name: str, _namespace: str) -> Any:
            return SimpleNamespace(metadata=SimpleNamespace(name="piceli-renderer"))

        def create_namespaced_config_map(self, _namespace: str, value: Any) -> None:
            staged["configmap"] = value

        def read_namespaced_config_map(self, _name: str, _namespace: str) -> Any:
            if staged["configmap"] is None:
                raise ApiException(status=404)
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    labels=staged["configmap"]["metadata"]["labels"]
                )
            )

        def delete_namespaced_config_map(self, _name: str, _namespace: str) -> None:
            staged["configmap"] = None

        def list_namespaced_pod(self, _namespace: str, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(
                            name="renderer-pod",
                            owner_references=[
                                SimpleNamespace(
                                    kind="Job", uid=pod_owner, controller=True
                                )
                            ],
                        ),
                        status=SimpleNamespace(phase="Succeeded"),
                    )
                ]
            )

        def read_namespaced_pod_log(
            self, _name: str, _namespace: str, **_kwargs: Any
        ) -> Any:
            from io import BytesIO

            assert _kwargs["_preload_content"] is False
            assert (
                _kwargs["limit_bytes"] == evaluator.kube_renderer.max_output_bytes + 1
            )
            line = "PICELI_RENDER_RESULT_V1:" + json.dumps(output)
            if log_kind == "invalid-utf8":
                return BytesIO(line.encode() + b"\xff")
            return BytesIO(line.encode())

    class Batch:
        def __init__(self, _api: Any) -> None:
            pass

        def create_namespaced_job(self, _namespace: str, value: Any) -> None:
            staged["job"] = value

        def read_namespaced_job(self, _name: str, _namespace: str) -> Any:
            if staged["job"] is None:
                raise ApiException(status=404)
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    labels=staged["job"]["metadata"]["labels"], uid="job-uid"
                ),
                status=SimpleNamespace(failed=0, succeeded=1),
            )

        def delete_namespaced_job(
            self, _name: str, _namespace: str, **_kwargs: Any
        ) -> None:
            staged["job"] = None

    class Network:
        def __init__(self, _api: Any) -> None:
            pass

        def read_namespaced_network_policy(self, _name: str, _namespace: str) -> Any:
            return SimpleNamespace(
                metadata=SimpleNamespace(name="piceli-renderer-deny-egress"),
                spec=SimpleNamespace(
                    pod_selector=SimpleNamespace(
                        match_labels={"app.kubernetes.io/component": "renderer"}
                    ),
                    policy_types=["Egress"],
                    egress=[],
                ),
            )

        def list_namespaced_network_policy(self, _namespace: str) -> Any:
            return SimpleNamespace(items=[])

    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.read_cluster_identity",
        lambda _api, _target: None,
    )
    monkeypatch.setattr("piceli.services.cluster_evaluation.client.CoreV1Api", Core)
    monkeypatch.setattr("piceli.services.cluster_evaluation.client.BatchV1Api", Batch)
    monkeypatch.setattr(
        "piceli.services.cluster_evaluation.client.NetworkingV1Api", Network
    )
    evaluator._client_factory = API
    if pod_owner == "job-uid" and log_kind != "invalid-utf8":
        rendered = evaluator.render(preview.id, preview.digest)
        assert rendered.components[0]["resources"][0]["manifest"]["data"] == {
            "mode": "test"
        }
    else:
        code = "evaluation-renderer" if pod_owner != "job-uid" else "evaluation-output"
        with pytest.raises(EvaluationError, match=code):
            evaluator.render(preview.id, preview.digest)
    assert staged == {"configmap": None, "job": None, "closed": True}
    assert not (evaluator.store / preview.id / "source").exists()
