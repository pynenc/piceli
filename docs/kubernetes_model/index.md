# Kubernetes Model

The Kubernetes model in Piceli offers a flexible and powerful way to define and manage your Kubernetes objects. Piceli supports several methods for defining Kubernetes objects, catering to different preferences and project requirements. These include using Piceli templates, the official Kubernetes Python library, and YAML or JSON files.

## Defining Kubernetes Objects

Piceli provides various avenues for defining Kubernetes objects:

- **Piceli Templates**: Utilize Python classes to define Kubernetes objects in a programmatic and expressive manner. This method is highly recommended for Python projects.

- **Kubernetes Python Library**: Leverage the official Kubernetes Python client library for defining objects. This approach offers direct access to the Kubernetes API through Python.

- **YAML and JSON Files**: Define your Kubernetes objects in YAML or JSON format. Piceli can load and manage these objects directly, making it a versatile option for projects that prefer configuration files.

```{toctree}
:hidden:
:maxdepth: 2
:caption: Kubernetes Model Sections

piceli_templates/index
kubernetes_python_library
yaml_json_definitions
```
