import importlib

import yaml

from piceli.k8s.k8s_objects.base import K8sObject, OriginTemplate, OriginYAML
from piceli.k8s.templates.deployable.base import Deployable


def load_models_from_modules(module_names: list[str]) -> list[K8sObject]:
    """
    Loads and returns a list of Kubernetes model instances from specified Python modules.
    """
    models = []
    for module_name in module_names:
        module = importlib.import_module(module_name)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, Deployable):
                origin = OriginTemplate(module_name, attr_name)
                for spec in attr.api_data():
                    models.append(K8sObject(spec, origin))
            # TODO add direct support for Kubernetes library object
    return models


def load_resources_from_yaml(paths: list[str]) -> list[K8sObject]:
    """
    Loads and returns a list of resource dictionaries from specified YAML files.
    """
    resources = []
    for path in paths:
        with open(path) as file:
            documents = yaml.safe_load_all(file)
            for doc in documents:
                if isinstance(doc, dict):
                    origin = OriginYAML(path)
                    resources.append(K8sObject(doc, origin))
    return resources


def load_all_resources(
    module_names: list[str], yaml_paths: list[str]
) -> list[K8sObject]:
    """
    Loads all resources from both Python modules and YAML files.
    """
    models = load_models_from_modules(module_names)
    yamls = load_resources_from_yaml(yaml_paths)
    return models + yamls
