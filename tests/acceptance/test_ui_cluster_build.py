"""The installed build adapter approves an immutable Job, never source code."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from piceli.artifacts.cluster_build import ClusterBuildConfig
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.cluster_install import (
    InstalledBuildConfig,
    render_cluster_install,
)
from piceli.services.authority import FileScopePolicy, ScopePolicy, request_principal
from piceli.services.cluster_build_control import (
    ClusterBuildControl,
    ClusterBuildProfile,
)
from piceli.services.contracts import Principal
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration
from piceli.services.store import Store
from tests.unit.server.test_cluster_install import _config, _find, _manual


def test_cluster_build_is_scoped_reviewed_and_runs_in_token_free_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ClusterBuildConfig(
        image="example.invalid/builder@sha256:" + "a" * 64,
        repo="https://example.invalid/shop.git",
        registry_url="oci://127.0.0.1:5000/shop",
        repo_root=tmp_path / "build-specs",
        namespace="shop",
        service_account="piceli-builder",
    )
    query = QueryService(
        [
            Registration(
                "shop", "Shop", KubeconfigTarget(tmp_path / "kc", "fake", "shop")
            ),
            Registration(
                "other", "Other", KubeconfigTarget(tmp_path / "kc", "fake", "other")
            ),
        ],
        scope_policy=ScopePolicy(
            {"operator": {"shop": frozenset({"inspect", "deploy"})}}
        ),
    )
    control = ClusterBuildControl(
        query,
        "shop",
        ClusterBuildProfile("host-build.toml", config, ("linux/amd64",)),
        Store(tmp_path / "operations.sqlite3"),
        tmp_path,
    )
    observed: list[str] = []

    def run(*args: object, **kwargs: object) -> dict:
        observed.append(str(kwargs["approve"]))
        return {
            "delivered": {
                "linux/amd64": {
                    "web": {"pull_ref": "example.invalid/web@sha256:" + "b" * 64}
                }
            },
            "job": {"name": "piceli-build"},
        }

    monkeypatch.setattr("piceli.services.cluster_build_control.run_build_job", run)
    actor = Principal(id="operator", name="Operator", kind="oidc")
    with request_principal(actor):
        plan = control.plan("a" * 40, "main")
        assert plan["digest"].startswith("sha256:")
        assert control._pipeline().builds[0].path is not None
        assert not control._pipeline().builds[0].path.exists()
        with pytest.raises(QueryError, match="ui-approval-mismatch"):
            control.admit(plan["id"], "sha256:" + "f" * 64, "first")
        operation = control.admit(plan["id"], plan["digest"], "approved")
        assert (
            control.admit(plan["id"], plan["digest"], "approved")["id"]
            == operation["id"]
        )
        with pytest.raises(QueryError, match="ui-not-found"):
            query.registration("other", action="deploy")
    control.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with request_principal(actor):
                finished = control.operation(operation["id"])
            if finished["state"] == "succeeded":
                break
            time.sleep(0.02)
        assert finished["state"] == "succeeded"
        assert observed == [plan["digest"]]
        assert "delivered" not in finished
        assert "linux/amd64" in finished["images"]
    finally:
        control.close()
        control.store.close()
    objects = render_cluster_install(
        replace(
            _config(),
            experimental=True,
            manual=_manual(),
            build=InstalledBuildConfig(
                "host-build.toml", config.image, config.repo, config.registry_url or ""
            ),
        )
    )
    builder = _find(objects, "ServiceAccount", "piceli-builder")
    assert builder["automountServiceAccountToken"] is False
    role = _find(objects, "Role", "piceli-ui")
    assert any(
        rule["resources"] == ["persistentvolumeclaims"] and rule["verbs"] == ["create"]
        for rule in role["rules"]
    )
    assert not any("secrets" in rule["resources"] for rule in role["rules"])
    assert not any(
        subject.get("name") == "piceli-builder"
        for item in objects
        if item["kind"] == "RoleBinding"
        for subject in item["subjects"]
    )


def test_projected_grant_revocation_fails_closed(tmp_path: Path) -> None:
    principal = hashlib.sha256(b"issuer\0operator").hexdigest()
    file = tmp_path / "grants.json"
    file.write_text(json.dumps({principal: ["inspect", "access"]}))
    policy = FileScopePolicy(file, "shop")
    assert policy.allows(principal, "shop", "access")
    first = policy.revision()
    file.write_text("{}")
    assert not policy.allows(principal, "shop", "access")
    assert policy.revision() > first
    file.write_text("{broken")
    assert not policy.has_grant(principal, ["shop"])
