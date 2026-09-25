"""Opt-in: an operator-like writer shares a custom resource Piceli manages (kind).

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-m2 --kubeconfig /tmp/m2.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/m2.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-m2 \\
      uv run pytest tests/integration/test_ownership_operator_kind.py

The test installs a small CRD (``Widget``), releases one Widget, and plays
the operator with ``kubectl --server-side --field-manager=widget-operator``:
it writes ``spec.color`` (a field the release does not declare) and the
``status`` subresource. Piceli must keep both across a new release and a
rollback, show no drift for them, and report a field both declare (the
operator took ``spec.size``) as drift naming the operator. The CRD is
deleted at the end.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from kind_support import (
    cli,
    get,
    kubectl,
    managers,
    operations,
    requires_kind,
    write_spec,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(900), requires_kind]

CRD = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: widgets.m2.piceli.test}
spec:
  group: m2.piceli.test
  scope: Namespaced
  names: {plural: widgets, singular: widget, kind: Widget}
  versions:
  - name: v1
    served: true
    storage: true
    subresources: {status: {}}
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            properties:
              size: {type: integer}
              color: {type: string}
          status:
            type: object
            properties:
              phase: {type: string}
              conditions:
                type: array
                items:
                  type: object
                  properties:
                    type: {type: string}
                    status: {type: string}
"""

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    widget = ResourceIntent.from_manifest({{
        "apiVersion": "m2.piceli.test/v1", "kind": "Widget",
        "metadata": {{"name": "shop", "namespace": ctx.namespace}},
        "spec": {{"size": {size}}},
    }})
    return DeploymentComposition((DeploymentComponent("widgets", (widget,)),))
"""


@pytest.fixture(scope="module")
def crd() -> Iterator[None]:
    kubectl("apply", "-f", "-", stdin=CRD)
    try:
        kubectl(
            "wait",
            "--for=condition=Established",
            "crd/widgets.m2.piceli.test",
            "--timeout=120s",
        )
        yield
    finally:
        kubectl("delete", "crd", "widgets.m2.piceli.test", "--wait=false", check=False)


def _operator(namespace: str, spec: dict, *, force: bool = False) -> None:
    body = {
        "apiVersion": "m2.piceli.test/v1",
        "kind": "Widget",
        "metadata": {"name": "shop", "namespace": namespace},
        "spec": spec,
    }
    kubectl(
        "apply",
        "--server-side",
        "--field-manager=widget-operator",
        *(["--force-conflicts"] if force else []),
        "-f",
        "-",
        namespace=namespace,
        stdin=json.dumps(body),
    )


def _status(namespace: str) -> None:
    kubectl(
        "patch",
        "widget",
        "shop",
        "--subresource=status",
        "--type=merge",
        "--field-manager=widget-operator",
        "-p",
        json.dumps(
            {
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                }
            }
        ),
        namespace=namespace,
    )


def test_operator_fields_survive_releases_and_conflicts_are_reported(
    tmp_path: Path, kind_namespace: str, crd: None
) -> None:
    namespace = kind_namespace
    for size in (1, 2):
        (tmp_path / f"size{size}.py").write_text(COMPOSITION.format(size=size))

    spec = write_spec(tmp_path, namespace, "size1.py")
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    first = applied["release"]

    _operator(namespace, {"color": "blue"})
    _status(namespace)

    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert operations(planned) == {"Widget/shop": "no-op"}, planned["diffs"]
    assert planned["drift"] == []

    spec = write_spec(tmp_path, namespace, "size2.py")
    code, applied = cli(spec, "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    live = get("widget", "shop", namespace)
    assert live["spec"] == {"size": 2, "color": "blue"}
    assert live["status"]["phase"] == "Running"
    owned = managers(live)
    assert "f:color" in json.dumps(owned["widget-operator"]["fieldsV1"])
    assert "f:color" not in json.dumps(owned["m2-e2e"]["fieldsV1"])

    # The operator takes a field the release declares: the plan says so.
    _operator(namespace, {"color": "blue", "size": 5}, force=True)
    code, planned = cli(spec, "plan")
    assert code == 0, planned
    assert operations(planned) == {"Widget/shop": "apply"}
    assert [item["managers"] for item in planned["drift"]] == [["widget-operator"]]
    (diff,) = planned["diffs"]
    assert [(c["path"], c["before"], c["after"]) for c in diff["changes"]] == [
        ("/spec/size", 5, 2)
    ]

    # Rolling back rewrites only what the release declares.
    code, rolled = cli(spec, "rollback", first, "--auto-approve")
    assert code == 0 and rolled["selected"] == first, rolled
    live = get("widget", "shop", namespace)
    assert live["spec"] == {"size": 1, "color": "blue"}
    assert live["status"]["phase"] == "Running"
