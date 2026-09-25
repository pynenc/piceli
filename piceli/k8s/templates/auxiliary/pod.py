from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import container as container_lib
from piceli.k8s.templates.auxiliary import names, pod_security_context
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import service_account as sa_lib


class Pod(BaseModel):
    """
    Represents a common Kubernetes Pod definition within Piceli, encapsulating the necessary configuration for deploying containers.

    Attributes:
        :param names.Name name: The name of the Pod, unique within a namespace.
        :param list[container_lib.Container] containers: A list of containers to include in the Pod.
        :param list[container_lib.Container] init_containers: A list of initialization containers that run before the app containers.
        :param Optional[sa_lib.ServiceAccount] service_account: The service account associated with the Pod.
        :param Optional[bool] automount_service_account_token: Indicates whether to automatically mount the service account token.
        :param Optional[int] port: The port that the container exposes.
        :param policies.RestartPolicy restart_policy: The Pod's restart policy.
        :param Optional[int] security_context_uid: UID to use for the Pod's security context.
        :param Optional[Labels] template_labels: Labels to apply to the Pod for identification and selection.
        :param list[str] image_pull_secrets: Names of the Kubernetes secrets used to pull container images.
        :param Optional[int] termination_grace_period_seconds: Duration in seconds to wait before forcibly terminating the container.

    This class provides a structured way to define and manage the configuration of Pods, crucial for the deployment of containerized applications within Kubernetes. It integrates with various auxiliary classes and templates to specify containers, security settings, and other critical aspects of Pod deployment.
    """

    name: names.Name
    containers: list[container_lib.Container] = []
    init_containers: list[container_lib.Container] = []
    service_account: sa_lib.ServiceAccount | None = None
    automount_service_account_token: bool | None = None
    port: int | None = None
    restart_policy: policies.RestartPolicy = policies.RestartPolicy.NEVER
    security_context_uid: int | None = None
    template_labels: Labels | None = None
    image_pull_secrets: list[str] = []
    termination_grace_period_seconds: int | None = None

    @property
    def container_map(self) -> dict[str, container_lib.Container]:
        """returns a dict of containers by name"""
        return {container.name: container for container in self.containers}

    def get_pod_spec(self) -> client.V1PodTemplateSpec:
        """
        Generates the Kubernetes Pod template specification for the defined Pod configuration.

        :return: A `client.V1PodTemplateSpec` instance representing the Pod's configuration for deployment.
        """
        containers = []
        init_containers = []
        _volume_claims: dict[str, client.V1Volume] = {}
        env: client.V1EnvVar | None = None

        def get_container_spec_and_update_volumes(
            container: container_lib.Container,
        ) -> client.V1Container:
            container_spec = container.get_container_spec()
            if env:
                if container_spec.env:
                    container_spec.env.append(env)
                else:
                    container_spec.env = [env]

            for volume_claim in container.get_volume_claims():
                if volume_claim.name in _volume_claims:
                    if _volume_claims[volume_claim.name] != volume_claim:
                        raise ValueError(
                            f"Volume claim {volume_claim} is already defined with a different configuration {_volume_claims[volume_claim.name]}"
                        )
                    continue
                _volume_claims[volume_claim.name] = volume_claim
            return container_spec

        for container in self.containers:
            containers.append(get_container_spec_and_update_volumes(container))
        for container in self.init_containers:
            init_containers.append(get_container_spec_and_update_volumes(container))

        service_account_name = (
            self.service_account.name if self.service_account else None
        )
        _image_pull_secrets = [
            client.V1LocalObjectReference(name=ps) for ps in self.image_pull_secrets
        ]
        pod_template = client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                restart_policy=self.restart_policy.value,
                containers=containers,
                init_containers=init_containers or None,
                image_pull_secrets=_image_pull_secrets or None,
                service_account_name=service_account_name,
                automount_service_account_token=self.automount_service_account_token,
                volumes=list(_volume_claims.values()) if _volume_claims else None,
                security_context=pod_security_context.get_security_context(
                    self.security_context_uid
                ),
                termination_grace_period_seconds=self.termination_grace_period_seconds,
            ),
            metadata=client.V1ObjectMeta(name=self.name, labels=self.template_labels),
        )
        return pod_template

    def get_label_selector(self) -> str:
        """
        Constructs a label selector string from the Pod's labels for Kubernetes operations.

        :return: A string representation of the label selector.
        """
        labels = [f"{k}={v}" for k, v in self.get_pod_spec().metadata.labels.items()]
        return ",".join(labels)

    # TODO check what to do with ops
