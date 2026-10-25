"""Opt-in: ``helm install``, ``upgrade`` and ``uninstall`` of a ``piceli bundle --helm`` chart.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-it --kubeconfig /tmp/it.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/it.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-it \\
      uv run pytest tests/integration/test_bundle_helm_kind.py

It needs ``kubectl`` and ``helm`` on ``PATH`` and network access for one
pinned public image. The test never reads the ambient kubeconfig.

1. ``helm install`` with the required settings and the imported token from a
   file: the StatefulSet becomes Ready, the generated Secret holds the token
   and two random values.
2. ``helm upgrade`` without the token: the Secret keeps every value.
3. ``helm uninstall``: the ClusterRole, its binding and the Secret are gone;
   the StatefulSet's claim stays (deleting the namespace removes it).

Everything it created is removed afterwards (release, namespace).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
APP = f"""
from piceli import (App, ClaimTemplate, Container, MemoryVolume, Pipeline, PodDefaults,
                    Random, Resources, Rule, Secrets, SecretVolume, Security, Target)
from piceli import HelmChart
from piceli.k8s.release_secret_spec import ImportSecretSpec

RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi")
app = App("agent", pod_defaults=PodDefaults(security=Security.restricted(
    user=10001, group=10001, fs_group=10001, read_only_root_filesystem=True)))
secrets = Secrets(
    api_token=ImportSecretSpec.model_validate({{"type": "import", "env": "API_TOKEN"}}),
    intake_token=Random(32),
)
private = app.secret("agent-private", {{
    "api-token": secrets.ref("api_token"), "intake-token": secrets.ref("intake_token")}})
settings = app.config("agent-settings", {{"team": "REPLACE"}})
reader = app.service_account("agent-reader", cluster_rules=[
    Rule(resources=["nodes"], verbs=["get", "list"])])
app.stateful_set(
    "agent", image="{BUSYBOX}", command=["sh", "-c", "sleep 3600"],
    env={{"TEAM": settings.key("team")}}, resources=RESOURCES,
    volumes={{"/data": ClaimTemplate("state", size="16Mi"),
             "/run/agent": SecretVolume(private, default_mode=0o440),
             "/tmp": MemoryVolume(size_limit="1Mi", name="tmp")}},
    sidecars=[Container(name="side", image="{BUSYBOX}", command=("sh", "-c", "sleep 3600"),
                        resources=RESOURCES)],
    service_account=reader, replicas=1, headless=False, service_name="agent",
)
pipeline = Pipeline(app, Target.profile("client", namespace="agent"), secrets=secrets)
chart = HelmChart(pipeline, required=("team",))
"""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(600),
    pytest.mark.skipif(
        not (
            KUBECONFIG and CONTEXT and shutil.which("kubectl") and shutil.which("helm")
        ),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT; needs kubectl and helm",
    ),
]


def _env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(home)}


def _tool(home: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        list(args), capture_output=True, text=True, timeout=400, env=_env(home)
    )
    if check and result.returncode:
        raise AssertionError(f"{args[0]} {args[3:5]} failed: {result.stderr[-2000:]}")
    return result.stdout


def test_install_upgrade_and_uninstall(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(textwrap.dedent(APP))
    out = tmp_path / "chart"
    cwd = Path.cwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(
            cli, ["bundle", "app.py:chart", "--helm", "--out", str(out)]
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    namespace = f"piceli-helm-{uuid.uuid4().hex[:8]}"
    token = tmp_path / "token"
    token.write_text("token-from-file\n")
    kube = ("--kubeconfig", KUBECONFIG)

    def helm(*args: str, check: bool = True) -> str:
        return _tool(
            tmp_path, "helm", *kube, "--kube-context", CONTEXT, *args, check=check
        )

    def kubectl(*args: str, check: bool = True) -> str:
        return _tool(
            tmp_path, "kubectl", *kube, "--context", CONTEXT, *args, check=check
        )

    def secret() -> dict[str, str]:
        body = json.loads(
            kubectl("-n", namespace, "get", "secret", "agent-private", "-o", "json")
        )
        return dict(body["data"])

    try:
        helm(
            "install", "agent", str(out), "-n", namespace, "--create-namespace",
            "--set", "team=t1", "--set-file", f"apiToken={token}",
            "--wait", "--timeout", "5m",
        )  # fmt: skip
        first = secret()
        assert set(first) == {"api-token", "intake-token"}
        cluster = f"{namespace}:agent:agent-reader"
        assert kubectl("get", "clusterrole", cluster, "-o", "name").strip()
        helm(
            "upgrade", "agent", str(out), "-n", namespace, "--set", "team=t2",
            "--wait", "--timeout", "5m",
        )  # fmt: skip
        assert secret() == first, "helm upgrade changed the generated Secret"
        settings = json.loads(
            kubectl("-n", namespace, "get", "configmap", "agent-settings", "-o", "json")
        )
        assert settings["data"]["team"] == "t2"
        helm("uninstall", "agent", "-n", namespace, "--wait", "--timeout", "5m")
        assert not kubectl(
            "get",
            "clusterrole,clusterrolebinding",
            cluster,
            "--ignore-not-found",
            "-o",
            "name",
        ).strip()
        assert not kubectl(
            "-n",
            namespace,
            "get",
            "secret",
            "agent-private",
            "--ignore-not-found",
            "-o",
            "name",
        ).strip()
        claims = kubectl("-n", namespace, "get", "pvc", "-o", "name").split()
        assert claims == ["persistentvolumeclaim/state-agent-0"]
    finally:
        helm("uninstall", "agent", "-n", namespace, "--ignore-not-found", check=False)
        kubectl(
            "delete",
            "namespace",
            namespace,
            "--ignore-not-found",
            "--wait=true",
            check=False,
        )
