"""Kubeconfig-to-provider factory: explicit authority and identity checks."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from piceli.k8s.ops.discovery import EvidenceSource
from piceli.k8s.ops.provider_factory import (
    KubeconfigTarget,
    NodeExpectation,
    ProviderFactoryError,
    api_client_from_kubeconfig,
    build_provider,
)
from tests.acceptance.fake_api import TARGET, serve


def kubeconfig(
    path: Path, server: str, *, user: str = "{}", cluster_extra: str = ""
) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            current-context: ambient
            clusters:
            - name: c
              cluster: {{server: "{server}"{cluster_extra}}}
            users:
            - name: u
              user: {user}
            contexts:
            - name: explicit
              context: {{cluster: c, user: u}}
            - name: ambient
              context: {{cluster: c, user: u}}
            """
        )
    )
    return path


def target(path: Path, **kwargs) -> KubeconfigTarget:
    values = {
        "kubeconfig": path,
        "context": "explicit",
        "namespace": TARGET.namespace,
        "transport": "loopback-http",
    }
    values.update(kwargs)
    return KubeconfigTarget(**values)


def test_builds_identity_checked_loopback_provider(tmp_path):
    with serve() as (_, url):
        path = kubeconfig(tmp_path / "kc", url)
        binding = build_provider(
            target(path, cluster_uid="cluster-uid", namespace_uid="namespace-uid"),
            field_manager="manager",
            owner_id="owner",
        )
        try:
            assert binding.identity.cluster_uid == "cluster-uid"
            assert binding.target.cluster_id == "cluster-uid"
            assert binding.provider.provenance.source is EvidenceSource.LOOPBACK
            binding.provider.verify_target()
        finally:
            binding.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cluster_uid": "other"}, "cluster identity mismatch"),
        ({"namespace_uid": "other"}, "namespace identity mismatch"),
        ({"namespace": "absent"}, "does not exist"),
        (
            {"nodes": {"primary": NodeExpectation("missing-node")}},
            "not found",
        ),
    ],
)
def test_identity_mismatches_are_refused(tmp_path, kwargs, message):
    with serve() as (_, url):
        path = kubeconfig(tmp_path / "kc", url)
        with pytest.raises(ProviderFactoryError, match=message):
            build_provider(target(path, **kwargs), field_manager="m", owner_id="o")


@pytest.mark.parametrize(
    ("user", "extra", "transport", "message"),
    [
        ("{exec: {command: gcloud}}", "", "https", "exec/auth-provider"),
        ("{auth-provider: {name: oidc}}", "", "https", "exec/auth-provider"),
        ("{username: a, password: b}", "", "https", "unsupported"),
        ("{}", ", proxy-url: 'http://proxy'", "https", "proxied"),
        ("{}", ", insecure-skip-tls-verify: true", "https", "insecure"),
        ("{}", "", "https", "https"),
    ],
)
def test_unsafe_kubeconfigs_are_refused(tmp_path, user, extra, transport, message):
    path = kubeconfig(
        tmp_path / "kc", "http://127.0.0.1:1", user=user, cluster_extra=extra
    )
    with pytest.raises(ProviderFactoryError, match=message):
        build_provider(
            target(path, transport=transport), field_manager="m", owner_id="o"
        )


def test_loopback_transport_requires_literal_loopback(tmp_path):
    path = kubeconfig(tmp_path / "kc", "http://example.test:80")
    with pytest.raises(ProviderFactoryError, match="loopback"):
        build_provider(target(path), field_manager="m", owner_id="o")


def test_context_must_be_explicit_and_present(tmp_path):
    path = kubeconfig(tmp_path / "kc", "https://127.0.0.1:6443")
    with pytest.raises(ProviderFactoryError, match="explicit"):
        KubeconfigTarget(path, "", "demo")
    with pytest.raises(ProviderFactoryError, match="exactly one context"):
        api_client_from_kubeconfig(path, "missing")
    with pytest.raises(ProviderFactoryError, match="not found"):
        api_client_from_kubeconfig(tmp_path / "absent", "explicit")


def test_static_token_drops_the_sdk_refresh_hook(tmp_path):
    path = kubeconfig(
        tmp_path / "kc", "https://127.0.0.1:6443", user="{token: static-token}"
    )
    client = api_client_from_kubeconfig(path, "explicit")
    try:
        assert client.configuration.refresh_api_key_hook is None
        assert client.configuration.api_key["BearerToken"] == "Bearer static-token"
        assert client.configuration.host == "https://127.0.0.1:6443"
    finally:
        client.close()
