from typing import Iterator, Optional

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.constants import policies
from piceli.k8s.templates.auxiliary import env_vars, names, port, resource_request
from piceli.k8s.templates.deployable import configmap, secret, volume


class Container(BaseModel):
    """Container definition"""

    name: names.Name
    image: str
    command: Optional[list[str]] = None
    args: Optional[list[str]] = None
    image_pull_policy: Optional[policies.ImagePullPolicy] = None
    ports: Optional[list[port.Port]] = None
    env: Optional[
        dict[str, str | env_vars.ValueFromField | env_vars.ValueFromResourceField]
    ] = None
    volumes: Optional[list[volume.VolumeMount]] = None
    liveness_pre_stop_command: Optional[list[str]] = None
    liveness_post_start_command: Optional[list[str]] = None
    readiness_command: Optional[list[str]] = None
    liveness_command: Optional[list[str]] = None
    resources: Optional[resource_request.Resources] = None
    env_sources: list[configmap.ConfigMap | secret.Secret] = []
    security_context_uid: Optional[int] = None

    @property
    def ports_dict(self) -> dict[str, int]:
        """ports to dict"""
        if not self.ports:
            return {}
        return {port.name: port.port for port in self.ports}

    def _volumes(self) -> Iterator[tuple[str, volume.VolumeMount]]:
        """Assign an unique name to the volumes and iterates over them"""
        for i, _volume in enumerate(self.volumes or []):
            name = f"{self.name}-data-{i}"
            if isinstance(_volume, volume.VolumeMountEmptyDir):
                name = _volume.name
            if isinstance(_volume, volume.VolumeMountPVCTemplate):
                # for PVC Templates in statefulsets
                name = _volume.pvc_template.name
                # TODO refactor so all the volume mounts can share volume claims between containers
            yield name, _volume

    def get_volume_claims(self) -> Iterator[client.V1PersistentVolumeClaim]:
        """gets the volume claims"""
        for name, _volume in self._volumes():
            if isinstance(_volume, volume.VolumeMountPVC):
                yield client.V1Volume(
                    name=name,
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=_volume.pvc.name
                    ),
                )
            elif isinstance(_volume, volume.VolumeMountPVCTemplate):
                yield client.V1Volume(
                    name=_volume.pvc_template.name,
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=_volume.pvc_template.name
                    ),
                )
            elif isinstance(_volume, volume.VolumeMountConfigMap):
                keys = list(_volume.config_map.data.keys())
                yield client.V1Volume(
                    name=name,
                    config_map=client.V1ConfigMapVolumeSource(
                        name=_volume.config_map.name,
                        items=[{"key": k, "path": k} for k in keys],
                        default_mode=_volume.default_mode,
                    ),
                )
            elif isinstance(_volume, volume.VolumeMountSecret):
                if _volume.secret.data:
                    keys = list(_volume.secret.data.keys())
                elif _volume.secret.string_data:
                    keys = list(_volume.secret.string_data.keys())
                else:
                    raise ValueError(
                        f"Secret {_volume.secret.name} has no data to mount"
                    )
                yield client.V1Volume(
                    name=name,
                    secret=client.V1SecretVolumeSource(
                        secret_name=_volume.secret.name,
                        items=[{"key": k, "path": k} for k in keys],
                        default_mode=_volume.default_mode,
                    ),
                )
            elif isinstance(_volume, volume.VolumeMountEmptyDir):
                yield client.V1Volume(
                    name=name, empty_dir=client.V1EmptyDirVolumeSource()
                )
            else:
                raise ValueError(f"Not supported volume instace {_volume}")

    def get_volume_mounts(self) -> Iterator[client.V1VolumeMount]:
        """gets the volume mounts"""
        for name, _volume in self._volumes():
            if isinstance(_volume, volume.VolumeMountPVC):
                yield client.V1VolumeMount(
                    name=name, mount_path=_volume.mount_path, sub_path=_volume.sub_path
                )
            elif isinstance(_volume, volume.VolumeMountPVCTemplate):
                yield client.V1VolumeMount(
                    name=_volume.pvc_template.name,
                    mount_path=_volume.mount_path,
                    sub_path=_volume.sub_path,
                )
            elif isinstance(
                _volume,
                (
                    volume.VolumeMountConfigMap,
                    volume.VolumeMountSecret,
                    volume.VolumeMountEmptyDir,
                ),
            ):
                yield client.V1VolumeMount(
                    name=name, mount_path=_volume.mount_path, sub_path=None
                )
            else:
                raise ValueError(f"Not supported volume instace {_volume}")

    def get_container_spec(self) -> client.V1Container:
        """gets the Pod template spec definition"""
        env = env_vars.get_env_from_source(self.env_sources)
        if self.env:
            _env = self.env.copy()
            for key, _value in self.env.items():
                if isinstance(
                    _value, (env_vars.ValueFromField, env_vars.ValueFromResourceField)
                ):
                    _env[key] = _value.get()
            env = env_vars.upsert_envvars(env, env_vars.get_env_from_dict(_env))

        volume_mounts = list(self.get_volume_mounts()) if self.volumes else None

        ports, readiness_probe, liveness_probe, life_cycle, sec_ctx = (
            None,
            None,
            None,
            None,
            None,
        )
        if self.ports_dict:
            ports = [
                client.V1ContainerPort(container_port=p, name=n)
                for n, p in self.ports_dict.items()
            ]
        if self.readiness_command:
            readiness_probe = client.V1Probe(
                _exec=client.V1ExecAction(self.readiness_command),
                initial_delay_seconds=5,
                period_seconds=5,
            )
        if self.liveness_command:
            liveness_probe = client.V1Probe(
                _exec=client.V1ExecAction(self.liveness_command),
                initial_delay_seconds=30,
                period_seconds=30,
                failure_threshold=3,
            )
        if self.liveness_pre_stop_command or self.liveness_post_start_command:
            pre_stop, post_start = None, None
            if self.liveness_pre_stop_command:
                pre_stop = client.V1LifecycleHandler(
                    _exec=client.V1ExecAction(command=self.liveness_pre_stop_command)
                )
            if self.liveness_post_start_command:
                post_start = client.V1LifecycleHandler(
                    _exec=client.V1ExecAction(command=self.liveness_post_start_command)
                )
            life_cycle = client.V1Lifecycle(pre_stop=pre_stop, post_start=post_start)

        if not self.security_context_uid:
            sec_ctx = None
        else:
            sec_ctx = client.V1PodSecurityContext(
                fs_group=self.security_context_uid,
                run_as_group=self.security_context_uid,
                run_as_user=self.security_context_uid,
                run_as_non_root=True,
                # fs_group_change_policy="Always",
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            )

        image_pull_policy = (
            self.image_pull_policy.value if self.image_pull_policy else None
        )

        resources = (
            client.V1ResourceRequirements(requests=self.resources.get_k8s_request())
            if self.resources
            else None
        )

        return client.V1Container(
            image=self.image,
            name=self.name,
            image_pull_policy=image_pull_policy,
            command=self.command,
            args=self.args,
            env=env if env else None,
            ports=ports,
            volume_mounts=volume_mounts,
            readiness_probe=readiness_probe,
            liveness_probe=liveness_probe,
            lifecycle=life_cycle,
            resources=resources,
            security_context=sec_ctx,
        )
