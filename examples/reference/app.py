"""The reference app: one typed module, three environments, no manifest dicts.

A small but complete application, deployed to ``dev``, ``staging`` and
``prod`` from this file alone (see docs/reference_app.md):

* ``web``: a Deployment with a Service, an autoscaler and a disruption budget;
* ``api``: a Deployment with a Service and a ServiceAccount that may read
  ConfigMaps (RBAC);
* ``store``: a Redis StatefulSet with one claim per pod (``ClaimTemplate``);
* ``migrate`` (a Job the api waits for) and ``report`` (a nightly CronJob);
* a Gateway API HTTPRoute (plus an Ingress in ``dev`` only), a cert-manager
  ``Certificate`` typed by ``piceli codegen crd`` (``crds/certificate.py``),
  and one NetworkPolicy for the whole release;
* the store password from a SOPS file (``secrets/example.enc.yaml``), and
  post-deploy checks.

Every pod runs with the ``restricted`` Pod Security settings and a read-only
root filesystem. The images are small public images pinned by digest, so
nothing is built: ``busybox`` serves static files as stand-ins for the web
and api servers.

**The age key in ``secrets/example-only.agekey`` is public and for this
example only.** Encrypt your own secrets for your own key.

    export SOPS_AGE_KEY_FILE=$PWD/examples/reference/secrets/example-only.agekey
    piceli render examples/reference/app.py:app --env prod
    piceli render examples/reference/app.py:app --env staging --diff-env prod
    piceli deploy examples/reference/app.py:pipeline --env dev --plan
    piceli deploy examples/reference/app.py:pipeline --env dev --approve <hash>
    piceli status examples/reference/app.py:dev

The ``dev`` target is a local kind cluster named ``reference`` whose
kubeconfig is ``examples/reference/reference.kubeconfig``; the
``REFERENCE_*`` environment variables override it.
"""

from __future__ import annotations

import os

from crds.certificate import API_VERSION, KIND, CertificateSpec
from crds.certificate import CertificateSpecIssuerRef as IssuerRef
from crds.certificate import CertificateSpecPrivateKey as PrivateKey

from piceli import (
    App,
    Checks,
    ClaimTemplate,
    ConfigVolume,
    GatewayRef,
    Pipeline,
    PodDefaults,
    Resources,
    Route,
    Rule,
    Scaling,
    Secrets,
    SecretVolume,
    Security,
    Sops,
    Target,
    Template,
)

# Small public images, pinned by digest (multi-arch indexes).
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
REDIS = (
    "docker.io/library/redis:7.4.2-alpine"
    "@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952"
)

app = App(
    "reference",
    pod_defaults=PodDefaults(
        security=Security.restricted(
            user=10001, fs_group=10001, read_only_root_filesystem=True
        ),
        automount_token=False,  # only pods bound to a declared account get a token
        termination_grace_seconds=10,
    ),
)

# --------------------------------------------------------------- secrets
# The store password comes from a SOPS file; Piceli reads it at plan time
# into its private store and never prints it. The URL and the Redis config
# are rendered from it.
secrets = Secrets(
    store_password=Sops(
        "secrets/example.enc.yaml", "store.password", pass_env=["SOPS_AGE_KEY_FILE"]
    ),
    store_url=Template("redis://:{store_password}@store:6379/0"),
    store_conf=Template("dir /data\nappendonly yes\nrequirepass {store_password}\n"),
)
# Secrets are never rewritten: a rotated value goes into a new name (-2).
credentials = app.secret(
    "store-credentials-1",
    {
        "password": secrets.ref("store_password"),
        "url": secrets.ref("store_url"),
        "redis.conf": secrets.ref("store_conf"),
    },
)

# ---------------------------------------------------------- configuration
settings = app.config("settings", {"LOG_LEVEL": "info", "FEATURE_REPORTS": "on"})
site = app.config(
    "site",
    {
        "index.html": "<h1>reference app</h1>\n",
        "healthz": "ok\n",
        "version": "1\n",
    },
)

# ------------------------------------------------------------------ store
store = app.stateful_set(
    "store",
    image=REDIS,
    command=["redis-server", "/etc/store/redis.conf"],
    ports=[6379],
    ready=app.probe.tcp(6379),
    live=app.probe.tcp(6379, initial_delay_seconds=10),
    env={"REDISCLI_AUTH": credentials.key("password")},  # for redis-cli checks
    volumes={
        "/data": ClaimTemplate("data", size="64Mi"),
        "/etc/store": SecretVolume(credentials, items={"redis.conf": "redis.conf"}),
    },
    resources=Resources(cpu="50m", memory="64Mi", memory_limit="128Mi"),
)

# ---------------------------------------------------------------- workers
migrate = app.job(
    "migrate",
    image=REDIS,
    command=["redis-cli", "-h", "store", "SET", "schema-version", "1"],
    env={"REDISCLI_AUTH": credentials.key("password")},
    backoff_limit=4,
    active_deadline_seconds=300,
    resources=Resources(cpu="10m", memory="16Mi"),
)
app.depends(migrate, on=store)
app.cron_job(
    "report",
    schedule="0 3 * * *",
    time_zone="Etc/UTC",
    concurrency="Forbid",
    image=REDIS,
    command=["redis-cli", "-h", "store", "INCR", "reports"],
    env={"REDISCLI_AUTH": credentials.key("password")},
    backoff_limit=1,
    resources=Resources(cpu="10m", memory="16Mi"),
)

# -------------------------------------------------------------------- api
# The api may read ConfigMaps (feature flags) and nothing else.
api_account = app.service_account(
    "api-reader",
    rules=[Rule(resources=["configmaps"], verbs=["get", "list", "watch"])],
)
api = app.deployment(
    "api",
    image=BUSYBOX,
    command=["httpd", "-f", "-p", "8080", "-h", "/srv"],
    ports=[8080],
    ready=app.probe.http("/healthz", 8080),
    env={
        "LOG_LEVEL": settings.key("LOG_LEVEL"),
        "STORE_URL": credentials.key("url"),
    },
    volumes={"/srv": ConfigVolume(site)},
    resources=Resources(cpu="20m", memory="16Mi", memory_limit="32Mi"),
    service_account=api_account,
)
api_service = app.service(api, port=8080)
app.depends(api, on=migrate)

# -------------------------------------------------------------------- web
web = app.deployment(
    "web",
    image=BUSYBOX,
    command=["httpd", "-f", "-p", "8080", "-h", "/srv"],
    ports=[8080],
    ready=app.probe.http("/healthz", 8080),
    env={"API_URL": "http://api:8080"},
    volumes={"/srv": ConfigVolume(site)},
    resources=Resources(cpu="20m", memory="16Mi", memory_limit="32Mi"),
)
web_service = app.service(
    web,
    port=80,
    target_port=8080,
    access=app.access.forward(local=18080, path="/"),  # piceli access
)
app.autoscaler(web, min_replicas=2, max_replicas=6, cpu=70)
app.disruption_budget(web, max_unavailable=1)
app.depends(web, on=api)

# ---------------------------------------------------------------- traffic
app.http_route(
    "site",
    gateway=GatewayRef("public", namespace="gateways", section="https"),
    hosts=["dev.reference.example.com"],
    routes=[Route(web_service, "/"), Route(api_service, "/api")],
)
# A local cluster without a Gateway controller gets an Ingress too (dev only).
app.ingress(
    "site",
    hosts=["dev.reference.example.com"],
    routes=[Route(web_service, "/"), Route(api_service, "/api")],
    component="dev-ingress",
)
app.resource(
    API_VERSION,
    KIND,
    "site-tls",
    CertificateSpec(
        secret_name="site-tls",
        dns_names=["dev.reference.example.com"],
        issuer_ref=IssuerRef(name="self-signed", kind="ClusterIssuer"),
        private_key=PrivateKey(algorithm="ECDSA", size=256),
    ),
    public=["spec.privateKey"],  # algorithm settings, not key material
)
# Only this release's pods may connect to its pods.
app.network_policy(
    selector=app.release_selector,
    allow_from_selector=app.release_selector,
    name="internal",
)


# ----------------------------------------------------------- environments
def certificate(*hosts: str, issuer: str) -> CertificateSpec:
    return CertificateSpec(
        secret_name="site-tls",
        dns_names=list(hosts),
        issuer_ref=IssuerRef(name=issuer, kind="ClusterIssuer"),
        private_key=PrivateKey(algorithm="ECDSA", size=256),
    )


app.environment(
    "dev",
    namespace="reference-dev",
    config={"settings": {"LOG_LEVEL": "debug"}},
    autoscalers={"web": Scaling(min_replicas=1, max_replicas=2)},
)
app.environment(
    "staging",
    namespace="reference-staging",
    hosts={"HTTPRoute/site": ["staging.reference.example.com"]},
    specs={
        "site-tls": certificate(
            "staging.reference.example.com", issuer="letsencrypt-staging"
        )
    },
    enabled={"dev-ingress": False},
)
app.environment(
    "prod",
    namespace="reference-prod",
    replicas={"api": 3},
    autoscalers={"web": Scaling(min_replicas=3, max_replicas=10)},
    resources={
        "web": Resources(cpu="100m", memory="64Mi", memory_limit="128Mi"),
        "api": Resources(cpu="200m", memory="128Mi", memory_limit="256Mi"),
    },
    config={"settings": {"LOG_LEVEL": "warning"}},
    hosts={"HTTPRoute/site": ["reference.example.com", "www.reference.example.com"]},
    specs={
        "site-tls": certificate(
            "reference.example.com", "www.reference.example.com", issuer="letsencrypt"
        )
    },
    enabled={"dev-ingress": False},
)

# ------------------------------------------------------------------ deploy
pipeline = Pipeline(
    app,
    {  # one target per environment; `--env` selects one (with its own state)
        "dev": Target.kubeconfig(
            os.environ.get("REFERENCE_KUBECONFIG", "reference.kubeconfig"),
            context=os.environ.get("REFERENCE_CONTEXT", "kind-reference"),
            namespace=os.environ.get("REFERENCE_NAMESPACE", "reference-dev"),
        ),
        "staging": Target.kubeconfig(
            "cluster.kubeconfig", context="main", namespace="reference-staging"
        ),
        "prod": Target.kubeconfig(
            "cluster.kubeconfig", context="main", namespace="reference-prod"
        ),
    },
    secrets=secrets,
    checks=[
        Checks.http(web_service, "/", expect=200, body_contains="reference app"),
        Checks.exec(store, ["redis-cli", "GET", "schema-version"], output_contains="1"),
    ],
    rollback_on_failed_checks=True,
    state_dir=os.environ.get("REFERENCE_STATE_DIR", ".piceli-deploy"),
    execution={"readiness_seconds": 300},
)
# `piceli status` and `piceli access` take a pipeline with one target.
dev = pipeline.for_environment("dev")
