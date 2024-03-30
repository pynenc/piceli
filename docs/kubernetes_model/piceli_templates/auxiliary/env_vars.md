# Environment Variables

Defines mechanisms for setting environment variables in Kubernetes containers, as managed by Piceli. These can be directly set to static values or dynamically sourced from Kubernetes resources like ConfigMaps and Secrets.

## Overview

Environment variables are key for configuring containerized applications, allowing them to adapt to their runtime environment. Piceli supports both static and dynamic setting of environment variables, enhancing flexibility and security in application configuration.

## Features

- **Static Environment Variables**: Direct assignment of values to environment variables.
- **Dynamic Environment Variables**:
  - **ValueFromField**: Set an environment variable from a field in the pod spec.
  - **ValueFromResourceField**: Set an environment variable from resource fields like CPU and memory limits.

## Utility Functions

- `get_env_pair(key: str, value: str)`: Creates an environment variable with a static value.
- `get_env_from_dict(data: dict)`: Converts a dictionary into a list of environment variables, suitable for direct use in container configurations.
- `describe_envvar(env_var: client.V1EnvVar)`: Provides a description of an environment variable, aiding in logging and debugging.
- `upsert_envvars(base_env: list, new_env: list)`: Merges two lists of environment variables, with `new_env` variables updating or adding to those in `base_env`.
- `get_env_from_source(sources: list)`: Generates environment variables from sources like ConfigMaps or Secrets, enabling dynamic configuration based on cluster resources.

These functionalities are integrated into the {container}`./container` module for defining container environments within pods, facilitating both fixed and dynamic configurations.

## Usage Example

Defining a mix of static and dynamic environment variables for a container:

```python
from piceli.k8s import templates

# Static environment variables
env_vars_static = {
    "LOG_LEVEL": "info",
    "APP_MODE": "production"
}

# Dynamic environment variables from a ConfigMap
config_map_name = "app-config"
env_vars_from_config_map = templates.get_env_from_source([templates.configmap.ConfigMap(name=config_map_name)])

# Combine static and dynamic environment variables
env_vars = templates.upsert_envvars(
    base_env=templates.get_env_from_dict(env_vars_static),
    new_env=env_vars_from_config_map
)
```

This example showcases how to define environment variables within a Piceli-managed Kubernetes container, leveraging both static values and dynamically sourced configurations.
