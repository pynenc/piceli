"""Acceptance: ``http`` and ``metric`` checks reach the app through the API server proxy.

A controller pod has no ``kubectl``, so the checks must not need one: they GET
``services/NAME:PORT/proxy/PATH`` (or a ready pod's proxy for a Deployment)
through the same explicit kubeconfig as every other API call. The context is
built with a ``kubectl`` that does not exist, as in the controller image.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


import pytest

from piceli.checks import CheckContext, Checks, run_checks
from piceli.testing import FakeAPI, manifest, serve, write_kubeconfig


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[FakeAPI, CheckContext]]:
    with serve() as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig", context="fake")
        service = manifest("Service", "web")
        service["spec"] = {"selector": {"app": "web"}, "ports": [{"port": 80}]}
        api.put(service)
        api.put(manifest("Deployment", "web"))
        pod = manifest("Pod", "web-1")
        pod["metadata"]["labels"] = {"app": "web"}
        pod["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"
        pod["spec"] = {"containers": [{"name": "web", "image": "x"}]}
        pod["status"] = {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
        api.put(pod)
        with CheckContext(
            kubeconfig,
            "fake",
            api.namespace,
            "shop-1",
            transport="loopback-http",
            kubectl=str(tmp_path / "no-kubectl"),
        ) as context:
            yield api, context


def _proxied(api: FakeAPI) -> list[str]:
    return [
        request["path"] for request in api.requests if "/proxy" in request["path"]
    ]


def test_an_http_check_passes_without_kubectl(cluster) -> None:
    api, context = cluster
    api.proxy("service/web", 80, "/login", body=b"<form>sign in</form>")
    report = run_checks(
        [Checks.http("service/web", "/login", body_contains="sign in", retries=0)],
        context,
    )
    assert report.passed, report.to_dict()
    assert _proxied(api) == [
        f"/api/v1/namespaces/{api.namespace}/services/web:80/proxy/login"
    ]


def test_a_deployment_target_is_reached_through_a_ready_pod(cluster) -> None:
    api, context = cluster
    api.proxy("pod/web-1", 8080, "/healthz", status=204)
    report = run_checks(
        [Checks.http("deployment/web", "/healthz", port=8080, retries=0)], context
    )
    assert report.passed, report.to_dict()
    assert _proxied(api) == [
        f"/api/v1/namespaces/{api.namespace}/pods/web-1:8080/proxy/healthz"
    ]


def test_the_status_and_body_of_the_app_decide_the_check(cluster) -> None:
    api, context = cluster
    api.proxy("service/web", 80, "/broken", status=500, body=b"oops")
    (result,) = run_checks(
        [Checks.http("service/web", "/broken", retries=0)], context
    ).results
    assert (result.passed, result.code) == (False, "check-failed")
    assert "returned 500" in result.detail


def test_a_metric_check_queries_through_the_proxy(cluster) -> None:
    api, context = cluster
    document = {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"value": [0, "0.01"]}]},
    }
    api.proxy("service/web", 80, "/api/v1/query", body=json.dumps(document).encode())
    report = run_checks(
        [Checks.metric("service/web", "errors", op="<", threshold=0.05, retries=0)],
        context,
    )
    assert report.passed, report.to_dict()
    (request,) = [r for r in api.requests if "/proxy" in r["path"]]
    assert request["query"] == {"query": ["errors"]}


def test_a_refused_proxy_names_the_permission_not_the_server_message(cluster) -> None:
    api, context = cluster
    api.forbid_proxy = True
    (result,) = run_checks(
        [Checks.http("service/web", "/login", retries=0)], context
    ).results
    assert result.code == "check-forward-unavailable"
    assert "services/proxy" in result.detail and "HTTP 403" in result.detail
    assert "forbidden" not in result.detail.lower().replace("http 403", "")


def test_no_endpoint_is_a_forward_failure(cluster) -> None:
    api, context = cluster
    (result,) = run_checks(
        [Checks.http("service/web", "/nothing", retries=0)], context
    ).results
    assert result.code == "check-forward-unavailable"
    assert "HTTP 503" in result.detail




def test_a_refused_proxy_falls_back_to_kubectl_when_it_is_installed(
    cluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from piceli.checks import context as context_module

    api, context = cluster
    api.forbid_proxy = True
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\nexit 0\n")
    kubectl.chmod(0o700)
    context.kubectl = str(kubectl)
    forwards: list[tuple[str, int]] = []

    @contextmanager
    def forward(_: Any, target: str, port: int) -> Iterator[str]:
        forwards.append((target, port))
        raise context_module.CheckError("check-forward-unavailable", "stub forward")
        yield ""  # pragma: no cover

    monkeypatch.setattr(context_module, "supervised_forward", forward)
    (result,) = run_checks(
        [Checks.http("service/web", "/login", retries=0)], context
    ).results
    assert forwards == [("service/web", 80)]
    assert result.detail == "stub forward"
