"""Stages 20-21 of the k3s lifecycle acceptance: removed objects (0.14.7), and 30.

Mixed into :class:`lifecycle.Lifecycle` (its cluster, repositories, status
and run readers). Both run last, in ``main`` (auto-approved), because they
change the app the other stages check:

20. Pruning: a first push adds a StatefulSet ``ledger`` with a claim
    template (a retained claim); one push then removes ``ledger`` and turns
    ``cache`` from a Deployment into a StatefulSet of the same name behind
    the same Service. One sync: no Deployment ``cache``, StatefulSet
    ``ledger`` or Service ``ledger`` left (listed by the app's label), the
    Service ``cache`` selects only the new pod, the checks pass, and the
    claim ``data-ledger-0`` is kept and listed (``kept_orphaned``, with its
    command) in the status and the deployment history; the listed command
    deletes it.
21. A failed check right after a removal: a push removes ``reporter`` and
    adds a check that fails. The rollback restores the previous release
    whole (0.16.0; from 0.14.7 to 0.15.1 it did not re-create ``reporter``):
    ``reporter`` runs again; the environment is ``failed`` with
    ``checks-failed-rolled-back`` and nothing runs again for several polls
    (no new run, restore point or rollout); a fix (a new revision) deploys
    and prunes ``reporter``.
30. Times and heartbeat (0.16.0, last): main's history runs have each
    stage's ``started_at`` and ``finished_at`` (and stage 21's rolled-back
    run its ``rollback`` stage); a build made slow on purpose (``sleep`` in
    the host build) leaves ``last_poll`` standing while ``heartbeat_at``
    advances and the controller never reads as stale; the build's times are
    in the history; ``piceli env stop main`` and ``env start main`` are
    history entries (``kind`` ``stop`` and ``start``) next to the runs.
"""

from __future__ import annotations

import json
import time
from typing import Any

from lifecycle_support import StageFailed, check, log

LC_MAIN = "lc-main"
APP_LABEL = "app.kubernetes.io/part-of=lifecycle"

LEDGER = """
# A StatefulSet with a retained claim (lifecycle stage 20 removes it).
ledger = app.stateful_set(
    "ledger",
    image=BUSYBOX,
    command=["sh", "-c", "while true; do sleep 3600; done"],
    resources=SMALL,
    replicas=1,
    volumes={
        "/var/lib/ledger": ClaimTemplate(
            "data", size="16Mi", storage_class="lifecycle-retain"
        ),
    },
)
"""
CACHE_DEPLOYMENT = 'cache = app.deployment(\n    "cache",\n'
CACHE_STATEFULSET = (
    'cache = app.stateful_set(\n    "cache",\n    replicas=1,\n'
    '    headless=False,\n    service_name="cache",\n'
)
REPORTER = """reporter = app.deployment(
    "reporter",
    image=BUSYBOX,
    command=["sh", "-c", "while true; do sleep 3600; done"],
    resources=SMALL,
)
"""
REPORTER_CHECK = """    Checks.exec(
        "deployment/reporter",
        ["echo", "reporter-ok"],
        output_contains="reporter-ok",
        name="reporter-runs",
    ),
"""
FAILING_CHECK = """    Checks.exec(
        "statefulset/store",
        ["cat", "/var/lib/store/no-such-file"],
        name="always-fails",
    ),
"""
FULL_STACK = 'full = Stack("full", workloads=[*minimal.workloads, "reporter"])'
#: Polls (10 s each) a stopped environment is watched for a re-attempt.
QUIET_SECONDS = 45
#: How long stage 30's build sleeps: longer than ``stale`` from the last
#: poll alone (three 10 s polls and a minute).
SLOW_BUILD_SECONDS = 120


class PruneStages:
    """Stages 20 and 21 (see the module docstring)."""

    # Provided by lifecycle.Lifecycle.
    cluster: Any
    repos: Any
    since_epoch: float

    def _main_at(self, sha: str, what: str) -> dict[str, Any]:
        """Wait until main deployed infra ``sha``; fail fast on ``failed``."""
        return self.wait_env(  # type: ignore[attr-defined,no-any-return]
            "main",
            self.deployed("main", {"infra": sha}),  # type: ignore[attr-defined]
            timeout=1200,
            what=what,
        )

    def _names(self, kind: str) -> set[str]:
        body = self.cluster.get(kind, "-n", LC_MAIN, "-l", APP_LABEL) or {"items": []}
        return {
            item["metadata"]["name"]
            for item in body["items"]
            if not item["metadata"].get("deletionTimestamp")
        }

    def _history_run(self) -> dict[str, Any]:
        found = self.cluster.get(
            "configmap", "piceli-gitops-history", "-n", "piceli-system"
        )
        document = json.loads(
            ((found or {}).get("data") or {}).get("history.json") or "{}"
        )
        runs = ((document.get("envs") or {}).get("main") or {}).get("runs") or []
        check(runs, "the deployment history lists no run of main")
        return dict(runs[0])

    # ------------------------------------------------------------ stage 20
    def stage_20_prune(self) -> None:
        app = self.repos.read("infra", "lifecycle_app.py")
        infra = self.repos.read("infra", "infra.py")
        check(CACHE_DEPLOYMENT in app, "lifecycle_app.py: no cache Deployment")
        check(FULL_STACK in infra, "infra.py: no full stack")
        anchor = "# --- checks:"
        check(anchor in app, "lifecycle_app.py: no checks marker")
        with_ledger = app.replace(anchor, LEDGER + "\n" + anchor, 1)
        full_ledger = FULL_STACK.replace('"reporter"])', '"reporter", "ledger"])')
        sha = self.repos.commit(
            "infra", "ledger: a StatefulSet with a retained claim",
            {"lifecycle_app.py": with_ledger, "infra.py": infra.replace(FULL_STACK, full_ledger)},
        )  # fmt: skip
        self._main_at(sha, "the ledger is added")
        check("ledger" in self._names("statefulsets"), "no StatefulSet ledger")
        claims = self.cluster.get("pvc", "data-ledger-0", "-n", LC_MAIN)
        check(claims is not None, "no claim data-ledger-0")

        # One push: ledger removed, cache renamed to a StatefulSet.
        self.mark()  # type: ignore[attr-defined]
        renamed = app.replace(CACHE_DEPLOYMENT, CACHE_STATEFULSET, 1)
        sha = self.repos.commit(
            "infra", "cache: a StatefulSet; ledger removed",
            {"lifecycle_app.py": renamed, "infra.py": infra},
        )  # fmt: skip
        record = self._main_at(sha, "the prune")
        problems: list[str] = []
        deployments = self._names("deployments")
        sets = self._names("statefulsets")
        services = self._names("services")
        log(f"lc-main: deployments {sorted(deployments)}, statefulsets "
            f"{sorted(sets)}, services {sorted(services)}")  # fmt: skip
        if "cache" in deployments:
            problems.append("Deployment cache left behind")
        if "ledger" in sets or "ledger" in services:
            problems.append("StatefulSet or Service ledger left behind")
        if "cache" not in sets or "cache" not in services:
            problems.append("no StatefulSet cache behind the Service cache")
        pods = self.pod_images(LC_MAIN)  # type: ignore[attr-defined]
        cache_pods = sorted({p["pod"] for p in pods if p["pod"].startswith("cache")})
        if cache_pods != ["cache-0"]:
            problems.append(f"cache pods {cache_pods} (expected only cache-0)")
        slices = self.cluster.get(
            "endpointslices", "-n", LC_MAIN, "-l", "kubernetes.io/service-name=cache"
        ) or {"items": []}
        targets = sorted(
            (endpoint.get("targetRef") or {}).get("name", "")
            for item in slices["items"]
            for endpoint in item.get("endpoints") or []
        )
        if targets != ["cache-0"]:
            problems.append(f"Service cache selects {targets} (expected cache-0)")
        run = self.checked_run("/main/", self.since_epoch)  # type: ignore[attr-defined]
        results = {r["name"]: r["passed"] for r in run["checks"]["results"]}
        if not results or not all(results.values()):
            problems.append(f"checks after the prune: {results}")
        deleted = {(i.get("kind"), i.get("name")) for i in record.get("deleted") or []}
        for item in (
            ("Deployment", "cache"),
            ("StatefulSet", "ledger"),
            ("Service", "ledger"),
        ):
            if item not in deleted:
                problems.append(f"status deleted lacks {item}: {sorted(deleted)}")
        kept = {i.get("name"): i for i in record.get("kept_orphaned") or []}
        claim = kept.get("data-ledger-0") or {}
        expected = (
            f"kubectl --namespace {LC_MAIN} delete persistentvolumeclaim data-ledger-0"
        )
        if claim.get("command") != expected:
            problems.append(
                f"status kept_orphaned: {json.dumps(record.get('kept_orphaned'))}"
            )
        if self.cluster.get("pvc", "data-ledger-0", "-n", LC_MAIN) is None:
            problems.append("the claim data-ledger-0 was deleted")
        history = self._history_run()
        if not any(
            i.get("name") == "data-ledger-0" for i in history.get("kept_orphaned") or []
        ):
            problems.append(f"history kept_orphaned: {history.get('kept_orphaned')}")
        if ("Deployment", "cache") not in {
            (i.get("kind"), i.get("name")) for i in history.get("deleted") or []
        }:
            problems.append(f"history deleted: {history.get('deleted')}")
        check(not problems, "; ".join(problems))
        # The listed command deletes the claim (the owner's decision, made here).
        args = expected.split()[1:]
        self.cluster.kubectl(*args, "--wait=false")
        gone = self.cluster.get("pvc", "data-ledger-0", "-n", LC_MAIN)
        check(
            gone is None or bool(gone["metadata"].get("deletionTimestamp")),
            "the listed command did not delete the claim",
        )

    # ------------------------------------------------------------ stage 21
    def stage_21_rollback_no_loop(self) -> None:
        app = self.repos.read("infra", "lifecycle_app.py")
        infra = self.repos.read("infra", "infra.py")
        for text, what in ((REPORTER, "reporter"), (REPORTER_CHECK, "reporter check")):
            check(text in app, f"lifecycle_app.py: no {what}")
        check(FULL_STACK in infra, "infra.py: no full stack")
        check("reporter" in self._names("deployments"), "no Deployment reporter")
        broken = app.replace(REPORTER, "", 1).replace(REPORTER_CHECK, FAILING_CHECK, 1)
        without = FULL_STACK.replace(', "reporter"])', "])")
        self.mark()  # type: ignore[attr-defined]
        sha = self.repos.commit(
            "infra", "reporter removed; a check that fails",
            {"lifecycle_app.py": broken, "infra.py": infra.replace(FULL_STACK, without)},
        )  # fmt: skip

        def stopped(record: dict[str, Any]) -> bool:
            revision = record.get("revision") or {}
            return revision.get("infra") == sha and record.get("state") == "failed"

        record = self.wait_env(  # type: ignore[attr-defined]
            "main", stopped, timeout=1200, what="the failed check's rollback"
        )
        problems: list[str] = []
        if record.get("reason") != "checks-failed-rolled-back":
            problems.append(f"reason {record.get('reason')!r}")
        if record.get("next_attempt_at") is not None:
            problems.append(f"next_attempt_at {record.get('next_attempt_at')!r}")
        # 0.16.0: the rollback restores the previous release whole, reporter too.
        if "reporter" not in self._names("deployments"):
            problems.append("the rollback did not re-create the Deployment reporter")
        runs = self.runs("/main/", self.since_epoch)  # type: ignore[attr-defined]
        generations = self.generations(LC_MAIN)  # type: ignore[attr-defined]
        log(f"main stopped after {len(runs)} run(s); watching {QUIET_SECONDS}s")
        time.sleep(QUIET_SECONDS)
        later = self.env_record("main")  # type: ignore[attr-defined]
        again = self.runs("/main/", self.since_epoch)  # type: ignore[attr-defined]
        if len(again) != len(runs):
            problems.append(f"main ran again: {len(runs)} -> {len(again)} runs")
        if later.get("state") != "failed" or later.get("attempts") != record.get(
            "attempts"
        ):
            problems.append(
                f"main changed while stopped: {later.get('state')}, attempts "
                f"{record.get('attempts')} -> {later.get('attempts')}"
            )
        if self.generations(LC_MAIN) != generations:  # type: ignore[attr-defined]
            problems.append("workloads rolled while stopped")
        if "reporter" not in self._names("deployments"):
            problems.append("the Deployment reporter went away while stopped")
        check(not problems, "; ".join(problems))

        # A fix (a new revision) deploys again, without reporter (pruned).
        fixed = broken.replace(FAILING_CHECK, "", 1)
        sha = self.repos.commit(
            "infra", "the failing check removed", {"lifecycle_app.py": fixed}
        )
        try:
            self._main_at(sha, "the fix")
        except StageFailed as error:
            raise StageFailed(f"after the fix: {error}") from None
        check(
            "reporter" not in self._names("deployments"),
            "the fix did not prune reporter",
        )

    # ------------------------------------------------------------ stage 30
    def _history_runs(self, env: str = "main") -> list[dict[str, Any]]:
        found = self.cluster.get(
            "configmap", "piceli-gitops-history", "-n", "piceli-system"
        )
        document = json.loads(
            ((found or {}).get("data") or {}).get("history.json") or "{}"
        )
        return list(((document.get("envs") or {}).get(env) or {}).get("runs") or [])

    def _history_envs(self) -> list[str]:
        found = self.cluster.get(
            "configmap", "piceli-gitops-history", "-n", "piceli-system"
        )
        document = json.loads(
            ((found or {}).get("data") or {}).get("history.json") or "{}"
        )
        return sorted(document.get("envs") or {})

    def stage_30_times_and_heartbeat(self) -> None:
        problems: list[str] = []
        runs = [r for r in self._history_runs() if r.get("kind", "run") == "run"]
        check(runs, "the deployment history lists no run of main")
        for run in runs[:3]:
            for name in ("plan", "apply", "checks"):
                stage = (run.get("stages") or {}).get(name) or {}
                if stage.get("state") in {"done", "failed"} and not (
                    stage.get("started_at") and stage.get("finished_at")
                ):
                    problems.append(f"run {run.get('run_id')} {name}: {stage}")
        if "21" in self.args.stages:  # type: ignore[attr-defined]
            rolled = [r for r in runs if "rollback" in (r.get("stages") or {})]
            if not rolled:
                problems.append("no run of main has a rollback stage")
            else:
                log(f"rollback stage: {rolled[0]['stages']['rollback']}")
        check(not problems, "; ".join(problems))

        # A slow build: the heartbeat moves while last_poll stands.
        spec = self.repos.read("infra", "host-build.toml")
        tools = 'tools = ["sh", "cc"]'
        commands = "commands = [\n"
        check(tools in spec and commands in spec, "host-build.toml: no tools/commands")
        slow = spec.replace(tools, 'tools = ["sh", "cc", "sleep"]', 1).replace(
            commands, f'{commands}  ["sleep", "{SLOW_BUILD_SECONDS}"],\n', 1
        )
        store = self.repos.read("store", "bin/run.sh").replace(
            "# The store", "# The store (slow build)", 1
        )
        self.repos.commit("infra", "a slow host build", {"host-build.toml": slow})
        sha = self.repos.commit("store", "store: slow build", {"bin/run.sh": store})
        samples: list[tuple[Any, Any, str]] = []
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            status = self.status()  # type: ignore[attr-defined]
            controller = status.get("controller") or {}
            record = (status.get("envs") or {}).get("main") or {}
            if record.get("in_progress"):
                samples.append(
                    (
                        controller.get("last_poll"),
                        controller.get("heartbeat_at"),
                        str(status.get("health")),
                    )
                )
            if self.deployed("main", {"store": sha})(record):  # type: ignore[attr-defined]
                break
            time.sleep(5)
        else:
            raise StageFailed("main did not deploy the slow build")
        by_poll: dict[Any, set[Any]] = {}
        for poll, beat, _health in samples:
            by_poll.setdefault(poll, set()).add(beat)
        moving = max((len(beats) for beats in by_poll.values()), default=0)
        healths = sorted({health for *_, health in samples})
        log(f"slow build: {len(samples)} samples, heartbeats per poll {moving}, "
            f"health {healths}")  # fmt: skip
        if moving < 3:
            problems.append(f"heartbeat_at did not advance during the build: {samples}")
        if "stale" in healths:
            problems.append("the controller read as stale during the build")
        # The build runs in the step of the first environment that needs it
        # (environments step by name: with the edge stages, `edge` builds
        # what main then deploys), so look in every environment's newest run.
        newest = {
            env: next(
                (r for r in self._history_runs(env) if r.get("kind", "run") == "run"),
                {},
            )
            for env in self._history_envs()
        }
        builds = {
            env: (run.get("builds") or {}).get("store") or {}
            for env, run in newest.items()
        }
        log(f"history build of store: {builds}")
        if not any(
            b.get("started_at") and b.get("finished_at") for b in builds.values()
        ):
            problems.append(f"no build times of store in the history: {builds}")
        check(not problems, "; ".join(problems))

        # Stop and start main: two history entries next to its runs.
        self.run_step("env-stop", stop_env="main")  # type: ignore[attr-defined]
        self.wait_env(  # type: ignore[attr-defined]
            "main", lambda r: r.get("state") == "stopped", timeout=300, what="stopped"
        )
        self.run_step("env-start", stop_env="main")  # type: ignore[attr-defined]
        self.wait_env(  # type: ignore[attr-defined]
            "main",
            self.deployed("main"),
            timeout=900,
            what="started",  # type: ignore[attr-defined]
        )
        entries = self._history_runs()
        kinds = [e.get("kind") for e in entries[:3]]
        log(f"main history head: {kinds}")
        stop = next((e for e in entries if e.get("kind") == "stop"), None)
        start = next((e for e in entries if e.get("kind") == "start"), None)
        check(stop is not None and start is not None, f"no stop/start entries: {kinds}")
        assert stop is not None and start is not None
        check(
            (stop.get("stop") or {}).get("by") == "requested"
            and bool((stop.get("stop") or {}).get("at"))
            and stop.get("via") == "cli",
            f"stop entry: {stop}",
        )
        check(start.get("state") == "started", f"start entry: {start}")
