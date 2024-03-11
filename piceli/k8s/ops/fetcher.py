from collections import defaultdict
from typing import Any, Callable, NamedTuple

from kubernetes import client

from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject, OriginCluster
from piceli.k8s.utils import utils_api


class ClusterObjects(NamedTuple):
    namespace: str | None
    objects: list[K8sObject]


def add_all_custom_objects(
    ctx: ClientContext,
    namespace: str | None,
    objects: dict[str | None, list[K8sObject]],
) -> None:
    namespaces: list[str | None] = (
        [namespace] if namespace else get_all_namespaces(ctx) + [None]
    )
    for crd in ctx.extensions_api.list_custom_resource_definition().items:
        group = crd.spec.group
        versions = [v.name for v in crd.spec.versions]
        plural = crd.spec.names.plural
        scope = crd.spec.scope
        kind = crd.spec.names.kind
        for namespace in namespaces:
            add_custom_objects(
                ctx, namespace, objects, group, versions, plural, scope, kind
            )


def add_custom_objects(
    ctx: ClientContext,
    namespace: str | None,
    objects: dict[str | None, list[K8sObject]],
    group: str,
    versions: list[str],
    plural: str,
    scope: str,
    kind: str,
) -> None:
    if scope == "Namespaced" and namespace is not None:
        op = "list_namespaced_custom_object"
        op_args: tuple = (namespace, group)
    elif scope == "Cluster" and namespace is None:
        op = "list_cluster_custom_object"
        op_args = (group,)
    else:
        return
    origin = OriginCluster(ctx.kubeconfig, namespace)
    for version in versions:
        args = op_args + (version, plural)
        try:
            for item in getattr(ctx.custom_api, op)(*args)["items"]:
                spec = ctx.api_client.sanitize_for_serialization(item)
                spec["apiVersion"] = f"{group}/{version}"
                spec["kind"] = kind
                objects[namespace].append(K8sObject(spec, origin))
        except client.ApiException as ex:
            if ex.status != 404:
                raise ex


def add_all_namespaced_objects(
    ctx: ClientContext,
    namespaces: list[str],
    api: Callable,
    api_version: str,
    resource: Any,
    objects: dict[str | None, list[K8sObject]],
) -> None:
    op_end = utils_api.get_api_func_ending(resource.kind)
    op = f"list_namespaced_{op_end}"
    if not hasattr(api, op):
        return
    for namespace in namespaces:
        origin = OriginCluster(ctx.kubeconfig, namespace)
        args = (namespace,)
        for item in getattr(api, op)(*args).items:
            spec = ctx.api_client.sanitize_for_serialization(item)
            spec["apiVersion"] = api_version
            spec["kind"] = resource.kind
            objects[namespace].append(K8sObject(spec, origin))


def add_all_non_namespaced_objects(
    ctx: ClientContext,
    api_version: str,
    api: Callable,
    resource: client.V1APIResourceList,
    objects: dict[str | None, list[K8sObject]],
) -> None:
    origin = OriginCluster(ctx.kubeconfig, None)
    op_end = utils_api.get_api_func_ending(resource.kind)
    op = f"list_{op_end}"
    if not hasattr(api, op):
        return
    for item in getattr(api, op)().items:
        spec = ctx.api_client.sanitize_for_serialization(item)
        spec["apiVersion"] = api_version
        spec["kind"] = resource.kind
        objects[None].append(K8sObject(spec, origin))


APIS = ["core_api", "batch_api", "apps_api", "rbacauthorization_api", "hpa_api"]


def get_all_from_context(
    ctx: ClientContext,
    namespace: str | None = None,
    ignore_kinds: list[str] | None = None,
) -> dict[str | None, list[K8sObject]]:
    namespaces = [namespace] if namespace else get_all_namespaces(ctx)
    objects: dict[str | None, list[K8sObject]] = defaultdict(list)
    ignore_kinds = ignore_kinds or []
    for api_name in APIS:
        api = getattr(ctx, api_name)
        resources = api.get_api_resources()
        if isinstance(resources, client.V1APIResourceList):
            api_version = resources.group_version
            for resource in resources.resources:
                if resource.kind in ignore_kinds:
                    continue
                if "list" not in resource.verbs:
                    continue
                if resource.namespaced:
                    add_all_namespaced_objects(
                        ctx, namespaces, api, api_version, resource, objects
                    )
                if namespace is None:
                    add_all_non_namespaced_objects(
                        ctx, api, api_version, resource, objects
                    )
        else:
            print(resources)
    add_all_custom_objects(ctx, namespace, objects)
    return objects


def get_all_namespaces(ctx: ClientContext) -> list[str]:
    return [item.metadata.name for item in ctx.core_api.list_namespace().items]
