import logging

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.templates.auxiliary import names, quantity
from piceli.k8s.templates.deployable import configmap, secret

logger = logging.getLogger(__name__)


class ValueFromField(BaseModel):
    """
    Represents a mechanism to set an environment variable from a PodSpec field.

    :param names.FieldPath field_path: The path of the field from which to derive the environment variable value.
    """

    field_path: names.FieldPath

    def get(self) -> client.V1EnvFromSource:
        """
        Creates a Kubernetes environment variable source from a field reference.

        :return: A Kubernetes environment variable source object.
        """
        return client.V1EnvVarSource(
            field_ref=client.V1ObjectFieldSelector(field_path=self.field_path)
        )


class ValueFromResourceField(BaseModel):
    """
    Allows setting an environment variable from a resource field like CPU or memory limits.

    :param names.Name container_name: The name of the container for which the resource field is referenced.
    :param quantity.Quantity divisor: The divisor used when setting resource-based environment variables.
    :param names.FieldPath resource: The specific resource field to reference for the environment variable.
    """

    container_name: names.Name
    divisor: quantity.Quantity
    resource: names.FieldPath

    def get(self) -> client.V1EnvFromSource:
        """
        Generates a Kubernetes environment variable source from a resource field reference.

        :return: A Kubernetes environment variable source object.
        """
        return client.V1EnvVarSource(
            resource_field_ref=client.V1ResourceFieldSelector(
                container_name=self.container_name,
                divisor=self.divisor,
                resource=self.resource,
            )
        )


def get_env_pair(key: str, value: str) -> client.V1EnvVar:
    """
    Creates a simple environment variable key-value pair.

    :param str key: The name of the environment variable.
    :param str value: The value of the environment variable.
    :return: A Kubernetes environment variable object.
    """
    return client.V1EnvVar(key, value)


def get_env_from_dict(data: dict) -> list[client.V1EnvVar]:
    """
    Converts a dictionary of environment variable names and values to a list of Kubernetes environment variable objects.

    :param dict data: A dictionary where keys are environment variable names and values are the environment variable values.
    :return: A list of Kubernetes environment variable objects.
    """
    env = []
    for key, value in data.items():
        if isinstance(value, client.V1EnvVarSource):
            env.append(client.V1EnvVar(name=key, value_from=value))
        else:
            env.append(get_env_pair(key, value))
    return env


def describe_envvar(env_var: client.V1EnvVar) -> str:
    """
    Provides a basic description of a Kubernetes environment variable, useful for logging and debugging.

    :param client.V1EnvVar env_var: The environment variable to describe.
    :return: A string description of the environment variable.
    """
    if not env_var.value_from:
        return f"V1EnvVar({env_var.name} with a direct value)"
    if ref := env_var.value_from.config_map_key_ref:
        return f"V1EnvVar({env_var.name} from config-map:{ref})"
    if ref := env_var.value_from.secret_key_ref:
        return f"V1EnvVar({env_var.name} from secret:{ref})"
    return f"V1EnvVar({env_var.name} from unknown type)"


def upsert_envvars(
    base_env: list[client.V1EnvVar], new_env: list[client.V1EnvVar]
) -> list[client.V1EnvVar]:
    """
    Merges two lists of environment variables, with variables in the new list updating or adding to those in the base list.

    :param list[client.V1EnvVar] base_env: The base list of environment variables.
    :param list[client.V1EnvVar] new_env: The new environment variables to upsert into the base list.
    :return: A list of merged and upserted environment variables.
    """
    env_map = {item.name: item for item in base_env}
    for new_item in new_env:
        if new_item.name in env_map:
            logger.warning(
                f"replacing existing {describe_envvar(env_map[new_item.name])} by new {describe_envvar(new_item)}"
            )
        else:
            logger.debug(
                f"Adding new {describe_envvar(new_item)} to environment variables"
            )
        env_map[new_item.name] = new_item
    return list(env_map.values())


def get_env_from_source(
    sources: list[configmap.ConfigMap | secret.Secret],
) -> list[client.V1EnvVar]:
    """
    Generates a list of environment variables based on the specified list of ConfigMaps or Secrets.

    :param list[configmap.ConfigMap | secret.Secret] sources: The ConfigMaps or Secrets to source environment variables from.
    :return: A list of Kubernetes environment variable objects.
    """
    list_envs = []

    for source in sources:
        keys = []
        if isinstance(source, configmap.ConfigMap):
            selector = client.V1ConfigMapKeySelector
            key_ref = "config_map_key_ref"
            keys = list(source.data.keys())
        elif isinstance(source, secret.Secret):
            selector = client.V1SecretKeySelector
            key_ref = "secret_key_ref"
            if source.string_data:
                keys = list(source.string_data.keys())
            if source.data:
                keys.extend(list(source.data.keys()))
        else:
            raise ValueError(f"Unexpected {source=}")
        for key in keys:
            map_ref = selector(key=key, name=source.name)
            env_var = client.V1EnvVarSource(**{key_ref: map_ref})
            env_object = client.V1EnvVar(name=key, value_from=env_var)
            list_envs.append(env_object)

    return list_envs
