"""An app delivered to a client's cluster that we never access.

``piceli bundle`` packages the ``client`` environment: a kustomize base and
overlay, ``prepare.sh`` (the generated Secrets, made in the client's cluster),
the image archives, ``INSTALL.md`` and ``UNINSTALL.md``. See
docs/client_delivery.md.

    piceli render examples/client_bundle/app.py:pipeline --env client   # no cluster
    piceli artifacts build-spec run --spec examples/client_bundle/host-build.toml \\
        --out receipt.json                                              # both platforms
    piceli bundle examples/client_bundle/app.py:pipeline --env client \\
        --receipt receipt.json --out shop-bundle                       # amd64 archives

The web page shows the cluster's identity (the ``kube-system`` namespace UID,
read at runtime by ``app.cluster_identity``) and whether the generated
Secrets are mounted, never their values.
"""

from __future__ import annotations

from piceli import (
    App,
    Build,
    MemoryVolume,
    Pipeline,
    PodDefaults,
    Random,
    Registry,
    Resources,
    Secrets,
    SecretVolume,
    Security,
    Target,
    Template,
    TlsCa,
)

# Every pod: non-root, read-only root filesystem, no privilege escalation.
app = App(
    "shop",
    pod_defaults=PodDefaults(
        security=Security.restricted(
            user=10001, fs_group=10001, read_only_root_filesystem=True
        )
    ),
)

# Made by prepare.sh in the client's cluster; the bundle carries no value.
secrets = Secrets(
    api_token=Random(32),
    cache_url=Template("redis://:{api_token}@cache:6379/0"),
    tls=TlsCa({"web": ["web"]}, common_name="shop private CA", days=90),
)
private = app.secret(
    "shop-private",
    {
        "api-token": secrets.ref("api_token"),
        "cache-url": secrets.ref("cache_url"),
        "ca.crt": secrets.ref("tls.ca.crt"),
        "web.crt": secrets.ref("tls.web.crt"),
        "web.key": secrets.ref("tls.web.key"),
    },
)

build = Build.spec("host-build.toml", builder="host")

# The cluster's identity, read from the cluster when the pod starts.
identity = app.cluster_identity(image=build["web"])
web = app.deployment(
    "web",
    image=build["web"],
    ports=[8080],
    init=[identity.init],
    volumes={
        **identity.volumes,
        "/tmp": MemoryVolume(size_limit="8Mi"),
        "/run/private": SecretVolume(private, default_mode=0o440),
    },
    service_account=identity.service_account,
    env={"CLUSTER_UID_FILE": identity.file},
    ready=app.probe.http("/", 8080),
    resources=Resources(
        cpu="10m", memory="16Mi", cpu_limit="200m", memory_limit="64Mi"
    ),
)
app.service(web, port=8080, access=app.access.forward(local=18080, health="/"))
# Declared egress: only the init container's call to the API server.
app.network_policy(web, egress=[identity.egress], name="web-api-server")

app.environment("client", namespace="shop")

# The client's cluster is never contacted: the target only names the namespace
# the bundle renders into, and the registry is the client's (set in the overlay).
pipeline = Pipeline(
    app,
    {"client": Target.profile("client", namespace="shop")},
    build=build,
    deliver=Registry("oci://registry.example.invalid/shop"),
    secrets=secrets,
)
