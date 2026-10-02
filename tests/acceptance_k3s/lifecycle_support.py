"""Plumbing of the k3s lifecycle acceptance: processes, the cluster, Git, a webhook.

Nothing here runs at import. Everything it creates lives under one scratch
directory or in the disposable k3d cluster, both removed by the caller.

- :class:`Proc`: runs commands with an explicit environment, never echoing
  secrets (stdin values are never logged).
- :class:`K3d`: a 3-node k3d (k3s) cluster with a scratch kubeconfig; the
  default kubeconfig is never touched.
- :class:`GitServer`: HTTPS smart Git (``git http-backend``) with basic auth,
  serving bare repositories of the scratch directory to the cluster's pods at
  ``https://host.k3d.internal:<port>/<name>.git``.
- :class:`GitTlsWebhook`: a mutating admission webhook (served from this
  process) that lets Git in ``piceli-system`` pods accept the test server's
  throwaway certificate (``GIT_SSL_NO_VERIFY``). Test plumbing only: the
  controller and build Jobs are otherwise unchanged.
- :class:`Commands`: ``lifecycle_commands.toml``, the commands a note gives.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import json
import os
import shlex
import shutil
import ssl
import subprocess
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST_ALIAS = "host.k3d.internal"


class StageFailed(AssertionError):
    """A stage's assertion failed (the message is safe to print)."""


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def check(condition: object, message: str) -> None:
    if not condition:
        raise StageFailed(message)


# ----------------------------------------------------------------- processes
@dataclass
class Result:
    argv: list[str]
    code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        text = self.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            # JSON lines or text before the object: take the last JSON line.
            for line in reversed(text.splitlines()):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None


@dataclass
class Proc:
    """Run commands with a fixed environment and working directory."""

    env: dict[str, str]
    cwd: Path
    transcript: list[str] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        timeout: float = 900,
        check_exit: Sequence[int] | None = (0,),
        cwd: Path | None = None,
        quiet: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        argv = [str(part) for part in argv]
        shown = shlex.join(argv)
        if not quiet:
            log(f"$ {shown}" + (" (stdin: <secret>)" if stdin is not None else ""))
        self.transcript.append(shown)
        try:
            done = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd or self.cwd),
                env={**self.env, **(env or {})},
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise StageFailed(f"timed out after {timeout:.0f}s: {shown}") from error
        result = Result(argv, done.returncode, done.stdout, done.stderr)
        if check_exit is not None and done.returncode not in check_exit:
            raise StageFailed(
                f"exit {done.returncode} (expected {list(check_exit)}): {shown}\n"
                f"--- stdout (tail)\n{done.stdout[-3000:]}\n"
                f"--- stderr (tail)\n{done.stderr[-3000:]}"
            )
        return result


def wait_for(
    what: str,
    probe: Callable[[], Any],
    *,
    timeout: float,
    interval: float = 5,
) -> Any:
    """Poll ``probe`` until it returns a truthy value; StageFailed on timeout."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            last = probe()
        except StageFailed:
            last = None
        if last:
            return last
        if time.monotonic() > deadline:
            raise StageFailed(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


# ------------------------------------------------------------------- the cluster
@dataclass
class K3d:
    """A disposable k3d cluster: one server, two agents."""

    name: str
    proc: Proc
    k3d: list[str]
    kubeconfig: Path
    kubectl_bin: str

    @property
    def context(self) -> str:
        return f"k3d-{self.name}"

    @property
    def server(self) -> str:
        return f"k3d-{self.name}-server-0"

    @property
    def agents(self) -> tuple[str, str]:
        return f"k3d-{self.name}-agent-0", f"k3d-{self.name}-agent-1"

    def create(self) -> None:
        self.delete()
        self.proc.run(
            [
                *self.k3d, "cluster", "create", self.name, "--agents", "2",
                "--kubeconfig-update-default=false",
                "--kubeconfig-switch-context=false",
                "--k3s-arg", "--disable=traefik@server:0",
                # A laptop's Docker VM is often fuller than a node's disk:
                # keep kubelet from evicting or collecting images mid-run.
                "--k3s-arg", "--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%@all",
                "--k3s-arg", "--kubelet-arg=image-gc-high-threshold=99@all",
                "--k3s-arg", "--kubelet-arg=image-gc-low-threshold=98@all",
                "--wait", "--timeout", "600s",
            ],
            timeout=900,
        )  # fmt: skip
        text = self.proc.run(
            [*self.k3d, "kubeconfig", "get", self.name], quiet=True
        ).stdout
        self.kubeconfig.write_text(text)
        self.kubeconfig.chmod(0o600)

    def delete(self) -> None:
        self.proc.run(
            [*self.k3d, "cluster", "delete", self.name],
            timeout=600,
            check_exit=None,
            quiet=True,
        )

    def kubectl(
        self,
        *args: str,
        stdin: str | None = None,
        check_exit: Sequence[int] | None = (0,),
        timeout: float = 300,
        quiet: bool = True,
    ) -> Result:
        return self.proc.run(
            [
                self.kubectl_bin, "--kubeconfig", str(self.kubeconfig),
                "--context", self.context, *args,
            ],
            stdin=stdin, check_exit=check_exit, timeout=timeout, quiet=quiet,
        )  # fmt: skip

    def get(self, *args: str) -> Any:
        found = self.kubectl("get", *args, "-o", "json", check_exit=None)
        if found.code != 0:
            return None
        return json.loads(found.stdout)

    def apply(self, objects: Sequence[Mapping[str, Any]]) -> None:
        body = {"apiVersion": "v1", "kind": "List", "items": list(objects)}
        self.kubectl("apply", "-f", "-", stdin=json.dumps(body))

    def api_server(self) -> str:
        config = json.loads(
            self.kubectl("config", "view", "--raw", "-o", "json").stdout
        )
        return str(config["clusters"][0]["cluster"]["server"])


# ---------------------------------------------------------------- certificates
def make_tls(
    directory: Path, name: str, hosts: Sequence[str]
) -> tuple[Path, Path, bytes]:
    """A throwaway CA and a server certificate for ``hosts``: (cert, key, ca_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    names: list[x509.GeneralName] = []
    for host in hosts:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{name}.crt", directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path, ca.public_bytes(serialization.Encoding.PEM)


class _Server:
    """A threaded HTTPS server on 127.0.0.1 (reached as host.k3d.internal)."""

    handler: type[BaseHTTPRequestHandler]

    def __init__(self, cert: Path, key: Path) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.httpd.owner = self  # type: ignore[attr-defined]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ------------------------------------------------------------------- Git server
class _GitHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:  # no request logs (no secrets)
        return

    def _authorized(self) -> bool:
        owner: GitServer = self.server.owner  # type: ignore[attr-defined]
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except ValueError:
            return False
        ok = user == owner.username and password == owner.password
        owner.requests.append((self.command, self.path.split("?")[0], ok))
        return ok

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _handle(self) -> None:
        body = self._body() if self.command == "POST" else b""
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        owner: GitServer = self.server.owner  # type: ignore[attr-defined]
        path, _, query = self.path.partition("?")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(owner.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": owner.username,
            "REMOTE_ADDR": self.client_address[0],
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(owner.root),
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        done = subprocess.run(
            [owner.backend], input=body, capture_output=True, env=env, timeout=300
        )
        head, _, payload = done.stdout.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = done.stdout.partition(b"\n\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


class GitServer(_Server):
    """Smart HTTPS Git with basic auth over the bare repositories in ``root``."""

    handler = _GitHandler

    def __init__(
        self, root: Path, cert: Path, key: Path, username: str, password: str
    ) -> None:
        super().__init__(cert, key)
        self.root = root
        self.username = username
        self.password = password
        self.requests: list[tuple[str, str, bool]] = []
        exec_path = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.backend = str(Path(exec_path) / "git-http-backend")

    def url(self, name: str) -> str:
        return f"https://{HOST_ALIAS}:{self.port}/{name}.git"


# ------------------------------------------------------------ admission webhook
class _WebhookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: object) -> None:
        return

    def do_POST(self) -> None:
        owner: GitTlsWebhook = self.server.owner  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        review = json.loads(self.rfile.read(length) or b"{}")
        request = review.get("request") or {}
        pod = request.get("object") or {}
        patch = []
        spec = pod.get("spec") or {}
        for kind in ("initContainers", "containers"):
            for index, container in enumerate(spec.get(kind) or []):
                entry = {"name": "GIT_SSL_NO_VERIFY", "value": "true"}
                if container.get("env"):
                    patch.append(
                        {
                            "op": "add",
                            "path": f"/spec/{kind}/{index}/env/-",
                            "value": entry,
                        }
                    )
                else:
                    patch.append(
                        {
                            "op": "add",
                            "path": f"/spec/{kind}/{index}/env",
                            "value": [entry],
                        }
                    )
        owner.mutated += 1
        response = {
            "apiVersion": "admission.k8s.io/v1",
            "kind": "AdmissionReview",
            "response": {
                "uid": request.get("uid"),
                "allowed": True,
                "patchType": "JSONPatch",
                "patch": base64.b64encode(json.dumps(patch).encode()).decode(),
            },
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GitTlsWebhook(_Server):
    """Adds ``GIT_SSL_NO_VERIFY=true`` to pods created in ``piceli-system``."""

    handler = _WebhookHandler
    NAME = "lifecycle-test-git-tls"

    def __init__(self, cert: Path, key: Path, ca_pem: bytes, host: str) -> None:
        super().__init__(cert, key)
        self.ca_pem = ca_pem
        self.host = host
        self.mutated = 0

    def configuration(self) -> dict[str, Any]:
        return {
            "apiVersion": "admissionregistration.k8s.io/v1",
            "kind": "MutatingWebhookConfiguration",
            "metadata": {"name": self.NAME},
            "webhooks": [
                {
                    "name": "git-tls.lifecycle.test",
                    "clientConfig": {
                        "url": f"https://{self.host}:{self.port}/mutate",
                        "caBundle": base64.b64encode(self.ca_pem).decode(),
                    },
                    "rules": [
                        {
                            "operations": ["CREATE"],
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "resources": ["pods"],
                        }
                    ],
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "piceli-system"}
                    },
                    "failurePolicy": "Ignore",
                    "sideEffects": "None",
                    "admissionReviewVersions": ["v1"],
                    "timeoutSeconds": 5,
                }
            ],
        }


# ------------------------------------------------------------------ Git repos
@dataclass
class Repos:
    """Bare remotes in ``remotes/`` and working clones in ``work/``."""

    root: Path
    proc: Proc

    @property
    def remotes(self) -> Path:
        return self.root / "remotes"

    def work(self, name: str) -> Path:
        return self.root / "work" / name

    def _git(self, name: str, *args: str) -> str:
        return self.proc.run(
            ["git", "-C", str(self.work(name)), *args], quiet=True
        ).stdout

    def create(self, name: str, source: Path) -> str:
        bare = self.remotes / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        self.proc.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(bare)], quiet=True
        )
        work = self.work(name)
        shutil.copytree(source, work, ignore=shutil.ignore_patterns("__pycache__"))
        self.proc.run(["git", "init", "-q", "-b", "main", str(work)], quiet=True)
        self._git(name, "remote", "add", "origin", str(bare))
        return self.commit(name, "initial", push=True)

    def commit(
        self,
        name: str,
        message: str,
        files: Mapping[str, str] | None = None,
        *,
        branch: str = "main",
        push: bool = True,
    ) -> str:
        current = self._git(name, "symbolic-ref", "--short", "HEAD").strip()
        if current != branch:
            exists = self.proc.run(
                ["git", "-C", str(self.work(name)), "rev-parse", "--verify", "-q", branch],
                check_exit=None, quiet=True,
            ).code == 0  # fmt: skip
            self._git(name, "checkout", "-q", *([] if exists else ["-b"]), branch)
        for path, text in (files or {}).items():
            target = self.work(name) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self._git(name, "add", "-A")
        self._git(
            name, "-c", "user.name=Lifecycle", "-c", "user.email=lifecycle@example.invalid",
            "commit", "-q", "--allow-empty", "-m", message,
        )  # fmt: skip
        sha = self._git(name, "rev-parse", "HEAD").strip()
        if push:
            self._git(name, "push", "-q", "origin", f"{branch}:{branch}")
        if branch != "main":
            self._git(name, "checkout", "-q", "main")
        return sha

    def delete_branch(self, name: str, branch: str) -> None:
        self._git(name, "push", "-q", "origin", "--delete", branch)

    def head(self, name: str, branch: str = "main") -> str:
        return self._git(name, "rev-parse", branch).strip()

    def read(self, name: str, path: str) -> str:
        return (self.work(name) / path).read_text()


# ------------------------------------------------------------------- commands
@dataclass
class Commands:
    """``lifecycle_commands.toml``: steps, groups and the note's placeholders."""

    steps: dict[str, dict[str, Any]]
    groups: dict[str, list[str]]
    note: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> Commands:
        data = tomllib.loads(path.read_text())
        return cls(dict(data["steps"]), dict(data["groups"]), dict(data["note"]))

    def line(self, step: str, values: Mapping[str, str]) -> str:
        text = " ".join(self.steps[step]["run"].replace("\\\n", " ").split())
        return text.format_map(dict(values))

    def render_note(self) -> str:
        out = []
        for group, steps in self.groups.items():
            out.append(f"# {group}")
            for step in steps:
                if self.steps[step].get("note", True):
                    out.append(self.line(step, self.note))
            out.append("")
        return "\n".join(out)

    def run_group(
        self,
        group: str,
        proc: Proc,
        values: dict[str, str],
        secrets: Mapping[str, str],
        *,
        cwd: Path,
        results: dict[str, Result] | None = None,
    ) -> dict[str, Result]:
        """Run each step of ``group`` as written; captured keys update ``values``."""
        done: dict[str, Result] = {} if results is None else results
        for step in self.groups[group]:
            done[step] = self.run_step(step, proc, values, secrets, cwd=cwd)
        return done

    def run_step(
        self,
        step: str,
        proc: Proc,
        values: dict[str, str],
        secrets: Mapping[str, str],
        *,
        cwd: Path,
    ) -> Result:
        spec = self.steps[step]
        for name in spec.get("reset", []):
            values.pop(name, None)
        when = spec.get("when")
        if when and not values.get(when):
            log(f"skip {step}: no {when}")
            return Result([], 0, "", "")
        argv = shlex.split(self.line(step, values))
        stdin = secrets[spec["stdin"]] if spec.get("stdin") else None
        result = proc.run(
            argv, stdin=stdin, check_exit=list(spec.get("exit", [0])), cwd=cwd
        )
        if result.code == 3:
            body = result.json() or {}
            for name, key in (spec.get("capture") or {}).items():
                if isinstance(body, dict) and body.get(key):
                    values[name] = str(body[key])
        return result
