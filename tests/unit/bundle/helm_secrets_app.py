"""0.80.0: an app with three Secrets, for the ``existing_secrets`` tests.

``shop-db`` holds an imported password and a URL template that reads it;
``shop-api`` holds a random key and the CA certificate; ``shop-tls`` holds
the web certificate. The workload reads each one (an environment variable, a
volume, a projected volume). Every key name is unique, so ``existingSecret``
may also replace all of them.
"""

from __future__ import annotations

from piceli import (
    App,
    HelmChart,
    MemoryVolume,
    Pipeline,
    PodDefaults,
    Random,
    Resources,
    Secrets,
    SecretVolume,
    Security,
    Target,
    Template,
    TlsCa,
)
from piceli.k8s.release_secret_spec import ImportSecretSpec

IMAGE = "registry.example/shop/web@sha256:" + "d" * 64
RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi")

secrets = Secrets(
    db_password=ImportSecretSpec.model_validate(
        {"type": "import", "env": "DB_PASSWORD"}
    ),
    db_url=Template("postgres://app:{db_password}@db/shop"),
    api_key=Random(32),
    tls=TlsCa({"web": ["web"]}, common_name="shop test CA", days=30),
)
app = App(
    "shop",
    pod_defaults=PodDefaults(
        security=Security.restricted(user=10001, read_only_root_filesystem=True)
    ),
)
db = app.secret(
    "shop-db",
    {"password": secrets.ref("db_password"), "url": secrets.ref("db_url")},
)
api = app.secret(
    "shop-api",
    {"api-key": secrets.ref("api_key"), "api-ca.crt": secrets.ref("tls.ca.crt")},
)
tls = app.secret(
    "shop-tls",
    {"tls.crt": secrets.ref("tls.web.crt"), "tls.key": secrets.ref("tls.web.key")},
)
web = app.deployment(
    "web",
    image=IMAGE,
    env={"DB_PASSWORD": db.key("password"), "DB_URL": db.key("url")},
    ports=[8080],
    volumes={
        "/run/api": SecretVolume(api, default_mode=0o440),
        "/run/tls": SecretVolume(tls, default_mode=0o440),
        "/tmp": MemoryVolume(size_limit="8Mi", name="tmp"),
    },
    resources=RESOURCES,
)
pipeline = Pipeline(app, Target.profile("client", namespace="shop"), secrets=secrets)

# One value per Secret: ``existingSecret`` stays the value that replaces all.
chart = HelmChart(
    pipeline,
    existing_secrets={
        "db.existingSecret": ["shop-db"],
        "api.existingSecret": ["shop-api"],
    },
)
# ``existingSecret`` itself limited to one Secret; another value for a second.
scoped = HelmChart(
    pipeline,
    existing_secrets={
        "existingSecret": ["shop-db"],
        "api.existingSecret": ["shop-api"],
    },
)
