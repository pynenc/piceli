# Defining Kubernetes Objects with YAML and JSON

Piceli Docs offers the flexibility to manage Kubernetes resources using YAML or JSON files, similar to the functionality provided by `kubectl`. This feature allows users to define and deploy Kubernetes objects directly from YAML or JSON definitions, facilitating a seamless integration into existing workflows and supporting a broad range of Kubernetes resource types.

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

To use YAML or JSON definitions with Piceli, simply include your files in the designated directory or reference them in your Piceli configurations. Piceli will process these files, ensuring that your Kubernetes objects are deployed as defined. Key considerations include:

- File Organization: Organize your YAML and JSON files in a way that aligns with your project structure and deployment strategy.
- Validation: Ensure your YAML and JSON files are correctly formatted and valid according to Kubernetes API standards to avoid deployment errors.
- Compatibility: Check that your object definitions are compatible with the version of Kubernetes you are using, as API versions and resource specifications can vary.

## Conclusion

By supporting YAML and JSON Kubernetes object definitions, Piceli enhances its versatility as an infrastructure management tool. This feature bridges the gap between traditional kubectl workflows and the advanced deployment capabilities of Piceli, providing users with a comprehensive solution for Kubernetes resource management.
