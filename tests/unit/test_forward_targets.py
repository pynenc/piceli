"""Port-forward targets: services, pods and deployments, consistently everywhere."""

import pytest

from piceli.artifacts import registry_delivery
from piceli.k8s.observe import PortForward
from piceli.k8s.ui_config import UiShortcut

ACCEPTED = ["service/registry", "pod/registry-5d8f7c-abcde", "deployment/registry"]
REJECTED = ["statefulset/registry", "deployment/", "Deployment/registry", "registry"]


@pytest.mark.parametrize("target", ACCEPTED)
def test_forward_targets_accepted(target: str) -> None:
    PortForward("registry", "demo", target, 15000, 5000)
    UiShortcut(
        id="registry",
        label="Registry",
        target=target,
        local_port=15000,
        remote_port=5000,
    )
    assert registry_delivery._FORWARD_TARGET.fullmatch(target)


@pytest.mark.parametrize("target", REJECTED)
def test_forward_targets_rejected(target: str) -> None:
    with pytest.raises(ValueError):
        PortForward("registry", "demo", target, 15000, 5000)
    with pytest.raises(ValueError):
        UiShortcut(
            id="registry",
            label="Registry",
            target=target,
            local_port=15000,
            remote_port=5000,
        )
    assert not registry_delivery._FORWARD_TARGET.fullmatch(target)
