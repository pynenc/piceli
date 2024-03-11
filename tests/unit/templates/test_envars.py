import pytest
from kubernetes import client

from piceli.k8s.constants import secret_type
from piceli.k8s.templates.auxiliary import env_vars
from piceli.k8s.templates.deployable import configmap, secret

CONFIGMAP = configmap.ConfigMap(name="config0", data={"var0": "val0"})
SECRET = secret.Secret(
    name="secret0",
    secret_type=secret_type.SecretType.GENERIC,
    string_data={"var1": "val1"},
    data={"var2": "val2"},
)


def test_get_env_from_source() -> None:
    env = env_vars.get_env_from_source([CONFIGMAP, SECRET])
    assert len(env) == 3

    env_var0 = client.V1EnvVar(
        name="var0",
        value_from=client.V1EnvVarSource(
            config_map_key_ref=client.V1ConfigMapKeySelector(key="var0", name="config0")
        ),
    )
    assert env[0] == env_var0
    env_var1 = client.V1EnvVar(
        name="var1",
        value_from=client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(key="var1", name="secret0")
        ),
    )
    assert env[1] == env_var1
    env_var2 = client.V1EnvVar(
        name="var2",
        value_from=client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(key="var2", name="secret0")
        ),
    )
    assert env[2] == env_var2


@pytest.mark.parametrize(
    "base_env,new_env,expected_result",
    [
        # Test adding a new environment variable
        (
            [client.V1EnvVar(name="EXISTING_VAR", value="1")],
            [client.V1EnvVar(name="NEW_VAR", value="2")],
            [{"name": "EXISTING_VAR", "value": "1"}, {"name": "NEW_VAR", "value": "2"}],
        ),
        # Test updating an existing environment variable
        (
            [client.V1EnvVar(name="EXISTING_VAR", value="1")],
            [client.V1EnvVar(name="EXISTING_VAR", value="2")],
            [{"name": "EXISTING_VAR", "value": "2"}],
        ),
        # Test no changes when new_env is empty
        (
            [client.V1EnvVar(name="EXISTING_VAR", value="1")],
            [],
            [{"name": "EXISTING_VAR", "value": "1"}],
        ),
    ],
)
def test_upsert_envvars(base_env: list, new_env: list, expected_result: list) -> None:
    result = env_vars.upsert_envvars(base_env, new_env)
    # Convert result to list of dicts for easier comparison
    result_as_dicts = [
        {"name": env_var.name, "value": env_var.value} for env_var in result
    ]
    assert result_as_dicts == expected_result


@pytest.mark.parametrize(
    "env_var,expected_description",
    [
        # Test case for direct value
        (
            client.V1EnvVar(name="DIRECT_VAR", value="direct_value"),
            "V1EnvVar(DIRECT_VAR with a direct value)",
        ),
        # Test case for config-map reference
        (
            client.V1EnvVar(
                name="CONFIG_VAR",
                value_from=client.V1EnvVarSource(
                    config_map_key_ref=client.V1ConfigMapKeySelector(
                        name="config_map", key="config_key"
                    )
                ),
            ),
            "V1EnvVar(CONFIG_VAR from config-map:{'key': 'config_key', 'name': 'config_map', 'optional': None})",
        ),
        # Test case for secret-key reference
        (
            client.V1EnvVar(
                name="SECRET_VAR",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name="secret", key="secret_key"
                    )
                ),
            ),
            "V1EnvVar(SECRET_VAR from secret:{'key': 'secret_key', 'name': 'secret', 'optional': None})",
        ),
        # Test case for unknown type
        (
            client.V1EnvVar(name="UNKNOWN_VAR", value_from=client.V1EnvVarSource()),
            "V1EnvVar(UNKNOWN_VAR from unknown type)",
        ),
    ],
)
def test_describe_envvar(env_var: client.V1EnvVar, expected_description: str) -> None:
    assert env_vars.describe_envvar(env_var) == expected_description


@pytest.mark.parametrize(
    "input_data, expected_output",
    [
        # Test with direct values
        (
            {"VAR1": "value1", "VAR2": "value2"},
            [
                client.V1EnvVar(name="VAR1", value="value1"),
                client.V1EnvVar(name="VAR2", value="value2"),
            ],
        ),
        # Test with a mix of direct values and env var sources
        (
            {
                "VAR1": "value1",
                "VAR2": client.V1EnvVarSource(
                    field_ref=client.V1ObjectFieldSelector(
                        field_path="metadata.namespace"
                    )
                ),
            },
            [
                client.V1EnvVar(name="VAR1", value="value1"),
                client.V1EnvVar(
                    name="VAR2",
                    value_from=client.V1EnvVarSource(
                        field_ref=client.V1ObjectFieldSelector(
                            field_path="metadata.namespace"
                        )
                    ),
                ),
            ],
        ),
        # Add more cases as needed
    ],
)
def test_get_env_from_dict(input_data: dict, expected_output: list) -> None:
    result = env_vars.get_env_from_dict(input_data)
    assert len(result) == len(
        expected_output
    ), "The number of environment variables does not match expected"
    for env_var, expected_var in zip(result, expected_output):
        assert (
            env_var.name == expected_var.name
        ), f"Name mismatch: {env_var.name} != {expected_var.name}"
        if env_var.value:
            assert (
                env_var.value == expected_var.value
            ), f"Value mismatch: {env_var.value} != {expected_var.value}"
        if env_var.value_from:
            # Add more detailed checks for V1EnvVarSource if necessary
            assert isinstance(
                env_var.value_from, client.V1EnvVarSource
            ), "Value_from should be a V1EnvVarSource"


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("VAR1", "value1", client.V1EnvVar(name="VAR1", value="value1")),
        ("VAR2", "value2", client.V1EnvVar(name="VAR2", value="value2")),
    ],
)
def test_get_env_pair(key: str, value: str, expected: client.V1EnvVar) -> None:
    result = env_vars.get_env_pair(key, value)
    assert (
        result.name == expected.name
    ), f"Name mismatch: expected {expected.name}, got {result.name}"
    assert (
        result.value == expected.value
    ), f"Value mismatch: expected {expected.value}, got {result.value}"
    assert result.value_from is None, "value_from should be None"
