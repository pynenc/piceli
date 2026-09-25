```python
from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.testing import fake_cluster, manifest


def test_settings_are_readable() -> None:
    with fake_cluster() as cluster:
        cluster.api.put(manifest("ConfigMap", "settings", value="on"))
        found = cluster.provider.get(
            ResourceIdentity("v1", "ConfigMap", cluster.namespace, "settings")
        )
        assert found.manifest["data"] == {"mode": "on"}
```
