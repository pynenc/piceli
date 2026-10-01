"""The app the composition deploys: typed Python, in the composition repository.

Its images come from ``host-build.toml`` next to this file, whose contexts
read the composition's sources (``infra.py``). Nothing here is specific to
one environment: ``infra.py`` says where and when it runs. The same module
also deploys on its own (``piceli deploy example_app.py:pipeline``).
"""

from __future__ import annotations

import os

from piceli import App, Build, Checks, Pipeline, Registry, Resources, Target

REGISTRY_NODE = os.environ.get("EXAMPLE_REGISTRY_NODE", "my-cluster-worker")
#: A third-party image, pinned: copied into the in-cluster registry on sync,
#: never pulled from a hosted registry at run time.
CACHE_IMAGE = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

registry = Registry.in_cluster(
    on=REGISTRY_NODE, repository="example", storage="1Gi", mirror=[CACHE_IMAGE]
)
images = Build.spec("host-build.toml", builder="host")

app = App("example")
api = app.deployment(
    "api",
    image=images["api"],
    ports=[8080],
    ready=app.probe.http("/index.html", 8080),
    resources=Resources(cpu="10m", memory="16Mi", memory_limit="64Mi"),
)
worker = app.deployment(
    "worker",
    image=images["worker"],
    resources=Resources(cpu="10m", memory="16Mi", memory_limit="64Mi"),
)
cache = app.deployment(
    "cache",
    image=CACHE_IMAGE,
    command=["httpd", "-f", "-p", "6379", "-h", "/tmp"],
    ports=[6379],
    ready=app.probe.tcp(6379),
    resources=Resources(cpu="10m", memory="16Mi", memory_limit="64Mi"),
)
app.service(api, port=8080)
app.service(cache, port=6379)

pipeline = Pipeline(
    app,
    # Only for `piceli deploy` from a laptop; the controller deploys with its
    # own service account into each environment's namespace.
    Target.profile("my-cluster", namespace="example"),
    build=images,
    deliver=registry,
    checks=Checks.http(api, "/index.html", expect=200),
    rollback_on_failed_checks=True,
)
