# Autoscaler Templates

In Kubernetes, autoscalers are crucial for dynamically adjusting the resources and replicas of pods to meet the current demand. Piceli provides templates for both Horizontal Pod Autoscaler (HPA) and Vertical Pod Autoscaler (VPA), allowing users to automate the scaling of pod replicas based on observed CPU utilization or other metrics (HPA) and adjust pod resources to optimize for resource utilization (VPA). These templates simplify defining autoscaling behaviors programmatically, ensuring that applications remain responsive under varying loads while making efficient use of cluster resources.

## Horizontal Pod Autoscaler (HPA)

The `HorizontalPodAutoscaler` template is used to define a Kubernetes Horizontal Pod Autoscaler (HPA) object. It automates the scaling of the number of pod replicas in a deployment, replication controller, stateful set, or replica set based on observed CPU utilization or custom metrics.

### HPA Properties

- `name`: The name of the Horizontal Pod Autoscaler.
- `target_kind`: The kind of target to scale (e.g., Deployment, ReplicaSet).
- `target_name`: The name of the target to scale.
- `min_replicas`: The minimum number of replicas.
- `max_replicas`: The maximum number of replicas.
- `target_cpu_utilization_percentage`: The target CPU utilization percentage to trigger scaling.
- `labels`: Optional labels to apply to the HPA object.

### HPA Example

This example creates an HPA object targeting a Deployment named "my-app", designed to maintain CPU utilization at or below 80%.

```python
hpa = HorizontalPodAutoscaler(
    name="my-hpa",
    target_kind="Deployment",
    target_name="my-app",
    min_replicas=1,
    max_replicas=10,
    target_cpu_utilization_percentage=80
)
```

## Vertical Pod Autoscaler (VPA)

The `VerticalPodAutoscaler` (VPA) template automates the process of adjusting the CPU and memory reservations for the pods in a deployment, stateful set, or replica set based on usage. Unlike the Horizontal Pod Autoscaler, the VPA adjusts the resources requested by pods, potentially improving resource utilization.

### VPA Properties

- `name`: The name of the Vertical Pod Autoscaler.
- `target_kind`: The kind of target for the VPA (e.g., Deployment, StatefulSet).
- `target_name`: The name of the target for the VPA.
- `container_name`: Optional name of the specific container to target within the pod.
- `min_allowed`: The minimum resources allowed for each container.
- `max_allowed`: The maximum resources allowed for each container.
- `control_cpu`: Boolean indicating whether the CPU resource should be managed.
- `control_memory`: Boolean indicating whether the memory resource should be managed.

### VPA Example

This example creates a VPA object for a Deployment named "my-app", with specified minimum and maximum CPU and memory resources, and enables control over both CPU and memory resources.

```python
vpa = VerticalPodAutoscaler(
    name="my-vpa",
    target_kind="Deployment",
    target_name="my-app",
    min_allowed=resource_request.Resources(cpu="500m", memory="256Mi"),
    max_allowed=resource_request.Resources(cpu="2000m", memory="1Gi"),
    control_cpu=True,
    control_memory=True
)
```
