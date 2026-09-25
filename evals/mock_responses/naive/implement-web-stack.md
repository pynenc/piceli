```python
from piceli import App, HorizontalPodAutoscaler

app = App("shop")
web = app.deployment(
    "web",
    image="registry.example/shop/web@sha256:" + "3" * 64,
    port=8080,
    replicas=2,
)
app.hpa(web, min=2, max=10, cpu_percent=70)
db = app.stateful_set(
    "db",
    image="docker.io/library/postgres:16.4@sha256:" + "4" * 64,
    env={"POSTGRES_PASSWORD": "postgres"},
    volume_size="10Gi",
)
```

```bash
piceli render app.py
```
