from dataclasses import dataclass

import yaml


@dataclass(frozen=True, eq=True)
class KubeConfig:
    cluster_name: str
    cert: str
    endpoint: str

    @property
    def as_dict(self) -> dict:
        """Generate a dictionary representation of the kubeconfig."""
        kubeconfig_dict = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [
                {
                    "name": f"kube-config-{self.cluster_name}",
                    "cluster": {
                        "certificate-authority-data": self.cert,
                        "server": f"https://{self.endpoint}",
                    },
                }
            ],
            "contexts": [
                {
                    "name": f"kube-config-{self.cluster_name}",
                    "context": {
                        "cluster": f"kube-config-{self.cluster_name}",
                        "user": f"kube-config-{self.cluster_name}",
                    },
                }
            ],
            "current-context": f"kube-config-{self.cluster_name}",
            "users": [
                {
                    "name": f"kube-config-{self.cluster_name}",
                    "user": {
                        "auth-provider": {
                            "name": "gcp",
                            "config": {
                                "scopes": "https://www.googleapis.com/auth/cloud-platform"
                            },
                        }
                    },
                }
            ],
        }
        return kubeconfig_dict

    @property
    def as_yaml(self) -> str:
        """Convert the kubeconfig dictionary to YAML format."""
        return yaml.safe_dump(self.as_dict)

    # def __hash__(self) -> int:
    #     """Generate a unique hash based on the cluster name, cert, and endpoint."""
    #     return hash((self.cluster_name, self.cert, self.endpoint))
