# Tooling implemented based on the kubernetes library:
# https://github.com/kubernetes-client/python/blob/master/kubernetes/utils/create_from_yaml.py
from typing import Optional


def get_object_group_and_version(obj: dict) -> tuple[str, str]:
    """Returns the group of a kubernetes object"""
    group, _, version = obj["apiVersion"].partition("/")
    if version == "":
        version = group
        group = "core"
    # Take care for the case e.g. api_type is "apiextensions.k8s.io"
    # Only replace the last instance
    group = "".join(group.rsplit(".k8s.io", 1))
    group = "".join(word.capitalize() for word in group.split("."))
    return group, version


def get_api_name(group: str, version: str) -> str:
    """Returns the api of a kubernetes object"""
    return f"{group}{version.capitalize()}Api"


def get_object_api_name(obj: dict) -> str:
    """Returns the api of a kubernetes object"""
    group, version = get_object_group_and_version(obj)
    return get_api_name(group, version)


def get_namespace(obj: dict) -> Optional[str]:
    """Returns the namespace of a kubernetes object"""
    return obj.get("metadata", {}).get("namespace", None)
