"""Opt-in ``piceli release`` through an exec credential plugin on kind.

kind only issues client certificates, so an exec plugin is emulated: a local
script prints the kind admin certificate and key as an ExecCredential
(``clientCertificateData``/``clientKeyData``) with a short expiry, and the
release runs through a kubeconfig whose user is that ``exec`` plugin. Runs
only when both variables name a disposable cluster, for example::

    kind create cluster --name piceli-exec --kubeconfig /tmp/exec.kubeconfig
    PICELI_KIND_KUBECONFIG=/tmp/exec.kubeconfig \\
    PICELI_KIND_CONTEXT=kind-piceli-exec \\
      uv run pytest tests/integration/test_exec_auth_kind.py

The test never reads the ambient kubeconfig. The script reads the named kind
kubeconfig itself; Piceli never writes the certificate or key to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.exec_credentials import ExecPolicy
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

KUBECONFIG = os.environ.get("PICELI_KIND_KUBECONFIG", "")
CONTEXT = os.environ.get("PICELI_KIND_CONTEXT", "")
DIGEST = "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "release"
EXPIRES_IN = 6  # seconds: refreshed after ~3 s, several times per apply

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (KUBECONFIG and CONTEXT),
        reason="set PICELI_KIND_KUBECONFIG and PICELI_KIND_CONTEXT",
    ),
]

PLUGIN = """\
#!{python}
import json, os, sys, base64
from datetime import datetime, timedelta, timezone
import yaml
here = os.path.dirname(os.path.abspath(__file__))
counter = os.path.join(here, "runs")
runs = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
open(counter, "w").write(str(runs))
document = yaml.safe_load(open(os.environ["SOURCE_KUBECONFIG"]))
context = next(c["context"] for c in document["contexts"]
               if c["name"] == os.environ["SOURCE_CONTEXT"])
user = next(u["user"] for u in document["users"] if u["name"] == context["user"])
at = datetime.now(timezone.utc) + timedelta(seconds={expires})
json.dump({{
    "apiVersion": "client.authentication.k8s.io/v1",
    "kind": "ExecCredential",
    "status": {{
        "clientCertificateData":
            base64.b64decode(user["client-certificate-data"]).decode(),
        "clientKeyData": base64.b64decode(user["client-key-data"]).decode(),
        "expirationTimestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }},
}}, sys.stdout)
"""


@pytest.fixture
def exec_kubeconfig(tmp_path: Path) -> tuple[Path, Path]:
    """An exec-plugin kubeconfig for the kind cluster, and its plugin."""
    source = yaml.safe_load(Path(KUBECONFIG).read_text())
    context = next(c["context"] for c in source["contexts"] if c["name"] == CONTEXT)
    cluster = next(
        c["cluster"] for c in source["clusters"] if c["name"] == context["cluster"]
    )
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(mode=0o700)
    plugin = plugin_dir / "kind-exec-credential"
    plugin.write_text(PLUGIN.format(python=sys.executable, expires=EXPIRES_IN))
    plugin.chmod(0o700)
    user = {
        "exec": {
            "apiVersion": "client.authentication.k8s.io/v1",
            "command": str(plugin),
            "interactiveMode": "Never",
            "env": [
                {"name": "SOURCE_KUBECONFIG", "value": str(Path(KUBECONFIG))},
                {"name": "SOURCE_CONTEXT", "value": CONTEXT},
            ],
        }
    }
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "kind", "cluster": cluster}],
        "users": [{"name": "exec-user", "user": user}],
        "contexts": [
            {"name": "kind-exec", "context": {"cluster": "kind", "user": "exec-user"}}
        ],
    }
    path = tmp_path / "exec.kubeconfig"
    path.write_text(yaml.safe_dump(document))
    path.chmod(0o600)
    return path, plugin


@pytest.fixture
def namespace(exec_kubeconfig):
    from kubernetes.client import CoreV1Api

    path, _ = exec_kubeconfig
    name = "exec-" + uuid.uuid4().hex[:8]
    client = api_client_from_kubeconfig(path, "kind-exec", exec_policy=ExecPolicy(True))
    api = CoreV1Api(client)
    api.create_namespace({"metadata": {"name": name}})
    try:
        yield name
    finally:
        api.delete_namespace(name)
        client.close()


def _spec(directory: Path, kubeconfig: Path, namespace: str, exec_keys: str) -> Path:
    shutil.copy(EXAMPLE / "composition.py", directory / "composition.py")
    path = directory / "release.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "{kubeconfig}"
            context = "kind-exec"
            namespace = "{namespace}"
            {exec_keys}

            [release]
            name = "web"
            owner = "exec-e2e"
            field_manager = "exec-e2e"
            composition = "composition.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 300
            readiness_seconds = 240

            [images]
            web = "docker.io/library/nginx@{DIGEST}"

            [secrets.api-token]
            type = "random"

            [secrets.web-tls]
            type = "tls-self-signed"
            dns_names = ["web.{namespace}.svc"]
            """
        )
    )
    return path


def _run(spec: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _runs(plugin: Path) -> int:
    counter = plugin.parent / "runs"
    return int(counter.read_text()) if counter.exists() else 0


def test_release_plan_and_apply_through_an_exec_plugin(
    tmp_path, exec_kubeconfig, namespace
):
    path, plugin = exec_kubeconfig
    before = _runs(plugin)
    spec = _spec(tmp_path, path, namespace, "")
    code, refused = _run(spec, "plan")
    assert code != 0
    assert refused["code"] == "exec-auth-not-allowed"
    assert _runs(plugin) == before  # never run without opt-in

    pin = "sha256:" + hashlib.sha256(plugin.read_bytes()).hexdigest()
    spec = _spec(tmp_path, path, namespace, f'allow_exec = true\nexec_sha256 = "{pin}"')
    code, planned = _run(spec, "plan")
    assert code == 0, planned
    code, applied = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied

    from kubernetes.client import AppsV1Api

    client = api_client_from_kubeconfig(
        path, "kind-exec", exec_policy=ExecPolicy(True, sha256=pin)
    )
    try:
        deployment = AppsV1Api(client).read_namespaced_deployment("web", namespace)
        assert deployment.spec.template.spec.containers[0].image.endswith(DIGEST)
        # The Piceli-owned hook re-runs the pinned plugin once it nears expiry.
        source = client.configuration.refresh_api_key_hook.__self__
        runs = source.runs
        time.sleep(EXPIRES_IN / 2 + 0.5)
        AppsV1Api(client).read_namespaced_deployment("web", namespace)
        assert source.runs == runs + 1
    finally:
        client.close()
    assert _runs(plugin) - before >= 4
