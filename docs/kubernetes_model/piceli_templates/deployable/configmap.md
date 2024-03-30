# ConfigMap

**A representation of a Kubernetes ConfigMap object within Piceli.**

ConfigMaps allow you to decouple configuration artifacts from image content to keep containerized applications portable. The `ConfigMap` class in Piceli facilitates the creation and management of ConfigMaps, enabling you to store configuration data as key-value pairs and consume them in your pods or use them to set command-line arguments for your containers.

## Properties

- `name`: The name of the ConfigMap, defined using the `names.Name` type to ensure it adheres to Kubernetes naming conventions.
- `data`: A dictionary where each key-value pair represents the data stored in the ConfigMap.
- `labels`: Optional labels to apply to the ConfigMap, enhancing its discoverability and organization within Kubernetes.

### Examples

```python
config_map = ConfigMap(
    name="example-configmap",
    data={
        "configKey": "configValue",
        "settings.json": "{\"key\": \"value\"}"
    }
)
```

This example demonstrates the creation of a ConfigMap named "example-configmap" with two entries in its data: a simple key-value pair and a JSON configuration stored as a string. This ConfigMap can then be used by pods to externalize configuration details.
