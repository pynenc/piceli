"""Live objects as the API server returns them after ``kubectl create``."""

from __future__ import annotations

import copy
from typing import Any

NS = "shop"

_META = {
    "uid": "0b6c7e36-0000-4000-8000-000000000001",
    "resourceVersion": "4242",
    "generation": 1,
    "creationTimestamp": "2026-09-01T00:00:00Z",
    "managedFields": [
        {
            "manager": "kubectl-create",
            "operation": "Update",
            "apiVersion": "v1",
            "fieldsType": "FieldsV1",
            "fieldsV1": {"f:metadata": {}},
        }
    ],
}


def _meta(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "namespace": NS, **copy.deepcopy(_META), **extra}


def deployment() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(
            "cache",
            labels={"app": "cache"},
            annotations={"deployment.kubernetes.io/revision": "1"},
        ),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "cache"}},
            "progressDeadlineSeconds": 600,
            "revisionHistoryLimit": 10,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
            },
            "template": {
                "metadata": {"creationTimestamp": None, "labels": {"app": "cache"}},
                "spec": {
                    "containers": [
                        {
                            "name": "redis",
                            "image": "redis:7.2",
                            "imagePullPolicy": "IfNotPresent",
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "64Mi"},
                                "limits": {"memory": "128Mi", "example.com/gpu": "1"},
                            },
                            "terminationMessagePath": "/dev/termination-log",
                            "terminationMessagePolicy": "File",
                            "ports": [{"containerPort": 6379, "protocol": "TCP"}],
                            "env": [
                                {
                                    "name": "MODE",
                                    "valueFrom": {
                                        "configMapKeyRef": {
                                            "name": "settings",
                                            "key": "mode",
                                        }
                                    },
                                },
                                {
                                    "name": "PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "cache-auth",
                                            "key": "password",
                                        }
                                    },
                                },
                                {
                                    "name": "CPU",
                                    "valueFrom": {
                                        "resourceFieldRef": {
                                            "resource": "limits.cpu",
                                            "divisor": "0",
                                        }
                                    },
                                },
                                {"name": "LEVEL", "value": "info"},
                            ],
                            "readinessProbe": {
                                "tcpSocket": {"port": 6379},
                                "timeoutSeconds": 1,
                                "periodSeconds": 5,
                                "successThreshold": 1,
                                "failureThreshold": 3,
                            },
                            "securityContext": {"runAsNonRoot": True},
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {
                                    "name": "conf",
                                    "mountPath": "/etc/redis",
                                    "readOnly": True,
                                },
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        },
                        {
                            "name": "exporter",
                            "image": "example.org/exporter:1.0",
                            "imagePullPolicy": "IfNotPresent",
                            "terminationMessagePath": "/dev/termination-log",
                            "terminationMessagePolicy": "File",
                            "ports": [
                                {
                                    "name": "metrics",
                                    "containerPort": 9121,
                                    "protocol": "TCP",
                                }
                            ],
                        },
                    ],
                    "initContainers": [
                        {
                            "name": "prepare",
                            "image": "busybox:1.36",
                            "command": ["sh", "-c", "echo ready"],
                            "imagePullPolicy": "IfNotPresent",
                            "terminationMessagePath": "/dev/termination-log",
                            "terminationMessagePolicy": "File",
                        }
                    ],
                    "volumes": [
                        {"name": "tmp", "emptyDir": {}},
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "cache-data"},
                        },
                        {
                            "name": "conf",
                            "configMap": {"name": "settings", "defaultMode": 420},
                        },
                    ],
                    "dnsPolicy": "ClusterFirst",
                    "restartPolicy": "Always",
                    "schedulerName": "default-scheduler",
                    "securityContext": {},
                    "terminationGracePeriodSeconds": 30,
                    "tolerations": [{"key": "dedicated", "operator": "Exists"}],
                },
            },
        },
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def service() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta("cache", labels={"app": "cache"}),
        "spec": {
            "clusterIP": "10.96.12.34",
            "clusterIPs": ["10.96.12.34"],
            "internalTrafficPolicy": "Cluster",
            "ipFamilies": ["IPv4"],
            "ipFamilyPolicy": "SingleStack",
            "ports": [{"port": 6379, "protocol": "TCP", "targetPort": 6379}],
            "selector": {"app": "cache"},
            "sessionAffinity": "None",
            "type": "ClusterIP",
        },
        "status": {"loadBalancer": {}},
    }


def config() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta("settings"),
        "data": {"mode": "fast", "redis.conf": "maxmemory 64mb\nappendonly yes\n"},
    }


SECRET_VALUE = "c3VwZXItc2VjcmV0LXZhbHVl"  # base64 of a value that must never leak


def secret() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta("cache-auth"),
        "type": "Opaque",
        "data": {"password": SECRET_VALUE},
    }


def claim() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _meta(
            "cache-data",
            annotations={"pv.kubernetes.io/bind-completed": "yes"},
            finalizers=["kubernetes.io/pvc-protection"],
        ),
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
            "storageClassName": "standard",
            "volumeMode": "Filesystem",
            "volumeName": "pvc-1",
        },
        "status": {"phase": "Bound"},
    }


def policy() -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _meta("cache-ingress"),
        "spec": {
            "podSelector": {"matchLabels": {"app": "cache"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": {"app": "cache"}}}],
                    "ports": [{"port": 6379, "protocol": "TCP"}],
                }
            ],
        },
    }


def token_secret() -> dict[str, Any]:
    value = secret()
    value["metadata"]["name"] = "default-token"
    value["type"] = "kubernetes.io/service-account-token"
    return value


def stateful_set() -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": _meta("queue"),
        "spec": {
            "serviceName": "queue",
            "selector": {"matchLabels": {"app": "queue"}},
            "template": {
                "metadata": {"labels": {"app": "queue"}},
                "spec": {"containers": [{"name": "queue", "image": "nats:2"}]},
            },
        },
    }


def stack() -> list[dict[str, Any]]:
    return [deployment(), service(), config(), secret(), claim(), policy()]
