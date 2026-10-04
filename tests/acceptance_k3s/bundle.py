"""Opt-in: deliver ``examples/client_bundle`` to a clean cluster we treat as a client's.

Skipped unless ``PICELI_K3S_BUNDLE=1``; needs ``k3d``, ``skopeo``, ``docker``,
``kubectl``, ``openssl`` and ``uv`` on ``PATH`` (``make acceptance-bundle``
takes k3d and skopeo from nix) and network access to Docker Hub (the
example's BusyBox base and ``registry:2``)::

    PICELI_K3S_BUNDLE=1 make acceptance-bundle

Stages, in order (the cluster and the registry are removed on every outcome,
the scratch directory too; ``~/.kube/config`` is never read or written):

1. A clean one-node k3d cluster and a private ``registry:2`` container that
   the nodes reach through a containerd mirror of ``127.0.0.1:<port>``.
2. ``piceli artifacts build-spec run`` builds the example's image for amd64
   and arm64 (host builder), then ``piceli bundle --all-platforms``.
3. INSTALL.md, **as written**: every ``sh`` block but the port-forward, in
   one shell, with ``NAMESPACE``, ``REGISTRY`` and ``SKOPEO_FLAGS`` set as
   the guide asks (a fresh namespace other than the rendered one).
4. The pods are ready; the page served through INSTALL.md's own
   port-forward shows the cluster's ``kube-system`` UID and the generated
   Secrets; a probe pod in the namespace reaches the Service and one in
   another namespace cannot (the default-deny NetworkPolicy holds).
5. ``piceli support-bundle``: GET only, and no Secret value in any file.
6. UNINSTALL.md, as written; then nothing labelled
   ``app.kubernetes.io/part-of=shop`` is left, namespaced or cluster-scoped.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.acceptance_k3s.lifecycle_support import (
    Proc,
    StageFailed,
    check,
    log,
    wait_for,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "client_bundle"
CLUSTER = "piceli-bundle"
REGISTRY_CONTAINER = "piceli-bundle-registry"
REGISTRY_IMAGE = "registry:2"
NAMESPACE = "shop-acceptance"
PART_OF = "app.kubernetes.io/part-of=shop"
_BLOCK = re.compile(r"^## (\d+)\. (.+?)\n.*?```sh\n(.*?)```", re.S | re.M)


def blocks(path: Path) -> list[tuple[str, str]]:
    """``(heading, commands)`` of every numbered ``sh`` block, in order."""
    return [
        (match.group(2), match.group(3)) for match in _BLOCK.finditer(path.read_text())
    ]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Acceptance:
    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.kubeconfig = scratch / "kubeconfig"
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"KUBECONFIG", "PICELI_K3S_BUNDLE"}
        }
        env.update(
            KUBECONFIG=str(self.kubeconfig),
            KUBECACHEDIR=str(scratch / "kube-cache"),
            TMPDIR=str(scratch / "tmp"),
        )
        (scratch / "tmp").mkdir()
        self.proc = Proc(env, scratch)
        self.k3d = [shutil.which("k3d") or "k3d"]
        self.port = 0

    # ------------------------------------------------------------ helpers

    def kubectl(self, *args: str, check_exit=(0,), stdin: str | None = None):
        return self.proc.run(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", f"k3d-{CLUSTER}", *args],
            check_exit=check_exit, quiet=True, stdin=stdin, timeout=600,
        )  # fmt: skip

    def piceli(self, *args: str, check_exit=(0,)):
        return self.proc.run(
            ["uv", "run", "--frozen", "--project", str(ROOT), "piceli", *args],
            check_exit=check_exit, timeout=1800, cwd=ROOT,
        )  # fmt: skip

    def cleanup(self) -> None:
        log("cleanup: cluster, registry")
        self.proc.run(
            ["docker", "rm", "-f", REGISTRY_CONTAINER], check_exit=None, quiet=True
        )
        self.proc.run(
            [*self.k3d, "cluster", "delete", CLUSTER],
            check_exit=None,
            quiet=True,
            timeout=600,
        )

    # ------------------------------------------------------------- stages

    def cluster(self) -> None:
        self.cleanup()
        self.proc.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                REGISTRY_CONTAINER,
                "-p",
                "127.0.0.1::5000",
                REGISTRY_IMAGE,
            ]
        )
        published = self.proc.run(
            ["docker", "port", REGISTRY_CONTAINER, "5000/tcp"], quiet=True
        ).stdout
        self.port = int(published.strip().splitlines()[0].rsplit(":", 1)[1])
        config = self.scratch / "registries.yaml"
        config.write_text(
            "mirrors:\n"
            f'  "127.0.0.1:{self.port}":\n'
            f'    endpoint: ["http://{REGISTRY_CONTAINER}:5000"]\n'
        )
        self.proc.run(
            [
                *self.k3d, "cluster", "create", CLUSTER,
                "--kubeconfig-update-default=false", "--kubeconfig-switch-context=false",
                "--k3s-arg", "--disable=traefik@server:0",
                "--registry-config", str(config), "--wait", "--timeout", "600s",
            ],
            timeout=900,
        )  # fmt: skip
        self.proc.run(
            ["docker", "network", "connect", f"k3d-{CLUSTER}", REGISTRY_CONTAINER]
        )
        text = self.proc.run(
            [*self.k3d, "kubeconfig", "get", CLUSTER], quiet=True
        ).stdout
        self.kubeconfig.write_text(text)
        self.kubeconfig.chmod(0o600)
        wait_for("a ready node", lambda: "True" in self.kubectl(
            "get", "nodes", "-o", "jsonpath={.items[*].status.conditions[?(@.type=='Ready')].status}"
        ).stdout, timeout=300)  # fmt: skip

    def bundle(self) -> Path:
        cache = self.scratch / "build-cache"
        receipt = self.scratch / "build" / "receipt.json"
        receipt.parent.mkdir()
        spec = str(EXAMPLE / "host-build.toml")
        preview = self.piceli(
            "artifacts",
            "build-spec",
            "preview",
            "--spec",
            spec,
            "--cache-dir",
            str(cache),
        ).json()
        self.piceli(
            "artifacts", "build-spec", "run", "--spec", spec, "--cache-dir", str(cache),
            "--approve-plan", preview["plan_hash"], "--out", str(receipt), "--progress", "quiet",
        )  # fmt: skip
        out = self.scratch / "shop-bundle"
        written = self.piceli(
            "bundle", f"{EXAMPLE / 'app.py'}:pipeline", "--env", "client",
            "--receipt", str(receipt), "--all-platforms", "--out", str(out),
        ).json()  # fmt: skip
        check(written["state"] == "written", "the bundle was not written")
        check(
            not any(item.endswith("Secret") for item in written["files"]),
            "a Secret file",
        )
        return out

    def install(self, bundle: Path) -> subprocess.Popen[str]:
        steps = blocks(bundle / "INSTALL.md")
        forward = [
            body
            for heading, body in steps
            if body.lstrip().startswith('kubectl -n "$NAMESPACE" port-forward')
        ]
        commands = [body for heading, body in steps if body not in forward]
        check(len(forward) == 1, "INSTALL.md has no port-forward block")
        log(f"INSTALL.md: {len(commands)} block(s), run as written")
        # A client's machine: skopeo reads the containers policy that its
        # package installs (/etc/containers/policy.json); a nix-installed
        # skopeo has none, so the client's HOME here holds the default one.
        home = self.scratch / "client-home"
        policy = home / ".config" / "containers" / "policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text('{"default": [{"type": "insecureAcceptAnything"}]}\n')
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "NAMESPACE": NAMESPACE,
            "REGISTRY": f"127.0.0.1:{self.port}/shop",
            "SKOPEO_FLAGS": "--dest-tls-verify=false",
        }
        self.proc.run(
            ["sh", "-ec", "\n".join(commands)], cwd=bundle, env=env, timeout=1200
        )
        log("INSTALL.md: port-forward, as written")
        return subprocess.Popen(
            ["sh", "-ec", forward[0]],
            cwd=bundle,
            env={**self.proc.env, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )

    def verify(self, bundle: Path, forward: subprocess.Popen[str]) -> None:
        pods = json.loads(
            self.kubectl("-n", NAMESPACE, "get", "pods", "-o", "json").stdout
        )["items"]
        check(pods and all(
            condition["status"] == "True"
            for pod in pods for condition in pod["status"].get("conditions", [])
            if condition["type"] == "Ready"
        ), "a pod is not ready")  # fmt: skip
        uid = self.kubectl(
            "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"
        ).stdout.strip()
        port = int(
            re.search(
                r"(\d+):\d+",
                (bundle / "INSTALL.md").read_text().split("port-forward", 1)[1],
            ).group(1)
        )  # type: ignore[union-attr]

        def page() -> str | None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5
                ) as response:
                    return response.read().decode()
            except OSError:
                return None

        text = wait_for(
            "the page through INSTALL.md's port-forward", page, timeout=90, interval=2
        )
        check(
            f"cluster {uid}" in text,
            "the page does not show the cluster's kube-system UID",
        )
        check(
            "token present" in text and "certificate present" in text, "Secrets missing"
        )
        log("identity: the page shows the kube-system UID; Secrets mounted")
        os.killpg(forward.pid, 15)
        # The default-deny NetworkPolicy: same namespace reaches, another does not.
        image = json.loads(self.kubectl("-n", NAMESPACE, "get", "deployment", "web", "-o", "json").stdout)[
            "spec"]["template"]["spec"]["containers"][0]["image"]  # fmt: skip
        url = f"http://web.{NAMESPACE}.svc:8080/"
        self.kubectl("create", "namespace", "probe")
        inside = self._probe(NAMESPACE, image, url)
        outside = self._probe("probe", image, url)
        check(inside == 0, "a pod in the namespace cannot reach the Service")
        check(outside != 0, "a pod in another namespace reached the Service")
        log("network: same namespace reaches web; another namespace is refused")

    def _probe(self, namespace: str, image: str, url: str) -> int:
        name = f"probe-{namespace}"
        overrides = {
            "spec": {
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
                "containers": [{
                    "name": "probe", "image": image, "command": ["wget", "-q", "-T", "5", "-O", "/dev/null", url],
                    "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False},
                    "resources": {"limits": {"cpu": "50m", "memory": "16Mi"}, "requests": {"cpu": "5m", "memory": "8Mi"}},
                }],
            }
        }  # fmt: skip
        self.kubectl(
            "-n", namespace, "run", name, "--image", image, "--restart=Never",
            "--overrides", json.dumps(overrides),
        )  # fmt: skip

        def done() -> int | None:
            pod = json.loads(
                self.kubectl("-n", namespace, "get", "pod", name, "-o", "json").stdout
            )
            terminated = (
                (pod["status"].get("containerStatuses") or [{}])[0]
                .get("state", {})
                .get("terminated")
            )
            return None if terminated is None else int(terminated["exitCode"]) + 1000

        return wait_for(f"probe in {namespace}", done, timeout=120, interval=2) - 1000

    def support(self) -> None:
        out = self.scratch / "support.tar.gz"
        result = self.piceli(
            "support-bundle", "--namespace", NAMESPACE, "--out", str(out),
            "--kubeconfig", str(self.kubeconfig), "--context", f"k3d-{CLUSTER}",
        ).json()  # fmt: skip
        check(result["methods"] == ["GET"], "the support bundle used a write method")
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(out.read_bytes()))) as tar:
            files = {
                info.name: tar.extractfile(info).read() for info in tar.getmembers()
            }  # type: ignore[union-attr]
        manifest = json.loads(files["support-bundle/manifest.json"])
        check(manifest["requests"]["methods"] == ["GET"], "manifest: write method")
        check(any(name.startswith("support-bundle/logs/") for name in files), "no logs")
        secret = json.loads(
            self.kubectl(
                "-n", NAMESPACE, "get", "secret", "shop-private", "-o", "json"
            ).stdout
        )
        values = [base64.b64decode(value) for value in secret["data"].values()]
        everything = b"".join(files.values())
        for value in values:  # never printed
            for line in value.splitlines():
                if len(line) >= 12:
                    check(
                        line not in everything,
                        "a Secret value is in the support bundle",
                    )
        log(f"support-bundle: {len(files)} files, GET only, no Secret value")

    def uninstall(self, bundle: Path) -> None:
        steps = blocks(bundle / "UNINSTALL.md")
        log(f"UNINSTALL.md: {len(steps)} block(s), run as written")
        self.proc.run(["sh", "-ec", "\n".join(body for _, body in steps)], cwd=bundle,
                      env={"NAMESPACE": NAMESPACE}, timeout=600)  # fmt: skip
        kinds = self.kubectl(
            "api-resources", "--verbs=list", "-o", "name"
        ).stdout.split()
        kinds = [
            kind
            for kind in kinds
            if kind not in {"events", "events.events.k8s.io", "componentstatuses"}
        ]
        left = self.kubectl(
            "get", ",".join(kinds), "-A", "-l", PART_OF, "-o", "name", check_exit=None
        ).stdout.strip()
        check(left == "", f"objects labelled {PART_OF} remain: {left}")
        check(
            self.kubectl(
                "get", "namespace", NAMESPACE, "--ignore-not-found"
            ).stdout.strip()
            == "",
            "namespace left",
        )
        log("uninstall: nothing labelled part-of=shop remains")


def main() -> int:
    if os.environ.get("PICELI_K3S_BUNDLE") != "1":
        print("skipped: set PICELI_K3S_BUNDLE=1", file=sys.stderr)
        return 0
    for tool in ("k3d", "skopeo", "docker", "kubectl", "openssl", "uv"):
        if not shutil.which(tool):
            print(f"missing tool: {tool}", file=sys.stderr)
            return 2
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="piceli-bundle-") as directory:
        run = Acceptance(Path(directory))
        try:
            log("1. cluster and private registry")
            run.cluster()
            log("2. build and bundle")
            bundle = run.bundle()
            log("3. install as written")
            forward = run.install(bundle)
            try:
                log("4. verify")
                run.verify(bundle, forward)
            finally:
                if forward.poll() is None:
                    os.killpg(forward.pid, 9)
            log("5. support bundle")
            run.support()
            log("6. uninstall as written")
            run.uninstall(bundle)
        except StageFailed as error:
            log(f"FAILED: {error}")
            return 1
        finally:
            run.cleanup()
    log(f"passed in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
