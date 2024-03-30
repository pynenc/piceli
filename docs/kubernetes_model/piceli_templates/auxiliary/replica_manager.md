# ReplicaManager

The `ReplicaManager` is an abstract base class in Piceli designed to unify and simplify the definition of Kubernetes `Deployment` and `StatefulSet` objects. It incorporates common properties and methods required for managing replicas and their associated services in a Kubernetes cluster.

## Key Features

- `restart_policy`: Defines the restart policy for the pods. Defaults to `Always`.
- `replicas`: Specifies the number of pod replicas. Defaults to `1`.
- `create_service`: A boolean indicating whether a service should be automatically created for the replica manager.
- `hpa`: An optional `HorizontalPodAutoscaler` configuration.
- `vpa`: An optional `VerticalPodAutoscaler` configuration.
- `labels`: Optional labels to be applied to the replica manager.

## Methods

- `get_replica_manager()`: Abstract method that must be implemented by subclasses to define the specific replica manager (`Deployment` or `StatefulSet`).
- `get_service()`: Constructs and returns a `Service` object associated with the replica manager, if `create_service` is `True`.
- `get_hpa()`: Retrieves the configured `HorizontalPodAutoscaler` object.
- `get_vpa()`: Retrieves the configured `VerticalPodAutoscaler` object.
- `get()`: Aggregates and returns a list of Kubernetes objects that need to be created or managed, including the replica manager itself, optional services, HPA, and VPA.

## Usage

`ReplicaManager` serves as a foundation for `Deployment` and `StatefulSet` classes in Piceli, enabling them to inherit and extend its functionalities. It is not meant to be instantiated directly but through its concrete subclasses.
