"""The app the lifecycle composition deploys: one Pipeline for every environment.

Five workloads, each exercising one part of a release:

- ``web``: a static site and a small C program, both host-built; checked over
  HTTP (also inside an isolated branch environment) and by running the
  program in the pod;
- ``store``: a StatefulSet with a retained claim (``ClaimTemplate``), a
  pre-rollout configuration check that reads the release's own Secret and
  ConfigMap (also on a first install into an empty namespace), a read-only
  upgrade check of its data and restore points;
- ``watcher``: a service account that reads pods (a Role) and nodes (a
  ClusterRole) from the API, also in a branch environment (``allow_api``);
- ``cache``: a third-party image pinned by digest, copied into the
  in-cluster registry;
- ``reporter``: only in the full stack, so its check is skipped
  (``not-in-stack``) in branch environments.

``infra.py`` says where and when it deploys.
"""

from __future__ import annotations

import json

from lifecycle_site import SERVER

from piceli import (
    App,
    Build,
    Checks,
    ClaimTemplate,
    ConfigVolume,
    Pipeline,
    Probe,
    Random,
    Registry,
    Resources,
    RestorePoints,
    Rule,
    Secrets,
    SecretVolume,
    Target,
    UpgradeCheck,
)
from piceli.approval_policy import ApprovalPolicy

#: A third-party image, pinned by its index digest: copied into the
#: in-cluster registry when an environment syncs.
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

registry = Registry.in_cluster(
    on=SERVER, storage="8Gi", repository="lifecycle", mirror=[BUSYBOX]
)
images = Build.spec("host-build.toml", builder="host")
SMALL = Resources(cpu="10m", memory="16Mi", memory_limit="64Mi")

app = App("lifecycle", owner="lifecycle")

# --- private material, generated once and kept by the release ---------------
secrets = Secrets(store_token=Random(24))
store_secret = app.secret("store-secret", {"token": secrets.ref("store_token")})
store_config = app.config(
    "store-config",
    {"store.json": json.dumps({"name": "lifecycle", "retain": True}, sort_keys=True)},
)

# --- workloads ---------------------------------------------------------------
web = app.deployment(
    "web",
    image=images["web"],
    ports=[8080],
    ready=Probe.http("/index.html", 8080),
    resources=SMALL,
)
web_service = app.service(web, port=8080)

store = app.stateful_set(
    "store",
    image=images["store"],
    ports=[7000],
    ready=Probe.tcp(7000),
    resources=SMALL,
    replicas=1,
    volumes={
        "/var/lib/store": ClaimTemplate(
            "data", size="64Mi", storage_class="lifecycle-retain"
        ),
        "/etc/store/secret": SecretVolume(store_secret),
        "/etc/store/config": ConfigVolume(store_config),
    },
)
app.service(store, port=7000, name="store-http")
# Before the store's pods change, the new image checks its configuration with
# the release's own Secret and ConfigMap (created by this release on a first
# install), then opens the running store's data read-only.
app.pre_rollout(
    store,
    ["sh", "/opt/store/check-config.sh"],
    upgrade=UpgradeCheck(["sh", "/opt/store/verify.sh", "/var/lib/store"]),
)

READ = ("get", "list", "watch")
watcher_account = app.service_account(
    "watcher",
    rules=[Rule(resources=["pods"], verbs=READ)],
    cluster_rules=[Rule(resources=["nodes"], verbs=READ)],
)
watcher = app.deployment(
    "watcher",
    image=images["watcher"],
    service_account=watcher_account,
    resources=SMALL,
)

cache = app.deployment(
    "cache",
    image=BUSYBOX,
    command=["httpd", "-f", "-p", "6379", "-h", "/tmp"],
    ports=[6379],
    ready=Probe.tcp(6379),
    resources=SMALL,
)
app.service(cache, port=6379)

reporter = app.deployment(
    "reporter",
    image=BUSYBOX,
    command=["sh", "-c", "while true; do sleep 3600; done"],
    resources=SMALL,
)

# --- checks: after every rollout; a failed check rolls the release back -------
#: Checks added after the first release (the acceptance adds one here to
#: change only a check).
EXTRA_CHECKS: list = []
CHECKS = [
    Checks.http(
        web_service,
        "/index.html",
        expect=200,
        body_contains="lifecycle",
        name="web-index",
    ),
    Checks.exec(
        "deployment/web",
        ["/opt/web/bin/web-version"],
        output_contains="web-version",
        name="web-binary",
    ),
    Checks.exec(
        "statefulset/store",
        ["cat", "/var/lib/store/ready"],
        output_contains="ok",
        name="store-ready",
    ),
    Checks.exec(
        "deployment/watcher",
        ["cat", "/tmp/watch/state"],
        output_contains="pods-ok",
        name="watcher-reads-api",
        retries=10,
        interval=3,
    ),
    Checks.exec(
        "deployment/reporter",
        ["echo", "reporter-ok"],
        output_contains="reporter-ok",
        name="reporter-runs",
    ),
    # No target: runs in every stack (in the controller, through the API).
    Checks.python("checks_probe.py:site_config", name="site-config"),
    *EXTRA_CHECKS,
]

pipeline = Pipeline(
    app,
    # Only for a deploy from a laptop; the controller deploys each
    # environment into its own namespace with its own service account.
    Target.profile("lifecycle", namespace="lifecycle"),
    build=images,
    deliver=registry,
    secrets=secrets,
    checks=CHECKS,
    rollback_on_failed_checks=True,
    restore_points=RestorePoints(include="all", timeout_seconds=600),
    auto_approve=ApprovalPolicy(allow={"create", "apply", "no-op"}, max_objects=32),
)
