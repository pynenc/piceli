"""observe/operator reach a cluster only through an explicit, named context.

No test reaches a cluster: the dynamic client is replaced by a recorder, and
kubectl is a path that does not exist (it must never be started).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.observe import app as observe_app
from piceli.k8s.cli.operator import app as operator_app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

runner = CliRunner()
LAB = "https://127.0.0.1:6443"
PROD = "https://prod.example.test:443"


def kubeconfig(path: Path, *, lab_user: str = "{token: lab-token}", extra="") -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            current-context: prod
            clusters:
            - name: lab
              cluster: {{server: "{LAB}"{extra}}}
            - name: prod
              cluster: {{server: "{PROD}"}}
            users:
            - name: lab
              user: {lab_user}
            - name: prod
              user: {{token: prod-token}}
            contexts:
            - name: lab
              context: {{cluster: lab, user: lab}}
            - name: prod
              context: {{cluster: prod, user: prod}}
            """
        )
    )
    return path


@pytest.mark.parametrize(
    ("app", "argv"),
    [
        (observe_app, ["status", "--archive", "{archive}"]),
        (observe_app, ["serve", "--archive", "{archive}"]),
        (observe_app, ["logs-run", "--namespace", "d", "--target", "pod/a"]),
        (observe_app, ["logs-command", "--namespace", "d", "--target", "pod/a"]),
        (observe_app, ["forward-run", "--user", "u", "--name", "n"]),
        (observe_app, ["forwards", "apply", "--profile", "{archive}"]),
        (operator_app, ["status"]),
        (operator_app, ["serve"]),
    ],
)
def test_context_is_required(tmp_path, app, argv):
    archive = tmp_path / "archive.json"
    archive.write_text("{}")
    path = kubeconfig(tmp_path / "kc")
    argv = [item.replace("{archive}", str(archive)) for item in argv]
    with patch("kubernetes.dynamic.DynamicClient") as dynamic:
        result = runner.invoke(app, [*argv, "--kubeconfig", str(path)])
    assert result.exit_code == 2
    assert "--context" in result.output
    dynamic.assert_not_called()


class Recorder:
    clients: list[Any] = []

    def __init__(self, client: Any) -> None:
        Recorder.clients.append(client)


def test_named_context_is_used_never_current_context(tmp_path):
    archive = tmp_path / "archive.json"
    archive.write_text("{}")
    path = kubeconfig(tmp_path / "kc")
    Recorder.clients = []
    with (
        patch("kubernetes.dynamic.DynamicClient", Recorder),
        patch("piceli.k8s.cli.observe._archive", return_value=object()),
        patch("piceli.k8s.cli.observe.observe_session") as session,
    ):
        session.return_value.to_dict.return_value = {}
        result = runner.invoke(
            observe_app,
            ["status", "--archive", str(archive)]
            + ["--kubeconfig", str(path), "--context", "lab"],
        )
    assert result.exit_code == 0, result.output
    (client,) = Recorder.clients
    assert client.configuration.host == LAB
    assert client.configuration.api_key["BearerToken"] == "Bearer lab-token"
    assert client.configuration.refresh_api_key_hook is None


def test_operator_status_uses_the_factory(tmp_path):
    path = kubeconfig(tmp_path / "kc", extra=", proxy-url: 'http://proxy.test'")
    with patch("kubernetes.dynamic.DynamicClient") as dynamic:
        result = runner.invoke(
            operator_app, ["status", "--kubeconfig", str(path), "--context", "lab"]
        )
    assert result.exit_code == 2
    refusal = json.loads(result.stdout)
    assert refusal == {
        "state": "refused",
        "reason": "proxied API transport is not supported",
        "code": "target-refused",
    }
    dynamic.assert_not_called()


def test_kubectl_commands_refuse_before_starting_kubectl(tmp_path):
    exec_user = "{exec: {apiVersion: client.authentication.k8s.io/v1, command: absent}}"
    path = kubeconfig(tmp_path / "kc", lab_user=exec_user)
    missing = str(tmp_path / "no-kubectl")
    base = ["logs-run", "--namespace", "d", "--target", "pod/a"]
    base += ["--kubeconfig", str(path), "--kubectl", missing]
    with patch("piceli.k8s.cli.observe.run_logs") as run:
        refused = runner.invoke(observe_app, [*base, "--context", "lab"])
        unknown = runner.invoke(observe_app, [*base, "--context", "staging"])
        allowed = runner.invoke(
            observe_app, [*base, "--context", "lab", "--allow-exec"]
        )
    run.assert_not_called()
    assert json.loads(refused.stdout)["code"] == "exec-auth-not-allowed"
    assert json.loads(unknown.stdout)["code"] == "target-refused"
    # Allowed, but the plugin command does not exist: still refused, not run.
    assert json.loads(allowed.stdout)["code"] == "exec-command-not-found"


def test_kubectl_argv_always_names_the_context(tmp_path):
    path = kubeconfig(tmp_path / "kc")
    result = runner.invoke(
        observe_app,
        ["logs-command", "--namespace", "d", "--target", "pod/a"]
        + ["--kubeconfig", str(path), "--context", "lab"],
    )
    argv = json.loads(result.stdout)
    assert argv[1:5] == ["--kubeconfig", str(path), "--context", "lab"]


def test_static_client_certificates_stay_in_memory(tmp_path, monkeypatch):
    import base64

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not available")
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes"]
        + ["-subj", "/CN=lab", "-days", "1"]
        + ["-keyout", str(tmp_path / "k.pem"), "-out", str(tmp_path / "c.pem")],
        check=True,
        capture_output=True,
    )
    cert = base64.b64encode((tmp_path / "c.pem").read_bytes()).decode()
    key = base64.b64encode((tmp_path / "k.pem").read_bytes()).decode()
    path = kubeconfig(
        tmp_path / "kc",
        lab_user=f"{{client-certificate-data: {cert}, client-key-data: {key}}}",
        extra=f", certificate-authority-data: {cert}",
    )

    def no_temp_files(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("credentials must not be written to disk")

    monkeypatch.setattr(tempfile, "mkstemp", no_temp_files)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_temp_files)
    client = api_client_from_kubeconfig(path, "lab")
    try:
        assert client.configuration.cert_file is None
        assert client.configuration.key_file is None
        assert client.configuration.ssl_ca_cert is None
        pool = client.rest_client.pool_manager
        assert pool.connection_pool_kw["ssl_context"].verify_mode.name == (
            "CERT_REQUIRED"
        )
    finally:
        client.close()
