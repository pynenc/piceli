"""Stage 31 of the k3s lifecycle acceptance: the controller's OpenTelemetry (0.16).

31. An OpenTelemetry Collector (pinned by digest; OTLP gRPC and HTTP in, a
    bearer token required, the ``file`` exporter writing JSON lines that a
    sidecar reads) runs in the cluster. ``Controller(telemetry=Otlp(...))``
    in ``infra.py`` with a headers Secret, the ``telemetry`` group (the
    Secret, ``gitops enable``), then:

    - a successful deploy of main: one trace, the root span ``SYNC
      lifecycle/main`` with per-stage children (build per image, plan,
      restore point, pre-rollout, apply, checks with one child per check),
      ``piceli.deploy.rolled`` in the trace;
    - a failed check with rollback: result ``rolled-back``, the failing
      check's span in error with its detail, a ``rollback`` span,
      ``piceli.deploy.checks.failed`` and ``piceli.deploy.rollback``;
    - an approval wait (``promote rc``, then ``gitops approve rc``): the
      ``approval wait`` span, ``approval.required`` and ``approved`` (cli)
      in the same trace;
    - ``env stop rc`` / ``env start rc``: ``piceli.environment.stopped`` and
      ``.started``;
    - a broken build in a ``wp-*`` branch: ``piceli.build.failed`` with at
      most 20 lines of the redacted log tail;
    - the controller killed mid-build: its restart closes the run as
      ``interrupted`` in the trace whose id it had persisted;
    - every event has a ``piceli.event.id`` of ``<cluster>:<env>/<run>:…``,
      every trace id is the one derived from the cluster, environment and
      run, children link to their parents;
    - the CI/CD metrics arrive (``cicd.pipeline.run.duration``, ``.active``,
      ``cicd.worker.count``, Piceli's heartbeat);
    - with the Collector scaled to 0 a deploy still succeeds; scaled back,
      data arrives again;
    - the bearer token is in no log line and no exported attribute.

The Collector's namespace is deleted whatever the outcome.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from lifecycle_support import check, log, wait_for

NAMESPACE = "lc-otel"
COLLECTOR = "otel-collector"
#: otel/opentelemetry-collector-contrib:0.161.0 (index digest, amd64 + arm64).
COLLECTOR_IMAGE = (
    "docker.io/otel/opentelemetry-collector-contrib:0.161.0"
    "@sha256:fd328de2552466ad78385e1b1289c3f2402b1c45f265b252aab1955b42845ac1"
)
ENDPOINT = f"http://{COLLECTOR}.{NAMESPACE}.svc:4317"
HEADERS_SECRET = "otlp-auth"
DATA = "/data/otel.jsonl"
CLUSTER = "lifecycle"
#: The reader sidecar (the lifecycle's pinned busybox).
BUSYBOX = (
    "docker.io/library/busybox:1.37.0"
    "@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
FAILING_CHECK = """EXTRA_CHECKS: list = [
    Checks.exec(
        "statefulset/store",
        ["cat", "/var/lib/store/no-such-file"],
        name="otel-always-fails",
    ),
]"""
REPORTER_COMMAND = '"while true; do sleep 3600; done"'
EVENT_ID = re.compile(r"lifecycle:[a-z0-9-]+/\d{8}T\d{9}Z:[a-z0-9:-]+")


def _value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values") or []]
    return None


def _attrs(items: Any) -> dict[str, Any]:
    return {item["key"]: _value(item.get("value") or {}) for item in items or []}


class Telemetry:
    """What the Collector wrote: spans, log records (events) and metric names."""

    def __init__(self, text: str) -> None:
        self.spans: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.metrics: dict[str, list[dict[str, Any]]] = {}
        self.raw = text
        for line in text.splitlines():
            try:
                body = json.loads(line)
            except ValueError:
                continue
            for resource in body.get("resourceSpans") or []:
                where = _attrs((resource.get("resource") or {}).get("attributes"))
                for scope in resource.get("scopeSpans") or []:
                    for span in scope.get("spans") or []:
                        self.spans.append(
                            {**span, "attributes": _attrs(span.get("attributes")),
                             "resource": where}
                        )  # fmt: skip
            for resource in body.get("resourceLogs") or []:
                for scope in resource.get("scopeLogs") or []:
                    for record in scope.get("logRecords") or []:
                        attrs = _attrs(record.get("attributes"))
                        name = record.get("eventName") or attrs.get("event.name")
                        self.events.append(
                            {**record, "name": name, "attributes": attrs}
                        )
            for resource in body.get("resourceMetrics") or []:
                for scope in resource.get("scopeMetrics") or []:
                    for metric in scope.get("metrics") or []:
                        points = []
                        for kind in ("sum", "gauge", "histogram"):
                            for point in (metric.get(kind) or {}).get(
                                "dataPoints"
                            ) or []:
                                points.append(
                                    {
                                        **point,
                                        "attributes": _attrs(point.get("attributes")),
                                    }
                                )
                        self.metrics.setdefault(metric["name"], []).extend(points)

    def roots(self, name: str, **attributes: Any) -> list[dict[str, Any]]:
        return [
            span
            for span in self.spans
            if span["name"] == name
            and not span.get("parentSpanId")
            and all(span["attributes"].get(k) == v for k, v in attributes.items())
        ]

    def trace(self, trace_id: str) -> dict[str, dict[str, Any]]:
        return {s["name"]: s for s in self.spans if s["traceId"] == trace_id}

    def named(self, name: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["name"] == name]

    def with_event(self, root: str, event: str, **attributes: Any) -> Telemetry | None:
        """Itself once the newest ``root`` span and its ``event`` both arrived
        (spans and log records are exported in separate batches)."""
        roots = self.roots(root, **attributes)
        traces = {item.get("traceId") for item in self.named(event)}
        return self if roots and roots[-1]["traceId"] in traces else None


class OtelStages:
    """Stage 31; mixed into ``Lifecycle``."""

    otel_token = ""

    def _ok(self, *args: str, **kwargs: Any) -> Any:
        return self.kubectl(*args, **kwargs)  # type: ignore[attr-defined]

    def _collector_objects(self, token: str) -> list[dict[str, Any]]:
        config = {
            "extensions": {"bearertokenauth": {"token": token}},
            "receivers": {
                "otlp": {
                    "protocols": {
                        "grpc": {
                            "endpoint": "0.0.0.0:4317",
                            "auth": {"authenticator": "bearertokenauth"},
                        },
                        "http": {
                            "endpoint": "0.0.0.0:4318",
                            "auth": {"authenticator": "bearertokenauth"},
                        },
                    }
                }
            },
            "exporters": {"file": {"path": DATA}, "debug": {}},
            "service": {
                "extensions": ["bearertokenauth"],
                "pipelines": {
                    kind: {"receivers": ["otlp"], "exporters": ["file", "debug"]}
                    for kind in ("traces", "logs", "metrics")
                },
            },
        }
        labels = {"app.kubernetes.io/name": COLLECTOR}
        return [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "collector-config", "namespace": NAMESPACE},
                "stringData": {"config.yaml": json.dumps(config)},
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": COLLECTOR, "namespace": NAMESPACE, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "securityContext": {"fsGroup": 10001},
                            "containers": [
                                {
                                    "name": "collector",
                                    "image": COLLECTOR_IMAGE,
                                    "args": ["--config=/etc/otelcol/config.yaml"],
                                    "ports": [
                                        {"containerPort": 4317, "name": "otlp-grpc"},
                                        {"containerPort": 4318, "name": "otlp-http"},
                                    ],
                                    "volumeMounts": [
                                        {"name": "config", "mountPath": "/etc/otelcol"},
                                        {"name": "data", "mountPath": "/data"},
                                    ],
                                },
                                {
                                    # Reads the exporter's file (the collector image has no shell).
                                    "name": "reader",
                                    "image": BUSYBOX,
                                    "command": ["sh", "-c", "while true; do sleep 3600; done"],
                                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                                },
                            ],
                            "volumes": [
                                {"name": "config", "secret": {"secretName": "collector-config"}},
                                {"name": "data", "emptyDir": {}},
                            ],
                        },
                    },
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": COLLECTOR, "namespace": NAMESPACE},
                "spec": {
                    "selector": labels,
                    "ports": [
                        {"name": "otlp-grpc", "port": 4317, "targetPort": 4317},
                        {"name": "otlp-http", "port": 4318, "targetPort": 4318},
                    ],
                },
            },
        ]  # fmt: skip

    def _collector_ready(self, replicas: int = 1) -> None:
        def ready() -> bool:
            found = (
                self.cluster.get(  # type: ignore[attr-defined]
                    "deployment", COLLECTOR, "-n", NAMESPACE
                )
                or {}
            )
            status = found.get("status") or {}
            return int(status.get("readyReplicas") or 0) == replicas

        wait_for(f"the Collector at {replicas} ready", ready, timeout=600, interval=5)

    def _collected(self) -> Telemetry:
        found = self._ok(
            "exec", "-n", NAMESPACE, f"deploy/{COLLECTOR}", "-c", "reader", "--",
            "cat", DATA, check_exit=None, timeout=120,
        )  # fmt: skip
        return Telemetry(found.stdout if found.code == 0 else "")

    def _wait_data(
        self, what: str, probe: Callable[[Telemetry], Any], timeout: float = 300
    ) -> Any:
        return wait_for(
            what, lambda: probe(self._collected()), timeout=timeout, interval=10
        )

    def _controller_file(self, path: str) -> str:
        found = self._ok(
            "exec", "-n", "piceli-system", "deploy/piceli-gitops", "--", "cat", path,
            check_exit=None, timeout=60,
        )  # fmt: skip
        return str(found.stdout) if found.code == 0 else ""

    def _children_linked(
        self,
        data: Telemetry,
        root: dict[str, Any],
        names: list[str],
        problems: list[str],
    ) -> dict[str, dict[str, Any]]:
        tree = data.trace(root["traceId"])
        for name in names:
            span = tree.get(name)
            if span is None:
                problems.append(
                    f"{root['name']}: no child span {name!r} (has {sorted(tree)})"
                )
            elif not span.get("parentSpanId"):
                problems.append(f"{name}: no parent")
        for span in tree.values():
            parent = span.get("parentSpanId")
            if parent and parent not in {s["spanId"] for s in tree.values()}:
                problems.append(f"{span['name']}: parent {parent} not in its trace")
            if int(span["endTimeUnixNano"]) < int(span["startTimeUnixNano"]):
                problems.append(f"{span['name']}: ends before it starts")
        return tree

    def _derived(self, root: dict[str, Any], problems: list[str]) -> None:
        """The trace id is the one derived from the cluster, environment and run."""
        from piceli.gitops.otel import trace_id_of

        run_id = str(root["attributes"].get("cicd.pipeline.run.id") or "")
        env, _, run = run_id.partition("/")
        if f"{trace_id_of(CLUSTER, env, run):032x}" != root["traceId"]:
            problems.append(
                f"{root['name']}: trace id {root['traceId']} is not derived"
            )

    def stage_31_otel(self) -> None:
        self.otel_token = secrets.token_urlsafe(24)  # throwaway, never printed
        problems: list[str] = []
        try:
            self._stage_31(problems)
        finally:
            self._ok("delete", "namespace", NAMESPACE, "--wait=false", check_exit=None)
        check(not problems, "; ".join(problems))

    def _stage_31(self, problems: list[str]) -> None:
        token = self.otel_token
        self.cluster.apply(self._collector_objects(token))  # type: ignore[attr-defined]
        self._collector_ready()
        header = self.scratch / "otlp-authorization"  # type: ignore[attr-defined]
        header.write_text(f"Bearer {token}")
        header.chmod(0o600)
        self.values["otlp_header_file"] = str(header)  # type: ignore[attr-defined]
        site = self._site(  # type: ignore[attr-defined]
            self.candidate_image,  # type: ignore[attr-defined]
            telemetry=f"Otlp({ENDPOINT!r}, insecure=True, headers_secret={HEADERS_SECRET!r})",
        )
        infra_sha = self.repos.commit(  # type: ignore[attr-defined]
            "infra", "send the controller's telemetry", {"lifecycle_site.py": site}
        )
        since = self.mark()  # type: ignore[attr-defined]
        self.run_group("telemetry")  # type: ignore[attr-defined]
        header.unlink()
        self._wait_controller(  # type: ignore[attr-defined]
            self.candidate_image,
            names=("piceli-gitops",),  # type: ignore[attr-defined]
        )
        wait_for(
            "the controller exporting",
            lambda: "telemetry: OTLP grpc to" in self.controller_log(since),  # type: ignore[attr-defined]
            timeout=300,
        )
        self._main_at(infra_sha, "main at the telemetry commit")  # type: ignore[attr-defined]
        data = self._wait_data(
            "piceli.controller.started at the Collector",
            lambda d: d.named("piceli.controller.started") and d,
        )
        resource = (data.spans or [{}])[0].get("resource") or {}
        log(
            f"collector: {len(data.spans)} span(s), {len(data.events)} event(s), metrics {sorted(data.metrics)[:12]}"
        )

        # --- a successful deploy (one source changed)
        run = self.repos.read("store", "bin/run.sh")  # type: ignore[attr-defined]
        sha = self.repos.commit(  # type: ignore[attr-defined]
            "store", "store: telemetry edition",
            {"bin/run.sh": run.replace("# The store", "# The store (telemetry)", 1)},
        )  # fmt: skip
        self.wait_env(
            "main",
            self.deployed("main", {"store": sha}),
            timeout=1500,
            what="store change",
        )  # type: ignore[attr-defined]
        data = self._wait_data(
            "the deploy's trace",
            lambda d: [
                r for r in d.roots("SYNC lifecycle/main", **{"piceli.deploy.result": "success"})
                if sha in (r["attributes"].get("piceli.source.revisions") or [])
                and r["traceId"] in {e.get("traceId") for e in d.named("piceli.deploy.rolled")}
            ] and d,
        )  # fmt: skip
        root = next(
            r for r in data.roots("SYNC lifecycle/main", **{"piceli.deploy.result": "success"})
            if sha in (r["attributes"].get("piceli.source.revisions") or [])
        )  # fmt: skip
        resource = root["resource"]
        for key, wanted in (
            ("service.name", "piceli-controller"),
            ("k8s.cluster.name", CLUSTER),
        ):
            if resource.get(key) != wanted:
                problems.append(f"resource {key} {resource.get(key)!r}")
        if not resource.get("k8s.cluster.uid"):
            problems.append("resource has no k8s.cluster.uid")
        attrs = root["attributes"]
        for key, wanted in (
            ("cicd.pipeline.result", "success"), ("cicd.pipeline.action.name", "SYNC"),
            ("deployment.environment.name", "main"), ("k8s.namespace.name", "lc-main"),
            ("piceli.approval.via", "policy"), ("piceli.trigger.kind", "push"),
        ):  # fmt: skip
            if attrs.get(key) != wanted:
                problems.append(f"main root {key} {attrs.get(key)!r}")
        if root.get("kind") not in (2, "SPAN_KIND_SERVER"):
            problems.append(f"root kind {root.get('kind')}")
        tree = self._children_linked(
            data, root, ["build store", "plan", "restore point", "pre-rollout", "apply", "checks", "check store-ready"],
            problems,
        )  # fmt: skip
        if tree.get("check store-ready", {}).get("parentSpanId") != tree.get(
            "checks", {}
        ).get("spanId"):
            problems.append("check store-ready is not a child of checks")
        if attrs.get("k8s.statefulset.name") != "store":
            problems.append(
                f"k8s.statefulset.name {attrs.get('k8s.statefulset.name')!r}"
            )
        self._derived(root, problems)
        rolled = [
            e
            for e in data.named("piceli.deploy.rolled")
            if e.get("traceId") == root["traceId"]
        ]
        if not rolled or "store" not in (
            rolled[0]["attributes"].get("piceli.components") or []
        ):
            problems.append(
                f"no piceli.deploy.rolled with store in the trace ({len(rolled)})"
            )
        log(f"success: trace {root['traceId']}, children {sorted(tree)}")

        # --- a failed check with rollback (a manifest change and a failing check)
        app = self.repos.read("infra", "lifecycle_app.py")  # type: ignore[attr-defined]
        check(
            "EXTRA_CHECKS: list = []" in app and REPORTER_COMMAND in app,
            "lifecycle_app.py markers",
        )
        broken = app.replace("EXTRA_CHECKS: list = []", FAILING_CHECK, 1).replace(
            REPORTER_COMMAND, '"while true; do sleep 3599; done"', 1
        )
        bad = self.repos.commit(
            "infra", "a check that fails", {"lifecycle_app.py": broken}
        )  # type: ignore[attr-defined]
        self.wait_env(  # type: ignore[attr-defined]
            "main",
            lambda r: (r.get("revision") or {}).get("infra") == bad and r.get("state") == "failed",
            timeout=1500, what="the failed check's rollback",
        )  # fmt: skip
        data = self._wait_data(
            "the rolled-back trace and its rollback event",
            lambda d: d.with_event(
                "SYNC lifecycle/main",
                "piceli.deploy.rollback",
                **{"piceli.deploy.result": "rolled-back"},
            ),
        )
        root = data.roots(
            "SYNC lifecycle/main", **{"piceli.deploy.result": "rolled-back"}
        )[-1]
        tree = self._children_linked(
            data,
            root,
            ["apply", "checks", "check otel-always-fails", "rollback"],
            problems,
        )
        failing = tree.get("check otel-always-fails") or {}
        if (failing.get("status") or {}).get("code") not in (2, "STATUS_CODE_ERROR"):
            problems.append(f"the failing check's status {failing.get('status')}")
        if (root.get("status") or {}).get("code") not in (2, "STATUS_CODE_ERROR"):
            problems.append(f"the rolled-back root's status {root.get('status')}")
        for name in ("piceli.deploy.checks.failed", "piceli.deploy.rollback"):
            if not [e for e in data.named(name) if e.get("traceId") == root["traceId"]]:
                problems.append(f"no {name} in the rolled-back trace")
        failed = [
            e
            for e in data.named("piceli.deploy.checks.failed")
            if e.get("traceId") == root["traceId"]
        ]
        if failed and "otel-always-fails" not in (
            failed[0]["attributes"].get("piceli.checks.failed") or []
        ):
            problems.append(
                f"checks.failed names {failed[0]['attributes'].get('piceli.checks.failed')}"
            )
        self._derived(root, problems)
        fixed = self.repos.commit(
            "infra", "the failing check removed", {"lifecycle_app.py": app}
        )  # type: ignore[attr-defined]
        self._main_at(fixed, "the fix")  # type: ignore[attr-defined]
        log("rolled-back: checks, rollback and their events in one trace")

        # --- an approval wait (rc: promote, then approve)
        web = self.repos.head("web")  # type: ignore[attr-defined]
        self.run_step("promote-rc", sha=web)  # type: ignore[attr-defined]
        record = self.wait_env(  # type: ignore[attr-defined]
            "rc", lambda r: r.get("state") == "approval-required" and bool(r.get("plan_hash")),
            timeout=1500, what="approval request",
        )  # fmt: skip
        self._wait_data(
            "piceli.deploy.approval.required",
            lambda d: d.named("piceli.deploy.approval.required"),
        )
        time.sleep(20)  # the owner reads the plan
        self.values["rc_hash"] = str(record["plan_hash"])  # type: ignore[attr-defined]
        self.run_step("approve-rc")  # type: ignore[attr-defined]
        self.wait_env(
            "rc",
            self.deployed("rc", {"web": web}),
            timeout=1500,
            what="approved deploy",
        )  # type: ignore[attr-defined]
        data = self._wait_data(
            "rc's trace and its approval",
            lambda d: d.with_event(
                "SYNC lifecycle/rc",
                "piceli.deploy.approved",
                **{"piceli.deploy.result": "success"},
            ),
        )
        root = data.roots("SYNC lifecycle/rc", **{"piceli.deploy.result": "success"})[
            -1
        ]
        tree = self._children_linked(
            data, root, ["approval wait", "plan", "apply", "checks"], problems
        )
        wait = tree.get("approval wait") or {}
        waited = (
            int(wait.get("endTimeUnixNano") or 0)
            - int(wait.get("startTimeUnixNano") or 0)
        ) / 1e9
        if waited < 15:
            problems.append(f"approval wait lasted {waited:.1f}s, not the real wait")
        if root["attributes"].get("piceli.approval.via") != "cli":
            problems.append(
                f"rc approval via {root['attributes'].get('piceli.approval.via')!r}"
            )
        for name in ("piceli.deploy.approval.required", "piceli.deploy.approved"):
            found = [e for e in data.named(name) if e.get("traceId") == root["traceId"]]
            if not found:
                problems.append(f"no {name} in rc's trace")
            elif found[0].get("spanId") != wait.get("spanId"):
                problems.append(f"{name} is not on the approval wait span")
        self._derived(root, problems)
        log(f"approval: waited {waited:.0f}s in trace {root['traceId']}")

        # --- stop and start rc
        self.values["stop_env"] = "rc"  # type: ignore[attr-defined]
        self.run_step("env-stop")  # type: ignore[attr-defined]
        self.wait_env(
            "rc", lambda r: r.get("state") == "stopped", timeout=600, what="stopped"
        )  # type: ignore[attr-defined]
        self.run_step("env-start")  # type: ignore[attr-defined]
        self.wait_env(
            "rc", lambda r: r.get("state") == "deployed", timeout=900, what="started"
        )  # type: ignore[attr-defined]
        data = self._wait_data(
            "stop and start events",
            lambda d: (
                d.with_event("STOP lifecycle/rc", "piceli.environment.stopped")
                and d.with_event("START lifecycle/rc", "piceli.environment.started")
            ),
        )
        for name, span in (
            ("piceli.environment.stopped", "STOP lifecycle/rc"),
            ("piceli.environment.started", "START lifecycle/rc"),
        ):
            event = data.named(name)[-1]
            roots = [
                r for r in data.roots(span) if r["traceId"] == event.get("traceId")
            ]
            if not roots:
                problems.append(f"{name} is not in a {span} trace")
        log("stop and start: events in their traces")

        # --- a broken build (branch environment)
        source = self.repos.read("web", "tool/version.c")  # type: ignore[attr-defined]
        self.repos.commit(  # type: ignore[attr-defined]
            "web", "web: broken build (telemetry)",
            {"tool/version.c": source.replace("return 0;", "return 0 /* otel */ +;")},
            branch="wp-otel",
        )  # fmt: skip
        self.wait_env(  # type: ignore[attr-defined]
            "wp-otel", lambda r: bool((r.get("failure") or {}).get("log_tail")),
            timeout=1500, what="the broken build",
        )  # fmt: skip
        data = self._wait_data(
            "the broken build's trace and piceli.build.failed",
            lambda d: d.with_event("SYNC lifecycle/wp-otel", "piceli.build.failed"),
        )
        event = data.named("piceli.build.failed")[-1]
        tail = str(event["attributes"].get("piceli.build.log_tail") or "")
        if not tail or len(tail.splitlines()) > 20 or "version.c" not in tail:
            problems.append(
                f"build.failed tail: {len(tail.splitlines())} line(s), version.c {'version.c' in tail}"
            )
        roots = [
            r
            for r in data.roots("SYNC lifecycle/wp-otel")
            if r["traceId"] == event.get("traceId")
        ]
        if not roots or roots[0]["attributes"].get("piceli.deploy.result") != "failure":
            problems.append("the broken build's run is not a failed trace")
        self.repos.delete_branch("web", "wp-otel")  # type: ignore[attr-defined]
        wait_for(
            "wp-otel removed",
            lambda: "wp-otel" not in (self.status().get("envs") or {}),  # type: ignore[attr-defined]
            timeout=900,
        )
        log("broken build: piceli.build.failed with its tail")

        # --- the controller killed mid-build: the restart closes the run interrupted
        run = self.repos.read("store", "bin/run.sh")  # type: ignore[attr-defined]
        sha = self.repos.commit(  # type: ignore[attr-defined]
            "store", "store: interrupted edition",
            {"bin/run.sh": run.replace("# The store (telemetry)", "# The store (interrupted)", 1)},
        )  # fmt: skip
        self.wait_env(  # type: ignore[attr-defined]
            "main",
            lambda r: (r.get("in_progress") or {}).get("action") == "deploy"
            and any(c.get("state") == "building" for c in (r.get("components") or {}).values()),
            timeout=900, what="main building",
        )  # fmt: skip
        stored = json.loads(
            self._controller_file("/var/lib/piceli-gitops/telemetry/runs.json") or "{}"
        )
        open_run = (stored.get("runs") or {}).get("main") or {}
        check(
            open_run.get("in_step"),
            f"no open run of main in the controller's state: {sorted(stored.get('runs') or {})}",
        )
        pod = self._controller_pod()  # type: ignore[attr-defined]
        self._ok(
            "-n",
            "piceli-system",
            "delete",
            "pod",
            pod["metadata"]["name"],
            "--grace-period=1",
            "--wait=false",
        )
        self.wait_env(
            "main",
            self.deployed("main", {"store": sha}),
            timeout=1800,
            what="after the restart",
        )  # type: ignore[attr-defined]
        data = self._wait_data(
            "the interrupted run and its event",
            lambda d: d.with_event(
                "SYNC lifecycle/main",
                "piceli.deploy.interrupted",
                **{"piceli.deploy.result": "interrupted"},
            ),
        )
        root = data.roots(
            "SYNC lifecycle/main", **{"piceli.deploy.result": "interrupted"}
        )[-1]
        if root["traceId"] != open_run.get("trace_id"):
            problems.append(
                f"interrupted trace {root['traceId']} is not the persisted {open_run.get('trace_id')}"
            )
        if not [
            e
            for e in data.named("piceli.deploy.interrupted")
            if e.get("traceId") == root["traceId"]
        ]:
            problems.append("no piceli.deploy.interrupted in the interrupted trace")
        if len(data.named("piceli.controller.started")) < 2:
            problems.append("the restart sent no second piceli.controller.started")
        self._derived(root, problems)
        log(
            f"restart: run {open_run.get('run')} closed interrupted in trace {root['traceId']}"
        )

        # --- stable ids, links, metrics, secrets over everything collected
        ids: dict[str, dict[str, Any]] = {}
        for event in data.events:
            event_id = event["attributes"].get("piceli.event.id")
            if not isinstance(event_id, str):
                problems.append(f"{event['name']}: no piceli.event.id")
                continue
            if not EVENT_ID.fullmatch(event_id) and not event_id.startswith(
                "lifecycle:controller/"
            ):
                problems.append(f"event id {event_id!r}")
            seen = ids.setdefault(event_id, event)
            if seen is not event and seen["attributes"] != event["attributes"]:
                problems.append(f"{event_id}: sent twice with other attributes")
            if event["name"] != "piceli.controller.started" and not event.get(
                "traceId"
            ):
                problems.append(f"{event_id}: not correlated to a trace")
        for name in (
            "cicd.pipeline.run.duration", "cicd.pipeline.run.active", "cicd.worker.count",
            "piceli.build.duration", "piceli.check.runs", "piceli.controller.heartbeats",
            "piceli.deploy.last_success.age",
        ):  # fmt: skip
            if name not in data.metrics:
                problems.append(f"no metric {name}")
        if token in data.raw:
            problems.append("the bearer token is in the exported data")
        if token in self.controller_log():  # type: ignore[attr-defined]
            problems.append("the bearer token is in the controller's log")
        log(
            f"collected: {len(data.spans)} spans, {len(data.events)} events, {len(ids)} event ids"
        )

        # --- the Collector down: deploys go on; back: data resumes
        self._ok("-n", NAMESPACE, "scale", f"deploy/{COLLECTOR}", "--replicas=0")
        wait_for(
            "the Collector stopped",
            lambda: not (self.cluster.get("pods", "-n", NAMESPACE) or {}).get("items"),  # type: ignore[attr-defined]
            timeout=300,
        )
        run = self.repos.read("store", "bin/run.sh")  # type: ignore[attr-defined]
        began = time.monotonic()
        sha = self.repos.commit(  # type: ignore[attr-defined]
            "store", "store: collector down",
            {"bin/run.sh": run.replace("# The store (interrupted)", "# The store (no collector)", 1)},
        )  # fmt: skip
        self.wait_env(
            "main",
            self.deployed("main", {"store": sha}),
            timeout=1500,
            what="deploy without a Collector",
        )  # type: ignore[attr-defined]
        log(f"collector down: main deployed in {time.monotonic() - began:.0f}s")
        if "Traceback" in self.controller_log(since):  # type: ignore[attr-defined]
            problems.append("a traceback in the controller's log")
        self._ok("-n", NAMESPACE, "scale", f"deploy/{COLLECTOR}", "--replicas=1")
        self._collector_ready()
        self._wait_data(
            "data again after the Collector is back",
            lambda d: d.metrics.get("piceli.controller.heartbeats"),
            timeout=300,
        )
        log("collector back: metrics arrive again")
