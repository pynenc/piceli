"""A client app for the ``bundle --helm`` tests.

One StatefulSet (an agent with a collector sidecar on loopback, an init
container) with one claim, a settings ConfigMap the client fills, one Secret
holding an imported token and two generated ones, read-only cluster RBAC,
and an egress policy whose UDP rule a toggle narrows (``relayOnly``).
"""

from __future__ import annotations

from piceli import (
    App,
    ClaimTemplate,
    Container,
    HelmChart,
    HelmToggle,
    MemoryVolume,
    NetworkPeer,
    NetworkPort,
    NetworkRule,
    Pipeline,
    PodDefaults,
    Probe,
    Random,
    Resources,
    Rule,
    Secrets,
    SecretVolume,
    Security,
    Target,
)
from piceli.app.model import FieldRef
from piceli.k8s.release_secret_spec import ImportSecretSpec

AGENT_IMAGE = "registry.example/acme/agent@sha256:" + "a" * 64
COLLECTOR_IMAGE = "registry.example/acme/collector@sha256:" + "b" * 64
RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="64Mi")
LABELS = {"app.kubernetes.io/name": "agent", "app.kubernetes.io/part-of": "agent"}

app = App(
    "agent",
    labels=LABELS,
    pod_defaults=PodDefaults(
        security=Security.restricted(
            user=10001, group=10001, fs_group=10001, read_only_root_filesystem=True
        )
    ),
)
secrets = Secrets(
    api_token=ImportSecretSpec.model_validate({"type": "import", "env": "API_TOKEN"}),
    intake_token=Random(32),
    cluster_key=Random(32),
)
private = app.secret(
    "agent-private",
    {
        "api-token": secrets.ref("api_token"),
        "intake-token": secrets.ref("intake_token"),
        "cluster-key": secrets.ref("cluster_key"),
    },
)
settings = app.config(
    "agent-settings",
    {"team": "REPLACE-team", "cluster-id": "REPLACE-cluster-id", "site-name": "lab"},
)
reader = app.service_account(
    "agent-reader",
    cluster_rules=[Rule(resources=["pods", "nodes"], verbs=["get", "list", "watch"])],
)
agent = app.stateful_set(
    "agent",
    image=AGENT_IMAGE,
    container="agent",
    env={
        "POD_NAME": FieldRef(field_path="metadata.name"),
        "TEAM": settings.key("team"),
        "CLUSTER_ID": settings.key("cluster-id"),
    },
    ports=[7000],
    resources=RESOURCES,
    ready=Probe.tcp(7000),
    volumes={
        "/var/lib/agent": ClaimTemplate("state", size="1Gi"),
        "/run/agent": SecretVolume(private, default_mode=0o440),
        "/tmp": MemoryVolume(size_limit="8Mi", name="tmp"),
    },
    init=[
        Container(
            name="prepare",
            image=COLLECTOR_IMAGE,
            command=("/bin/collector", "--prepare"),
            resources=RESOURCES,
        )
    ],
    sidecars=[
        Container(
            name="collector",
            image=COLLECTOR_IMAGE,
            command=("/bin/collector",),
            env={"SITE": settings.key("site-name")},
            resources=RESOURCES,
        )
    ],
    service_account=reader,
    replicas=1,
    headless=False,
    service_name="agent",
)
ANYWHERE = (NetworkPeer.cidr("0.0.0.0/0"), NetworkPeer.cidr("::/0"))
policy = app.network_policy(
    agent,
    egress=[
        NetworkRule(peers=ANYWHERE, ports=(NetworkPort(port=443),)),
        NetworkRule(
            peers=ANYWHERE,
            ports=(NetworkPort(port=1, end_port=65535, protocol="UDP"),),
        ),
    ],
    allow_dns=True,
    policy_types=("Ingress", "Egress"),
)

pipeline = Pipeline(
    app,
    Target.profile("client", namespace="agent"),
    secrets=secrets,
    owner="agent-client",
)

RELAY_PORTS = [
    {"port": 3478, "protocol": "UDP"},
    {"port": 49160, "endPort": 49999, "protocol": "UDP"},
]
chart = HelmChart(
    pipeline,
    required=("team", "clusterId"),
    toggles={
        "relayOnly": HelmToggle(
            "UDP only to the relay's ports, never directly to peers",
            patches=[
                (
                    policy,
                    [
                        {
                            "op": "test",
                            "path": "/spec/egress/1/ports/0",
                            "value": {"port": 1, "endPort": 65535, "protocol": "UDP"},
                        },
                        {
                            "op": "replace",
                            "path": "/spec/egress/1/ports",
                            "value": RELAY_PORTS,
                        },
                    ],
                )
            ],
        )
    },
    descriptions={"team": "the team id", "apiToken": "a file holding the API token"},
)
