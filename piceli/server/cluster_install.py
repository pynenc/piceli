"""Pure Kubernetes manifest renderer for one namespaced Piceli UI installation.

Rendering does not read kubeconfig, contact a cluster, or write a file. An
operator reviews and applies the returned YAML using an explicit context.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import yaml

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_SIZE = re.compile(r"[1-9][0-9]*(?:Mi|Gi|Ti)")
_LABEL = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?")
_RBAC_RESOURCE = re.compile(r"[a-z][a-z0-9.-]*")
_RBAC_GROUP = re.compile(r"(?:[a-z0-9-]+\.)*[a-z0-9-]+")
_WRITE_VERBS = frozenset({"create", "update", "patch", "delete"})
_VERBS = frozenset({"get", "list", "watch", *_WRITE_VERBS})
_CLUSTER_RESOURCES = frozenset(
    {
        "namespaces",
        "nodes",
        "persistentvolumes",
        "clusterroles",
        "clusterrolebindings",
        "customresourcedefinitions",
        "storageclasses",
        "mutatingwebhookconfigurations",
        "validatingwebhookconfigurations",
    }
)

_MATERIALIZE_SOURCE = """\
import hashlib
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
verified = []
for name, expected in json.loads(sys.argv[3]):
    content = (source / name).read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError('release source does not match the reviewed manifest')
    verified.append((name, content))
for name, content in verified:
    destination = target / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    destination.write_bytes(content)
    destination.chmod(0o444)
"""


def _source_path(value: str) -> None:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
            for part in value.split("/")
        )
        or value == "release.toml"
        or len(value) > 240
    ):
        raise ValueError("source allowlist paths must be safe relative files")


@dataclass(frozen=True)
class DeployResourceRule:
    """One operator-reviewed namespaced Kubernetes RBAC resource."""

    api_group: str
    resource: str
    verbs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "verbs", tuple(self.verbs))
        if (
            (self.api_group and not _RBAC_GROUP.fullmatch(self.api_group))
            or not _RBAC_RESOURCE.fullmatch(self.resource)
            or self.resource in _CLUSTER_RESOURCES
            or "/" in self.resource
            or not self.verbs
            or not set(self.verbs) <= _VERBS
            or not set(self.verbs) & _WRITE_VERBS
        ):
            raise ValueError(
                "manual resource rule must name one namespaced write resource"
            )


@dataclass(frozen=True)
class ManualDeliveryConfig:
    """Explicit source, renderer and per-resource rights for manual delivery."""

    release_definition_toml: str
    source_files: Mapping[str, str]
    source_file_allowlist: tuple[str, ...]
    renderer_image: str
    renderer_platform: str
    authorized_deploy_subjects: tuple[str, ...]
    deploy_resources: tuple[DeployResourceRule, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_files", MappingProxyType(dict(self.source_files))
        )
        object.__setattr__(
            self, "source_file_allowlist", tuple(self.source_file_allowlist)
        )
        object.__setattr__(
            self, "authorized_deploy_subjects", tuple(self.authorized_deploy_subjects)
        )
        object.__setattr__(self, "deploy_resources", tuple(self.deploy_resources))
        _image(self.renderer_image)
        if self.renderer_platform not in {"linux/amd64", "linux/arm64"}:
            raise ValueError("renderer platform must be linux/amd64 or linux/arm64")
        if (
            not self.authorized_deploy_subjects
            or not self.deploy_resources
            or not self.source_files
            or len(self.source_files) > 128
            or set(self.source_file_allowlist) != set(self.source_files)
            or len(self.source_file_allowlist) != len(self.source_files)
        ):
            raise ValueError(
                "manual delivery requires subjects, source allowlist and resource rules"
            )
        if any(
            not subject or len(subject) > 256
            for subject in self.authorized_deploy_subjects
        ):
            raise ValueError("deployment subjects must be nonempty and bounded")
        for path, content in self.source_files.items():
            _source_path(path)
            if not isinstance(content, str):
                raise ValueError("source files must be text")
            if any(
                "/".join(path.split("/")[:depth])
                in {*self.source_files, "release.toml"}
                for depth in range(1, len(path.split("/")))
            ):
                raise ValueError("source files cannot contain a parent file")
        if (
            sum(len(content.encode()) for content in self.source_files.values())
            + len(self.release_definition_toml.encode())
            > 256 * 1024
        ):
            raise ValueError("mounted release source exceeds renderer input limit")
        try:
            definition = tomllib.loads(self.release_definition_toml)
        except tomllib.TOMLDecodeError as error:
            raise ValueError("release definition must be valid TOML") from error
        target = definition.get("target", {})
        release = definition.get("release", {})
        if (
            not isinstance(target, dict)
            or not isinstance(release, dict)
            or target.get("kubeconfig") != "/var/lib/piceli/control/target.kubeconfig"
            or target.get("context") != "piceli-incluster"
            or not isinstance(target.get("namespace"), str)
            or target.get("transport", "https") != "https"
            or target.get("allow_exec", False)
            or target.get("nodes")
            or not isinstance(release.get("state_dir"), str)
            or release.get("state", "local") != "local"
        ):
            raise ValueError(
                "release definition must use the installed target and state"
            )
        state = release["state_dir"]
        if not state.startswith("/var/lib/piceli/control/") or any(
            part in {".", ".."} for part in state.split("/")
        ):
            raise ValueError(
                "release state must be under the private control directory"
            )
        for key in ("catalog", "journal", "secret_store"):
            value = release.get(key)
            if value is not None and (
                not isinstance(value, str)
                or not value.startswith("/var/lib/piceli/control/")
                or any(part in {".", ".."} for part in value.split("/"))
            ):
                raise ValueError(
                    "release data paths must stay on the private state PVC"
                )

    @property
    def namespace(self) -> str:
        return str(tomllib.loads(self.release_definition_toml)["target"]["namespace"])


def _https_url(value: str, *, label: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTPS URL")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise ValueError(f"{label} has an invalid port") from error
    return parsed.hostname, port


def _origin(value: str, *, label: str) -> tuple[str, int]:
    host, port = _https_url(value, label=label)
    if urlsplit(value).path not in {"", "/"}:
        raise ValueError(f"{label} must be an HTTPS origin")
    return host, port


def _cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ValueError("egress destinations must be exact CIDR networks") from error
    if network.prefixlen == 0:
        raise ValueError("unrestricted egress is not an install default")
    return str(network)


def _image(value: str) -> None:
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", value):
        raise ValueError("installation images must be pinned by sha256 digest")


@dataclass(frozen=True)
class ClusterInstallConfig:
    """Inputs that an operator must choose before reviewing the manifest.

    CIDRs must cover the Kubernetes API endpoint and OIDC provider actually
    reached by the UI pod. Standard NetworkPolicy cannot select DNS names.
    """

    namespace: str
    origin: str
    api_server: str
    oidc_issuer: str
    oidc_metadata_url: str
    oidc_client_id: str
    authorized_subjects: tuple[str, ...]
    ui_image: str
    gateway_image: str
    tls_secret: str
    ingress_class: str
    ingress_namespace: str
    ingress_pod_labels: Mapping[str, str]
    backend_tls_annotation_key: str
    backend_tls_annotation_value: str
    api_egress_cidrs: tuple[str, ...]
    oidc_egress_cidrs: tuple[str, ...]
    state_claim: str = "piceli-ui-state"
    state_size: str = "5Gi"
    storage_class: str | None = None
    url_prefix: str = ""
    dns_namespace: str = "kube-system"
    authorized_access_subjects: tuple[str, ...] = ()
    manual: ManualDeliveryConfig | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ingress_pod_labels", MappingProxyType(dict(self.ingress_pod_labels))
        )
        object.__setattr__(self, "authorized_subjects", tuple(self.authorized_subjects))
        object.__setattr__(
            self, "authorized_access_subjects", tuple(self.authorized_access_subjects)
        )
        object.__setattr__(self, "api_egress_cidrs", tuple(self.api_egress_cidrs))
        object.__setattr__(self, "oidc_egress_cidrs", tuple(self.oidc_egress_cidrs))
        names = (
            self.namespace,
            self.tls_secret,
            self.ingress_class,
            self.ingress_namespace,
            self.state_claim,
            self.dns_namespace,
        )
        if any(len(name) > 63 or not _NAME.fullmatch(name) for name in names):
            raise ValueError("installation names must be DNS labels")
        host, port = _origin(self.origin, label="UI origin")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            origin_is_ip = False
        else:
            origin_is_ip = True
        if (
            port != 443
            or urlsplit(self.origin).port is not None
            or host == "localhost"
            or origin_is_ip
            or "." not in host
        ):
            raise ValueError("UI origin must be a public HTTPS DNS name on port 443")
        _origin(self.api_server, label="Kubernetes API server")
        _https_url(self.oidc_issuer, label="OIDC issuer")
        _https_url(self.oidc_metadata_url, label="OIDC metadata URL")
        if not self.oidc_client_id or not self.authorized_subjects:
            raise ValueError("OIDC client and at least one subject are required")
        if any(
            not subject or len(subject) > 256 for subject in self.authorized_subjects
        ):
            raise ValueError("OIDC subjects must be nonempty and bounded")
        if not set(self.authorized_access_subjects) <= set(self.authorized_subjects):
            raise ValueError("access subjects must also be observation subjects")
        if not _SIZE.fullmatch(self.state_size):
            raise ValueError("state size must be an explicit Mi/Gi/Ti quantity")
        if self.storage_class is not None and (
            len(self.storage_class) > 253
            or any(
                len(part) > 63 or not _NAME.fullmatch(part)
                for part in self.storage_class.split(".")
            )
        ):
            raise ValueError("storage class must be a DNS name")
        if self.url_prefix and not re.fullmatch(
            r"(?:/[A-Za-z0-9_-]+)+", self.url_prefix
        ):
            raise ValueError("URL prefix must match cluster session routing")
        if not self.ingress_pod_labels or any(
            not key or not value or not _LABEL.fullmatch(value)
            for key, value in self.ingress_pod_labels.items()
        ):
            raise ValueError("ingress controller pod labels must be explicit")
        if (
            not self.backend_tls_annotation_key
            or not self.backend_tls_annotation_value
            or any(char.isspace() for char in self.backend_tls_annotation_key)
            or self.backend_tls_annotation_value.lower() != "https"
        ):
            raise ValueError("an explicit HTTPS backend ingress annotation is required")
        if not self.api_egress_cidrs or not self.oidc_egress_cidrs:
            raise ValueError("explicit API and OIDC egress CIDRs are required")
        for image in (self.ui_image, self.gateway_image):
            _image(image)
        for value in (*self.api_egress_cidrs, *self.oidc_egress_cidrs):
            _cidr(value)
        if self.manual is not None and (
            self.manual.namespace != self.namespace
            or not set(self.manual.authorized_deploy_subjects)
            <= set(self.authorized_subjects)
        ):
            raise ValueError(
                "manual delivery target and subjects must match this installation"
            )


def _object(kind: str, name: str, namespace: str, spec: dict) -> dict:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
        **spec,
    }


def _egress(cidrs: tuple[str, ...], port: int) -> dict:
    return {
        "to": [{"ipBlock": {"cidr": value}} for value in cidrs],
        "ports": [{"protocol": "TCP", "port": port}],
    }


def render_cluster_install(config: ClusterInstallConfig) -> tuple[dict, ...]:
    """Return reviewable Kubernetes objects; never apply them."""
    namespace = config.namespace
    manual = config.manual
    source_data: dict[str, str] = {}
    source_items: list[dict[str, str]] = []
    if manual is not None:
        source_data["release.toml"] = manual.release_definition_toml
        source_items.append({"key": "release.toml", "path": "release.toml"})
        for index, path in enumerate(sorted(manual.source_files)):
            key = f"src-{index:03d}"
            source_data[key] = manual.source_files[path]
            source_items.append({"key": key, "path": path})
    labels = {"app.kubernetes.io/name": "piceli-ui"}
    ui_labels = {**labels, "app.kubernetes.io/component": "ui"}
    ingress_host = urlsplit(config.origin).hostname
    assert ingress_host is not None
    _, api_port = _origin(config.api_server, label="Kubernetes API server")
    _, oidc_port = _https_url(config.oidc_issuer, label="OIDC issuer")
    metadata_port = urlsplit(config.oidc_metadata_url).port or 443
    if metadata_port != oidc_port:
        raise ValueError("OIDC issuer and metadata must use one egress port")
    ui_command = [
        "piceli",
        "ui",
        "cluster-serve" if manual is not None else "cluster-observe",
        "--api-server",
        config.api_server,
        "--ca-file",
        "/var/run/piceli-credentials/ca.crt",
        "--token-file",
        "/var/run/piceli-credentials/token",
        "--namespace",
        namespace,
        "--control-dir",
        "/var/lib/piceli/control",
        "--origin",
        config.origin,
        "--oidc-issuer",
        config.oidc_issuer,
        "--oidc-metadata-url",
        config.oidc_metadata_url,
        "--oidc-client-id",
        config.oidc_client_id,
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ]
    if config.url_prefix:
        ui_command += ["--url-prefix", config.url_prefix]
    for subject in config.authorized_subjects:
        ui_command += ["--authorized-sub", subject]
    for subject in config.authorized_access_subjects:
        ui_command += ["--authorized-access-sub", subject]
    if manual is not None:
        ui_command += [
            "--definition",
            "/opt/piceli/source/release.toml",
            "--source-root",
            "/opt/piceli/source",
            "--renderer-image",
            manual.renderer_image,
            "--renderer-platform",
            manual.renderer_platform,
        ]
        for path in manual.source_file_allowlist:
            ui_command += ["--source-file", path]
        for subject in manual.authorized_deploy_subjects:
            ui_command += ["--authorized-deploy-sub", subject]

    caddyfile = (
        "{\n    admin off\n}\n"
        ":8443 {\n"
        "    tls /etc/piceli/tls/tls.crt /etc/piceli/tls/tls.key\n"
        "    reverse_proxy 127.0.0.1:8000\n"
        "}\n"
    )
    config_hash = hashlib.sha256(
        json.dumps(
            {"command": ui_command, "caddyfile": caddyfile, "source": source_data},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    identity_name = (
        "piceli-ui-identity-" + hashlib.sha256(namespace.encode()).hexdigest()[:12]
    )
    role_rules = [
        {
            "apiGroups": [""],
            "resources": ["pods", "services", "events", "persistentvolumeclaims"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": [""],
            "resources": ["pods/log"],
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["batch"],
            "resources": ["jobs", "cronjobs"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["ingresses"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "resourceNames": ["piceli-renderer-deny-egress"],
            "verbs": ["get"],
        },
        {
            "apiGroups": ["networking.k8s.io"],
            "resources": ["networkpolicies"],
            "verbs": ["list"],
        },
    ]
    if manual is not None:
        role_rules += [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["create", "get", "delete"],
            },
            {
                "apiGroups": ["batch"],
                "resources": ["jobs"],
                "verbs": ["create", "get", "delete"],
            },
            *(
                {
                    "apiGroups": [rule.api_group],
                    "resources": [rule.resource],
                    "verbs": list(rule.verbs),
                }
                for rule in manual.deploy_resources
            ),
        ]
    deployment: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "piceli-ui", "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": ui_labels},
            "template": {
                "metadata": {
                    "labels": ui_labels,
                    "annotations": {"install.piceli.dev/config-sha256": config_hash},
                },
                "spec": {
                    "serviceAccountName": "piceli-ui",
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "ui",
                            "image": config.ui_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ui_command,
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "env": [
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "XDG_CACHE_HOME", "value": "/tmp"},
                            ],
                            "volumeMounts": [
                                {"name": "state", "mountPath": "/var/lib/piceli"},
                                {
                                    "name": "credentials",
                                    "mountPath": "/var/run/piceli-credentials",
                                    "readOnly": True,
                                },
                                {"name": "ui-tmp", "mountPath": "/tmp"},
                            ],
                            "readinessProbe": {
                                "exec": {
                                    "command": [
                                        "python",
                                        "-c",
                                        "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()",
                                    ]
                                },
                                "periodSeconds": 5,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                        },
                        {
                            "name": "tls-gateway",
                            "image": config.gateway_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["sh", "-c"],
                            "args": [
                                "cp /usr/bin/caddy /tmp/caddy && "
                                "exec /tmp/caddy run --config /etc/piceli/Caddyfile"
                            ],
                            "ports": [{"name": "https", "containerPort": 8443}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {
                                    "name": "gateway-config",
                                    "mountPath": "/etc/piceli",
                                    "readOnly": True,
                                },
                                {
                                    "name": "tls",
                                    "mountPath": "/etc/piceli/tls",
                                    "readOnly": True,
                                },
                                {"name": "gateway-data", "mountPath": "/data"},
                                {"name": "gateway-runtime", "mountPath": "/config"},
                                {"name": "gateway-tmp", "mountPath": "/tmp"},
                            ],
                            "readinessProbe": {
                                "tcpSocket": {"port": 8443},
                                "periodSeconds": 5,
                            },
                            "resources": {
                                "requests": {"cpu": "25m", "memory": "32Mi"},
                                "limits": {"cpu": "250m", "memory": "128Mi"},
                            },
                        },
                    ],
                    "volumes": [
                        {
                            "name": "state",
                            "persistentVolumeClaim": {"claimName": config.state_claim},
                        },
                        {
                            "name": "credentials",
                            "projected": {
                                "defaultMode": 0o440,
                                "sources": [
                                    {
                                        "serviceAccountToken": {
                                            "path": "token",
                                            "expirationSeconds": 3600,
                                        }
                                    },
                                    {
                                        "configMap": {
                                            "name": "kube-root-ca.crt",
                                            "items": [
                                                {"key": "ca.crt", "path": "ca.crt"}
                                            ],
                                        }
                                    },
                                ],
                            },
                        },
                        {"name": "ui-tmp", "emptyDir": {}},
                        {
                            "name": "gateway-config",
                            "configMap": {"name": "piceli-ui-gateway"},
                        },
                        {"name": "tls", "secret": {"secretName": config.tls_secret}},
                        {"name": "gateway-data", "emptyDir": {}},
                        {"name": "gateway-runtime", "emptyDir": {}},
                        {"name": "gateway-tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }
    if manual is not None:
        pod = deployment["spec"]["template"]["spec"]
        source_hashes = [
            (
                item["path"],
                hashlib.sha256(source_data[item["key"]].encode()).hexdigest(),
            )
            for item in source_items
        ]
        pod["initContainers"] = [
            {
                "name": "materialize-release-source",
                "image": config.ui_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-I", "-c", _MATERIALIZE_SOURCE],
                "args": [
                    "/opt/piceli/source-configmap",
                    "/opt/piceli/source-materialized",
                    json.dumps(source_hashes, separators=(",", ":")),
                ],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "25m", "memory": "32Mi"},
                    "limits": {"cpu": "250m", "memory": "128Mi"},
                },
                "volumeMounts": [
                    {
                        "name": "release-source-configmap",
                        "mountPath": "/opt/piceli/source-configmap",
                        "readOnly": True,
                    },
                    {
                        "name": "release-source",
                        "mountPath": "/opt/piceli/source-materialized",
                    },
                ],
            }
        ]
        pod["containers"][0]["volumeMounts"].append(
            {
                "name": "release-source",
                "mountPath": "/opt/piceli/source",
                "readOnly": True,
            }
        )
        pod["volumes"].append(
            {
                "name": "release-source-configmap",
                "configMap": {
                    "name": "piceli-ui-source",
                    "items": source_items,
                },
            }
        )
        pod["volumes"].append({"name": "release-source", "emptyDir": {}})
    objects = [
        _object(
            "ServiceAccount",
            "piceli-ui",
            namespace,
            {"automountServiceAccountToken": False},
        ),
        _object(
            "ServiceAccount",
            "piceli-renderer",
            namespace,
            {"automountServiceAccountToken": False},
        ),
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "piceli-ui", "namespace": namespace},
            "rules": role_rules,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "piceli-ui", "namespace": namespace},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "piceli-ui",
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": "piceli-ui", "namespace": namespace}
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": identity_name},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["namespaces"],
                    "resourceNames": ["kube-system", namespace],
                    "verbs": ["get"],
                }
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": identity_name},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": identity_name,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": "piceli-ui", "namespace": namespace}
            ],
        },
        _object(
            "ConfigMap",
            "piceli-ui-gateway",
            namespace,
            {"data": {"Caddyfile": caddyfile}},
        ),
        _object(
            "PersistentVolumeClaim",
            config.state_claim,
            namespace,
            {
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": config.state_size}},
                    **(
                        {"storageClassName": config.storage_class}
                        if config.storage_class
                        else {}
                    ),
                }
            },
        ),
        deployment,
        _object(
            "Service",
            "piceli-ui",
            namespace,
            {
                "spec": {
                    "type": "ClusterIP",
                    "selector": ui_labels,
                    "ports": [{"name": "https", "port": 443, "targetPort": 8443}],
                }
            },
        ),
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": "piceli-ui",
                "namespace": namespace,
                "annotations": {
                    config.backend_tls_annotation_key: config.backend_tls_annotation_value
                },
            },
            "spec": {
                "ingressClassName": config.ingress_class,
                "tls": [{"hosts": [ingress_host], "secretName": config.tls_secret}],
                "rules": [
                    {
                        "host": ingress_host,
                        "http": {
                            "paths": [
                                {
                                    "path": config.url_prefix or "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": "piceli-ui",
                                            "port": {"name": "https"},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "piceli-ui-network", "namespace": namespace},
            "spec": {
                "podSelector": {"matchLabels": ui_labels},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": config.ingress_namespace
                                    }
                                },
                                "podSelector": {
                                    "matchLabels": dict(config.ingress_pod_labels)
                                },
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8443}],
                    }
                ],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": config.dns_namespace
                                    }
                                }
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    _egress(config.api_egress_cidrs, api_port),
                    _egress(config.oidc_egress_cidrs, oidc_port),
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "piceli-renderer-deny-egress", "namespace": namespace},
            "spec": {
                "podSelector": {
                    "matchLabels": {"app.kubernetes.io/component": "renderer"}
                },
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        },
    ]
    if manual is not None:
        objects.insert(
            objects.index(deployment),
            _object(
                "ConfigMap",
                "piceli-ui-source",
                namespace,
                {"data": source_data},
            ),
        )
    return tuple(objects)


def cluster_install_yaml(config: ClusterInstallConfig) -> str:
    """Serialize the exact reviewable objects as a multi-document manifest."""
    return yaml.safe_dump_all(render_cluster_install(config), sort_keys=False)
