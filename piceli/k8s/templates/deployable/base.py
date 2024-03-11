from abc import abstractmethod
from typing import Any

from kubernetes import client
from pydantic import BaseModel


class Deployable(BaseModel):
    """Deployable"""

    @abstractmethod
    def get(self) -> list[Any]:
        """gets a list of deployable kubernetes objects"""

    def api_data(self) -> list[dict]:
        """gets the deployable dictionary for the kubernetes API"""
        return client.ApiClient().sanitize_for_serialization(self.get())
