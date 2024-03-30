# Container

Defines the configuration for a container within a Kubernetes pod, as managed by Piceli.

Containers are the basic executable units in a Kubernetes pod, encapsulating the application code, runtime environment, libraries, and their dependencies. The `Container` class in Piceli provides a declarative approach to defining these configurations.

Utilized within the {pod}`./pod` class for defining container specifications within Kubernetes pods.

## Properties

- `name`: The unique name of the container within the pod.
- `image`: The Docker image to use for the container.
- `command`: The command to run in the container (overrides the image's default command).
- `args`: Arguments to the command.
- `image_pull_policy`: Policy for pulling images.
- `ports`: List of ports the container exposes.
- `env`: Environment variables to set in the container.
- `volumes`: Volumes to mount into the container.
- `liveness_pre_stop_command`: Commands run before stopping the container.
- `liveness_post_start_command`: Commands run after starting the container.
- `readiness_command`: Command to determine the readiness of the container.
- `liveness_command`: Command to determine the liveness of the container.
- `resources`: Resource requests and limits for the container.
- `env_sources`: ConfigMaps or Secrets used as sources for environment variables.
- `security_context_uid`: User ID to run the container under.

This class is used in conjunction with the {pod}`./pod` class to define containers within pods, ensuring that each container is configured with the necessary resources, environment variables, and policies for its operation within the Kubernetes cluster.

### Example Usage

```python
from piceli.k8s import templates

# Define a container for a web server
web_server_container = templates.Container(
    name="web-server",
    image="nginx:latest",
    ports=[templates.Port(name="http", port=80)],
    resources=templates.Resources(cpu="500m", memory="256Mi"),
    env={"NGINX_PORT": "80"},
)
```

In this example, a container named "web-server" is defined using the nginx:latest image, exposing port 80, with specified resource requests, and setting an environment variable for the Nginx port.
