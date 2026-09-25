"""The shop app and how it is deployed (to a test cluster listening on 127.0.0.1)."""

from piceli import App, Pipeline, Target

target = Target.kubeconfig(
    "cluster.kubeconfig",  # in this directory
    context="test",
    namespace="{namespace}",
    transport="loopback-http",  # the test cluster's API is plain http on loopback
)

app = App("shop")
web = app.deployment(
    "web",
    image="registry.example/shop/web@sha256:" + "2" * 64,
    ports=[8080],
)
app.service(web, port=80, target_port=8080)

pipeline = Pipeline(
    app,
    target,
    state_dir=".piceli-deploy",
    execution={"readiness_seconds": 10, "poll_seconds": 0.05},
)
