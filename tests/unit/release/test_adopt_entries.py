"""``[release] adopt``/``--adopt`` entries: RBAC names may hold ':'."""

from __future__ import annotations

import pytest

from piceli.k8s.release_spec import ReleaseSpecError, parse_adopt_entry


@pytest.mark.parametrize(
    "entry, parsed",
    [
        ("ConfigMap/settings", (None, "ConfigMap", "settings")),
        ("apps/v1/Deployment/api", ("apps/v1", "Deployment", "api")),
        (
            "ClusterRole/staging:shop:watcher",
            (None, "ClusterRole", "staging:shop:watcher"),
        ),
        (
            "rbac.authorization.k8s.io/v1/ClusterRoleBinding/system:x",
            ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "system:x"),
        ),
        ("Role/Mixed_Case", (None, "Role", "Mixed_Case")),
    ],
)
def test_valid_entries(entry, parsed):
    assert parse_adopt_entry(entry) == parsed


@pytest.mark.parametrize(
    "entry",
    [
        "ConfigMap/a:b",
        "Deployment/Upper",
        "example.com/v1/Role/a:b",
        "ClusterRole/..",
        "ClusterRole/a%2Fb",
        "ClusterRole/",
        "settings",
    ],
)
def test_invalid_entries(entry):
    with pytest.raises(ReleaseSpecError) as error:
        parse_adopt_entry(entry)
    assert error.value.code == "invalid-adopt-entry"
