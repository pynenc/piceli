"""Opt-in: ``helm install``, ``upgrade`` and ``uninstall`` of a ``piceli bundle --helm`` chart.

Runs only when the variables name a disposable cluster, for example::

    kind create cluster --name piceli-it --kubeconfig /tmp/it.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/it.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-it \\
      uv run pytest tests/integration/test_bundle_helm_kind.py

It needs ``kubectl`` and ``helm`` on ``PATH`` and network access for one
pinned public image. The test never reads the ambient kubeconfig.

1. ``helm install`` with the required settings and the imported token from a
   file: the StatefulSet becomes Ready, the generated Secret holds the token,
   a random value and a private CA with one leaf certificate.
2. ``helm upgrade`` without the token: the Secret keeps every value, the
   certificates included.
3. ``helm uninstall``: the ClusterRole, its binding and the Secret are gone;
   the StatefulSet's claim stays (deleting the namespace removes it).

A second test (0.80.0) uses an app with two Secrets and an optional value:

4. ``tokenSecret`` set to a Secret the test made: the chart renders the other
   Secret and not the replaced one, and the install needs no ``--set-file``.
5. ``helm upgrade --reuse-values`` from a release made without the optional
   value keeps its default (the optional ConfigMap is installed); an explicit
   ``flags.enabled=false`` removes it.

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
                    Random, Resources, Rule, Secrets, SecretVolume, Security, Target,
                    TlsCa)
from piceli import HelmChart
from piceli.k8s.release_secret_spec import ImportSecretSpec

RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi")
app = App("agent", pod_defaults=PodDefaults(security=Security.restricted(
    user=10001, group=10001, fs_group=10001, read_only_root_filesystem=True)))
secrets = Secrets(
    api_token=ImportSecretSpec.model_validate({{"type": "import", "env": "API_TOKEN"}}),
    intake_token=Random(32),
    tls=TlsCa({{"intake": ["agent-intake", "agent-intake.agent.svc"]}}),
)
private = app.secret("agent-private", {{
    "api-token": secrets.ref("api_token"), "intake-token": secrets.ref("intake_token"),
    "ca.crt": secrets.ref("tls.ca.crt"), "tls.crt": secrets.ref("tls.intake.crt"),
    "tls.key": secrets.ref("tls.intake.key")}})
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

EXISTING_APP = """
from piceli import (App, HelmChart, HelmOptional, MemoryVolume, Pipeline, PodDefaults,
                    Random, Resources, Secrets, SecretVolume, Security, Target)
from piceli.k8s.release_secret_spec import ImportSecretSpec

RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi")
app = App("agent", pod_defaults=PodDefaults(security=Security.restricted(
    user=10001, group=10001, fs_group=10001, read_only_root_filesystem=True)))
secrets = Secrets(
    api_token=ImportSecretSpec.model_validate({"type": "import", "env": "API_TOKEN"}),
    extra_key=Random(32),
)
token = app.secret("agent-token", {"api-token": secrets.ref("api_token")})
extra = app.secret("agent-extra", {"extra-key": secrets.ref("extra_key")})
flags = app.config("agent-flags", {"mode": "on"})
app.deployment(
    "agent", image="@@IMAGE@@", command=["sh", "-c", "sleep 3600"], resources=RESOURCES,
    volumes={"/run/token": SecretVolume(token, default_mode=0o440),
             "/run/extra": SecretVolume(extra, default_mode=0o440),
             "/tmp": MemoryVolume(size_limit="1Mi", name="tmp")},
)
pipeline = Pipeline(app, Target.profile("client", namespace="agent"), secrets=secrets)
chart = HelmChart(pipeline, existing_secrets={"tokenSecret": ["agent-token"]}@@OPTIONAL@@)
"""
FLAGS = ', optional={"flags.enabled": HelmOptional("flags", objects=[flags], default=True, default_on_reuse=True)}'

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
        assert set(first) == {
            "api-token", "intake-token", "ca.crt", "tls.crt", "tls.key"
        }  # fmt: skip
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


def _chart(tmp_path: Path, name: str, optional: str) -> Path:
    """The two-Secret app as a chart in ``tmp_path/name`` (no cluster needed)."""
    source = EXISTING_APP.replace("@@IMAGE@@", BUSYBOX).replace(
        "@@OPTIONAL@@", optional
    )
    directory = tmp_path / f"{name}-src"
    directory.mkdir()
    (directory / "app.py").write_text(source)
    out = tmp_path / name
    cwd = Path.cwd()
    os.chdir(directory)
    try:
        result = CliRunner().invoke(
            cli, ["bundle", "app.py:chart", "--helm", "--out", str(out)]
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    return out


def test_one_existing_secret_and_optional_defaults_on_reuse(tmp_path: Path) -> None:
    older = _chart(tmp_path, "older", "")  # a release made before the optional
    newer = _chart(tmp_path, "newer", FLAGS)
    namespace = f"piceli-helm-{uuid.uuid4().hex[:8]}"
    kube = ("--kubeconfig", KUBECONFIG)

    def helm(*args: str, check: bool = True) -> str:
        return _tool(
            tmp_path, "helm", *kube, "--kube-context", CONTEXT, *args, check=check
        )

    def kubectl(*args: str, check: bool = True) -> str:
        return _tool(
            tmp_path, "kubectl", *kube, "--context", CONTEXT, *args, check=check
        )

    def present(kind: str, name: str) -> bool:
        found = kubectl(
            "-n", namespace, "get", kind, name, "--ignore-not-found", "-o", "name",
            check=False,
        )  # fmt: skip
        return bool(found.strip())

    try:
        kubectl("create", "namespace", namespace)
        kubectl(
            "-n", namespace, "create", "secret", "generic", "client-token",
            "--from-literal=api-token=made-by-the-client",
        )  # fmt: skip
        # Only tokenSecret is set: no --set-file, the client's Secret is read,
        # the chart makes the other Secret and not the replaced one.
        helm(
            "install", "agent", str(older), "-n", namespace,
            "--set", "tokenSecret=client-token", "--wait", "--timeout", "5m",
        )  # fmt: skip
        assert present("secret", "agent-extra")
        assert not present("secret", "agent-token")
        pod = json.loads(
            kubectl("-n", namespace, "get", "deployment", "agent", "-o", "json")
        )
        volumes = pod["spec"]["template"]["spec"]["volumes"]
        assert {v["secret"]["secretName"] for v in volumes if "secret" in v} == {
            "client-token", "agent-extra",
        }  # fmt: skip
        extra = kubectl("-n", namespace, "get", "secret", "agent-extra", "-o", "json")
        assert present("configmap", "agent-flags")  # a plain object of the older chart
        # The newer chart, over the stored values of the older release.
        helm(
            "upgrade", "agent", str(newer), "-n", namespace, "--reuse-values",
            "--wait", "--timeout", "5m",
        )  # fmt: skip
        assert present("configmap", "agent-flags"), "reuse-values dropped the default"
        assert (
            kubectl("-n", namespace, "get", "secret", "agent-extra", "-o", "json")
            == extra
        )
        helm(
            "upgrade", "agent", str(newer), "-n", namespace, "--reuse-values",
            "--set", "flags.enabled=false", "--wait", "--timeout", "5m",
        )  # fmt: skip
        assert not present("configmap", "agent-flags"), (
            "explicit false must turn it off"
        )
    finally:
        helm("uninstall", "agent", "-n", namespace, "--ignore-not-found", check=False)
        kubectl(
            "delete", "namespace", namespace, "--ignore-not-found", "--wait=true",
            check=False,
        )  # fmt: skip
