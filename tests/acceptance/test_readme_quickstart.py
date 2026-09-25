"""Acceptance: the README quick start, run as written, against the fake API.

``README.md`` includes ``examples/readme/app.py`` and
``examples/readme/commands.sh`` verbatim (``scripts/readme_examples.py check``
keeps them identical; ``tests/unit/test_readme_examples.py`` runs it). This
test copies ``app.py`` to a scratch directory and runs every ``piceli``
command of ``commands.sh`` in order, through the real CLI. Only what the
reader provides is replaced:

* the ``kind`` cluster and its ``hello`` namespace: ``hello.kubeconfig``
  names the in-process fake API (serving namespace ``hello``) under the
  README's context ``kind-hello``, and the target gets
  ``transport="loopback-http"`` (the fake API speaks plain HTTP on loopback);
* ``kubectl``: a fake on ``PATH`` whose ``port-forward`` serves ``200 OK``
  where nginx would, so the README's HTTP check really runs;
* the kubelet: two ready Pods are added once the Deployment exists, so
  ``piceli status`` has something to report.

``piceli access`` runs until Ctrl-C, so it is the one command not run here;
``tests/unit/access`` covers it with the same kind of fake ``kubectl``.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.testing import FakeAPI, serve, write_kubeconfig

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "readme"

FAKE_KUBECTL = textwrap.dedent(
    """\
    import re, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if "port-forward" not in sys.argv:
        sys.exit(1)
    ports = next(a for a in sys.argv[1:] if re.fullmatch(r"\\d+:\\d+", a))
    local = int(ports.split(":")[0])


    class Nginx(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<h1>Welcome to nginx!</h1>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass


    ThreadingHTTPServer(("127.0.0.1", local), Nginx).serve_forever()
    """
)


def _commands() -> list[list[str]]:
    """The ``piceli`` argv of every line of ``commands.sh`` (comments dropped)."""
    commands = []
    for line in (EXAMPLE / "commands.sh").read_text().splitlines():
        argv = shlex.split(line, comments=True)
        if argv:
            assert argv[0] == "piceli", line
            commands.append(argv[1:])
    return commands


def _pod(name: str, namespace: str, image: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": "web"},
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "web", "image": image, "ready": True, "restartCount": 0}
            ],
        },
    }


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


@pytest.mark.timeout(120)
def test_readme_quick_start_runs_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (EXAMPLE / "app.py").read_text()
    anchor = '    namespace="hello",\n)'
    assert source.count(anchor) == 1
    (tmp_path / "app.py").write_text(
        source.replace(
            anchor, '    namespace="hello",\n    transport="loopback-http",\n)'
        )
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert shutil.which("kubectl") == str(kubectl)
    monkeypatch.chdir(tmp_path)

    commands = _commands()
    assert [argv[:2] for argv in commands] == [
        ["render", "app.py:app"],
        ["deploy", "app.py:pipeline"],
        ["deploy", "app.py:pipeline"],
        ["status", "app.py:pipeline"],
        ["access", "app.py:pipeline"],
    ]
    render, plan, approve, status, _access = commands
    runner = CliRunner()
    with serve(FakeAPI(namespace="hello")) as (api, url):
        write_kubeconfig(url, tmp_path / "hello.kubeconfig", context="kind-hello")

        result = runner.invoke(cli, render)
        assert result.exit_code == 0, result.output
        assert "kind: Deployment" in result.stdout and "kind: Service" in result.stdout
        assert "namespace: hello" in result.stdout

        result = runner.invoke(cli, [*plan, "--json"])
        assert result.exit_code == 0, result.output
        planned = _json_lines(result.stdout)[-1]
        assert planned["state"] == "planned", planned
        creates = {
            f"{c['kind']}/{c['name']}"
            for c in planned["stages"]["plan"]["changes"]
            if c["operation"] == "create"
        }
        assert creates == {"Deployment/web", "Service/web"}

        index = approve.index("<combined-hash>")
        approve[index] = planned["combined_hash"]
        result = runner.invoke(cli, [*approve, "--json"])
        assert result.exit_code == 0, result.output
        done = _json_lines(result.stdout)[-1]
        assert done["state"] == "ready", done
        assert done["stages"]["checks"] == "done"
        checks = [
            e
            for e in _json_lines(result.stdout)
            if e.get("stage") == "checks" and e.get("state") == "done"
        ]
        assert checks[-1]["detail"]["results"][0]["passed"] is True

        deployment = api.objects[("Deployment", "web")]
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        assert image.startswith("docker.io/library/nginx:1.27@sha256:")
        for index in (1, 2):
            api.objects[("Pod", f"web-{index}")] = _pod(f"web-{index}", "hello", image)
        result = runner.invoke(cli, [*status, "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.stdout)
        assert report["state"] == "up", report
        assert [w["health"] for w in report["workloads"]] == ["ready"]

        # "Run `piceli deploy` again without changes and the plan reports no
        # changes, so nothing is applied."
        result = runner.invoke(cli, [*plan, "--json"])
        assert result.exit_code == 0, result.output
        again = _json_lines(result.stdout)[-1]
        assert again["stages"]["plan"]["changes"] == [] or {
            c["operation"] for c in again["stages"]["plan"]["changes"]
        } == {"no-op"}, again["stages"]["plan"]
