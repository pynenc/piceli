"""Stages 20-21 of the k3s lifecycle acceptance: removed objects (0.14.7).

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
    adds a check that fails. The release is rolled back without
    re-creating ``reporter``; the environment is ``failed`` with
    ``checks-failed-rolled-back`` and nothing runs again for several polls
    (no new run, restore point or rollout); a fix (a new revision) deploys.
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
        if "reporter" in self._names("deployments"):
            problems.append("the rollback re-created the Deployment reporter")
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
        if "reporter" in self._names("deployments"):
            problems.append("the Deployment reporter came back")
        check(not problems, "; ".join(problems))

        # A fix (a new revision) deploys again, without reporter.
        fixed = broken.replace(FAILING_CHECK, "", 1)
        sha = self.repos.commit(
            "infra", "the failing check removed", {"lifecycle_app.py": fixed}
        )
        try:
            self._main_at(sha, "the fix")
        except StageFailed as error:
            raise StageFailed(f"after the fix: {error}") from None
        check("reporter" not in self._names("deployments"), "reporter came back")
