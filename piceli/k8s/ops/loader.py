import importlib
import json
import logging
import os
import pkgutil
import sys
from importlib import util as importlib_util
from itertools import chain
from pathlib import Path
from types import ModuleType
from typing import Iterable, Iterator

import yaml
from kubernetes import client
from kubernetes.client import models as k8s_models

from piceli.k8s.k8s_objects.base import (
    K8sObject,
    ObjectOrigin,
    OriginJSON,
    OriginK8sLib,
    OriginTemplate,
    OriginYAML,
)
from piceli.k8s.templates.deployable.base import Deployable

logger = logging.getLogger(__name__)


def string_in_k8s_models(target_string: str) -> bool:
    if target_string in dir(k8s_models):
        attr = getattr(k8s_models, target_string)
        return isinstance(attr, type)
    return False


def load_models_from_module_names(module_names: Iterable[str]) -> Iterator[K8sObject]:
    """
    Loads and returns a list of Kubernetes model instances from specified Python modules.
    """
    for module_name in module_names:
        module = importlib.import_module(module_name)
        yield from load_models_from_module(module)


def load_models_from_module(module: ModuleType) -> Iterator[K8sObject]:
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if isinstance(attr, Deployable):
            origin: ObjectOrigin = OriginTemplate(module.__name__, attr_name)
            for spec in attr.api_data():
                yield K8sObject(spec, origin)
        if isinstance(attr, object) and string_in_k8s_models(attr.__class__.__name__):
            spec = client.ApiClient().sanitize_for_serialization(attr)
            origin = OriginK8sLib(module.__name__, attr_name)
            yield K8sObject(spec, origin)


def load_resources_from_any(
    obj: dict | list, origin: ObjectOrigin
) -> Iterator[K8sObject]:
    if isinstance(obj, dict):
        yield K8sObject(obj, origin)
    else:
        for doc in obj:
            yield from load_resources_from_any(doc, origin)


def load_resources_from_files(paths: Iterable[str]) -> Iterator[K8sObject]:
    """
    Loads and returns a list of resource dictionaries from specified YAML/JSON files.
    """
    for path in paths:
        with open(path) as file:
            if path.lower().endswith(".yaml") or path.lower().endswith(".yml"):
                documents = list(yaml.safe_load_all(file))
                yield from load_resources_from_any(documents, OriginYAML(path))
            elif path.lower().endswith(".json"):
                loaded_json = json.load(file)
                yield from load_resources_from_any(loaded_json, OriginJSON(path))
            else:
                logger.info(f"ignoring file: {path}")


def load_all_resources(
    module_names: list[str], file_paths: list[str]
) -> Iterator[K8sObject]:
    """
    Loads all resources from both Python modules and YAML files.
    """
    models = load_models_from_module_names(module_names)
    yamls = load_resources_from_files(file_paths)
    return chain(models, yamls)


def find_modules_by_name(module_name: str, sub_elements: bool) -> Iterator[str]:
    """Load from Python module using import notation"""
    if not module_name:
        return
    module = importlib.import_module(module_name)
    yield module_name
    if sub_elements:
        # If sub_elements is True, find and add all submodules
        if hasattr(module, "__path__"):
            for _, sub_module_name, _ in pkgutil.walk_packages(
                module.__path__, module.__name__ + "."
            ):
                yield sub_module_name


def load_modules_by_path(module_path: str, sub_elements: bool) -> Iterator[ModuleType]:
    """
    Load Python modules or packages from a given file path. If the path is a directory,
    it can optionally include submodules and packages within that directory.

    Args:
        module_path (str): The path of the Python module or package to load.
        sub_elements (bool): Whether to include modules in subdirectories recursively.

    Returns:
        Iterator[str]: An iterator over the names of modules that can be loaded.
    """
    if not module_path:
        return
    module_path = Path(module_path)
    if module_path.is_dir():
        if sub_elements:
            # Include submodules and packages if sub_elements is True
            for dirpath, _, files in os.walk(module_path):
                for filename in files:
                    yield from load_modules_by_path(
                        os.path.join(dirpath, filename), sub_elements
                    )
        else:
            for file in module_path.glob("*.py"):
                yield from load_modules_by_path(str(file.resolve()), sub_elements)
    elif module_path.is_file() and module_path.suffix == ".py":
        dot_path = (
            str(module_path.with_suffix(""))
            .replace(os.sep, ".")
            .replace(module_path.parent.as_posix(), "")
            .strip(".")
        )
        spec = importlib_util.spec_from_file_location(dot_path, str(module_path))
        if spec and spec.loader:
            if str(module_path.parent) not in sys.path:
                sys.path.insert(0, str(module_path.parent))
            yield importlib_util.module_from_spec(spec)


def load_files_from_folder(folder_path: str, sub_elements: bool) -> Iterator[str]:
    """
    Load all the files in a folder (and subfolders if sub_elements)

    Args:
        folder_path (str): The path to the folder to load.
        sub_elements (bool): Whether to include subdirectories recursively.

    Returns:
        Iterator[str]: An iterator over the filepaths to load.
    """
    if not folder_path:
        return
    if sub_elements:
        for dirpath, _, files in os.walk(folder_path):
            for filename in files:
                yield os.path.join(dirpath, filename)
    else:
        for file in os.listdir(folder_path):
            full_path = os.path.join(folder_path, file)
            if os.path.isfile(full_path):
                yield full_path


def load_all(
    module_name: str, module_path: str, folder_path: str, sub_elements: bool = True
) -> Iterator[K8sObject]:
    """
    Load all the resources from modules or the specified folder.
    """
    yield from load_models_from_module_names(
        find_modules_by_name(module_name, sub_elements)
    )
    for module in load_modules_by_path(module_path, sub_elements):
        yield from load_models_from_module(module)
    yield from load_resources_from_files(
        load_files_from_folder(folder_path, sub_elements)
    )
