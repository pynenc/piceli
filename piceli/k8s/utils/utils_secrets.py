import base64
import json


def get_docker_registry_secret_data(docker_auth: str) -> dict[str, str]:
    """gets the data field for the docker secret from docker env vars"""
    key_data_str = "_json_key:" + base64.b64decode(docker_auth).decode()
    data = {
        "auths": {
            "gcr.io": {
                "auth": base64.b64encode(key_data_str.encode("utf8")).decode(),
            }
        }
    }
    return {
        ".dockerconfigjson": base64.b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode()
    }
