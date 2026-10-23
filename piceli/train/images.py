"""The train's images handed to the GitOps controller (``Step.images``).

The cluster's push-to-running wait is mostly building: the controller sees
the push, waits for a build host (or starts a build Job) and only then
deploys. A train that just tested and promoted a commit can build those
images itself, before the push the controller reacts to:

1. Read the composition module from the promoted head and compute the
   **change key** the controller will compute for that commit:

   - for each component built from this repository, its contract
     (``piceli.toml``) and the Git object id of every path its build reads
     (:func:`piceli.infra.contract.contract_digest`, the same function);
   - for each build spec of a pipeline environment (a host build:
     ``[build]`` commands, contexts, images, toolchains), each image's key
     from the spec, its contexts' files and the controller's platforms
     (:func:`piceli.infra.pipelines.image_keys`, the same function, over
     this repository's Git objects with :class:`LocalSources`). Its
     contexts must read only this repository (the composition repository):
     the train cannot know the commit the controller will follow of
     another source.
2. Build them from the promoted head with the same host-build code as
   ``piceli builder serve`` (a container of the builder image on a docker
   build host; every platform, one OCI index over several), push them to
   the cluster's registry and attach their provenance as OCI referrers.
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
    #: The toolchains the last build reported (for the receipt).
    toolchains: Callable[[], Mapping[str, Any]] = field(
        default=lambda: {}  # type: ignore[assignment]
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


def _platforms(composition: Any, platforms: Sequence[str]) -> list[str]:
    """The step's platforms, else the controller's default: ``linux/<arch>``
    of every node of the cluster, sorted (``piceli gitops enable``)."""
    if platforms:
        return list(platforms)
    nodes = getattr(composition.cluster, "nodes", ()) or ()
    found = sorted(
        {
            f"linux/{node.arch}"
            for node in nodes
            if getattr(node, "arch", None) in {"amd64", "arm64"}
        }
    )
    return found or ["linux/amd64"]


def _prefix(composition: Any) -> str:
    registry = composition.registry
    return (getattr(registry, "repository", None) or composition.name).strip("/")


def component_build(
    composition: Any,
    checkout: Path,
    head: str,
    *,
    repo: Path,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The components build the controller will want for ``head`` (its
    ``components`` wanted-build shape, each with its change key), or
    ``None`` when no component is built from this repository.

    :param repo: The repository the build host fetches the commit from.
    :param names: Only these components (default: every one built from this
        repository's remotes).
    :raises TrainError: ``train-images-failed``.
    """
    from piceli.infra.contract import CONTRACT_FILE, contract_digest, parse_contracts

    here = _remote_urls(checkout) | {_normalise(str(repo))}
    chosen = [
        item
        for item in composition.components
        if item.source is not None
        and (item.name in names if names else _normalise(item.source.url) in here)
    ]
    if not chosen:
        return None
    contract_file = checkout / CONTRACT_FILE
    if not contract_file.is_file():
        raise _failed(f"the promoted head has no {CONTRACT_FILE}")
    contracts = parse_contracts(contract_file.read_text())
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
                "repository": f"{_prefix(composition)}/{component.name}",
            }
        )
    return {
        "id": "train",
        "kind": "components",
        "components": items,
        "urls": {item["source"]: str(repo) for item in items},
        "platforms": _platforms(composition, platforms),
    }


class LocalSources:
    """The controller's source reads (:class:`~piceli.infra.sources.SourceSet`:
    ``fetch``, ``tree``, ``read``, ``object_id``) answered from this
    repository, for the one source name ``name``. A build that reads another
    source cannot have its key computed here (``train-images-failed``)."""

    def __init__(self, checkout: Path, name: str) -> None:
        self.checkout = checkout
        self.name = name

    def _check(self, key: str) -> None:
        if key != self.name:
            raise _failed(
                f"the build reads source {key!r}: only builds whose contexts read "
                f"this repository ({self.name}) can be built by the train"
            )

    def _git(self, *args: str) -> bytes:
        done = subprocess.run(
            ["git", *args], cwd=self.checkout, capture_output=True, timeout=600
        )
        if done.returncode != 0:
            raise _failed(f"git {args[0]} failed in the promoted head")
        return done.stdout

    def fetch(self, key: str) -> None:
        self._check(key)

    def object_id(self, key: str, commit: str, path: str) -> str | None:
        self._check(key)
        return _object_id(self.checkout, commit, path)

    def read(self, key: str, commit: str, path: str) -> bytes | None:
        if self.object_id(key, commit, path) is None:
            return None
        return self._git("show", f"{commit}:{path}")

    def tree(self, key: str, commit: str, prefix: str) -> list[tuple[str, str, str]]:
        """As :meth:`piceli.infra.sources.SourceSet.tree`."""
        self._check(key)
        prefix = prefix.strip("/")
        prefix = "" if prefix in ("", ".") else prefix
        args = ["ls-tree", "-r", "-z", "--full-tree", commit]
        if prefix:
            args += ["--", prefix + "/"]
        found: list[tuple[str, str, str]] = []
        for record in self._git(*args).split(b"\0"):
            if not record:
                continue
            meta, _, raw = record.partition(b"\t")
            mode, kind, oid = meta.decode().split(" ")
            if kind != "blob":
                continue
            path = raw.decode("utf-8", "surrogateescape")
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                path = path[len(prefix) + 1 :]
            found.append((path, mode, oid))
        return found


def repo_source(composition: Any, checkout: Path) -> str:
    """The composition repository's source name, as ``piceli gitops enable``
    records it: the composition source with the URL of ``origin``, else the
    name of that URL."""
    from piceli.infra import Source

    done = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=checkout,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        raise _failed(
            "a pipeline environment's builds need the repository's origin remote "
            "(the controller names the composition repository by its URL)"
        )
    try:
        url = Source(done.stdout.strip()).url
    except Exception:
        raise _failed(
            "the origin remote is not a Git URL without credentials"
        ) from None
    same = [source for source in composition.sources if source.url == url]
    return same[0].key if same else Source(url).key


def spec_builds(
    composition: Any,
    checkout: Path,
    head: str,
    *,
    repo: Path,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The spec builds the controller will want for ``head``: one per build
    spec of a pipeline environment, in its ``spec`` wanted-build shape, each
    image with the change key the controller computes
    (:func:`piceli.infra.pipelines.image_keys`, the same function, over this
    repository's Git objects).

    :raises TrainError: ``train-images-failed`` (a context reads another
        source, a spec missing or invalid).
    """
    from piceli.infra import CompositionError
    from piceli.infra.pipelines import image_keys, load_spec, spec_paths

    pipelines = {
        id(env.pipeline): env.pipeline
        for env in composition.environments
        if getattr(env, "pipeline", None) is not None
    }
    if not pipelines:
        return []
    name = repo_source(composition, checkout)
    sources = LocalSources(checkout, name)
    chosen = _platforms(composition, platforms)
    platform = ",".join(chosen)
    found: dict[str, dict[str, Any]] = {}
    for pipeline in pipelines.values():
        try:
            paths = spec_paths(pipeline, checkout)
        except CompositionError as error:
            raise _failed(str(error)) from None
        for path, build in paths:
            if path in found:
                continue
            text = sources.read(name, head, path)
            if text is None:
                raise _failed(f"the build spec {path} is not in the promoted head")
            facts = None if build.node_facts is None else build.node_facts.to_dict()
            try:
                spec = load_spec(text, path, repo=name, facts=facts)
                keys = image_keys(spec, {name: head}, sources, platform=platform)
            except CompositionError as error:
                raise _failed(
                    f"{error}: only builds whose contexts read this repository "
                    "can be built by the train"
                ) from None
            images = {
                image: {
                    "repository": f"{_prefix(composition)}/{image}",
                    "key": key.key,
                }
                for image, key in sorted(keys.items())
                if not names or image in names
            }
            if images:
                found[path] = {
                    "id": "train",
                    "kind": "spec",
                    "spec": path,
                    "repo": name,
                    "commits": {name: head},
                    "urls": {name: str(repo)},
                    "facts": facts,
                    "keys": images,
                    "platforms": chosen,
                }
    return list(found.values())


def wanted_builds(
    composition: Any,
    checkout: Path,
    head: str,
    *,
    repo: Path,
    names: Sequence[str] = (),
    platforms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Every build the controller will want for ``head``: its components
    built from this repository, and the build specs of its pipeline
    environments.

    :raises TrainError: ``train-images-failed`` (nothing to build, an
        unknown name, a key that cannot be computed here).
    """
    known = {item.name for item in composition.components}
    builds = spec_builds(
        composition, checkout, head, repo=repo, names=names, platforms=platforms
    )
    known |= {image for build in builds for image in build["keys"]}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise _failed(f"the composition builds no image {', '.join(unknown)}")
    third = [
        item.name
        for item in composition.components
        if item.source is None and item.name in names
    ]
    if third:
        raise _failed(f"{', '.join(third)}: a third-party image is mirrored, not built")
    components = component_build(
        composition, checkout, head, repo=repo, names=names, platforms=platforms
    )
    found = ([components] if components is not None else []) + builds
    if not found:
        raise _failed(
            "nothing the composition builds comes from this repository (no "
            "component whose source is one of its remotes, no pipeline build "
            "spec): name the images with Step.images(components=[...])"
        )
    return found


def expected(build: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """``{image: {"key", "source", "commit"}}`` a wanted build makes."""
    if build.get("kind") == "spec":
        commit = str(build["commits"][build["repo"]])
        return {
            name: {
                "key": str(item["key"]),
                "source": str(build["repo"]),
                "commit": commit,
                "build": str(build["spec"]),
            }
            for name, item in build["keys"].items()
        }
    return {
        item["component"]: {
            "key": item["digest"],
            "source": item["source"],
            "commit": item["commit"],
        }
        for item in build["components"]
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
        builds = wanted_builds(
            composition, ctx.checkout, ctx.head, repo=repo,
            names=step.components, platforms=step.platforms,
        )  # fmt: skip
        wanted_images = {
            name: item for build in builds for name, item in expected(build).items()
        }
        detail["keys"] = {name: item["key"] for name, item in wanted_images.items()}
        say(
            "images: "
            + ", ".join(f"{k} {v[:19]}" for k, v in sorted(detail["keys"].items()))
        )
        factory = tools or cluster_tools
        with factory(composition, step, ctx) as found:
            host = found.host
            began = time.time()
            images: dict[str, dict[str, Any]] = {}
            toolchains: dict[str, Any] = {}
            for build in builds:
                built = dict(found.build(build, tail))
                toolchains.update(found.toolchains() or {})
                for name, item in expected(build).items():
                    image = built.get(name)
                    if image is None:
                        raise _failed(f"the build made no image {name}")
                    if str(image.get("key")) != item["key"]:
                        raise _failed(f"{name} was built from other inputs")
                    images[name] = {**dict(image), **item}
            from piceli.gitops.state import prebuilt_request
            from piceli.infra import provenance

            platforms = sorted({p for build in builds for p in build["platforms"]})
            receipt: dict[str, Any] = {
                "host": found.host,
                "image": found.image,
                "started_at": _iso(began),
                "finished_at": _iso(time.time()),
                "seconds": round(time.time() - began, 3),
                "platforms": platforms,
                "commits": {i["source"]: i["commit"] for i in wanted_images.values()},
                "train": {
                    "name": str(ctx.origin.get("train") or ""),
                    "run": str(ctx.origin.get("run") or ""),
                    "tag": tag,
                },
            }
            if toolchains:
                receipt["toolchains"] = toolchains
            record = provenance.from_receipt(receipt, found.host)
            referrers: dict[str, str] = {}
            for build in builds:
                mine = {name: images[name] for name in expected(build)}
                referrers.update(found.attest(build, mine, record) or {})
            if referrers:
                receipt["referrers"] = referrers
            key, body = prebuilt_request(found.host, images, receipt)
            found.offer(key, body)
            detail["images"] = {
                name: {
                    "pull_ref": item["pull_ref"],
                    "key": item["key"],
                    "platforms": sorted(item.get("platforms") or ()),
                }
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
            toolchains=lambda: dict(worker.toolchains),
        )
