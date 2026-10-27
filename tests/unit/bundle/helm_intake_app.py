"""0.34.0: ``intake.expose`` also patches the agent to listen on every
address (``INTAKE_LISTEN``), and a private CA is an optional input
(``privateCa``).

0.31.0: the ``helm_app`` client with an optional intake (a Service and an
ingress NetworkPolicy from listed namespaces), a disk limit that follows
``storage.size``, for the ``bundle --helm`` tests.

The original fixture: a client app for the ``bundle --helm`` tests.

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
    HelmOptional,
    HelmToggle,
    HelmValue,
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
    private_ca=ImportSecretSpec.model_validate({"type": "import", "env": "PRIVATE_CA"}),
)
private = app.secret(
    "agent-private",
    {
        "api-token": secrets.ref("api_token"),
        "intake-token": secrets.ref("intake_token"),
        "cluster-key": secrets.ref("cluster_key"),
        "private-ca": secrets.ref("private_ca"),
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
        "DISK_LIMIT": "900M",
        "INTAKE_LISTEN": "127.0.0.1:4318",
    },
    ports=[7000, 4318],
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

intake = app.service(agent, port=4318, name="agent-intake")
intake_policy = app.network_policy(
    agent,
    name="agent-intake",
    ingress=[
        NetworkRule(
            peers=(NetworkPeer.namespaces(["workers"]),),
            ports=(NetworkPort(port=4318),),
        )
    ],
    policy_types=("Ingress",),
)
app.safety_exception(
    intake_policy, "network", reason="the client lists the namespaces that send"
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
# A merge patch: containers and env are merged by name.
LISTEN_EVERYWHERE = {
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "agent",
                        "env": [{"name": "INTAKE_LISTEN", "value": "0.0.0.0:4318"}],
                    }
                ]
            }
        }
    }
}
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
    optional={
        "intake.expose": HelmOptional(
            "Expose the intake to other namespaces",
            objects=[intake, intake_policy],
            patches=[(agent, LISTEN_EVERYWHERE)],
        )
    },
    values={
        "intake.allowNamespaces": HelmValue(
            "Namespaces whose pods may send to the intake",
            targets=[
                (
                    intake_policy,
                    "/spec/ingress/0/from/0/namespaceSelector/matchExpressions/0/values",
                )
            ],
        )
    },
    storage_env={"DISK_LIMIT": 90},
    optional_inputs=("privateCa",),
)
