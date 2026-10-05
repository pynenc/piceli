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
