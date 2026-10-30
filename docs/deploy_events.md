# Deploy events to OpenTelemetry

```{admonition} Maturity: experimental
:class: note

Deploy events (`piceli.deploy-otel.v1`) are new in 0.11.0 and may change in a
minor release, with a changelog entry. See the {doc}`roadmap` for every
feature's status.
```

`piceli deploy`, `piceli release apply` and `piceli release rollback` can send
what a run did to an OTLP endpoint (an OpenTelemetry Collector, or any backend
that accepts OTLP/HTTP), so a dashboard can show each deploy as a marker and a
trace of its stages. It is **off unless you configure it**, it **never fails or
noticeably slows a deploy**, and it **never exports secrets**.

```{note}
This page is about `piceli deploy` and `piceli release` on a laptop or in CI.
The GitOps controller sends its own traces, events and metrics over OTLP
gRPC or HTTP (`Controller(telemetry=Otlp(...))`): see {doc}`opentelemetry`.
```

## Turn it on

Use the standard OpenTelemetry variables, or the option:

| Setting | Meaning |
| --- | --- |
| `--otlp-endpoint URL` | Base URL of the collector; wins over the environment. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL; Piceli appends `/v1/traces` and `/v1/logs`. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | Full URLs of each signal (no suffix added). |
| `OTEL_EXPORTER_OTLP_HEADERS` | `key=value,key2=value2`, for example an `authorization` header. Read from the environment only (never a command-line flag) and never printed. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Milliseconds for the whole export; default 3000, at most 10000. |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Resource of the events (default service name `piceli`). |
| `OTEL_SDK_DISABLED=true` | Turns it off even when an endpoint is set. |

The URL must be `http` or `https`, with no credentials, query or fragment.
Piceli always sends OTLP/HTTP with the JSON encoding (`Content-Type:
application/json`, accepted by the OpenTelemetry Collector's `otlp` receiver).
`OTEL_EXPORTER_OTLP_PROTOCOL` is accepted as `http/protobuf` or `http/json`;
`grpc` is refused (`deploy-events-config-invalid`). No OpenTelemetry SDK or
extra package is needed.

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.observability:4318
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer $TOKEN"
piceli deploy app.py:pipeline --approve <combined-hash>
```

## What is sent

One **trace per run**: a root span `piceli.deploy` (`piceli.release` for
`release apply|rollback`) with one child span per stage
(`piceli.deploy.inputs`, `.build`, `.deliver`, `.prerollout`, `.backup`,
`.plan`, `.apply`, `.checks`, and `.rollback` after failed checks that rolled
back). Release runs have `.apply` and `.checks` children. A span's status is
error when the stage failed.

One **log record per finished run** with body `piceli.deploy.<outcome>`
(`ready`, `failed`, `rolled-back`, `interrupted`, `rejected`), severity `INFO`
or `ERROR`, `event.name = piceli.deploy` and the same attributes, linked to the
root span. Use it as the dashboard marker. A run that only plans
(`--plan`, `approval-required`, `--until` before apply) sends its trace with
outcome `planned`, `approval-required` or `stopped` but no log record, so
markers show real deploys only.

Attributes (root span and log record):

| Attribute | Value |
| --- | --- |
| `piceli.app`, `k8s.namespace.name`, `piceli.environment` | The app, the target namespace and the `--env` name. |
| `piceli.run.id` | The run (`piceli runs`). |
| `piceli.release.id` | The release the run applied. |
| `piceli.plan.hash` | The combined hash (deploy) or plan hash (release) that was approved. |
| `piceli.commit`, `piceli.commits`, `piceli.ref`, `piceli.source.dirty` | The built commit (one source), `name=sha` per source, the requested `--ref` values, and whether the tree was dirty. |
| `piceli.image.digests` | `name=sha256:…` per image of the release (deploy). |
| `piceli.outcome`, `piceli.reason` | Result state and the fixed error code when it did not succeed. |
| `piceli.duration_ms` | Wall time of the whole run. |
| `piceli.release.intent` | `apply` or `rollback` (release). |

Child spans carry `piceli.stage`, `piceli.stage.state`,
`piceli.stage.duration_ms` and `piceli.reason`.

Only these fixed fields are sent: names, hashes, commits, digests and error
codes. Manifests, Secret values, messages, logs, exception text, kubeconfig
paths and headers are never included.

## Failure behaviour

Events are collected during the run and exported once at its end, in a
background thread that is waited for at most the timeout. An unreachable,
slow or refusing endpoint prints one warning line and nothing else changes:
the exit code and stdout are those of the deploy.

```text
warning: deploy events not exported (deploy-events-export-failed); the deploy is unaffected
```

A malformed configuration warns with `deploy-events-config-invalid` and
exports nothing. Nothing is retried or queued across runs.

## Dashboards

Key a deploy marker on the log records with `event.name = piceli.deploy`
(annotation query for your backend), label it with `piceli.release.id`,
`piceli.commit` and `piceli.outcome`, and filter by `piceli.app` and
`k8s.namespace.name`. `piceli.image.digests` links a marker to the images.

## Release markers

From 0.38.0 every finished release sends one **release marker**: an
OpenTelemetry log record with the event name `piceli.release`. Any
OpenTelemetry backend can draw it on graphs and compare release N with
release N-1. Two senders, each to the endpoint you already configured:

- the GitOps controller, for each environment deploy that applied a
  release and for each published `NixRelease`, through
  `Controller(telemetry=Otlp(...))` ({doc}`opentelemetry`);
- the host agent of each Nix host, for each switch it made, through the
  host's own `NixHost(report=HostReport(endpoint))` ({doc}`nix_hosts`).

A run that applied nothing (a failed build, a verification, an unchanged
release) sends no marker. A rollback that ends a release sends one with
`deployment.status = rolled_back`: failed checks the controller rolled
back, a host whose switch or checks failed, an interrupted host run, or
`piceli host rollback`.

| Attribute | Value |
| --- | --- |
| `deployment.environment.name` | The environment; for a Nix host, the host's name; for a `NixRelease`, its name. |
| `deployment.id` | This release: `<env>/<run>` (the same as the controller's root span), `<host>/<attempt>` or `nix/<attempt>`. |
| `deployment.status` | `succeeded`, `failed` (applied, checks failed, nothing rolled back) or `rolled_back`. |
| `service.version` | The tag the first source follows when it is a tag, else its commit (a host: the build's tag, else its commit). |
| `vcs.ref.head.revision` | The commit released (the first source, by name). |
| `vcs.repository.url.full` | That source's repository, without credentials, when the controller knows it. |
| `piceli.release.components` | The components the release changed (their image digest differs from the previous release); a host: its build. |
| `piceli.release.started_at`, `piceli.release.finished_at`, `piceli.release.seconds` | The window: from the start of the apply (a host: of the attempt) to the end, checks settled. |
| `piceli.release.previous.id`, `.previous.version`, `.previous.revision`, `.previous.finished_at` | The release that ran before (absent for the first one). A rolled-back release never becomes the previous one. |
| `piceli.release.id`, `piceli.run.id` | Controller only: the release applied and the run journal (the `run_id` of `piceli gitops history`). |

The controller's marker also carries `piceli.deploy.result`,
`vcs.ref.head.name`, `vcs.ref.head.type`, `vcs.repository.name` and the
common attributes of its events (`cicd.pipeline.name`,
`cicd.pipeline.run.id`, `k8s.namespace.name`, `piceli.event.id`), and is
linked to the run's trace. The host agent's carries `host.name`,
`piceli.trigger`, `piceli.state` and `piceli.reason`, and its resource now
has `deployment.environment.name` too. `deployment.*`, `service.version`
and `vcs.*` follow the OpenTelemetry semantic conventions; `rolled_back` is
Piceli's value next to their `succeeded` and `failed`. Release attributes
change with each release, so they are on the record, not on the resource
(the controller's resource stays `service.name = piceli-controller`).

**Before and after.** Take a marker's `piceli.release.finished_at` and
compare the hour before `piceli.release.started_at` with the hour after
`piceli.release.finished_at`, for example the error rate of the services
in `piceli.release.components`:

```text
errors / requests  where time in [started_at - 1h, started_at)   -> release N-1
errors / requests  where time in (finished_at, finished_at + 1h] -> release N
```

An agent without access to the backend gets the same windows from
`piceli gitops history --json` (each run's `release_window`, see
{doc}`gitops`) and `piceli gitops compare ENV --json` (`before` and
`after`).

`piceli deploy` and `piceli release` (this page) keep their own marker,
the `piceli.deploy` log record described above.
