# OpenTelemetry from the GitOps controller

```{admonition} Maturity: experimental
:class: note

The controller's OpenTelemetry is new in 0.16.0. Span, event, attribute and
metric names may change in a minor release, with a changelog entry. See the
{doc}`roadmap` for every feature's status.
```

The composition controller ({doc}`gitops`) can send what it does to an
OpenTelemetry Collector, or any backend that accepts OTLP: **one trace per
deploy** with a span per stage, **events** (log records) for approvals,
rollouts, rollbacks, failed checks, failed builds and stops, and **metrics**
for run durations, results, builds, checks and the controller's heartbeat.
A dashboard can then place a deploy marker on the workloads a run changed,
show where its time went, and alert on failures or a silent controller.

It is **off unless you configure it**, it **never blocks or fails a
deploy**, and it **never exports a secret**. The names follow the
[OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
**v1.43.0** (CI/CD, VCS, deployment, Kubernetes and events); where no
convention exists the attribute is `piceli.*`.

For `piceli deploy` and `piceli release` on a laptop or in CI, see
{doc}`deploy_events` (a different, SDK-free export of one run).

## Turn it on

Declare where to send it on the controller of the composition's cluster:

```python
from piceli.infra import Cluster, Controller, Otlp

cluster = Cluster(
    "my-cluster",
    api="https://10.0.0.1:6443",
    credentials="my-cluster",
    controller=Controller(
        on="node-a",
        telemetry=Otlp(
            "https://collector.observability:4317",
            protocol="grpc",               # or "http/protobuf" (port 4318)
            headers_secret="otlp-auth",    # optional: auth headers
            ca_secret="otlp-ca",           # optional: the endpoint's CA
        ),
    ),
)
```

Then create the Secrets in the controller's namespace (`piceli-system` by
default) and run `piceli gitops enable` as usual (plan, then approve):

```sh
# One key per header; the value is read from a file, never typed on the command line.
kubectl -n piceli-system create secret generic otlp-auth \
  --from-file=authorization=./otlp-authorization.txt   # e.g. "Bearer <token>"
kubectl -n piceli-system create secret generic otlp-ca --from-file=ca.crt=./ca.crt
piceli gitops enable infra.py …                         # plan, then --approve HASH
```

| `Otlp(...)` | Meaning |
| --- | --- |
| `endpoint` | `https://host:4317` (gRPC) or `https://host:4318` (HTTP; Piceli appends `/v1/traces`, `/v1/logs`, `/v1/metrics`). No credentials, query or fragment. |
| `protocol` | `"grpc"` (default) or `"http/protobuf"`. |
| `headers_secret` | A Secret whose keys are header names and values header values (`authorization`). Mounted read-only into the controller at `/var/run/piceli-gitops/otlp-headers`. |
| `ca_secret` | A Secret with `ca.crt`, the CA that signs the endpoint's certificate (mounted at `/var/run/piceli-gitops/otlp-ca`). |
| `insecure` | `True` to send without TLS to an `http://` endpoint (an in-cluster Collector). Plain `http://` is refused without it, except on loopback. |

`gitops enable` puts the setting in the controller's configuration (only
when it is set, so the configuration hash of a controller without telemetry
does not change) and mounts the Secrets; their values never enter the
configuration, the plan, a log line or an attribute. A changed setting rolls
the controller. An invalid one is refused with `cluster-invalid` when the
composition is imported.

Without `Controller(telemetry=...)`, the standard variables configure it
when the controller's environment has them (`OTEL_EXPORTER_OTLP_ENDPOINT`
or a per-signal `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` /
`_LOGS_ENDPOINT` / `_METRICS_ENDPOINT`, with `OTEL_EXPORTER_OTLP_PROTOCOL`,
`_HEADERS`, `_CERTIFICATE`, `_TIMEOUT` read by the OpenTelemetry SDK; the
protocol defaults to `http/protobuf`). This is for `piceli gitops run`
outside the cluster. `OTEL_SDK_DISABLED=true` turns telemetry off either
way.

The controller image includes the `telemetry` extra (the OpenTelemetry SDK
and its OTLP gRPC and HTTP exporters). Installing Piceli yourself:
`pip install "piceli[telemetry]"`.

## Traces

**One trace per pipeline run**: one environment's deploy of one revision,
from the step that starts it to its result. A run that waits for an
approval stays open: the approval wait is a span of it, and the deploy after
the approval continues the same trace. A retry after a failure is a run of
its own (`piceli.run.attempt`). A newer revision arriving while a run waits
for its approval closes it as `superseded`.

| Span | Kind | Parent | When |
| --- | --- | --- | --- |
| `SYNC <composition>/<env>` | SERVER | — | The run (`cicd.pipeline.action.name = SYNC`). |
| `build <image>` | INTERNAL | root | Each image built or mirrored (`cicd.pipeline.task.type = build`). |
| `approval wait` | INTERNAL | root | From the plan asking for approval to `piceli gitops approve` (or the UI). |
| `plan` | INTERNAL | root | The deploy's plan stage. |
| `restore point` | INTERNAL | root | Restore points (`RestorePoints`) taken before the rollout. |
| `pre-rollout` | INTERNAL | root | Pre-rollout and upgrade checks ({doc}`pre_rollout_checks`). |
| `apply` | INTERNAL | root | The release applied. |
| `prune` | INTERNAL | `apply` | Objects the release no longer declares, deleted (only when there were). |
| `checks` | INTERNAL | root | The post-deploy checks (`cicd.pipeline.task.type = test`). |
| `check <name>` | INTERNAL | `checks` | One check: its outcome and, when it failed, its detail line. |
| `rollback` | INTERNAL | root | The previous release re-applied after failed checks. |
| `STOP <composition>/<env>` | SERVER | — | A named environment stopped (or a branch environment stopped when idle). |
| `START <composition>/<env>` | SERVER | — | A stopped environment started. |
| `TEARDOWN <composition>/<env>` | SERVER | — | A branch environment removed (or a placement leaving a cluster). |

A span's status is `OK` when its task succeeded, `ERROR` (with the error
code as message) when it failed, and unset for a skipped stage.

### Times

Every start and end is a recorded time, never an estimate:

- `plan`, `restore point`, `pre-rollout`, `apply` and `checks`: the run
  journal's `started_at` and `finished_at` of that stage;
- `build <image>`: the controller's clock when it started the build Job and
  when the image came back (one Job builds several images, so they share
  its start); a build entry's own `started_at` / `finished_at` win when the
  record has them;
- `approval wait`: when the plan asked and when the approval arrived;
- `check <name>`: laid out from the `checks` stage's start in the order the
  checks ran, each as long as its recorded duration;
- `rollback` runs inside the `checks` stage after the last check: from the
  end of the last check to the end of the stage. `prune` runs inside
  `apply` and covers it: Piceli does not time either separately;
- the root: from the start of the run's first step to the end of its last.

### Root span attributes

| Attribute | Value |
| --- | --- |
| `cicd.pipeline.name` | `<composition>/<env>`. |
| `cicd.pipeline.run.id` | `<env>/<run>` (unique; also in event ids). |
| `cicd.pipeline.action.name` | `SYNC`. |
| `cicd.pipeline.result` | `success`, `failure` (also rolled back), `error` (interrupted), `cancellation` (superseded). |
| `piceli.deploy.result` | `success`, `failure`, `rolled-back`, `interrupted`, `superseded`. |
| `error.type` | The registered error code when it did not succeed (`pipeline-checks-failed`, `component-build-failed`, `controller-restarted`, …). |
| `deployment.environment.name` | The environment. |
| `deployment.id`, `deployment.status` | `<env>/<run>`; `succeeded` or `failed`. |
| `cicd.worker.name`, `cicd.worker.id` | `piceli-controller`; its pod name. |
| `vcs.ref.head.revision`, `vcs.ref.head.name`, `vcs.ref.head.type`, `vcs.repository.name` | The first source (by name): its commit, branch or tag name, `branch` or `tag`, and its name. |
| `piceli.sources`, `piceli.source.revisions`, `piceli.source.refs` | Every source of the revision, sorted by name, with its commit and followed ref at the same index. |
| `piceli.trigger`, `piceli.trigger.kind` | What started it (`push web/main@…`) and its kind: `push`, `tag`, `promote`, `sync`, `start`. |
| `piceli.approval.via` | Who approved: `policy`, `cli` or `ui`. |
| `piceli.release.id`, `piceli.plan.hash` | The release applied; the approved plan hash. |
| `piceli.image.digests` | `name=sha256:…` per component. |
| `piceli.run.attempt`, `piceli.run.kind` | The attempt of this revision; `deploy`, `stop`, `start_owner`, `teardown`. |
| `k8s.namespace.name` | The environment's namespace. |
| `k8s.deployment.name`, `k8s.statefulset.name` | The Deployment or StatefulSet the run created or changed, when it is one. |
| `piceli.k8s.deployments`, `piceli.k8s.statefulsets` | All of them. |
| `piceli.cluster` | The cluster of a placement, for an environment on several clusters. |

Stage spans carry `cicd.pipeline.task.name`, `cicd.pipeline.task.run.id`,
`cicd.pipeline.task.run.result` (`success`, `failure`, `error`, `skip`,
`cancellation`), `cicd.pipeline.task.type`, `piceli.stage`, `error.type`
and the run's `cicd.pipeline.name`, `cicd.pipeline.run.id`,
`deployment.environment.name` and `k8s.namespace.name`. Build spans add
`piceli.image` and `piceli.image.digest`; check spans `piceli.check.name`,
`piceli.check.type`, `piceli.check.attempts` and, when failed,
`piceli.check.detail` (its short detail line, at most 300 characters);
`rollback` adds `piceli.rollback.from`, `.to` and `.state`; `prune` adds
`piceli.pruned` (`Kind/name`) and `piceli.pruned.count`; `apply` adds the
`k8s.*` workloads and `piceli.release.id`.

## Events

Events are OpenTelemetry log records with an event name (the
`EventName` field of OTLP log records) and a short display body, emitted
when they happen and correlated to the run's trace and to the span they
belong to. Severity is `INFO`, or `ERROR` for failures.

| Event | Span | Attributes (besides the common ones) |
| --- | --- | --- |
| `piceli.controller.started` | — | `piceli.controller.version` |
| `piceli.deploy.approval.required` | `approval wait` | `piceli.plan.hash` |
| `piceli.deploy.approved` | `approval wait` (cli, ui) or root (policy) | `piceli.approval.via`, `piceli.plan.hash` |
| `piceli.deploy.rolled` | `apply` | `piceli.components` (rolled), `piceli.image.digests`, `piceli.release.id`, `k8s.deployment.name`, `k8s.statefulset.name`, `piceli.k8s.*` |
| `piceli.deploy.pruned` | `prune` | `piceli.pruned` |
| `piceli.deploy.checks.failed` | `checks` | `piceli.checks.failed` (names), `piceli.checks.detail` (`name: detail`), `error.type` |
| `piceli.deploy.rollback` | `rollback` | `piceli.rollback.from`, `piceli.rollback.to` (releases), `piceli.rollback.state` |
| `piceli.build.failed` | `build <image>` | `piceli.build.images`, `piceli.build.log_tail` (the last 20 lines, redacted as the build log is), `piceli.build.kept_job`, `error.type` |
| `piceli.environment.stopped` | `STOP …` | `piceli.stop.reason` (`declared`, `requested`, `idle-stop`), `piceli.stop.via` |
| `piceli.environment.started` | `START …` | — |
| `piceli.deploy.interrupted` | root | `error.type = controller-restarted` |

Common attributes: `piceli.event.id`, `cicd.pipeline.name`,
`cicd.pipeline.run.id`, `deployment.environment.name`,
`k8s.namespace.name`, `piceli.cluster`.

**`piceli.event.id`** is stable: `<cluster>:<env>/<run>:<what>` (for
example `my-cluster:main/20261004T101500123Z:rolled:1`;
`<cluster>:controller/<start>:started` for the controller). An event sent
again carries the same id, so a backend can drop the duplicate.

## Metrics

| Metric | Type | Unit | Attributes |
| --- | --- | --- | --- |
| `cicd.pipeline.run.duration` | Histogram | `s` | `cicd.pipeline.name`, `cicd.pipeline.run.state` (`pending`: waiting for an approval; `executing`), `cicd.pipeline.result`, `piceli.deploy.result`, `deployment.environment.name` |
| `cicd.pipeline.run.active` | UpDownCounter | `{run}` | `cicd.pipeline.name`, `cicd.pipeline.run.state`, `deployment.environment.name` |
| `cicd.pipeline.run.errors` | Counter | `{error}` | `cicd.pipeline.name`, `error.type`, `deployment.environment.name` |
| `cicd.worker.count` | UpDownCounter | `{worker}` | `cicd.worker.state` (`available`, `busy`): the controller |
| `piceli.build.duration` | Histogram | `s` | `piceli.image`, `piceli.build.result`, `deployment.environment.name` |
| `piceli.check.runs` | Counter | `{check}` | `piceli.check.name`, `piceli.check.result` (`passed`, `failed`), `deployment.environment.name` |
| `piceli.deploy.approval.wait` | Histogram | `s` | `cicd.pipeline.name`, `deployment.environment.name` |
| `piceli.deploy.queue.wait` | Histogram | `s` | time from a wanted revision to its deploy step; `cicd.pipeline.name`, `deployment.environment.name` |
| `piceli.deploy.queue.size` | Gauge | `{run}` | environments due for a deploy step |
| `piceli.deploy.last_success.age` | Gauge | `s` | seconds since the environment's last successful deploy; `deployment.environment.name` |
| `piceli.controller.heartbeat` | Gauge | `s` | Unix time of the controller's last heartbeat |
| `piceli.controller.heartbeats` | Counter | `{heartbeat}` | — |

The heartbeat comes from a background thread every 15 seconds, so it keeps
ticking while a step runs for minutes (a long build): alert when
`piceli.controller.heartbeats` stops increasing. Metrics are exported every
30 seconds.

The resource of every signal: `service.name = piceli-controller`,
`service.version`, `service.namespace = piceli`, `service.instance.id` (the
pod), `k8s.cluster.uid` (the `kube-system` namespace's UID, when the
controller may read it), `k8s.cluster.name` (the composition's cluster) and
`piceli.controller.namespace`.

## A controller restart

The open run (its ids, start, stage marks and the events it sent) is kept
in the controller's state volume (`telemetry/runs.json`); the heartbeat
writes its time there too. Trace and span ids are derived from the cluster,
the environment and the run (a SHA-256), so a run's ids never change.

When the controller dies inside a run (a node lost, the pod killed), the
next controller closes that run at start as `interrupted`
(`cicd.pipeline.result = error`), in the same trace, ending at the last
heartbeat; the run's events are sent again (same `piceli.event.id`) with a
`piceli.deploy.interrupted` event, then the environment's deploy runs again
as a new run. A run waiting for an approval is not interrupted: it continues
after the restart.

## Failure behaviour

Telemetry is off the deploy path:

- spans and events go to bounded in-memory queues (2048 per signal); the
  SDK exports them in background threads in batches, each export bounded
  to 5 seconds with the exporter's own retries inside that time; when the
  queue is full, new data is dropped;
- nothing is persisted for an unreachable endpoint except the open run:
  data produced while the Collector is down is lost, and export resumes
  when it answers again;
- every hook catches its own errors: a telemetry problem logs one line per
  kind (`telemetry: step end skipped (…)`) and the deploy goes on;
- a configuration that cannot work logs `telemetry: off (…)` once at start
  and the controller runs without telemetry; failed exports log one error
  line per batch (retries are not logged);
- shutting down flushes for at most 5 seconds.

Header values and the CA are read from the mounted files at start. They are
never printed, logged, written to the configuration or put in an attribute;
the build log tail of `piceli.build.failed` is the redacted tail the status
already shows.

## Semantic conventions

Followed: [CI/CD spans and metrics](https://opentelemetry.io/docs/specs/semconv/cicd/)
(`cicd.pipeline.*`, `cicd.pipeline.run.*`, `cicd.pipeline.task.*`,
`cicd.worker.*`), [VCS](https://opentelemetry.io/docs/specs/semconv/registry/attributes/vcs/)
(`vcs.ref.head.*`, `vcs.repository.name`),
[deployment](https://opentelemetry.io/docs/specs/semconv/registry/attributes/deployment/)
(`deployment.environment.name`, `deployment.id`, `deployment.status`),
Kubernetes resource attributes (`k8s.cluster.*`, `k8s.namespace.name`,
`k8s.deployment.name`, `k8s.statefulset.name`) and
[events](https://opentelemetry.io/docs/specs/semconv/general/events/)
(log records with an event name), all at v1.43.0, schema URL
`https://opentelemetry.io/schemas/1.43.0`. As the Collector's GitHub
receiver does for workflow runs, trace and span ids are derived from the
run's identity.

Choices where the conventions leave room:

- a run has several sources: the `vcs.*` attributes name the first one,
  and `piceli.sources` / `.source.revisions` / `.source.refs` list all;
- task spans have no `cicd.pipeline.task.run.url.full` (the controller
  serves no URL per task);
- a run changes several workloads: `k8s.deployment.name` /
  `k8s.statefulset.name` only when there is one, `piceli.k8s.*` lists all;
- events use the log record's event name field (the `event.name` attribute
  is deprecated in these conventions);
- UIDs of the workloads are not looked up (one more API read per object).
