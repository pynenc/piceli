# Artifact delivery and operation telemetry

Piceli's artifact API separates immutable planning from any tool, daemon,
network or Kubernetes client. Importing `piceli.artifacts` is side-effect free.
No API in this profile pushes an image or deploys a workload implicitly.

## Pure OCI API

Classify each input explicitly with `SourcePin.capture(root, path, public=True)`,
then create `ArtifactFile` entries and a `BuildPlan`. Plans bind exact source
bytes, destination paths, platform, numeric user, entrypoint and byte budget.
Private-looking paths, links, devices, traversal, changed pins and overlapping
destinations fail closed.

`OciBuilder().build(plan, root, output)` assembles one deterministic,
uncompressed OCI image layout without executing repository code. It publishes
the output only after complete validation. `inspect_oci(path)` rechecks bounded
metadata, descriptor hashes, platform, layer types and archive members. Public
receipts contain only digests, size, platform and explicit `pushed: false` /
`imported: false` claims.

For an external build tool, capture an absolute `ToolPin`, construct a
`BuildCommand`, inspect `preview()`, and supply an exact unexpired
`ExecutionGrant`. Execution uses no shell or ambient credentials, bounds wall
time and output, and kills the POSIX process group on timeout or cancellation.
This is process control, not a sandbox: arbitrary authorized tools may access
the host and therefore require an explicit network-capable grant.

`DockerLocalImporter` is a separate opt-in adapter. It requires a pinned Docker
binary, absolute Unix socket and `LocalImportGrant` for the exact OCI manifest.
It validates the layout before loading, creates no tag, performs no push, and
returns a secret-safe receipt. Callers own daemon authorization and image cleanup.

## Thin CLI

The same boundaries are available as JSON commands:

```sh
python -m piceli artifacts pin --source-root ROOT --public-file FILE
python -m piceli artifacts preview --plan PLAN.json
python -m piceli artifacts build --plan PLAN.json --source-root ROOT --output OCI
python -m piceli artifacts inspect --layout OCI
python -m piceli artifacts import-local --layout OCI --docker /abs/docker \
  --docker-sha256 sha256:... --socket /abs/docker.sock \
  --approve-digest sha256:...
```

`preview-command` and `execute-command` expose pinned external tools. The latter
also requires `--approve-plan`, `--allow-code-execution` and `--allow-network`.
Failures return fixed JSON and never echo paths, tool output, manifests or tokens.

## Deployment telemetry

Pass `OperationTelemetry(OtlpOptions(endpoint, token, ...))` to `PlanExecutor`.
It emits bounded OTLP/HTTP-Protobuf spans and logs for plan, build, import, apply,
resume, cancel and compensation operations. The mapping is
`piceli.operation-otel.v1`; it deliberately does not claim
task/attempt semantics of a task runner such as Rustvello. Journal states, including
`compensated-with-retention`, are preserved exactly.

Admission and exporter work are bounded. `stats()` distinguishes attempted,
acknowledged, rejected, unknown and known-not-sent records, plus queue drops and
post-shutdown rejection. `flush(timeout)` and `shutdown(timeout)` return within
their caller budget and never report an undrained queue as lossless. Payloads,
exception bodies, private version IDs and bearer tokens are not exported.

Consumers can pin the exact Piceli sources, schemas and tool versions they
integrate against and verify them with their own end-to-end acceptance. A
typical no-deployment check uses a disposable loopback API, imports but never
runs an owned format-test image, and verifies operation records across a restart
of the telemetry consumer.

## Runnable Linux images

A separate *runnable* profile covers images that are executed. `BuildCommand` can export
an exact, already-present base image with bounded process control;
`DockerArchiveOciBuilder` validates that archive's pinned config identity and
architecture, preserves every runtime layer, and adds a deterministic public
application layer. It does not resolve dependencies: native execution is the
required proof that the supplied base actually contains the entrypoint's runtime
closure. `RunnableBuild` is available when an external pinned builder already
produces an OCI tar.

`DockerLocalImporter`, `DockerLocalRunner.inspect_image` and
`DockerLocalRunner.run_image` are three explicit actions with distinct grants.
The runner executes the exact imported image ID with no network, a read-only
root filesystem, no Linux capabilities, a finite deadline/output budget and
owned-container cleanup after normal exit, failure, timeout or cancellation.
Receipts omit process output, Docker socket paths, command arguments and tokens.

A source-bound acceptance for this profile runs on a Linux/arm64 Docker engine
with the pinned base already present. It never pulls, pushes or deploys, and
covers exact base export, OCI inspection/import, successful readiness,
missing-runtime failure, architecture rejection, bounded cancellation, cleanup,
and operation traces/logs across a restart of the telemetry consumer.
