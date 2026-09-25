```python
from piceli import App, Resources

app = App("shop")
settings = app.config("settings", {"LOG_LEVEL": "debug"})
api = app.deployment(
    "api",
    image="registry.example/shop/api@sha256:" + "5" * 64,
    ports=[8080],
    env={"LOG_LEVEL": settings.key("LOG_LEVEL")},
    resources=Resources(cpu="100m", memory="128Mi"),
)
app.service(api, port=80, target_port=8080)

app.environment(
    "staging",
    overrides={
        "namespace": "shop-staging",
        "replicas": 3,
        "env": {"LOG_LEVEL": "info"},
    },
)
```

```bash
piceli render app.py:app --environment staging
```
