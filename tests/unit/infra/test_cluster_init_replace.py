"""0.92.0: ``cluster init`` and a development cache claim that cannot grow.

A claim on a storage class without ``allowVolumeExpansion`` cannot grow: the
API refuses the patch with a 403 whose reason is not RBAC. Init stopped at
that first refusal, so the objects after it (the development config) were
never applied, and the message said to check the account's permissions.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from kubernetes.client.exceptions import ApiException

from piceli.gitops import GitOpsError
from piceli.gitops.install import (
    Api,
    InstallPlan,
    api_reason,
    execute,
    plan_objects,
)
from piceli.infra.cluster_init import replace_dev_cache

CLAIM = "/api/v1/namespaces/piceli-dev/persistentvolumeclaims/piceli-dev-cache"
CLAIMS = "/api/v1/namespaces/piceli-dev/persistentvolumeclaims"
CONFIG = "/api/v1/namespaces/piceli-dev/configmaps/piceli-dev-config"
PV = "/api/v1/persistentvolumes/pvc-1"
CLASS = "/apis/storage.k8s.io/v1/storageclasses/retain-local"
PODS = "/api/v1/namespaces/piceli-dev/pods"


def _claim(size: str, storage_class: str = "retain-local") -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": "piceli-dev-cache", "namespace": "piceli-dev"},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
        },
    }


CONFIG_MAP = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {"name": "piceli-dev-config", "namespace": "piceli-dev"},
    "data": {"config.json": '{"cache_size": "110Gi"}'},
}


class StubApi(Api):
    """Objects by path; ``refuse`` maps ``(method, path)`` to an error."""

    def __init__(self) -> None:
        super().__init__(client=None)
        self.objects: dict[str, dict[str, Any]] = {}
        self.pods: list[dict[str, Any]] = []
        self.refuse: dict[tuple[str, str], GitOpsError] = {}
        self.calls: list[tuple[str, str]] = []
        self.serial = 0

    def put(self, path: str, value: dict[str, Any]) -> None:
        self.serial += 1
        value = copy.deepcopy(value)
        value["metadata"] = {
            **value.get("metadata", {}),
            "uid": f"uid-{self.serial}",
            "resourceVersion": str(self.serial),
        }
        self.objects[path] = value

    def call(  # type: ignore[override]
        self,
        path: str,
        method: str,
        body: Any = None,
        content: str = "application/json",
        *,
        missing_ok: bool = True,
    ) -> Any:
        self.calls.append((method, path))
        if (method, path) in self.refuse:
            raise self.refuse[(method, path)]
        if method == "GET":
            if path == PODS:
                return {"items": self.pods}
            return copy.deepcopy(self.objects.get(path))
        if method == "PATCH":
            live = self.objects[path]
            for key, value in body.items():
                if key == "spec":
                    live.setdefault("spec", {}).update(value)
                elif key != "metadata":
                    live[key] = value
            return live
        if method == "DELETE":
            self.objects.pop(path, None)
            return {}
        if method == "POST":
            self.put(f"{path}/{body['metadata']['name']}", body)
            return self.objects[f"{path}/{body['metadata']['name']}"]
        raise AssertionError(method)


def _cluster(expand: bool = False, dynamic: bool = True) -> StubApi:
    api = StubApi()
    live = _claim("80Gi")
    live["spec"]["volumeName"] = "pvc-1"
    api.put(CLAIM, live)
    api.put(
        PV,
        {
            "metadata": {
                "name": "pvc-1",
                "annotations": {
                    "pv.kubernetes.io/provisioned-by": "rancher.io/local-path"
                }
                if dynamic
                else {},
            },
            "spec": {"persistentVolumeReclaimPolicy": "Retain"},
        },
    )
    api.put(
        CLASS,
        {"metadata": {"name": "retain-local"}, "allowVolumeExpansion": expand},
    )
    config = copy.deepcopy(CONFIG_MAP)
    config["data"] = {"config.json": '{"cache_size": "80Gi"}'}
    api.put(CONFIG, config)
    api.put("/api/v1/namespaces/kube-system", {"metadata": {"name": "kube-system"}})
    return api


def _plan(api: StubApi, size: str = "110Gi", **claim: Any) -> InstallPlan:
    objects = plan_objects(
        api, [_claim(size, **claim), CONFIG_MAP], action="enable", config={}
    )
    return replace_dev_cache(api, objects)


def test_a_claim_its_class_cannot_grow_is_planned_as_a_replace() -> None:
    api = _cluster(expand=False)
    plan = _plan(api)
    claim, config = plan.objects
    assert claim.operation == "replace"
    assert claim.note == (
        "the cache; its storage class retain-local cannot resize volumes: "
        "deleted with its volume and made again at 110Gi"
    )
    assert claim.describe()["note"] == claim.note
    assert config.operation == "apply"
    unchanged = plan_objects(
        api, [_claim("110Gi"), CONFIG_MAP], action="enable", config={}
    )
    assert plan.plan_hash != unchanged.plan_hash  # the replace is what is approved


@pytest.mark.parametrize(
    ("expand", "dynamic", "size", "claim", "operation", "why"),
    [
        (True, True, "110Gi", {}, "apply", None),  # the API grows it
        (True, False, "110Gi", {}, "replace", "was not provisioned"),
        (True, True, "40Gi", {}, "replace", "cannot shrink"),
        (True, True, "80Gi", {"storage_class": "other"}, "replace", "storage class"),
    ],
)
def test_only_a_change_the_api_refuses_replaces_the_claim(
    expand: bool,
    dynamic: bool,
    size: str,
    claim: dict[str, Any],
    operation: str,
    why: str | None,
) -> None:
    item = _plan(_cluster(expand=expand, dynamic=dynamic), size, **claim).objects[0]
    assert item.operation == operation
    assert (item.note is None) if why is None else (why in (item.note or ""))


def test_other_claims_are_never_replaced() -> None:
    api = _cluster()
    other = _claim("110Gi")
    other["metadata"]["name"] = "piceli-state"
    api.put(f"{CLAIMS}/piceli-state", _claim("80Gi"))
    plan = replace_dev_cache(
        api, plan_objects(api, [other], action="enable", config={})
    )
    assert plan.objects[0].operation == "apply"


def test_the_replace_cleans_the_retained_volume_and_makes_the_claim_again() -> None:
    api = _cluster()
    plan = _plan(api)
    said: list[str] = []
    done = execute(api, plan, said.append, sleep=lambda _: None)
    assert [d["operation"] for d in done] == ["replace", "apply"]
    # The Retain volume goes with the claim, instead of staying on the node.
    assert api.objects[PV]["spec"]["persistentVolumeReclaimPolicy"] == "Delete"
    order = [call for call in api.calls if call[0] != "GET"]
    assert order[:3] == [("PATCH", PV), ("DELETE", CLAIM), ("POST", CLAIMS)]
    assert api.objects[CLAIM]["spec"]["resources"]["requests"]["storage"] == "110Gi"
    assert json.loads(api.objects[CONFIG]["data"]["config.json"]) == {
        "cache_size": "110Gi"
    }
    assert "  reclaim PersistentVolume/pvc-1: Delete (was Retain)" in said


def test_a_claim_in_use_is_left_and_the_rest_of_the_plan_still_applies() -> None:
    api = _cluster()
    api.pods = [
        {
            "metadata": {"name": "run-1"},
            "spec": {
                "volumes": [
                    {"persistentVolumeClaim": {"claimName": "piceli-dev-cache"}}
                ]
            },
            "status": {"phase": "Running"},
        },
        {
            "metadata": {"name": "run-0"},
            "spec": {
                "volumes": [
                    {"persistentVolumeClaim": {"claimName": "piceli-dev-cache"}}
                ]
            },
            "status": {"phase": "Succeeded"},
        },
    ]
    failures: list[dict[str, Any]] = []
    done = execute(api, _plan(api), failures=failures, sleep=lambda _: None)
    assert [d["kind"] for d in done] == ["ConfigMap"]
    (failed,) = failures
    assert failed["operation"] == "replace"
    assert "in use by pod run-1;" in failed["message"]
    assert api.objects[CLAIM]["spec"]["resources"]["requests"]["storage"] == "80Gi"
    with pytest.raises(GitOpsError, match="in use by pod run-1"):
        execute(api, _plan(api), sleep=lambda _: None)  # without failures: raises


def test_a_claim_held_by_a_finalizer_fails_after_the_wait() -> None:
    api = _cluster()
    original = api.call

    def held(path: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "DELETE" and path == CLAIM:
            return {}  # Terminating: the object stays
        return original(path, method, *args, **kwargs)

    api.call = held  # type: ignore[method-assign]
    ticks = iter(range(0, 1000, 30))
    failures: list[dict[str, Any]] = []
    execute(
        api,
        _plan(api),
        failures=failures,
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )
    assert "still there after 120 s" in failures[0]["message"]


class _Client:
    def __init__(self, status: int, message: str | None) -> None:
        self.error = ApiException(status=status, reason="Forbidden")
        self.error.body = json.dumps(
            {"kind": "Status", "message": message} if message else {}
        ).encode()

    def call_api(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


def test_the_error_names_the_apis_reason_and_tells_admission_from_rbac() -> None:
    resize = (
        'persistentvolumeclaims "piceli-dev-cache" is forbidden: only dynamically '
        "provisioned pvc can be resized and the storageclass that provisions the "
        "pvc must support resize"
    )
    with pytest.raises(GitOpsError) as refused:
        Api(_Client(403, resize)).call(CLAIM, "PATCH", {})
    assert str(refused.value).endswith(f"(HTTP 403): {resize}")
    assert refused.value.details["refused"]["reason"] == resize
    rbac = 'User "dev" cannot patch resource "persistentvolumeclaims"'
    with pytest.raises(GitOpsError) as denied:
        Api(_Client(403, rbac)).call(CLAIM, "PATCH", {})
    assert "denied" in denied.value.details
    with pytest.raises(GitOpsError) as bare:
        Api(_Client(403, None)).call(CLAIM, "PATCH", {})
    assert str(bare.value).endswith("(HTTP 403)") and "denied" in bare.value.details
    assert api_reason(type("E", (), {"body": "x" * 10})()) is None
    assert (
        len(
            api_reason(type("E", (), {"body": json.dumps({"message": "y" * 400})})())
            or ""
        )
        == 300
    )
