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

From 0.14.6 (stage numbers say what they check, not when they run):

11. After 6: a check change (A) asks rc once and is approved; a failing
    check (B) asks and is not approved; the commit reverting B brings back
    A's plan, which rc applies without asking again.
12. After 7: the in-cluster UI (``piceli access ui``, as a user opens it)
    lists the branch environment's runs in its deployment history.
13. A stale UI forward: ``piceli access ui`` killed with SIGKILL leaves its
    ``kubectl``; the next ``access ui`` reaps it (piceli recorded it) or,
    with that record lost, names it as Piceli's own and ``piceli access stop
    --stale`` stops it.
14. Last: the UI's deployment history lists every run of main and rc that a
    stage made, newest first, with trigger, commits, plan hash, rolled
    components, approver and check outcomes (and, when ``ui/node_modules``
    has Playwright, the Deployment history page shows their runs); the
    registry's usage is measured by the controller (``du``); the history's
    ConfigMap has its schema. Stage 11 also reads rc's pending plan in the UI.
15. After 8: the broken build's run in the UI (``failure.log_tail``), then
    the branch is deleted with its kept Job.

The UI's launch token is never printed: the served command's output stays
in memory and is redacted before any failure prints it.

The cluster, the scratch directory, the local images and both scratch
virtual environments are removed whatever the outcome. A failing stage prints
the controller's log tail, the GitOps status and failed Job logs.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
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
    Served,
    StageFailed,
    UiClient,
    UiError,
    alive,
    check,
    kill_group,
    log,
    make_tls,
    port_open,
    processes,
    redact,
    run_bounded,
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
#: The previous release and its public images (amd64 + arm64; the index
#: digests of ghcr.io/pynenc/piceli-{controller,builder}:0.14.5).
PREVIOUS = "0.14.5"
PREVIOUS_IMAGE = (
    "ghcr.io/pynenc/piceli-controller"
    "@sha256:bb2a693ec923ce514ba1dde507a07a9938791581fe1b7d65643c4da269a60a44"
)
PREVIOUS_BUILDER = (
    "ghcr.io/pynenc/piceli-builder"
    "@sha256:7a35ab4d4d8ea3c09ce6e65a756ad4b338bbcc56f33d073aef931f8136e92e11"
)
#: The in-cluster UI's local port (``piceli access ui``).
UI_PORT = 8790
UI_URL = r"Piceli UI: (http://\S+)"
WORKLOADS = ("web", "store", "watcher", "cache", "reporter")
ALL_CHECKS = {
    "web-index",
    "web-binary",
    "store-ready",
    "watcher-reads-api",
    "reporter-runs",
    "site-config",
}
STAGE_ORDER = (
    "2", "3", "1", "4", "5", "6", "11", "7", "12", "8", "15", "9", "10", "13", "14",
)  # fmt: skip
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
    "11": "reverted check change asks once",
    "12": "UI history of the branch environment",
    "13": "stale access forward",
    "14": "UI history of main and rc",
    "15": "UI history of the broken build",
}
STAGE_METHODS = {
    "1": "1_upgrade", "2": "2_bootstrap", "3": "3_first_main", "4": "4_one_source",
    "5": "5_check_only", "6": "6_promote", "7": "7_branch", "8": "8_broken_build",
    "9": "9_teardown", "10": "10_retention", "11": "11_reverted_check",
    "12": "12_ui_branch", "13": "13_stale_access", "14": "14_ui_history",
    "15": "15_ui_broken",
}  # fmt: skip
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
            # piceli's local state (the access ui forward registry).
            "XDG_STATE_HOME": str(scratch / "state"),
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
        #: Per environment, the runs the stages made (oldest first), as the
        #: UI's deployment history must list them.
        self.expected: dict[str, list[dict[str, Any]]] = {}
        self.served: list[Served] = []
        self.broken_job = ""

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

    def expect(
        self,
        stage: str,
        env: str,
        record: dict[str, Any],
        *,
        rolled: set[str] | None,
        run: dict[str, Any] | None = None,
        plan_hash: str = "",
        plan: bool = True,
        action: str = "",
        trigger: str = "",
        via: str = "",
        log_tail: str = "",
        legacy: bool = False,
    ) -> None:
        """A run the UI's deployment history of ``env`` must list (stages 12, 14, 15).

        ``rolled``: the components it rolled (``None``: some, a first
        install); ``plan_hash``: the hash it ran (else any, unless ``plan``
        is false); ``action``/``trigger``/``via`` (``approved_by.via``) and
        ``log_tail`` (text in ``failure.log_tail``) when the stage knows them.
        ``legacy``: run by the previous release, whose run record has no
        sources, trigger or rolled components: only its place is checked.
        """
        revision = record.get("deployed_revision") or record.get("revision") or {}
        self.expected.setdefault(env, []).append(
            {
                "stage": stage,
                "commits": {k: v for k, v in revision.items() if isinstance(v, str)},
                "plan_hash": plan_hash,
                "plan": plan,
                "rolled": sorted(rolled) if rolled is not None else None,
                "checks": sorted(
                    str(r["name"])
                    for r in (run or {}).get("checks", {}).get("results", [])
                ),
                "action": action,
                "trigger": trigger,
                "via": via,
                "log_tail": log_tail,
                "legacy": legacy,
            }
        )

    # ------------------------------------------------------------ the UI
    def serve(self, step: str) -> Served:
        """Start a ``serve`` step of the commands file as written (until stopped)."""
        argv = self.commands.argv(step, {**self.values, "piceli": self.piceli})
        served = Served(argv, env=self.env, cwd=self.infra)
        self.served.append(served)
        return served

    def bounded(self, step: str, timeout: float = 120) -> Result:
        """Run a step that might start a forward instead of exiting (own session)."""
        argv = self.commands.argv(step, {**self.values, "piceli": self.piceli})
        return run_bounded(argv, env=self.env, cwd=self.infra, timeout=timeout)

    def _ui_port_free(self) -> None:
        check(
            not port_open(UI_PORT),
            f"127.0.0.1:{UI_PORT} is in use on this machine before the run opens "
            "the UI (not by this run): free it and run again",
        )

    @contextlib.contextmanager
    def ui_session(self) -> Iterator[tuple[UiClient, str]]:
        """``piceli access ui`` as a user runs it; a client with the UI's session.

        Yields the client and the launch URL (in memory only, never logged).
        Stops the command (Ctrl-C, then SIGKILL) and any forward it left.
        """
        self._ui_port_free()
        served = self.serve("access-ui")
        children: list[tuple[int, str]] = []
        try:
            url = served.wait_line(UI_URL, timeout=180).group(1)
            wait_for(
                f"127.0.0.1:{UI_PORT} forwarded",
                lambda: port_open(UI_PORT),
                timeout=120,
                interval=2,
            )
            children = served.children()
            client = UiClient(url)
            client.open()
            yield client, url
        finally:
            children = children or served.children()
            served.stop()
            for pid, command in children:
                if "port-forward" in command and alive(pid):
                    kill_group(pid)

    def check_history(
        self, client: UiClient, envs: list[str], problems: list[str]
    ) -> None:
        """Each environment's runs in the UI's deployment history, newest first.

        Through the endpoints the Deployment history page calls: the
        ``composition_history`` capability, ``/composition/history`` (every
        environment) and ``/composition/environments/<env>/history``.
        """
        caps = client.get("/api/v1/capabilities")
        if isinstance(caps, UiError):
            problems.append(f"GET /api/v1/capabilities: {caps.status} {caps.code}")
            return
        capability = ((caps or {}).get("actions") or {}).get(
            "composition_history"
        ) or {}
        if not capability.get("allowed"):
            problems.append(
                f"composition_history not allowed ({capability.get('reason')!r})"
            )
            return
        whole = client.get("/api/v1/composition/history")
        if isinstance(whole, UiError):
            problems.append(
                f"GET /api/v1/composition/history: {whole.status} {whole.code}"
            )
            return
        listed = {
            str(e.get("name")): len(e.get("runs") or [])
            for e in whole.get("environments") or []
        }
        log(f"composition history: configured {whole.get('configured')}, available "
            f"{whole.get('available')}, truncated {whole.get('truncated')}, runs {listed}")  # fmt: skip
        if not (whole.get("configured") and whole.get("available")):
            problems.append(
                f"history configured {whole.get('configured')!r}, "
                f"available {whole.get('available')!r}"
            )
        for env in envs:
            if env not in listed:
                problems.append(f"{env}: not in /composition/history")
            path = f"/api/v1/composition/environments/{env}/history"
            body = client.get(path)
            if isinstance(body, UiError):
                problems.append(f"{env}: GET {path}: {body.status} {body.code}")
                continue
            runs = next(
                (
                    e.get("runs") or []
                    for e in body.get("environments") or []
                    if e.get("name") == env
                ),
                None,
            )
            if runs is None:
                problems.append(f"{env}: {path} has no runs list")
                continue
            log(f"{env}: {len(runs)} run(s); newest: " + " | ".join(
                f"{r.get('action')}/{r.get('state')} {r.get('trigger')} {r.get('started_at')} "
                f"rolled {r.get('rolled')}" for r in runs[:8]
            ))  # fmt: skip
            problems.extend(
                f"{env}: {item}"
                for item in _history_problems(runs, self.expected.get(env) or [])
            )

    def _pending_plan(self, client: UiClient, env: str, plan_hash: str) -> list[str]:
        """The UI shows the plan waiting for approval before it is approved."""
        path = f"/api/v1/composition/environments/{env}/actions"
        body = client.get(path)
        if isinstance(body, UiError):
            return [f"GET {path}: {body.status} {body.code}"]
        pending = (body or {}).get("pending_plan") or {}
        log(f"{env} pending plan in the UI: {str(pending.get('plan_hash'))[:23]}, "
            f"changes_total {pending.get('changes_total')}, counts {pending.get('counts')}")  # fmt: skip
        problems = []
        if pending.get("plan_hash") != plan_hash:
            problems.append(
                f"{env} pending_plan.plan_hash {str(pending.get('plan_hash'))[:23]!r}, "
                f"not {plan_hash[:23]}"
            )
        for key in ("combined_hash", "counts", "changes"):
            if key not in pending:
                problems.append(f"{env} pending_plan has no {key}")
        if not isinstance(pending.get("changes_total"), int):
            problems.append(f"{env} pending_plan.changes_total is not a number")
        return problems

    def _registry_usage(self, client: UiClient) -> list[str]:
        """The registry's claim usage in the UI, as measured by the controller."""
        last: dict[str, Any] = {}

        def measured() -> bool:
            nonlocal last
            body = client.get("/api/v1/cluster/status")
            if isinstance(body, UiError):
                last = {"error": f"{body.status} {body.code}"}
                return False
            last = ((body or {}).get("registry") or {}).get("storage") or {}
            return (
                isinstance(last.get("used_bytes"), int)
                and last.get("used_source") == "du"
                and last.get("measured_by") == "controller"
            )

        try:
            wait_for(
                "the registry's usage in the UI", measured, timeout=600, interval=15
            )
        except StageFailed:
            return [f"cluster status registry.storage {json.dumps(last)[:300]}"]
        log(f"registry in the UI: {json.dumps(last)[:300]}")
        return []

    def _history_configmap(self) -> list[str]:
        found = self.cluster.get("configmap", "piceli-gitops-history", "-n", SYSTEM)
        if not found:
            return ["no ConfigMap piceli-system/piceli-gitops-history"]
        try:
            document = json.loads((found.get("data") or {}).get("history.json") or "")
        except ValueError:
            return ["piceli-gitops-history history.json is not JSON"]
        schema = document.get("schema") if isinstance(document, dict) else None
        if schema != "piceli.gitops-history.v1":
            return [f"piceli-gitops-history schema {schema!r}"]
        return []

    def _browser_history(self, url: str, problems: list[str]) -> None:
        """Optional: the Deployment history page shows rows (Playwright from ``ui/``)."""
        ui = self.args.playwright_ui
        node = shutil.which("node")
        if not (node and (ui / "node_modules" / "@playwright" / "test").is_dir()):
            log(f"browser check skipped: no node or no Playwright in {ui}/node_modules")
            return
        envs = ",".join(env for env in ("main", "rc") if self.expected.get(env))
        script = self.scratch / "history.mjs"
        script.write_text(BROWSER_SCRIPT)
        try:
            done = subprocess.run(
                [node, str(script)], capture_output=True, text=True, timeout=180,
                env={**self.env, "PW_UI_DIR": str(ui), "PW_LAUNCH_URL": url, "PW_ENVS": envs},
                check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            problems.append("browser: the Deployment history page did not load in 180s")
            return
        body = Result([], done.returncode, done.stdout, done.stderr).json() or {}
        log(f"browser: {json.dumps(body)[:300]}")
        if done.returncode != 0 or not body:
            problems.append(
                f"browser: exit {done.returncode}: {redact(done.stderr[-600:])}"
            )
        else:
            for env, rows in (body.get("rows") or {}).items():
                if not rows:
                    problems.append(
                        f"browser: Deployment history shows no run of {env}"
                    )
            if body.get("unavailable"):
                problems.append("browser: Deployment history unavailable")

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
        self.expect(
            "3", "main", record, rolled=None, run=run,
            plan_hash=self.values.get("main_hash", ""),
            legacy=self.piceli == self.prev_cli,
        )  # fmt: skip

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
        if _version(self.args.previous) >= (0, 14, 5):
            # The previous release recorded its check set: the upgrade has
            # nothing new to verify ("release already verified").
            if record.get("health") not in {None, "healthy"}:
                problems.append(
                    f"main health {record.get('health')!r} after the upgrade"
                )
        elif (
            verification.get("state") != "verified"
            or verification.get("trigger") != "unverified"
        ):
            problems.append(
                "main was not verified once after the upgrade "
                f"(verification {json.dumps(verification)[:200]})"
            )
        if verification.get("rolled"):
            problems.append(f"the verification rolled {verification.get('rolled')}")
        self.expect("1", "main", record, rolled=set())
        # Re-running the bootstrap commands with the new release is idempotent.
        done = self.run_group("bootstrap")
        for step in ("cluster-init-plan", "gitops-enable-plan"):
            body = done[step].json() or {}
            if not (done[step].code == 0 and body.get("state") == "unchanged"):
                changes = [
                    line.strip()
                    for line in done[step].stderr.splitlines()
                    if re.search(
                        r"\b(create|apply|delete|label|update|replace)\b", line
                    )
                    and "no-op" not in line
                ]
                problems.append(
                    f"{step} after the upgrade: exit {done[step].code}, "
                    f"{body.get('state')} ({'; '.join(changes[:8]) or 'no change lines'})"
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
        self.expect("4", "main", record, rolled={"store"}, run=run)
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
            self.expect(
                "5", "main", record, rolled=set(), run=run,
                action="verified", trigger="checks-changed",
            )  # fmt: skip
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
        self.expect(
            "6", "rc", record, rolled=None, run=run,
            plan_hash=self.values["rc_hash"], via="cli",
        )  # fmt: skip
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
        self.expect(
            "7",
            "wp-feature",
            record,
            rolled=None,
            run=run,
        )
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
        self.expect(
            "8", "wp-broken", record, rolled=set(), plan=False, log_tail="version.c"
        )
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
        self.broken_job = str(job)
        if "15" in self.args.stages:
            return  # stage 15 reads its history first, then deletes it
        self._delete_broken()

    def _delete_broken(self) -> None:
        """Delete the broken branch: its kept build Job goes with it."""
        job = self.broken_job
        self.broken_job = ""
        self.repos.delete_branch("web", "wp-broken")
        wait_for(
            "the kept build Job removed with its branch",
            lambda: (
                self.cluster.get("job", job, "-n", SYSTEM) is None
                and "wp-broken" not in (self.status().get("envs") or {})
            ),
            timeout=600,
        )

    def stage_15_ui_broken(self) -> None:
        """The failed build's run in the UI (failure.log_tail), then delete it."""
        check(self.broken_job, "no broken build kept (stage 8)")
        problems: list[str] = []
        try:
            with self.ui_session() as (client, _):
                self.check_history(client, ["wp-broken"], problems)
        finally:
            try:
                self._delete_broken()
            except StageFailed as error:
                problems.append(str(error))
        check(not problems, "; ".join(problems))

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

    def stage_11_reverted_check(self) -> None:
        """A check change asks rc once; reverting a later one asks nothing.

        A: a check-only change; rc asks, it is approved and deployed. B: a
        failing check, not approved. The revert of B brings back A's plan:
        rc applies it again without asking (``deployed_plan_hash`` is A's).
        """
        check(
            self.cluster.get("namespace", "lc-rc") is not None,
            "rc is not deployed (stage 6 must run first)",
        )
        before = {ns: self.generations(ns) for ns in ("lc-main", "lc-rc")}
        source = self.repos.read("infra", "lifecycle_app.py")
        if "EXTRA_CHECKS: list = []" in source:
            source = source.replace("EXTRA_CHECKS: list = []", EXTRA_CHECK)
        check('["echo", "cache-answers"]' in source, "no cache-answers check to change")
        change_a = source.replace(
            '["echo", "cache-answers"]', '["echo", "cache-answers", "again"]'
        )
        self.log_since = self.mark()
        since = self.since_epoch
        sha_a = self.repos.commit(
            "infra", "checks: the cache answers again", {"lifecycle_app.py": change_a}
        )
        hash_a = self._rc_settle(sha_a, "check change A", approve=True)
        check(hash_a, "rc did not ask for approval of check change A")
        record = self.wait_env(
            "rc", self.deployed("rc", {"infra": sha_a}), timeout=1200, what="change A"
        )
        run = self.checked_run("/rc/", since)
        self.expect("11", "rc", record, rolled=set(), run=run, plan_hash=hash_a)
        main = self.wait_env(
            "main",
            self.deployed("main", {"infra": sha_a}),
            timeout=900,
            what="change A",
        )
        self.expect(
            "11", "main", main, rolled=set(), run=self.checked_run("/main/", since)
        )
        # B: a failing check; rc asks, nobody approves.
        change_b = change_a.replace(
            'output_contains="cache-answers"', 'output_contains="never-printed"'
        )
        check(change_b != change_a, "no output_contains of cache-answers to break")
        sha_b = self.repos.commit(
            "infra", "checks: a failing check", {"lifecycle_app.py": change_b}
        )
        hash_b = self._rc_settle(sha_b, "check change B", approve=False)
        log(f"rc asks for B: {hash_b[:23] or 'no'}; not approved")
        ui_problems: list[str] = []
        if hash_b and {"12", "14", "15"} & self.args.stages:
            # The UI shows the plan before anyone approves it.
            with self.ui_session() as (client, _):
                ui_problems = self._pending_plan(client, "rc", hash_b)
        # The revert of B: A's plan again, already approved and running.
        self.log_since = self.mark()
        since = self.since_epoch
        reverted = self.repos.revert("infra")
        problems: list[str] = [f"UI: {p}" for p in ui_problems]
        again = self._rc_settle(reverted, "the revert of B", approve=True)
        if again:
            same = "A's plan" if again == hash_a else "not A's plan"
            problems.append(
                f"rc asked again after the revert (plan {again[:23]}, {same})"
            )
        record = self.wait_env(
            "rc", self.deployed("rc", {"infra": reverted}), timeout=1200, what="revert"
        )
        log(f"rc after the revert: plan_hash {record.get('plan_hash')!r}, "
            f"deployed_plan_hash {str(record.get('deployed_plan_hash'))[:23]!r}, "
            f"last_action {record.get('last_action')!r}, health {record.get('health')!r}")  # fmt: skip
        if record.get("plan_hash") is not None:
            problems.append(
                f"rc plan_hash {str(record.get('plan_hash'))[:23]!r}, not null"
            )
        if record.get("deployed_plan_hash") != hash_a:
            problems.append(
                f"rc deployed_plan_hash {str(record.get('deployed_plan_hash'))[:23]!r}, "
                f"not A's {hash_a[:23]}"
            )
        line = f"rc: plan {hash_a} already approved and running; applying it without asking again"
        if line not in self.controller_log(self.log_since):
            problems.append("no 'rc: plan <A> already approved and running' log line")
        try:
            run = self.checked_run("/rc/", since)
        except StageFailed as error:
            run = None
            problems.append(str(error))
        self.expect("11", "rc", record, rolled=set(), run=run, plan_hash=hash_a)

        def main_back(record: dict[str, Any]) -> bool:
            revision = record.get("deployed_revision") or record.get("revision") or {}
            return (
                record.get("state") == "deployed" and revision.get("infra") == reverted
            )

        main = self.wait_env("main", main_back, timeout=900, what="revert")
        try:
            main_run = self.checked_run("/main/", since)
        except StageFailed:
            main_run = None
        self.expect("11", "main", main, rolled=set(), run=main_run)
        after = {ns: self.generations(ns) for ns in ("lc-main", "lc-rc")}
        if after != before:
            problems.append(f"check changes rolled workloads: {before} -> {after}")
        check(not problems, "; ".join(problems))

    def _rc_settle(self, sha: str, what: str, *, approve: bool) -> str:
        """Wait until rc deploys ``infra@sha`` or asks; the hash asked ("" if none).

        With ``approve`` the asked plan is approved (the note's approve-rc).
        """

        def settled(record: dict[str, Any]) -> bool:
            revision = record.get("revision") or {}
            if (
                record.get("state") == "approval-required"
                and record.get("plan_hash")
                and revision.get("infra", sha) == sha
            ):
                return True
            return self.deployed("rc", {"infra": sha})(record)

        record = self.wait_env("rc", settled, timeout=900, what=what)
        if record.get("state") != "approval-required":
            return ""
        asked = str(record["plan_hash"])
        log(f"rc asks for approval after {what} ({record.get('reason')})")
        if approve:
            self.values["rc_hash"] = asked
            self.run_group("approve-rc")
        return asked

    def stage_12_ui_branch(self) -> None:
        check(self.expected.get("wp-feature"), "no branch run recorded (stage 7)")
        problems: list[str] = []
        with self.ui_session() as (client, _):
            self.check_history(client, ["wp-feature"], problems)
        check(not problems, "; ".join(problems))

    def stage_13_stale_access(self) -> None:
        """A UI forward whose piceli was killed never blocks the next one.

        Two rounds: (a) the killed ``access ui`` recorded its kubectl in
        piceli's state (``$XDG_STATE_HOME``): the next ``access ui`` reaps
        it and starts; (b) that record is lost (another state directory):
        the next ``access ui`` refuses with ``access-port-conflict``, names
        the holder as Piceli's own forward and hints at ``access stop
        --stale``, which stops it. Either outcome passes each round; a
        stale forward called "(not piceli)" or left holding the port fails.
        """
        problems: list[str] = []
        for label, state in (
            ("recorded", None),
            ("record lost", str(self.scratch / "state-elsewhere")),
        ):
            problems.extend(f"{label}: {p}" for p in self._stale_round(label, state))
        check(not problems, "; ".join(problems))

    def _stale_round(self, label: str, state: str | None) -> list[str]:
        self._ui_port_free()
        argv = self.commands.argv("access-ui", {**self.values, "piceli": self.piceli})
        env = {**self.env, **({"XDG_STATE_HOME": state} if state else {})}
        first = Served(argv, env=env, cwd=self.infra)
        self.served.append(first)
        kubectl: list[int] = []
        second: Served | None = None
        problems: list[str] = []
        try:
            first.wait_line(UI_URL, timeout=180)
            wait_for(f"127.0.0.1:{UI_PORT} forwarded", lambda: port_open(UI_PORT),
                     timeout=120, interval=2)  # fmt: skip
            kubectl = [pid for pid, cmd in first.children() if "port-forward" in cmd]
            check(
                kubectl,
                f"no kubectl port-forward child of piceli access ui ({first.pid})",
            )
            os.kill(first.pid, signal.SIGKILL)
            first.process.wait(timeout=10)
            time.sleep(2)
            check(
                any(alive(pid) for pid in kubectl) and port_open(UI_PORT),
                "kubectl exited with its piceli: nothing stale to detect",
            )
            log(f"{label}: piceli access ui killed; its kubectl (pid {kubectl}) holds "
                f"127.0.0.1:{UI_PORT}")  # fmt: skip
            second = self.serve("access-ui")
            started = wait_for(
                "the second access ui to start or refuse",
                lambda: _started(second) or second.process.poll() is not None,
                timeout=180, interval=1,
            )  # fmt: skip
            del started
            if second.process.poll() is None:
                # Started: the stale kubectl must be gone (reaped).
                if any(alive(pid) for pid in kubectl):
                    problems.append(
                        f"a second access ui started beside the stale kubectl {kubectl}"
                    )
                else:
                    log(
                        f"{label}: the second access ui reaped the stale forward and started"
                    )
                return problems
            second.process.wait(timeout=10)
            time.sleep(0.5)
            body = Result([], 0, "\n".join(second.out), "").json() or {}
            said = redact("\n".join(second.err))[-800:]
            code = second.process.returncode
            log(f"{label}: second access ui: exit {code}, reason {body.get('reason')!r}, "
                f"conflicts {json.dumps(body.get('conflicts'))[:400]}")  # fmt: skip
            holders = [c.get("holder") for c in body.get("conflicts") or []]
            if code != 2 or body.get("reason") != "access-port-conflict":
                problems.append(
                    f"second access ui: exit {code}, {body.get('reason')!r}: {said[-300:]}"
                )
            if "piceli-forward" not in holders:
                problems.append(
                    f"the conflict does not name Piceli's own forward (holders {holders})"
                )
            stop_line = self.commands.line(
                "access-stop-stale", {**self.values, "piceli": "piceli"}
            )
            if stop_line not in said:
                problems.append(f"no '{stop_line}' hint in the refusal")
            if "(not piceli)" in said:
                problems.append("the refusal calls Piceli's forward '(not piceli)'")
            stopped = self.bounded("access-stop-stale", timeout=60)
            log(f"{label}: access stop --stale: exit {stopped.code}: "
                f"{redact(stopped.stdout.strip())[-400:]}")  # fmt: skip
            gone = [
                (i.get("port"), i.get("holder"))
                for i in (stopped.json() or {}).get("stopped") or []
            ]
            if stopped.code != 0:
                problems.append(
                    f"access stop --stale: exit {stopped.code}: "
                    f"{redact(stopped.stderr.strip())[-300:]}"
                )
            elif (UI_PORT, "piceli-forward") not in gone:
                problems.append(
                    f"access stop --stale stopped {gone}, not the UI forward"
                )
            try:
                wait_for(
                    "the stale forward stopped",
                    lambda: not port_open(UI_PORT) and not any(alive(p) for p in kubectl),
                    timeout=20, interval=1,
                )  # fmt: skip
            except StageFailed:
                problems.append(
                    f"the stale kubectl (pid {kubectl}) still holds {UI_PORT}"
                )
            return problems
        finally:
            for served in (second, first):
                if served is not None:
                    children = served.children()
                    served.stop()
                    kubectl += [p for p, c in children if "port-forward" in c]
            for pid in kubectl:
                if any(p == pid and "port-forward" in c for p, _, c in processes()):
                    kill_group(pid)
            wait_for(f"127.0.0.1:{UI_PORT} free", lambda: not port_open(UI_PORT),
                     timeout=30, interval=1)  # fmt: skip

    def stage_14_ui_history(self) -> None:
        envs = [env for env in ("main", "rc") if self.expected.get(env)]
        check(envs, "no main or rc run recorded")
        problems: list[str] = []
        with self.ui_session() as (client, url):
            self.check_history(client, envs, problems)
            self._browser_history(url, problems)
            problems.extend(self._registry_usage(client))
        problems.extend(self._history_configmap())
        check(not problems, "; ".join(problems))

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
            method = getattr(self, "stage_" + STAGE_METHODS[stage])
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
        for served in self.served:
            served.stop(timeout=5)
        # Any forward of this run's kubeconfig left behind (a killed piceli).
        mine = str(self.cluster.kubeconfig)
        for pid, _, command in processes():
            if "port-forward" in command and mine in command:
                kill_group(pid)
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


BROWSER_SCRIPT = """\
import { createRequire } from 'node:module';
const require = createRequire(process.env.PW_UI_DIR + '/package.json');
const { chromium } = require('@playwright/test');
const launch = process.env.PW_LAUNCH_URL;
const origin = new URL(launch).origin;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(launch);
  const rows = {};
  let unavailable = 0;
  for (const env of (process.env.PW_ENVS || '').split(',').filter(Boolean)) {
    await page.goto(`${origin}/delivery?historySource=environments&environment=${env}`);
    await page.waitForLoadState('networkidle').catch(() => {});
    const runs = page.locator('ol.history-runs details.history-run');
    await runs.first().waitFor({ timeout: 30000 }).catch(() => {});
    rows[env] = await runs.count();
    unavailable += await page.getByText(/Deployment history unavailable/).count();
  }
  console.log(JSON.stringify({ rows, unavailable: unavailable > 0 }));
} finally {
  await browser.close();
}
"""


def _started(served: Served) -> bool:
    """Whether a served ``access ui`` printed its URL (the line is not kept)."""
    return any(re.search(UI_URL, line) for line in list(served.out))


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _find(value: Any, keys: tuple[str, ...]) -> Any:
    """The first non-empty value under any of ``keys`` (in order), at any depth."""
    for key in keys:
        stack = [value]
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                if item.get(key) not in (None, "", [], {}):
                    return item[key]
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return None


def _names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value}
    if isinstance(value, list):
        return {
            str(v.get("name") or v.get("component") or v)
            if isinstance(v, dict)
            else str(v)
            for v in value
        }
    return set()


def _sources(run: dict[str, Any]) -> dict[str, str]:
    return {
        str(s.get("name")): str(s.get("commit"))
        for s in run.get("sources") or []
        if isinstance(s, dict)
    }


def _history_problems(runs: list[Any], expected: list[dict[str, Any]]) -> list[str]:
    """What the history ``runs`` (newest first) lack of the ``expected`` runs.

    A stage's run is the newest whose ``sources[].commit`` are its revision;
    it must carry a trigger, ``started_at``, the plan hash (the one it ran,
    when the stage knows it), the rolled components, the checks that ran
    (by name) and what the stage knows of action, trigger, approver and
    failure. Stages appear newest first.
    """
    problems: list[str] = []
    stamps = [str(r.get("started_at") or "") for r in runs]
    if any(a and b and a < b for a, b in itertools.pairwise(stamps)):
        problems.append("runs are not newest first")
    matched: list[tuple[int, str]] = []
    for want in expected:
        label = f"stage {want['stage']}"
        commits = {k: v for k, v in want["commits"].items() if v}
        index = next(
            (
                i
                for i, run in enumerate(runs)
                if commits
                and all(_sources(run).get(k) == v for k, v in commits.items())
            ),
            None,
        )
        if index is None and want["legacy"]:
            # The previous release's run (a run record only): the oldest
            # deployed one; the order check places it below later stages.
            index = next(
                (
                    i
                    for i in range(len(runs) - 1, -1, -1)
                    if runs[i].get("recorded_by") == "run"
                    and runs[i].get("state") == "deployed"
                ),
                None,
            )
            if index is not None:
                log(f"{label}: the previous release's run {runs[index].get('run_id')} "
                    f"(run record only: sources {runs[index].get('sources')}, "
                    f"trigger {runs[index].get('trigger')!r})")  # fmt: skip
                matched.append((index, label))
                continue
        if index is None:
            short = {k: v[:12] for k, v in commits.items()}
            problems.append(f"no run of {label} (commits {short})")
            continue
        matched.append((index, label))
        run = runs[index]
        if not run.get("trigger"):
            problems.append(f"{label}: no trigger")
        if not run.get("started_at"):
            problems.append(f"{label}: no started_at")
        if want["action"] and run.get("action") != want["action"]:
            problems.append(
                f"{label}: action {run.get('action')!r}, not {want['action']!r}"
            )
        # The verification's trigger (checks-changed, unverified) or the run's.
        triggers = {run.get("trigger"), (run.get("verification") or {}).get("trigger")}
        if want["trigger"] and want["trigger"] not in triggers:
            problems.append(
                f"{label}: trigger {sorted(map(str, triggers))}, not {want['trigger']!r}"
            )
        if want["plan_hash"]:
            if run.get("plan_hash") != want["plan_hash"]:
                problems.append(
                    f"{label}: plan_hash {str(run.get('plan_hash'))[:23]!r}, "
                    f"not {want['plan_hash'][:23]}"
                )
        elif want["plan"] and not run.get("plan_hash"):
            problems.append(f"{label}: no plan_hash")
        rolled = set(_names(run.get("rolled") or []))
        if want["rolled"] is None:
            if want["plan"] and not rolled:
                problems.append(f"{label}: rolled nothing (a first install)")
        elif rolled != set(want["rolled"]):
            problems.append(f"{label}: rolled {sorted(rolled)}, not {want['rolled']}")
        results = {
            str(r.get("name"))
            for r in ((run.get("checks") or {}).get("results") or [])
            if isinstance(r, dict)
        }
        missing = sorted(set(want["checks"]) - results)
        if missing:
            problems.append(f"{label}: checks not listed: {missing}")
        via = (
            ((run.get("approved_by") or {}).get("via"))
            if isinstance(run.get("approved_by"), dict)
            else None
        )
        if want["via"] and via != want["via"]:
            problems.append(f"{label}: approved_by.via {via!r}, not {want['via']!r}")
        tail = str((run.get("failure") or {}).get("log_tail") or "")
        if want["log_tail"] and want["log_tail"] not in tail:
            problems.append(f"{label}: failure.log_tail lacks {want['log_tail']!r}")
    # ``matched`` is in stage order (oldest first): each later stage's run
    # must sit above (at a smaller index than) the earlier one's.
    for (earlier_at, earlier), (later_at, later) in itertools.pairwise(matched):
        if not later_at < earlier_at:
            problems.append(f"{later} is not listed above {earlier}")
    return problems


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
        "--stages", default="1-15", help="e.g. 1-15 or 1,2,3 (setup always runs)"
    )
    parser.add_argument("--playwright-ui", type=Path, default=None,
                        help="ui/ directory with node_modules for the optional browser "
                        "check (default: the candidate's ui/; skipped without Playwright)")  # fmt: skip
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
    args.playwright_ui = (args.playwright_ui or args.candidate / "ui").resolve()
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
