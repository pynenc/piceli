"""GKE kubeconfig clients must never leave credential files or env state behind."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.oauth2 import service_account

from piceli.k8s.config.kubeconfig import KubeConfig
from piceli.k8s.k8s_client import client as client_module

SA_INFO = {"type": "service_account", "client_email": "sa@example.test"}
KUBECONFIG = KubeConfig("test-cluster", "Y2VydA==", "10.0.0.1")


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        client_module,
        "GCE_SA_INFO",
        base64.b64encode(json.dumps(SA_INFO).encode()).decode(),
    )
    monkeypatch.setattr(client_module.ClientManager, "_clients", {})
    seen: dict[str, Any] = {}

    class FakeLoader:
        def __init__(self, config_dict: dict, **kwargs: Any) -> None:
            seen["config"] = config_dict
            seen["kwargs"] = kwargs
            seen["env_during_load"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            seen["files_during_load"] = sorted(os.listdir(tmp_path))

        def load_and_set(self, configuration: Any) -> None:
            seen["credentials"] = seen["kwargs"]["get_google_credentials"]()

    monkeypatch.setattr(
        client_module.config.kube_config, "KubeConfigLoader", FakeLoader
    )
    fake_credentials = MagicMock(name="credentials")
    from_info = MagicMock(return_value=fake_credentials)
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_info",
        from_info,
    )
    seen["from_info"] = from_info
    seen["fake_credentials"] = fake_credentials
    seen["cwd"] = tmp_path
    return seen


def test_gke_client_builds_credentials_in_memory(
    isolated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/previous/creds.json")

    client_module.ClientManager().get_client(KUBECONFIG)

    assert isolated["config"] == KUBECONFIG.as_dict
    assert isolated["files_during_load"] == []
    assert os.listdir(isolated["cwd"]) == []
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == "/previous/creds.json"
    assert isolated["env_during_load"] == "/previous/creds.json"
    isolated["from_info"].assert_called_once_with(
        SA_INFO, scopes=list(client_module.GCP_SCOPES)
    )
    isolated["fake_credentials"].refresh.assert_called_once()
    assert isolated["credentials"] is isolated["fake_credentials"]


def test_gke_client_leaves_env_unset_when_it_was_unset(
    isolated: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    client_module.ClientManager().get_client(KUBECONFIG)

    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
    assert not (isolated["cwd"] / "sa.json").exists()
    assert os.listdir(isolated["cwd"]) == []
