```bash
python3 -m venv .venv
. .venv/bin/activate
pip install piceli
piceli render app.py:app --namespace hello
```

`piceli render` imports `app.py` and prints the manifests; it never contacts a
cluster and never reads secret values.

```python
from piceli import App

app = App("hello")
hello = app.deployment(
    "hello",
    image=(
        "docker.io/library/nginx:1.27"
        "@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    ),
    ports=[80],
    ready=app.probe.http("/", 80),
)
app.service(hello, port=80)
```
