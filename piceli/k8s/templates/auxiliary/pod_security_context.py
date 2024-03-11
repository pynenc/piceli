from typing import Optional

from kubernetes import client


def get_security_context(
    security_context_uid: Optional[int],
) -> Optional[client.V1PodSecurityContext]:
    """get the security context"""
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
