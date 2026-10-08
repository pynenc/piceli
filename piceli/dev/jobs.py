"""The objects of development builds: what ``cluster init`` installs, a run's Job.

Runs are untrusted code from a repository. Each one is its own pod in
``piceli-dev``: non-root, no capabilities, no privilege escalation, no
service account token, no Secret, a memory limit, a bounded scratch, a
deadline, pinned to the builder node. The namespace's NetworkPolicy denies
all ingress and lets runs reach only DNS and, with ``network="fetch"``,
public addresses on ports 80 and 443 (crates, toolchains): never the API
server, cluster services or private networks.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from piceli.dev.model import (
    CACHE_CLAIM,
    CONFIG_MAP,
    NAMESPACE,
    DevBuilds,
    DevProfile,
    parse_seconds,
)

MANAGED = {"app.kubernetes.io/managed-by": "piceli"}
RUN_LABEL = "piceli.io/dev-run"
REQUESTER_LABEL = "piceli.io/dev-requester"
PRIORITY_LABEL = "piceli.io/dev-priority"
PROFILE_LABEL = "piceli.io/dev-profile"
SCHEDULER = "piceli-dev-scheduler"
#: Seconds a run waits for its upload, then for the client to take artifacts.
UPLOAD_SECONDS = 600
COLLECT_SECONDS = 300
#: Addresses runs never reach (private, link-local, carrier-grade NAT).
PRIVATE = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
)


def wrapper_source() -> str:
    """The stdlib-only wrapper the run's pod executes (:mod:`piceli.dev.pod`)."""
    return (Path(__file__).with_name("pod.py")).read_text()


def install_objects(cluster: Any) -> list[dict[str, Any]]:
    """What ``cluster init`` applies for ``Cluster(dev=...)``, in order."""
    from piceli.gitops.install import NAME
    from piceli.infra.cluster_init import NAMESPACE as SYSTEM

    dev: DevBuilds = cluster.dev
    meta = {"namespace": NAMESPACE, "labels": dict(MANAGED)}
    egress: list[dict[str, Any]] = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        }
    ]
    if dev.network == "fetch":
        egress.append(
            {
                "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE)}}],
                "ports": [
                    {"protocol": "TCP", "port": 80},
                    {"protocol": "TCP", "port": 443},
                ],
            }
        )
    claim: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": dev.cache_size}},
    }
    if cluster.storage_class:
        claim["storageClassName"] = cluster.storage_class
    objects: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NAMESPACE,
                "labels": {
                    **MANAGED,
                    "pod-security.kubernetes.io/enforce": "restricted",
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": CACHE_CLAIM, **meta},
            "spec": claim,
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "piceli-dev-runs", **meta},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "egress": egress,
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": CONFIG_MAP, **meta},
            "data": {"config.json": json.dumps(dev.describe(), sort_keys=True)},
        },
    ]
    if cluster.controller is not None:
        rbac = "rbac.authorization.k8s.io"
        objects += [
            {
                "apiVersion": f"{rbac}/v1",
                "kind": "Role",
                "metadata": {"name": SCHEDULER, **meta},
                "rules": [
                    {
                        "apiGroups": ["batch"],
                        "resources": ["jobs"],
                        "verbs": ["get", "list", "watch", "patch", "delete"],
                    },
                    {
                        "apiGroups": [""],
                        "resources": ["pods"],
                        "verbs": ["get", "list"],
                    },
                    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                    {
                        "apiGroups": [""],
                        "resources": ["configmaps"],
                        "resourceNames": [CONFIG_MAP],
                        "verbs": ["get"],
                    },
                ],
            },
            {
                "apiVersion": f"{rbac}/v1",
                "kind": "RoleBinding",
                "metadata": {"name": SCHEDULER, **meta},
                "roleRef": {"apiGroup": rbac, "kind": "Role", "name": SCHEDULER},
                "subjects": [
                    {"kind": "ServiceAccount", "name": NAME, "namespace": SYSTEM}
                ],
            },
        ]
    return objects


def run_name(run: str) -> str:
    return f"piceli-dev-{run}"


def run_job(
    dev: DevBuilds,
    profile: DevProfile,
    *,
    run: str,
    spec: dict[str, Any],
    requester: str,
    priority: str,
    suspend: bool = False,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The Job of one run (``spec``: the client's part of the wrapper's spec)."""
    timeout = parse_seconds(profile.timeout or dev.timeout)
    full = {
        "run": run,
        "cwd": ".",
        "env": dict(profile.env),
        "timeout_seconds": timeout,
        "upload_timeout_seconds": UPLOAD_SECONDS,
        "collect_timeout_seconds": COLLECT_SECONDS,
        "poll_seconds": 0.5,
        "max_lineages": max_lineages(dev),
        "cache_max_bytes": _cache_budget(dev),
        "log_tail_lines": 80,
        "prefetch": profile.prefetch,
        "toolchain_probe": None
        if profile.toolchain is None
        else list(profile.toolchain),
        "tools": list(profile.tools),
        "artifacts": [],
        **spec,
    }
    labels = {
        **MANAGED,
        RUN_LABEL: run,
        REQUESTER_LABEL: requester,
        PRIORITY_LABEL: priority,
        PROFILE_LABEL: profile.name,
    }
    resources = {
        "requests": {
            "cpu": profile.cpu or dev.run_cpu,
            "memory": profile.memory or dev.run_memory,
            "ephemeral-storage": "1Gi",
        },
        "limits": {
            "memory": profile.memory or dev.run_memory,
            "ephemeral-storage": "2Gi",
        },
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": run_name(run),
            "namespace": NAMESPACE,
            "labels": labels,
            "annotations": dict(annotations or {}),
        },
        "spec": {
            "suspend": suspend,
            "backoffLimit": 0,
            "activeDeadlineSeconds": timeout + UPLOAD_SECONDS + COLLECT_SECONDS + 900,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/hostname": dev.node},
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "terminationGracePeriodSeconds": 10,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "run",
                            "image": profile.image or dev.image,
                            "command": ["python3", "-I", "-c", wrapper_source()],
                            "env": [
                                {"name": "PICELI_DEV_SPEC", "value": json.dumps(full, sort_keys=True)},
                                {"name": "HOME", "value": "/work/home"},
                                {"name": "TMPDIR", "value": "/work/tmp"},
                            ],
                            "resources": resources,
                            "volumeMounts": [
                                {"name": "work", "mountPath": "/work"},
                                {"name": "cache", "mountPath": "/cache"},
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "work", "emptyDir": {"sizeLimit": dev.run_storage}},
                        {"name": "cache", "persistentVolumeClaim": {"claimName": CACHE_CLAIM}},
                    ],
                },
            },
        },
    }  # fmt: skip


def max_lineages(dev: DevBuilds) -> int:
    """Lineages per cache key: one per slot (at least 2, so a sibling can warm)."""
    return max(2, dev.slots if isinstance(dev.slots, int) else 4)


def _cache_budget(dev: DevBuilds) -> int:
    from piceli.dev.model import bytes_of

    # The recorded bytes of lineages; the rest of the claim is cargo's home.
    return int(bytes_of(dev.cache_size) * 0.85)
