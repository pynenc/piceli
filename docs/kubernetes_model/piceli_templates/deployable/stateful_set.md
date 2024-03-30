# StatefulSet

`StatefulSet` in Piceli is a simplification of the Kubernetes `StatefulSet` resource. It extends `ReplicaManager` to facilitate the management of stateful applications, ensuring ordered and graceful deployment and scaling.

## Properties

- `replicas`: Overrides the default number of replicas from `ReplicaManager` to `2` for stateful sets.

## Methods

Inherits and utilizes methods from `ReplicaManager`, with an overridden implementation for `get_replica_manager()` to create stateful sets.

## Overridden Methods

- `get_replica_manager()`: Constructs and returns a `V1StatefulSet` object, including configurations for persistent volume claim templates, pod management policies, and service names necessary for managing stateful applications.

## Usage

The `StatefulSet` class provides a structured approach to deploying and managing stateful applications in Kubernetes.

### Example

```python
stateful_set = StatefulSet(
    name="example-statefulset",
    replicas=2,
    # Additional configurations
)
```

This example demonstrates defining a Kubernetes StatefulSet with 2 replicas, incorporating specific configurations for stateful application management.
