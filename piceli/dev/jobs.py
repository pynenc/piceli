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
from piceli.dev.pod import MIN_FREE_RATIO

MANAGED = {"app.kubernetes.io/managed-by": "piceli"}
RUN_LABEL = "piceli.io/dev-run"
REQUESTER_LABEL = "piceli.io/dev-requester"
PRIORITY_LABEL = "piceli.io/dev-priority"
PROFILE_LABEL = "piceli.io/dev-profile"
SCHEDULER = "piceli-dev-scheduler"
#: The Git credentials the fetch init container uses (0.19.0): kept by the
#: controller from its own, limited to the composition's source URLs.
GIT_SECRET = "piceli-dev-git"
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
    """The stdlib-only wrapper the run's pod executes (:mod:`piceli.dev.pod`),
    as a ``python3 -c`` program: compressed (0.20.0), so the one argument
    stays far below Linux's 128 KiB limit for a single argument
    (``MAX_ARG_STRLEN``) however the wrapper grows, and the Job is small.

    0.22: :mod:`piceli.dev.toolchains` travels with it, registered as the
    module ``piceli_dev_toolchains`` before the wrapper runs."""
    import base64
    import zlib

    def packed(name: str) -> str:
        raw = (Path(__file__).with_name(name)).read_bytes()
        return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")

    return (
        "import base64,sys,types,zlib\n"
        "def _unpack(blob):\n"
        " return zlib.decompress(base64.b64decode(blob))\n"
        "_m=types.ModuleType('piceli_dev_toolchains')\n"
        "sys.modules['piceli_dev_toolchains']=_m\n"
        f"exec(compile(_unpack('{packed('toolchains.py')}'),"
        "'piceli-dev-toolchains','exec'),_m.__dict__)\n"
        f"exec(compile(_unpack('{packed('pod.py')}'),'piceli-dev-wrapper','exec'))"
    )


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
                    # 0.38.0: why the node or a quota refused a cache-clear pod.
                    {
                        "apiGroups": [""],
                        "resources": ["events"],
                        "verbs": ["get", "list"],
                    },
                    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                    {
                        "apiGroups": [""],
                        "resources": ["configmaps"],
                        "resourceNames": [CONFIG_MAP],
                        "verbs": ["get"],
                    },
                    # 0.19.0: the fetch init container's Git credentials.
                    {
                        "apiGroups": [""],
                        "resources": ["secrets"],
                        "resourceNames": [GIT_SECRET],
                        "verbs": ["get", "update", "patch"],
                    },
                    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
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
        "cache_min_free_ratio": MIN_FREE_RATIO,
        "log_tail_lines": 80,
        "prefetch": profile.prefetch,
        "toolchain_probe": None
        if profile.toolchain is None
        else list(profile.toolchain),
        "tools": list(profile.tools),
        **({"toolchains": dict(profile.toolchains)} if profile.toolchains else {}),
        # 0.24: a docker host mounts its own Nix into the run.
        **({"nix": True} if profile.nix else {}),
        "artifacts": [],
        # No code from the repository runs until the API server is cut off.
        "isolation_check": True,
        "isolation_timeout_seconds": 60,
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
                    **(
                        {"initContainers": [_fetch_container(profile, dev, full)]}
                        if full.get("sources")
                        else {}
                    ),
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
                        # Mounted by the fetch init container only (never by
                        # the container that runs the repository's code).
                        *(
                            [{"name": "git", "secret": {"secretName": GIT_SECRET, "optional": True, "defaultMode": 0o440}}]
                            if full.get("sources")
                            else []
                        ),
                    ],
                },
            },
        },
    }  # fmt: skip


#: The label of a cache-clear Job (never a run: the scheduler ignores it).
CLEAR_LABEL = "piceli.io/dev-cache-clear"


#: 0.35.0: a Job that frees disk space tolerates the taint a node under
#: disk pressure carries (``piceli dev cache clear``, ``piceli gitops cache
#: clear``); every other Job keeps off such a node.
FREES_SPACE_TOLERATIONS: tuple[dict[str, str], ...] = (
    {
        "key": "node.kubernetes.io/disk-pressure",
        "operator": "Exists",
        "effect": "NoSchedule",
    },
)


#: 0.38.0: under DiskPressure the kubelet admits only critical pods; a Job
#: that frees space has this priority class. Since Kubernetes 1.17 it is
#: usable outside ``kube-system`` unless a ResourceQuota of the namespace
#: limits it (the Job's pods are then refused, with the quota's message).
FREES_SPACE_PRIORITY = "system-node-critical"


def cache_clear_job(
    dev: DevBuilds,
    *,
    name: str,
    lineage: str | None,
    crates: list[str],
    evict: bool = False,
) -> dict[str, Any]:
    """A short Job that empties free lineages' cargo targets (0.19.0).

    Same node, user and cache claim as a run; no network need, no Secret,
    no repository code: it runs the wrapper with ``PICELI_DEV_ROLE=clear``.
    ``evict`` (0.38.0): instead, evict least recently used free lineages to
    the cache's budget, as a run does before it starts (the controller's
    answer to a node under disk pressure); its result has the ``budget``.
    """
    labels = {**MANAGED, CLEAR_LABEL: name}
    spec: dict[str, Any] = {"lineage": lineage, "crates": list(crates)}
    if evict:
        spec.update(
            evict=True,
            cache_max_bytes=_cache_budget(dev),
            cache_min_free_ratio=MIN_FREE_RATIO,
        )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": f"piceli-dev-clear-{name}", "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {"kubernetes.io/hostname": dev.node},
                    # 0.35.0: it frees space, so it runs on a node under disk pressure.
                    "tolerations": [dict(item) for item in FREES_SPACE_TOLERATIONS],
                    # 0.38.0: and the kubelet admits it there.
                    "priorityClassName": FREES_SPACE_PRIORITY,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
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
                            "image": dev.image,
                            "command": ["python3", "-I", "-c", wrapper_source()],
                            "env": [
                                {"name": "PICELI_DEV_ROLE", "value": "clear"},
                                {"name": "PICELI_DEV_SPEC", "value": json.dumps(spec, sort_keys=True)},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"memory": "256Mi"},
                            },
                            "volumeMounts": [{"name": "cache", "mountPath": "/cache"}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "cache", "persistentVolumeClaim": {"claimName": CACHE_CLAIM}}
                    ],
                },
            },
        },
    }  # fmt: skip


def _fetch_container(
    profile: DevProfile, dev: DevBuilds, spec: dict[str, Any]
) -> dict[str, Any]:
    """The init container (0.19.0): fetch the sources' remotes into their
    mirrors with the cluster's Git credentials, before any repository code
    runs, and report which commits the mirrors have (``/work/have.json``)."""
    return {
        "name": "fetch",
        "image": profile.image or dev.image,
        "command": ["python3", "-I", "-c", wrapper_source()],
        "env": [
            {"name": "PICELI_DEV_ROLE", "value": "fetch"},
            {"name": "PICELI_DEV_SPEC", "value": json.dumps(spec, sort_keys=True)},
            {"name": "HOME", "value": "/work/home"},
        ],
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"memory": "1Gi"},
        },
        "volumeMounts": [
            {"name": "work", "mountPath": "/work"},
            {"name": "cache", "mountPath": "/cache"},
            {"name": "git", "mountPath": "/etc/piceli-git", "readOnly": True},
        ],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def max_lineages(dev: DevBuilds) -> int:
    """Lineages per cache key: one per slot (at least 2, so a sibling can warm)."""
    return max(2, dev.slots if isinstance(dev.slots, int) else 4)


def _cache_budget(dev: DevBuilds) -> int:
    """0.35.0: everything in the cache counts against this share of its claim."""
    from piceli.dev.model import bytes_of

    return int(bytes_of(dev.cache_size) * dev.cache_budget)
