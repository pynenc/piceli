The plan refused with `resource-requires-adoption`: `Deployment/web` and
`Service/web` already exist but are not managed by this release, so Piceli will
not change them until you authorize a takeover (`adopt`, same objects, fields
nobody declares are removed) or a replacement (delete and recreate). You want to
keep them, so the pipeline adopts both:

```python
"""The shop app and how it is deployed (to a test cluster listening on 127.0.0.1)."""

from piceli import App, Pipeline, Target

target = Target.kubeconfig(
    "cluster.kubeconfig",  # in this directory
    context="test",
    namespace="piceli-test",
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
    adopt=["Deployment/web", "Service/web"],
    state_dir=".piceli-deploy",
    execution={"readiness_seconds": 10, "poll_seconds": 0.05},
)
```

```bash
piceli explain resource-requires-adoption --json
piceli deploy app.py:pipeline --plan --json
# show the owner the adopt lines and wait for the approval of the combined hash
piceli deploy app.py:pipeline --approve <hash> --json
```
