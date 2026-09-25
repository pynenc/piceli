"""Opt-in: a fresh agent with only the skill deploys and rolls back ``examples/shop`` on kind.

The same walkthrough as ``scripts/skill_check.py`` (every command quoted from
``skills/piceli/SKILL.md``, run from a copy of the skill), against a
disposable kind cluster instead of the fake API, with ``examples/shop``: its
Rust build, the node-loopback registry, the generated secret and the cache's
existing claim. Runs only when the variables name a disposable cluster::

    kind create cluster --name piceli-skill --kubeconfig /tmp/skill.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/skill.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-skill \\
    PICELI_KIND_NODE=piceli-skill-control-plane \\
      uv run pytest tests/integration/test_skill_kind.py

It needs ``docker`` on ``PATH`` and never reads the ambient kubeconfig. The
flow: plan → deploy with the approved hash → status → a change applied
inside the owner's policy → a stale hash refused and diagnosed → rollback
planned, then applied with its approved hash → status.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
NODE = os.environ.get("PICELI_KIND_NODE", "")
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "shop" / "app.py"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(2400),
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT and NODE and shutil.which("docker")),
        reason="set PICELI_KIND_KUBECONFIG, PICELI_KIND_CONTEXT and PICELI_KIND_NODE",
    ),
]


def _harness():
    spec = importlib.util.spec_from_file_location(
        "skill_check", ROOT / "scripts" / "skill_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def namespace():
    from kubernetes.client import CoreV1Api

    name = "skill-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(Path(KUBECONFIG), CONTEXT)
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name, api
    finally:
        api.delete_namespace(name)
        client.close()


def _module(path: Path, state: Path, change: int) -> None:
    """``examples/shop`` with the owner's policy; ``change`` makes a new release."""
    path.write_text(
        textwrap.dedent(
            f"""
            import importlib.util

            from piceli import ApprovalPolicy, NodeLoopbackRegistry, Pipeline

            spec = importlib.util.spec_from_file_location("shop_example", {str(EXAMPLE)!r})
            shop = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(shop)
            shop.app.config("revision", {{"change": "{change}"}})
            pipeline = Pipeline(
                shop.app, shop.target, build=shop.images,
                deliver=NodeLoopbackRegistry(port=5000, storage="1Gi"),
                secrets=shop.secrets, state_dir={str(state)!r},
                execution={{"readiness_seconds": 120, "max_seconds": 900}},
                auto_approve=ApprovalPolicy(max_objects=10),
            )
            """
        )
    )
    shutil.rmtree(path.parent / "__pycache__", ignore_errors=True)


def test_skill_deploys_and_rolls_back_the_shop(namespace, tmp_path) -> None:
    name, api = namespace
    harness = _harness()
    api.create_namespaced_persistent_volume_claim(
        name,
        {
            "metadata": {"name": "cache-state"},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "64Mi"}},
            },
        },
    )
    skill = harness.copy_skill(tmp_path)
    module = skill / "deploy_shop.py"
    _module(module, tmp_path / "state", 0)
    env = {
        "SHOP_KUBECONFIG": KUBECONFIG,
        "SHOP_CONTEXT": CONTEXT,
        "SHOP_NAMESPACE": name,
        "SHOP_NODE": NODE,
        # Never the ambient kubeconfig.
        "KUBECONFIG": str(tmp_path / "missing-kubeconfig"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    session = harness.Session(skill, env, pipeline="deploy_shop.py:pipeline")

    def live(index: int) -> None:
        data = api.read_namespaced_config_map("revision", name).data
        assert data == {"change": str(index)}, data

    harness.walkthrough(
        session,
        change=lambda index: _module(module, tmp_path / "state", index),
        check_image=live,
    )
