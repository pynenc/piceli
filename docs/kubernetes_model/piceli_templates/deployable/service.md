# Service and ServicePort

Efficiently Manage Kubernetes Services with Piceli

A Kubernetes `Service` is an abstraction that defines a logical set of Pods and a policy by which to access them. This abstraction enables pod-to-pod communication and external-to-pod communication. The `Service` class in Piceli simplifies defining and managing these services, allowing for easy setup of networking within Kubernetes clusters.

## ServicePort

Before defining a service, it's essential to specify the ports it exposes. The `ServicePort` class in Piceli provides a straightforward way to define these details.

### ServicePort Properties

- `name`: The name of the port, used for identification.
- `port`: The port number that the service will expose.
- `target_port`: The target port on the pods that the service forwards to.

## Service

The `Service` class represents a Kubernetes Service, facilitating the creation of network routes to pods based on labels and selectors.

### Service Properties

- `name`: The unique name of the service.
- `ports`: A list of `ServicePort` objects defining the ports exposed by the service.
- `selector`: A dictionary defining how the service selects pods. It matches labels on pods to include in the service.
- `labels`: Optional labels for the service, used for organization and selection within Kubernetes.

### Example

```python
from piceli.k8s import templates

# Define the service ports
service_port = templates.ServicePort(
    name="http",
    port=80,
    target_port=8080
)

# Create the service
my_service = templates.Service(
    name="my-service",
    ports=[service_port],
    selector={"app": "myApp"},
    labels={"environment": "production"}
)
```

This example outlines creating a Kubernetes Service named "my-service" that routes external traffic on port 80 to port 8080 on pods labeled with app: myApp. The service itself is labeled as part of the production environment.
