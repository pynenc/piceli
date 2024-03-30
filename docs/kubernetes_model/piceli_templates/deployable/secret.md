# Secret

**Overview of Managing Kubernetes Secrets with Piceli**

Kubernetes secrets are used to store and manage sensitive information, such as passwords, OAuth tokens, and ssh keys, allowing for more secure application configuration and deployment. The `Secret` class in Piceli simplifies the creation and management of these Kubernetes Secret objects.

## Properties

- `name`: The unique name of the secret, adhering to DNS subdomain name conventions.
- `secret_type`: The type of the secret (e.g., Opaque, kubernetes.io/dockerconfigjson), defining how the secret is interpreted and used by Kubernetes.
- `string_data`: An optional dictionary where keys represent data field names and values are unencoded strings. This attribute provides a way to specify secret data in its unencoded form.
- `data`: An optional dictionary where keys represent data field names and values are base64-encoded strings. It's an alternative to `string_data` for providing encoded secret content.
- `labels`: Optional labels to organize and select secrets within Kubernetes.

## Creating a Secret

The `get` method constructs a `V1Secret` Kubernetes object ready for deployment. It encapsulates the necessary configurations, including the secret type, metadata, and the actual secret data.

### Handling Docker Credentials

Piceli's `Secret` class includes utility methods for generating secrets to store Docker registry credentials. The `get_docker_json_data` method formats Docker credentials into the required `.dockerconfigjson` format, while `get_docker_json_secret` creates a secret object specifically for Docker registry authentication.

### Example

```python
from piceli.k8s import templates

# Creating a generic secret
generic_secret = templates.Secret(
        name="test-secret",
        secret_type=constants.SecretType.OPAQUE,
        string_data={"KEY0": "VALUE0"},
    )

# Creating a Docker registry secret
docker_auth = (
    "eyJhdXRocyI6eyJnY3IuaW8iOnsiYXV0aCI6ImRtVnllVk5sWTNWeVpWQmhjM009In19fQ=="
)
secret = templates.Secret.get_docker_json_secret(
    "docker-secret", docker_auth=docker_auth
)
```

These examples illustrate how to define a generic secret with unencoded data and how to create a Docker registry secret for Kubernetes, facilitating secure access to private Docker registries.
