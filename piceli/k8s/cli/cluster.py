"""``piceli cluster init|status`` and ``piceli secrets git``.

- ``cluster init MODULE:ATTR`` plans everything a declared
  :class:`~piceli.infra.Cluster` needs (node role labels, the in-cluster
  registry and its node mirrors, the GitOps controller's foundation, the UI)
  and applies it with ``--approve HASH`` (exit 3 with the plan until then).
  Idempotent: a re-run plans only what changed.
- ``cluster status MODULE:ATTR`` shows nodes, registry and mirrors (and which
  k3s nodes need a restart), controller, UI and the Git Secret (read-only).
- ``secrets git --cluster MODULE:ATTR --prompt`` reads a Git token from stdin
  (no echo), never from an argument or the environment, and writes the
  Secret ``piceli-build-git`` (``username``, ``password``) the controller and
  cluster build Jobs use. It prints names only.

The cluster is reached with the declaration's credential profile
(``Cluster(credentials=)``, or ``--profile NAME``), whose API server must be
``Cluster(api=)``; never the ambient context. Machine output is one JSON
object on stdout; human text on stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, reject, say
from piceli.k8s.cli.cluster_access import AllowExecOption, ExecSha256Option, exec_policy

if TYPE_CHECKING:
    from piceli.gitops.install import Api
    from piceli.infra import Cluster

app = typer.Typer(
    rich_markup_mode=None,
    help="Set a declared cluster up (piceli.infra.Cluster) and show its state.",
    no_args_is_help=True,
)
secrets_app = typer.Typer(
    rich_markup_mode=None,
    help="Credentials stored in the cluster (typed, never passed as arguments).",
    no_args_is_help=True,
)

ClusterArgument = Annotated[
    str,
    typer.Argument(
        help="MODULE:ATTR (or path/file.py:ATTR) of a piceli.infra.Cluster",
        show_default=False,
    ),
]
ApproveOption = Annotated[
    str | None, typer.Option("--approve", help="The plan hash to execute")
]
TransportOption = Annotated[
    str,
    typer.Option("--transport", help="https, or loopback-http for a local test API"),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON object on stdout")
]
#: The variable ``secrets git`` refuses to run with (a token never comes from
#: the environment, where it would leak into process listings and logs).
TOKEN_ENV = "PICELI_GIT_TOKEN"


@contextmanager
def _guard() -> Iterator[None]:
    from piceli.gitops import GitOpsError
    from piceli.infra.cluster import ClusterError
    from piceli.pipeline.errors import PipelineError
    from piceli.profiles import ProfileError

    try:
        yield
    except ClusterError as error:
        reject(error.code, str(error))
    except ProfileError as error:
        reject(error.code or "profile-invalid", str(error))
    except GitOpsError as error:
        code = {
            "gitops-cluster-failed": "cluster-api-failed",
            "gitops-plan-changed": "cluster-plan-changed",
        }.get(error.code, error.code)
        reject(code, str(error))
    except PipelineError as error:
        reject("cluster-invalid", str(error))
    except ValueError as error:
        # The UI installer's refusals (UiInstallError) carry their own code.
        code = getattr(error, "code", None)
        if not isinstance(code, str):
            raise
        reject(code, str(error))


def load_cluster(ref: str) -> Cluster:
    """The ``Cluster`` that ``ref`` names (a registered refusal otherwise)."""
    from piceli.app.render import RenderError, load_target
    from piceli.infra import Cluster
    from piceli.infra.cluster import ClusterError

    try:
        value = load_target(ref, Path.cwd())
    except RenderError as error:
        reject("cluster-not-found", str(error))
    except ClusterError as error:
        reject(error.code, str(error))
    except Exception as error:  # the module raised while importing
        from piceli.cli_contract import describe_user_error

        reject(
            "cluster-load-failed",
            f"importing {ref} failed: {describe_user_error(error)}",
        )
    if not isinstance(value, Cluster):
        reject("cluster-not-found", f"{ref} is not a piceli.infra.Cluster")
    return value


@contextmanager
def connect(
    cluster: Cluster, transport: str, allow_exec: bool, exec_sha256: str | None
) -> Iterator[Api]:
    """An API client through the cluster's credential profile, checked against ``api``."""
    from piceli.gitops.install import Api
    from piceli.infra.cluster import ClusterError, api_matches, kubeconfig_server
    from piceli.infra.cluster_init import FIELD_MANAGER
    from piceli.k8s.ops.provider_factory import (
        _read_kubeconfig,
        api_client_from_kubeconfig,
    )
    from piceli.profiles import override, resolve

    policy = exec_policy(allow_exec, exec_sha256)
    with _guard():
        name = override() or cluster.credentials
        found = resolve(name)
        if not found.in_cluster:
            # In a pod the profile is the service account (the in-cluster API
            # address), so only a local profile is compared with ``api``.
            try:
                document = _read_kubeconfig(found.kubeconfig)
            except Exception:
                raise ClusterError(
                    "cluster-api-failed",
                    f"the kubeconfig of profile {name!r} is unreadable",
                ) from None
            server = kubeconfig_server(document, found.context)
            if server is None or not api_matches(cluster.api, server):
                raise ClusterError(
                    "cluster-api-mismatch",
                    f"profile {name!r} does not point at Cluster(api={cluster.api!r})",
                )
        try:
            client = api_client_from_kubeconfig(
                found.kubeconfig.absolute(),
                found.context,
                transport=transport,  # type: ignore[arg-type]
                exec_policy=policy,
            )
        except ValueError as error:
            raise ClusterError(
                "cluster-api-failed", f"kubeconfig refused: {error}"
            ) from None
        api = Api(client, field_manager=FIELD_MANAGER)
        try:
            yield api
        finally:
            api.close()


def _command(head: str, ref: str, transport: str, allow_exec: bool) -> str:
    parts = [head, ref]
    if transport != "https":
        parts += ["--transport", transport]
    if allow_exec:
        parts.append("--allow-exec")
    return " ".join(parts)


def _registry_status(api: Api, cluster: Cluster) -> dict[str, Any] | None:
    """``registry status`` for the cluster's registry (with agent reports)."""
    from piceli.artifacts import cluster_registry as cr
    from piceli.infra.cluster_init import list_items
    from piceli.k8s.cli.registry import read_reports
    from piceli.pipeline.model import ClusterRegistry

    registry = cluster.registry
    if not isinstance(registry, ClusterRegistry):
        return None
    ns = registry.namespace
    pods = [
        pod
        for pod in list_items(api, f"/api/v1/namespaces/{ns}/pods")
        if ((pod.get("metadata") or {}).get("labels") or {}).get(
            "app.kubernetes.io/instance"
        )
        == registry.name
    ]
    deployment = api.call(
        f"/apis/apps/v1/namespaces/{ns}/deployments/{registry.name}", "GET"
    )
    service = api.call(f"/api/v1/namespaces/{ns}/services/{registry.name}", "GET")
    claim = api.call(
        f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{cr.claim_name(registry)}",
        "GET",
    )
    return cr.summarize(
        registry,
        deployment=deployment if isinstance(deployment, dict) else None,
        service=service if isinstance(service, dict) else None,
        claim=claim if isinstance(claim, dict) else None,
        nodes=list_items(api, "/api/v1/nodes"),
        pods=pods,
        reports=read_reports(api, ns, pods),
    )


def _wait_for_mirrors(
    api: Api, cluster: Cluster, seconds: float
) -> list[dict[str, Any]]:
    """Each node's mirror after an install: wait until every agent reported."""
    deadline = time.monotonic() + seconds
    while True:
        status = _registry_status(api, cluster)
        mirrors = list((status or {}).get("mirrors") or ())
        if (
            status is None
            or (mirrors and all("runtime" in m for m in mirrors))
            or time.monotonic() >= deadline
        ):
            return mirrors
        time.sleep(3)


def _say_mirrors(mirrors: list[dict[str, Any]]) -> None:
    from piceli.infra.cluster_init import restart_needed, unmergeable
    from piceli.k8s.cli.registry import mirror_note

    for item in mirrors:
        say(f"  mirror on {item['node']}: {item['mirror']}{mirror_note(item)}")
    restart = restart_needed(mirrors)
    if restart:
        say(
            "k3s loads the mirror on these nodes only when it restarts; Piceli "
            "never restarts k3s. Restart it there (systemctl restart k3s, or "
            "k3s-agent on agents) when convenient: " + ", ".join(restart)
        )
    blocked = unmergeable(mirrors)
    if blocked:
        say(
            "registries.yaml on these nodes is not block-style YAML; the agent "
            "left it unchanged. Add the mirror by hand or rewrite the file in "
            "block style: " + ", ".join(blocked)
        )


@app.command("init")
def init(
    ref: ClusterArgument,
    approve: ApproveOption = None,
    wait: Annotated[
        int,
        typer.Option(
            "--wait",
            help="Seconds to wait for the node mirrors to report after installing",
        ),
    ] = 120,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Set the cluster up: node labels, registry and mirrors, controller, UI (plan first; --approve HASH applies)."""
    from piceli.infra.cluster_init import (
        execute_init,
        plan_init,
        restart_needed,
        ui_renderer,
        unmergeable,
    )

    cluster = load_cluster(ref)
    command = _command("piceli cluster init", ref, transport, allow_exec)
    with connect(cluster, transport, allow_exec, exec_sha256) as api, _guard():
        renderer = ui_renderer()
        plan = plan_init(api, cluster, renderer)
        say(f"cluster {cluster.name} init plan:")
        for change in plan.nodes:
            detail = ", ".join(
                [f"{k}={v}" for k, v in sorted(change.set.items())]
                + [f"-{k}" for k in change.remove]
            )
            say(
                f"  {change.operation:8} Node/{change.node}"
                + (f" {detail}" if detail else "")
            )
        for item in plan.objects.objects:
            meta = item.manifest["metadata"]
            where = f" in {meta['namespace']}" if "namespace" in meta else ""
            say(f"  {item.operation:8} {item.manifest['kind']}/{meta['name']}{where}")
        if plan.ui == "unavailable":
            say("  the UI is declared but this Piceli build cannot install it yet")
        say(f"plan hash: {plan.plan_hash}")
        body = plan.to_dict()
        if not plan.changes:
            say("nothing to change")
            mirrors = _wait_for_mirrors(api, cluster, 0)
            _say_mirrors(mirrors)
            emit_json(
                {
                    **body,
                    "state": "unchanged",
                    "mirrors": mirrors,
                    "restart_needed": restart_needed(mirrors),
                    "unmergeable": unmergeable(mirrors),
                }
            )
            return
        if approve is None:
            say("approve with:")
            say(f"  {command} --approve {plan.plan_hash}")
            emit_json(
                {
                    **body,
                    "state": "approval-required",
                    "approve_command": f"{command} --approve {plan.plan_hash}",
                }
            )
            raise typer.Exit(EXIT_APPROVAL)
        if approve != plan.plan_hash:
            reject(
                "cluster-plan-changed",
                "the plan changed since it was approved (or the hash is wrong); "
                "review the new plan above",
            )
        done = execute_init(api, plan, say)
        mirrors = _wait_for_mirrors(api, cluster, max(0, wait))
        _say_mirrors(mirrors)
        emit_json(
            {
                **body,
                "state": "initialized",
                "done": done,
                "mirrors": mirrors,
                "restart_needed": restart_needed(mirrors),
                "unmergeable": unmergeable(mirrors),
            }
        )


@app.command("status")
def status(
    ref: ClusterArgument,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Nodes, registry and mirrors, controller, UI and Git Secret of a declared cluster (read-only)."""
    from piceli.gitops.install import DEPLOYER, NAME, STATE_CLAIM
    from piceli.infra import cluster_init as ci

    cluster = load_cluster(ref)
    with connect(cluster, transport, allow_exec, exec_sha256) as api, _guard():
        ns = ci.NAMESPACE
        config = api.call(
            f"/api/v1/namespaces/{ns}/configmaps/{ci.CLUSTER_CONFIG}", "GET"
        )
        nodes = ci.list_items(api, "/api/v1/nodes")
        registry = _registry_status(api, cluster)
        rbac = "/apis/rbac.authorization.k8s.io/v1"
        foundation = {
            "service_account": api.call(f"/api/v1/namespaces/{ns}/serviceaccounts/{NAME}", "GET") is not None,
            "cluster_role": api.call(f"{rbac}/clusterroles/{NAME}", "GET") is not None,
            "deployer_role": api.call(f"{rbac}/clusterroles/{DEPLOYER}", "GET") is not None,
            "state_claim": api.call(f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{STATE_CLAIM}", "GET") is not None,
        }  # fmt: skip
        deployment = api.call(
            f"/apis/apps/v1/namespaces/{ns}/deployments/{NAME}", "GET"
        )
        controller = None
        if isinstance(deployment, dict):
            pod = ((deployment.get("spec") or {}).get("template") or {}).get(
                "spec"
            ) or {}
            controller = {
                "installed": True,
                "ready": bool((deployment.get("status") or {}).get("readyReplicas")),
                "node": (pod.get("nodeSelector") or {}).get("kubernetes.io/hostname"),
            }
        controller_health = _controller_health(api, ns, deployment)
        renderer = ci.ui_renderer()
        ui = ci.ui_state(cluster, renderer)
        ui_present = None
        ui_health = None
        if ui == "included" and renderer is not None:
            try:
                expected = renderer(cluster)
            except ValueError:  # not installable as declared: init refuses it
                expected = None
            ui_present = expected is not None and all(
                api.get(item) is not None for item in expected
            )
            ui_health = _ui_health(api, ns)
        secret = api.call(f"/api/v1/namespaces/{ns}/secrets/{ci.GIT_SECRET}", "GET")
        secret_row = {
            "namespace": ns,
            "name": ci.GIT_SECRET,
            "present": isinstance(secret, dict),
            # Key names only; the values are never read into the output.
            "keys": sorted((secret or {}).get("data") or {})
            if isinstance(secret, dict)
            else [],
        }
    body = ci.summarize(
        cluster,
        config=config if isinstance(config, dict) else None,
        nodes=nodes,
        registry=registry,
        foundation=foundation,
        controller=controller,
        ui=ui,
        ui_present=ui_present,
        secret=secret_row,
        controller_health=controller_health,
        ui_health=ui_health,
    )
    say(f"cluster {cluster.name}: {body['state']}")
    for row in body["nodes"]:
        if not row["present"]:
            say(f"  node {row['name']}: missing")
            continue
        labels = (
            "labels ok"
            if row["labels_ok"]
            else "labels missing: " + ", ".join(row["missing_labels"])
        )
        say(
            f"  node {row['name']}: {row['arch']}, {row['runtime'] or 'unknown runtime'}, {labels}"
        )
    if registry is not None:
        say(f"  registry {registry['host']}: {registry['state']}")
        _say_mirrors(list(registry.get("mirrors") or ()))
    if body["controller"] is not None:
        found = all(body["controller"]["foundation"].values())
        deployed = body["controller"]["deployment"]
        say(
            f"  controller on {body['controller']['on']}: foundation "
            + ("installed" if found else "missing")
            + (
                ", running"
                if deployed and deployed["ready"]
                else ", not enabled (piceli gitops enable)"
                if not deployed
                else ", starting"
            )
        )
    say(
        f"  ui: {body['ui']['state']}"
        + (
            ""
            if body["ui"]["installed"] is None
            else (", installed" if body["ui"]["installed"] else ", not installed")
        )
    )
    say(
        f"  git secret {ns}/{ci.GIT_SECRET}: "
        + (
            "present"
            if secret_row["present"]
            else "missing (piceli secrets git --prompt)"
        )
    )
    if as_json:
        emit_json(body)


def _controller_health(api: Api, ns: str, deployment: Any) -> dict[str, Any]:
    """``health`` and ``last_poll`` as ``piceli gitops status`` computes them."""
    import json

    from piceli.gitops.state import STATUS_CONFIGMAP, STATUS_KEY
    from piceli.k8s.cli.gitops import _health

    if not isinstance(deployment, dict):
        return {"health": "not-enabled", "last_poll": None}
    found = api.call(f"/api/v1/namespaces/{ns}/configmaps/{STATUS_CONFIGMAP}", "GET")
    document: dict[str, Any] | None = None
    text = (
        ((found or {}).get("data") or {}).get(STATUS_KEY)
        if isinstance(found, dict)
        else None
    )
    if isinstance(text, str):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        document = value if isinstance(value, dict) else None
    rollout = deployment.get("status") or {}
    ready = bool(rollout.get("availableReplicas") or rollout.get("readyReplicas"))
    controller = (document or {}).get("controller") or {}
    return {
        "health": _health(document, ready, time.time()),
        "last_poll": controller.get("last_poll"),
    }


def _ui_health(api: Api, ns: str) -> str:
    """``healthy``, ``starting`` or ``not-installed`` from the UI's Deployment."""
    from piceli.infra.ui_install import NAME as UI_NAME

    deployment = api.call(f"/apis/apps/v1/namespaces/{ns}/deployments/{UI_NAME}", "GET")
    if not isinstance(deployment, dict):
        return "not-installed"
    rollout = deployment.get("status") or {}
    return "healthy" if rollout.get("readyReplicas") else "starting"


def _read_token() -> str:
    """The token from stdin: typed without echo on a terminal, else one line."""
    import getpass

    if sys.stdin is not None and sys.stdin.isatty():
        try:
            return getpass.getpass("Git token (not echoed): ", stream=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            return ""
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.rstrip("\r\n")


@secrets_app.command(
    "git",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def git(
    context: typer.Context,
    cluster_ref: Annotated[
        str,
        typer.Option(
            "--cluster",
            help="MODULE:ATTR of the piceli.infra.Cluster whose controller and builds use it",
        ),
    ],
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Read the token from stdin (typed without echo, or piped); required",
        ),
    ] = False,
    username: Annotated[
        str,
        typer.Option("--username", help="Git user name stored with the token"),
    ] = "git",
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Store the Git token the controller and cluster builds use (read from stdin, never printed)."""
    import base64

    from piceli.infra import cluster_init as ci

    if context.args:
        # Never echo what was passed: it may be the token itself.
        reject(
            "secrets-token-refused",
            "the token is never an argument; pass --prompt and type it (or pipe it on stdin)",
        )
    if os.environ.get(TOKEN_ENV):
        reject(
            "secrets-token-refused",
            f"the token is never read from the environment; unset {TOKEN_ENV} and use --prompt",
        )
    if not prompt:
        reject(
            "secrets-prompt-required",
            "pass --prompt: the token is read from stdin (no echo on a terminal)",
        )
    if (
        not username
        or len(username) > 256
        or any(c.isspace() or not c.isprintable() for c in username)
    ):
        reject("secrets-invalid", "--username must be printable, without spaces")
    cluster = load_cluster(cluster_ref)
    token = _read_token()
    if not token or any(c in token for c in "\r\n\0"):
        reject("secrets-token-empty", "no token was read from stdin")
    ns = ci.NAMESPACE
    data = {
        "username": base64.b64encode(username.encode()).decode(),
        "password": base64.b64encode(token.encode()).decode(),
    }
    del token
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        "piceli.io/component": "git-credentials",
    }
    with connect(cluster, transport, allow_exec, exec_sha256) as api, _guard():
        initialized = api.call(
            f"/api/v1/namespaces/{ns}/configmaps/{ci.CLUSTER_CONFIG}", "GET"
        )
        if initialized is None:
            reject(
                "cluster-not-initialized",
                f"run piceli cluster init {cluster_ref} first (it creates {ns})",
            )
        path = f"/api/v1/namespaces/{ns}/secrets"
        live = api.call(f"{path}/{ci.GIT_SECRET}", "GET")
        if live is None:
            api.call(
                path,
                "POST",
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": ci.GIT_SECRET,
                        "namespace": ns,
                        "labels": labels,
                    },
                    "type": "kubernetes.io/basic-auth",
                    "data": data,
                },
                missing_ok=False,
            )
            state = "created"
        else:
            api.call(
                f"{path}/{ci.GIT_SECRET}",
                "PATCH",
                {
                    "metadata": {
                        "uid": live["metadata"]["uid"],
                        "resourceVersion": live["metadata"]["resourceVersion"],
                        "labels": labels,
                    },
                    "data": data,
                },
                "application/merge-patch+json",
                missing_ok=False,
            )
            state = "updated"
    say(
        f"{state} Secret {ns}/{ci.GIT_SECRET} (keys: password, username); the token was not printed"
    )
    emit_json(
        {
            "state": state,
            "secret": {
                "namespace": ns,
                "name": ci.GIT_SECRET,
                "keys": ["password", "username"],
            },
            "cluster": cluster.name,
        }
    )


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="cluster")
    root.add_typer(secrets_app, name="secrets")
