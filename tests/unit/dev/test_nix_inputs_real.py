"""A private flake input fetched on the build host with the real ``nix`` (0.26.0).

Opt-in: ``PICELI_TEST_NIX=1`` on a machine with a multi-user Nix and Git
(``git http-backend``). A local smart-HTTP Git server with basic auth (a
thread of this test) serves the private repository ``app``; a flake pins it
as an input (``git+http://127.0.0.1:PORT/app.git``). On a native build
host:

- without the input fetched first, the build run fails (it has no
  credential);
- ``--input app=app-token`` with no such credential refuses before any
  build (``infra-nix-input-unreachable``, naming the input and its URL);
- with the token stored (``piceli secrets git-token``), the input is fetched
  into the store first and the build passes and publishes; the token is in
  no output, no log, and no file of the published closure or the fetched
  tree.

Everything lives in ``tmp_path``; the store paths it created are deleted at
the end when Nix allows it.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix import NixCache
from piceli.dev.nix_publish import NIX_PATHS, path_info
from tests.unit.dev.test_nix_real import IMAGE, NIX, _nix
from tests.unit.dev.test_pack import commit, git, repo

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_TEST_NIX") != "1", reason="set PICELI_TEST_NIX=1"
    ),
    pytest.mark.skipif(NIX is None, reason="no nix on this machine"),
    pytest.mark.skipif(shutil.which("git") is None, reason="no git"),
    pytest.mark.timeout(600),
]

USER = "builder"
FLAKE = """{
  inputs.app.url = "git+http://127.0.0.1:PORT/app.git";
  outputs = { self, app }: {
    packages.SYSTEM.hello = derivation {
      name = "piceli-private-input-test";
      system = "SYSTEM";
      builder = "/bin/sh";
      args = [ "-c" "echo ${app.message} > $out" ];
    };
  };
}
"""


@pytest.fixture
def git_server(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """A smart-HTTP Git server over ``tmp_path/served`` that wants basic auth."""
    root = tmp_path / "served"
    root.mkdir()
    token = "tok-" + secrets.token_hex(16)
    wanted = "Basic " + base64.b64encode(f"{USER}:{token}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def handle_one(self) -> None:
            if self.headers.get("Authorization") != wanted:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            env = {
                **os.environ, "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path,
                "QUERY_STRING": query, "REQUEST_METHOD": self.command,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length), "REMOTE_USER": USER,
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            }  # fmt: skip
            out = subprocess.run(
                ["git", "http-backend"], input=body, env=env, capture_output=True
            ).stdout
            head, _, rest = out.partition(b"\r\n\r\n")
            status, headers = 200, []
            for line in head.decode().split("\r\n"):
                key, _, value = line.partition(":")
                if key.lower() == "status":
                    status = int(value.split()[0])
                elif key:
                    headers.append((key, value.strip()))
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(rest)))
            self.end_headers()
            self.wfile.write(rest)

        do_GET = do_POST = handle_one

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"root": root, "port": server.server_address[1], "token": token}
    finally:
        server.shutdown()
        server.server_close()


def _askpass_env(tmp_path: Path, token: str) -> dict[str, str]:
    """An environment whose Git answers with the token (only to lock the flake)."""
    script = tmp_path / "askpass.sh"
    script.write_text(
        f'#!/bin/sh\ncase "$1" in Username*) echo {USER};; *) echo {token};; esac\n'
    )
    script.chmod(0o700)
    return {**os.environ, "GIT_ASKPASS": str(script), "GIT_TERMINAL_PROMPT": "0",
            "XDG_CACHE_HOME": str(tmp_path / "lock-cache"),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": ""}  # fmt: skip


def _files(path: str) -> Iterator[bytes]:
    found = Path(path)
    if found.is_file():
        yield found.read_bytes()
    for item in found.rglob("*") if found.is_dir() else ():
        if item.is_file() and not item.is_symlink():
            yield item.read_bytes()


def test_a_private_input_is_fetched_on_the_host_and_never_reaches_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: dict[str, Any]
) -> None:
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.infra import Cluster, Node
    from piceli.k8s.cli import app

    token, port = git_server["token"], git_server["port"]
    system = _nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    system_name = system.stdout.strip()
    # The private repository: a fresh message, so no cache anywhere holds it.
    message = "private-" + secrets.token_hex(8)
    private = repo(tmp_path / "app-src", {"flake.nix":
                   f'{{ outputs = _: {{ message = "{message}"; }}; }}\n'})  # fmt: skip
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(private),
         str(git_server["root"] / "app.git")], check=True,
    )  # fmt: skip
    flake = FLAKE.replace("PORT", str(port)).replace("SYSTEM", system_name)
    source = repo(tmp_path / "hosts", {"flake.nix": flake})
    locked = subprocess.run(
        [str(NIX), "--extra-experimental-features", "nix-command flakes",
         "flake", "lock"], cwd=source, env=_askpass_env(tmp_path, token),
        capture_output=True, text=True,
    )  # fmt: skip
    assert locked.returncode == 0, locked.stderr
    commit(source, {"flake.lock": (source / "flake.lock").read_text()})
    lock = json.loads((source / "flake.lock").read_text())
    # Where the input's tree lands in the store (fetched with the token).
    from piceli.dev.nix_fetch import eval_argv

    shown = subprocess.run(
        eval_argv(str(NIX), lock["nodes"]["app"]["locked"]),
        env=_askpass_env(tmp_path, token), capture_output=True, text=True,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    tree = shown.stdout.strip()
    _nix("store", "delete", tree, check=False)  # the build must fetch it again
    assert not Path(tree).exists()

    monkeypatch.chdir(source)
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", os.pathsep.join([os.environ["PATH"], *NIX_PATHS]))
    cache_dir = tmp_path / "binary-cache"
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("builder-1", arch="amd64", roles=["builder"])],
        dev=DevBuilds(node=None, image=IMAGE, profiles=[DevProfile.for_nix()],
                      hosts=[host]),
    )  # fmt: skip
    runner = CliRunner()
    made = runner.invoke(app, ["secrets", "nix-cache", "cache-test", "--generate"])
    public = json.loads(made.stdout)["public_key"]
    monkeypatch.setattr(dev_nix, "load_dev", lambda _ref: cluster.dev)
    monkeypatch.setattr(
        dev_nix, "load_cache", lambda _ref: NixCache(public, url=f"file://{cache_dir}")
    )
    attr = f"packages.{system_name}.hello"
    build = ["dev", "nix", "build", attr, "--cluster", "infra.py:c", "--json"]
    store_path = None
    outputs: list[str] = []
    try:
        # 1. Without the input fetched first, the build run cannot fetch it.
        bare = runner.invoke(app, build)
        outputs += [bare.stdout, bare.stderr]
        assert bare.exit_code == 1, bare.stdout + bare.stderr
        assert not Path(tree).exists()

        # 2. Declared, without the credential: refused before any build.
        missing = runner.invoke(app, [*build, "--input", "app=app-token"])
        outputs += [missing.stdout, missing.stderr]
        assert missing.exit_code == 2
        refusal = json.loads(missing.stdout)
        assert refusal["reason"] == "infra-nix-input-unreachable"
        assert f"app git+http://127.0.0.1:{port}/app.git (credential app-token is " in (
            missing.stderr + missing.stdout
        )

        # 3. With the token stored on this host: fetched first, built, published.
        stored = runner.invoke(
            app, ["secrets", "git-token", "app-token", "--host", "127.0.0.1",
                  "--username", USER, "--prompt"], input=token + "\n",
        )  # fmt: skip
        assert stored.exit_code == 0, stored.stdout + stored.stderr
        publish = [*build, "--ref", "HEAD", "--name", "hello", "--publish",
                   "infra.py:cache", "--input", "app=app-token"]  # fmt: skip
        planned = runner.invoke(app, publish)
        assert planned.exit_code == 3, planned.stdout + planned.stderr
        plan = json.loads(planned.stdout)
        assert plan["plan"]["inputs"][0]["credential"] == "app-token"
        done = runner.invoke(app, [*publish, "--approve", plan["digest"]])
        outputs += [stored.stdout, stored.stderr, planned.stdout, planned.stderr,
                    done.stdout, done.stderr]  # fmt: skip
        assert done.exit_code == 0, done.stdout + done.stderr
        body = json.loads(done.stdout)
        store_path = body["store_path"]
        assert Path(store_path).read_text() == message + "\n"
        assert Path(tree).is_dir()

        # 4. The token is nowhere: outputs, logs, the closure, the fetched tree.
        closure = _nix("path-info", "--json", "-r", store_path).stdout
        assert token not in closure
        for text in outputs:
            assert token not in text
        paths = json.loads(closure)
        names = list(paths) if isinstance(paths, dict) else [p["path"] for p in paths]
        for found in [*names, tree]:
            for data in _files(found):
                assert token.encode() not in data
        for data in _files(str(cache_dir)):
            assert token.encode() not in data
        manifest = (
            cache_dir / "piceli" / "hello" / f"{body['commit']}.json"
        ).read_text()
        assert token not in manifest
        assert not list((tmp_path / "xdg-cache").rglob("app*"))  # no clone kept
        assert git(source, "status", "--porcelain") == ""
    finally:
        for link in (tmp_path / "state").rglob(nix.OUT_LINK):
            link.unlink()
        if store_path:
            shown = _nix("path-info", "--json", store_path, check=False).stdout
            deriver = path_info(shown, store_path).get("deriver")
            _nix("store", "delete", store_path, check=False)
            if deriver:
                _nix("store", "delete", deriver, check=False)
        _nix("store", "delete", tree, check=False)
