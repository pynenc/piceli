"""Stages 20-22 of the k3s lifecycle acceptance: checks, stops, one access command.

Mixed into :class:`lifecycle.Lifecycle` (it uses its cluster, commands,
``status``/``wait_env`` and ``ui_session``). From 0.14.7, after stage 6 (rc
deployed by the candidate's controller):

20. After a rollout the status shows the checks: each environment the
    candidate deployed has ``checks`` (passed N/N, every check's name, when,
    the trigger), and the in-cluster UI's environment view shows the same.
21. Stop and start a named environment: ``piceli env stop rc`` scales rc's
    workloads to zero and keeps its volumes; a push to a source rc follows is
    deployed to main but not even planned for rc; ``piceli env start rc``
    scales it back (and deploys the moved revision, approved if it asks);
    the UI offers Start while it is stopped.
22. ``piceli access main --cluster infra.py:cluster --ui``: main's declared
    forward on its port, the UI's launch URL, in one command; killing piceli
    with SIGKILL (piceli only) leaves no ``kubectl`` and frees every port.

The launch token is never printed (the served output stays in memory).
"""

from __future__ import annotations

import json
import os
import signal
import time
from typing import Any

from lifecycle_reach import ACCESS_PORT, WEB_PAGE, _fetch
from lifecycle_support import (
    UiClient,
    UiError,
    alive,
    check,
    kill_group,
    log,
    port_open,
    processes,
    wait_for,
)

#: The in-cluster UI's local port (``piceli access ui``).
UI_PORT = 8790
#: Checks main and rc run (``examples/lifecycle``; rc runs the full stack).
FULL_CHECKS = {
    "web-index",
    "web-binary",
    "store-ready",
    "watcher-reads-api",
    "reporter-runs",
    "site-config",
}
STOPPED_ENV = ("rc", "lc-rc")


def _replicas(cluster: Any, namespace: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for kind in ("deployments", "statefulsets"):
        body = cluster.get(kind, "-n", namespace) or {"items": []}
        for item in body["items"]:
            replicas = (item.get("spec") or {}).get("replicas")
            found[f"{kind[:-1]}/{item['metadata']['name']}"] = (
                1 if replicas is None else int(replicas)
            )
    return found


def _claims(cluster: Any, namespace: str) -> set[str]:
    body = cluster.get("persistentvolumeclaims", "-n", namespace) or {"items": []}
    return {item["metadata"]["name"] for item in body["items"]}


def _running_pods(cluster: Any, namespace: str) -> list[str]:
    body = cluster.get("pods", "-n", namespace) or {"items": []}
    return [
        pod["metadata"]["name"]
        for pod in body["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and (pod.get("status") or {}).get("phase") in {"Running", "Pending"}
    ]


def _json_lines(lines: list[str]) -> list[dict[str, Any]]:
    found = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


class OpsStages:
    """Stages 20-22 (see the module docstring); mixed into ``Lifecycle``."""

    # ------------------------------------------------------------ 20
    def stage_20_checks_status(self) -> None:
        problems: list[str] = []
        status = self.status()  # type: ignore[attr-defined]
        envs = status.get("envs") or {}
        candidates = ["rc"] + (
            ["main"] if {"4", "5"} & self.args.stages else []  # type: ignore[attr-defined]
        )
        for name in candidates:
            record = envs.get(name) or {}
            checks = record.get("checks") or {}
            names = {item.get("name") for item in checks.get("results") or []}
            log(f"{name}: verification {record.get('verification')!r}; checks "
                f"{checks.get('state')} {checks.get('passed')}/{checks.get('total')} "
                f"at {checks.get('at')} ({checks.get('trigger')})")  # fmt: skip
            if checks.get("state") != "passed":
                problems.append(f"{name}: checks {checks.get('state')!r}")
            if not checks.get("total") or checks.get("passed") != checks.get("total"):
                problems.append(f"{name}: {checks.get('passed')}/{checks.get('total')}")
            if not names >= FULL_CHECKS:
                problems.append(f"{name}: checks {sorted(n for n in names if n)}")
            if not checks.get("at") or not checks.get("trigger"):
                problems.append(f"{name}: no time or trigger: {checks}")
        with self.ui_session() as (client, _url):  # type: ignore[attr-defined]
            for name in candidates:
                body = client.get(f"/api/v1/composition/environments/{name}")
                if isinstance(body, UiError):
                    problems.append(f"UI {name}: {body.status} {body.code}")
                    continue
                seen = ((body or {}).get("environment") or {}).get("checks") or {}
                shown = {item.get("name") for item in seen.get("results") or []}
                if seen.get("state") != "passed" or not shown >= FULL_CHECKS:
                    problems.append(
                        f"UI {name}: checks {seen.get('state')!r} {sorted(n for n in shown if n)}"
                    )
                if seen.get("passed") != seen.get("total") or not seen.get("at"):
                    problems.append(
                        f"UI {name}: {seen.get('passed')}/{seen.get('total')}"
                    )
                if "detail" in json.dumps(seen):
                    problems.append(f"UI {name}: a check's detail line is shown")
        check(not problems, "; ".join(problems))

    # ------------------------------------------------------------ 21
    def stage_21_stop_start(self) -> None:
        env, namespace = STOPPED_ENV
        cluster = self.cluster  # type: ignore[attr-defined]
        before = _replicas(cluster, namespace)
        claims = _claims(cluster, namespace)
        check(before and any(before.values()), f"{env} runs nothing: {before}")
        log(f"{env}: replicas {before}; claims {sorted(claims)}")
        since = self.mark()  # type: ignore[attr-defined]
        self.run_step("env-stop", stop_env=env)  # type: ignore[attr-defined]
        record = self.wait_env(  # type: ignore[attr-defined]
            env, lambda r: r.get("state") == "stopped", timeout=300, what="stopped"
        )
        check(
            record.get("reason") == "requested"
            and (record.get("stop") or {}).get("by") == "requested",
            f"{env}: reason {record.get('reason')!r}, stop {record.get('stop')}",
        )
        wait_for(
            f"{env}: no pods",
            lambda: not _running_pods(cluster, namespace),
            timeout=300, interval=5,
        )  # fmt: skip
        stopped = _replicas(cluster, namespace)
        check(not any(stopped.values()), f"{env}: replicas while stopped {stopped}")
        check(
            _claims(cluster, namespace) == claims,
            f"{env}: claims {sorted(_claims(cluster, namespace))} != {sorted(claims)}",
        )
        revision = dict(record.get("revision") or {})

        # A push to a source rc follows: main deploys it, rc is not planned.
        sha = self.repos.commit(  # type: ignore[attr-defined]
            "store", "store: notes while rc is stopped",
            {"NOTES.md": "Written while rc was stopped.\n"},
        )  # fmt: skip
        self.wait_env(  # type: ignore[attr-defined]
            "main",
            self.deployed("main", {"store": sha}),  # type: ignore[attr-defined]
            timeout=900,
            what="main deploys the store push",
        )
        poll = (self.status().get("controller") or {}).get("poll_seconds")  # type: ignore[attr-defined]
        time.sleep(2 * int(poll or 10))  # two more polls: rc stays as it is
        record = self.env_record(env)  # type: ignore[attr-defined]
        log(f"{env} after the push: state {record.get('state')}, plan_hash "
            f"{record.get('plan_hash')!r}, store {str((record.get('revision') or {}).get('store'))[:12]}")  # fmt: skip
        check(record.get("state") == "stopped", f"{env}: {record.get('state')}")
        check(record.get("plan_hash") is None, f"{env} was planned while stopped")
        check(
            (record.get("revision") or {}) == revision,
            f"{env}: the push was wanted while stopped",
        )
        text = self.controller_log(since)  # type: ignore[attr-defined]
        check(
            f"{env}: stopped; push store/main not deployed" in text,
            f"the controller log does not say {env} skipped the push",
        )
        rolled = [
            line for line in text.splitlines() if "main: deployed; rolled" in line
        ]
        log(f"main on a push that changed no image: {rolled[-1:] or 'no deploy line'}")

        with self.ui_session() as (client, _url):  # type: ignore[attr-defined]
            options = client.get(f"/api/v1/composition/environments/{env}/actions")
            check(
                not isinstance(options, UiError)
                and (options.get("start") or {}).get("allowed") is True,
                f"the UI offers no Start for stopped {env}: {options}",
            )
            view = client.get(f"/api/v1/composition/environments/{env}")
            shown = {} if isinstance(view, UiError) else view.get("environment") or {}
            check(
                shown.get("state") == "stopped"
                and (shown.get("stop") or {}).get("by") == "requested",
                f"the UI shows {env} {shown.get('state')!r} {shown.get('stop')}",
            )

        self.run_step("env-start", stop_env=env)  # type: ignore[attr-defined]
        record = self.wait_env(  # type: ignore[attr-defined]
            env,
            lambda r: (
                r.get("state") in {"deployed", "approval-required"}
                and (r.get("revision") or {}).get("store") == sha
            ),
            timeout=900,
            what="started at the moved revision",
        )
        if record.get("state") == "approval-required":
            self.values["rc_hash"] = str(record["plan_hash"])  # type: ignore[attr-defined]
            self.run_step("approve-rc")  # type: ignore[attr-defined]
        self.wait_env(  # type: ignore[attr-defined]
            env,
            self.deployed(env, {"store": sha}),  # type: ignore[attr-defined]
            timeout=900,
            what="deployed after the start",
        )
        wait_for(
            f"{env}: replicas back",
            lambda: _replicas(cluster, namespace) == before,
            timeout=300, interval=5,
        )  # fmt: skip
        pods = self.pod_images(namespace)  # type: ignore[attr-defined]
        wait_for(
            f"{env}: pods ready",
            lambda: (lambda found: found and all(p["ready"] == "True" for p in found))(
                self.pod_images(namespace)  # type: ignore[attr-defined]
            ),
            timeout=600, interval=5,
        )  # fmt: skip
        check(_claims(cluster, namespace) == claims, f"{env}: claims changed")
        log(
            f"{env}: started; replicas {_replicas(cluster, namespace)}; {len(pods)} pods"
        )

    # ------------------------------------------------------------ 22
    def stage_22_access_ui_killed(self) -> None:
        self._ui_port_free()  # type: ignore[attr-defined]
        served = self.serve("access-env-ui", values={"access_env": "main"})  # type: ignore[attr-defined]
        kubectl: list[int] = []
        ports: list[int] = []
        try:

            def started() -> dict[str, Any] | None:
                if served.process.poll() is not None:
                    raise RuntimeError(
                        f"piceli access exited {served.process.returncode}: "
                        f"{served.text()[-600:]}"
                    )
                return next(
                    (e for e in _json_lines(served.out) if e.get("event") == "started"),
                    None,
                )

            event = wait_for(
                "access main --ui started", started, timeout=180, interval=1
            )
            forwards = event.get("forwards") or []
            ports = [int(item["local_port"]) for item in forwards]
            ids = sorted(str(item.get("id")) for item in forwards)
            log(f"main --ui: forwards {ids} on {ports}")  # never the "ui" URL
            check(UI_PORT in ports, f"the UI is not forwarded: {ports}")
            check(
                ACCESS_PORT in ports, f"main's declared forward is not on {ACCESS_PORT}"
            )
            url = str(event.get("ui") or "")
            check(url.startswith(f"http://127.0.0.1:{UI_PORT}/"), "no UI launch URL")
            wait_for(
                "every forward listening",
                lambda: all(port_open(port) for port in ports),
                timeout=180, interval=2,
            )  # fmt: skip
            body = wait_for(
                f"main's web through {ACCESS_PORT}",
                lambda: _fetch(ACCESS_PORT, WEB_PAGE),
                timeout=120, interval=2,
            )  # fmt: skip
            check("lifecycle" in body, "127.0.0.1:18080 is not main's web page")
            client = UiClient(url)
            client.open()
            composition = client.get("/api/v1/composition")
            check(
                not isinstance(composition, UiError),
                "the UI opened by access --ui does not answer",
            )
            kubectl = [p for p, c in served.children() if "port-forward" in c]
            check(len(kubectl) == len(ports), f"kubectl children {kubectl} for {ports}")

            os.kill(served.pid, signal.SIGKILL)  # piceli only, as a crash
            served.process.wait(timeout=10)
            log(f"piceli access killed (SIGKILL); its kubectl were {kubectl}")
            wait_for(
                "no kubectl left and every port free",
                lambda: not any(alive(p) for p in kubectl)
                and not any(port_open(p) for p in ports),
                timeout=30, interval=1,
            )  # fmt: skip
            mine = str(self.cluster.kubeconfig)  # type: ignore[attr-defined]
            left = [
                pid
                for pid, _, command in processes()
                if "port-forward" in command and mine in command
            ]
            check(not left, f"kubectl port-forward left behind: {left}")
        finally:
            served.stop()
            for pid in kubectl:
                if alive(pid):
                    kill_group(pid)
