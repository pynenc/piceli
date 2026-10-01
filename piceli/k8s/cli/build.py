"""``piceli build job``: run the pipeline's host builds as a Job in the cluster.

``build job MODULE:ATTR --commit SHA --image BUILDER --repo URL`` plans a Job
on a labelled builder node (see :mod:`piceli.artifacts.cluster_build`):
without ``--approve`` it prints the plan and its hash (exit ``3``);
``--approve HASH`` creates the cache claim if missing and the Job, waits, and
prints a summary (image digests by platform). The full receipt is written
(private file) to ``--receipt-out``, default
``<state_dir>/cluster-builds/<commit>.json``.

``build job-run`` is the command the Job itself runs; it prints the receipt as
one line.

Output follows the CLI contract: one JSON object on stdout, human text on
stderr. Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, EXIT_FAILED, emit_json, reject, say

SCHEMA = "piceli.build-job.v1"

app = typer.Typer(
    rich_markup_mode=None,
    help="Builds that run in the cluster.",
    no_args_is_help=True,
)


def _selector(values: list[str] | None) -> dict[str, str] | None:
    if not values:
        return None
    result = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            reject("cluster-build-invalid", "--selector is KEY=VALUE")
        result[key] = value
    return result


@app.command("job")
def job(
    target: Annotated[
        str,
        typer.Argument(
            help="The pipeline, MODULE:ATTR or path/to/file.py:ATTR",
            show_default=False,
        ),
    ],
    commit: Annotated[str, typer.Option("--commit", help="Full Git commit id")],
    image: Annotated[
        str, typer.Option("--image", help="Builder image, name@sha256:...")
    ],
    repo: Annotated[
        str, typer.Option("--repo", help="Git remote URL (no credentials)")
    ],
    cache_key: Annotated[
        str | None, typer.Option("--cache-key", help="The branch (default: the commit)")
    ] = None,
    platform: Annotated[
        list[str] | None,
        typer.Option("--platform", help="linux/arm64, linux/amd64 (repeat)"),
    ] = None,
    git_secret: Annotated[
        str, typer.Option("--git-secret", help="Secret with keys username, password")
    ] = "piceli-build-git",
    selector: Annotated[
        list[str] | None,
        typer.Option("--selector", help="Builder node label KEY=VALUE (repeat)"),
    ] = None,
    namespace: Annotated[str | None, typer.Option("--namespace")] = None,
    storage: Annotated[
        str, typer.Option("--storage", help="Cache claim size")
    ] = "20Gi",
    storage_class: Annotated[str | None, typer.Option("--storage-class")] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Job deadline in seconds")
    ] = 3600,
    registry_url: Annotated[
        str | None, typer.Option("--registry-url", help="oci://host[:port]/prefix")
    ] = None,
    node_registry: Annotated[str | None, typer.Option("--node-registry")] = None,
    repo_root: Annotated[Path | None, typer.Option("--repo-root")] = None,
    receipt_out: Annotated[
        Path | None, typer.Option("--receipt-out", help="Where to write the receipt")
    ] = None,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan hash to execute")
    ] = None,
    env: Annotated[str | None, typer.Option("--env")] = None,
) -> None:
    """Build at a commit as a Job on a builder node (plan, then --approve HASH)."""
    from piceli.artifacts.cluster_build import (
        DEFAULT_PLATFORMS,
        DEFAULT_SELECTOR,
        ClusterBuildConfig,
        plan_build_job,
        run_build_job,
    )
    from piceli.k8s.cli.maintenance import _pipeline
    from piceli.pipeline.errors import PipelineError

    pipeline = _pipeline(target, env)
    try:
        config = ClusterBuildConfig(
            image=image,
            repo=repo,
            git_secret=git_secret,
            selector=_selector(selector) or dict(DEFAULT_SELECTOR),
            namespace=namespace,
            storage=storage,
            storage_class=storage_class,
            timeout_seconds=timeout,
            registry_url=registry_url,
            node_registry=node_registry,
            repo_root=repo_root,
        )
        platforms = tuple(platform or DEFAULT_PLATFORMS)
        key = cache_key or commit
        plan = plan_build_job(
            pipeline, commit, cache_key=key, platforms=platforms, config=config
        )
        say(
            f"build job {plan.job['metadata']['name']}: {commit[:12]} for "
            f"{', '.join(plan.platforms)} on {plan.job['spec']['template']['spec']['nodeSelector']}"
        )
        if approve is None:
            say(
                f"approve with: piceli build job {target} ... --approve {plan.plan_hash}"
            )
            emit_json(
                {"schema": SCHEMA, "state": "approval-required", **plan.preview()}
            )
            raise typer.Exit(EXIT_APPROVAL)
        receipt = run_build_job(
            pipeline,
            commit,
            cache_key=key,
            platforms=platforms,
            config=config,
            approve=approve,
            say=say,
        )
    except PipelineError as error:
        if error.failed:
            emit_json(
                {
                    "schema": SCHEMA,
                    "state": "failed",
                    "reason": error.code,
                    **error.details,
                }
            )
            say(f"failed: {error} [{error.code}]")
            raise typer.Exit(EXIT_FAILED) from None
        reject(error.code, str(error))
    from piceli.pipeline.journal import write_private

    path = (
        receipt_out or pipeline.state_dir / "cluster-builds" / f"{commit[:12]}.json"
    ).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    emit_json(
        {
            "schema": SCHEMA,
            "state": "succeeded",
            "commit": commit,
            "receipt": str(path),
            "job": receipt.get("job"),
            "images": receipt.get("delivered", {}),
        }
    )


@app.command("job-run")
def job_run(
    source: Annotated[Path, typer.Option("--source")],
    cache: Annotated[Path, typer.Option("--cache")],
    out: Annotated[Path, typer.Option("--out")],
    commit: Annotated[str, typer.Option("--commit")],
    registry_url: Annotated[str, typer.Option("--registry-url")],
    spec: Annotated[list[str], typer.Option("--spec", help="host-build.toml (repeat)")],
    node_registry: Annotated[str | None, typer.Option("--node-registry")] = None,
    timeout: Annotated[int, typer.Option("--timeout")] = 3600,
) -> None:
    """Inside the build Job: build and push; print the receipt line."""
    import sys

    from piceli.artifacts.build_spec import BuildSpecError
    from piceli.artifacts.cluster_build import encode_receipt
    from piceli.artifacts.cluster_build import job_run as run
    from piceli.artifacts.node_facts import NodeFacts, NodeFactsError
    from piceli.pipeline.errors import PipelineError

    try:
        raw: dict[str, Any] = json.loads(os.environ.get("PICELI_BUILD_FACTS", ""))
        facts = {name: NodeFacts.from_dict(value) for name, value in raw.items()}
    except (ValueError, NodeFactsError, AttributeError):
        reject("node-facts-unavailable", "PICELI_BUILD_FACTS is missing or invalid")
    try:
        receipt = run(
            source=source,
            specs=spec,
            cache=cache,
            out=out,
            registry_url=registry_url,
            node_registry=node_registry,
            facts=facts,
            commit=commit,
            timeout_seconds=timeout,
            say=say,
        )
    except BuildSpecError as error:
        reject(error.code, str(error))
    except PipelineError as error:
        reject(error.code, str(error), exit_code=EXIT_FAILED)
    sys.stdout.write("\n" + encode_receipt(receipt) + "\n")
    sys.stdout.flush()
