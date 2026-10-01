"""Server-validate the manual UI install in a disposable explicit kind target.

This admission check does not prove image contents, CNI enforcement or the
authenticated browser journey; those require the full Wave 4 runtime gate.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from piceli.server.cluster_install import cluster_install_yaml
from tests.integration.kind_support import kubectl, requires_kind, requires_ui_kind
from tests.unit.server.test_cluster_install import _config, _manual

pytestmark = [pytest.mark.integration, requires_kind, requires_ui_kind]


def test_manual_ui_manifest_is_admitted_by_kubernetes() -> None:
    namespace = "piceli-ui-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", namespace)
    try:
        manual = _manual()
        manual = replace(
            manual,
            release_definition_toml=manual.release_definition_toml.replace(
                'namespace = "shop"', f'namespace = "{namespace}"'
            ),
        )
        config = replace(
            _config(), namespace=namespace, experimental=True, manual=manual
        )
        output = kubectl(
            "apply",
            "--dry-run=server",
            "-f",
            "-",
            stdin=cluster_install_yaml(config),
        )
        assert "deployment.apps/piceli-ui" in output
        assert "networkpolicy.networking.k8s.io/piceli-renderer-deny-egress" in output
    finally:
        kubectl("delete", "namespace", namespace, "--wait=false")
