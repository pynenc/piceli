"""Test a new controller version before it replaces the live one (0.28.0).

``piceli gitops enable infra.py --approve HASH --canary`` runs the *new*
controller image once, as a Kubernetes Job in the controller's namespace,
before the live controller is replaced. The Job runs ``piceli gitops
canary``: the new version's own code fetches the sources, imports the
composition at its branch head, resolves every environment's revision,
loads its build specs and components and plans it, and prints one JSON
result. It applies nothing:

- its state directory is a scratch volume (never the live state claim) and
  its config a copy in its own ConfigMap;
- builds, mirrors and registry copies are not run (an image the live
  controller already built for the same source digest is reused, any other
  is counted as a build and given a placeholder reference);
- each environment is planned with ``plan_only`` (no namespace, grant or
  apply); a method the controller would use to change the cluster is not
  there (:class:`DryPorts` lists what it allows).

The command reads the result from the Job's log and removes the Job, its
pod and its ConfigMap when it ends: pass, fail or crash.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

SCHEMA = "piceli.gitops-canary.v1"
#: The canary Job, its pod label and its ConfigMap (the controller's namespace).
CANARY = "piceli-gitops-canary"
#: A placeholder image reference for a component the canary would build.
PLACEHOLDER_HOST = "canary.invalid"
ZERO = "sha256:" + "0" * 64
#: Waiting reasons of a pod that will never start (the canary fails at once).
_STUCK = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerConfigError",
    }
)


# ---------------------------------------------------------------- in the pod


class Planned(Exception):
    """The plan of an environment: raised by :meth:`DryPorts.env_up` to stop the step."""

    def __init__(self, result: Any) -> None:
        super().__init__("planned")
        self.result = result


class DryBuilder:
    """Counts the builds a step would need; builds nothing.

    ``known`` maps ``(image name, source digest)`` to ``(pull_ref,
    manifest_digest)`` of an image the live controller already runs.
    """

    def __init__(self, known: Mapping[tuple[str, str], tuple[str, str]]) -> None:
        self.known = dict(known)
        self.builds: list[str] = []

    def _image(self, name: str, key: str, repository: str, *, count: bool) -> Any:
        from piceli.infra.builders import BuiltImage

        hit = self.known.get((name, key))
        if hit is not None:
            return BuiltImage(hit[0], hit[1])
        if count:
            self.builds.append(name)
        return BuiltImage(f"{PLACEHOLDER_HOST}/{repository}@{ZERO}", ZERO)

    def build(self, items: Any, checkout: Any) -> dict[str, Any]:
        return {
            item.component: self._image(
                item.component, item.digest, item.repository, count=True
            )
            for item in items
        }

    def build_spec(self, request: Any, checkout: Any) -> dict[str, Any]:
        return {
            name: self._image(name, wanted["key"], wanted["repository"], count=True)
            for name, wanted in request.images.items()
        }

    def mirror(self, items: Any) -> dict[str, Any]:
        return {
            item.component: self._image(
                item.component, item.reference, item.repository, count=False
            )
            for item in items
        }


def plan_only(
    pipeline: Any,
    name: str,
    *,
    commit: str | None,
    digests: Mapping[str, Any] | None,
    receipt: Mapping[str, Any] | None,
) -> Any:
    """The environment's plan (``env_up(plan_only=True)``): nothing is created."""
    import piceli.envs as envs  # type: ignore[import-not-found,unused-ignore]

    return envs.env_up(
        pipeline,
        name,
        commit=commit or None,
        digests=dict(digests) if digests else None,
        build_receipt=receipt,
        plan_only=True,
    )


class DryPorts:
    """The controller's ports in the canary: render and plan, never change.

    Wraps the real ports (``inner``) for what only reads or renders
    (pipelines, namespaces' liveness, other clusters' reachability); every
    method that would create, apply, copy or delete is either replaced by
    its dry form or absent (an ``AttributeError`` fails the environment).
    """

    def __init__(
        self,
        inner: Any,
        planner: Callable[..., Any] = plan_only,
        *,
        known: Mapping[tuple[str, str], tuple[str, str]] | None = None,
        builder: DryBuilder | None = None,
        progress: dict[str, str] | None = None,
    ) -> None:
        self.inner = inner
        self.planner = planner
        self.builder = builder or DryBuilder(known or {})
        #: ``{"stage": ...}`` shared with the ports of other clusters.
        self.progress = progress if progress is not None else {"stage": "resolve"}

    def for_cluster(self, name: str, *, home: bool) -> DryPorts:
        return DryPorts(
            self.inner.for_cluster(name, home=home),
            self.planner,
            builder=self.builder,
            progress=self.progress,
        )

    def probe_cluster(self, name: str) -> Any:
        return self.inner.probe_cluster(name)

    def namespace_live(self, namespace: str) -> bool | None:
        live = getattr(self.inner, "namespace_live", None)
        return live(namespace) if callable(live) else None

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.pipeline(*args, **kwargs)

    def pipeline_env(self, *args: Any, **kwargs: Any) -> Any:
        return self.inner.pipeline_env(*args, **kwargs)

    def fill_registry(self, refs: Mapping[str, str], **_kwargs: Any) -> dict[str, str]:
        return dict(refs)  # nothing is copied into another cluster's registry

    def prepare_env(self, pipeline: Any, name: str) -> str | None:
        try:
            import piceli.envs as envs  # type: ignore[import-not-found,unused-ignore]

            return str(envs.namespace_for(pipeline, name))
        except Exception:  # only a label in the canary's result
            return None

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> Any:
        self.progress["stage"] = "plan"
        raise Planned(
            self.planner(
                pipeline,
                name,
                commit=kwargs.get("commit"),
                digests=kwargs.get("digests"),
                receipt=kwargs.get("receipt"),
            )
        )


def known_images(
    status: Mapping[str, Any] | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """``(image, source digest) -> (pull_ref, digest)`` the live controller runs."""
    found: dict[tuple[str, str], tuple[str, str]] = {}
    for record in ((status or {}).get("envs") or {}).values():
        if not isinstance(record, Mapping):
            continue
        for name, entry in (record.get("components") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            key, image, digest = (
                str(entry.get(field) or "")
                for field in ("source_digest", "image", "digest")
            )
            if key and image and digest:
                found[(str(name), key)] = (image, digest)
    return found


def _failure(env: str | None, stage: str, error: BaseException) -> dict[str, Any]:
    from piceli.gitops.controller import error_code

    return {
        "env": env,
        "stage": stage,
        "reason": error_code(error),
        "detail": str(error) or type(error).__name__,
    }


def dry_run(controller: Any, ports: DryPorts) -> dict[str, Any]:
    """One canary pass of a :class:`~piceli.infra.controller.CompositionController`.

    Polls the sources, imports the composition (repository mode: at its
    branch head), wants every environment at the revision it would deploy
    and runs the controller's own deploy step on :class:`DryPorts` until its
    plan. Returns the ``piceli.gitops-canary.v1`` result.
    """
    from piceli import __version__

    controller.ports = ports
    controller._builder = lambda: ports.builder  # never the build hosts
    # A placeholder image is never cached: each environment counts its builds.
    controller._remember = lambda *_args: None
    controller.alarm_sender = None
    now = controller.clock()
    controller.refs = {}
    controller.sources.new_poll()
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "version": __version__,
        "state": "ok",
        "composition": None,
        "envs": {},
        "failure": None,
    }
    failed: list[str] = []
    for key in sorted(controller.sources.remotes):
        failed += controller._poll_source(key, now)
    composition: dict[str, Any] = {"state": "imports", "commit": None}
    if controller.repo is not None:
        failed += controller._load_composition(now)
        entry = controller.state.get("composition") or {}
        composition["commit"] = entry.get("commit") or entry.get("failed")
        if controller._composition is None:
            composition.update(
                state="failed",
                reason=entry.get("error") or "composition-invalid",
                detail=entry.get("error_detail") or "the composition did not load",
            )
    body["composition"] = composition
    if failed:
        detail = (controller.state["sources"].get(failed[0]) or {}).get(
            "last_error_detail"
        )
        body["git"] = {"failed": failed, "detail": detail}
    if composition["state"] == "failed":
        body["state"] = "failed"
        body["failure"] = {
            "env": None,
            "stage": "import",
            "reason": composition["reason"],
            "detail": composition["detail"],
        }
        return body
    controller._desired()
    envs: dict[str, dict[str, Any]] = controller._envs()
    for instance in controller._instances():
        if instance.name in envs:
            continue
        if controller._stop_reason(instance) is not None:
            body["envs"][instance.name] = {"state": "skipped", "why": "stopped"}
            continue
        revision, used, missing = controller._resolve(instance)
        if missing:  # the live controller waits for it too (a tag not pushed yet)
            body["envs"][instance.name] = {
                "state": "skipped",
                "why": f"waiting for a ref of {', '.join(missing)}",
            }
            continue
        controller._want(instance.name, revision, used, "canary")
    for name, record in sorted(envs.items()):
        body["envs"][name] = _check(controller, ports, name, record)
    for name, result in sorted(body["envs"].items()):
        if result["state"] == "failed":
            body["state"] = "failed"
            body["failure"] = {
                "env": name,
                **{k: result[k] for k in ("stage", "reason", "detail")},
            }
            break
    if failed and body["failure"] is None:
        body["state"] = "failed"
        body["failure"] = {
            "env": None,
            "stage": "git",
            "reason": "gitops-git-failed",
            "detail": f"{failed[0]}: {body['git']['detail']}",
        }
    return body


def _check(
    controller: Any, ports: DryPorts, name: str, record: dict[str, Any]
) -> dict[str, Any]:
    from piceli.gitops.ports import compact_plan

    if record.get("reason") == "composition-ref-unresolved":
        missing = ", ".join(record.get("missing_sources") or [])
        return {"state": "skipped", "why": f"waiting for a ref of {missing}"}
    ports.progress["stage"] = "resolve"
    start = len(ports.builder.builds)
    result: dict[str, Any] = {"revision": dict(record.get("revision") or {})}
    try:
        controller._deploy(record)
    except Planned as planned:
        plan = compact_plan(planned.result) or {}
        result.update(
            state="ok",
            changes=int(plan.get("changes_total") or 0),
            create_namespace=bool(plan.get("create_namespace")),
            plan_hash=(planned.result or {}).get("plan_hash")
            if isinstance(planned.result, Mapping)
            else None,
        )
    except Exception as error:
        failure = _failure(name, ports.progress["stage"], error)
        result.update(
            state="failed", **{k: failure[k] for k in ("stage", "reason", "detail")}
        )
    else:
        result.update(
            state="failed",
            stage="plan",
            reason="gitops-canary-failed",
            detail="the deploy step ended without a plan",
        )
    result["builds"] = ports.builder.builds[start:]
    result["digests"] = {
        key: entry.get("source_digest")
        for key, entry in sorted((record.get("components") or {}).items())
    }
    return result


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def failure_text(failure: Mapping[str, Any] | None) -> str:
    """``main: plan failed (code): detail`` of the first failure."""
    if not failure:
        return "unknown failure"
    where = failure.get("env") or "the composition"
    return (
        f"{where}: {failure.get('stage')} failed ({failure.get('reason')}): "
        f"{failure.get('detail')}"
    )


def summary_lines(body: Mapping[str, Any]) -> list[str]:
    """The short human lines of a canary result."""
    lines: list[str] = []
    composition = body.get("composition") or {}
    commit = str(composition.get("commit") or "")[:12]
    if composition.get("state") == "failed":
        lines.append(
            f"composition: does not import at {commit or 'its head'} "
            f"({composition.get('reason')}): {composition.get('detail')}"
        )
    elif composition:
        lines.append(f"composition: imports{' at ' + commit if commit else ''}")
    git = body.get("git")
    if isinstance(git, Mapping):
        lines.append(f"git: {', '.join(git.get('failed') or [])}: {git.get('detail')}")
    for name, env in sorted((body.get("envs") or {}).items()):
        if env.get("state") == "ok":
            changes = int(env.get("changes") or 0)
            planned = (
                "a new environment"
                if env.get("create_namespace") and not changes
                else _plural(changes, "change")
            )
            builds = len(env.get("builds") or ())
            built = _plural(builds, "component") if builds else "nothing"
            lines.append(f"{name}: imports, plans {planned}, builds {built}: ok")
        elif env.get("state") == "skipped":
            lines.append(f"{name}: not checked ({env.get('why')})")
        else:
            lines.append(
                f"{name}: {env.get('stage')} failed ({env.get('reason')}): "
                f"{env.get('detail')}"
            )
    if body.get("state") == "failed" and not body.get("envs") and not composition:
        lines.append(failure_text(body.get("failure")))
    lines.append(f"canary: {body.get('state')}")
    return lines


# ---------------------------------------------------------------- the command side


def canary_objects(
    objects: list[dict[str, Any]], namespace: str, deadline: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The canary's ConfigMap and Job, from the new controller's own objects.

    The Job's pod is the new Deployment's pod (its image, service account,
    mounts and credentials) with: its own label (no Service or selector of
    the controller matches it), ``gitops canary`` instead of ``gitops
    run``, the config from the canary's ConfigMap and a scratch state
    volume; it never restarts and is gone ten minutes after it ends even if
    nothing removes it.
    """
    from piceli.gitops.install import CONFIG_MAP, MANAGED, NAME

    config_map = next(
        o for o in objects
        if o["kind"] == "ConfigMap" and o["metadata"]["name"] == CONFIG_MAP
    )  # fmt: skip
    deployment = next(
        o for o in objects
        if o["kind"] == "Deployment" and o["metadata"]["name"] == NAME
    )  # fmt: skip
    labels = {**MANAGED, "app.kubernetes.io/name": CANARY}
    meta = {"name": CANARY, "namespace": namespace, "labels": labels}
    pod = copy.deepcopy(deployment["spec"]["template"]["spec"])
    pod["restartPolicy"] = "Never"
    volumes = []
    for volume in pod.get("volumes") or []:
        if volume["name"] == "config":
            volume = {"name": "config", "configMap": {"name": CANARY}}
        elif volume["name"] == "state":
            volume = {"name": "state", "emptyDir": {}}
        volumes.append(volume)
    pod["volumes"] = volumes
    container = pod["containers"][0]
    container["name"] = "canary"
    args = list(container["args"])
    container["args"] = ["gitops", "canary", *args[2:]]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": dict(meta),
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": max(int(deadline), 60),
            "ttlSecondsAfterFinished": 600,
            "template": {"metadata": {"labels": dict(labels)}, "spec": pod},
        },
    }
    return (
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": dict(meta),
            "data": dict(config_map["data"]),
        },
        job,
    )


def _failed(detail: str, tail: str | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "state": "failed",
        "composition": None,
        "envs": {},
        "failure": {
            "env": None,
            "stage": "canary",
            "reason": "gitops-canary-failed",
            "detail": detail,
        },
        **({"log_tail": tail} if tail else {}),
    }


def _tail(log: str | None, lines: int = 5) -> str | None:
    if not log:
        return None
    return "\n".join(log.rstrip("\n").splitlines()[-lines:])


def parse_result(log: str | None) -> dict[str, Any] | None:
    """The last JSON object the canary printed (its result or its rejection)."""
    for line in reversed((log or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and (
            value.get("schema") == SCHEMA or value.get("state") == "rejected"
        ):
            return value
    return None


def _pods(api: Any, namespace: str) -> list[dict[str, Any]]:
    found = api.call(
        f"/api/v1/namespaces/{namespace}/pods?labelSelector=job-name%3D{CANARY}", "GET"
    )
    return [p for p in (found or {}).get("items") or [] if isinstance(p, dict)]


def _stuck(pods: list[dict[str, Any]]) -> str | None:
    for pod in pods:
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            waiting = (status.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in _STUCK:
                return f"{waiting['reason']}: {waiting.get('message') or ''}".strip()
    return None


def _remove(api: Any, config_map: dict[str, Any], job: dict[str, Any]) -> None:
    from piceli.gitops.install import object_path

    api.call(object_path(job), "DELETE", {"propagationPolicy": "Background"})
    api.call(object_path(config_map), "DELETE")


def run_canary(
    api: Any,
    objects: list[dict[str, Any]],
    namespace: str,
    *,
    timeout: float,
    interval: float,
    say: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Run the canary Job of the new controller and return its result.

    ``{"state": "skipped"}`` when no controller runs in ``namespace``
    (nothing to replace). The Job, its pod and its ConfigMap are removed
    when this returns or raises.
    """
    from piceli.gitops import GitOpsError
    from piceli.gitops.install import NAME, object_path

    if (
        api.call(f"/apis/apps/v1/namespaces/{namespace}/deployments/{NAME}", "GET")
        is None
    ):
        say(f"canary: no controller to replace in {namespace}; nothing to test")
        return {"schema": SCHEMA, "state": "skipped", "why": "no controller to replace"}
    config_map, job = canary_objects(objects, namespace, int(timeout))
    image = job["spec"]["template"]["spec"]["containers"][0]["image"]
    say(f"canary: running {image} on the composition (nothing is applied)")
    try:
        if api.call(object_path(job), "GET") is not None or (
            api.call(object_path(config_map), "GET") is not None
        ):
            _remove(api, config_map, job)  # a crashed run's leftovers
        api.call(object_path(config_map, collection=True), "POST", config_map,
                 missing_ok=False)  # fmt: skip
        api.call(object_path(job, collection=True), "POST", job, missing_ok=False)
        return _await(api, job, namespace, timeout, interval)
    except GitOpsError as error:
        return _failed(f"the canary could not run: {error} ({error.code})")
    finally:
        try:
            _remove(api, config_map, job)
        except GitOpsError as error:
            say(f"canary: not removed ({error}); delete Job/{CANARY} in {namespace}")


def _await(
    api: Any, job: dict[str, Any], namespace: str, timeout: float, interval: float
) -> dict[str, Any]:
    from piceli.gitops.install import object_path

    deadline = time.monotonic() + timeout
    while True:
        status = ((api.call(object_path(job), "GET") or {}).get("status")) or {}
        if status.get("succeeded") or status.get("failed"):
            break
        stuck = _stuck(_pods(api, namespace))
        if stuck:
            return _failed(f"the canary pod cannot start ({stuck})")
        if time.monotonic() >= deadline:
            return _failed(f"the canary timed out after {int(timeout)}s")
        time.sleep(interval)
    pods = _pods(api, namespace)
    log = None
    if pods:
        name = (pods[0].get("metadata") or {}).get("name")
        log = api.text(
            f"/api/v1/namespaces/{namespace}/pods/{name}/log", [("container", "canary")]
        )
    result = parse_result(log)
    if result is None:
        tail = _tail(log)
        return _failed(
            "the canary printed no result (an image older than 0.28.0 has no "
            f"`gitops canary`){': ' + tail if tail else ''}",
            tail,
        )
    if result.get("state") == "rejected":
        return _failed(f"{result.get('message')} ({result.get('reason')})")
    return result
