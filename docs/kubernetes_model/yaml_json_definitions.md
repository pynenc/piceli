# Defining Kubernetes Objects with YAML and JSON

Piceli offers the flexibility to manage Kubernetes resources using YAML or JSON files, similar to the functionality provided by `kubectl`. This feature allows users to define and deploy Kubernetes objects directly from YAML or JSON definitions, facilitating a seamless integration into existing workflows and supporting a broad range of Kubernetes resource types.

## Overview

YAML and JSON are widely used for defining Kubernetes objects due to their readability and compatibility with the Kubernetes API. Piceli leverages this standardization, enabling the deployment of resources defined in YAML or JSON without requiring conversion to other formats. This capability ensures that you can use Piceli alongside kubectl and other Kubernetes management tools, maintaining consistency across your DevOps toolchain.

## Example: Defining a Kubernetes Service in YAML

Below is an example of a Kubernetes Service defined in YAML, which can be managed through Piceli:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: test-service
  labels:
    service: test-service
    component: test-service
spec:
  ports:
    - name: test-service
      port: 5432
      targetPort: 5432
    - name: test-service-2
      port: 5433
      targetPort: 5434
  type: ClusterIP
  selector:
    pod_name: pod-to-select
```

This Service definition specifies a ClusterIP type service named test-service, which routes traffic to the specified ports on pods labeled with pod_name: pod-to-select. The YAML format allows for clear and structured definition of the service, including its metadata, specifications, ports, and selector criteria.

## Integration with Piceli

Load a folder of YAML or JSON files with `piceli.k8s.ops.loader` and put the
objects into a composition with `component_from_objects`. The composition
function is what `piceli render` and `piceli release` run:

```python
from pathlib import Path

from piceli.k8s.ops import loader
from piceli.k8s.ops.plan import DeploymentComposition, component_from_objects

MANIFESTS = Path(__file__).parent / "manifests"


def build(ctx):
    objects = loader.load_all(
        module_name="", module_path="", folder_path=str(MANIFESTS)
    )
    return DeploymentComposition((component_from_objects("app", objects),))
```

```bash
piceli render infra.py:build --namespace my-app
```

`loader.load_all` also accepts `module_name` (dotted import path) and
`module_path` (a file or folder of Python modules), and loads every Piceli
template and `kubernetes.client` object they define. `sub_elements=False`
skips sub-folders and sub-modules. Key considerations:

- File Organization: Organize your YAML and JSON files in a way that aligns with your project structure and deployment strategy.
- Validation: Ensure your YAML and JSON files are correctly formatted and valid according to Kubernetes API standards to avoid deployment errors.
- Compatibility: Check that your object definitions are compatible with the version of Kubernetes you are using, as API versions and resource specifications can vary.

## Conclusion

By supporting YAML and JSON Kubernetes object definitions, Piceli enhances its versatility as an infrastructure management tool. This feature bridges the gap between traditional kubectl workflows and the advanced deployment capabilities of Piceli, providing users with a comprehensive solution for Kubernetes resource management.
