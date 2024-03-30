# Labels

Defines and validates Kubernetes labels for resources managed by Piceli. Labels are key/value pairs attached to objects, such as pods and services, used for identifying attributes of those objects that are meaningful and relevant to users.

Labels and annotations serve as additional information for Kubernetes objects, allowing for more efficient management and operation of clusters.

## Features

- **Standard Label Validation**: Ensures labels conform to Kubernetes' label syntax requirements.
- **Cluster Label Validation**: Special validation for specific cluster-level labels, following best practices for cloud providers like GCP.

## Utility Functions

- `validate_cluster_label(key: str, value: str)`: Validates the format of cluster-specific labels according to GCP's requirements.
- `validate_label(key: str, value: str)`: Validates the format of standard Kubernetes labels.
- `check_labels(v: dict[str, str])`: Validates a dictionary of labels, applying the appropriate validation rules based on label keys.

## Usage

Labels are utilized throughout Kubernetes objects defined in Piceli, enhancing their manageability and interoperability within Kubernetes and cloud environments.

- {pod}`auxiliary/pod`
- {replica_manager}`auxiliary/replica_manager`
- {autoscaler}`deployable/autoscaler`
- {configmap}`deployable/configmap`
- {job}`deployable/job`
- {role}`deployable/role`
- {role_binding}`deployable/role_binding`
- {secret}`deployable/secret`
- {service}`deployable/service`
- {service_account}`deployable/service_account`
- {volume}`deployable/volume`

### Example Usage

Creating and validating labels for a Kubernetes pod:

```python
from piceli.k8s.templates.auxiliary import labels

# Define a set of labels
pod_labels = labels.Labels({
    "app": "my-application",
    "component": "web-server",
    "environment": "production"
})

# Validate labels
validated_labels = labels.check_labels(pod_labels)
```

In this example, a set of labels is defined and validated for use with a Kubernetes pod, ensuring they adhere to Kubernetes' and GCP's labeling requirements. This functionality is integral to managing Kubernetes resources within Piceli, facilitating the organization, selection, and operation of resources across clusters.
