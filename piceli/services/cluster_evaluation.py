"""Credential-free Kubernetes Job boundary for approved source evaluation.

This evaluator shares DockerEvaluator's frozen-source staging and approval
digest. The API process holds the target credential; neither the Job nor the
ConfigMap receives it. The installation owns the renderer ServiceAccount and
an enforced deny-egress policy selected by the Job's labels.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from kubernetes import client

from piceli.k8s.ops.provider_factory import (
    KubeconfigTarget,
    api_client_from_kubeconfig,
    read_cluster_identity,
)
from piceli.services.contracts import EvaluationPreview
from piceli.services.evaluation import (
    DockerEvaluator,
    EvaluationError,
    RenderedComposition,
    RendererConfig,
    _digest,
    _read_source,
)

_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_RESULT = "PICELI_RENDER_RESULT_V1:"
_LABELS = {
    "app.kubernetes.io/name": "piceli",
    "app.kubernetes.io/component": "renderer",
}


@dataclass(frozen=True)
class KubernetesRendererConfig:
    """One prebuilt pinned image and one explicit target, with bounded input."""

    image_id: str
    platform: str
    target: KubeconfigTarget
    max_seconds: int = 60
    max_output_bytes: int = 1024 * 1024
    max_source_bytes: int = 256 * 1024
    max_files: int = 128
    service_account: str = "piceli-renderer"
    network_policy: str = "piceli-renderer-deny-egress"
    egress_probe: tuple[str, int] | None = None

    def __post_init__(self) -> None:
        if (
            not _IMAGE.fullmatch(self.image_id)
            or self.platform not in {"linux/amd64", "linux/arm64"}
            or not self.target.cluster_uid
            or not self.target.namespace_uid
            or not 1 <= self.max_seconds <= 300
            or not 1 <= self.max_output_bytes <= 8 * 1024 * 1024
            or not 1 <= self.max_source_bytes <= 256 * 1024
            or not 1 <= self.max_files <= 128
            or self.service_account != "piceli-renderer"
            or self.network_policy != "piceli-renderer-deny-egress"
            or (
                self.egress_probe is not None
                and (
                    len(self.egress_probe) != 2
                    or not self.egress_probe[0]
                    or not 1 <= self.egress_probe[1] <= 65535
                )
            )
        ):
            raise EvaluationError("evaluation-config")


class KubernetesJobEvaluator(DockerEvaluator):
    """Stage an immutable request and run it in a no-token, no-egress Job."""

    def __init__(
        self,
        store: Path,
        renderer: KubernetesRendererConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.kube_renderer = renderer
        self._client_factory = client_factory or (
            lambda: api_client_from_kubeconfig(
                renderer.target.kubeconfig,
                renderer.target.context,
                transport=renderer.target.transport,
                exec_policy=renderer.target.exec_policy,
            )
        )
        # The inherited staging code uses only the common image/limit fields.
        # Docker-specific calls are overridden below.
        super().__init__(store, cast(RendererConfig, renderer))

    def _boundary_warning(self) -> str:
        return "Approved Python executes in an isolated Kubernetes renderer Job."

    def _tool_identity(self) -> str:
        target = self.kube_renderer.target
        return _digest(
            {
                "runner": "kubernetes-job-v1",
                "cluster_uid": target.cluster_uid,
                "namespace_uid": target.namespace_uid,
                "namespace": target.namespace,
                "service_account": self.kube_renderer.service_account,
                "network_policy": self.kube_renderer.network_policy,
            }
        )

    def _client(self) -> Any:
        return self._client_factory()

    def _boundary_ready(self, api: Any) -> None:
        config = self.kube_renderer
        read_cluster_identity(api, config.target)
        core = client.CoreV1Api(api)
        network = client.NetworkingV1Api(api)
        account = core.read_namespaced_service_account(
            config.service_account, config.target.namespace
        )
        if account.metadata.name != config.service_account:
            raise EvaluationError("evaluation-config")
        policy = network.read_namespaced_network_policy(
            config.network_policy, config.target.namespace
        )
        selector = policy.spec.pod_selector.match_labels or {}
        if (
            policy.metadata.name != config.network_policy
            or selector != {"app.kubernetes.io/component": "renderer"}
            or getattr(policy.spec.pod_selector, "match_expressions", None)
            or "Egress" not in (policy.spec.policy_types or [])
            or bool(policy.spec.egress)
        ):
            raise EvaluationError("evaluation-config")
        # NetworkPolicies are additive: one permissive policy selecting these
        # Pods would undo the named deny policy. Check the current namespace
        # before staging any source. The actual CNI boundary is verified by
        # the disposable-cluster acceptance probe.
        for other in network.list_namespaced_network_policy(
            config.target.namespace
        ).items:
            if other.metadata.name == config.network_policy:
                continue
            other_spec = other.spec
            if other_spec.egress and self._policy_selects_renderer(
                other_spec.pod_selector
            ):
                raise EvaluationError("evaluation-config")

    @staticmethod
    def _policy_selects_renderer(selector: Any) -> bool:
        labels = _LABELS
        if any(
            key != "piceli.evaluation" and labels.get(key) != value
            for key, value in (selector.match_labels or {}).items()
        ):
            return False
        for expression in getattr(selector, "match_expressions", None) or []:
            if expression.key == "piceli.evaluation":
                if expression.operator == "DoesNotExist":
                    return False
                if expression.operator not in {"In", "NotIn", "Exists"}:
                    raise EvaluationError("evaluation-config")
                continue
            value = labels.get(expression.key)
            choices = expression.values or []
            if expression.operator == "In" and value not in choices:
                return False
            if expression.operator == "NotIn" and value in choices:
                return False
            if expression.operator == "Exists" and value is None:
                return False
            if expression.operator == "DoesNotExist" and value is not None:
                return False
            if expression.operator not in {"In", "NotIn", "Exists", "DoesNotExist"}:
                raise EvaluationError("evaluation-config")
        return True

    def available(self) -> bool:
        api = None
        try:
            api = self._client()
            self._boundary_ready(api)
            return True
        except Exception:
            return False
        finally:
            if api is not None:
                api.close()

    @staticmethod
    def _name(preview_id: str) -> str:
        return "piceli-render-" + preview_id

    def _labels(self, preview_id: str) -> dict[str, str]:
        return {**_LABELS, "piceli.evaluation": preview_id}

    def _config_map(self, stage: Path, preview: EvaluationPreview) -> dict[str, Any]:
        binary: dict[str, str] = {}
        for index, name in enumerate(preview.files):
            binary[f"file{index:03d}"] = base64.b64encode(
                (stage / "source" / name).read_bytes()
            ).decode("ascii")
        binary["request"] = base64.b64encode(
            (stage / "request.json").read_bytes()
        ).decode("ascii")
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self._name(preview.id),
                "namespace": self.kube_renderer.target.namespace,
                "labels": self._labels(preview.id),
            },
            "binaryData": binary,
            "immutable": True,
        }

    def _job(
        self, preview: EvaluationPreview, source_hashes: dict[str, str]
    ) -> dict[str, Any]:
        config = self.kube_renderer
        name = self._name(preview.id)
        labels = self._labels(preview.id)
        arch = config.platform.split("/", 1)[1]
        source_items = [
            {"key": f"file{index:03d}", "path": filename, "mode": 0o444}
            for index, filename in enumerate(preview.files)
        ]
        result: dict[str, Any] = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": name,
                "namespace": config.target.namespace,
                "labels": labels,
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": config.max_seconds,
                "ttlSecondsAfterFinished": 300,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": config.service_account,
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "restartPolicy": "Never",
                        "nodeSelector": {
                            "kubernetes.io/os": "linux",
                            "kubernetes.io/arch": arch,
                        },
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "runAsGroup": 65532,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containers": [
                            {
                                "name": "renderer",
                                "image": config.image_id,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "-I",
                                    "-m",
                                    "piceli.services.render_worker",
                                    "/request/request.json",
                                ],
                                "env": [
                                    {"name": "HOME", "value": "/tmp"},
                                    {
                                        "name": "PYTHONDONTWRITEBYTECODE",
                                        "value": "1",
                                    },
                                    {
                                        "name": "PICELI_RENDER_RESULT_LINE",
                                        "value": "1",
                                    },
                                    {
                                        "name": "PICELI_RENDER_SOURCE_HASHES",
                                        "value": json.dumps(
                                            source_hashes, sort_keys=True
                                        ),
                                    },
                                    {
                                        "name": "PICELI_RENDER_REQUEST_SHA256",
                                        "value": preview.input_digest,
                                    },
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {
                                        "cpu": "100m",
                                        "memory": "128Mi",
                                        "ephemeral-storage": "16Mi",
                                    },
                                    "limits": {
                                        "cpu": "1",
                                        "memory": "256Mi",
                                        "ephemeral-storage": "32Mi",
                                    },
                                },
                                "volumeMounts": [
                                    {
                                        "name": "source",
                                        "mountPath": "/source",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "request",
                                        "mountPath": "/request",
                                        "readOnly": True,
                                    },
                                    {"name": "scratch", "mountPath": "/tmp"},
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "source",
                                "configMap": {
                                    "name": name,
                                    "items": source_items,
                                },
                            },
                            {
                                "name": "request",
                                "configMap": {
                                    "name": name,
                                    "items": [
                                        {
                                            "key": "request",
                                            "path": "request.json",
                                            "mode": 0o444,
                                        }
                                    ],
                                },
                            },
                            {
                                "name": "scratch",
                                "emptyDir": {
                                    "medium": "Memory",
                                    "sizeLimit": "64Mi",
                                },
                            },
                        ],
                    },
                },
            },
        }
        if config.egress_probe is not None:
            result["spec"]["template"]["spec"]["containers"][0]["env"].append(
                {
                    "name": "PICELI_RENDER_EGRESS_PROBE",
                    "value": json.dumps(config.egress_probe),
                }
            )
        return result

    def _cleanup(self, api: Any, preview_id: str) -> None:
        name = self._name(preview_id)
        namespace = self.kube_renderer.target.namespace
        batch = client.BatchV1Api(api)
        core = client.CoreV1Api(api)
        # Named resources are owned by this evaluator's unguessable ID. A
        # mismatch must not cause deletion of an unrelated object. Wait for
        # foreground deletion before discarding source or staging data.
        try:
            job = batch.read_namespaced_job(name, namespace)
        except client.exceptions.ApiException as error:
            if error.status != 404:
                raise
        else:
            if (job.metadata.labels or {}).get("piceli.evaluation") != preview_id:
                raise EvaluationError("evaluation-interrupted")
            batch.delete_namespaced_job(
                name, namespace, propagation_policy="Foreground"
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    batch.read_namespaced_job(name, namespace)
                except client.exceptions.ApiException as error:
                    if error.status == 404:
                        break
                    raise
                time.sleep(0.2)
            else:
                raise EvaluationError("evaluation-interrupted")
        try:
            staged = core.read_namespaced_config_map(name, namespace)
        except client.exceptions.ApiException as error:
            if error.status == 404:
                return
            raise
        if (staged.metadata.labels or {}).get("piceli.evaluation") != preview_id:
            raise EvaluationError("evaluation-interrupted")
        core.delete_namespaced_config_map(name, namespace)

    def recover_interrupted(self) -> None:
        for stage in self.store.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", stage.name) or not stage.is_dir():
                continue
            try:
                record = json.loads((stage / "record.json").read_bytes())
            except (OSError, ValueError):
                continue
            if record.get("state") != "running":
                continue
            api = self._client()
            try:
                self._cleanup(api, stage.name)
            finally:
                api.close()
            record["state"] = "interrupted"
            self._save(stage, record)
            self._discard_source(stage)

    def _run_job(
        self,
        api: Any,
        stage: Path,
        preview: EvaluationPreview,
        cancel: threading.Event | None,
    ) -> dict[str, Any]:
        namespace = self.kube_renderer.target.namespace
        name = self._name(preview.id)
        core = client.CoreV1Api(api)
        batch = client.BatchV1Api(api)
        core.create_namespaced_config_map(namespace, self._config_map(stage, preview))
        record = json.loads((stage / "record.json").read_bytes())
        batch.create_namespaced_job(namespace, self._job(preview, record["hashes"]))
        deadline = time.monotonic() + self.kube_renderer.max_seconds + 10
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise EvaluationError("evaluation-interrupted")
            job = batch.read_namespaced_job(name, namespace)
            if job.status.failed:
                raise EvaluationError("evaluation-renderer")
            if job.status.succeeded:
                break
            time.sleep(0.2)
        else:
            raise EvaluationError("evaluation-renderer")
        pods = core.list_namespaced_pod(
            namespace, label_selector=f"batch.kubernetes.io/job-name={name}", limit=2
        ).items
        if not pods:
            pods = core.list_namespaced_pod(
                namespace, label_selector=f"job-name={name}", limit=2
            ).items
        if (
            len(pods) != 1
            or pods[0].status.phase != "Succeeded"
            or not job.metadata.uid
            or not any(
                owner.kind == "Job"
                and owner.uid == job.metadata.uid
                and owner.controller is True
                for owner in (pods[0].metadata.owner_references or [])
            )
        ):
            raise EvaluationError("evaluation-renderer")
        response = core.read_namespaced_pod_log(
            pods[0].metadata.name,
            namespace,
            container="renderer",
            limit_bytes=self.kube_renderer.max_output_bytes + 1,
            _preload_content=False,
        )
        try:
            raw = response.read(self.kube_renderer.max_output_bytes + 1)
        finally:
            response.close()
            if hasattr(response, "release_conn"):
                response.release_conn()
        if not isinstance(raw, bytes) or len(raw) > self.kube_renderer.max_output_bytes:
            raise EvaluationError("evaluation-output")
        try:
            output = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise EvaluationError("evaluation-output") from None
        lines = [line for line in output.splitlines() if line.startswith(_RESULT)]
        if len(lines) != 1:
            raise EvaluationError("evaluation-output")
        try:
            result = json.loads(lines[0][len(_RESULT) :])
        except ValueError:
            raise EvaluationError("evaluation-output") from None
        if not isinstance(result, dict):
            raise EvaluationError("evaluation-output")
        return result

    def _render(
        self,
        preview_id: str,
        approved_digest: str,
        cancel: threading.Event | None,
    ) -> RenderedComposition:
        stage = self.store / preview_id
        record = json.loads((stage / "record.json").read_bytes())
        preview = EvaluationPreview.model_validate(record["preview"])
        material = record["material"]
        config = self.kube_renderer
        if (
            preview.digest != approved_digest
            or _digest(material) != approved_digest
            or preview.model_dump(exclude={"digest"})
            != {
                key: value
                for key, value in material.items()
                if key not in {"platform", "tool"}
            }
            or preview.renderer_digest != config.image_id
            or material.get("platform") != config.platform
            or material.get("tool") != self._tool_identity()
            or record["state"] != "ready"
            or datetime.fromisoformat(preview.expires_at) <= datetime.now(UTC)
            or preview.limits
            != {
                "seconds": config.max_seconds,
                "output_bytes": config.max_output_bytes,
                "source_bytes": config.max_source_bytes,
                "files": config.max_files,
            }
        ):
            raise EvaluationError("evaluation-approval")
        actual_files = {
            str(path.relative_to(stage / "source"))
            for path in (stage / "source").rglob("*")
            if not path.is_dir()
        }
        if actual_files != set(preview.files) or any(
            path.is_symlink() for path in stage.rglob("*")
        ):
            raise EvaluationError("evaluation-approval")
        hashes = {
            name: hashlib.sha256(
                _read_source(stage / "source", name, config.max_source_bytes)
            ).hexdigest()
            for name in preview.files
        }
        request = json.loads((stage / "request.json").read_bytes())
        if (
            hashes != record["hashes"]
            or _digest(hashes) != preview.source.revision
            or _digest(request) != preview.input_digest
        ):
            raise EvaluationError("evaluation-approval")
        api = self._client()
        try:
            self._boundary_ready(api)
            record["state"] = "running"
            self._save(stage, record)
            result = self._run_job(api, stage, preview, cancel)
            checks = result.pop("pre_rollout_checks", 0)
            pre_rollouts = result.pop("pre_rollouts", [])
            if (
                set(result) != {"components"}
                or type(checks) is not int
                or checks < 0
                or not isinstance(pre_rollouts, list)
            ):
                raise EvaluationError("evaluation-output")
            if checks:
                raise EvaluationError("ui-prerollout-unsupported")
            rendered = RenderedComposition.from_dict(
                {
                    "preview_id": preview_id,
                    "digest": preview.digest,
                    "source": preview.source.model_dump(),
                    "inputs": request["inputs"],
                    "components": result["components"],
                    **({"pre_rollouts": pre_rollouts} if pre_rollouts else {}),
                }
            )
            record["state"] = "succeeded"
            return rendered
        finally:
            try:
                self._cleanup(api, preview_id)
                if record["state"] != "succeeded":
                    record["state"] = "failed"
                self._save(stage, record)
                self._discard_source(stage)
            finally:
                api.close()
