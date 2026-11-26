"""Release candidates (0.83.0): the quiet timer, cancel, request, keep, failures.

No network, no threads: a fake clock, a fake history reader and a fake copy
set; the worker either runs inline or waits in a list the test runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from piceli.gitops import GitOpsError
from piceli.gitops.candidates import (
    BACKOFF,
    MAX_ATTEMPTS,
    NEWEST_KEY,
    STATE_KEY,
    Candidate,
    CandidateCopy,
    Candidates,
    describe,
    protected_digests,
    status_view,
)
from piceli.gitops.state import REQUEST_KINDS, candidate_request
from piceli.infra.cluster import ClusterError

NOW = 2_000_000_000.0
A, B, C = "a" * 40, "b" * 40, "c" * 40
DIGEST = {"web": "sha256:" + "1" * 64, "worker": "sha256:" + "2" * 64}
REGISTRY = "piceli-registry.piceli-system.svc:5000"


def _iso(seconds: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def passed(*commits: tuple[str, float, str]) -> dict[str, Any]:
    """The record of passed commits: ``(commit, passed at, run)``."""
    return {
        "staging": {
            "app": {
                commit: {
                    "first_run": run,
                    "first_at": _iso(at),
                    "last_run": run,
                    "last_at": _iso(at),
                }
                for commit, at, run in commits
            }
        }
    }


def component(name: str, *, image: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "source": "app",
        "digest": DIGEST[name],
        "image": {
            "repository": f"shop/{name}",
            "reference": f"{REGISTRY}/shop/{name}@{DIGEST[name]}",
        }
        if image
        else None,
    }


class Rig:
    """A candidates object with a fake clock, history and copy set."""

    def __init__(
        self,
        copies: tuple[CandidateCopy, ...] | None = None,
        *,
        keep: int = 5,
        quiet: str = "30m",
        inline: bool = True,
    ) -> None:
        self.now = NOW
        self.logs: list[str] = []
        self.copied: list[tuple[str, str, str | None, str]] = []
        #: component -> report; a callable raises or answers
        self.outcome: dict[str, Any] = {}
        self.components = [component("web")]
        self.run_found = True
        self.waiting: list[Callable[[], None]] = []
        self.state: dict[str, Any] = {}
        self.setting = Candidate(
            environment="staging",
            quiet=quiet,
            keep=keep,
            copies=copies
            or (
                CandidateCopy(
                    "web",
                    "oci://ghcr.io/example/shop-web",
                    tag="{commit}",
                    credentials="shop-push",
                ),
            ),
        )
        self.candidates = Candidates(
            self.state,
            self.setting,
            clock=lambda: self.now,
            log=self.logs.append,
            artifacts=self._artifacts,
            copy=self._copy,
            spawn=(lambda work: work()) if inline else self.waiting.append,
            registry_host=REGISTRY,
        )

    def _artifacts(self, document: Any, run: str, env: str | None = None) -> Any:
        if not self.run_found:
            return None
        return {"env": env, "run": run, "components": self.components}

    def _copy(self, item: Any, secret: str | None, ledger: str) -> Any:
        self.copied.append((item["source"], item["target"], secret, ledger))
        found = self.outcome.get(item["component"])
        if isinstance(found, Exception):
            raise found
        if found is None:
            return SimpleNamespace(ok=True, failed=None)
        return SimpleNamespace(ok=False, failed=found)

    def poll(self, record: dict[str, Any]) -> None:
        self.candidates.poll(record, {"schema": "history"}, sources=["app"])

    def record(self, commit: str) -> dict[str, Any]:
        return self.state[STATE_KEY][commit]

    def run_waiting(self) -> None:
        while self.waiting:
            self.waiting.pop(0)()


# ------------------------------------------------------------- declaration


def test_a_declaration_round_trips_and_names_its_targets() -> None:
    setting = Candidate(
        environment="staging",
        quiet="30m",
        keep=5,
        copies=(
            CandidateCopy("web", "oci://ghcr.io/example/shop-web", tag="{commit}"),
            CandidateCopy(
                "worker", "oci://registry.example.org:5000/shop/worker", credentials="s"
            ),
        ),
    )
    assert Candidate.from_dict(json.loads(json.dumps(setting.to_dict()))) == setting
    assert setting.quiet_seconds == 1800
    assert setting.secrets == ("s",)
    web, worker = setting.copies
    assert web.target(A) == "oci://ghcr.io/example/shop-web:" + "a" * 12
    assert worker.target(A) == "oci://registry.example.org:5000/shop/worker"
    assert Candidate(environment="staging").copies == ()


@pytest.mark.parametrize(
    "build, words",
    [
        (lambda: Candidate(environment="Staging!"), "environment"),
        (lambda: Candidate(environment="staging", quiet="soon"), "quiet"),
        (lambda: Candidate(environment="staging", keep=0), "keep"),
        (lambda: Candidate(environment="staging", keep=True), "keep"),  # type: ignore[arg-type]
        (lambda: Candidate(environment="staging", copies=["web"]), "CandidateCopy"),  # type: ignore[list-item]
        (lambda: CandidateCopy("Web_1", "oci://ghcr.io/example/web"), "component"),
        (lambda: CandidateCopy("web", "ghcr.io/example/web"), "oci://"),
        (lambda: CandidateCopy("web", "oci://ghcr.io/example/web:1"), "no tag"),
        (lambda: CandidateCopy("web", "oci://ghcr.io/example/web@sha256:1"), "no tag"),
        (lambda: CandidateCopy("web", "oci://ghcr.io/e/web", tag="{branch}"), "commit"),
        (lambda: CandidateCopy("web", "oci://ghcr.io/e/web", tag="-bad"), "tag"),
        (
            lambda: CandidateCopy("web", "oci://ghcr.io/e/web", credentials="A b"),
            "Secret",
        ),
    ],
)
def test_invalid_declarations_are_refused(build: Any, words: str) -> None:
    with pytest.raises(ClusterError, match=words) as error:
        build()
    assert error.value.code == "cluster-invalid"


def test_two_copies_may_not_share_a_component_or_a_target() -> None:
    web = CandidateCopy("web", "oci://ghcr.io/example/web")
    with pytest.raises(ClusterError, match="component twice"):
        Candidate(
            environment="staging",
            copies=(web, CandidateCopy("web", "oci://ghcr.io/example/other")),
        )
    with pytest.raises(ClusterError, match="target twice"):
        Candidate(
            environment="staging",
            copies=(web, CandidateCopy("worker", "oci://ghcr.io/example/web")),
        )


# ------------------------------------------------------------- the quiet timer


def test_a_passed_commit_waits_for_the_quiet_time_then_copies_and_is_ready() -> None:
    rig = Rig()
    record = passed((A, NOW, "run-1"))
    rig.poll(record)
    assert rig.record(A)["state"] == "waiting"
    assert rig.record(A)["quiet_until"] == _iso(NOW + 1800)
    assert rig.copied == []
    rig.now += 1799
    rig.poll(record)
    assert rig.copied == []
    rig.now += 1
    rig.poll(record)
    (source, target, secret, ledger) = rig.copied[0]
    assert source == f"oci://{REGISTRY}/shop/web@{DIGEST['web']}"
    assert target == "oci://ghcr.io/example/shop-web:" + "a" * 12
    assert secret == "shop-push"
    assert ledger.startswith("candidate-" + "a" * 12)
    found = rig.record(A)
    assert found["state"] == "ready"
    assert found["copies"] == {"web": "copied"}
    assert found["digests"] == [DIGEST["web"]]
    assert found["run"] == "run-1"
    rig.poll(record)  # ready stays ready and copies nothing again
    assert len(rig.copied) == 1
    assert [describe(item) for item in status_view(rig.state)] == [
        f"candidate {'a' * 7}: ready"
    ]


def test_the_quiet_time_counts_from_when_the_commit_passed() -> None:
    rig = Rig()
    # The controller was off: the commit passed an hour ago.
    rig.poll(passed((A, NOW - 3600, "run-1")))
    assert rig.record(A)["state"] == "ready"


def test_a_newer_commit_restarts_the_timer_and_drops_the_waiting_one() -> None:
    rig = Rig()
    rig.poll(passed((A, NOW, "run-1")))
    rig.now += 1200
    record = passed((A, NOW, "run-1"), (B, NOW + 1200, "run-2"))
    rig.poll(record)
    assert A not in rig.state[STATE_KEY]  # it never started: nothing to cancel
    assert rig.state[NEWEST_KEY] == B
    assert rig.record(B)["state"] == "waiting"
    rig.now += 1799  # A's quiet time is over, B's is not
    rig.poll(record)
    assert rig.copied == []
    rig.now += 1
    rig.poll(record)
    assert rig.record(B)["state"] == "ready"
    assert "b" * 12 in rig.copied[0][1]


# ------------------------------------------------------------- cancel


def test_a_newer_commit_cancels_a_candidate_still_building() -> None:
    rig = Rig(inline=False)
    rig.poll(passed((A, NOW - 3600, "run-1")))
    assert rig.record(A)["state"] == "building"
    assert len(rig.waiting) == 1  # the worker has not copied anything yet
    rig.now += 60
    rig.poll(passed((A, NOW - 3600, "run-1"), (B, NOW + 30, "run-2")))
    cancelled = rig.record(A)
    assert cancelled["state"] == "cancelled"
    assert cancelled["reason"] == "candidate-superseded"
    assert "b" * 12 in cancelled["detail"]
    rig.run_waiting()  # the worker stops between items: nothing was copied
    assert rig.copied == []
    assert rig.record(B)["state"] == "waiting"
    assert describe(status_view(rig.state)[1]).startswith(
        f"candidate {'a' * 7}: not ready: candidate-superseded"
    )


def test_a_request_resumes_a_cancelled_candidate() -> None:
    rig = Rig(copies=_two(), inline=False)
    record = passed((A, NOW - 3600, "run-1"))
    rig.components = [component("web"), component("worker")]
    rig.poll(record)
    rig.run_waiting()
    rig.poll(record)  # web and worker are copied
    assert rig.record(A)["state"] == "ready"
    assert len(rig.copied) == 2

    # Another commit: the worker stops after its first item (cancel set between).
    rig2 = Rig(copies=_two(), inline=False)
    rig2.components = [component("web"), component("worker")]
    first = passed((A, NOW - 3600, "run-1"))
    rig2.poll(first)
    job = rig2.candidates._jobs[A]
    job.cancel.set()  # a newer commit passed: the worker checks before each item
    rig2.run_waiting()
    assert rig2.copied == []
    newer = passed((A, NOW - 3600, "run-1"), (B, NOW, "run-2"))
    rig2.poll(newer)
    assert rig2.record(A)["state"] == "cancelled"
    rig2.candidates.request("aaaaaaa")
    assert rig2.record(A)["state"] == "building"
    assert rig2.record(A)["requested"] is True
    rig2.poll(newer)  # a requested candidate is not cancelled again
    rig2.run_waiting()
    rig2.poll(newer)
    assert rig2.record(A)["state"] == "ready"
    assert len(rig2.copied) == 2


def _two() -> tuple[CandidateCopy, ...]:
    return (
        CandidateCopy("web", "oci://ghcr.io/example/shop-web", tag="{commit}"),
        CandidateCopy("worker", "oci://ghcr.io/example/shop-worker"),
    )


def test_a_commit_that_passes_again_after_a_newer_one_waits_again() -> None:
    rig = Rig(inline=False)
    rig.poll(passed((A, NOW - 3600, "run-1")))
    rig.poll(passed((A, NOW - 3600, "run-1"), (B, NOW, "run-2")))
    rig.run_waiting()
    assert rig.record(A)["state"] == "cancelled"
    rig.now += 100
    rig.poll(passed((A, NOW + 100, "run-3"), (B, NOW, "run-2")))  # A passed again
    assert rig.record(A)["state"] == "waiting"
    assert rig.record(A)["run"] == "run-3"
    assert rig.record(A)["quiet_until"] == _iso(NOW + 100 + 1800)


# ------------------------------------------------------------- request


def test_a_request_starts_the_candidate_at_once_without_the_quiet_wait() -> None:
    rig = Rig()
    record = passed((A, NOW, "run-1"))
    rig.state["passed_commits"] = record
    rig.candidates.request(A[:10])
    assert rig.record(A)["state"] == "building"
    rig.poll(record)  # one poll: no 30 minutes
    assert rig.record(A)["state"] == "ready"
    assert rig.record(A)["requested"] is True
    assert len(rig.copied) == 1
    rig.candidates.request(A)  # ready: left alone
    assert rig.record(A)["state"] == "ready"


def test_a_request_for_an_older_commit_is_not_cancelled_by_the_newest() -> None:
    rig = Rig(inline=False)
    record = passed((A, NOW - 7200, "run-1"), (B, NOW, "run-2"))
    rig.state["passed_commits"] = record
    rig.poll(record)  # B waits
    rig.candidates.request(A)
    rig.poll(record)
    rig.run_waiting()
    rig.poll(record)
    assert rig.record(A)["state"] == "ready"
    assert rig.record(B)["state"] == "waiting"


def test_requests_for_unknown_commits_or_other_environments_are_refused() -> None:
    rig = Rig()
    rig.state["passed_commits"] = passed((A, NOW, "run-1"))
    with pytest.raises(GitOpsError, match="did not pass") as error:
        rig.candidates.request(B[:8])
    assert error.value.code == "gitops-request-invalid"
    with pytest.raises(GitOpsError, match="hexadecimal"):
        rig.candidates.request("not-a-commit")
    with pytest.raises(GitOpsError, match="not production"):
        rig.candidates.handle({"action": "request", "commit": A, "env": "production"})
    with pytest.raises(GitOpsError, match="unknown candidate action"):
        rig.candidates.handle({"action": "delete", "commit": A})
    rig.candidates.handle({"action": "request", "commit": A, "env": "staging"})
    assert rig.record(A)["state"] == "building"


def test_the_request_the_command_sends_is_validated() -> None:
    assert "candidate" in REQUEST_KINDS
    key, body = candidate_request("request", "ABCDEF1", "staging")
    assert key.startswith("candidate.")
    assert body["commit"] == "abcdef1" and body["env"] == "staging"
    assert body["action"] == "request" and body["kind"] == "candidate"
    for action, commit, env in (
        ("request", "nope", None),
        ("forget", A, None),
        ("request", A, "Not_A_Label"),
    ):
        with pytest.raises(GitOpsError) as error:
            candidate_request(action, commit, env)
        assert error.value.code == "gitops-request-invalid"


# ------------------------------------------------------------- keep, release


def _ready(state: dict[str, Any], commit: str, at: float, **extra: Any) -> None:
    state.setdefault(STATE_KEY, {})[commit] = {
        "state": "ready",
        "since": _iso(at),
        "passed_at": _iso(at),
        "run": "run",
        "copies": {"web": "copied"},
        "reason": None,
        "released": False,
        "digests": ["sha256:" + commit[0] * 64],
        **extra,
    }


def test_the_newest_candidates_and_every_released_one_stay() -> None:
    rig = Rig(keep=2)
    commits = ["1" * 40, "2" * 40, "3" * 40, "4" * 40]
    for number, commit in enumerate(commits):
        _ready(rig.state, commit, NOW + number, released=number == 0)
    rig.state[NEWEST_KEY] = commits[-1]
    rig.poll(passed((commits[-1], NOW + 3, "run")))
    assert sorted(rig.state[STATE_KEY]) == [commits[0], commits[2], commits[3]]
    assert protected_digests(rig.state) == {
        "sha256:" + "1" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
    }
    assert [item["commit"] for item in status_view(rig.state)] == [
        commits[3],
        commits[2],
        commits[0],
    ]


def test_a_building_candidate_is_never_pruned_and_release_needs_a_ready_one() -> None:
    rig = Rig(keep=1, inline=False)
    _ready(rig.state, A, NOW)
    rig.state[STATE_KEY][B] = {
        "state": "building",
        "since": _iso(NOW - 100),
        "passed_at": _iso(NOW - 100),
        "copies": {},
        "plan": [],
        "digests": [],
        "requested": True,
    }
    rig.state[NEWEST_KEY] = A
    rig.poll(passed((A, NOW, "run")))
    assert set(rig.state[STATE_KEY]) == {A, B}
    with pytest.raises(GitOpsError, match="building, not ready"):
        rig.candidates.release(B[:7])
    assert rig.candidates.release(A[:7]) == A
    assert rig.record(A)["released"] is True
    assert status_view(rig.state)[0]["released"] is True
    assert (
        describe(status_view(rig.state)[0]) == f"candidate {'a' * 7}: ready, released"
    )
    with pytest.raises(GitOpsError, match="did not pass"):
        rig.candidates.release(C[:7])


# ------------------------------------------------------------- failures


def test_a_component_without_an_image_fails_the_candidate_naming_it() -> None:
    rig = Rig()
    rig.components = [component("web", image=False)]
    rig.poll(passed((A, NOW - 3600, "run-1")))
    found = rig.record(A)
    assert found["state"] == "failed"
    assert found["reason"] == "candidate-no-image"
    assert "web" in found["detail"]
    assert rig.copied == []
    assert describe(status_view(rig.state)[0]) == (
        f"candidate {'a' * 7}: not ready: candidate-no-image "
        "(component web has no image reference)"
    )


def test_a_run_or_component_the_history_lacks_fails_the_candidate() -> None:
    rig = Rig()
    rig.run_found = False
    rig.poll(passed((A, NOW - 3600, "run-1")))
    assert rig.record(A)["reason"] == "candidate-run-missing"
    other = Rig()
    other.components = [component("worker")]
    other.poll(passed((A, NOW - 3600, "run-1")))
    assert other.record(A)["reason"] == "candidate-no-component"
    assert "web" in other.record(A)["detail"]


def test_a_permanent_copy_failure_fails_once_and_names_the_component() -> None:
    rig = Rig()
    rig.outcome["web"] = "copy-tag-exists"
    record = passed((A, NOW - 3600, "run-1"))
    rig.poll(record)
    found = rig.record(A)
    assert (found["state"], found["reason"], found["detail"]) == (
        "failed",
        "copy-tag-exists",
        "web",
    )
    assert found["copies"] == {"web": "failed"}
    rig.now += 100_000
    rig.poll(record)  # no loop: a failed candidate is not tried again
    assert len(rig.copied) == 1


def test_a_transient_failure_is_tried_again_with_a_backoff_then_gives_up() -> None:
    rig = Rig()
    rig.outcome["web"] = "registry-unreachable"
    record = passed((A, NOW - 3600, "run-1"))
    rig.poll(record)
    found = rig.record(A)
    assert found["state"] == "building"
    assert found["reason"] == "registry-unreachable"
    assert found["retry_at"] == _iso(NOW + BACKOFF[0])
    rig.now += BACKOFF[0] - 1
    rig.poll(record)
    assert len(rig.copied) == 1  # not yet
    for attempt in range(1, MAX_ATTEMPTS - 1):
        rig.now += BACKOFF[attempt - 1]
        rig.poll(record)
        assert rig.record(A)["state"] == "building"
        assert len(rig.copied) == attempt + 1
    rig.now += 100_000
    rig.poll(record)
    found = rig.record(A)
    assert found["state"] == "failed"
    assert found["reason"] == "registry-unreachable"
    assert len(rig.copied) == MAX_ATTEMPTS
    rig.now += 100_000
    rig.poll(record)
    assert len(rig.copied) == MAX_ATTEMPTS


def test_a_transient_failure_that_clears_makes_the_candidate_ready() -> None:
    rig = Rig()
    rig.outcome["web"] = "registry-unreachable"
    record = passed((A, NOW - 3600, "run-1"))
    rig.poll(record)
    del rig.outcome["web"]
    rig.now += BACKOFF[0]
    rig.poll(record)
    found = rig.record(A)
    assert found["state"] == "ready"
    assert found["reason"] is None and "detail" not in found
    assert found["retry_at"] is None


def test_a_copy_that_raises_fails_the_candidate_with_its_reason() -> None:
    rig = Rig()
    error = RuntimeError("no credentials")
    error.code = "candidate-credentials-missing"  # type: ignore[attr-defined]
    rig.outcome["web"] = error
    rig.poll(passed((A, NOW - 3600, "run-1")))
    assert rig.record(A)["reason"] == "candidate-credentials-missing"
    other = Rig()
    other.outcome["web"] = ValueError("boom")
    other.poll(passed((A, NOW - 3600, "run-1")))
    assert other.record(A)["reason"] == "candidate-copy-error"


def test_the_copies_done_stay_done_when_a_later_one_fails_and_resumes() -> None:
    rig = Rig(copies=_two())
    rig.components = [component("web"), component("worker")]
    rig.outcome["worker"] = "registry-unreachable"
    record = passed((A, NOW - 3600, "run-1"))
    rig.poll(record)
    assert rig.record(A)["copies"] == {"web": "copied", "worker": "pending"}
    del rig.outcome["worker"]
    rig.now += BACKOFF[0]
    rig.poll(record)
    assert rig.record(A)["state"] == "ready"
    assert [item[1] for item in rig.copied].count(
        "oci://ghcr.io/example/shop-web:" + "a" * 12
    ) == 1  # web was not copied again


# ------------------------------------------------------------- status


def test_the_status_lists_each_candidate_newest_first() -> None:
    rig = Rig()
    record = passed((A, NOW - 7200, "run-1"), (B, NOW, "run-2"))
    rig.state["passed_commits"] = record
    rig.poll(record)  # B waits; A was never the newest seen
    rig.candidates.request(A)
    rig.poll(record)
    shown = status_view(rig.state)
    assert [item["commit"] for item in shown] == [B, A]
    assert set(shown[0]) >= {"commit", "state", "since", "reason", "copies"}
    assert shown[0]["state"] == "waiting"
    assert shown[0]["quiet_until"] == _iso(NOW + 1800)
    assert shown[1]["state"] == "ready" and "quiet_until" not in shown[1]
    assert json.loads(json.dumps(shown)) == shown
    assert describe(shown[0]).startswith(
        f"candidate {'b' * 7}: not ready: waiting for a quiet time"
    )
    assert describe(shown[1]) == f"candidate {'a' * 7}: ready"
    assert describe(
        {"commit": C, "state": "building", "copies": {"web": "copied", "w": "pending"}}
    ) == (f"candidate {'c' * 7}: not ready: building (1/2 copied)")


def test_a_state_without_candidates_has_nothing_to_show() -> None:
    assert status_view({}) == []
    assert protected_digests({}) == frozenset()
    assert protected_digests({STATE_KEY: {A: "junk"}}) == frozenset()
