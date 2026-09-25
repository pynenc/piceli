"""``piceli.testing``: the public fake Kubernetes API for consumers' tests."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import piceli.testing as testing
from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.k8s.ops.provider_factory import KubeconfigTarget, build_provider
from tests.acceptance import fake_api as shim


def test_import_is_lazy_and_side_effect_free() -> None:
    code = textwrap.dedent(
        """
        import json, socket, sys
        opened = []
        socket.socket.connect = lambda *a, **k: opened.append(a)
        import piceli.testing
        print(json.dumps({
            "fake_api": "piceli.testing.fake_api" in sys.modules,
            "kubernetes": "kubernetes" in sys.modules,
            "opened": len(opened),
        }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == {
        "fake_api": False,
        "kubernetes": False,
        "opened": 0,
    }


def test_public_names() -> None:
    assert set(testing.__all__) <= set(dir(testing))
    for name in testing.__all__:
        assert getattr(testing, name) is getattr(testing.fake_api, name)


def test_the_acceptance_shim_reexports_the_same_objects() -> None:
    assert shim.FakeAPI is testing.FakeAPI
    assert shim.serve is testing.serve
    assert shim.TARGET is testing.TARGET


def test_fake_cluster_serves_the_real_client() -> None:
    with testing.fake_cluster() as cluster:
        cluster.api.put(testing.manifest("ConfigMap", "settings", value="on"))
        found = cluster.provider.get(
            ResourceIdentity("v1", "ConfigMap", cluster.namespace, "settings")
        )
        assert found is not None and found.manifest["data"] == {"mode": "on"}
        cluster.api.put(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "api", "namespace": cluster.namespace},
                "spec": {"ports": [{"port": 80}]},
            }
        )
        service = cluster.api.objects[("Service", "api")]["spec"]
        assert service["clusterIP"].startswith("10.96.")


def test_kubeconfig_works_with_the_explicit_provider_factory(tmp_path: Path) -> None:
    with testing.fake_cluster() as cluster:
        path = cluster.kubeconfig(tmp_path / "kubeconfig", context="unit")
        binding = build_provider(
            KubeconfigTarget(
                path, "unit", cluster.namespace, transport="loopback-http"
            ),
            field_manager="unit",
            owner_id="unit",
        )
        try:
            assert binding.identity.cluster_uid == "cluster-uid"
        finally:
            binding.close()


def test_custom_types_and_injected_faults() -> None:
    api = testing.FakeAPI(types={"configmaps": ("v1", "ConfigMap", True)})
    with testing.serve(api) as (served, url):
        assert served is api
        provider = testing.provider_at(url)
        try:
            api.inject("GET", "/configmaps/settings", status=503)
            api.put(testing.manifest("ConfigMap", "settings"))
            identity = ResourceIdentity(
                "v1", "ConfigMap", testing.TARGET.namespace, "settings"
            )
            try:
                provider.get(identity)
                raise AssertionError("the injected fault did not fire")
            except Exception as error:
                assert getattr(error, "category", "") == "api-unavailable"
            assert provider.get(identity) is not None
        finally:
            provider.client.close()


def test_pytest_plugin_fixture(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        'pytest_plugins = ["piceli.testing.pytest_plugin"]\n'
    )
    (tmp_path / "test_consumer.py").write_text(
        textwrap.dedent(
            """
            from piceli.testing import manifest

            def test_uses_fixture(piceli_fake_cluster):
                piceli_fake_cluster.api.put(manifest("ConfigMap", "x"))
                assert ("ConfigMap", "x") in piceli_fake_cluster.api.objects
                assert piceli_fake_cluster.url.startswith("http://127.0.0.1:")
            """
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_a_fake_api_can_serve_the_app_s_own_namespace() -> None:
    api = testing.FakeAPI(namespace="my-app")
    assert ("Namespace", "my-app") in api.objects
    api.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "settings", "namespace": "my-app"},
            "data": {"mode": "on"},
        }
    )
    with testing.serve(api) as (_, url):

        def status(namespace: str) -> int:
            path = f"{url}/api/v1/namespaces/{namespace}/configmaps/settings"
            try:
                with urlopen(path, timeout=5) as response:
                    return int(response.status)
            except HTTPError as error:
                return error.code

        assert status("my-app") == 200
        assert status(testing.TARGET.namespace) == 403
