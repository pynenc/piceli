"""A forward started against the pod that backs its Service is still Piceli's."""

from __future__ import annotations

from piceli.k8s.port_owner import (
    OTHER,
    PICELI_FORWARD,
    PortOwner,
    ProcessInfo,
    is_piceli_forward,
    recognise,
    same_target,
)

PICELI = ProcessInfo(77, "/work/.venv/bin/python -m piceli access release.toml")


def _argv(target: str) -> str:
    return (
        "/usr/local/bin/kubectl --kubeconfig /work/kubeconfig --context demo "
        f"--namespace shop port-forward {target} 18090:80 --address 127.0.0.1"
    )


def _check(target: str, parent: ProcessInfo | None = PICELI) -> str:
    return recognise(
        PortOwner(18090, 4242, _argv(target), parent),
        kubeconfig="/work/kubeconfig",
        context="demo",
        forwards=(("shop", "service/api", 18090, 80),),
        targets=("release.toml",),
    ).kind


def test_the_backing_pod_counts_as_the_declared_service() -> None:
    assert _check("service/api") == PICELI_FORWARD
    assert _check("pod/api-6d5f7c9b8-x2x4z") == PICELI_FORWARD
    assert _check("pod/api-6d5f7c9b8-x2x4z", parent=None) == PICELI_FORWARD
    assert _check("pod/Bad_Name") == OTHER
    assert _check("service/other") == OTHER
    assert _check("pod/api-1", parent=ProcessInfo(9, "bash dev.sh")) == OTHER


def test_same_target_only_widens_service_and_deployment() -> None:
    assert same_target("pod/api-1", "deployment/api")
    assert not same_target("pod/api-1", "pod/api-2")
    assert not same_target("service/api", "deployment/api")


def test_is_piceli_forward_accepts_the_backing_pod() -> None:
    owner = PortOwner(18090, 4242, _argv("pod/api-1"), PICELI)
    kwargs = {
        "context": "demo",
        "namespace": "shop",
        "local_port": 18090,
        "remote_port": 80,
    }
    assert is_piceli_forward(owner, target="service/api", **kwargs)
    assert not is_piceli_forward(owner, target="pod/api-2", **kwargs)
