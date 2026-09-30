"""``piceli env push BRANCH MODULE:ATTR``: record a laptop-built digest for a branch.

A branch environment deploys images by digest. When the image was built on a
laptop (``piceli deploy --plan``/host build, or ``piceli build job``), this
command records its digest where ``env up`` and the gitops controller read
it: the ConfigMap ``piceli-env-<branch slug>`` in the environment's
namespace, with keys

``images``
    JSON ``{image: {"digest": "sha256:…", "pull_ref": "host/repo@sha256:…"?,
    "platform": "linux/arm64"?}}``
``commit``
    the source commit, when the receipt records one
``pushed_at``
    UTC timestamp (the environment budget evicts the least recently pushed)

It pushes no image and deploys nothing. Like every change it plans first:
without ``--approve`` it prints the plan and its hash (exit 3); ``--approve
HASH`` writes the ConfigMap (creating the namespace when it does not exist).

Output follows the CLI contract: one JSON object on stdout, human text on
stderr. Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, reject, say

SCHEMA = "piceli.env-push.v1"
MANAGER = "piceli-env-push"
CONFIGMAP_PREFIX = "piceli-env-"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")

app = typer.Typer(
    rich_markup_mode=None,
    help="Branch environments.",
    no_args_is_help=True,
)


def configmap_name(branch: str) -> str:
    """``piceli-env-<branch slug>`` (at most 63 characters)."""
    from piceli.artifacts.cluster_build import slug

    return f"{CONFIGMAP_PREFIX}{slug(branch, 63 - len(CONFIGMAP_PREFIX))}"


def images_from_receipt(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{image: {digest, pull_ref?, platform?}}`` of a host-build receipt."""
    entries = (receipt.get("outputs") or {}).get("images")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("the receipt has no images")
    images: dict[str, dict[str, Any]] = {}
    for name, entry in entries.items():
        digest = entry.get("digest") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError(f"image {name!r} has no manifest digest")
        item: dict[str, Any] = {"digest": digest}
        ref = (entry.get("delivered") or {}).get("pull_ref")
        if isinstance(ref, str):
            item["pull_ref"] = ref
        if isinstance(entry.get("platform"), str):
            item["platform"] = entry["platform"]
        images[name] = item
    return images


def images_from_digests(values: list[str]) -> dict[str, dict[str, Any]]:
    images: dict[str, dict[str, Any]] = {}
    for value in values:
        name, _, digest = value.partition("=")
        if not _IMAGE.fullmatch(name) or not _DIGEST.fullmatch(digest):
            raise ValueError("--digest is IMAGE=sha256:<64 hex>")
        images[name] = {"digest": digest}
    return images


def plan_push(
    branch: str,
    namespace: str,
    images: dict[str, dict[str, Any]],
    commit: str | None,
) -> dict[str, Any]:
    """The ConfigMap to write (without the timestamp) and its hash."""
    data = {"images": json.dumps(images, sort_keys=True)}
    if commit:
        data["commit"] = commit
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name(branch),
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "piceli",
                "piceli.io/env-push": "true",
            },
        },
        "data": data,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return {
        "configmap": body,
        "plan_hash": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
    }


def apply_push(cluster: Any, branch: str, plan: dict[str, Any]) -> None:
    """Create the namespace if needed, then create or replace the ConfigMap."""
    from kubernetes.client.exceptions import ApiException

    body = json.loads(json.dumps(plan["configmap"]))
    body["data"]["pushed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    namespace = body["metadata"]["namespace"]
    if cluster._get(f"/api/v1/namespaces/{namespace}") is None:
        cluster._send(
            "/api/v1/namespaces",
            "POST",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": namespace,
                    "labels": {
                        "app.kubernetes.io/managed-by": "piceli",
                        "piceli.io/env-branch": configmap_name(branch)[
                            len(CONFIGMAP_PREFIX) :
                        ],
                    },
                },
            },
            fieldManager=MANAGER,
        )
    path = f"/api/v1/namespaces/{namespace}/configmaps"
    name = body["metadata"]["name"]
    try:
        if cluster._get(f"{path}/{name}") is None:
            cluster._send(path, "POST", body, fieldManager=MANAGER)
        else:
            cluster._send(f"{path}/{name}", "PUT", body, fieldManager=MANAGER)
    except ApiException as error:
        reject(
            "env-push-invalid",
            f"the API refused to write the ConfigMap (HTTP {error.status})",
        )


@app.command("push")
def push(
    branch: Annotated[str, typer.Argument(help="The branch", show_default=False)],
    target: Annotated[
        str,
        typer.Argument(
            help="The pipeline, MODULE:ATTR or path/to/file.py:ATTR",
            show_default=False,
        ),
    ],
    receipt: Annotated[
        Path | None,
        typer.Option("--receipt", help="A host-build receipt (JSON)"),
    ] = None,
    digest: Annotated[
        list[str] | None,
        typer.Option("--digest", help="IMAGE=sha256:<digest>; repeat per image"),
    ] = None,
    commit: Annotated[
        str | None, typer.Option("--commit", help="The source commit of the build")
    ] = None,
    namespace: Annotated[
        str | None,
        typer.Option(
            "--namespace",
            help="The environment's namespace (default: from the pipeline's envs)",
        ),
    ] = None,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan hash to execute")
    ] = None,
    env: Annotated[
        str | None, typer.Option("--env", help="The pipeline's environment")
    ] = None,
) -> None:
    """Record a laptop-built digest for a branch environment (plan, then --approve HASH)."""
    from piceli.k8s.cli.maintenance import _pipeline

    if (receipt is None) == (not digest):
        reject("env-push-invalid", "give --receipt FILE or --digest IMAGE=sha256:...")
    try:
        if receipt is not None:
            document = json.loads(receipt.read_text())
            images = images_from_receipt(document)
            commit = commit or (
                document.get("commit") if isinstance(document, dict) else None
            )
        else:
            images = images_from_digests(list(digest or []))
    except (OSError, ValueError, AttributeError):
        reject(
            "env-push-invalid",
            "the receipt or digests are not valid (a host-build receipt with "
            "image digests, or IMAGE=sha256:<64 hex>)",
        )
    pipeline = _pipeline(target, env)
    if namespace is None:
        try:
            from piceli.envs import namespace_for  # type: ignore[attr-defined]

            namespace = namespace_for(pipeline, branch)
        except (ImportError, TypeError, AttributeError):
            reject(
                "env-push-invalid",
                "the pipeline declares no branch environments; pass --namespace",
            )
    plan = plan_push(branch, namespace, images, commit)
    say(
        f"env push {branch}: {len(images)} image(s) into "
        f"{namespace}/{plan['configmap']['metadata']['name']}"
    )
    if approve is None:
        say(
            f"approve with: piceli env push {branch} {target} ... --approve {plan['plan_hash']}"
        )
        emit_json(
            {
                "schema": SCHEMA,
                "state": "approval-required",
                "plan_hash": plan["plan_hash"],
                "namespace": namespace,
                "configmap": plan["configmap"]["metadata"]["name"],
                "images": images,
            }
        )
        raise typer.Exit(EXIT_APPROVAL)
    if approve != plan["plan_hash"]:
        reject(
            "env-push-invalid", "the approved hash is not the current plan; plan again"
        )
    from piceli.pipeline.backend import Backend

    cluster = Backend().prerollout_cluster(pipeline.target)
    try:
        apply_push(cluster, branch, plan)
    finally:
        cluster.close()
    emit_json(
        {
            "schema": SCHEMA,
            "state": "recorded",
            "namespace": namespace,
            "configmap": plan["configmap"]["metadata"]["name"],
            "images": images,
        }
    )
