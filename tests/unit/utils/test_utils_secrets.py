from piceli.k8s.utils import utils_secrets


def test_dockerconfig_json() -> None:
    """test generation of the secret for docer pull data"""
    docker_auth = (
        "eyJhdXRocyI6eyJnY3IuaW8iOnsiYXV0aCI6ImRtVnllVk5sWTNWeVpWQmhjM009In19fQ=="
    )
    data = utils_secrets.get_docker_registry_secret_data(docker_auth)
    expected = {
        ".dockerconfigjson": "eyJhdXRocyI6eyJnY3IuaW8iOnsiYXV0aCI6IlgycHpiMjVmYTJWNU9uc2lZWFYwYUhNaU9uc2laMk55TG1sdklqcDdJbUYxZEdnaU9pSmtiVlo1WlZaT2JGa3pWbmxhVmtKb1l6Tk5QU0o5ZlgwPSJ9fX0="
    }
    assert data == expected
