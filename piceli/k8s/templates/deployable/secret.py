import base64
import json
from typing import Optional

from kubernetes import client

from piceli.k8s.constants import secret_type
from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


class Secret(base.Deployable):
    """Secret"""

    name: names.Name
    secret_type: secret_type.SecretType
    string_data: Optional[dict[str, str]] = None
    data: Optional[dict[str, str]] = None
    labels: Optional[Labels] = None
    # API: ClassVar[str] = "core"
    # API_FUNC: ClassVar[str] = "secret"

    def get(self) -> list[client.V1Secret]:
        """get the k8s object to apply"""
        obj = client.V1Secret(
            api_version="v1",
            kind="Secret",
            type=self.secret_type.value,
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            string_data=self.string_data,
            data=self.data,
        )
        return [obj]

    @staticmethod
    def get_docker_json_data(docker_auth: str) -> dict[str, str]:
        key_data_str = "_json_key:" + base64.b64decode(docker_auth).decode()
        auth_data = {
            "auths": {
                "gcr.io": {
                    "auth": base64.b64encode(key_data_str.encode("utf8")).decode(),
                }
            }
        }
        return {
            ".dockerconfigjson": base64.b64encode(
                json.dumps(auth_data, separators=(",", ":")).encode()
            ).decode()
        }

    @classmethod
    def get_docker_json_secret(cls, name: str, docker_auth: str) -> "Secret":
        data = cls.get_docker_json_data(docker_auth)
        return cls(name=name, secret_type=secret_type.SecretType.DOCKER_JSON, data=data)
