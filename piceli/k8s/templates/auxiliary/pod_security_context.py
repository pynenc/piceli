from typing import Optional

from kubernetes import client


def get_security_context(
    security_context_uid: Optional[int],
) -> Optional[client.V1PodSecurityContext]:
    """
    Generates a Kubernetes V1PodSecurityContext object with security settings for a pod.

    This security context applies to all containers running within the pod, setting user and group IDs,
    enforcing non-root execution, and applying a default seccomp profile for enhanced security.

    :param Optional[int] security_context_uid: The user ID that will be used to set `fs_group`,
    `run_as_group`, and `run_as_user`. If specified, this ensures that the pod and its containers
    run with these security settings. If `None`, no security context is applied.

    :return: An instance of `client.V1PodSecurityContext` configured with the specified security settings
    if `security_context_uid` is provided; otherwise, `None`.
    """
    if not security_context_uid:
        return None
    return client.V1PodSecurityContext(
        fs_group=security_context_uid,
        run_as_group=security_context_uid,
        run_as_user=security_context_uid,
        run_as_non_root=True,
        # fs_group_change_policy="Always",
        seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
    )
