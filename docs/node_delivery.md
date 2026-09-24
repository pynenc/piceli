# Direct node delivery

`piceli artifacts deliver` copies one image straight into the containerd of a
Kubernetes node, with no registry in between. It streams the image into the
node runtime's `images import -`, checks on the node that the imported image
has the approved digest, and writes a JSON receipt. The same call is a no-op
when the node already has that image.

Use it for small or air-gapped clusters (k3s on single-board computers, lab
nodes, kind) where running a registry only to move a few images is more
infrastructure than the images themselves.

## What is approved: the config digest

`--approve-digest` is the image's **config digest**: the SHA-256 of the image
configuration JSON. Docker's classic image store uses it as the image ID
(`docker image inspect --format '{{.Id}}'`), and the Kubernetes CRI uses it as
the image ID too (`crictl images`, `crictl inspecti`).

The config digest is used because it is the only identifier that stays the same
on both sides of a delivery:

| Where | Manifest digest | Config digest |
| --- | --- | --- |
| Registry (compressed layers) | registry manifest | same |
| `docker save` (Docker ≤ 24) | none, the archive has no manifest | file name `<hex>.json` |
| `docker save` (Docker ≥ 25) | a manifest written locally | `blobs/sha256/<hex>` |
| After `ctr images import` | a manifest that `ctr` creates | same |

A manifest digest depends on layer compression and on who wrote the manifest,
so it is not comparable. The config lists the uncompressed digest of every
layer (`rootfs.diff_ids`), and containerd checks those digests when it unpacks
the layers. So the config digest also fixes the content of every layer.

## How a delivery runs

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

## Targets

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

Registry delivery (`--to registry://…`) is not a node target. To push to an
owner-operated registry, use `piceli.artifacts.registry.StreamedOciRegistryClient`.

## CLI

```sh
DOCKER=$(realpath "$(command -v docker)")
python -m piceli artifacts deliver \
  --image example/app:1.4.2 \
  --to 'ssh://ops@node-1.example?runtime=k3s-containerd' \
  --approve-digest sha256:<config digest> \
  --docker "$DOCKER" --docker-sha256 "sha256:$(shasum -a 256 "$DOCKER" | cut -d' ' -f1)" \
  --docker-socket /var/run/docker.sock \
  --ssh /usr/bin/ssh --ssh-sha256 "sha256:$(shasum -a 256 /usr/bin/ssh | cut -d' ' -f1)" \
  --ssh-agent-socket "$SSH_AUTH_SOCK" \
  --receipt delivery-receipt.json --journal deliveries.jsonl
```

- Use `--archive image.tar` instead of `--image` for a `docker save` or OCI
  image-layout tar. The archive must contain exactly one image.
- An archive delivered over ssh needs no Docker pins.
- Add `--ref registry.test/app:1.4.2` to set the name on the node. It is
  required when `--image` is an image ID, or when the archive has no tag or
  more than one.
- `--timeout` (seconds, default 600, at most 3600) limits both the grant and
  the transfer.
- `--receipt` writes the receipt atomically as a mode-0600 file. `--journal`
  appends it as one JSON line.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | `imported` or `already-present` |
| 1 | `rejected` or `failed` (see `reason`) |
| 2 | Invalid input. A fixed error is printed; paths and tool output are never echoed. |

## Python API

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

## Receipt (`piceli.node-delivery.v1`)

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

## Trying it on kind

A kind node is a container that runs containerd, which makes it a disposable
target for delivery. Always pass `--kubeconfig` with a separate file, so that
your default kubeconfig and current context are never changed.

```sh
kind create cluster --name delivery-demo --kubeconfig /tmp/delivery-demo.kubeconfig
docker pull busybox:1.36
ID=$(docker image inspect --format '{{.Id}}' busybox:1.36)   # classic store
python -m piceli artifacts deliver --image busybox:1.36 \
  --to 'docker://delivery-demo-control-plane?runtime=containerd' \
  --approve-digest "$ID" --docker … --docker-sha256 … --docker-socket …
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

## Limits

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
