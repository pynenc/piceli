# Image delivery

```{admonition} Maturity: preview
:class: note

`piceli artifacts deliver` options and receipts may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

`piceli artifacts deliver` moves one digest-approved image to where a cluster
can run it, and writes a JSON receipt. It has two modes:

- **Registry delivery (default)**, `--to oci://host[:port]/repository[:tag]`:
  pushes the image to an OCI registry. Only blobs the registry does not
  already have are uploaded, and workloads pull an immutable
  `registry/repository@sha256:<manifest digest>`.
- **Node import (fallback)**, `--to ssh://…` or `--to docker://…`: streams the
  whole image into one node's containerd with `ctr images import`. Use it to
  bootstrap a cluster (before a registry runs) or for air-gapped nodes.

Prefer the registry. A node import resends every layer on every release and
leaves one imported image per release on the node; a registry moves only the
layers that changed.

## What is approved: the config digest

In both modes `--approve-digest` is the image's **config digest**: the SHA-256
of the image configuration JSON. Docker's classic image store uses it as the
image ID (`docker image inspect --format '{{.Id}}'`), and the Kubernetes CRI
uses it as the image ID too (`crictl images`, `crictl inspecti`).

The config digest is used because it is the only identifier that stays the same
everywhere an image goes:

| Where | Manifest digest | Config digest |
| --- | --- | --- |
| Registry | the pushed manifest | same |
| `docker save` (Docker ≤ 24) | none, the archive has no manifest | file name `<hex>.json` |
| `docker save` (Docker ≥ 25) | a manifest written locally | `blobs/sha256/<hex>` |
| After `ctr images import` | a manifest that `ctr` creates | same |

A manifest digest depends on layer compression and on who wrote the manifest,
so it is not comparable across these. The config lists the uncompressed digest
of every layer (`rootfs.diff_ids`); Piceli checks every layer against that
list, so the config digest also fixes the content of every layer.

## Registry delivery (default)

```sh
python -m piceli artifacts deliver \
  --image example/app:1.4.2 \
  --to oci://registry.example:5000/team/app:1.4.2 \
  --approve-digest sha256:<config digest> \
  --credentials ~/.config/piceli/registry.json \
  --receipt delivery-receipt.json --journal deliveries.jsonl
```

The receipt's `pull_ref` is what a workload should reference.

### Tools and pins

Every external tool is pinned by SHA-256, and the pins are recorded in the
receipt's `tools` (`docker`, `kubectl`, `ssh`). You do not have to pin them
by hand:

- **docker** (needed for `--image`, and for `docker://` node targets) and
  **kubectl** (needed for `--via-forward`) are found on `PATH`, like
  `build-spec` does. **ssh** is found the same way for `ssh://` targets.
- The path is resolved to the real file (symbolic links followed, so Docker
  Desktop's or Nix's links pin the actual binary), its SHA-256 is computed,
  and the pin is checked again after the delivery.
- `--docker PATH`, `--kubectl PATH`, `--ssh PATH` choose a different binary.
  `--docker-sha256`, `--kubectl-sha256`, `--ssh-sha256` require a known
  digest: a different binary is refused with `tool-pin-mismatch`.
- The Docker socket is `--docker-socket` when given, otherwise the unix
  socket in `DOCKER_HOST`, otherwise the endpoint of the current Docker
  context (`docker context inspect`). Only `unix://` endpoints are used; a
  TCP or SSH endpoint is refused with `docker-socket-required`.

### Target

`oci://host[:port]/repository[:tag][?tls=true|false]`

- `repository` follows the distribution grammar (lowercase path components).
- `tag` is optional. Without a tag the manifest is pushed by digest only.
  Digest references are not targets: the manifest digest is the result.
- TLS is on by default. **Plain HTTP is allowed only for a loopback host**
  (`127.0.0.0/8`, `::1`, `localhost`), which is the default there. That
  covers a registry bound to node loopback and one reached through a local
  port-forward. `?tls=false` on any other host is refused.
- `--ca-file` verifies the registry's certificate against an explicit CA.

### Reaching an in-cluster registry through a port-forward

An owner-operated registry does not need to be exposed. With `--via-forward`,
Piceli starts a supervised `kubectl port-forward` for the duration of the
push and stops it afterwards, whatever the outcome:

```sh
python -m piceli artifacts deliver \
  --image sha256:<image ID> \
  --to oci://127.0.0.1:15000/team/app:1.4.2 \
  --approve-digest sha256:<config digest> \
  --via-forward service/registry --namespace registry --forward-remote-port 5000 \
  --kubeconfig /abs/path/cluster.kubeconfig --context my-cluster \
  --node-registry 127.0.0.1:5000
```

- The `--to` host must be loopback and must have a port: it is the local end
  of the forward. `--forward-remote-port` (default 5000) is the registry port
  on the Service or Pod (`service/NAME` or `pod/NAME`).
- The forward listens on `127.0.0.1` only. kubectl always gets the explicit
  `--kubeconfig` (and `--context` when given); the default kubeconfig and
  current context are never used. kubectl is found and pinned like the
  other tools (see Tools and pins).
- The forward runs under the same `ForwardSupervisor` as `piceli observe`:
  it is health-checked with `GET /v2/` (any status below 500 counts, so an
  auth challenge is healthy) and restarted if the probe keeps failing. If
  the local port is already served by another process the push fails with
  `forward-port-in-use` and nothing is started.
- **`--node-registry` is required with a forward.** The address the push
  uses (`127.0.0.1:15000` on your machine) is not the address nodes pull
  from. For a registry bound to node loopback that is `127.0.0.1:5000`, and
  containerd pulls from a loopback registry over plain HTTP without any
  registry configuration. Without a forward, `--node-registry` defaults to
  the `--to` registry.

### Credentials

`--credentials FILE` names a JSON file with either
`{"username": "…", "password": "…"}` or `{"token": "…"}`. The file must be a
regular file (not a symbolic link) with no group or world permissions
(`chmod 600`). Username and password are sent as HTTP Basic auth, or
exchanged for a bearer token when the registry answers with a
`Bearer realm=…` challenge (the Docker token flow, scope
`repository:<repo>:pull,push`); a token is sent as a bearer token.
Credentials never appear in receipts, output or errors. The receipt only
records the scheme (`target.auth`: `none`, `basic` or `bearer`). An upload
`Location` on another scheme, host or port is refused, so credentials never
follow a redirect.

### How a push runs

1. **Check the grant and tools.** The grant must name this exact `oci://`
   target and must not be expired. Docker (for `--image`) and kubectl (for
   `--via-forward`) must match their SHA-256 pins, before and after.
2. **Read the source once and plan the push.** `docker image save <image
   ID>` (by immutable ID, not by a tag that could move), or the archive, is
   read as a stream. Every member is hashed, and gzip members are also
   hashed decompressed. Nothing is written to disk. From that pass:
   - For an **OCI image layout** (`oci-layout` + `index.json`, which Docker
     ≥ 25 also writes), the archive's own manifest is pushed **byte for
     byte**, so its digest is kept. An index is followed to the one runnable
     platform manifest whose blobs are in the archive. Layer media types
     (`application/vnd.oci.image.layer.v1.tar`, `…tar+gzip`, and the Docker
     equivalents) are kept as they are.
   - For a **classic `docker save`** (`manifest.json` only, Docker ≤ 24) there
     is no manifest, so Piceli writes a canonical OCI manifest
     (`application/vnd.oci.image.manifest.v1+json`, sorted keys, no
     whitespace): config `application/vnd.oci.image.config.v1+json`, layers
     `application/vnd.oci.image.layer.v1.tar` (uncompressed, as saved). The
     same image always gives the same manifest digest, which is what makes
     the next run idempotent.
   - Every layer's uncompressed digest must equal the config's `diff_ids`
     entry, and every blob must match its name and descriptor size.
3. **The gate.** The config digest recomputed from the streamed bytes must
   equal `--approve-digest`. On a mismatch the result is `rejected` /
   `digest-mismatch` and **the registry is never contacted**. For `--image`
   with Docker's classic store, the local image ID is compared first, before
   the image is even saved.
4. **Start the forward** (with `--via-forward`) and wait until it is healthy.
5. **Ask the registry what it has.** `HEAD` the manifest by digest, the tag,
   and every blob (config and layers). If the manifest and every blob are
   present and the tag already points to the manifest, the result is
   `already-present` and nothing is uploaded.
6. **Upload only the missing blobs.** The config is sent from memory. Missing
   layers are sent by reading the source a second time (`docker image save`
   again, or seeking in the archive) and hashing the bytes while they are
   sent. Blobs up to 32 MiB go in one `PUT`; larger ones go in 16 MiB `PATCH`
   chunks and are committed with the final `PUT ?digest=` only when the hash
   matched. A registry that refuses chunks gets one streamed `PUT` instead
   (`mode: monolithic-fallback`).
7. **Put the manifest.** Just before the `PUT`, the manifest's config
   descriptor is checked against the approval once more. The manifest goes
   to the tag (or to its digest when there is no tag), and the registry's
   `Docker-Content-Digest` must equal the digest Piceli computed.
8. **Verify.** The manifest is read back by digest; its bytes must hash to
   the manifest digest and it must name the approved config. The tag must
   resolve to the same digest.

A failed push can simply be run again: blobs that reached the registry are
skipped the second time.

### Receipt (`piceli.registry-delivery.v1`)

A real receipt (Docker 24 classic store, pushed through a port-forward to a
kind cluster; the second run of the same command gave `already-present` with
all four blobs skipped):

```json
{
  "schema": "piceli.registry-delivery.v1",
  "result": "pushed",
  "reason": null,
  "state": "succeeded",
  "approved_digest": "sha256:236f74224a40770314a101521111776708792a8f4434bd6887fdbda9cbcf94e7",
  "image": {
    "config_digest": "sha256:236f74224a40770314a101521111776708792a8f4434bd6887fdbda9cbcf94e7",
    "manifest_digest": "sha256:f07999a09231c883e2139c27aaa6b396d269b1b26aa91df7cb5fdf9f706ac35f",
    "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
    "manifest_origin": "synthesized",
    "platform": "linux/arm64/v8",
    "layers": 3
  },
  "blobs": {
    "total": 4, "uploaded": 2, "skipped": 2,
    "uploaded_bytes": 3621, "skipped_bytes": 46286336,
    "items": [
      {"digest": "sha256:236f…", "size": 1573, "media_type": "application/vnd.oci.image.config.v1+json",
       "action": "uploaded", "mode": "monolithic"},
      {"digest": "sha256:6d0c…", "size": 4341760, "media_type": "application/vnd.oci.image.layer.v1.tar",
       "action": "skipped"},
      {"digest": "sha256:4afe…", "size": 41944576, "media_type": "application/vnd.oci.image.layer.v1.tar",
       "action": "skipped"},
      {"digest": "sha256:1ef8…", "size": 2048, "media_type": "application/vnd.oci.image.layer.v1.tar",
       "action": "uploaded", "mode": "monolithic"}
    ]
  },
  "previous": null,
  "node_registry": "127.0.0.1:5000",
  "pull_ref": "127.0.0.1:5000/demo/big@sha256:f07999a09231c883e2139c27aaa6b396d269b1b26aa91df7cb5fdf9f706ac35f",
  "pushed": true,
  "source": {"kind": "docker-image", "format": "docker", "local_image_id": "sha256:236f74224a40770314a101521111776708792a8f4434bd6887fdbda9cbcf94e7",
             "stream_sha256": "sha256:1555c8c0b0ac6e0a07814c5c81327fb8aeddd54ef1c3a47da265784fd901cd2a",
             "bytes": 46303232},
  "target": {"registry": "127.0.0.1:15055", "repository": "demo/big", "tag": "2", "tls": false,
             "auth": "none",
             "forward": {"namespace": "oci-test", "target": "service/registry",
                         "local_port": 15055, "remote_port": 5000, "context": "kind-piceli-oci"}},
  "tools": {"docker": "sha256:…", "kubectl": "sha256:…"},
  "started_at": "2026-09-24T19:03:16.716Z",
  "finished_at": "2026-09-24T19:03:20.981Z",
  "seconds": 4.257
}
```

`result` is one of:

- `pushed`: the manifest was written (possibly with zero blob uploads, when
  only the tag moved).
- `already-present`: manifest, tag and blobs were all there; nothing was
  written.
- `rejected`, with a `reason` of `digest-mismatch` or `invalid-archive`.
- `failed`, with a `reason` such as `source-unavailable`,
  `invalid-image-stream`, `source-changed`, `forward-port-in-use`,
  `forward-unavailable`, `registry-unreachable`, `registry-unauthorized`,
  `registry-forbidden`, `registry-not-oci-v2`, `upload-rejected`,
  `manifest-rejected`, `cross-origin-location-refused`,
  `blob-digest-mismatch`, `registry-digest-mismatch`, `verification-failed`,
  `timed-out` or `cancelled`.

Other fields:

- `image.manifest_digest` is the digest of the manifest in the registry;
  `image.config_digest` is the approved config digest (set only on success).
  `manifest_origin` is `archive` (kept byte for byte) or `synthesized`.
- `blobs.items` lists each distinct blob once, in manifest order (config
  first), with `action` `uploaded` (and the upload `mode`) or `skipped`.
- `previous.tag_digest` records the manifest the tag pointed to before it
  was moved.
- `pull_ref` is `<node_registry>/<repository>@<manifest digest>`.

Receipts never contain local paths (archive, kubeconfig, credentials file),
credentials, tool output or archive contents.

### Exit codes and input rejections

| Exit | Meaning |
| --- | --- |
| 0 | `pushed`, `imported` or `already-present` |
| 1 | `rejected` or `failed`: a receipt is printed (and written), see `reason` |
| 2 | Invalid or unavailable input: nothing ran and no receipt is written |

For exit code 2 the CLI prints one line to stderr,
`{"state": "rejected", "reason": "<code>"}`. The code is fixed text; paths,
credentials and tool output are never echoed.

| Code | Meaning |
| --- | --- |
| `invalid-approved-digest` | `--approve-digest` is not `sha256:<64 hex>` |
| `invalid-timeout` | `--timeout` is not a number of seconds in (0, 3600] |
| `invalid-source` | `--image` or `--archive` is malformed |
| `invalid-target` | `--to` does not parse (`oci://`, `ssh://` or `docker://`) |
| `plain-http-not-loopback` | plain HTTP asked for a registry that is not loopback |
| `invalid-reference` | `--ref` is not a valid tagged name |
| `reference-required` | node import of an image ID without `--ref` |
| `node-options-on-registry-target` | `--ref`/`--ssh*` given with an `oci://` target |
| `registry-options-on-node-target` | registry or forward options given with a node target |
| `forward-options-incomplete` | `--via-forward` without `--namespace` and `--kubeconfig` |
| `forward-options-without-forward` | forward options given without `--via-forward` |
| `invalid-forward` | bad forward target, namespace, context or port |
| `forward-target-not-loopback` | a forwarded push whose `--to` is not a loopback host with a port |
| `node-registry-required` | a forwarded push without `--node-registry` |
| `invalid-node-registry` | `--node-registry` is not `host[:port]` |
| `docker-tool-required` | docker is needed but not on `PATH` (or not usable) |
| `docker-socket-required` | no usable unix Docker socket (explicit, `DOCKER_HOST` or context) |
| `kubectl-tool-required` | `--via-forward` but kubectl is not on `PATH` (or not usable) |
| `ssh-tool-required` | `ssh://` target but ssh is not on `PATH` (or not usable) |
| `tool-pin-mismatch` | a tool differs from its `--*-sha256` pin, or changed during the run |
| `invalid-credentials-file` | `--credentials` is missing, not a regular file, or not valid JSON |
| `credentials-file-not-private` | `--credentials` has group or world permissions |
| `invalid-ca-file` | `--ca-file` is unusable |
| `invalid-ssh-agent-socket` | `--ssh-agent-socket` is not an absolute path |
| `grant-mismatch` | (API) the grant names another target or has expired |
| `invalid-delivery-input` | any other invalid input |

In the Python API these are `piceli.artifacts.delivery_inputs.DeliveryInputError`
(a `ValueError`) with the code in `.code`.

### Python API

```python
import time
from pathlib import Path

from piceli.artifacts.delivery import DeliveryGrant, DockerImageSource
from piceli.artifacts.process import ToolPin
from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
from piceli.artifacts.registry_delivery import RegistryDelivery, RegistryForward

url = "oci://127.0.0.1:15000/team/app:1.4.2"
delivery = RegistryDelivery(
    docker=ToolPin.capture(Path("/usr/local/bin/docker")),
    docker_socket=Path("/var/run/docker.sock"),
    forward=RegistryForward(
        namespace="registry",
        target="service/registry",
        remote_port=5000,
        kubeconfig=Path("/abs/path/cluster.kubeconfig"),
        kubectl=ToolPin.capture(Path("/usr/local/bin/kubectl")),
    ),
)
receipt = delivery.deliver(
    DockerImageSource("sha256:<image ID>"),
    RegistryTarget.parse(url),
    DeliveryGrant("sha256:<config digest>", url, time.time() + 600),
    node_registry="127.0.0.1:5000",
)
print(receipt["pull_ref"])
```

- `piceli.artifacts.image_manifest.scan_image_stream(stream)` plans a push
  from a tar stream on its own (manifest, config and layer descriptors).
- `piceli.artifacts.registry.StreamedOciRegistryClient` is the OCI
  Distribution v2 client (`blob_size`, `push_blob`, `manifest_digest`,
  `get_manifest`, `push_manifest`).
- `RegistryDelivery(runner=…, forwarder=…, client_factory=…)` are the seams
  the tests use to run without Docker, kubectl or a real registry.

### Limits

- **Layers are pushed as they are in the source.** A classic `docker save`
  holds uncompressed layers, so they are pushed uncompressed. That is valid
  OCI and every containerd pulls it, but it uses more registry space and
  bandwidth than gzip. An OCI layout keeps its own (for example gzip)
  layers.
- **One image, one platform per delivery**, as for node import. zstd layers
  and foreign (`urls`) layers are refused.
- **No cross-repository mount.** A blob already present in another
  repository of the same registry is uploaded again.
- **No retry inside one run.** If the forward or the registry drops a
  connection, the run fails; the next run skips what was already uploaded.

## Node import (fallback)

`--to ssh://…` or `--to docker://…` copies one image straight into the
containerd of a Kubernetes node, with no registry in between. It streams the
image into the node runtime's `images import -`, checks on the node that the
imported image has the approved config digest, and writes a JSON receipt. The
same call is a no-op when the node already has that image.

Use it for bootstrap (before the cluster runs a registry) and for air-gapped
nodes. For everything else, prefer registry delivery.

### How a delivery runs

1. **Check the grant and tools.** The grant must name this exact target and
   must not be expired. The Docker and ssh binaries must match their SHA-256
   pins. Nothing runs if either check fails.
2. **Check the source locally.** For an archive, Piceli reads it once,
   recomputes the config digest from the config bytes and checks that the
   config file name matches its hash. For a Docker image, it compares the
   local image ID with the approval. A mismatch stops here, before any byte
   leaves the machine. With Docker's containerd image store the local ID is a
   manifest digest, so this check is skipped and step 4 decides.
3. **Skip if already present.** Piceli runs `ctr images ls` on the node and
   follows the named image's manifest (or index) with `ctr content get` to its
   config digest. If that digest is the approved one, the result is
   `already-present` and nothing is sent.
4. **Stream with a gate.** `docker image save <image ID>` (by immutable ID, not
   by a tag that could move), or the archive file, is piped through a
   validating relay into `ctr images import -`. Layer bytes are forwarded as
   they arrive; nothing is buffered on disk. The archive's index members
   (`manifest.json`, `index.json`, `oci-layout`) are held back until the whole
   stream has been read and its config digest recomputed. If the digest
   differs, the index is never sent. The runtime then fails with
   "unrecognized image format" and creates no image. Any layer blobs it had
   already stored are unreferenced, and containerd's garbage collector removes
   them.
5. **Name the image exactly.** Before the index members are sent, they are
   rewritten so that the image is imported under one reference: `--ref`, the
   `--image` name, or the archive's single tag. Other names from the archive
   are dropped.
6. **Verify on the node.** After the import, the node lookup from step 3 runs
   again. The result is `imported` only if the reference now resolves to the
   approved config digest.

### Targets

| Target | Node-side command |
| --- | --- |
| `ssh://[user@]host[:port]?runtime=k3s-containerd` | `sudo -n k3s ctr -n k8s.io images import -` |
| `ssh://[user@]host[:port]?runtime=containerd` | `sudo -n ctr -n k8s.io images import -` |
| `docker://<container>?runtime=containerd` | `docker exec -i <container> ctr -n k8s.io images import -` |
| `docker://<container>?runtime=k3s-containerd` | the same with `k3s ctr` (k3s in Docker, such as k3d) |

Options: `namespace` (default `k8s.io`, the namespace the kubelet uses) and
`sudo` (default `true` for ssh, `false` for docker). With `sudo=true` the
remote user needs passwordless `sudo` for the runtime command. `sudo -n` fails
instead of prompting.

Transport rules:

- No local shell is used. Each argument is passed to the process as-is.
- ssh runs with `BatchMode=yes`, so it fails instead of prompting. Host keys,
  identities and jump hosts come from your normal ssh configuration.
- ssh passes the remote command to the remote user's login shell. For that
  reason every remote argument must match a fixed set of characters, and each
  one is also quoted.
- The environment is not inherited. The only credential that can be passed to
  ssh is an agent socket, and only when you give it with `--ssh-agent-socket`.
- Docker is always called with an explicit `--host unix://<socket>`.

An `oci://` target is registry delivery (above), not a node target.

### CLI

```sh
python -m piceli artifacts deliver \
  --image example/app:1.4.2 \
  --to 'ssh://ops@node-1.example?runtime=k3s-containerd' \
  --approve-digest sha256:<config digest> \
  --ssh-agent-socket "$SSH_AUTH_SOCK" \
  --receipt delivery-receipt.json --journal deliveries.jsonl
```

- Use `--archive image.tar` instead of `--image` for a `docker save` or OCI
  image-layout tar. The archive must contain exactly one image.
- docker and ssh are found on `PATH` and pinned automatically, exactly as for
  registry delivery (see Tools and pins); `--docker`, `--ssh` and the
  `--*-sha256` flags override. An archive delivered over ssh needs no docker.
- The ssh agent socket is never picked up implicitly: pass
  `--ssh-agent-socket` to forward it.
- Add `--ref registry.test/app:1.4.2` to set the name on the node. It is
  required when `--image` is an image ID, or when the archive has no tag or
  more than one.
- `--timeout` (seconds, default 600, at most 3600) limits both the grant and
  the transfer.
- `--receipt` writes the receipt atomically as a mode-0600 file. `--journal`
  appends it as one JSON line.

Exit codes and input rejection codes are the same as for registry delivery
(see Exit codes and input rejections above).

### Python API

```python
import time
from pathlib import Path

from piceli.artifacts.delivery import (
    ArchiveSource,
    DeliveryGrant,
    DockerImageSource,
    NodeDelivery,
    append_journal,
)
from piceli.artifacts.node_transport import NodeTarget
from piceli.artifacts.process import ProcessLimits, ToolPin

url = "ssh://node-1.example?runtime=k3s-containerd"
delivery = NodeDelivery(ssh=ToolPin.capture(Path("/usr/bin/ssh")))
receipt = delivery.deliver(
    ArchiveSource(Path("/abs/path/app.tar")),
    NodeTarget.parse(url),
    DeliveryGrant("sha256:<config digest>", url, time.time() + 600),
    reference="registry.test/app:1.4.2",
    limits=ProcessLimits(600),
)
append_journal(Path("deliveries.jsonl"), receipt)
```

- `NodeDelivery.preview(target, digest, reference)` returns the node commands
  without contacting anything.
- `NodeDelivery.inspect_node(target, reference)` returns the config digests
  that a reference currently resolves to on the node.
- `relay_image_stream(source, destination, approved_digest=…, reference=…)` is
  the gate on its own. With `destination=None` it only validates an archive
  and reports its identity.
- `NodeDelivery(runner=…)` accepts any object with `capture`, `feed` and
  `read` methods. The tests use this to run without ssh or Docker.

### Receipt (`piceli.node-delivery.v1`)

```json
{
  "schema": "piceli.node-delivery.v1",
  "result": "imported",
  "reason": null,
  "state": "succeeded",
  "approved_digest": "sha256:…",
  "image": {
    "reference": "registry.test/app:1.4.2",
    "config_digest": "sha256:…",
    "node_target_digest": "sha256:…",
    "node_media_type": "application/vnd.docker.distribution.manifest.v2+json"
  },
  "previous": null,
  "source": {"kind": "archive", "format": "docker",
             "archive_sha256": "sha256:…", "stream_sha256": "sha256:…", "bytes": 4349440},
  "target": {"transport": "ssh", "host": "node-1.example", "runtime": "k3s-containerd",
             "namespace": "k8s.io", "sudo": true},
  "tools": {"ssh": "sha256:…"},
  "transfer": {"streamed": true, "bytes": 4349440,
               "intermediate_registry": false, "temporary_archive": false},
  "pushed": false,
  "started_at": "2026-01-01T12:00:00.000Z",
  "finished_at": "2026-01-01T12:00:03.214Z",
  "seconds": 3.214
}
```

`result` is one of:

- `imported`
- `already-present`
- `rejected`, with a `reason` of `digest-mismatch`, `invalid-archive` or
  `reference-required`
- `failed`, with a `reason` of `source-unavailable`, `node-query-failed`,
  `invalid-image-stream`, `import-failed`, `import-timed-out`,
  `import-cancelled`, `cancelled` or `verification-failed`

Other fields:

- `previous` records the config digests that the reference pointed to before
  it was replaced.
- For a Docker image source, `source` holds `local_image_id` instead of
  `archive_sha256`.
- `node_target_digest` is the manifest digest that the node created. It is
  kept for reference only; the approval is always compared with
  `config_digest`.

Receipts never contain local paths, the ssh user, tool output, archive
contents, sockets or credentials.

### Trying it on kind

A kind node is a container that runs containerd, which makes it a disposable
target for delivery. Always pass `--kubeconfig` with a separate file, so that
your default kubeconfig and current context are never changed.

```sh
kind create cluster --name delivery-demo --kubeconfig /tmp/delivery-demo.kubeconfig
docker pull busybox:1.36
ID=$(docker image inspect --format '{{.Id}}' busybox:1.36)   # classic store
python -m piceli artifacts deliver --image busybox:1.36 \
  --to 'docker://delivery-demo-control-plane?runtime=containerd' \
  --approve-digest "$ID"
docker exec delivery-demo-control-plane ctr -n k8s.io images ls | grep busybox
docker exec delivery-demo-control-plane crictl images | grep busybox  # IMAGE ID = config digest
kind delete cluster --name delivery-demo --kubeconfig /tmp/delivery-demo.kubeconfig
```

The repository's opt-in end-to-end test does the same with generated images
in both archive formats. It checks idempotency, a wrong digest, and a real
`ctr` rejecting a stream whose index was held back:

```sh
PICELI_KIND_NODE=delivery-demo-control-plane \
PICELI_DOCKER_SOCKET=/var/run/docker.sock \
  uv run pytest tests/integration/test_node_delivery_kind.py
```

### Limits

- **One image, one platform per delivery.** An OCI index must resolve to
  exactly one runnable manifest. Attestation manifests are ignored.
- **Platform is not checked.** Delivering an image for another architecture is
  not rejected here. The container runtime handles it (usually the unpack or
  the container start fails).
- **Pods keep running the old image.** Replacing a tag on a node does not
  restart pods that already run the previous image. Roll them out yourself;
  the receipt's `previous` field records what was replaced.
- **Layer blobs from a rejected stream may stay briefly.** They are
  unreferenced, and containerd garbage-collects them.
