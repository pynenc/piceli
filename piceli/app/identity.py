"""The cluster's identity, read from the cluster at runtime (``app.cluster_identity``).

A cluster has no name of its own, but its ``kube-system`` namespace has a UID
that never changes while the cluster lives. An app that must know which
cluster it runs in (a licence, a tenant, telemetry) reads that UID from the
cluster instead of carrying a constant per cluster:

- a ServiceAccount whose only permission is ``get`` on the namespace
  ``kube-system`` (a ClusterRole restricted by ``resourceNames``);
- an init container that asks the API server for that namespace and writes
  its UID to a file on a memory volume (``/run/cluster-identity/uid``);
- the egress rule the init container needs to reach the API server (TCP 443
  and 6443), for apps behind a default-deny NetworkPolicy.

The init container runs POSIX ``sh`` with ``curl`` (the API server's
certificate is verified with the service account's CA) or, without curl,
BusyBox ``wget`` (which cannot verify certificates: prefer an image with
curl). Pass ``command=`` to use your own program instead.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from piceli.app.model import (
    Container,
    MemoryVolume,
    NetworkRule,
    Resources,
    Rule,
    ServiceAccount,
)

#: Where the init container writes the UID, by default.
DIRECTORY = "/run/cluster-identity"
#: The API server ports a pod reaches: the ``kubernetes`` Service (443) and,
#: after its DNAT, the API server itself (6443 on most distributions).
API_PORTS = (443, 6443)

#: The init container's script: ``$IDENTITY_FILE`` gets the UID.
SCRIPT = r"""set -eu
sa="${SERVICE_ACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}"
host="${KUBERNETES_SERVICE_HOST:?not in a pod}"
case "$host" in *:*) host="[$host]" ;; esac
url="https://$host:${KUBERNETES_SERVICE_PORT:-443}/api/v1/namespaces/kube-system"
token="$(cat "$sa/token")"
if command -v curl >/dev/null 2>&1; then
  body="$(curl -fsS --max-time 20 --cacert "$sa/ca.crt" -H "Authorization: Bearer $token" "$url")"
else
  body="$(wget -q -T 20 -O - --header "Authorization: Bearer $token" "$url")"
fi
uid="$(printf '%s' "$body" | tr -d ' \n' | sed -n 's/^[^{]*{.*"metadata":{[^}]*"uid":"\([0-9a-f-]*\)".*$/\1/p')"
[ -n "$uid" ] || { echo "cluster identity: no kube-system uid" >&2; exit 1; }
printf '%s\n' "$uid" > "$IDENTITY_FILE.tmp"
mv "$IDENTITY_FILE.tmp" "$IDENTITY_FILE"
echo "cluster identity: written"
"""


def identity_rule() -> Rule:
    """The one RBAC rule that reads the cluster identity: ``get namespaces/kube-system``.

    Add it to the ``cluster_rules`` of a service account the workload
    already has, instead of :meth:`App.cluster_identity <piceli.app.App.cluster_identity>`'s own.
    """
    return Rule(
        resources=("namespaces",), verbs=("get",), resource_names=("kube-system",)
    )


@dataclass(frozen=True)
class ClusterIdentity:
    """What :meth:`App.cluster_identity <piceli.app.App.cluster_identity>` declares; wire it into a workload.

    :param service_account: The ServiceAccount allowed to read ``kube-system``
        (pass it as the workload's ``service_account=``).
    :param init: The init container that writes :attr:`file`.
    :param volumes: ``{directory: MemoryVolume}``; mount it in the main
        container too (``volumes={**identity.volumes, ...}``).
    :param file: The file holding the UID (one line).
    :param egress: The egress rule to the API server (TCP 443 and 6443),
        for ``app.network_policy(workload, egress=[identity.egress])``.

    Example::

        identity = app.cluster_identity(image=build["api"])
        api = app.deployment(
            "api", image=build["api"],
            init=[identity.init], volumes=identity.volumes,
            service_account=identity.service_account,
            env={"CLUSTER_UID_FILE": identity.file},
        )
        app.network_policy(api, egress=[identity.egress], name="api-api-server")
    """

    service_account: ServiceAccount
    init: Container
    volumes: Mapping[str, MemoryVolume] = field(default_factory=dict)
    file: str = f"{DIRECTORY}/uid"
    egress: NetworkRule = field(
        default_factory=lambda: NetworkRule(ports=API_PORTS)  # type: ignore[arg-type]
    )

    @staticmethod
    def rule() -> Rule:
        """See :func:`identity_rule`."""
        return identity_rule()


def identity_volume() -> MemoryVolume:
    """The memory volume holding the UID file (shared by init and main containers)."""
    return MemoryVolume(name="cluster-identity", size_limit="1Mi")


def init_container(
    *,
    image: str,
    directory: str = DIRECTORY,
    name: str = "cluster-identity",
    command: Sequence[str] | None = None,
    resources: Resources | None = None,
    volume: MemoryVolume | None = None,
) -> Container:
    """The init container writing ``<directory>/uid`` (see the module docstring)."""
    volume = volume or identity_volume()
    return Container(
        name=name,
        image=image,
        command=tuple(command) if command is not None else ("/bin/sh", "-c", SCRIPT),
        env={"IDENTITY_FILE": f"{directory}/uid"},
        resources=resources
        or Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi"),
        volumes={directory: volume},
    )
