from piceli import App, Checks, Pipeline, Target

target = Target.kubeconfig(
    "hello.kubeconfig",  # an explicit file; never ~/.kube/config
    context="kind-hello",  # an explicit context; never current-context
    namespace="hello",
)

app = App("hello")
web = app.deployment(
    "web",
    image=(
        "docker.io/library/nginx:1.27"
        "@sha256:6784fb0834aa7dbbe12e3d7471e69c290df3e6ba810dc38b34ae33d3c1c05f7d"
    ),
    ports=[80],
    ready=app.probe.http("/", 80),
)
app.service(web, port=80, access=app.access.forward(local=18080, health="/"))

pipeline = Pipeline(app, target, checks=Checks.http(web, "/", expect=200))
