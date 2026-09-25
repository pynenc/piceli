"""The comparison app in Piceli: web and api, three environments, one module.

A slice of ``examples/reference`` (see docs/comparisons.md): two busybox
web servers serving a ConfigMap, their Services, an autoscaler and a
disruption budget for ``web``, one NetworkPolicy, restricted pods, and
``dev``, ``staging`` and ``prod`` that differ in log level, replicas,
autoscaling and resources. The same app is written with Helm, Kustomize,
cdk8s and Pulumi in the sibling directories; ``tests/unit/test_comparisons.py``
checks that all of them render the same objects.

    piceli render examples/comparisons/piceli/app.py:app --env prod
    piceli render examples/comparisons/piceli/app.py:app --env staging --diff-env prod
"""

from __future__ import annotations

from piceli import App, ConfigVolume, PodDefaults, Resources, Scaling, Security

BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

app = App(
    "webapp",
    pod_defaults=PodDefaults(
        security=Security.restricted(
            user=10001, fs_group=10001, read_only_root_filesystem=True
        ),
        automount_token=False,
        termination_grace_seconds=10,
    ),
)

settings = app.config("settings", {"LOG_LEVEL": "info", "FEATURE_REPORTS": "on"})
site = app.config("site", {"index.html": "<h1>webapp</h1>\n", "healthz": "ok\n"})

api = app.deployment(
    "api",
    image=BUSYBOX,
    command=["httpd", "-f", "-p", "8080", "-h", "/srv"],
    ports=[8080],
    ready=app.probe.http("/healthz", 8080),
    env={"LOG_LEVEL": settings.key("LOG_LEVEL")},
    volumes={"/srv": ConfigVolume(site)},
    resources=Resources(cpu="20m", memory="16Mi", memory_limit="32Mi"),
)
app.service(api, port=8080)

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
app.service(web, port=80, target_port=8080)
app.autoscaler(web, min_replicas=2, max_replicas=6, cpu=70)
app.disruption_budget(web, max_unavailable=1)
app.depends(web, on=api)

# Only this release's pods may connect to its pods.
app.network_policy(
    selector=app.release_selector,
    allow_from_selector=app.release_selector,
    name="internal",
)

app.environment(
    "dev",
    namespace="webapp-dev",
    config={"settings": {"LOG_LEVEL": "debug"}},
    autoscalers={"web": Scaling(min_replicas=1, max_replicas=2)},
)
app.environment("staging", namespace="webapp-staging")
app.environment(
    "prod",
    namespace="webapp-prod",
    replicas={"api": 3},
    autoscalers={"web": Scaling(min_replicas=3, max_replicas=10)},
    resources={
        "web": Resources(cpu="100m", memory="64Mi", memory_limit="128Mi"),
        "api": Resources(cpu="200m", memory="128Mi", memory_limit="256Mi"),
    },
    config={"settings": {"LOG_LEVEL": "warning"}},
)
