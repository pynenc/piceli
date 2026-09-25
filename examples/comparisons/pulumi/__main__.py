"""The comparison app in Pulumi (Python, Kubernetes provider).

One stack per environment (``Pulumi.<env>.yaml``); the provider takes an
explicit kubeconfig file and context from the stack configuration.

    pulumi stack select dev && pulumi preview && pulumi up
"""

from __future__ import annotations

from typing import Any

import pulumi
import pulumi_kubernetes as k8s

IMAGE = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
PART_OF = {"app.kubernetes.io/part-of": "webapp"}
DEFAULT_RESOURCES = {"cpu": "20m", "memory": "16Mi", "memoryLimit": "32Mi"}

config = pulumi.Config()
namespace = config.require("namespace")
provider = k8s.Provider(
    "cluster",
    kubeconfig=config.require("kubeconfig"),  # a file path; never the ambient one
    context=config.require("context"),
    namespace=namespace,
)
options = pulumi.ResourceOptions(provider=provider)


def meta(name: str, labels: dict[str, str] | None = None) -> k8s.meta.v1.ObjectMetaArgs:
    return k8s.meta.v1.ObjectMetaArgs(
        name=name, namespace=namespace, labels=labels or PART_OF
    )


def server(
    name: str, env: list[k8s.core.v1.EnvVarArgs], resources: dict[str, Any]
) -> k8s.core.v1.PodTemplateSpecArgs:
    """A restricted busybox web server serving the ``site`` ConfigMap."""
    return k8s.core.v1.PodTemplateSpecArgs(
        metadata=k8s.meta.v1.ObjectMetaArgs(
            labels={"app.kubernetes.io/name": name, **PART_OF}
        ),
        spec=k8s.core.v1.PodSpecArgs(
            automount_service_account_token=False,
            termination_grace_period_seconds=10,
            security_context=k8s.core.v1.PodSecurityContextArgs(
                fs_group=10001,
                run_as_group=10001,
                run_as_non_root=True,
                run_as_user=10001,
                seccomp_profile=k8s.core.v1.SeccompProfileArgs(type="RuntimeDefault"),
            ),
            containers=[
                k8s.core.v1.ContainerArgs(
                    name=name,
                    image=IMAGE,
                    command=["httpd", "-f", "-p", "8080", "-h", "/srv"],
                    ports=[k8s.core.v1.ContainerPortArgs(container_port=8080)],
                    readiness_probe=k8s.core.v1.ProbeArgs(
                        http_get=k8s.core.v1.HTTPGetActionArgs(
                            path="/healthz", port=8080
                        )
                    ),
                    env=env,
                    resources=k8s.core.v1.ResourceRequirementsArgs(
                        requests={
                            "cpu": resources["cpu"],
                            "memory": resources["memory"],
                        },
                        limits={"memory": resources["memoryLimit"]},
                    ),
                    security_context=k8s.core.v1.SecurityContextArgs(
                        allow_privilege_escalation=False,
                        capabilities=k8s.core.v1.CapabilitiesArgs(drop=["ALL"]),
                        read_only_root_filesystem=True,
                    ),
                    volume_mounts=[
                        k8s.core.v1.VolumeMountArgs(name="site", mount_path="/srv")
                    ],
                )
            ],
            volumes=[
                k8s.core.v1.VolumeArgs(
                    name="site",
                    config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name="site"),
                )
            ],
        ),
    )


def deployment(
    name: str, replicas: int, template: k8s.core.v1.PodTemplateSpecArgs
) -> None:
    k8s.apps.v1.Deployment(
        name,
        metadata=meta(name, {"app.kubernetes.io/name": name, **PART_OF}),
        spec=k8s.apps.v1.DeploymentSpecArgs(
            replicas=replicas,
            selector=k8s.meta.v1.LabelSelectorArgs(
                match_labels={"app.kubernetes.io/name": name}
            ),
            template=template,
        ),
        opts=options,
    )


def service(name: str, port: int) -> None:
    k8s.core.v1.Service(
        f"{name}-service",
        metadata=meta(name),
        spec=k8s.core.v1.ServiceSpecArgs(
            selector={"app.kubernetes.io/name": name},
            ports=[
                k8s.core.v1.ServicePortArgs(port=port, target_port=8080, protocol="TCP")
            ],
        ),
        opts=options,
    )


k8s.core.v1.ConfigMap(
    "settings",
    metadata=meta("settings"),
    data={
        "LOG_LEVEL": config.get("logLevel") or "info",
        "FEATURE_REPORTS": "on",
    },
    opts=options,
)
k8s.core.v1.ConfigMap(
    "site",
    metadata=meta("site"),
    data={"index.html": "<h1>webapp</h1>\n", "healthz": "ok\n"},
    opts=options,
)

log_level = k8s.core.v1.EnvVarArgs(
    name="LOG_LEVEL",
    value_from=k8s.core.v1.EnvVarSourceArgs(
        config_map_key_ref=k8s.core.v1.ConfigMapKeySelectorArgs(
            name="settings", key="LOG_LEVEL"
        )
    ),
)
deployment(
    "api",
    config.get_int("apiReplicas") or 1,
    server("api", [log_level], config.get_object("apiResources") or DEFAULT_RESOURCES),
)
service("api", 8080)

web_min = config.get_int("webMinReplicas") or 2
web_max = config.get_int("webMaxReplicas") or 6
deployment(
    "web",
    web_min,
    server(
        "web",
        [k8s.core.v1.EnvVarArgs(name="API_URL", value="http://api:8080")],
        config.get_object("webResources") or DEFAULT_RESOURCES,
    ),
)
service("web", 80)
k8s.autoscaling.v2.HorizontalPodAutoscaler(
    "web-autoscaler",
    metadata=meta("web"),
    spec=k8s.autoscaling.v2.HorizontalPodAutoscalerSpecArgs(
        scale_target_ref=k8s.autoscaling.v2.CrossVersionObjectReferenceArgs(
            api_version="apps/v1", kind="Deployment", name="web"
        ),
        min_replicas=web_min,
        max_replicas=web_max,
        metrics=[
            k8s.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=k8s.autoscaling.v2.ResourceMetricSourceArgs(
                    name="cpu",
                    target=k8s.autoscaling.v2.MetricTargetArgs(
                        type="Utilization", average_utilization=70
                    ),
                ),
            )
        ],
    ),
    opts=options,
)
k8s.policy.v1.PodDisruptionBudget(
    "web-budget",
    metadata=meta("web"),
    spec=k8s.policy.v1.PodDisruptionBudgetSpecArgs(
        max_unavailable=1,
        selector=k8s.meta.v1.LabelSelectorArgs(
            match_labels={"app.kubernetes.io/name": "web"}
        ),
    ),
    opts=options,
)
# Only this release's pods may connect to its pods.
k8s.networking.v1.NetworkPolicy(
    "internal",
    metadata=meta("internal"),
    spec=k8s.networking.v1.NetworkPolicySpecArgs(
        pod_selector=k8s.meta.v1.LabelSelectorArgs(match_labels=PART_OF),
        policy_types=["Ingress"],
        ingress=[
            k8s.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    k8s.networking.v1.NetworkPolicyPeerArgs(
                        pod_selector=k8s.meta.v1.LabelSelectorArgs(match_labels=PART_OF)
                    )
                ]
            )
        ],
    ),
    opts=options,
)
