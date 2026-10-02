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
| One of the last `--keep N` **releases** (default 3; `--keep 0` keeps none, see below), newest first | the publish receipts (`artifacts publish --out`), registry-delivery receipts (`artifacts deliver --receipt`) and JSON Lines journals (`--journal`) you pass with `--receipts` (files or directories) |
| **Pinned** | `--pin sha256:…` (repeatable) and `--pin-file FILE` (one digest per line, `#` comments) |
| **Live**: a workload uses or will pull it, or an environment names it | read from the cluster (`--kubeconfig FILE --context NAME`, optionally `--live-namespace`): the pods that have not finished (specs and statuses' `imageID`), **the pod templates** of Deployments, StatefulSets, DaemonSets, ReplicaSets, CronJobs and unfinished Jobs, so a workload scaled to zero or a CronJob between runs still finds its image, and Piceli's ConfigMaps: the environment records (`piceli-env`) and pushed images (`piceli-env-<branch>`) of branch environments, and the GitOps controller's status (`piceli-gitops-status`); `--live-file` adds digests |
| A child or a referrer of a kept manifest | the platform manifests of a kept index; its SBOM, provenance and signatures (OCI referrers and `sha256-<hex>` fallback tags) |
| **Unledgered**: tagged, but no receipt mentions it | kept by default; `--collect-unledgered` collects it |

A release's receipt names a repository and a digest, never a host, so the same
receipts serve a registry reached directly, through `--via-forward`, or by a
node address. `piceli deploy` writes one delivery receipt per image into
`<state_dir>/deliveries/`: pass that directory. "Release *k*" is, for every
repository, its *k*-th newest distinct digest, so `--keep 3` keeps each
image's last three digests; images that change together line up, and an image
that did not change in a release keeps its newest digest.

**Live means what runs or is referenced now**, never that a receipt exists.
Three kinds of objects still name an image but will never pull it again, so
they do not make it live: a pod that `Succeeded` or `Failed`, a Job with a
true `Complete` or `Failed` condition, and a ReplicaSet a Deployment owns
that is scaled to zero with no pod left (its rollout history). A component
that moved to another registry (say a controller now pulled from a hosted
registry by another digest) leaves its old copy collectable. Pin a digest
(`--pin`) to keep a `kubectl rollout undo` target.

**Manifests without a tag or receipt.** The registry API lists tags, not
untagged manifests, and images delivered to `Registry.in_cluster` are pushed
by digest. So besides the catalog, tags and receipts, retention looks up
every digest the cluster still names (live or not: finished pods and Jobs,
rollout history, environment records) in the repository its reference
names. A manifest found that way and used by nothing live is collectable,
with `why: ["not-live"]` and the objects that still name it in `seen_in`.

**`--keep 0`** keeps no release: only live, pinned and unledgered (tagged,
no receipt) manifests and their children and referrers stay. It needs the
cluster read (`--kubeconfig` with `--context`); with only `--live-file`, or
none, it is refused (`retention-invalid`), because the live inventory is then
the only protection. It never deletes anything live: live digests and the
index and platform manifests of a live multi-platform image are kept by
construction, and the delete refuses any plan that names one.

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
and from where, by kind: `pod`, `deployment`, … `environment`, `controller`),
`releases` (kept or not, and why), one row per manifest with its tags, bytes,
`kept`, `reasons` (`release`, `budget`, `pinned`, `live`, `referenced`,
`unledgered`, `unreferenced`) and `live_by`, `kept.bytes`,
`collectable.reclaimable_bytes` and the `digest` of the plan. Each entry of
`collectable.deletions` names exactly what would be removed: `repository`,
`digest`, `bytes` (the manifest and its layers, before deduplication), `why`
it is not kept (`old-release` or `over-budget` for a release past the policy,
`not-live` when only finished or rollout-history objects name it,
`unledgered`, else `unreferenced`) and `seen_in` (for example
`replicaset:history`, `pod:finished`, `job:finished`). `--out FILE` also
writes it.

To remove every image nothing uses, for example on an in-cluster registry
reached through a port-forward (`--to` takes a repository prefix: run it once
per top-level prefix, such as `shop` for `shop/api` and `shop/worker`):

```sh
# Plan: prints the deletions and the hash (exit 3), changes nothing.
piceli artifacts retention --to oci://127.0.0.1:5000/shop \
    --receipts <state_dir>/deliveries/ --keep 0 \
    --kubeconfig ~/k/my-cluster.yaml --context my-cluster \
    --via-forward service/piceli-registry --namespace piceli-system \
    --forward-remote-port 5000 --delete
# Apply exactly that plan.
piceli artifacts retention … --delete --approve <digest>
```

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

- **Piceli's in-cluster registry (`Registry.in_cluster`):** run the
  registry's collector inside its pod, with the same explicit kubeconfig and
  context, at a time when no deploy, cluster build or mirror is pushing:

  ```sh
  kubectl --kubeconfig ~/k/my-cluster.yaml --context my-cluster \
      -n piceli-system exec deploy/piceli-registry -- \
      registry garbage-collect /etc/distribution/config.yml
  ```

  Add `--dry-run` first to see what it would remove. Never add
  `--delete-untagged`: images are pushed by digest, untagged, and it would
  delete every one of them. Piceli's registry configuration has no blob
  descriptor cache, so no restart is needed afterwards; `piceli registry
  status` shows the claim's usage. (For another name or namespace, use the
  registry's Deployment name and namespace.)
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
- Manifests that nothing tags, no receipt names and no object of the cluster
  names any more are invisible to the registry API; a blob garbage collection
  without `--delete-untagged` leaves them alone. Pass their digests with
  `--live-file` only to keep them, never to find them.
- A live digest read from a `--live-file` (or an environment record) without
  a repository is looked up in every repository under `--to`.
- Space shared with repositories outside `--to` is not counted as reclaimable
  for that registry's other users.
- Live workloads are read from the cluster you name. Workloads in namespaces you
  did not scan, on other clusters, or created after the report are not seen: pin
  their digests (`--pin`) or scan every namespace (the default).

## How often

Run the report after every release, and delete with the approved plan when it
shows collectable manifests or the registry is over its budget (for a
node-local registry: weekly, or from the release routine right after a
successful release, followed by the garbage-collection Job).

## Error codes

| Code | When |
| --- | --- |
| `retention-invalid` | a malformed `--keep`, `--budget`, pin, receipt or option combination; `--keep 0` without `--kubeconfig` and `--context` |
| `retention-live-unknown` | `--delete` without a live source, or reading the pods, workloads or Piceli ConfigMaps failed |
| `retention-not-approved` | `--approve` is not the hash of the plan computed now |
| `registry-delete-disabled` | the registry refuses `DELETE` |

The registry errors of {doc}`artifact_delivery` (`registry-unauthorized`,
`registry-unreachable`, …) apply as well.
