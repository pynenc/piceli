"""Provider-free session construction; execution is deliberately separate."""

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    PlanAuthorization,
    ResourceIntent,
)
from piceli.k8s.ops.session import DeploymentSession


def composition(inputs):
    secret = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "api-key", "namespace": "example"},
            "data": {"token": "<private>"},
        }
    ).with_secret("/data/token", inputs["api_token"])
    return DeploymentComposition((DeploymentComponent("credentials", (secret,)),))


# ``snapshot``, ``grant_factory``, ``journal`` and ``private_store`` are supplied
# by the caller's discovery/authorization boundary.  This call does not create a
# Kubernetes client or contact a cluster.
def create_session(snapshot, grant_factory, journal, private_store):
    return DeploymentSession.create(
        private_inputs={"api_token": "caller-owned-private-value"},
        composition_factory=composition,
        snapshot=snapshot,
        plan_authorization=PlanAuthorization(snapshot.target),
        authorization_factory=grant_factory,
        journal=journal,
        secrets=private_store,
    )
