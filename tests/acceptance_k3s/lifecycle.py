"""Opt-in: a composition's whole lifecycle on a disposable 3-node k3s cluster (k3d).

Skipped unless ``PICELI_K3S_LIFECYCLE=1``; needs ``k3d``, ``docker``,
``kubectl``, ``git`` and ``uv`` on ``PATH``, network access (PyPI, ghcr.io,
Docker Hub) and about 6 GB free in the Docker VM::

    PICELI_K3S_LIFECYCLE=1 make acceptance-k3s
    # or: PICELI_K3S_LIFECYCLE=1 uv run --frozen python tests/acceptance_k3s/lifecycle.py

The cluster: one k3s server (the API server runs on it, not in a pod;
labelled builder, controller and registry) and two agents (workloads; the
second also runs branch environments), with k3s's network policy controller.
Its kubeconfig is a scratch file; ``~/.kube/config`` is never read or written.

The composition is ``examples/lifecycle/``: three local Git repositories
(``infra``, the composition, and the product repositories ``web`` and
``store``), served over HTTPS with a Git token to the cluster's pods. Every
command an adoption note gives comes from ``lifecycle_commands.toml`` and is
run as written. Stages, in the order they run:

2. Bootstrap with the previous release (``piceli==<previous>`` from PyPI, its
   public images): login, cluster init, secrets git, gitops enable; the
   controller and the UI pull their images straight from their registry.
3. First ``main`` deploy into an empty namespace: every check passes.
1. Upgrade to the candidate (this checkout's wheel; images derived from the
   previous ones with that wheel, pushed by digest into the in-cluster
   registry, or ``--candidate-image``/``--candidate-builder-image``) with the
   upgrade commands only; nothing rolls; the bootstrap commands are idempotent.
4. A change in one source rebuilds and rolls only its image.
5. A change in a check only: the checks run, nothing rolls.
6. ``piceli promote rc main@<sha>``, then approve: rc's first install passes.
7. A ``wp-*`` branch: checks of workloads outside its stack are skipped
   (``not-in-stack``), the HTTP check passes inside isolation, a pod in
   another namespace cannot reach it.
8. A broken build: compiler output in ``failure.log_tail`` and in the kept
   Job's log; deleting the branch removes the Job.
9. Deleting the branch: no namespace, volume, cluster RBAC or controller
   state of it left.
10. Retention plan, approve, registry garbage collection: only seeded
    orphans and old controller/builder copies go; every workload still pulls.

The cluster, the scratch directory, the local images and both scratch
virtual environments are removed whatever the outcome. A failing stage prints
the controller's log tail, the GitOps status and failed Job logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifecycle_support import (
    Commands,
    GitServer,
    GitTlsWebhook,
    K3d,
    Proc,
    Repos,
    Result,
    StageFailed,
    check,
    log,
    make_tls,
    wait_for,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "lifecycle"
COMMANDS = Path(__file__).resolve().parent / "lifecycle_commands.toml"
REGISTRY_HOST = "piceli-registry.piceli-system.svc:5000"
SYSTEM = "piceli-system"
CONTROLLER = "piceli-gitops"
UI = "piceli-ui"
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
#: The previous release and its public images (amd64 + arm64).
PREVIOUS = "0.14.4"
PREVIOUS_IMAGE = (
    "ghcr.io/pynenc/piceli-controller"
    "@sha256:e130dbc2859dff2bee4f83f513323bdcb50535b955c23a33dddc118425d202ef"
)
PREVIOUS_BUILDER = (
    "ghcr.io/pynenc/piceli-builder"
    "@sha256:fcb10502d3ff494d033870a2588a607a212aa148255d91134ee90e17f7014ba6"
)
WORKLOADS = ("web", "store", "watcher", "cache", "reporter")
ALL_CHECKS = {
    "web-index",
    "web-binary",
    "store-ready",
    "watcher-reads-api",
    "reporter-runs",
    "site-config",
}
STAGE_ORDER = ("2", "3", "1", "4", "5", "6", "7", "8", "9", "10")
STAGE_TITLES = {
    "1": "upgrade from the previous release",
    "2": "bootstrap",
    "3": "first main deploy",
    "4": "change one source",
    "5": "change only a check",
    "6": "promote rc and approve",
    "7": "branch environment",
    "8": "broken build",
    "9": "delete the branch environment",
    "10": "retention and registry GC",
}
#: Stages whose failure stops the run (the rest depend on them).
CRITICAL = {"2", "3"}

CANDIDATE_DOCKERFILE = """\
ARG BASE
FROM ${BASE} AS controller
USER root
RUN --mount=type=bind,source=dist,target=/tmp/dist \\
    set -eu; wheel="$(ls /tmp/dist/piceli-*.whl)"; \\
    pip install --no-cache-dir --force-reinstall --no-deps "$wheel"; \\
    pip install --no-cache-dir "piceli[ui] @ file://${wheel}"
USER 65532:65532

# The candidate's build Job image: the controller plus a C compiler (what
# this composition's host build runs; the published builder has more).
FROM controller AS builder
USER root
RUN apt-get update \\
    && apt-get install -y --no-install-recommends gcc libc6-dev make \\
    && rm -rf /var/lib/apt/lists/*
USER 65532:65532
"""

SEED_DOCKERFILE = """\
FROM {busybox}
LABEL io.piceli.lifecycle.seed="{label}"
"""

EXTRA_CHECK = """EXTRA_CHECKS: list = [
    Checks.exec(
        "deployment/cache",
        ["echo", "cache-answers"],
        output_contains="cache-answers",
        name="cache-answers",
    ),
]"""


class Lifecycle:
    def __init__(self, args: argparse.Namespace, scratch: Path) -> None:
        self.args = args
        self.scratch = scratch
        self.commands = Commands.load(COMMANDS)
        self.token = secrets.token_urlsafe(24)  # throwaway Git token, never printed
        self.results: list[tuple[str, str, float, str]] = []
        self.images_to_remove: list[str] = []
        self.servers: list[Any] = []
        path = os.environ.get("PATH", "")
        self.env = {
            "PATH": path,
            "HOME": os.environ.get("HOME", str(scratch)),
            "TMPDIR": str(scratch / "tmp"),
            "PICELI_PROFILES_DIR": str(scratch / "profiles"),
            # Nothing may fall back to an ambient kubeconfig.
            "KUBECONFIG": str(scratch / "no-such-kubeconfig"),
            "LANG": "C.UTF-8",
            "NO_COLOR": "1",
            "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR")
            or str(Path.home() / ".cache" / "uv"),
        }
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONFIG",
            "SSL_CERT_FILE",
            "NIX_SSL_CERT_FILE",
        ):
            if os.environ.get(key):
                self.env[key] = os.environ[key]
        (scratch / "tmp").mkdir()
        self.proc = Proc(self.env, scratch)
        k3d = shutil.which("k3d")
        self.cluster = K3d(
            args.name,
            self.proc,
            [k3d] if k3d else ["nix", "shell", "nixpkgs#k3d", "-c", "k3d"],
            scratch / "kubeconfig",
            shutil.which("kubectl") or "kubectl",
        )
        self.repos = Repos(scratch / "git", self.proc)
        self.values: dict[str, str] = {}
        self.piceli = ""  # the CLI of the release in use
        self.prev_cli = ""
        self.cand_cli = ""
        self.candidate_image = args.candidate_image or ""
        self.candidate_builder = args.candidate_builder_image or ""
        self.seeds: dict[str, str] = {}
        self.arch = "amd64"
        self.log_since = ""
        self.since_epoch = 0.0
        self.first_main: dict[str, Any] = {}

    # ------------------------------------------------------------ helpers
    @property
    def infra(self) -> Path:
        return self.repos.work("infra")

    def kubectl(self, *args: str, **kwargs: Any) -> Result:
        return self.cluster.kubectl(*args, **kwargs)

    def run_group(self, group: str) -> dict[str, Result]:
        values = {**self.values, "piceli": self.piceli}
        done = self.commands.run_group(
            group, self.proc, values, {"git_token": self.token}, cwd=self.infra
        )
        self.values.update({k: v for k, v in values.items() if k not in {"piceli"}})
        return done

    def run_step(self, step: str, **extra: str) -> Result:
        values = {**self.values, "piceli": self.piceli, **extra}
        result = self.commands.run_step(
            step, self.proc, values, {"git_token": self.token}, cwd=self.infra
        )
        self.values.update({k: v for k, v in values.items() if k != "piceli"})
        return result

    def status(self) -> dict[str, Any]:
        found = self.proc.run(
            [
                self.piceli, "gitops", "status", "--kubeconfig", str(self.cluster.kubeconfig),
                "--context", self.cluster.context, "--json",
            ],
            quiet=True, check_exit=None,
        )  # fmt: skip
        body = found.json()
        return body if isinstance(body, dict) else {}

    def env_record(self, name: str) -> dict[str, Any]:
        return dict((self.status().get("envs") or {}).get(name) or {})

    def wait_env(
        self,
        name: str,
        done: Callable[[dict[str, Any]], bool],
        *,
        timeout: float,
        what: str,
    ) -> dict[str, Any]:
        last: dict[str, Any] = {}

        def probe() -> dict[str, Any] | None:
            nonlocal last
            last = self.env_record(name)
            return last if done(last) else None

        try:
            return dict(wait_for(f"{name}: {what}", probe, timeout=timeout, interval=5))
        except StageFailed as error:
            brief = {
                k: last.get(k)
                for k in ("state", "reason", "revision", "failure", "plan_hash")
            }
            raise StageFailed(
                f"{error}; last record: {json.dumps(brief)[:1500]}"
            ) from None

    def controller_log(self, since: str | None = None) -> str:
        args = ["logs", f"deploy/{CONTROLLER}", "-n", SYSTEM, "--tail=-1"]
        if since:
            args.append(f"--since-time={since}")
        found = self.kubectl(*args, check_exit=None, timeout=60)
        return found.stdout + found.stderr if found.code == 0 else ""

    def mark(self) -> str:
        """An RFC 3339 instant: controller log lines (and runs) after it."""
        self.since_epoch = time.time() - 2
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2))

    def generations(self, namespace: str) -> dict[str, int]:
        found: dict[str, int] = {}
        for kind in ("deployments", "statefulsets"):
            body = self.cluster.get(kind, "-n", namespace) or {"items": []}
            for item in body["items"]:
                found[f"{kind[:-1]}/{item['metadata']['name']}"] = int(
                    item["metadata"]["generation"]
                )
        return found

    def pod_images(
        self, namespace: str, selector: str | None = None
    ) -> list[dict[str, str]]:
        args = ["pods", "-n", namespace] + (["-l", selector] if selector else [])
        body = self.cluster.get(*args) or {"items": []}
        found = []
        for pod in body["items"]:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            for status in (pod.get("status") or {}).get("containerStatuses") or []:
                found.append(
                    {
                        "pod": pod["metadata"]["name"],
                        "node": pod["spec"].get("nodeName", ""),
                        "image": status.get("image", ""),
                        "image_id": status.get("imageID", ""),
                        "ready": str(status.get("ready")),
                    }
                )
        return found

    def deployed(
        self, name: str, sources: dict[str, str] | None = None
    ) -> Callable[[dict[str, Any]], bool]:
        def done(record: dict[str, Any]) -> bool:
            if record.get("state") in {"failed"}:
                raise_failed(name, record)
            if record.get("state") != "deployed":
                return False
            revision = record.get("deployed_revision") or record.get("revision") or {}
            return all(revision.get(k) == v for k, v in (sources or {}).items())

        return done

    def checks_lines(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if "[checks]" in line]

    def runs(self, part: str, since: float) -> list[dict[str, Any]]:
        """Run records of the controller's pipeline runs whose path holds ``part``.

        Read from the controller's state volume (``<env>/runs/<id>.json``),
        newest last: each run's stage states and its checks stage output.
        """
        script = (
            "import json, os, sys\n"
            "part, since = sys.argv[1], float(sys.argv[2])\n"
            "found = []\n"
            "for root, _, files in os.walk('/var/lib/piceli-gitops'):\n"
            "    if not root.endswith('/runs') or part not in root:\n"
            "        continue\n"
            "    for name in files:\n"
            "        path = os.path.join(root, name)\n"
            "        if not name.endswith('.json') or os.path.getmtime(path) < since:\n"
            "            continue\n"
            "        try:\n"
            "            data = json.load(open(path))\n"
            "        except Exception:\n"
            "            continue\n"
            "        stages = data.get('stages') or {}\n"
            "        checks = (stages.get('checks') or {}).get('output') or {}\n"
            "        found.append({'path': path, 'mtime': os.path.getmtime(path),\n"
            "            'state': data.get('state'),\n"
            "            'stages': {k: (v or {}).get('state') for k, v in stages.items()},\n"
            "            'checks': {'passed': checks.get('passed'), 'why': checks.get('why'),\n"
            "                'skipped': checks.get('skipped') or [],\n"
            "                'results': [{'name': r.get('name'), 'passed': r.get('passed'),\n"
            "                    'code': r.get('code')} for r in checks.get('results') or []]}})\n"
            "print(json.dumps(sorted(found, key=lambda r: r['mtime'])))\n"
        )
        found = self.kubectl(
            "exec", "-n", SYSTEM, f"deploy/{CONTROLLER}", "--",
            "python", "-c", script, part, str(since),
            check_exit=None, timeout=60,
        )  # fmt: skip
        try:
            return list(json.loads(found.stdout or "[]"))
        except ValueError:
            return []

    def checked_run(self, part: str, since: float) -> dict[str, Any]:
        """The newest run since ``since`` whose checks stage ran or was skipped."""
        runs = [
            r
            for r in self.runs(part, since)
            if r["stages"].get("checks") not in {None, "pending"}
        ]
        check(
            runs, f"no pipeline run with a checks stage for {part!r} since the change"
        )
        run = runs[-1]
        brief = {r["name"]: r["passed"] for r in run["checks"]["results"]}
        log(f"run {Path(run['path']).name}: {run['state']}, stages {run['stages']}, "
            f"checks {brief}, why {run['checks']['why']!r}, "
            f"skipped {[i.get('check') for i in run['checks']['skipped']]}")  # fmt: skip
        return run

    # ------------------------------------------------------------ set up
    def setup(self) -> None:
        log(f"scratch directory {self.scratch}")
        self._venvs()
        self.cluster.create()
        nodes = self.cluster.get("nodes")["items"]
        names = sorted(n["metadata"]["name"] for n in nodes)
        check(
            names == sorted([self.cluster.server, *self.cluster.agents]),
            f"nodes {names}",
        )
        archs = {n["status"]["nodeInfo"]["architecture"] for n in nodes}
        check(len(archs) == 1, f"mixed node architectures {archs}")
        self.arch = archs.pop()
        # A local-path class that keeps volumes, like a retained class of a
        # real cluster (the composition's storage_class).
        self.cluster.apply(
            [
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {"name": "lifecycle-retain"},
                    "provisioner": "rancher.io/local-path",
                    "reclaimPolicy": "Retain",
                    "volumeBindingMode": "WaitForFirstConsumer",
                }
            ]
        )
        # Git over HTTPS for the pods, and the test-only webhook that makes
        # Git in piceli-system accept the throwaway certificate.
        tls = self.scratch / "tls"
        tls.mkdir()
        cert, key, _ = make_tls(tls, "git", ["host.k3d.internal", "127.0.0.1"])
        self.git = GitServer(self.repos.remotes, cert, key, "git", self.token)
        # The API server (k3s on the server node) does not resolve
        # host.k3d.internal (only CoreDNS has it): the webhook goes by address.
        host_ip = self._host_ip()
        wcert, wkey, wca = make_tls(tls, "webhook", [host_ip])
        self.webhook = GitTlsWebhook(wcert, wkey, wca, host_ip)
        for server in (self.git, self.webhook):
            server.start()
            self.servers.append(server)
        self.cluster.apply([self.webhook.configuration()])
        self._repositories()
        self._preflight()

    def _venvs(self) -> None:
        uv = shutil.which("uv") or "uv"
        prev = self.scratch / "venv-previous"
        self.proc.run([uv, "venv", "-q", "--python", "3.12", str(prev)], timeout=300)
        self.proc.run(
            [uv, "pip", "install", "-q", "--python", str(prev / "bin" / "python"),
             f"piceli=={self.args.previous}"],
            timeout=900,
        )  # fmt: skip
        self.prev_cli = str(prev / "bin" / "piceli")
        cand = self.scratch / "venv-candidate"
        self.proc.run([uv, "venv", "-q", "--python", "3.12", str(cand)], timeout=300)
        if self.args.candidate_version:
            spec = f"piceli=={self.args.candidate_version}"
        else:
            dist = self.scratch / "dist"
            self.proc.run(
                [
                    uv,
                    "build",
                    "-q",
                    "--wheel",
                    "--out-dir",
                    str(dist),
                    str(self.args.candidate),
                ],
                timeout=600,
            )
            wheel = next(dist.glob("piceli-*.whl"))
            spec = str(wheel)
        self.proc.run(
            [uv, "pip", "install", "-q", "--python", str(cand / "bin" / "python"), spec],
            timeout=900,
        )  # fmt: skip
        self.cand_cli = str(cand / "bin" / "piceli")

    def _site(self, controller_image: str) -> str:
        return (
            '"""Where this composition runs (written by the lifecycle acceptance)."""\n\n'
            f"API = {self.cluster.api_server()!r}\n"
            f"SERVER = {self.cluster.server!r}\n"
            f"AGENT_A = {self.cluster.agents[0]!r}\n"
            f"AGENT_B = {self.cluster.agents[1]!r}\n"
            f"ARCH = {self.arch!r}\n"
            f"GIT_BASE = {f'https://host.k3d.internal:{self.git.port}'!r}\n"
            f"CONTROLLER_IMAGE = {controller_image!r}\n"
            'POLL = "10s"\n'
        )

    def _repositories(self) -> None:
        for name in ("web", "store"):
            self.repos.create(name, EXAMPLE / name)
        self.repos.create("infra", EXAMPLE / "infra")
        self.repos.commit(
            "infra",
            "pin the cluster and the previous release",
            {"lifecycle_site.py": self._site(self.args.previous_image)},
        )
        self.values.update(
            kubeconfig=str(self.cluster.kubeconfig),
            context=self.cluster.context,
            repo=self.git.url("infra"),
            image=self.args.previous_image,
            builder_image=self.args.previous_builder_image,
            platform=f"linux/{self.arch}",
            kubectl=self.cluster.kubectl_bin,
        )

    def _host_ip(self) -> str:
        """The host's address as k3d gave it to CoreDNS (host.k3d.internal)."""

        def found() -> str | None:
            config = self.cluster.get("configmap", "coredns", "-n", "kube-system") or {}
            for line in (config.get("data") or {}).get("NodeHosts", "").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == "host.k3d.internal":
                    return parts[0]
            return None

        return str(
            wait_for("host.k3d.internal in CoreDNS", found, timeout=180, interval=3)
        )

    def _preflight(self) -> None:
        """A pod reaches the Git server (401 without the token)."""
        script = (
            "import ssl, urllib.request, urllib.error, sys\n"
            "ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE\n"
            "import time\n"
            "for attempt in range(30):\n"
            "    try:\n"
            f"        urllib.request.urlopen('{self.git.url('infra')}/info/refs?service=git-upload-pack', context=ctx, timeout=10)\n"
            "        print('open'); break\n"
            "    except urllib.error.HTTPError as e:\n"
            "        print('status', e.code); break\n"
            "    except OSError as e:\n"
            "        print('retry', e); time.sleep(3)\n"
        )
        self.kubectl("create", "namespace", "lc-preflight")
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "git-reach", "namespace": "lc-preflight"},
            "spec": {
                "restartPolicy": "Never",
                "nodeSelector": {"kubernetes.io/hostname": self.cluster.server},
                "containers": [
                    {"name": "main", "image": self.args.previous_image,
                     "command": ["python", "-c", script]}
                ],
            },
        }  # fmt: skip
        self.cluster.apply([pod])
        wait_for(
            "the preflight pod",
            lambda: (
                (self.cluster.get("pod", "git-reach", "-n", "lc-preflight") or {})
                .get("status", {})
                .get("phase")
                in {"Succeeded", "Failed"}
            ),
            timeout=600,
        )
        out = self.kubectl(
            "logs", "git-reach", "-n", "lc-preflight", check_exit=None
        ).stdout
        self.kubectl(
            "delete", "namespace", "lc-preflight", "--wait=false", check_exit=None
        )
        check("status 401" in out, f"a pod cannot reach the Git server: {out[-500:]}")
        log("preflight: pods reach the Git server (401 without the token)")

    # ------------------------------------------------------------ stages
    def stage_2_bootstrap(self) -> None:
        check(
            self.cluster.get("namespace", "lc-main") is None,
            "lc-main exists before bootstrap",
        )
        self.piceli = self.prev_cli
        self.log_since = self.mark()
        self.run_group("bootstrap")
        self._wait_controller(self.args.previous_image)
        self._check_image_ids(self.args.previous_image, public=True)
        status = self.run_step("cluster-status").json() or {}
        log(f"cluster status: {status.get('state')} {status.get('problems') or ''}")

    def _wait_controller(
        self,
        image: str,
        names: tuple[str, ...] = (CONTROLLER, UI),
        timeout: float = 900,
    ) -> None:
        digest = image.split("@", 1)[1]

        def ready() -> bool:
            for name in names:
                pods = self.pod_images(SYSTEM, f"app.kubernetes.io/name={name}") or [
                    p
                    for p in self.pod_images(SYSTEM)
                    if p["pod"].startswith(name + "-")
                ]
                good = [
                    p
                    for p in pods
                    if p["ready"] == "True" and p["image_id"].endswith(digest)
                ]
                if not good:
                    return False
            return True

        wait_for(
            f"{', '.join(names)} ready on {image}", ready, timeout=timeout, interval=5
        )

    def _check_image_ids(self, image: str, *, public: bool) -> None:
        repository, digest = image.split("@", 1)
        pods = [
            p for p in self.pod_images(SYSTEM)
            if p["pod"].startswith((CONTROLLER + "-", UI + "-")) and p["ready"] == "True"
        ]  # fmt: skip
        check(pods, "no ready controller or UI pod")
        for pod in pods:
            log(f"{pod['pod']} on {pod['node']}: {pod['image_id']}")
            check(
                pod["image_id"].endswith(digest),
                f"{pod['pod']} runs {pod['image_id']}, not {digest}",
            )
            if public:
                # Pulled straight from the public registry: no mirror copy.
                check(
                    pod["image_id"].startswith(repository + "@"),
                    f"{pod['pod']} pulled {pod['image_id']}, not from {repository}",
                )
        # The test-only webhook gave Git the throwaway certificate's trust.
        env = self.kubectl(
            "get",
            f"deploy/{CONTROLLER}",
            "-n",
            SYSTEM,
            "-o",
            "jsonpath={.metadata.name}",
        ).stdout
        check(env == CONTROLLER, "no controller Deployment")
        check(
            self.webhook.mutated > 0,
            "the test webhook never mutated a piceli-system pod",
        )

    def stage_3_first_main(self) -> None:
        sources = {name: self.repos.head(name) for name in ("infra", "web", "store")}

        # The first install creates a ClusterRole (the watcher's node reader):
        # outside the approval policy, so main asks once; approve that hash.
        def settled(record: dict[str, Any]) -> bool:
            if record.get("state") == "approval-required" and record.get("plan_hash"):
                return True
            return self.deployed("main", sources)(record)

        record = self.wait_env("main", settled, timeout=1800, what="first deploy")
        if record.get("state") == "approval-required":
            log(f"main asks for approval ({record.get('reason')}); approving its plan")
            self.values["main_hash"] = str(record["plan_hash"])
            self.run_step("approve-main")
            record = self.wait_env(
                "main",
                self.deployed("main", sources),
                timeout=1800,
                what="first deploy",
            )
        run = self.checked_run("/main/", self.since_epoch)
        results = {r["name"]: r["passed"] for r in run["checks"]["results"]}
        check(set(results) >= ALL_CHECKS, f"checks run on main: {sorted(results)}")
        check(all(results.values()), f"failed checks on main: {results}")
        check(
            run["stages"].get("prerollout") == "done",
            f"prerollout {run['stages'].get('prerollout')}",
        )
        states = {
            k: v.get("state") for k, v in (record.get("components") or {}).items()
        }
        log(f"main components: {states}")
        check(set(states) >= {"web", "store", "watcher"}, f"components {states}")
        check(
            set(states.values()) <= {"synced"}, f"components not all synced: {states}"
        )
        pods = self.pod_images("lc-main")
        check(
            len({p["pod"] for p in pods}) >= len(WORKLOADS), f"pods in lc-main: {pods}"
        )
        check(all(p["ready"] == "True" for p in pods), f"unready pods: {pods}")
        check(
            all(p["node"] == self.cluster.agents[0] for p in pods),
            "main pods off agent-0",
        )
        self.first_main = {"record": record, "generations": self.generations("lc-main")}

    def stage_1_upgrade(self) -> None:
        self.piceli = self.cand_cli
        if not (self.candidate_image and self.candidate_builder):
            self._build_candidate_images()
        self._seed_registry()
        before = self.generations("lc-main")
        log(f"candidate images: {self.candidate_image} / {self.candidate_builder}")
        # The note's upgrade: pin the new images in the composition, push,
        # then the upgrade commands only.
        sha = self.repos.commit(
            "infra",
            "upgrade piceli",
            {"lifecycle_site.py": self._site(self.candidate_image)},
        )
        self.values.update(
            image=self.candidate_image, builder_image=self.candidate_builder
        )
        self.log_since = self.mark()
        problems: list[str] = []
        done = self.run_group("upgrade")
        plan = done["gitops-enable-plan"].json() or {}
        if plan.get("ui") != "included":
            problems.append(
                f"the gitops enable plan does not include the UI (ui: {plan.get('ui')!r})"
            )
        self._wait_controller(self.candidate_image, names=(CONTROLLER,))
        try:
            self._wait_controller(self.candidate_image, names=(UI,), timeout=300)
        except StageFailed as error:
            problems.append(f"the UI was not upgraded by gitops enable: {error}")
        try:
            self._check_image_ids(
                self.candidate_image, public=self.args.candidate_public
            )
        except StageFailed as error:
            problems.append(str(error))
        record = self.wait_env(
            "main",
            self.deployed("main", {"infra": sha}),
            timeout=900,
            what="redeploy after the upgrade",
        )
        after = self.generations("lc-main")
        text = self.controller_log(self.log_since)
        lines = [
            line
            for line in text.splitlines()
            if line.find("main: ") >= 0 or "[checks]" in line
        ]
        log("after upgrade: " + " | ".join(lines[-6:]))
        if after != before:
            problems.append(f"the upgrade rolled workloads: {before} -> {after}")
        # The first sync on the new release verifies every environment once:
        # it has no recorded checks hash yet (trigger "unverified").
        verification = record.get("verification") or {}
        log(f"main after the upgrade: last_action {record.get('last_action')!r}, "
            f"health {record.get('health')!r}, verification {json.dumps(verification)[:300]}")  # fmt: skip
        if (
            verification.get("state") != "verified"
            or verification.get("trigger") != "unverified"
        ):
            problems.append(
                "main was not verified once after the upgrade "
                f"(verification {json.dumps(verification)[:200]})"
            )
        if verification.get("rolled"):
            problems.append(f"the verification rolled {verification.get('rolled')}")
        # Re-running the bootstrap commands with the new release is idempotent.
        done = self.run_group("bootstrap")
        for step in ("cluster-init-plan", "gitops-enable-plan"):
            body = done[step].json() or {}
            if not (done[step].code == 0 and body.get("state") == "unchanged"):
                problems.append(
                    f"{step} after the upgrade: exit {done[step].code}, {body.get('state')}"
                )
        # The registry of the composition's Cluster (0.14.5): its claim's usage.
        registry = self.run_step_soft("registry-status", problems)
        if registry and _used_source(registry) not in {"du", "volume-stats"}:
            problems.append(f"registry status used_source {_used_source(registry)!r}")
        check(not problems, "; ".join(problems))

    def _build_candidate_images(self) -> None:
        ctx = self.scratch / "image-context"
        (ctx / "dist").mkdir(parents=True)
        for wheel in (self.scratch / "dist").glob("piceli-*.whl"):
            shutil.copy(wheel, ctx / "dist" / wheel.name)
        check(
            any((ctx / "dist").iterdir()),
            "no candidate wheel (use --candidate-image with --candidate-version)",
        )
        suffix = uuid.uuid4().hex[:8]
        platform = f"linux/{self.arch}"
        refs = {}
        if self.args.images == "dockerfile":
            dockerfile, base = ROOT / "images" / "Dockerfile", []
        else:
            dockerfile = ctx / "Dockerfile"
            dockerfile.write_text(CANDIDATE_DOCKERFILE)
            base = ["--build-arg", f"BASE={self.args.previous_image}"]
        for target in ("controller", "builder"):
            tag = f"piceli-lc-candidate-{target}:{suffix}"
            self.proc.run(
                ["docker", "buildx", "build", "-q", "--load", "--platform", platform,
                 "--target", target, "-f", str(dockerfile), *base, "-t", tag, str(ctx)],
                timeout=3600,
            )  # fmt: skip
            self.images_to_remove.append(tag)
            refs[target] = self._push(tag, f"lifecycle/piceli-{target}")
        self.candidate_image = refs["controller"]
        self.candidate_builder = refs["builder"]

    def _push(self, tag: str, repository: str) -> str:
        """Push a local image by digest into the in-cluster registry."""
        os.environ["PICELI_PROFILES_DIR"] = self.env["PICELI_PROFILES_DIR"]
        from piceli import App, Pipeline, Registry, Target
        from piceli.pipeline.backend import Backend
        from piceli.pipeline.runner import PipelineRunner

        image_id = self.proc.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag], quiet=True
        ).stdout.strip()
        pipeline = Pipeline(
            App("lc-push"),
            Target.kubeconfig(
                self.cluster.kubeconfig, context=self.cluster.context, namespace=SYSTEM
            ),
            deliver=Registry.in_cluster(on=self.cluster.server),
            state_dir=self.scratch / "push-state",
        )
        runner = PipelineRunner.__new__(PipelineRunner)
        runner.pipeline = pipeline
        log(f"push {tag} -> {repository}")
        receipt = Backend().registry_deliver(runner._route(), image_id, repository)
        check(
            receipt.get("state") == "succeeded",
            f"push of {tag}: {receipt.get('reason')}",
        )
        return str(receipt["pull_ref"])

    def _seed_registry(self) -> None:
        """Digest-only orphans and old controller/builder copies for stage 10."""
        ctx = self.scratch / "seed-context"
        ctx.mkdir()
        for name, repository in (
            ("orphan", "lifecycle/orphan"),
            ("old-controller", "lifecycle/piceli-controller"),
            ("old-builder", "lifecycle/piceli-builder"),
        ):
            label = f"{name}-{uuid.uuid4().hex[:8]}"
            (ctx / "Dockerfile").write_text(
                SEED_DOCKERFILE.format(busybox=BUSYBOX, label=label)
            )
            tag = f"piceli-lc-seed-{name}:{label}"
            self.proc.run(
                ["docker", "buildx", "build", "-q", "--load", "--platform", f"linux/{self.arch}",
                 "-t", tag, str(ctx)],
                timeout=900,
            )  # fmt: skip
            self.images_to_remove.append(tag)
            self.seeds[name] = self._push(tag, repository)
        log(f"seeded: {json.dumps(self.seeds)}")

    def stage_4_one_source(self) -> None:
        before_record = self.env_record("main")
        before = self.generations("lc-main")
        digests = {
            k: v.get("digest")
            for k, v in (before_record.get("components") or {}).items()
        }
        self.log_since = self.mark()
        run = self.repos.read("store", "bin/run.sh").replace(
            "# The store:", "# The store (second edition):"
        )
        sha = self.repos.commit("store", "store: second edition", {"bin/run.sh": run})
        record = self.wait_env(
            "main",
            self.deployed("main", {"store": sha}),
            timeout=1200,
            what="store change",
        )
        after = self.generations("lc-main")
        states = {
            k: v.get("state") for k, v in (record.get("components") or {}).items()
        }
        log(f"components after the store change: {states}")
        check(states.get("store") == "synced", f"store {states.get('store')}")
        for name in ("web", "watcher"):
            check(states.get(name) == "unchanged", f"{name} {states.get(name)}")
            check(
                record["components"][name]["digest"] == digests.get(name),
                f"{name} digest changed",
            )
        check(
            record["components"]["store"]["digest"] != digests.get("store"),
            "store digest unchanged",
        )
        check(
            after["statefulset/store"] > before["statefulset/store"],
            "store was not rolled",
        )
        for key, value in before.items():
            if key != "statefulset/store":
                check(
                    after.get(key) == value,
                    f"{key} rolled: {value} -> {after.get(key)}",
                )
        # The store's new image passed its pre-rollout checks (configuration
        # and read-only upgrade check) and its claim got a restore point.
        run = self.checked_run("/main/", self.since_epoch)
        for stage in ("prerollout", "backup", "apply", "checks"):
            check(
                run["stages"].get(stage) == "done",
                f"main {stage}: {run['stages'].get(stage)}",
            )

    def stage_5_check_only(self) -> None:
        before = self.generations("lc-main")
        previous = (self.env_record("main").get("verification") or {}).get(
            "checks_hash"
        )
        self.log_since = self.mark()
        source = self.repos.read("infra", "lifecycle_app.py")
        check(
            "EXTRA_CHECKS: list = []" in source,
            "lifecycle_app.py has no EXTRA_CHECKS marker",
        )
        sha = self.repos.commit(
            "infra", "checks: the cache answers",
            {"lifecycle_app.py": source.replace("EXTRA_CHECKS: list = []", EXTRA_CHECK)},
        )  # fmt: skip
        record = self.wait_env(
            "main",
            self.deployed("main", {"infra": sha}),
            timeout=900,
            what="check change",
        )
        after = self.generations("lc-main")
        text = self.controller_log(self.log_since)
        lines = self.checks_lines(text)
        log("checks after the check change: " + (" | ".join(lines[-6:]) or "none"))
        verification = record.get("verification") or {}
        log(f"main: last_action {record.get('last_action')!r}, health {record.get('health')!r}, "
            f"verification {json.dumps(verification)[:400]}")  # fmt: skip
        problems = []
        if after != before:
            problems.append(f"a check change rolled workloads: {before} -> {after}")
        if any("already verified" in line for line in lines):
            problems.append(
                "the controller skipped the new check ('release already verified')"
            )
        try:
            run = self.checked_run("/main/", self.since_epoch)
            results = {r["name"]: r["passed"] for r in run["checks"]["results"]}
            if results.get("cache-answers") is not True:
                problems.append(
                    f"the new check did not run and pass (checks {results}, "
                    f"why {run['checks']['why']!r})"
                )
        except StageFailed as error:
            problems.append(str(error))
        if "main: verified (checks changed); rolled nothing" not in text:
            problems.append(
                "no 'main: verified (checks changed); rolled nothing' log line"
            )
        expected = {"state": "verified", "trigger": "checks-changed", "rolled": []}
        for key, value in expected.items():
            if verification.get(key) != value:
                problems.append(
                    f"verification.{key} is {verification.get(key)!r}, not {value!r}"
                )
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(verification.get("checks_hash"))
        ):
            problems.append("verification.checks_hash is not sha256:<hex>")
        elif previous and verification.get("checks_hash") == previous:
            problems.append("verification.checks_hash did not change with the checks")
        if record.get("last_action") != "verified" or record.get("health") != "healthy":
            problems.append(
                f"last_action {record.get('last_action')!r}, health {record.get('health')!r} "
                "(expected 'verified', 'healthy')"
            )
        check(not problems, "; ".join(problems))

    def stage_6_promote(self) -> None:
        check(
            self.cluster.get("namespace", "lc-rc") is None,
            "lc-rc exists before the promotion",
        )
        sha = self.repos.head("web")
        self.log_since = self.mark()
        self.run_step("promote-rc", sha=sha)
        record = self.wait_env(
            "rc", lambda r: r.get("state") == "approval-required" and bool(r.get("plan_hash")),
            timeout=900, what="approval request",
        )  # fmt: skip
        self.values["rc_hash"] = str(record["plan_hash"])
        self.run_step("approve-rc")
        record = self.wait_env(
            "rc",
            self.deployed("rc", {"web": sha}),
            timeout=1200,
            what="approved deploy",
        )
        run = self.checked_run("/rc/", self.since_epoch)
        results = {r["name"]: r["passed"] for r in run["checks"]["results"]}
        check(set(results) >= ALL_CHECKS, f"checks run on rc: {sorted(results)}")
        check(all(results.values()), f"failed checks on rc: {results}")
        check(
            run["stages"].get("prerollout") == "done",
            f"rc prerollout {run['stages'].get('prerollout')}",
        )
        pods = self.pod_images("lc-rc")
        check(pods and all(p["ready"] == "True" for p in pods), f"rc pods: {pods}")

    def stage_7_branch(self) -> None:
        self.log_since = self.mark()
        page = self.repos.read("web", "site/index.html").replace(
            "first edition", "feature edition"
        )
        self.repos.commit(
            "web", "web: feature page", {"site/index.html": page}, branch="wp-feature"
        )
        record = self.wait_env(
            "wp-feature",
            self.deployed("wp-feature"),
            timeout=1500,
            what="branch deploy",
        )
        namespace = record.get("namespace") or "lc-wp-feature"
        check(namespace == "lc-wp-feature", f"namespace {namespace}")
        run = self.checked_run("branches", self.since_epoch)
        results = {r["name"]: r["passed"] for r in run["checks"]["results"]}
        skipped = {i.get("check"): i.get("why") for i in run["checks"]["skipped"]}
        check(
            skipped.get("reporter-runs") == "not-in-stack",
            f"the reporter check was not skipped as not-in-stack: {skipped}",
        )
        check(
            results.get("web-index") is True, f"the HTTP check in the branch: {results}"
        )
        check(all(results.values()), f"failed checks in the branch: {results}")
        pods = self.pod_images(namespace)
        check(
            pods and all(p["node"] == self.cluster.agents[1] for p in pods),
            f"branch pods: {pods}",
        )
        policies = [
            p["metadata"]["name"]
            for p in self.cluster.get("networkpolicies", "-n", namespace)["items"]
        ]
        check("piceli-env-isolation" in policies, f"network policies {policies}")
        inside = self._wget(namespace, "http://web:8080/index.html")
        check(
            inside and "feature edition" in inside,
            "the web page is not served inside the branch",
        )
        outside = self._wget("lc-main", f"http://web.{namespace}.svc:8080/index.html")
        check(
            outside is None, "a pod in lc-main reached the isolated branch environment"
        )

    def _wget(self, namespace: str, url: str) -> str | None:
        pods = [
            p["pod"]
            for p in self.pod_images(namespace)
            if p["pod"].startswith("cache-")
        ]
        check(pods, f"no cache pod in {namespace}")
        found = self.kubectl(
            "exec", "-n", namespace, pods[0], "--", "wget", "-q", "-T", "5", "-O", "-", url,
            check_exit=None, timeout=60,
        )  # fmt: skip
        return found.stdout if found.code == 0 else None

    def stage_8_broken_build(self) -> None:
        source = self.repos.read("web", "tool/version.c")
        broken = source.replace("return 0;", "return 0 /* deliberate failure */ +;")
        self.repos.commit(
            "web", "web: broken build", {"tool/version.c": broken}, branch="wp-broken"
        )
        record = self.wait_env(
            "wp-broken",
            lambda r: bool((r.get("failure") or {}).get("log_tail")),
            timeout=1500, what="failed build with its log tail",
        )  # fmt: skip
        failure = record["failure"]
        tail = failure["log_tail"]
        log(
            f"failure: reason {record.get('reason')}, kept_job {failure.get('kept_job')}"
        )
        log("log_tail (end): " + tail[-600:].replace("\n", " | "))
        check(
            "version.c" in tail and "error" in tail,
            "no compiler output in failure.log_tail",
        )
        job = failure.get("kept_job")
        check(job, "the failure names no kept Job")
        job_log = self.kubectl(
            "logs", f"job/{job}", "-n", SYSTEM, check_exit=None
        ).stdout
        check(
            "version.c" in job_log and "error" in job_log,
            "no compiler output in the kept Job's log",
        )
        builder = self.pod_images(SYSTEM, f"job-name={job}")
        log(f"build pod image: {builder[:1]}")
        if builder:
            check(builder[0]["image_id"].endswith(self.candidate_builder.split("@", 1)[1]),
                  f"the build ran {builder[0]['image_id']}")  # fmt: skip
        self.repos.delete_branch("web", "wp-broken")
        wait_for(
            "the kept build Job removed with its branch",
            lambda: (
                self.cluster.get("job", job, "-n", SYSTEM) is None
                and "wp-broken" not in (self.status().get("envs") or {})
            ),
            timeout=600,
        )

    def stage_9_teardown(self) -> None:
        namespace = "lc-wp-feature"
        self.repos.delete_branch("web", "wp-feature")
        wait_for(
            "the branch environment removed",
            lambda: (
                "wp-feature" not in (self.status().get("envs") or {})
                and self.cluster.get("namespace", namespace) is None
            ),
            timeout=900,
        )
        problems = []
        volumes = [
            v["metadata"]["name"]
            for v in self.cluster.get("pv")["items"]
            if ((v["spec"].get("claimRef") or {}).get("namespace")) == namespace
        ]
        if volumes:
            problems.append(f"volumes left: {volumes}")
        for kind in ("clusterroles", "clusterrolebindings"):
            left = [
                i["metadata"]["name"]
                for i in self.cluster.get(kind)["items"]
                if i["metadata"]["name"].startswith(namespace + ":")
            ]
            if left:
                problems.append(f"{kind} left: {left}")
        state = self.kubectl(
            "exec", "-n", SYSTEM, f"deploy/{CONTROLLER}", "--",
            "find", "/var/lib/piceli-gitops", "-path", "*lc-wp-*",
            check_exit=None, timeout=60,
        ).stdout.split()  # fmt: skip
        if state:
            problems.append(f"controller state left: {state[:10]}")
        jobs = [
            j["metadata"]["name"]
            for j in self.cluster.get("jobs", "-n", SYSTEM)["items"]
            if (j["status"].get("failed") or 0) > 0
        ]
        if jobs:
            problems.append(f"failed build Jobs left: {jobs}")
        check(not problems, "; ".join(problems))

    def stage_10_retention(self) -> None:
        problems: list[str] = []
        before = self.run_step_soft("registry-status", problems)
        protected = self._protected()
        log(f"protected digests: {len(protected)}; seeds: {sorted(self.seeds)}")
        plan = self.run_step("retention-plan").json() or {}
        rows = plan.get("manifests") or plan.get("rows") or _rows(plan)
        deleted = {r["digest"] for r in rows if not r.get("kept")}
        for row in rows:
            log(f"  {'keep' if row.get('kept') else 'DELETE'} {row['repository']}@{row['digest'][:19]} "
                f"{','.join(row.get('reasons') or [])}")  # fmt: skip
        seeds = {ref.split("@", 1)[1] for ref in self.seeds.values()}
        missing = seeds - deleted
        wrong = deleted & set(protected)
        if missing:
            problems.append(
                f"seeded orphans/old copies not planned for deletion: {sorted(missing)}"
            )
        if wrong:
            problems.append(
                "the plan deletes digests in use or rollback targets: "
                + ", ".join(f"{protected[d]}@{d[:19]}" for d in sorted(wrong))
            )
        if wrong:
            # Never apply a plan that removes what runs.
            check(not problems, "; ".join(problems))
        self.run_step("retention-approve")
        self.run_step("registry-gc-dry-run")
        self.run_step("registry-gc")
        after = self.run_step_soft("registry-status", problems)
        log(f"registry status before: {_usage(before)}; after: {_usage(after)}")
        source = _used_source(after)
        if source not in {"du", "volume-stats"}:
            problems.append(f"registry status storage.used_source is {source!r}")
        self._pull_everything(protected, problems)
        check(not problems, "; ".join(problems))

    def run_step_soft(self, step: str, problems: list[str]) -> dict[str, Any]:
        try:
            return self.run_step(step).json() or {}
        except StageFailed as error:
            problems.append(f"{step}: {str(error).splitlines()[0]}")
            return {}

    def _protected(self) -> dict[str, str]:
        """Registry digests that must survive: live, configured, rollback targets."""
        found: dict[str, str] = {}

        def add(image: str, why: str) -> None:
            if image.startswith(REGISTRY_HOST + "/") and "@sha256:" in image:
                found.setdefault(image.split("@", 1)[1], why)

        pods = self.cluster.get("pods", "-A")["items"]
        for pod in pods:
            for c in [
                *(pod["spec"].get("containers") or []),
                *(pod["spec"].get("initContainers") or []),
            ]:
                add(
                    c["image"],
                    f"pod {pod['metadata']['namespace']}/{pod['metadata']['name']}",
                )
        for kind in ("statefulsets", "deployments"):
            for item in self.cluster.get(kind, "-A")["items"]:
                meta = item["metadata"]
                for c in item["spec"]["template"]["spec"].get("containers") or []:
                    add(c["image"], f"{kind[:-1]} {meta['namespace']}/{meta['name']}")
        # Rollback targets: the newest non-current ReplicaSet of each
        # Deployment and ControllerRevision of each StatefulSet.
        history: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
        for item in self.cluster.get("replicasets", "-A")["items"]:
            meta = item["metadata"]
            owner = next(iter(meta.get("ownerReferences") or []), {}).get("name", "")
            revision = int(
                (meta.get("annotations") or {}).get(
                    "deployment.kubernetes.io/revision", 0
                )
            )
            text = json.dumps(item["spec"]["template"])
            history.setdefault((meta["namespace"], owner), []).append(
                (revision, meta["name"], text)
            )
        for item in self.cluster.get("controllerrevisions", "-A")["items"]:
            meta = item["metadata"]
            owner = next(iter(meta.get("ownerReferences") or []), {}).get("name", "")
            text = json.dumps(item.get("data") or {})
            history.setdefault((meta["namespace"], "sts/" + owner), []).append(
                (int(item.get("revision") or 0), meta["name"], text)
            )
        for revisions in history.values():
            for _, name, text in sorted(revisions, reverse=True)[:2]:
                for image in re.findall(
                    re.escape(REGISTRY_HOST) + r"/[^\"]+@sha256:[0-9a-f]{64}", text
                ):
                    add(image, f"rollback target {name}")
        add(self.candidate_image, "controller image")
        add(self.candidate_builder, "builder image")
        return found

    def _pull_everything(self, protected: dict[str, str], problems: list[str]) -> None:
        """Every image a pod runs still pulls (imagePullPolicy: Always)."""
        live: dict[str, str] = {}
        for pod in self.cluster.get("pods", "-A")["items"]:
            for c in pod["spec"].get("containers") or []:
                if c["image"].startswith(REGISTRY_HOST + "/"):
                    live.setdefault(c["image"], pod["metadata"]["name"])
        live.setdefault(self.candidate_builder, "builder")
        self.kubectl("create", "namespace", "lc-pull", check_exit=None)
        try:
            pods = []
            for index, image in enumerate(sorted(live)):
                name = f"pull-{index}"
                pods.append((name, image))
                self.cluster.apply([{
                    "apiVersion": "v1", "kind": "Pod",
                    "metadata": {"name": name, "namespace": "lc-pull"},
                    "spec": {
                        "restartPolicy": "Never",
                        "nodeSelector": {"kubernetes.io/hostname": self.cluster.agents[1]},
                        "containers": [{"name": "main", "image": image, "imagePullPolicy": "Always",
                                        "command": ["sh", "-c", "exit 0"]}],
                    },
                }])  # fmt: skip
            for name, image in pods:

                def pulled(name: str = name, image: str = image) -> bool:
                    pod = self.cluster.get("pod", name, "-n", "lc-pull") or {}
                    for status in pod.get("status", {}).get("containerStatuses") or []:
                        if status.get("imageID"):
                            return True
                        waiting = (status.get("state") or {}).get("waiting") or {}
                        if waiting.get("reason") in {
                            "ErrImagePull",
                            "ImagePullBackOff",
                        }:
                            raise_pull(image, waiting)
                    return False

                try:
                    wait_for(f"pull of {image}", pulled, timeout=300, interval=3)
                    log(f"pulled {image.split('/', 1)[1][:80]}")
                except StageFailed as error:
                    problems.append(str(error))
        finally:
            self.kubectl(
                "delete", "namespace", "lc-pull", "--wait=false", check_exit=None
            )
        for kind in ("deployment", "statefulset"):
            self.kubectl("rollout", "restart", kind, "-n", "lc-main", check_exit=None)
        for key in self.generations("lc-main"):
            done = self.kubectl("rollout", "status", key, "-n", "lc-main", "--timeout=300s",
                                check_exit=None, timeout=360)  # fmt: skip
            if done.code != 0:
                problems.append(f"{key} did not roll out again after GC")

    # ------------------------------------------------------------ driver
    def tail(self, stage: str) -> None:
        """The evidence of a failing stage: controller log, status, failed Jobs."""
        print(f"----- stage {stage}: controller log (tail)", flush=True)
        print("\n".join(self.controller_log().splitlines()[-60:]), flush=True)
        status = self.status()
        print(f"----- stage {stage}: gitops status (envs)", flush=True)
        for name, env in (status.get("envs") or {}).items():
            brief = {
                k: env.get(k)
                for k in ("state", "reason", "namespace", "revision", "failure")
            }
            print(f"{name}: {json.dumps(brief)[:1200]}", flush=True)
        jobs = self.cluster.get("jobs", "-A") or {"items": []}
        for job in jobs["items"]:
            if (job["status"].get("failed") or 0) > 0:
                meta = job["metadata"]
                out = self.kubectl("logs", f"job/{meta['name']}", "-n", meta["namespace"],
                                   check_exit=None).stdout  # fmt: skip
                print(
                    f"----- failed Job {meta['namespace']}/{meta['name']} (log tail)",
                    flush=True,
                )
                print("\n".join(out.splitlines()[-30:]), flush=True)
        events = self.kubectl("get", "events", "-A", "--field-selector", "type=Warning",
                              "--sort-by=.lastTimestamp", check_exit=None).stdout  # fmt: skip
        print(f"----- stage {stage}: warning events (tail)", flush=True)
        print("\n".join(events.splitlines()[-15:]), flush=True)

    def run(self) -> int:
        started = time.monotonic()
        failed_critical = False
        try:
            self.setup()
            self.results.append(("setup", "passed", time.monotonic() - started, ""))
        except Exception as error:
            self.results.append(
                ("setup", "failed", time.monotonic() - started, first_line(error))
            )
            traceback.print_exc()
            return self.report(started)
        for stage in STAGE_ORDER:
            if stage not in self.args.stages:
                continue
            title = f"{stage}. {STAGE_TITLES[stage]}"
            if failed_critical:
                self.results.append(
                    (title, "not run", 0.0, "an earlier critical stage failed")
                )
                continue
            log(f"===== stage {title}")
            begin = time.monotonic()
            method = getattr(self, "stage_" + {
                "1": "1_upgrade", "2": "2_bootstrap", "3": "3_first_main", "4": "4_one_source",
                "5": "5_check_only", "6": "6_promote", "7": "7_branch", "8": "8_broken_build",
                "9": "9_teardown", "10": "10_retention",
            }[stage])  # fmt: skip
            try:
                method()
                self.results.append((title, "passed", time.monotonic() - begin, ""))
                log(f"===== stage {title}: passed ({time.monotonic() - begin:.0f}s)")
            except Exception as error:
                self.results.append(
                    (title, "FAILED", time.monotonic() - begin, first_line(error))
                )
                log(f"===== stage {title}: FAILED: {error}")
                if not isinstance(error, StageFailed):
                    traceback.print_exc()
                try:
                    self.tail(stage)
                except Exception:
                    traceback.print_exc()
                failed_critical = stage in CRITICAL
        return self.report(started)

    def report(self, started: float) -> int:
        print("\n===== lifecycle acceptance", flush=True)
        for title, state, seconds, detail in self.results:
            print(
                f"{state:8} {seconds:6.0f}s  {title}"
                + (f"  -- {detail}" if detail else "")
            )
        print(f"total {time.monotonic() - started:.0f}s", flush=True)
        return 0 if all(state in {"passed"} for _, state, _, _ in self.results) else 1

    def cleanup(self) -> None:
        for server in self.servers:
            try:
                server.stop()
            except Exception:
                pass
        self.cluster.delete()
        for tag in self.images_to_remove:
            self.proc.run(
                ["docker", "image", "rm", "-f", tag], check_exit=None, quiet=True
            )
        log("removed the cluster and the local images")


def raise_failed(name: str, record: dict[str, Any]) -> None:
    raise StageFailed(
        f"{name} failed: {record.get('reason')} "
        + json.dumps(record.get("failure") or {})[:1500]
    )


def raise_pull(image: str, waiting: dict[str, Any]) -> None:
    raise StageFailed(
        f"{image} does not pull: {waiting.get('reason')} {waiting.get('message', '')[:300]}"
    )


def first_line(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text.splitlines()[0][:300]


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    for value in plan.values():
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "digest" in value[0]
        ):
            return value
    return []


def _usage(status: dict[str, Any]) -> str:
    storage = (
        status.get("storage") or status.get("registry", {}).get("storage")
        if status
        else None
    )
    return json.dumps(storage)[:300] if storage else "n/a"


def _used_source(status: dict[str, Any]) -> Any:
    storage = (status or {}).get("storage") or {}
    return storage.get("used_source") if isinstance(storage, dict) else None


def parse_stages(text: str) -> set[str]:
    found: set[str] = set()
    for part in text.split(","):
        if "-" in part:
            low, high = part.split("-", 1)
            found |= {str(n) for n in range(int(low), int(high) + 1)}
        elif part.strip():
            found.add(part.strip())
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-commands", action="store_true",
                        help="print the note's command blocks and exit")  # fmt: skip
    parser.add_argument("--name", default="piceli-lc", help="k3d cluster name")
    parser.add_argument(
        "--previous", default=PREVIOUS, help="previous piceli version (PyPI)"
    )
    parser.add_argument("--previous-image", default=PREVIOUS_IMAGE)
    parser.add_argument("--previous-builder-image", default=PREVIOUS_BUILDER)
    parser.add_argument("--candidate", type=Path, default=ROOT,
                        help="checkout whose wheel is the candidate (default: this one)")  # fmt: skip
    parser.add_argument(
        "--candidate-version", help="install the candidate from PyPI instead"
    )
    parser.add_argument(
        "--candidate-image", help="published controller image (repo@sha256:...)"
    )
    parser.add_argument("--candidate-builder-image", help="published builder image")
    parser.add_argument("--images", choices=("derived", "dockerfile"), default="derived",
                        help="candidate images: previous images plus the wheel (default), "
                        "or images/Dockerfile")  # fmt: skip
    parser.add_argument(
        "--stages", default="1-10", help="e.g. 1-10 or 1,2,3 (setup always runs)"
    )
    args = parser.parse_args(argv)
    if args.print_commands:
        print(Commands.load(COMMANDS).render_note())
        return 0
    if os.environ.get("PICELI_K3S_LIFECYCLE") != "1":
        print("skipped: set PICELI_K3S_LIFECYCLE=1 (creates and deletes a k3d cluster)")
        return 0
    if bool(args.candidate_image) != bool(args.candidate_builder_image):
        parser.error("--candidate-image and --candidate-builder-image go together")
    args.candidate_public = bool(
        args.candidate_image
    ) and not args.candidate_image.startswith(REGISTRY_HOST)
    args.stages = parse_stages(args.stages) | {"1", "2", "3"}
    for tool in ("docker", "kubectl", "git", "uv"):
        if not shutil.which(tool):
            print(f"missing tool on PATH: {tool}")
            return 2
    if not (shutil.which("k3d") or shutil.which("nix")):
        print("missing tool on PATH: k3d (or nix to fetch it)")
        return 2
    with tempfile.TemporaryDirectory(prefix="piceli-lc-") as directory:
        lifecycle = Lifecycle(args, Path(directory))
        try:
            return lifecycle.run()
        finally:
            lifecycle.cleanup()


if __name__ == "__main__":
    sys.exit(main())
