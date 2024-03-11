from typing import Optional

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import container as container_lib
from piceli.k8s.templates.auxiliary import names, pod_security_context
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import service_account as sa_lib


class Pod(BaseModel):
    "k8s_client.Kubernetes common pod definition"

    name: names.Name
    containers: list[container_lib.Container] = []
    init_containers: list[container_lib.Container] = []
    service_account: Optional[sa_lib.ServiceAccount] = None
    automount_service_account_token: Optional[bool] = None
    port: Optional[int] = None
    restart_policy: policies.RestartPolicy = policies.RestartPolicy.NEVER
    security_context_uid: Optional[int] = None
    template_labels: Optional[Labels] = None
    image_pull_secrets: list[str] = []
    termination_grace_period_seconds: Optional[int] = None
    # API: ClassVar[str] = "core"
    # API_FUNC: ClassVar[str] = "job"

    @property
    def container_map(self) -> dict[str, container_lib.Container]:
        """returns a dict of containers by name"""
        return {container.name: container for container in self.containers}

    def get_pod_spec(self) -> client.V1PodTemplateSpec:
        """gets the Pod template spec definition"""
        containers = []
        init_containers = []
        _volume_claims: dict[str, client.V1Volume] = {}
        env: Optional[client.V1EnvVar] = None

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
        """Concatenate deployment pod labes to use in selector field"""
        labels = [f"{k}={v}" for k, v in self.get_pod_spec().metadata.labels.items()]
        return ",".join(labels)

    # TODO check what to do with ops

    # def delete_all_pods(
    #     self,
    #     k8s: k8s_client.Kubernetes,
    #     async_req: bool = False,
    #     dry_run: k8s_client.DryRun = k8s_client.DryRun.OFF,
    # ) -> None:
    #     """delete all pods that match label_selector"""
    #     log.info(
    #         "deleting all pods matching %s",
    #         label_selector := self.get_label_selector(k8s),
    #     )
    #     for pod in k8s.core_api.list_namespaced_pod(
    #         DEFAULT_NAMESPACE, label_selector=label_selector
    #     ).items:
    #         log.info(
    #             "deleting pod %s from labled_Selector %s",
    #             name := pod.metadata.name,
    #             label_selector,
    #         )
    #         k8s.core_api.delete_namespaced_pod(
    #             name, DEFAULT_NAMESPACE, async_req=async_req, dry_run=dry_run.value
    #         )

    # def wait_pod_deletion(
    #     self, k8s: k8s_client.Kubernetes, dry_run: k8s_client.DryRun
    # ) -> None:
    #     """Wait for all the pods to be deleted"""
    #     log.info(
    #         "Waiting for the deletation of all the pods from %s %s, with labels %s",
    #         _type := type(self).__name__,
    #         self.name,
    #         label_selector := self.get_label_selector(k8s),
    #     )
    #     timeout = time.time() + WAIT_TIMEOUT
    #     while pods := k8s.core_api.list_namespaced_pod(
    #         DEFAULT_NAMESPACE, label_selector=label_selector
    #     ).items:
    #         if dry_run == k8s_client.DryRun.ON:
    #             log.warning(
    #                 "Running apply with dry_run:ON, aborting wait for %s deletion "
    #                 "because the previous delete on dry_run did nothing, "
    #                 "so this will loop until timeout and return an error",
    #                 _type,
    #             )
    #             break
    #         log.info(
    #             "%s %s deleted, but still %s pods need to be terminated: %s",
    #             _type,
    #             self.name,
    #             len(pods),
    #             [pod.metadata.name for pod in pods],
    #         )
    #         if time.time() > timeout:
    #             break
    #         time.sleep(1)

    # def wait(self, k8s: k8s_client.Kubernetes) -> None:
    #     self._wait(
    #         k8s=k8s,
    #         func=k8s.core_api.list_namespaced_pod,
    #         args=(DEFAULT_NAMESPACE,),
    #         label_selector=self.get_label_selector(k8s),
    #         condition=k8s_client.WaitConditionPod.READY,
    #     )
