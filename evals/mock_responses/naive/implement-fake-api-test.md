```python
from piceli.testing import FakeCluster


def test_settings():
    cluster = FakeCluster()
    cluster.create_configmap("settings", {"mode": "on"})
    assert cluster.get_configmap("settings")["mode"] == "on"
```
