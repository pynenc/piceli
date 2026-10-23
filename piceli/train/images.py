"""The train's images handed to the GitOps controller (``Step.images``).

The cluster's push-to-running wait is mostly building: the controller sees
the push, waits for a build host (or starts a build Job) and only then
deploys. A train that just tested and promoted a commit can build those
images itself, before the push the controller reacts to:

1. Read the composition module from the promoted head and, for each of its
   components built from this repository, compute the **change key** the
   controller will compute for that commit: the component's contract
   (``piceli.toml``) and the Git object id of every path its build reads
   (:func:`piceli.infra.contract.contract_digest`, the same function).
2. Build them from the promoted head with the same host-build code as
   ``piceli builder serve`` (a container of the builder image on a docker
   build host; every platform), push them to the cluster's registry and
   attach their provenance as OCI referrers.
3. Offer them to the controller: a ``prebuilt`` request with each image's
   component, key, source, commit, reference and digest, and a receipt
   (host, builder image, times, commits, and the train run and tag).

The controller (``Controller(prebuilt=True)``) keeps the offer and uses it
only when a step's own change key for that component is the offered key and
the digest is in its registry (:mod:`piceli.infra.remote_builds`). So when
it sees the promoted commit, the build it wants exists: it deploys without
waiting for a host or a Job. A component whose key it computes differently
(another contract, another tree) is built as before.

A failure here (the controller does not take prebuilt images, no build
host, a failed build or push) fails the step (``train-images-failed``); the
promotion stands, and the controller builds the images itself.

Importing this module is side-effect free.
"""

from __future__ import annotations

import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.train.model import ImagesStep, TrainError
from piceli.train.runner import now_iso
from piceli.train.steps import StepContext, StepResult, log_path


def _failed(message: str) -> TrainError:
    return TrainError("train-images-failed", message)


def _iso(seconds: float) -> str:
    return now_iso(seconds)[:19] + "Z"


@dataclass
class ImageTools:
    """What an images step builds, pushes and offers with.

    :param build: ``(wanted build, tail) -> {component: {"key", "pull_ref",
        "manifest_digest", "platforms"}}``: build and push (the shape of
        :meth:`piceli.infra.builder_serve.BuilderServe.build`).
    :param offer: ``(request key, request body)``: write to the controller.
    :param attest: ``(wanted, images, provenance) -> {component: referrer}``.
    """

    host: str
    image: str
    build: Callable[[Mapping[str, Any], deque[str]], Mapping[str, Mapping[str, Any]]]
    offer: Callable[[str, Mapping[str, Any]], None]
    attest: Callable[..., Mapping[str, str]] = field(
        default=lambda *_args: {}  # type: ignore[assignment]
    )


#: ``(composition, step, ctx) -> context manager of ImageTools``.
ToolsFactory = Callable[[Any, ImagesStep, StepContext], Any]


def _normalise(url: str) -> str:
    text = url.strip().rstrip("/")
    text = text[:-4] if text.endswith(".git") else text
    for prefix in ("https://", "http://", "ssh://", "git://", "file://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.split("@", 1)[-1] if "@" in text.split("/", 1)[0] else text
    return text.replace(":", "/", 1) if ":" in text.split("/", 1)[0] else text


def _remote_urls(checkout: Path) -> set[str]:
    done = subprocess.run(
        ["git", "remote", "-v"], cwd=checkout, capture_output=True, text=True
    )
    found = set()
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            found.add(_normalise(parts[1]))
    return found


def _object_id(checkout: Path, commit: str, path: str) -> str | None:
    spec = f"{commit}:{path}" if path not in ("", ".") else f"{commit}^{{tree}}"
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", spec],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    found = done.stdout.strip()
    return found if done.returncode == 0 and found else None


def wanted_build(
    composition: Any,
    checkout: Path,
    head: str,
    *,
    repo: Path,
    components: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> dict[str, Any]:
    """The build the controller will want for ``head``: the ``components``
    shape of its wanted builds, each with its change key.

    :param repo: The repository the build host fetches the commit from.
    :raises TrainError: ``train-images-failed``.
    """
    from piceli.infra.contract import CONTRACT_FILE, contract_digest, parse_contracts

    declared = {item.name: item for item in composition.components}
    if components:
        unknown = [name for name in components if name not in declared]
        if unknown:
            raise _failed(f"the composition declares no {', '.join(unknown)}")
        chosen = [declared[name] for name in components]
    else:
        here = _remote_urls(checkout) | {_normalise(str(repo))}
        chosen = [
            item
            for item in composition.components
            if item.source is not None and _normalise(item.source.url) in here
        ]
        if not chosen:
            raise _failed(
                "no component of the composition is built from this repository "
                "(its sources' URLs are not this repository's remotes): name them "
                "with Step.images(components=[...])"
            )
    if any(item.source is None for item in chosen):
        raise _failed("a third-party image component is mirrored, not built")
    contract_file = checkout / CONTRACT_FILE
    if not contract_file.is_file():
        raise _failed(f"the promoted head has no {CONTRACT_FILE}")
    contracts = parse_contracts(contract_file.read_text())
    registry = composition.registry
    prefix = (getattr(registry, "repository", None) or composition.name).strip("/")
    items = []
    for component in chosen:
        contract = contracts.get(component.name)
        if contract is None or contract.build is None:
            raise _failed(f"{CONTRACT_FILE} has no build for {component.name}")
        trees = {
            path: _object_id(checkout, head, path) or "absent"
            for path in contract.build.paths
        }
        items.append(
            {
                "component": component.name,
                "source": component.source.key,
                "commit": head,
                "digest": contract_digest(contract, trees),
                "repository": f"{prefix}/{component.name}",
            }
        )
    if not platforms:
        cluster = composition.cluster
        nodes = getattr(cluster, "nodes", ()) or ()
        platforms = sorted({f"linux/{node.arch}" for node in nodes}) or ["linux/amd64"]
    return {
        "id": "train",
        "kind": "components",
        "components": items,
        "urls": {item["source"]: str(repo) for item in items},
        "platforms": list(platforms),
    }


def run_images_step(
    step: ImagesStep,
    ctx: StepContext,
    *,
    tag: str,
    tools: ToolsFactory | None = None,
) -> StepResult:
    """Build, push and offer the controller's images (see the module doc);
    never raises for a failure (verdict ``failed``)."""
    started, clock = time.time(), time.monotonic()
    log = log_path(ctx, step.name)
    log.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque(maxlen=120)
    detail: dict[str, Any] = {}
    host = "local"

    def say(text: str) -> None:
        tail.append(text)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    try:
        composition = _composition(step, ctx.checkout)
        repo = _repository(ctx.checkout)
        wanted = wanted_build(
            composition, ctx.checkout, ctx.head, repo=repo,
            components=step.components, platforms=step.platforms,
        )  # fmt: skip
        detail["keys"] = {i["component"]: i["digest"] for i in wanted["components"]}
        say(
            "images: "
            + ", ".join(f"{k} {v[:19]}" for k, v in sorted(detail["keys"].items()))
        )
        factory = tools or cluster_tools
        with factory(composition, step, ctx) as found:
            host = found.host
            began = time.time()
            built = dict(found.build(wanted, tail))
            images: dict[str, dict[str, Any]] = {}
            for item in wanted["components"]:
                image = built.get(item["component"])
                if image is None:
                    raise _failed(f"the build made no image {item['component']}")
                if str(image.get("key")) != item["digest"]:
                    raise _failed(f"{item['component']} was built from other inputs")
                images[item["component"]] = {
                    **dict(image),
                    "source": item["source"],
                    "commit": item["commit"],
                }
            from piceli.gitops.state import prebuilt_request
            from piceli.infra import provenance

            receipt: dict[str, Any] = {
                "host": found.host,
                "image": found.image,
                "started_at": _iso(began),
                "finished_at": _iso(time.time()),
                "seconds": round(time.time() - began, 3),
                "platforms": list(wanted["platforms"]),
                "commits": {i["source"]: i["commit"] for i in wanted["components"]},
                "train": {
                    "name": str(ctx.origin.get("train") or ""),
                    "run": str(ctx.origin.get("run") or ""),
                    "tag": tag,
                },
            }
            record = provenance.from_receipt(receipt, found.host)
            referrers = dict(found.attest(wanted, images, record) or {})
            if referrers:
                receipt["referrers"] = referrers
            key, body = prebuilt_request(found.host, images, receipt)
            found.offer(key, body)
            detail["images"] = {
                name: {"pull_ref": item["pull_ref"], "key": item["key"]}
                for name, item in sorted(images.items())
            }
            detail["offered"] = key
            say(f"images: offered {', '.join(sorted(images))} ({key})")
    except Exception as error:  # every failure fails the step, never the run
        code = getattr(error, "code", None) or "train-images-failed"
        say(f"images: {error} ({code})")
        return StepResult(
            step.name, step.kind, "failed", started, time.monotonic() - clock, host,
            str(log), "\n".join(list(tail)[-20:]), {**detail, "reason": code},
        )  # fmt: skip
    return StepResult(
        step.name, step.kind, "passed", started, time.monotonic() - clock, host,
        str(log), "", detail,
    )  # fmt: skip


def _composition(step: ImagesStep, checkout: Path) -> Any:
    from piceli.infra.composition import load_composition

    path = checkout / step.composition
    if not path.is_file():
        raise _failed(f"{step.composition} is not in the promoted head")
    try:
        return load_composition(path)
    except Exception as error:
        raise _failed(f"{step.composition}: {error}") from None


def _repository(checkout: Path) -> Path:
    """The repository a build host fetches the commit from: the checkout's
    common Git directory (the promoted head is there before any push)."""
    done = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise _failed("the checkout is not in a Git repository")
    return Path(done.stdout.strip())


@contextmanager
def cluster_tools(
    composition: Any, step: ImagesStep, ctx: StepContext
) -> Iterator[ImageTools]:
    """The real tools: a docker build host of the composition's cluster
    (``Cluster(dev=DevBuilds(hosts=[...]))``; the step's ``hosts`` pick one),
    its registry through a port-forward and its controller's request
    channel, with the cluster's credential profile (as ``piceli builder
    serve`` uses them)."""
    from piceli.dev.hosts import ContainerCli
    from piceli.gitops.state import ConfigMapChannel
    from piceli.infra.builder_serve import (
        BuilderServe,
        docker_container,
        ensure_image,
        prepare_cache,
    )
    from piceli.k8s.cli.cluster import connect
    from piceli.pipeline.backend import Backend, RegistryRoute
    from piceli.pipeline.model import ClusterRegistry
    from piceli.profiles import override, resolve

    cluster = composition.cluster
    controller = getattr(cluster, "controller", None)
    if cluster is None or controller is None:
        raise _failed("the composition's cluster declares no controller")
    if not controller.prebuilt:
        raise _failed(
            "the controller takes no prebuilt images: declare Controller(prebuilt=True)"
        )
    if controller.delivery == "nodes":
        raise _failed("prebuilt images go through the registry, not into the nodes")
    registry = cluster.registry
    if not isinstance(registry, ClusterRegistry):
        raise _failed("the cluster needs registry=Registry.in_cluster(...)")
    dev = cluster.dev
    hosts = [
        item
        for item in (dev.hosts if dev is not None else ())
        if item.kind == "docker" and item.runs
    ]
    if step.hosts:
        hosts = [item for item in hosts if item.name in step.hosts] or hosts
    if not hosts or dev is None:
        raise _failed("the cluster declares no docker build host to build on")
    chosen = hosts[0]
    image = str(chosen.image or dev.image)
    cli = ContainerCli(chosen)
    lines: list[str] = []
    cli.platform()
    ensure_image(cli, image, lines.append)
    prepare_cache(cli, image)
    profile = resolve(override() or cluster.credentials)
    backend = Backend()
    route = RegistryRoute(
        push=None,
        node_registry=registry.host,
        forward=f"service/{registry.name}",
        namespace=registry.namespace,
        remote_port=registry.port,
        kubeconfig=profile.kubeconfig,
        context=profile.context,
    )

    def push(image_id: str, repository: str, archive: Path) -> str:
        delivered: dict[str, Any] = backend.registry_deliver(
            route, image_id, repository, archive=archive
        )
        if delivered.get("state") != "succeeded" or not delivered.get("pull_ref"):
            raise _failed(f"push of {repository} ended {delivered.get('state')}")
        return str(delivered["pull_ref"])

    def index(repository: str, digests: Any) -> str:
        return str(backend.registry_index(route, repository, digests))

    def attest(repository: str, digest: str, body: bytes) -> Any:
        return backend.registry_attach(route, repository, digest, body)

    from piceli.train.local import cache_root

    with connect(cluster, "https", False, None) as api:
        channel = ConfigMapChannel(api.client, "piceli-system")
        worker = BuilderServe(
            host=chosen.name,
            image=image,
            channel=channel,
            container=docker_container(cli, image),
            push=push,
            index=index,
            root=cache_root(None).parent / "builder",
            attest=attest,
        )
        yield ImageTools(
            host=chosen.name,
            image=image,
            build=worker.build,
            offer=channel.add_request,
            attest=worker.attach,
        )
