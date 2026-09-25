```python
from piceli import App, ClaimTemplate, Random, Resources, Secrets

WEB = "registry.example/shop/web@sha256:" + "3" * 64
POSTGRES = "docker.io/library/postgres:16.4@sha256:" + "4" * 64

# Generated at plan time into Piceli's private store; rendered as <redacted>.
secrets = Secrets(db_password=Random(24))

app = App("shop")
credentials = app.secret("db-credentials", {"password": secrets.ref("db_password")})

db = app.stateful_set(
    "db",
    image=POSTGRES,
    ports=[5432],
    env={
        "POSTGRES_PASSWORD": credentials.key("password"),
        "PGDATA": "/var/lib/postgresql/data/pgdata",
    },
    volumes={"/var/lib/postgresql/data": ClaimTemplate("data", size="10Gi")},
    ready=app.probe.tcp(5432),
)

web = app.deployment(
    "web",
    image=WEB,
    ports=[8080],
    ready=app.probe.http("/healthz", 8080),
    resources=Resources(cpu="100m", memory="128Mi"),
    env={"DATABASE_HOST": "db", "DATABASE_PASSWORD": credentials.key("password")},
)
app.service(web, port=80, target_port=8080)
app.autoscaler(web, min_replicas=2, max_replicas=10, cpu=70)
app.depends(web, on=db)
```

```bash
piceli render app.py:app
```

The StatefulSet gets its headless Service `db` automatically, and the password
exists only as a reference: `Secrets(...)` belongs to the `Pipeline` that deploys
the app (`Pipeline(app, target, secrets=secrets)`).
