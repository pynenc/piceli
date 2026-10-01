"""``piceli registry …``: the in-cluster registry of ``Registry.in_cluster``.

- ``registry install [REF]`` plans the registry's objects (Namespace,
  ConfigMap, retained claim, Service, Deployment, node agent DaemonSet) and
  installs them with ``--approve HASH`` (exit 3 with the plan until then).
- ``registry status [REF]`` shows the registry pod, the mirror on every node
  and the storage use (read-only).
- ``registry uninstall [REF]`` plans the removal (``--approve HASH`` removes);
  the namespace always stays and the data stays without ``--delete-storage``.
- ``registry forward [REF]`` keeps a loopback port-forward to the registry
  Service open for pushes from this machine.

``REF`` is ``MODULE:ATTR`` naming a ``Pipeline`` whose ``deliver=`` is
``Registry.in_cluster(…)`` (its target names the cluster) or the
``Registry.in_cluster(…)`` value itself; without it, the options describe the
registry. Cluster commands take an explicit ``--kubeconfig`` and
``--context`` (or the pipeline's target); never the ambient context. Machine
output is one JSON object on stdout; human text on stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, reject, say
from piceli.k8s.cli.cluster_access import AllowExecOption, ExecSha256Option, exec_policy

app = typer.Typer(
    rich_markup_mode=None,
    help="The in-cluster registry every node pulls from (Registry.in_cluster).",
    no_args_is_help=True,
)

RefArgument = Annotated[
    str | None,
    typer.Argument(
        help="MODULE:ATTR of a Pipeline delivering to Registry.in_cluster(...), "
        "or of the Registry.in_cluster(...) value",
        show_default=False,
    ),
]
KubeconfigOption = Annotated[
    Path | None,
    typer.Option(
        "--kubeconfig",
        help="Explicit kubeconfig file (never ~/.kube/config or KUBECONFIG)",
    ),
]
ContextOption = Annotated[
    str | None,
    typer.Option("--context", help="Kubeconfig context (required with --kubeconfig)"),
]
EnvOption = Annotated[
    str | None,
    typer.Option("--env", help="The pipeline's environment, if it has several"),
]
OnOption = Annotated[
    str | None,
    typer.Option("--on", help="Node (kubernetes.io/hostname) that runs the registry"),
]
StorageOption = Annotated[
    str, typer.Option("--storage", help="Size of the registry's retained claim")
]
PortOption = Annotated[int, typer.Option("--port", help="Registry and Service port")]
NamespaceOption = Annotated[
    str, typer.Option("--namespace", help="Namespace of the registry")
]
NameOption = Annotated[str, typer.Option("--name", help="Base name of its objects")]
NodePortOption = Annotated[
    int | None,
    typer.Option("--node-port", help="Also expose a fixed NodePort (30000-32767)"),
]
StorageClassOption = Annotated[
    str | None, typer.Option("--storage-class", help="StorageClass of the claim")
]
MirrorDirOption = Annotated[
    str,
    typer.Option("--mirror-dir", help="The nodes' containerd certs.d directory"),
]
NodeMirrorOption = Annotated[
    str,
    typer.Option(
        "--node-mirror",
        help="auto (per node), k3s (registries.yaml) or containerd (certs.d)",
    ),
]
ImageOption = Annotated[
    str | None,
    typer.Option("--image", help="Registry image pinned by digest (repo@sha256:…)"),
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

_DEFAULTS: dict[str, Any] = {
    "on": None,
    "storage": "20Gi",
    "port": 5000,
    "namespace": "piceli-system",
    "name": "piceli-registry",
    "node_port": None,
    "storage_class": None,
    "mirror_dir": "/etc/containerd/certs.d",
    "image": None,
    "node_mirror": "auto",
}


@dataclass(frozen=True)
class _Cluster:
    kubeconfig: Path
    context: str
    transport: str
    exec_policy: Any


@contextmanager
def _guard() -> Iterator[None]:
    from piceli.gitops import GitOpsError
    from piceli.pipeline.errors import PipelineError

    codes = {
        "gitops-cluster-failed": "cluster-registry-cluster-failed",
        "gitops-plan-changed": "cluster-registry-plan-changed",
    }
    try:
        yield
    except GitOpsError as error:
        reject(codes.get(error.code, error.code), str(error))
    except PipelineError as error:
        reject(
            "cluster-registry-invalid"
            if error.code == "pipeline-invalid"
            else error.code,
            str(error),
        )


def _resolve(
    ref: str | None,
    env: str | None,
    options: dict[str, Any],
    *,
    need_node: bool,
) -> tuple[Any, Any]:
    """The ``ClusterRegistry`` and, from a pipeline, its target (else ``None``)."""
    from piceli.pipeline.model import ClusterRegistry, Pipeline, Registry

    given = {k: v for k, v in options.items() if v != _DEFAULTS[k]}
    if ref is None:
        if options["on"] is None and need_node:
            reject(
                "cluster-registry-invalid",
                "name the registry node with --on NODE (or pass MODULE:ATTR)",
            )
        with _guard():
            settings = dict(options)
            # status/uninstall find the objects by name; the node is read live.
            settings["on"] = settings["on"] or "unknown"
            return Registry.in_cluster(**settings), None
    if given:
        reject(
            "cluster-registry-invalid",
            "the registry is declared in "
            f"{ref}; drop --{next(iter(given)).replace('_', '-')}",
        )
    from piceli.app.render import RenderError, load_target

    try:
        value = load_target(ref, Path.cwd())
    except RenderError as error:
        reject("pipeline-not-found", str(error))
    except Exception as error:  # the module raised while importing
        from piceli.k8s.cli.deploy_pipeline import describe_user_error

        reject(
            "pipeline-load-failed",
            f"importing {ref} failed: {describe_user_error(error)}",
        )
    target = None
    if isinstance(value, Pipeline):
        if env is not None:
            with _guard():
                value = value.for_environment(env)
        elif value.needs_environment:
            reject(
                "environment-required",
                f"{ref} deploys one target per environment; pass --env NAME",
            )
        target = value.target
        value = value.deliver
    if not isinstance(value, ClusterRegistry):
        reject(
            "cluster-registry-invalid",
            f"{ref} is not Registry.in_cluster(...) nor a Pipeline delivering to it",
        )
    return value, target


def _cluster(
    kubeconfig: Path | None,
    context: str | None,
    transport: str,
    allow_exec: bool,
    exec_sha256: str | None,
    target: Any,
) -> _Cluster:
    if kubeconfig is not None or context is not None or target is None:
        if kubeconfig is None or not context:
            reject(
                "cluster-registry-target-required",
                "name the cluster with --kubeconfig FILE --context NAME "
                "(or pass a pipeline whose target names it)",
            )
        return _Cluster(
            kubeconfig, context, transport, exec_policy(allow_exec, exec_sha256)
        )
    return _Cluster(
        Path(target.kubeconfig),
        target.context,
        target.transport,
        target.exec_policy(),
    )


@contextmanager
def _api(cluster: _Cluster) -> Iterator[Any]:
    from piceli.artifacts.cluster_registry import FIELD_MANAGER
    from piceli.gitops.install import Api
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    try:
        client = api_client_from_kubeconfig(
            cluster.kubeconfig.absolute(),
            cluster.context,
            transport=cluster.transport,  # type: ignore[arg-type]
            exec_policy=cluster.exec_policy,
        )
    except ValueError as error:
        reject("cluster-registry-cluster-failed", f"kubeconfig refused: {error}")
    api = Api(client, field_manager=FIELD_MANAGER)
    try:
        with _guard():
            yield api
    finally:
        api.close()


def _command(
    head: str,
    ref: str | None,
    env: str | None,
    cluster: _Cluster,
    explicit: bool,
    options: dict[str, Any],
    flags: dict[str, bool],
) -> str:
    parts = [head] + ([ref] if ref else [])
    if env:
        parts += ["--env", env]
    if explicit:
        parts += ["--kubeconfig", str(cluster.kubeconfig), "--context", cluster.context]
        if cluster.transport != "https":
            parts += ["--transport", cluster.transport]
    for key, value in options.items():
        if value != _DEFAULTS[key] and value is not None:
            parts += [f"--{key.replace('_', '-')}", str(value)]
    parts += [flag for flag, on in flags.items() if on]
    return " ".join(parts)


def _describe(plan: Any) -> None:
    say(f"registry {plan.action} plan:")
    for item in plan.objects:
        meta = item.manifest["metadata"]
        where = f" in {meta['namespace']}" if "namespace" in meta else ""
        say(f"  {item.operation:8} {item.manifest['kind']}/{meta['name']}{where}")
    say(f"plan hash: {plan.plan_hash}")


def _run_plan(
    api: Any, plan: Any, approve: str | None, command: str, extra: dict[str, Any]
) -> None:
    from piceli.gitops.install import execute

    _describe(plan)
    body = {**plan.to_dict(), **extra}
    if not plan.changes:
        say("nothing to change")
        emit_json({**body, "state": "unchanged"})
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
            "cluster-registry-plan-changed",
            "the plan changed since it was approved (or the hash is wrong); "
            "review the new plan above",
        )
    done = execute(api, plan, say)
    emit_json(
        {
            **body,
            "state": "installed" if plan.action == "enable" else "uninstalled",
            "done": done,
        }
    )


def _options(**values: Any) -> dict[str, Any]:
    return values


@app.command("install")
def install(
    ref: RefArgument = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    env: EnvOption = None,
    on: OnOption = None,
    storage: StorageOption = "20Gi",
    port: PortOption = 5000,
    namespace: NamespaceOption = "piceli-system",
    name: NameOption = "piceli-registry",
    node_port: NodePortOption = None,
    storage_class: StorageClassOption = None,
    mirror_dir: MirrorDirOption = "/etc/containerd/certs.d",
    image: ImageOption = None,
    node_mirror: NodeMirrorOption = "auto",
    approve: ApproveOption = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Install the in-cluster registry and its node mirror (plan first; --approve HASH installs)."""
    from piceli.artifacts.cluster_registry import PLAN_SCHEMA, render
    from piceli.gitops.install import plan_objects

    options = _options(
        on=on, storage=storage, port=port, namespace=namespace, name=name,
        node_port=node_port, storage_class=storage_class, mirror_dir=mirror_dir,
        image=image, node_mirror=node_mirror,
    )  # fmt: skip
    registry, target = _resolve(ref, env, options, need_node=True)
    cluster = _cluster(kubeconfig, context, transport, allow_exec, exec_sha256, target)
    command = _command(
        "piceli registry install", ref, env, cluster, kubeconfig is not None,
        options, {"--allow-exec": allow_exec},
    )  # fmt: skip
    with _api(cluster) as api:
        plan = plan_objects(
            api,
            render(registry),
            action="enable",
            config=registry.describe(),
            schema=PLAN_SCHEMA,
        )
        _run_plan(api, plan, approve, command, {"registry": registry.describe()})


@app.command("uninstall")
def uninstall(
    ref: RefArgument = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    env: EnvOption = None,
    namespace: NamespaceOption = "piceli-system",
    name: NameOption = "piceli-registry",
    delete_storage: Annotated[
        bool,
        typer.Option("--delete-storage", help="Also delete the claim with the images"),
    ] = False,
    approve: ApproveOption = None,
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
) -> None:
    """Remove the registry and its node mirrors (plan first; --approve HASH removes)."""
    from piceli.artifacts.cluster_registry import PLAN_SCHEMA, removable, render
    from piceli.gitops.install import plan_objects

    options = {**_DEFAULTS, "namespace": namespace, "name": name}
    registry, target = _resolve(ref, env, options, need_node=False)
    cluster = _cluster(kubeconfig, context, transport, allow_exec, exec_sha256, target)
    command = _command(
        "piceli registry uninstall", ref, env, cluster, kubeconfig is not None,
        {"namespace": namespace, "name": name} if ref is None else {},
        {"--delete-storage": delete_storage, "--allow-exec": allow_exec},
    )  # fmt: skip
    with _api(cluster) as api:
        plan = plan_objects(
            api,
            removable(render(registry), delete_storage=delete_storage),
            action="disable",
            config={
                "namespace": registry.namespace,
                "name": registry.name,
                "delete_storage": delete_storage,
            },
            schema=PLAN_SCHEMA,
        )
        kept = ["Namespace/" + registry.namespace]
        if not delete_storage:
            kept.append(f"PersistentVolumeClaim/{registry.name}-storage")
        _run_plan(api, plan, approve, command, {"kept": kept})


def _items(api: Any, path: str) -> list[dict[str, Any]]:
    found = api.call(path, "GET")
    items = (found or {}).get("items") if isinstance(found, dict) else None
    return [item for item in items or () if isinstance(item, dict)]


def read_reports(
    api: Any, namespace: str, pods: list[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Each node agent's last report line, by node (from its log)."""
    from piceli.artifacts import cluster_registry as cr

    found: dict[str, dict[str, str]] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        node = (pod.get("spec") or {}).get("nodeName")
        component = (meta.get("labels") or {}).get("app.kubernetes.io/component")
        if not node or component not in cr.MIRROR_COMPONENTS:
            continue
        log = api.text(
            f"/api/v1/namespaces/{namespace}/pods/{meta.get('name')}/log",
            [("container", "mirror"), ("tailLines", "20")],
        )
        report = cr.parse_report(log or "")
        if report is not None:
            found[str(node)] = report
    return found


def mirror_note(item: dict[str, Any]) -> str:
    """`` (k3s, restart k3s to load it)`` and the like, from an agent report."""
    if "runtime" not in item:
        return ""
    if item.get("file") == "unmergeable":
        return f" ({item['runtime']}: registries.yaml is not block-style YAML; left unchanged)"
    if item.get("restart") == "needed":
        return f" ({item['runtime']}: restart k3s on this node to load it)"
    return f" ({item['runtime']})"


@app.command("status")
def status(
    ref: RefArgument = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    env: EnvOption = None,
    namespace: NamespaceOption = "piceli-system",
    name: NameOption = "piceli-registry",
    allow_exec: AllowExecOption = False,
    exec_sha256: ExecSha256Option = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Registry pod, mirror readiness on every node and storage use (read-only)."""
    from piceli.artifacts import cluster_registry as cr

    options = {**_DEFAULTS, "namespace": namespace, "name": name}
    registry, target = _resolve(ref, env, options, need_node=False)
    cluster = _cluster(kubeconfig, context, transport, allow_exec, exec_sha256, target)
    ns = registry.namespace
    with _api(cluster) as api:
        deployment = api.call(
            f"/apis/apps/v1/namespaces/{ns}/deployments/{registry.name}", "GET"
        )
        service = api.call(f"/api/v1/namespaces/{ns}/services/{registry.name}", "GET")
        claim = api.call(
            f"/api/v1/namespaces/{ns}/persistentvolumeclaims/{cr.claim_name(registry)}",
            "GET",
        )
        nodes = _items(api, "/api/v1/nodes")
        pods = [
            pod
            for pod in _items(api, f"/api/v1/namespaces/{ns}/pods")
            if (pod.get("metadata") or {})
            .get("labels", {})
            .get("app.kubernetes.io/instance")
            == registry.name
        ]
        if registry.on == "unknown" and isinstance(deployment, dict):
            import dataclasses

            node = (
                (
                    ((deployment.get("spec") or {}).get("template") or {}).get("spec")
                    or {}
                )
                .get("nodeSelector", {})
                .get("kubernetes.io/hostname")
            )
            if node:
                with _guard():
                    registry = dataclasses.replace(registry, on=node)
        used = None
        running = [
            pod
            for pod in pods
            if (pod.get("metadata") or {})
            .get("labels", {})
            .get("app.kubernetes.io/component")
            == "registry"
            and (pod.get("spec") or {}).get("nodeName")
        ]
        if running and claim is not None:
            # The kubelet's volume stats; needs nodes/proxy, else unknown.
            try:
                summary = api.call(
                    f"/api/v1/nodes/{running[0]['spec']['nodeName']}/proxy/stats/summary",
                    "GET",
                )
                if isinstance(summary, dict):
                    used = cr.used_bytes(summary, ns, cr.claim_name(registry))
            except typer.Exit:
                raise
            except Exception:
                used = None
        reports = read_reports(api, ns, pods)
    body = cr.summarize(
        registry,
        deployment=deployment if isinstance(deployment, dict) else None,
        service=service if isinstance(service, dict) else None,
        claim=claim if isinstance(claim, dict) else None,
        nodes=nodes,
        pods=pods,
        used_bytes=used,
        reports=reports,
    )
    say(f"registry {body['host']}: {body['state']}")
    if body["state"] != "not-installed":
        for pod in body["registry"]["pods"]:
            say(
                f"  pod {pod['name']} on {pod['node']}: {pod['phase']}"
                + (" (ready)" if pod["ready"] else "")
            )
        for item in body["mirrors"]:
            say(f"  mirror on {item['node']}: {item['mirror']}{mirror_note(item)}")
        storage = body["storage"] or {}
        if storage:
            used_text = (
                f"{storage['used_bytes']} bytes used"
                if storage.get("used_bytes") is not None
                else "use unknown"
            )
            say(
                f"  storage {storage['claim']}: {storage.get('phase')}, "
                f"{storage.get('capacity') or storage['requested']}, {used_text}"
            )
    else:
        say("  install it with: piceli registry install")
    if as_json:
        emit_json(body)


@app.command("forward")
def forward(
    ref: RefArgument = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    env: EnvOption = None,
    namespace: NamespaceOption = "piceli-system",
    name: NameOption = "piceli-registry",
    port: PortOption = 5000,
    local_port: Annotated[
        int | None,
        typer.Option("--local-port", help="Loopback port (default: a free one)"),
    ] = None,
) -> None:
    """Keep a loopback port-forward to the registry open for pushes from here."""
    import time

    from piceli.artifacts.delivery_inputs import DeliveryInputError, discover_tool
    from piceli.artifacts.registry_delivery import RegistryForward, supervised_forward
    from piceli.pipeline.backend import free_port

    options = {**_DEFAULTS, "namespace": namespace, "name": name, "port": port}
    registry, target = _resolve(ref, env, options, need_node=False)
    cluster = _cluster(kubeconfig, context, "https", False, None, target)
    chosen = local_port or free_port()
    try:
        tunnel = RegistryForward(
            namespace=registry.namespace,
            target=f"service/{registry.name}",
            remote_port=registry.port,
            kubeconfig=cluster.kubeconfig.absolute(),
            kubectl=discover_tool("kubectl"),
            context=cluster.context,
        )
    except DeliveryInputError as error:
        reject(error.code, str(error))
    prefix = f"/{registry.repository}" if registry.repository else ""
    try:
        with supervised_forward(tunnel, chosen):
            say(
                f"registry {registry.host} forwarded to 127.0.0.1:{chosen}; Ctrl-C stops"
            )
            say(
                f"  push: piceli artifacts deliver … --to oci://127.0.0.1:{chosen}"
                f"{prefix or '/REPO'}/IMAGE --node-registry {registry.host}"
            )
            emit_json(
                {
                    "schema": "piceli.cluster-registry-forward.v1",
                    "state": "forwarding",
                    "host": registry.host,
                    "local": f"127.0.0.1:{chosen}",
                    "push_url": f"oci://127.0.0.1:{chosen}{prefix}",
                }
            )
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        say("forward stopped")
    except Exception as error:
        reason = getattr(error, "reason", None)
        reject(
            reason if isinstance(reason, str) else "forward-unavailable",
            "the port-forward to the registry Service did not start",
        )


def register(root: typer.Typer) -> None:
    root.add_typer(app, name="registry")
