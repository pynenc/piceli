# Getting Started

Welcome to the initial setup guide for Piceli, designed to facilitate a smooth introduction to managing your Kubernetes and cloud infrastructure. This guide will walk you through the installation process, different ways to define Kubernetes objects, and the basics of deploying with Piceli.

## Installation

Start by installing Piceli using pip with the following command:

```bash
pip install piceli
```

Or add it to a project managed with [uv](https://docs.astral.sh/uv/):

```bash
uv add piceli
```

To work from a source checkout, see {doc}`../contributing/index`.

```{note}
The PyPI release can lag behind the documentation while the project is pre-alpha.
If an API described here is missing, install from the `main` branch:
`pip install git+https://github.com/pynenc/piceli.git`.
```

## Quick Start

Piceli supports defining Kubernetes objects using Piceli templates, the official Kubernetes Python client, YAML, JSON files, or a combination of these methods. Here's how to get started with each approach.

1. **Using Piceli Templates**:

   Define a Kubernetes job in a Python file (e.g., `define_job.py`) using Piceli's templates:

   ```python
   from piceli.k8s import templates

   job = templates.Job(
       name="job0",
       containers=[
           templates.Container(
               name="c0", command=["python", "--version"], image="python:latest",
           )
       ],
   )
   ```

2. **With the Kubernetes Python Library**:

   Alternatively, utilize the Kubernetes client in a Python file (e.g., `k8s_job.py`):

   ```python
   from kubernetes import client

   job = client.V1Job(
       api_version="batch/v1",
       kind="Job",
       metadata=client.V1ObjectMeta(name="job0"),
       spec=client.V1JobSpec(
           template=client.V1PodTemplateSpec(
               metadata=client.V1ObjectMeta(name="job0"),
               spec=client.V1PodSpec(
                   containers=[client.V1Container(
                       name="c0",
                       image="python:latest",
                       command=["python", "--version"],
                   )],
               ),
           ),
       ),
   )
   ```

3. **Defining Jobs with YAML or JSON Files**:

   Kubernetes objects can be defined in YAML or JSON files (e.g., `job.yaml` or `job.json`):

   ```yaml
   apiVersion: batch/v1
   kind: Job
   metadata:
     name: job0
   spec:
     template:
       spec:
         containers:
           - name: c0
             image: python:latest
             command: ["python", "--version"]
         restartPolicy: Never
   ```

## Configuration and Using Piceli CLI

Piceli accommodates multiple sources for Kubernetes object definitions simultaneously, including YAML, JSON, Piceli templates, and Kubernetes Python client objects. Set `PICELI__FOLDER_PATH` for YAML/JSON directories, and use `PICELI__MODULE_NAME` or `PICELI__MODULE_PATH` for Python modules:

```bash
PICELI__FOLDER_PATH=/path/to/your/definitions PICELI__MODULE_NAME=your_module_name PICELI__MODULE_PATH=/path/to/your/module python -m piceli model list
```

### Configuration via pyproject.toml

If you're using Piceli as part of another Python project, you can define your configuration in the `pyproject.toml` file under the `[tool.piceli]` table. This is particularly useful for maintaining a clean and centralized project setup.

```toml
   [tool.piceli]
   module_name = "path.to.model"
   module_path = "path/to/model.py"
   folder_path = "path/to/yamls/"
   sub_elements = true # include sub folders/modules
```

Here are some CLI commands for common Piceli operations:

```{warning}
`deploy run` uses the CLI engine, which **deletes and recreates** objects that
already exist. Always review `deploy detail` first, and use a disposable namespace
while evaluating Piceli. See {doc}`../overview` for the difference between the CLI
engine and the recoverable engine.
```

- **Deploying a Namespace**:

  ```bash
  PICELI__FOLDER_PATH=/path/to/your/definitions PICELI__NAMESPACE=test-run python -m piceli deploy run
  ```

- **Showing Detailed Deployment Plan**:

  ```bash
  PICELI__FOLDER_PATH=/path/to/your/definitions PICELI__NAMESPACE=test-run python -m piceli deploy detail
  ```

- **Hide No Action Required Details in Deployment Plan**:

  ```bash
  PICELI__FOLDER_PATH=/path/to/your/definitions PICELI__NAMESPACE=test-run python -m piceli deploy detail -hna
  ```

## Next steps

- {doc}`../overview` covers the mental model, the two execution engines and a glossary.
- {doc}`../kubernetes_model/piceli_templates/index` lists every template and its options.
- {doc}`../deployment_planning` covers durable, resumable deployments from Python.
- {doc}`../cli/index` is the full command reference.
