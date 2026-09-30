# Registry retention

A registry that every release pushes to only grows. A node-local registry
fills the node's disk (a few hundred MB per release for an app with several
images); a hosted registry fills a quota. `piceli artifacts retention` tells
you which manifests a registry must keep and which it can drop, and with your
approval deletes the rest.

```{admonition} Maturity: experimental
:class: note

New in 0.13.0; it may change in a minor release, with a changelog entry. See
the {doc}`roadmap`.
```

## What is kept

A manifest (an image, or an image index with its per-platform manifests) is
**kept** when any of these holds; everything else is **collectable**.

| Rule | Source |
| --- | --- |
| One of the last `--keep N` **releases** (default 3), newest first | the publish receipts (`artifacts publish --out`), registry-delivery receipts (`artifacts deliver --receipt`) and JSON Lines journals (`--journal`) you pass with `--receipts` (files or directories) |
| **Pinned** | `--pin sha256:…` (repeatable) and `--pin-file FILE` (one digest per line, `#` comments) |
| **Live**: a running workload uses it | the pod specs and statuses of a cluster (`--kubeconfig FILE --context NAME`, optionally `--live-namespace`) or `--live-file` |
| A child or a referrer of a kept manifest | the platform manifests of a kept index; its SBOM, provenance and signatures (OCI referrers and `sha256-<hex>` fallback tags) |
| **Unledgered**: tagged, but no receipt mentions it | kept by default; `--collect-unledgered` collects it |

A release's receipt names a repository and a digest, never a host, so the same
receipts serve a registry reached directly, through `--via-forward`, or by a
node address.

**Budget mode.** `--budget 10GiB` keeps more releases than `--keep`, newest
first, while everything kept (pinned and live digests included) stays within
the budget. Blobs are **deduplicated**: layers shared between releases, and
between images, count once. It never keeps fewer than `--keep` releases and
stops at the first release that does not fit. Sizes: `B`, `KB`/`MB`/`GB`/`TB`
(powers of 1000), `KiB`/`MiB`/`GiB`/`TiB` (powers of 1024).

## Report, then delete

```sh
# Report only: reads the registry and the cluster, changes nothing (exit 0).
piceli artifacts retention --to oci://127.0.0.1:5000/shop \
    --receipts dist/receipts/ --keep 3 --budget 10GiB \
    --kubeconfig ~/k/my-cluster.yaml --context my-cluster \
    --via-forward service/registry --namespace registry --forward-remote-port 5000

# Delete: the same command with --delete prints the plan and its hash (exit 3)...
piceli artifacts retention … --delete
# ...review the `collectable.deletions`, then approve exactly that hash.
piceli artifacts retention … --delete --approve <digest>
```

The report is one JSON object: `policy`, `live` (how many digests were read
and from where), `releases` (kept or not, and why), one row per manifest with
its tags, bytes, `kept` and `reasons` (`release`, `budget`, `pinned`, `live`,
`referenced`, `unledgered`, `unreferenced`), `kept.bytes`,
`collectable.reclaimable_bytes` and the `digest` of the plan. `--out FILE`
also writes it.

Deleting is approval-gated like every Piceli change. The digest covers the
registry and the exact list of `(repository, digest)` deletions, and the plan
is computed again when you pass `--approve`: if a release landed, a pin
changed or a workload started using one of the digests since you reviewed it,
the hash differs and nothing is deleted (`retention-not-approved`).

Safety rules that hold whatever the options:

- **A digest a running workload uses is never deleted.** `--delete` needs a
  live source (`retention-live-unknown` otherwise): an unknown inventory never
  licenses a deletion. Tag-only pod images keep the digest their tag names.
- Children are deleted after their index; a kept index never loses a child.
- A registry that refuses deletes answers with `registry-delete-disabled`
  (HTTP 405): enable `REGISTRY_STORAGE_DELETE_ENABLED=true` (the node-local
  registry already does). The receipt lists what was deleted before the refusal.
- Credentials come only from `--credentials FILE` or `--docker-config FILE`;
  plain HTTP is only for a loopback registry.

## Reclaiming the space

Deleting a manifest removes the reference, not the layers: the registry frees
disk when **its** garbage collector runs. The report's `reclaimable_bytes`
(and the receipt's `reclaimed_bytes`) are the deduplicated bytes of the blobs
and manifests that no kept manifest references, which is what the collector
frees.

- **Piceli's node-local registry:** apply `registry.garbage_collection(
  delete_untagged=False)` (see {doc}`node_local_registry`). Keep
  `delete_untagged=False`: images pushed by digest have no tag, and
  `--delete-untagged` would delete them all.
- **`distribution/registry`:** `registry garbage-collect
  /etc/docker/registry/config.yml` inside the registry container, with pushes
  stopped or the registry read-only.
- **Hosted registries** collect on their own schedule; some (Docker Hub, GHCR)
  have their own retention settings instead of the delete API.

## What it cannot see

- Repositories are found from the receipts, `--repository` (repeatable) and,
  when the registry serves it, `/v2/_catalog`, under the prefix of `--to`.
- Manifests that nothing tags and no receipt names are invisible to the
  registry API; a blob garbage collection without `--delete-untagged` leaves
  them alone.
- Space shared with repositories outside `--to` is not counted as reclaimable
  for that registry's other users.
- Live workloads are read from pods. A Deployment scaled to zero, a CronJob
  between runs or a rollback target that no pod uses is **not** live: pin its
  digest (`--pin`), or keep enough releases.

## How often

Run the report after every release, and delete with the approved plan when it
shows collectable manifests or the registry is over its budget (for a
node-local registry: weekly, or from the release routine right after a
successful release, followed by the garbage-collection Job).

## Error codes

| Code | When |
| --- | --- |
| `retention-invalid` | a malformed `--keep`, `--budget`, pin, receipt or option combination |
| `retention-live-unknown` | `--delete` without a live source, or reading the pods failed |
| `retention-not-approved` | `--approve` is not the hash of the plan computed now |
| `registry-delete-disabled` | the registry refuses `DELETE` |

The registry errors of {doc}`artifact_delivery` (`registry-unauthorized`,
`registry-unreachable`, …) apply as well.
