"""0.83.0: ``piceli artifacts copy`` as a set that resumes and retries, and as a Job.

``run_copy_set`` skips what a ledger says is done, tries a copy that failed
for a transient reason again as a whole, and stops at a permanent one. The
cluster part builds a Job (no secret in it), reads the ledger and the report
from the pod log and re-attaches to a Job a killed client left. Every copy
here is a fake: no registry, no cluster.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from kubernetes.client.exceptions import ApiException

from piceli.artifacts import copy_cluster
from piceli.artifacts.cli import build_parser, run_copy_command
from piceli.artifacts.copy_cluster import (
    LEDGER_MARKER,
    RESULT_MARKER,
    copy_in_cluster,
    credentials_from_env,
    decode,
    job_manifest,
    job_name,
    registry_service,
    rewrite_for_cluster,
    run_job,
    secret_data,
)
from piceli.artifacts.copy_set import (
    ConfigMapLedger,
    CopyItem,
    CopyOptions,
    CopyReport,
    CopySetError,
    CopySetPlan,
    FileLedger,
    MemoryLedger,
    is_transient,
    load_set_file,
    pause,
    run_copy_set,
)
from piceli.artifacts.image_copy import CopyError, CopyPlan
from piceli.artifacts.registry import RegistryCredentials, RegistryError
from piceli.pipeline.prerollout_cluster import Outcome

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
THIRD = "sha256:" + "c" * 64
IMAGE = "ghcr.io/example/piceli@sha256:" + "d" * 64


def item(name: str, digest: str = DIGEST) -> CopyItem:
    return CopyItem(
        f"oci://registry.example/shop/{name}@{digest}",
        f"oci://ghcr.io/example/{name}:1.0",
    )


def copied(target: str = "x") -> dict[str, Any]:
    return {
        "revision": "piceli.copy-receipt.v1",
        "state": "copied",
        "reason": None,
        "digest": DIGEST,
        "platforms": {},
        "referrers": [],
        "target": {"registry": "ghcr.io", "repository": target, "tag": "1.0"},
    }


def failed(reason: str, **extra: Any) -> dict[str, Any]:
    return {"state": "failed", "reason": reason, "error": None, **extra}


class Script:
    """A fake ``copy``: per target, the receipts to return in turn."""

    def __init__(self, receipts: dict[str, list[Any]]) -> None:
        self.receipts = {key: list(value) for key, value in receipts.items()}
        self.calls: list[str] = []

    def __call__(self, found: CopyItem) -> dict[str, Any]:
        self.calls.append(found.target)
        queue = self.receipts.get(found.target) or [copied(found.target)]
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, BaseException):
            raise step
        return step  # type: ignore[no-any-return]


def run(
    items: list[CopyItem],
    script: Callable[[CopyItem], dict[str, Any]],
    store: Any = None,
    **options: Any,
) -> tuple[CopyReport, list[float], Any]:
    sleeps: list[float] = []
    store = store or MemoryLedger()
    report = run_copy_set(
        items,
        ledger=options.pop("ledger", "release"),
        store=store,
        sleep=sleeps.append,
        copy=script,
        jitter=lambda _low, high: high,  # the longest pause: easy to assert
        **options,
    )
    return report, sleeps, store


# ----------------------------------------------------------------- the items
def test_an_item_key_is_stable_and_names_source_and_target() -> None:
    first = item("api")
    assert first.key == item("api").key
    assert first.key != item("web").key
    assert first.key != item("api", OTHER).key
    # Credentials are not part of what is copied.
    assert CopyItem(first.source, first.target, "/a", "/b").key == first.key
    assert CopyItem.from_dict(first.to_dict()) == first
    with pytest.raises(CopySetError):
        CopyItem.from_dict({"source": "a", "target": "b", "password": "x"})


def test_a_set_file_in_json_or_toml_lists_from_and_to(tmp_path: Path) -> None:
    pairs = [
        {"from": item("api").source, "to": item("api").target},
        {
            "from": item("web").source,
            "to": item("web").target,
            "credentials": "push.json",
        },
    ]
    (tmp_path / "set.json").write_text(json.dumps(pairs))
    toml = "".join(
        f'[[copy]]\nfrom = "{p["from"]}"\nto = "{p["to"]}"\n'
        + (f'credentials = "{p["credentials"]}"\n' if "credentials" in p else "")
        for p in pairs
    )
    (tmp_path / "set.toml").write_text(toml)
    for name in ("set.json", "set.toml"):
        found = load_set_file(tmp_path / name)
        assert [i.source for i in found] == [item("api").source, item("web").source]
        assert found[0].target_credentials is None
        assert found[1].target_credentials == str(tmp_path / "push.json")
    (tmp_path / "bad.json").write_text("[]")
    with pytest.raises(CopySetError):
        load_set_file(tmp_path / "bad.json")
    (tmp_path / "extra.json").write_text(json.dumps([{"from": "a", "to": "b", "x": 1}]))
    with pytest.raises(CopySetError):
        load_set_file(tmp_path / "extra.json")


def test_one_plan_digest_covers_the_whole_set() -> None:
    options = CopyOptions()
    one = CopySetPlan.parse([item("api")], options)
    # A set of one is the plan of that copy: approvals from before still hold.
    assert one.digest == CopyPlan.parse(item("api").source, item("api").target).digest
    both = CopySetPlan.parse([item("api"), item("web")], options)
    assert both.digest != one.digest
    assert (
        both.digest
        != CopySetPlan.parse([item("api"), item("api", OTHER)], options).digest
    )
    assert (
        both.digest
        != CopySetPlan.parse(
            [item("api"), item("web")], CopyOptions(move_tag=True)
        ).digest
    )
    assert both.digest != CopySetPlan.parse([item("web"), item("api")], options).digest
    assert both.preview()["plan_digest"] == both.digest
    assert len(both.preview()["items"]) == 2
    with pytest.raises(CopySetError):
        CopySetPlan.parse([item("api"), item("api")], options)


# ----------------------------------------------------------------- the ledger
def test_a_set_copies_in_order_and_records_each_item() -> None:
    script = Script({})
    report, sleeps, store = run([item("api"), item("web")], script)
    assert report.ok and report.failed is None and sleeps == []
    assert script.calls == [item("api").target, item("web").target]
    document = store.load("release")
    records = document["items"]
    assert set(records) == {item("api").key, item("web").key}
    for record in records.values():
        assert record["state"] == "done" and record["attempts"] == 1
        assert record["receipt_digest"].startswith("sha256:")
        assert record["at"].endswith("Z")
    as_dict = report.to_dict()
    assert as_dict["revision"] == "piceli.copy-set.v1"
    assert as_dict["ok"] is True and as_dict["failed"] is None
    assert [i["state"] for i in as_dict["items"]] == ["done", "done"]
    assert as_dict["items"][0]["receipt"]["state"] == "copied"
    assert CopyReport.from_dict(as_dict).ok


def test_a_rerun_with_the_same_ledger_skips_what_is_done() -> None:
    store = MemoryLedger()
    run([item("api"), item("web")], Script({}), store)
    script = Script({})
    report, _, _ = run([item("api"), item("web"), item("db")], script, store)
    assert script.calls == [item("db").target]
    assert [i.skipped for i in report.items] == [True, True, False]
    assert report.ok


def test_a_rerun_reports_what_the_done_copy_copied() -> None:
    """0.87.0: an item the ledger names done reports its receipt (target,
    tag, digest), not ``None``, marked ``from_ledger``."""
    store = MemoryLedger()
    run([item("api")], Script({}), store)
    report, _, _ = run([item("api")], Script({}), store)
    found = report.items[0].receipt
    assert report.items[0].skipped and found is not None
    assert found["target"]["tag"] == "1.0" and found["digest"] == DIGEST
    assert found["state"] == "copied" and found["from_ledger"] is True


def test_a_killed_run_leaves_the_ledger_and_the_rerun_continues() -> None:
    store = MemoryLedger()
    items = [item("api"), item("web"), item("db")]
    killed = Script({item("web").target: [KeyboardInterrupt()]})
    with pytest.raises(KeyboardInterrupt):
        run(items, killed, store)
    records = store.load("release")["items"]
    assert records[item("api").key]["state"] == "done"
    assert item("web").key not in records
    script = Script({})
    report, _, _ = run(items, script, store)
    assert script.calls == [item("web").target, item("db").target]
    assert report.ok and report.items[0].skipped


def test_a_transient_failure_is_tried_again_then_succeeds() -> None:
    script = Script(
        {
            item("api").target: [
                failed("registry-unreachable"),
                failed("registry-unreachable"),
                copied(),
            ]
        }
    )
    report, sleeps, store = run([item("api")], script, attempts=5)
    assert report.ok and len(script.calls) == 3
    # Exponential: 2 s then 4 s (the test's jitter takes the longest).
    assert sleeps == [2.0, 4.0]
    assert [r["attempt"] for r in report.retries] == [1, 2]
    record = store.load("release")["items"][item("api").key]
    assert record["state"] == "done" and record["attempts"] == 3
    assert record["reason"] is None


def test_the_ledger_records_every_failed_attempt() -> None:
    saved: list[dict[str, Any]] = []

    class Spy(MemoryLedger):
        def save(self, name: str, data: dict[str, Any]) -> None:
            super().save(name, data)
            saved.append(json.loads(json.dumps(data)))

    script = Script({item("api").target: [failed("registry-unreachable"), copied()]})
    run([item("api")], script, Spy())
    states = [s["items"][item("api").key]["state"] for s in saved]
    assert states[:2] == ["failed", "done"]
    assert saved[0]["items"][item("api").key]["reason"] == "registry-unreachable"


def test_a_transient_failure_gives_up_after_the_attempts() -> None:
    script = Script({item("api").target: [failed("registry-unreachable")]})
    report, sleeps, store = run(
        [item("api"), item("web")], script, attempts=3, backoff_cap=3.0
    )
    assert not report.ok and report.failed == "registry-unreachable"
    assert len(script.calls) == 3 and sleeps == [2.0, 3.0]  # capped
    assert [i.state for i in report.items] == ["failed", "pending"]
    assert store.load("release")["items"][item("api").key]["attempts"] == 3


def test_a_permanent_reason_stops_at_once() -> None:
    script = Script({item("api").target: [failed("copy-tag-exists")]})
    report, sleeps, store = run([item("api"), item("web")], script, attempts=5)
    assert len(script.calls) == 1 and sleeps == []
    assert report.failed == "copy-tag-exists"
    assert [i.state for i in report.items] == ["failed", "pending"]  # web untouched
    records = store.load("release")["items"]
    assert records[item("api").key]["state"] == "failed"
    assert item("web").key not in records
    # Fixed and run again: the same ledger continues.
    again, _, _ = run([item("api"), item("web")], Script({}), store)
    assert again.ok


def test_what_is_transient_follows_the_registry_clients_rules() -> None:
    assert is_transient(failed("registry-unreachable"))
    for status in (429, 502, 503, 504):
        assert is_transient(failed("registry-error", error={"status": status}))
    assert not is_transient(failed("registry-error", error={"status": 500}))
    assert not is_transient(failed("registry-error"))
    assert not is_transient(failed("registry-unauthorized"))
    assert not is_transient(failed("copy-verification-failed"))
    assert not is_transient(copied())


def test_a_raised_error_is_judged_by_its_own_transience() -> None:
    script = Script(
        {item("api").target: [RegistryError("registry-unreachable", transient=True)]}
    )
    report, sleeps, _ = run([item("api")], script, attempts=2)
    assert len(script.calls) == 2 and len(sleeps) == 1 and report.failed
    refused = Script({item("api").target: [CopyError("copy-not-approved")]})
    report, sleeps, _ = run([item("api")], refused)
    assert report.failed == "copy-not-approved" and sleeps == []


def test_the_pause_grows_and_is_capped_with_jitter() -> None:
    assert pause(1, 2.0, 60.0, lambda _a, _b: 0.0) == 1.0
    assert pause(1, 2.0, 60.0, lambda _a, b: b) == 2.0
    assert pause(4, 2.0, 60.0, lambda _a, b: b) == 16.0
    assert pause(10, 2.0, 60.0, lambda _a, b: b) == 60.0
    assert 30.0 <= pause(10, 2.0, 60.0) <= 60.0


def test_the_ledger_stays_bounded_to_the_current_set() -> None:
    store = MemoryLedger()
    stale = {
        "revision": "piceli.copy-ledger.v1",
        "items": {f"old{n}": {"state": "done", "attempts": 1} for n in range(50)},
    }
    store.save("release", stale)
    run([item("api")], Script({}), store)
    assert set(store.load("release")["items"]) == {item("api").key}
    assert len(json.dumps(store.load("release"))) < 600


def test_a_set_is_checked_before_anything_is_copied() -> None:
    script = Script({})
    with pytest.raises(CopySetError):
        run([item("api")], script, attempts=0)
    with pytest.raises(CopySetError):
        run([item("api"), item("api")], script)
    with pytest.raises(CopySetError):
        run([], script)
    with pytest.raises(CopySetError):
        run([item("api")], script, ledger="Not A Name")
    many = [
        CopyItem(f"oci://registry.example/s/{n}@{DIGEST}", f"oci://ghcr.io/e/{n}")
        for n in range(501)
    ]
    with pytest.raises(CopySetError):
        run(many, script)
    assert script.calls == []


def test_a_file_ledger_is_private_and_written_atomically(tmp_path: Path) -> None:
    store = FileLedger(tmp_path / "state")
    assert store.load("release") == {}
    run([item("api")], Script({}), store)
    path = tmp_path / "state" / "release.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["items"][item("api").key]["state"] == "done"
    assert [p.name for p in (tmp_path / "state").iterdir()] == ["release.json"]
    path.write_text("not json")
    with pytest.raises(CopySetError):
        store.load("release")
    with pytest.raises(CopySetError):
        store.load("../escape")


class FakeApi:
    """The ConfigMap calls of a Kubernetes ``ApiClient``."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def call_api(self, path: str, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        name = path.rsplit("/", 1)[-1] if not path.endswith("configmaps") else None
        if method == "GET":
            if name not in self.maps:
                raise ApiException(status=404)
            return _Response(self.maps[name])
        body = kwargs["body"]
        if method == "POST":
            name = body["metadata"]["name"]
            body["metadata"].update(uid="u1", resourceVersion="1")
            self.maps[name] = body
            return _Response(body)
        assert method == "PATCH" and name is not None
        data = self.maps[name].setdefault("data", {})
        for key, value in body["data"].items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return _Response(self.maps[name])


class _Response:
    def __init__(self, value: Any) -> None:
        self.data = json.dumps(value).encode()


def test_a_configmap_ledger_keeps_the_set_beside_it() -> None:
    api = FakeApi()
    store = ConfigMapLedger(api, "piceli-system")
    assert store.load("release") == {}
    store.put("release", {"set.json": "{}"})
    run([item("api")], Script({}), store)
    data = api.maps["piceli-copy-release"]["data"]
    assert set(data) == {"set.json", "ledger.json"}
    assert store.load("release")["items"][item("api").key]["state"] == "done"
    skipped = Script({})
    run([item("api")], skipped, store)
    assert skipped.calls == []


# ----------------------------------------------------------------- the Job
def test_the_job_has_no_secret_a_deadline_and_the_copy_command() -> None:
    manifest = job_manifest(
        name="piceli-copy-release-0123456789",
        namespace="piceli-system",
        image=IMAGE,
        ledger="release",
        configmap="piceli-copy-release",
        credentials_secret="copy-credentials",
        attempts=4,
        deadline=900,
    )
    spec = manifest["spec"]
    assert manifest["kind"] == "Job" and manifest["apiVersion"] == "batch/v1"
    assert spec["backoffLimit"] == 0 and spec["activeDeadlineSeconds"] == 900
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == IMAGE
    assert container["command"] == ["piceli", "artifacts", "copy-job-run"]
    assert container["args"] == [
        "--dir",
        copy_cluster.SET_DIR,
        "--ledger",
        "release",
        "--attempts",
        "4",
    ]
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    credentials = {
        e["name"]: e["valueFrom"]["secretKeyRef"]
        for e in container["env"]
        if "valueFrom" in e
    }
    assert credentials["PICELI_COPY_TARGET_PASSWORD"] == {
        "name": "copy-credentials",
        "key": "target-password",
        "optional": True,
    }
    assert set(credentials) == {
        f"PICELI_COPY_{role}_{field}"
        for role in ("SOURCE", "TARGET")
        for field in ("USERNAME", "PASSWORD", "TOKEN")
    }
    assert all("value" not in e for e in container["env"] if "valueFrom" in e)
    assert pod["volumes"][0] == {
        "name": "set",
        "configMap": {"name": "piceli-copy-release"},
    }
    # A credentials-free copy has no credential variables at all.
    plain = job_manifest(
        name="n",
        namespace="piceli-system",
        image=IMAGE,
        ledger="release",
        configmap="c",
        credentials_secret=None,
        attempts=1,
        deadline=60,
    )
    assert not [
        e
        for e in plain["spec"]["template"]["spec"]["containers"][0]["env"]
        if "valueFrom" in e
    ]


def test_the_job_name_follows_the_ledger_and_the_set() -> None:
    items = [item("api"), item("web")]
    name = job_name("release", items)
    assert name == job_name("release", items)
    assert name != job_name("release", items[:1])
    assert name != job_name("other", items)
    assert name.startswith("piceli-copy-release-") and len(name) <= 63


def test_a_loopback_registry_is_the_cluster_service_inside_the_job() -> None:
    assert registry_service("piceli-system") == "piceli-registry.piceli-system.svc:5000"
    source = f"oci://127.0.0.1:5000/shop/api@{DIGEST}"
    moved = rewrite_for_cluster(
        CopyItem(source, "oci://ghcr.io/example/api:1.0", "/secret/path", "/other"),
        "piceli-system",
    )
    assert (
        moved.source
        == f"oci://piceli-registry.piceli-system.svc:5000/shop/api@{DIGEST}"
    )
    assert moved.target == "oci://ghcr.io/example/api:1.0"
    assert moved.source_credentials is None and moved.target_credentials is None
    local = rewrite_for_cluster(
        CopyItem(f"oci://localhost:5001/shop/api@{DIGEST}", "oci://127.0.0.1:5000/x:2"),
        "builds",
    )
    assert local.source.startswith("oci://piceli-registry.builds.svc:5000/shop/api@")
    assert local.target == "oci://piceli-registry.builds.svc:5000/x:2"
    # The rewritten plan is a valid plan.
    CopyPlan.parse(moved.source, moved.target)


def test_credentials_travel_as_secret_keys_and_environment_variables() -> None:
    data = secret_data(
        RegistryCredentials("reader", "pull-secret"), RegistryCredentials(token="tok")
    )
    assert data == {
        "source-username": "reader",
        "source-password": "pull-secret",
        "target-token": "tok",
    }
    environ = {
        "PICELI_COPY_SOURCE_USERNAME": "reader",
        "PICELI_COPY_SOURCE_PASSWORD": "pull-secret",
        "PICELI_COPY_TARGET_TOKEN": "tok",
    }
    assert credentials_from_env(environ, "source") == RegistryCredentials(
        "reader", "pull-secret"
    )
    assert credentials_from_env(environ, "target") == RegistryCredentials(token="tok")
    assert credentials_from_env({}, "source") is None
    assert secret_data(None, None) == {}


# ------------------------------------------------------- the cluster, faked
class FakeCluster:
    """What ``CopyCluster`` does against the API. A created Job runs the real
    ``run_job`` on the set the client wrote, with a fake copy, when it is first
    looked at; its log is the lines the Job printed."""

    namespace = "piceli-system"
    poll_seconds = 0.0

    def __init__(self, tmp_path: Path, script: Script) -> None:
        self.directory = tmp_path / "mounted"
        self.directory.mkdir(exist_ok=True)
        self.script = script
        self.jobs: dict[str, dict[str, Any]] = {}
        self.configmaps: dict[str, dict[str, str]] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []
        self.deleted_secrets: list[str] = []
        self.lines: dict[str, list[str]] = {}

    @staticmethod
    def clock() -> float:
        import time

        return time.monotonic()

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None

    def close(self) -> None:
        return None

    def get_job(self, name: str) -> dict[str, Any] | None:
        return self.jobs.get(name)

    @staticmethod
    def finished(job: dict[str, Any]) -> bool:
        return job["state"] != "running"

    def read_object(self, kind: str, name: str) -> set[str] | None:
        assert kind == "Secret"
        return set(self.secrets[name]) if name in self.secrets else None

    def controller_image(self) -> str:
        return IMAGE

    def put_configmap(self, ledger: str, keys: dict[str, str]) -> None:
        self.configmaps.setdefault(ledger, {}).update(keys)

    def apply_secret(self, name: str, data: dict[str, str]) -> None:
        self.secrets[name] = dict(data)

    def delete_secret(self, name: str) -> None:
        self.deleted_secrets.append(name)
        self.secrets.pop(name, None)

    def create_job(self, job: dict[str, Any]) -> None:
        self.jobs[job["metadata"]["name"]] = {"state": "running", "manifest": job}

    def _execute(self, name: str) -> None:
        job = self.jobs[name]
        args = job["manifest"]["spec"]["template"]["spec"]["containers"][0]["args"]
        ledger = args[args.index("--ledger") + 1]
        attempts = int(args[args.index("--attempts") + 1])
        (self.directory / "set.json").write_text(self.configmaps[ledger]["set.json"])
        lines: list[str] = []
        report = run_job(
            self.directory,
            ledger=ledger,
            attempts=attempts,
            environ={},
            emit=lines.append,
            sleep=lambda _s: None,
            copy=self.script,
        )
        self.lines[name] = lines
        job["state"] = "passed" if report.ok else "failed"

    def outcome(self, name: str, _memo: dict[str, Any]) -> Outcome:
        job = self.jobs[name]
        if job["state"] == "running":
            self._execute(name)
        if job["state"] == "passed":
            return Outcome("passed", exit_code=0)
        return Outcome("failed", exit_code=1)

    def logs(self, name: str) -> str:
        return "\n".join(self.lines.get(name, []))

    def delete_job(self, name: str) -> bool:
        self.deleted.append(name)
        self.jobs.pop(name, None)
        return True


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


def in_cluster(
    cluster: FakeCluster, api: FakeApi, items: list[CopyItem], **extra: Any
) -> CopyReport:
    return copy_in_cluster(
        items,
        ledger=extra.pop("ledger", "release"),
        namespace="piceli-system",
        credentials_secret=extra.pop("credentials_secret", None),
        kube=api,
        attempts=extra.pop("attempts", 5),
        deadline=extra.pop("deadline", 600.0),
        cluster=cluster,  # type: ignore[arg-type]
        **extra,
    )


def test_the_job_runs_the_set_and_the_client_reads_ledger_and_report(
    tmp_path: Path, api: FakeApi
) -> None:
    cluster = FakeCluster(tmp_path, Script({}))
    items = [
        CopyItem(
            f"oci://127.0.0.1:5000/shop/api@{DIGEST}", "oci://ghcr.io/example/api:1.0"
        ),
        item("web"),
    ]
    report = in_cluster(cluster, api, items)
    assert report.ok and [i.state for i in report.items] == ["done", "done"]
    assert report.items[0].receipt is not None
    name = job_name("release", [rewrite_for_cluster(i, "piceli-system") for i in items])
    assert report.job is not None and report.job["name"] == name
    assert report.job["attached"] is False
    # The Job got the rewritten set; the ledger came back to the ConfigMap.
    sent = json.loads(cluster.configmaps["release"]["set.json"])
    assert sent["items"][0]["source"].startswith(
        "oci://piceli-registry.piceli-system.svc:5000/shop/api@"
    )
    ledger = json.loads(api.maps["piceli-copy-release"]["data"]["ledger.json"])
    assert {r["state"] for r in ledger["items"].values()} == {"done"}
    assert cluster.deleted == [name]  # a passed Job is removed
    assert cluster.script.calls[0].endswith("api:1.0")


def test_the_job_log_lines_are_the_ledger_and_the_report(
    tmp_path: Path, api: FakeApi
) -> None:
    cluster = FakeCluster(tmp_path, Script({}))
    in_cluster(cluster, api, [item("api")])
    lines = next(iter(cluster.lines.values())) if cluster.lines else []
    assert lines[-1].startswith(RESULT_MARKER)
    assert any(line.startswith(LEDGER_MARKER) for line in lines[:-1])
    result = decode(lines[-1][len(RESULT_MARKER) :])
    assert result["revision"] == "piceli.copy-set.v1" and result["ok"] is True
    assert "password" not in json.dumps(result)


def test_a_failed_job_keeps_its_ledger_and_the_rerun_continues(
    tmp_path: Path, api: FakeApi
) -> None:
    items = [item("api"), item("web")]
    broken = Script({item("web").target: [failed("copy-tag-exists")]})
    cluster = FakeCluster(tmp_path, broken)
    report = in_cluster(cluster, api, items)
    assert not report.ok and report.failed == "copy-tag-exists"
    assert [i.state for i in report.items] == ["done", "failed"]
    assert cluster.deleted == []  # kept: its log has the detail
    # The same command again: the failed Job is read and replaced, api is not
    # copied again, web is.
    fixed = Script({})
    again = FakeCluster(tmp_path, fixed)
    again.jobs = cluster.jobs
    again.lines = cluster.lines
    again.configmaps = cluster.configmaps
    report = in_cluster(again, api, items)
    assert report.ok and fixed.calls == [item("web").target]
    assert [i.skipped for i in report.items] == [True, False]


def test_an_in_cluster_rerun_of_a_done_ledger_reports_the_receipts(
    tmp_path: Path, api: FakeApi
) -> None:
    """0.87.0: every copy done, no Job runs, and each item still says what
    was copied (the ledger keeps a compact receipt)."""
    items = [item("api")]
    in_cluster(FakeCluster(tmp_path, Script({})), api, items)
    idle = Script({})
    report = in_cluster(FakeCluster(tmp_path, idle), api, items)
    assert idle.calls == [] and report.ok
    found = report.items[0]
    assert found.skipped and found.receipt is not None
    assert found.receipt["target"]["tag"] == "1.0"
    assert found.receipt["digest"] == DIGEST and found.receipt["from_ledger"]


def test_a_client_killed_after_the_job_ended_reads_its_result(
    tmp_path: Path, api: FakeApi
) -> None:
    items = [item("api")]
    first = FakeCluster(tmp_path, Script({}))
    in_cluster(first, api, items)
    name = job_name("release", items)
    # The Job finished, the client never saw it: its Job and log are there.
    second = FakeCluster(tmp_path, Script({}))
    second.jobs[name] = {"state": "passed", "manifest": {}}
    second.lines[name] = first.lines[name]
    second.configmaps = first.configmaps
    store = MemoryLedger()
    report = in_cluster(second, api, items, store=store)
    assert report.ok and report.job is not None and report.job["attached"] is True
    assert report.items[0].receipt is not None
    assert second.script.calls == []  # nothing was copied again
    assert store.load("release")["items"][items[0].key]["state"] == "done"
    assert second.deleted == [name]


def test_a_client_killed_while_the_job_runs_attaches_to_it(
    tmp_path: Path, api: FakeApi
) -> None:
    items = [item("api")]
    name = job_name("release", items)
    cluster = FakeCluster(tmp_path, Script({}))
    cluster.jobs[name] = {"state": "running", "manifest": {}}
    cluster.configmaps["release"] = {}
    # Nothing creates a second Job: the running one is waited for. (The fake
    # Job runs the set it finds mounted.)
    cluster.script = Script({})
    cluster.configmaps["release"]["set.json"] = json.dumps(
        {
            "revision": copy_cluster.SET_REVISION,
            "items": [rewrite_for_cluster(items[0], "piceli-system").to_dict()],
            "options": {},
            "ledger": {},
        }
    )
    cluster.jobs[name]["manifest"] = job_manifest(
        name=name,
        namespace="piceli-system",
        image=IMAGE,
        ledger="release",
        configmap="piceli-copy-release",
        credentials_secret=None,
        attempts=2,
        deadline=60,
    )
    report = in_cluster(cluster, api, items)
    assert report.ok and report.job is not None and report.job["attached"] is True
    assert cluster.deleted == [name]  # one Job only: the running one


def test_a_ledger_that_is_all_done_starts_no_job(tmp_path: Path, api: FakeApi) -> None:
    items = [item("api")]
    store = MemoryLedger()
    run(items, Script({}), store)
    cluster = FakeCluster(tmp_path, Script({}))
    report = in_cluster(cluster, api, items, store=store)
    assert report.ok and cluster.jobs == {} and cluster.script.calls == []


def test_a_missing_credentials_secret_is_refused_before_a_job(
    tmp_path: Path, api: FakeApi
) -> None:
    cluster = FakeCluster(tmp_path, Script({}))
    with pytest.raises(CopySetError):
        in_cluster(cluster, api, [item("api")], credentials_secret="absent")
    assert cluster.jobs == {}
    cluster.secrets["present"] = {"target-token": "x"}
    assert in_cluster(cluster, api, [item("api")], credentials_secret="present").ok


def test_a_job_with_no_report_is_a_failed_copy(tmp_path: Path, api: FakeApi) -> None:
    class Crashing(FakeCluster):
        def outcome(self, name: str, _memo: dict[str, Any]) -> Outcome:
            self.jobs[name]["state"] = "failed"
            return Outcome("failed", exit_code=137, reason="OOMKilled")

    cluster = Crashing(tmp_path, Script({}))
    report = in_cluster(cluster, api, [item("api"), item("web")])
    assert not report.ok and report.failed == "OOMKilled"
    assert [i.state for i in report.items] == ["failed", "pending"]


# ----------------------------------------------------------------- the CLI
def parse(*arguments: str) -> Any:
    return build_parser().parse_args(["copy", *arguments])


def test_the_new_flags_parse_with_their_defaults() -> None:
    args = parse("--from", "a", "--to", "b")
    assert args.attempts == 5 and args.set_file is None and args.ledger is None
    assert args.in_cluster is False and args.namespace == "piceli-system"
    assert args.credentials_secret is None and args.ledger_dir is None
    args = parse(
        "--set", "s.toml", "--attempts", "3", "--ledger", "rel", "--ledger-dir", "d",
        "--in-cluster", "--namespace", "ns", "--credentials-secret", "sec",
        "--kubeconfig", "k", "--context", "c", "--image", IMAGE,
    )  # fmt: skip
    assert args.set_file == Path("s.toml") and args.attempts == 3
    assert args.ledger == "rel" and args.ledger_dir == Path("d")
    assert (
        args.in_cluster and args.namespace == "ns" and args.credentials_secret == "sec"
    )
    hidden = build_parser().parse_args(
        ["copy-job-run", "--dir", "d", "--ledger", "rel", "--attempts", "2"]
    )
    assert hidden.command == "copy-job-run" and hidden.attempts == 2


def run_cli(
    capsys: pytest.CaptureFixture[str], *arguments: str, **seams: Any
) -> tuple[int, Any, str]:
    code = run_copy_command(parse(*arguments), **seams)
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else None, err


@pytest.fixture
def set_file(tmp_path: Path) -> Path:
    path = tmp_path / "images.json"
    path.write_text(
        json.dumps(
            [
                {"from": item("api").source, "to": item("api").target},
                {"from": item("web").source, "to": item("web").target},
            ]
        )
    )
    return path


def fake_copies(monkeypatch: pytest.MonkeyPatch, script: Script) -> None:
    monkeypatch.setattr(
        "piceli.artifacts.copy_set.prepare_copies", lambda *a, **k: script
    )


def test_the_approval_of_a_set_names_the_whole_set(
    capsys: pytest.CaptureFixture[str], set_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script({})
    fake_copies(monkeypatch, script)
    code, plan, err = run_cli(capsys, "--set", str(set_file))
    assert code == 3 and plan["state"] == "approval-required"
    assert len(plan["items"]) == 2 and plan["plan_hash"] == plan["plan_digest"]
    assert f"--approve {plan['plan_hash']}" in err and script.calls == []
    # The approval of one of the copies does not approve the set.
    single = CopyPlan.parse(item("api").source, item("api").target).digest
    code, refused, _ = run_cli(capsys, "--set", str(set_file), "--approve", single)
    assert code == 2 and refused["reason"] == "copy-not-approved"
    assert script.calls == []
    code, report, _ = run_cli(
        capsys, "--set", str(set_file), "--approve", plan["plan_hash"]
    )
    assert code == 0 and report["revision"] == "piceli.copy-set.v1"
    assert [i["state"] for i in report["items"]] == ["done", "done"]
    # --from and --to with --set is one or the other.
    code, bad, _ = run_cli(
        capsys, "--set", str(set_file), "--from", item("api").source, "--to", "x"
    )
    assert code == 2 and bad["reason"] == "copy-invalid"
    code, bad, _ = run_cli(capsys, "--from", item("api").source)
    assert code == 2 and bad["reason"] == "copy-invalid"


def test_a_single_copy_still_prints_its_plan_and_receipt_as_before(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Script({})
    fake_copies(monkeypatch, script)
    one = item("api")
    code, plan, err = run_cli(capsys, "--from", one.source, "--to", one.target)
    legacy = CopyPlan.parse(one.source, one.target)
    assert code == 3 and plan["plan_hash"] == legacy.digest
    assert f"; approve with --approve {legacy.digest}" in err
    code, receipt, _ = run_cli(
        capsys, "--from", one.source, "--to", one.target, "--approve", legacy.digest
    )
    assert code == 0 and receipt["revision"] == "piceli.copy-receipt.v1"
    assert "copy_retries" not in receipt


def test_the_cli_retries_a_transient_failure_and_reports_the_receipt(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    one = item("api")
    script = Script({one.target: [failed("registry-unreachable")] * 2 + [copied()]})
    fake_copies(monkeypatch, script)
    sleeps: list[float] = []
    digest = CopyPlan.parse(one.source, one.target).digest
    code, receipt, _ = run_cli(
        capsys, "--from", one.source, "--to", one.target, "--approve", digest,
        "--attempts", "3", sleep=sleeps.append,
    )  # fmt: skip
    assert code == 0 and receipt["state"] == "copied" and len(sleeps) == 2
    assert len(receipt["copy_retries"]) == 2
    # One attempt: the failure is the answer, with the old failure line.
    script = Script({one.target: [failed("registry-unreachable")]})
    fake_copies(monkeypatch, script)
    code, receipt, err = run_cli(
        capsys, "--from", one.source, "--to", one.target, "--approve", digest,
        "--attempts", "1", sleep=sleeps.append,
    )  # fmt: skip
    assert code == 1 and receipt["reason"] == "registry-unreachable"
    assert "copy: failed [registry-unreachable]" in err and len(script.calls) == 1


def test_a_ledger_resumes_a_set_across_cli_runs(
    capsys: pytest.CaptureFixture[str],
    set_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = CopySetPlan.parse(load_set_file(set_file), CopyOptions()).digest
    arguments = [
        "--set", str(set_file), "--approve", plan,
        "--ledger", "release", "--ledger-dir", str(tmp_path / "ledgers"),
    ]  # fmt: skip
    fake_copies(monkeypatch, Script({item("web").target: [failed("copy-tag-exists")]}))
    code, report, err = run_cli(capsys, *arguments)
    assert code == 1 and report["failed"] == "copy-tag-exists"
    assert "failed [copy-tag-exists]" in err
    saved = json.loads((tmp_path / "ledgers" / "release.json").read_text())
    assert saved["items"][item("api").key]["state"] == "done"
    assert saved["items"][item("web").key]["state"] == "failed"
    resumed = Script({})
    fake_copies(monkeypatch, resumed)
    code, report, _ = run_cli(capsys, *arguments)
    assert code == 0 and resumed.calls == [item("web").target]
    assert [i["skipped"] for i in report["items"]] == [True, False]


def test_in_cluster_copies_run_as_a_job_and_print_the_receipt(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    api: FakeApi,
) -> None:
    source = f"oci://127.0.0.1:5000/shop/api@{DIGEST}"
    to = "oci://ghcr.io/example/api:1.0"
    digest = CopyPlan.parse(source, to).digest
    cluster = FakeCluster(tmp_path, Script({}))
    common = [
        "--from", source, "--to", to, "--in-cluster",
        "--kubeconfig", str(tmp_path / "kubeconfig"), "--context", "lab",
    ]  # fmt: skip
    code, plan, _ = run_cli(capsys, *common, kube=api, cluster=cluster)
    assert code == 3 and cluster.jobs == {} and cluster.configmaps == {}
    code, receipt, _ = run_cli(
        capsys, *common, "--approve", digest, kube=api, cluster=cluster
    )
    assert code == 0 and receipt["state"] == "copied"
    # Named by the approved plan, so the same command finds the same Job.
    ledger = f"set-{digest.split(':')[1][:12]}"
    assert ledger in cluster.configmaps
    assert (
        "piceli-registry.piceli-system.svc:5000/shop/api"
        in cluster.configmaps[ledger]["set.json"]
    )
    assert cluster.secrets == {}  # no credentials: no Secret


def test_in_cluster_credentials_go_to_a_secret_that_is_removed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, api: FakeApi
) -> None:
    pushing = tmp_path / "push.json"
    pushing.write_text(json.dumps({"username": "robot", "password": "hunter2-secret"}))
    pushing.chmod(0o600)
    source = f"oci://127.0.0.1:5000/shop/api@{DIGEST}"
    to = "oci://ghcr.io/example/api:1.0"
    digest = CopyPlan.parse(source, to).digest
    seen: list[dict[str, str]] = []

    class Recording(FakeCluster):
        def apply_secret(self, name: str, data: dict[str, str]) -> None:
            seen.append(dict(data))
            super().apply_secret(name, data)

    cluster = Recording(tmp_path, Script({}))
    code, receipt, err = run_cli(
        capsys, "--from", source, "--to", to, "--approve", digest, "--in-cluster",
        "--kubeconfig", str(tmp_path / "kubeconfig"), "--context", "lab",
        "--credentials", str(pushing), "--ledger", "rel", kube=api, cluster=cluster,
    )  # fmt: skip
    assert code == 0, receipt
    assert seen == [{"target-username": "robot", "target-password": "hunter2-secret"}]
    assert cluster.deleted_secrets == ["piceli-copy-rel-credentials"]
    everything = json.dumps([cluster.configmaps, api.maps, receipt, err], default=str)
    assert "hunter2-secret" not in everything


def test_in_cluster_needs_an_explicit_kubeconfig_and_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = f"oci://127.0.0.1:5000/shop/api@{DIGEST}"
    to = "oci://ghcr.io/example/api:1.0"
    digest = CopyPlan.parse(source, to).digest
    code, refused, _ = run_cli(
        capsys, "--from", source, "--to", to, "--approve", digest, "--in-cluster"
    )
    assert code == 2 and refused["reason"] == "copy-invalid"


def test_the_job_command_reads_the_mounted_set(tmp_path: Path) -> None:
    items = [item("api")]
    (tmp_path / "set.json").write_text(
        json.dumps(
            {
                "revision": copy_cluster.SET_REVISION,
                "items": [i.to_dict() for i in items],
                "options": {"referrers": False},
                "ledger": {},
            }
        )
    )
    lines: list[str] = []
    report = run_job(
        tmp_path,
        ledger="release",
        attempts=2,
        environ={},
        emit=lines.append,
        copy=Script({}),
    )
    assert report.ok and lines[-1].startswith(RESULT_MARKER)
    (tmp_path / "set.json").write_text("{}")
    with pytest.raises(CopySetError):
        run_job(tmp_path, ledger="release", attempts=2, environ={}, emit=lines.append)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A test never waits for a real pause."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    yield
