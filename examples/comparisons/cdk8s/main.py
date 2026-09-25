"""The comparison app in cdk8s (Python): one chart per environment.

Typed Kubernetes objects from ``cdk8s_plus_34.k8s`` (the same classes
``cdk8s import k8s`` generates); ``python main.py`` (or ``cdk8s synth``)
writes ``dist/webapp-<env>.k8s.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

IMAGE = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
PART_OF = {"app.kubernetes.io/part-of": "webapp"}


@dataclass
class Resources:
    cpu: str = "20m"
    memory: str = "16Mi"
    memory_limit: str = "32Mi"


@dataclass
class Environment:
    namespace: str
    log_level: str = "info"
    api_replicas: int = 1
    web_min: int = 2
    web_max: int = 6
    api_resources: Resources = field(default_factory=Resources)
    web_resources: Resources = field(default_factory=Resources)


ENVIRONMENTS = {
    "dev": Environment("webapp-dev", log_level="debug", web_min=1, web_max=2),
    "staging": Environment("webapp-staging"),
    "prod": Environment(
        "webapp-prod",
        log_level="warning",
        api_replicas=3,
        web_min=3,
        web_max=10,
        api_resources=Resources("200m", "128Mi", "256Mi"),
        web_resources=Resources("100m", "64Mi", "128Mi"),
    ),
}


def _server(
    name: str, env: list[k8s.EnvVar], resources: Resources
) -> k8s.PodTemplateSpec:
    """A restricted busybox web server serving the ``site`` ConfigMap."""
    labels = {"app.kubernetes.io/name": name, **PART_OF}
    return k8s.PodTemplateSpec(
        metadata=k8s.ObjectMeta(labels=labels),
        spec=k8s.PodSpec(
            automount_service_account_token=False,
            termination_grace_period_seconds=10,
            security_context=k8s.PodSecurityContext(
                fs_group=10001,
                run_as_group=10001,
                run_as_non_root=True,
                run_as_user=10001,
                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
            ),
            containers=[
                k8s.Container(
                    name=name,
                    image=IMAGE,
                    command=["httpd", "-f", "-p", "8080", "-h", "/srv"],
                    ports=[k8s.ContainerPort(container_port=8080)],
                    readiness_probe=k8s.Probe(
                        http_get=k8s.HttpGetAction(
                            path="/healthz", port=k8s.IntOrString.from_number(8080)
                        )
                    ),
                    env=env,
                    resources=k8s.ResourceRequirements(
                        requests={
                            "cpu": k8s.Quantity.from_string(resources.cpu),
                            "memory": k8s.Quantity.from_string(resources.memory),
                        },
                        limits={
                            "memory": k8s.Quantity.from_string(resources.memory_limit)
                        },
                    ),
                    security_context=k8s.SecurityContext(
                        allow_privilege_escalation=False,
                        capabilities=k8s.Capabilities(drop=["ALL"]),
                        read_only_root_filesystem=True,
                    ),
                    volume_mounts=[k8s.VolumeMount(name="site", mount_path="/srv")],
                )
            ],
            volumes=[
                k8s.Volume(
                    name="site", config_map=k8s.ConfigMapVolumeSource(name="site")
                )
            ],
        ),
    )


def _service(scope: Construct, name: str, port: int) -> None:
    k8s.KubeService(
        scope,
        f"{name}-service",
        metadata=k8s.ObjectMeta(name=name, labels=PART_OF),
        spec=k8s.ServiceSpec(
            selector={"app.kubernetes.io/name": name},
            ports=[
                k8s.ServicePort(
                    port=port,
                    target_port=k8s.IntOrString.from_number(8080),
                    protocol="TCP",
                )
            ],
        ),
    )


def _deployment(
    scope: Construct, name: str, replicas: int, template: k8s.PodTemplateSpec
) -> None:
    k8s.KubeDeployment(
        scope,
        name,
        metadata=k8s.ObjectMeta(
            name=name, labels={"app.kubernetes.io/name": name, **PART_OF}
        ),
        spec=k8s.DeploymentSpec(
            replicas=replicas,
            selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": name}),
            template=template,
        ),
    )


class WebApp(Chart):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id, namespace=env.namespace)
        k8s.KubeConfigMap(
            self,
            "settings",
            metadata=k8s.ObjectMeta(name="settings", labels=PART_OF),
            data={"LOG_LEVEL": env.log_level, "FEATURE_REPORTS": "on"},
        )
        k8s.KubeConfigMap(
            self,
            "site",
            metadata=k8s.ObjectMeta(name="site", labels=PART_OF),
            data={"index.html": "<h1>webapp</h1>\n", "healthz": "ok\n"},
        )

        log_level = k8s.EnvVar(
            name="LOG_LEVEL",
            value_from=k8s.EnvVarSource(
                config_map_key_ref=k8s.ConfigMapKeySelector(
                    name="settings", key="LOG_LEVEL"
                )
            ),
        )
        _deployment(
            self,
            "api",
            env.api_replicas,
            _server("api", [log_level], env.api_resources),
        )
        _service(self, "api", 8080)

        api_url = k8s.EnvVar(name="API_URL", value="http://api:8080")
        _deployment(
            self, "web", env.web_min, _server("web", [api_url], env.web_resources)
        )
        _service(self, "web", 80)
        k8s.KubeHorizontalPodAutoscalerV2(
            self,
            "web-autoscaler",
            metadata=k8s.ObjectMeta(name="web", labels=PART_OF),
            spec=k8s.HorizontalPodAutoscalerSpecV2(
                scale_target_ref=k8s.CrossVersionObjectReferenceV2(
                    api_version="apps/v1", kind="Deployment", name="web"
                ),
                min_replicas=env.web_min,
                max_replicas=env.web_max,
                metrics=[
                    k8s.MetricSpecV2(
                        type="Resource",
                        resource=k8s.ResourceMetricSourceV2(
                            name="cpu",
                            target=k8s.MetricTargetV2(
                                type="Utilization", average_utilization=70
                            ),
                        ),
                    )
                ],
            ),
        )
        k8s.KubePodDisruptionBudget(
            self,
            "web-budget",
            metadata=k8s.ObjectMeta(name="web", labels=PART_OF),
            spec=k8s.PodDisruptionBudgetSpec(
                max_unavailable=k8s.IntOrString.from_number(1),
                selector=k8s.LabelSelector(
                    match_labels={"app.kubernetes.io/name": "web"}
                ),
            ),
        )
        # Only this release's pods may connect to its pods.
        k8s.KubeNetworkPolicy(
            self,
            "internal",
            metadata=k8s.ObjectMeta(name="internal", labels=PART_OF),
            spec=k8s.NetworkPolicySpec(
                pod_selector=k8s.LabelSelector(match_labels=PART_OF),
                policy_types=["Ingress"],
                ingress=[
                    k8s.NetworkPolicyIngressRule(
                        from_=[
                            k8s.NetworkPolicyPeer(
                                pod_selector=k8s.LabelSelector(match_labels=PART_OF)
                            )
                        ]
                    )
                ],
            ),
        )


app = App()
for name, environment in ENVIRONMENTS.items():
    WebApp(app, f"webapp-{name}", environment)

if __name__ == "__main__":
    app.synth()
