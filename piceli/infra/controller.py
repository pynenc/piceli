"""The GitOps controller of a composition: many sources, change-aware builds.

``piceli gitops enable infra.py`` installs Piceli's controller with a
composition config (:class:`CompositionConfig`); ``piceli gitops run`` then
runs :class:`CompositionController`. One poll:

1. ``git ls-remote`` every source (one Git Secret for all); a source that
   fails is recorded and keeps its last refs.
2. Handle requests: ``approve`` a pending plan hash, ``promote`` a commit
   to an environment that follows ``Promote()`` for that source, ``sync``
   an environment now (``--component``: rebuild that component).
3. For every environment (and every branch of the branch rule) resolve its
   ``follow`` mapping to one commit per source: the **revision**
   ``{source: sha}``. It is wanted again when a followed branch moved (the
   first sight too), a new matching tag appeared after the first poll (the
   baseline), on a promotion or a sync request. A branch environment whose
   branch is gone from every ``"{branch}"`` source is torn down.
4. Deploy, one environment at a time: read each component's contract at
   its source's commit, refuse unmet needs, compute each component's source
   digest, build only the components whose digest has no image yet (and
   mirror third-party images once), render the environment and ``env_up``
   it: components whose image did not change apply as no-op.

A composition with pipeline environments (``Environment(pipeline=...)``)
is followed in its own repository too (``CompositionConfig(repo=...)``): each
poll resolves the repository's branch, imports the module at that commit when
it moved (:class:`~piceli.infra.pipelines.CompositionRepo`), and every
environment's revision includes that commit, so a change there re-renders
every environment. A pipeline environment builds its pipeline's host specs,
change-aware per output image (:mod:`piceli.infra.pipelines`).

Status (``piceli.gitops-status.v1``, additive): ``sources.<name>`` (url, the
followed refs, last poll), ``envs.<env>.revision`` and
``envs.<env>.components.<name>`` (source, commit, digest, state, health; an
output image of a pipeline environment is a component).

A named environment can be **stopped** (0.14.7): declared with
``Environment(stopped=True)`` or requested with ``piceli env stop ENV``
(``piceli env start ENV`` ends it). While stopped its workloads run no
replicas (claims and objects kept) and it is neither planned nor deployed:
its record is ``state: stopped`` with ``reason`` ``declared`` or
``requested``. **The declaration wins**: a start request of a declared stop
is refused (``gitops-env-stop-declared``); a requested stop lasts until
``env start``, also after a declared stop is removed. Starting scales the
workloads back and deploys the current revision when it moved meanwhile.

**Several clusters** (0.15, :mod:`piceli.infra.multicluster`): an
environment with ``clusters=[Placement(...)]`` has one record per cluster
(``<env>@<cluster>``), deployed through that cluster's ports
(``ports.for_cluster``), its images copied into its own registry
(``ports.fill_registry``). Each poll probes the other clusters
(``ports.probe_cluster``): an unreachable one is retried when it answers,
never failed. A ``Rollout`` holds a later wave until the earlier ones run
the revision healthy (``rollout-waiting``), or for good when one failed it
(``held``, ``rollout-stopped``). The status keeps one entry per environment
with ``clusters.<cluster>``; a placement removed is torn down there.

After every deploy step the record keeps ``checks``: the outcome of the last
checks a run of it executed (passed or failed, each check's name and code,
when, the trigger and the run), also after a rollout.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from piceli.envs.model import (
    BRANCH,
    Branch,
    BranchEnvironments,
    Environment,
    Promote,
    Tag,
)
from piceli.gitops import GitOpsError
from piceli.gitops.config import (
    DEFAULT_NAMESPACE,
    MAX_POLL_SECONDS,
    MIN_POLL_SECONDS,
    backoff,
)
from piceli.gitops.controller import (
    ROLLED_BACK,
    _iso,
    _seconds,
    _version,
    _version_key,
    error_code,
    failed_verification,
    failure_detail,
    owner_approved,
    pruned_fields,
    pruned_line,
    replan_stale,
    step_reason,
    verified,
)
from piceli.gitops.heartbeat import HEARTBEAT_SECONDS
from piceli.gitops.ports import EnvOutcome
from piceli.gitops.repo import RemoteRefs
from piceli.gitops.state import (
    STATUS_SCHEMA,
    Channel,
    load_state,
    read_json,
    remember_rejected,
    remove_env_dir,
    save_state,
    sweep_env_dirs,
    write_json,
)
from piceli.infra import CompositionError, Source
from piceli.infra.builders import BuildItem, BuiltImage, MirrorItem
from piceli.infra.composition import COMPOSITION_SCHEMA, Composition, EnvItem
from piceli.infra.contract import ComponentContract, image_contract
from piceli.infra.sources import SourceSet, describe_refs
from piceli.pipeline.errors import PipelineError

CONFIG_SCHEMA = "piceli.gitops-composition-config.v1"
#: States of a component in the status.
COMPONENT_STATES = ("synced", "building", "rolling", "failed", "unchanged")
#: Errors no retry fixes: the environment waits for a new revision.
FINAL_CODES = frozenset(
    {
        ROLLED_BACK,
        "component-need-unmet",
        "component-contract-invalid",
        "component-contract-missing",
        "component-build-unsupported",
        "composition-invalid",
    }
)
#: How often the controller measures the in-cluster registry's claim (seconds).
REGISTRY_USAGE_SECONDS = 600
#: Why a named environment is stopped (its record's ``reason``).
STOP_REASONS = ("declared", "requested")
#: Failed attempts of one revision a record (and its successful run) keeps.
MAX_FAILED_ATTEMPTS = 3
#: Characters of a failed attempt's log tail kept.
ATTEMPT_TAIL_CHARS = 2000
#: Workload kinds whose change in a plan rolls their pods.
_ROLLING_KINDS = frozenset(
    {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet"}
)
_PINNED = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_QUANTITY = re.compile(r"[0-9]+(?:Ki|Mi|Gi|Ti)")


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class CompositionConfig:
    """What the controller of a composition watches and how it builds.

    :param composition: :meth:`Composition.to_dict` of the composition.
    :param namespace: The controller's namespace.
    :param poll_seconds: Seconds between polls of every source.
    :param builder_image: The build Job's image (pinned by digest); without
        it the controller only mirrors third-party images and deploys
        components whose images are in its cache.
    :param build_git_secret: The one Secret (``username``/``password``) the
        build Job fetches every source with.
    :param platforms: Platforms the Job builds for (the first one deploys).
    :param repo: The composition's own repository (``{"name", "url",
        "branch", "entry"}``, :class:`~piceli.infra.pipelines.RepoSettings`):
        the controller follows it and imports the module at its commit;
        ``composition`` is then the summary recorded at ``gitops enable``.
    :param telemetry: ``Otlp(...).to_dict()`` of ``Controller(telemetry=)``
        (0.16): where the controller sends OpenTelemetry
        (:mod:`piceli.gitops.otel`).
    """

    composition: Mapping[str, Any]
    namespace: str = DEFAULT_NAMESPACE
    poll_seconds: int = 60
    max_attempts: int = 5
    backoff_seconds: int = 30
    platforms: tuple[str, ...] = ("linux/amd64",)
    builder_image: str | None = None
    build_git_secret: str = "piceli-build-git"
    builder_selector: tuple[tuple[str, str], ...] = ()
    build_storage: str = "20Gi"
    repo: Mapping[str, Any] | None = None
    telemetry: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.telemetry is not None:
            from piceli.infra import Otlp

            try:
                Otlp.from_dict(self.telemetry)
            except ValueError as error:
                raise GitOpsError("gitops-config-invalid", str(error)) from None
        if self.repo is None:
            Composition.from_dict(self.composition)  # validates it
        else:
            from piceli.infra.pipelines import RepoSettings

            RepoSettings.from_dict(self.repo)
            if (
                not isinstance(self.composition, Mapping)
                or self.composition.get("schema") != COMPOSITION_SCHEMA
            ):
                raise CompositionError(
                    "composition-invalid", "unknown composition schema"
                )
        if not _LABEL.fullmatch(self.namespace):
            raise GitOpsError("gitops-config-invalid", "invalid controller namespace")
        if not MIN_POLL_SECONDS <= self.poll_seconds <= MAX_POLL_SECONDS:
            raise GitOpsError(
                "gitops-config-invalid",
                f"--poll must be {MIN_POLL_SECONDS}s to {MAX_POLL_SECONDS}s",
            )
        if not 1 <= self.max_attempts <= 100 or not 1 <= self.backoff_seconds <= 3600:
            raise GitOpsError(
                "gitops-config-invalid", "max_attempts 1-100, backoff_seconds 1-3600"
            )
        if self.builder_image is not None and not _PINNED.fullmatch(self.builder_image):
            raise GitOpsError(
                "gitops-image-unpinned",
                "--builder-image must be pinned by digest: repo@sha256:<64 hex>",
            )
        if not _LABEL.fullmatch(self.build_git_secret):
            raise GitOpsError("gitops-config-invalid", "invalid build git secret name")
        if not _QUANTITY.fullmatch(self.build_storage):
            raise GitOpsError(
                "gitops-config-invalid", "--build-storage must be like 20Gi"
            )
        object.__setattr__(self, "platforms", tuple(self.platforms) or ("linux/amd64",))
        for platform in self.platforms:
            if platform not in {"linux/amd64", "linux/arm64"}:
                raise GitOpsError(
                    "gitops-config-invalid", f"invalid platform {platform!r}"
                )

    @property
    def model(self) -> Composition:
        if self.repo is not None:
            raise GitOpsError(
                "gitops-config-invalid",
                "this composition is imported from its repository at the followed "
                "commit (CompositionController loads it)",
            )
        return Composition.from_dict(self.composition)

    @property
    def registry(self) -> Any:
        """The cluster's in-cluster registry (``None``: not declared)."""
        from piceli.infra.composition import _cluster_from

        cluster = _cluster_from(self.composition.get("cluster"))
        return None if cluster is None else cluster.registry

    @property
    def sources(self) -> tuple[Source, ...]:
        """Every source of the config: the composition's and its own repository."""
        found = {
            item["name"]: Source(item["url"], name=item["name"])
            for item in self.composition.get("sources") or ()
        }
        if self.repo is not None:
            found.setdefault(
                str(self.repo["name"]),
                Source(str(self.repo["url"]), name=str(self.repo["name"])),
            )
        return tuple(found[key] for key in sorted(found))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONFIG_SCHEMA,
            "composition": dict(self.composition),
            "namespace": self.namespace,
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "platforms": list(self.platforms),
            "build": {
                "builder_image": self.builder_image,
                "git_secret": self.build_git_secret,
                "selector": dict(self.builder_selector),
                "storage": self.build_storage,
            },
            # Only for a composition followed in its repository: other
            # configs (and their install plan hashes) are unchanged.
            **({"repo": dict(self.repo)} if self.repo is not None else {}),
            # 0.16, only when set: other configs and their hashes are unchanged.
            **(
                {"telemetry": dict(self.telemetry)}
                if self.telemetry is not None
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompositionConfig:
        if value.get("schema") != CONFIG_SCHEMA:
            raise GitOpsError(
                "gitops-config-invalid", "unknown controller config schema"
            )
        build = value.get("build") or {}
        try:
            return cls(
                composition=value["composition"],
                namespace=value.get("namespace", DEFAULT_NAMESPACE),
                poll_seconds=int(value.get("poll_seconds", 60)),
                max_attempts=int(value.get("max_attempts", 5)),
                backoff_seconds=int(value.get("backoff_seconds", 30)),
                platforms=tuple(value.get("platforms") or ("linux/amd64",)),
                builder_image=build.get("builder_image"),
                build_git_secret=build.get("git_secret") or "piceli-build-git",
                builder_selector=tuple(sorted((build.get("selector") or {}).items())),
                build_storage=build.get("storage") or "20Gi",
                repo=value.get("repo"),
                telemetry=value.get("telemetry"),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, GitOpsError):
                raise
            raise GitOpsError(
                "gitops-config-invalid", f"invalid controller config: {error}"
            ) from None
        except CompositionError as error:
            raise GitOpsError("gitops-config-invalid", str(error)) from None


def is_composition_config(document: Any) -> bool:
    return isinstance(document, Mapping) and document.get("schema") == CONFIG_SCHEMA


def load_config(path: Path) -> CompositionConfig:
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        raise GitOpsError(
            "gitops-config-invalid", "cannot read the controller config file"
        ) from None
    return CompositionConfig.from_dict(document)


# ---------------------------------------------------------------- ports


class CompositionPorts(Protocol):
    """What the controller needs besides Git: pipelines, environments, builds."""

    builder: Any

    def pipeline(
        self,
        composition: Composition,
        env: EnvItem,
        contracts: Mapping[str, ComponentContract],
        images: Mapping[str, str],
    ) -> Any: ...

    def pipeline_env(
        self, composition: Composition, env: EnvItem, images: Mapping[str, str]
    ) -> Any: ...

    def prepare_env(self, pipeline: Any, name: str) -> str | None: ...

    def env_up(
        self,
        pipeline: Any,
        name: str,
        *,
        commit: str,
        receipt: Mapping[str, Any] | None,
        digests: Mapping[str, Any] | None,
        approve: str | None,
    ) -> Any: ...

    def env_down(self, pipeline: Any, name: str) -> None: ...

    def env_stop(self, pipeline: Any, name: str) -> None: ...

    # Optional (0.14.7): a named environment the owner stops and starts
    # (``env_stop(pipeline, name, reason="declared"|"requested")``).
    # def env_start(self, pipeline: Any, name: str) -> None: ...


class DefaultCompositionPorts:
    """The real adapters: the 0.14 environment ports plus a builder.

    :param envs: A :class:`piceli.gitops.ports.DefaultPorts` (namespaces,
        ``env_up``/``env_down``/``env_stop`` with the controller's kubeconfig).
    :param builder: A :class:`~piceli.infra.builders.LocalBuilder` or
        :class:`~piceli.infra.builders.JobBuilder`.
    """

    def __init__(
        self,
        envs: Any,
        builder: Any,
        *,
        kubeconfig: Path,
        context: str,
        state_dir: Path,
        transport: str = "https",
        remote: Any = None,
        home: bool = True,
    ) -> None:
        self.envs = envs
        self.builder = builder
        self.kubeconfig = kubeconfig
        self.context = context
        self.state_dir = state_dir
        self.transport = transport
        #: :class:`~piceli.infra.multicluster.RemoteClusters` (several clusters).
        self.remote = remote
        #: ``False`` for the ports of another cluster than the controller's.
        self.home = home
        self.log: Callable[[str], None] = getattr(envs, "log", None) or (lambda _: None)

    # ------------------------------------------------------------ clusters (0.15)
    def for_cluster(self, name: str, *, home: bool) -> DefaultCompositionPorts:
        """The ports deploying to cluster ``name`` (its own state directory).

        The home cluster is reached as the controller is; another cluster
        through the kubeconfig of its credential Secret (read again each
        time, so a rotated Secret is used at once).
        """
        from piceli.gitops.ports import DefaultPorts

        state = self.state_dir / "clusters" / name
        if home:
            kubeconfig, context, envs = self.kubeconfig, self.context, self.envs
        else:
            if self.remote is None:
                raise CompositionError(
                    "cluster-credentials-missing",
                    f"no credentials for cluster {name!r} in this controller",
                )
            kubeconfig, context = self.remote.kubeconfig(name)
            envs = RemoteEnvPorts(
                DefaultPorts(
                    kubeconfig,
                    context,
                    state,
                    namespace=self.envs.namespace,
                    config=self.envs.config,
                    transport=self.transport,
                    log=self.log,
                )
            )
        return DefaultCompositionPorts(
            envs,
            self.builder,
            kubeconfig=kubeconfig,
            context=context,
            state_dir=state,
            transport=self.transport,
            remote=self.remote,
            home=home,
        )

    def probe_cluster(self, name: str) -> dict[str, Any]:
        """``{reachable, reason}`` of another cluster's API (``GET /version``)."""
        if self.remote is None:
            return {"reachable": False, "reason": "cluster-credentials-missing"}
        contact = self.remote.probe(name)
        return {"reachable": contact.reachable, "reason": contact.reason}

    def fill_registry(
        self,
        refs: Mapping[str, str],
        *,
        home: Any,
        registry: Any,
        repositories: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Copy ``refs`` from the home registry into this cluster's, by digest."""
        from piceli.artifacts.registry import (
            RegistryEndpoint,
            StreamedOciRegistryClient,
        )
        from piceli.infra.multicluster import ProxiedRegistryClient, copy_images
        from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

        source = StreamedOciRegistryClient(
            RegistryEndpoint(
                host=f"{home.name}.{home.namespace}.svc",
                port=int(home.port),
                use_tls=False,
            ),
            actions="pull",
        )
        client = api_client_from_kubeconfig(
            self.kubeconfig,
            self.context,
            transport=self.transport,  # type: ignore[arg-type]
        )
        try:
            return copy_images(
                refs,
                home_host=home.host,
                target_host=registry.host,
                source=source,
                target=ProxiedRegistryClient(client, registry),
                repositories=repositories,
            )
        finally:
            client.close()

    def remove_placement(self, *, namespace: str, app: str, env: str) -> dict[str, Any]:
        """Delete what Piceli created for ``env`` in this cluster (kept: claims, Secrets)."""
        from piceli.gitops.install import connect
        from piceli.infra.multicluster import remove_placement

        with connect(self.kubeconfig, self.context, transport=self.transport) as api:
            return remove_placement(api, namespace=namespace, app=app, env=env)

    def pipeline(
        self,
        composition: Composition,
        env: EnvItem,
        contracts: Mapping[str, ComponentContract],
        images: Mapping[str, str],
    ) -> Any:
        from piceli.infra.render import environment_pipeline

        return environment_pipeline(
            composition,
            env,
            contracts,
            images,
            kubeconfig=self.kubeconfig,
            context=self.context,
            state_dir=self.state_dir / "pipelines",
            transport=self.transport,
        )

    def pipeline_env(
        self, composition: Composition, env: EnvItem, images: Mapping[str, str]
    ) -> Any:
        """The declared pipeline of ``env``, deployed by this controller (images via ``env_up``)."""
        from piceli.infra.pipelines import environment_pipeline

        return environment_pipeline(
            composition,
            env,
            kubeconfig=self.kubeconfig,
            context=self.context,
            state_dir=self.state_dir / "pipelines",
            transport=self.transport,
            **({} if self.home else {"home": False}),
        )

    def prepare_env(self, pipeline: Any, name: str) -> str | None:
        return self.envs.prepare_env(pipeline, name)  # type: ignore[no-any-return]

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> Any:
        return self.envs.env_up(pipeline, name, **kwargs)

    def env_down(self, pipeline: Any, name: str) -> None:
        self.envs.env_down(pipeline, name)
        # A failed build Job kept for diagnosis goes with its environment.
        forget = getattr(self.builder, "forget", None)
        if callable(forget):
            try:
                removed = forget(name)
            except Exception as error:  # never fail a teardown on it
                self.log(
                    f"{name}: kept build Jobs not removed ({type(error).__name__})"
                )
            else:
                for job in removed:
                    self.log(f"{name}: removed kept build Job {job}")

    def env_stop(self, pipeline: Any, name: str, reason: str = "idle") -> None:
        if reason == "idle":
            self.envs.env_stop(pipeline, name)
        else:
            self.envs.env_stop(pipeline, name, reason=reason)

    def env_start(self, pipeline: Any, name: str) -> None:
        self.envs.env_start(pipeline, name)

    def namespace_live(self, namespace: str) -> bool | None:
        live = getattr(self.envs, "namespace_live", None)
        return live(namespace) if callable(live) else None

    def registry_usage(self, registry: Any) -> dict[str, Any] | None:
        """The in-cluster registry claim's use: ``du`` of its storage in the
        registry pod (``pods/exec`` in the registry's namespace), or ``None``.

        Published in the status so the in-cluster UI, which is granted
        neither ``pods/exec`` nor ``nodes/proxy``, shows it.
        """
        from piceli.artifacts import cluster_registry as cr
        from piceli.artifacts.registry_storage import read_disk_usage
        from piceli.gitops.install import connect

        namespace = str(getattr(registry, "namespace", "") or "")
        name = str(getattr(registry, "name", "") or "")
        if not namespace or not name:
            return None
        with connect(self.kubeconfig, self.context, transport=self.transport) as api:
            found = api.call(f"/api/v1/namespaces/{namespace}/pods", "GET")
            pods = [
                item
                for item in (found or {}).get("items", [])
                if isinstance(item, dict)
                and ((item.get("metadata") or {}).get("labels") or {}).get(
                    "app.kubernetes.io/instance"
                )
                == name
                and ((item.get("metadata") or {}).get("labels") or {}).get(
                    "app.kubernetes.io/component"
                )
                == "registry"
                and (item.get("status") or {}).get("phase") == "Running"
            ]
            if not pods:
                return None
            pod = str((pods[0].get("metadata") or {}).get("name"))
            used = read_disk_usage(api.client, namespace, pod)
        if used is None:
            return None
        return {
            "used_bytes": used,
            "used_source": "du",
            "claim": cr.claim_name(registry),
        }


class RemoteEnvPorts:
    """The environment ports of another cluster: as the home cluster's, but
    nothing is granted to the controller's service account there (the
    cluster's credentials deploy; the controller has no identity in it)."""

    def __init__(self, envs: Any) -> None:
        self._envs = envs
        self.namespace = envs.namespace
        self.config = envs.config
        self.log = envs.log

    def __getattr__(self, name: str) -> Any:
        return getattr(self._envs, name)

    def prepare_env(self, pipeline: Any, name: str) -> str | None:
        return str(self._envs._envs().namespace_for(pipeline, name))


# ---------------------------------------------------------------- the loop


@dataclass
class _Instance:
    """One deployable environment: a named one, or one branch of the rule.

    With several clusters (0.15) there is one instance per environment and
    cluster: ``name`` is ``<env>@<cluster>`` (its record's key), ``parent``
    the environment (or branch) name, ``env`` the environment that cluster
    runs (:func:`piceli.envs.placement.placed`).
    """

    name: str
    env: EnvItem
    branch: str | None = None
    cluster: str | None = None
    parent: str | None = None

    @property
    def rules(self) -> tuple[tuple[Any, tuple[Any, ...]], ...]:
        return self.env.sources

    @property
    def group(self) -> str:
        """The environment (or branch) name every cluster of it shares."""
        return self.parent or self.name

    @property
    def deploys_as(self) -> str:
        """The name the environment ports know it by (the branch, or the env)."""
        return self.branch if self.branch is not None else self.env.name


def _running_images(record: Mapping[str, Any]) -> dict[str, Any]:
    """The images the environment runs, by component (the release's identity)."""
    return {
        key: entry.get("deployed_image")
        for key, entry in sorted((record.get("components") or {}).items())
        if isinstance(entry, Mapping)
    }


class CompositionController:
    """Poll every source and keep each environment at its revision.

    :param config: :class:`CompositionConfig`.
    :param state_dir: The controller's private state (its volume).
    :param sources: The :class:`~piceli.infra.sources.SourceSet`.
    :param ports: :class:`CompositionPorts`.
    :param channel: Where status is published and requests arrive.
    """

    def __init__(
        self,
        config: CompositionConfig,
        *,
        state_dir: Path,
        sources: SourceSet,
        ports: CompositionPorts,
        channel: Channel,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = lambda _: None,
        telemetry: Any = None,
    ) -> None:
        self.config = config
        self.repo: Any = None
        self._composition: Composition | None = None
        if config.repo is not None:
            from piceli.infra.pipelines import CompositionRepo, RepoSettings

            settings = RepoSettings.from_dict(config.repo)
            sources.add(settings.source)
            self.repo = CompositionRepo(settings, sources, state_dir / "composition")
        else:
            self._composition = config.model
        self.state_dir = state_dir
        self.sources = sources
        self.ports = ports
        self.channel = channel
        self.clock = clock
        self.log = log
        self.state = load_state(state_dir)
        for key in (
            "sources",
            "env_seen",
            "promoted",
            "forced",
            "composition",
            "stops",
            "declared_stops",
            "starts",
        ):
            self.state.setdefault(key, {})
        self.refs: dict[str, RemoteRefs] = {}
        self.busy = False
        self.swept = False  # stale state is swept once, at the first full poll
        # The deploy step being recorded in the history (see _record_step).
        self._built: list[str] = []
        self._last_outcome: EnvOutcome | None = None
        self._step_started: str | None = None
        self._step_env: str | None = None
        self._history_digest: str | None = None
        self._runs: Any = None
        self._step_runs: Any = None  # the current step's run (its own cache)
        #: Each other cluster's contact this poll; whether it answered last time.
        self._polled: dict[str, dict[str, Any]] = {}
        self._reach_memory: dict[str, bool] = {}
        # 0.16.0: the heartbeat (beat) and the step's publish never interleave.
        self.publish_lock = threading.RLock()
        self.heartbeat_at: str | None = None
        self._builds: dict[str, dict[str, Any]] = {}
        self.recovered = False  # interrupted steps and runs marked once, at start
        # 0.16: OpenTelemetry (piceli.gitops.otel); a no-op unless configured.
        from piceli.gitops.otel import ControllerTelemetry

        self.telemetry = telemetry or ControllerTelemetry.disabled(state_dir)
        self.telemetry.attach(self)  # closes runs a crash interrupted

    # ------------------------------------------------------------ helpers
    @property
    def composition(self) -> Composition:
        """The composition being deployed (imported at the followed commit in repo mode)."""
        if self._composition is None:
            raise CompositionError(
                "composition-invalid", "the composition is not loaded yet"
            )
        return self._composition

    def _envs(self) -> dict[str, dict[str, Any]]:
        envs: dict[str, dict[str, Any]] = self.state["envs"]
        return envs

    def _set(self, record: dict[str, Any], **values: Any) -> None:
        record.update(values)
        record["updated_at"] = _iso(self.clock())

    def _want(
        self,
        name: str,
        revision: Mapping[str, str],
        refs: Mapping[str, str],
        trigger: str,
    ) -> None:
        now = self.clock()
        record = self._envs().get(name)
        if (
            record is not None
            and record.get("revision") == dict(revision)
            and record.get("state") != "deleting"
            and not trigger.startswith("sync")
        ):
            return
        primary = next(iter(sorted(revision.values())), None)
        first = sorted(revision)[0] if revision else None
        self._envs()[name] = {
            **(record or {}),
            "branch": name,
            "commit": revision.get(first) if first else primary,
            "revision": dict(sorted(revision.items())),
            "refs": dict(sorted(refs.items())),
            "deployed_commit": (record or {}).get("deployed_commit"),
            "deployed_revision": (record or {}).get("deployed_revision"),
            "namespace": (record or {}).get("namespace"),
            "components": (record or {}).get("components") or {},
            "state": "pending",
            "trigger": trigger,
            "attempts": 0,
            "next_attempt_at": None,
            "plan_hash": None,
            # An approval not applied yet stands while the plan keeps its
            # hash (an unrelated push): env_up refuses a changed plan and the
            # controller asks again (replan_stale).
            "approved_hash": (record or {}).get("approved_hash"),
            "reason": None,
            "failure": None,
            # Failed attempts of the previous revision belong to its runs.
            "failed_attempts": [],
            "pushed_at": _iso(now),
            "updated_at": _iso(now),
        }
        self.log(
            f"{name}: {trigger} -> "
            + ", ".join(f"{k}@{v[:12]}" for k, v in sorted(revision.items()))
        )

    def _fail(
        self,
        record: dict[str, Any],
        reason: str,
        error: BaseException | None = None,
    ) -> None:
        cluster = record.get("cluster")
        if (
            isinstance(cluster, str)
            and cluster != self._home
            and reason not in FINAL_CODES
        ):
            contact = self._contact(cluster, fresh=True)
            if not contact.get("reachable"):
                self._unreachable(record, contact)
                return
        attempts = int(record.get("attempts") or 0) + 1
        deleting = record.get("state") == "deleting"
        record["failure"] = None if error is None else failure_detail(error)
        if not deleting:
            self._remember_attempt(record, reason, attempts)
        # What runs after a failed step is uncertain: the next plan asks.
        record["deployed_plan_hash"] = None
        if not deleting and (
            reason in FINAL_CODES or attempts >= self.config.max_attempts
        ):
            self._set(
                record,
                state="failed",
                attempts=attempts,
                reason=reason,
                next_attempt_at=None,
            )
            self.log(
                f"{record['branch']}: failed ({reason}); waiting for a new revision"
            )
            return
        delay = backoff(self.config, attempts)  # type: ignore[arg-type]
        self._set(
            record,
            state="deleting" if deleting else "retrying",
            attempts=attempts,
            reason=reason,
            next_attempt_at=self.clock() + delay,
        )
        self.log(f"{record['branch']}: {reason}; retry in {delay}s")

    def _remember_attempt(
        self, record: dict[str, Any], reason: str, attempt: int
    ) -> None:
        """Keep a failed attempt of this revision (its code and bounded log tail).

        A retry that succeeds clears ``failure``; its run (history) and the
        record keep the failed attempts before it (:data:`MAX_FAILED_ATTEMPTS`,
        each tail at most :data:`ATTEMPT_TAIL_CHARS`).
        """
        failure = record.get("failure") or {}
        entry: dict[str, Any] = {
            "attempt": attempt,
            "at": _iso(self.clock()),
            "reason": reason,
        }
        tail = failure.get("log_tail") if isinstance(failure, Mapping) else None
        if isinstance(tail, str) and tail:
            entry["log_tail"] = tail[-ATTEMPT_TAIL_CHARS:]
        if isinstance(failure, Mapping) and isinstance(failure.get("kept_job"), str):
            entry["kept_job"] = failure["kept_job"]
        if isinstance(failure, Mapping) and failure.get("writers_started"):
            entry["writers_started"] = list(failure["writers_started"])
        kept = [
            item
            for item in record.get("failed_attempts") or []
            if isinstance(item, Mapping)
        ]
        record["failed_attempts"] = [*kept, entry][-MAX_FAILED_ATTEMPTS:]

    # ------------------------------------------------------------ stops
    def _stop_reason(self, instance: _Instance) -> str | None:
        """``declared``, ``requested`` or ``None``: why a named environment is stopped.

        The declaration (``Environment(stopped=True)``) wins over a request.
        """
        if not isinstance(instance.env, Environment):
            return None
        if instance.env.stopped:
            return "declared"
        if instance.group in self.state["stops"]:
            return "requested"
        return None

    def _stops(self) -> None:
        """Stop the named environments the owner stopped; start the others."""
        for instance in self._instances():
            if not isinstance(instance.env, Environment):
                continue
            why = self._stop_reason(instance)
            if why != "declared":
                self.state["declared_stops"].pop(instance.group, None)
            record = self._envs().get(instance.name)
            stopped = (
                record is not None
                and record.get("state") == "stopped"
                and record.get("reason") in STOP_REASONS
            )
            if why is not None and not stopped:
                if record is None:
                    record = self._envs().setdefault(
                        instance.name,
                        {"branch": instance.name, "state": "pending", "revision": {}},
                    )
                at = record.get("next_attempt_at")
                if record.get("stop_failed") and at is not None and at > self.clock():
                    continue
                reason = why

                def stop(item: dict[str, Any], reason: str = reason) -> None:
                    self._stop_owner(item, reason)

                self._step(record, stop)
            elif why is not None and record is not None and record.get("reason") != why:
                self._set(record, reason=why, stop=self._stop_detail(instance, why))
            elif why is None and stopped and record is not None:
                self._step(record, self._start_owner)
            elif why is None and record is not None and record.pop("stop_failed", None):
                self._set(record, next_attempt_at=None, attempts=0, reason=None)

    def _stop_detail(self, instance: _Instance, why: str) -> dict[str, Any]:
        request = self.state["stops"].get(instance.group) or {}
        declared = self.state["declared_stops"]
        return {
            "by": why,
            "via": request.get("via") if why == "requested" else "declaration",
            # 0.16.0: a declared stop's ``at`` is when the controller first
            # saw the declaration.
            "at": request.get("at")
            if why == "requested"
            else declared.setdefault(instance.group, _iso(self.clock())),
        }

    def _stop_owner(self, record: dict[str, Any], why: str) -> None:
        """Scale a named environment to zero (declared or requested); keep it."""
        name = record["branch"]
        instance = self._instance(name)
        assert instance is not None
        try:
            ports = self._ports_of(instance)
            ports.env_stop(  # type: ignore[call-arg]
                self._down_pipeline(instance.env, ports),
                instance.deploys_as,
                reason=why,
            )
        except Exception as error:
            attempts = int(record.get("attempts") or 0) + 1
            self._set(
                record,
                reason=error_code(error),
                stop_failed=True,
                attempts=attempts,
                next_attempt_at=self.clock() + backoff(self.config, attempts),  # type: ignore[arg-type]
            )
            self.log(f"{name}: stop failed ({record['reason']})")
            return
        record.pop("pending_plan", None)
        record.pop("stop_failed", None)
        self._set(
            record,
            state="stopped",
            reason=why,
            stop=self._stop_detail(instance, why),
            health="stopped",
            plan_hash=None,
            approved_hash=None,
            # Kept: scaling to zero changes no manifest, so the release the
            # owner approved is the one that starts again (not asked twice).
            attempts=0,
            next_attempt_at=None,
            stopped_at=_iso(self.clock()),
        )
        self._lifecycle(record, "stop", "stopped", **record["stop"])
        self.log(f"{name}: stopped ({why}); scaled to zero, claims kept")

    def _start_owner(self, record: dict[str, Any]) -> None:
        """Scale a named environment back; deploy its revision when it moved."""
        name = record["branch"]
        instance = self._instance(name)
        assert instance is not None
        ports = self._ports_of(instance)
        start = getattr(ports, "env_start", None)
        if record.get("namespace") and callable(start):
            start(self._down_pipeline(instance.env, ports), instance.deploys_as)
        stop = record.pop("stop", None) or {}
        record.pop("stopped_at", None)
        request = self.state["starts"].pop(instance.group, None) or {}
        self._lifecycle(
            record,
            "start",
            "started",
            by="requested" if request else f"{stop.get('by') or 'stop'}-removed",
            via=request.get("via") or ("declaration" if not request else None),
            at=request.get("at"),
        )
        revision, used, missing = self._resolve(instance)
        deployed = record.get("deployed_revision")
        if not missing and deployed and dict(deployed) == revision:
            self._set(
                record,
                state="deployed",
                reason=None,
                health="healthy",
                attempts=0,
                next_attempt_at=None,
            )
            self.log(f"{name}: started; scaled back at its deployed revision")
            return
        self._set(record, state="pending", reason=None, health="unknown")
        if missing:
            self.log(f"{name}: started; waiting for a ref of {', '.join(missing)}")
            return
        record["revision"] = None  # wanted again: the revision moved while stopped
        self._want(name, revision, used, "start")

    # ------------------------------------------------------------ instances
    def _instances(self) -> list[_Instance]:
        found: list[_Instance] = []
        for item in self.composition.environments:
            if isinstance(item, Environment):
                found += self._placed(item.name, item, None)
        rule = self.composition.branch_rule
        if rule is not None:
            for branch in self._rule_branches(rule):
                found += self._placed(self._branch_env_name(rule, branch), rule, branch)
        return found

    def _placed(self, name: str, env: EnvItem, branch: str | None) -> list[_Instance]:
        """One instance, or one per cluster of the environment (0.15)."""
        from piceli.envs.placement import placed

        placements = self.composition.placements_of(env)
        if not placements:
            return [_Instance(name, env, branch)]
        return [
            _Instance(
                f"{name}@{item.name}",
                placed(env, item),
                branch,
                cluster=item.name,
                parent=name,
            )
            for item in placements
        ]

    def _group(self, name: str) -> list[_Instance]:
        """The instances of environment (or branch) ``name``: itself or its clusters."""
        return [item for item in self._instances() if item.group == name]

    def _rule_branches(self, rule: BranchEnvironments) -> list[str]:
        names: set[str] = set()
        for source, items in rule.sources:
            if BRANCH not in items:
                continue
            refs = self.refs.get(source.key)
            if refs is None:
                continue
            names |= {
                branch
                for branch in refs.branches
                if rule.branches.matches(branch) and branch != rule.branches.fallback
            }
        fixed = {
            item.name
            for item in self.composition.environments
            if isinstance(item, Environment)
        }
        spaces = {
            item.namespace
            for item in self.composition.environments
            if isinstance(item, Environment)
        }
        config = rule.env_config()
        found = []
        for name in sorted(names - fixed):
            try:
                namespace = config.namespace_for(name)
            except Exception:  # a branch with no letter or digit
                continue
            if namespace not in spaces:  # never a named environment's namespace
                found.append(name)
        return found

    @staticmethod
    def _branch_env_name(rule: BranchEnvironments, branch: str) -> str:
        return branch

    def _instance(self, name: str) -> _Instance | None:
        return next((item for item in self._instances() if item.name == name), None)

    def _resolve(
        self, instance: _Instance
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """``(revision, refs, missing)``: a commit per source, by its rule."""
        revision: dict[str, str] = {}
        used: dict[str, str] = {}
        missing: list[str] = []
        promoted = self.state["promoted"].get(instance.group) or {}
        for source, items in instance.rules:
            refs = self.refs.get(source.key)
            found: tuple[str, str] | None = None
            if source.key in promoted and any(isinstance(i, Promote) for i in items):
                found = (promoted[source.key]["ref"], promoted[source.key]["commit"])
            for item in items:
                if found is not None or refs is None:
                    break
                if item == BRANCH:
                    assert isinstance(instance.env, BranchEnvironments)
                    branch = instance.branch or ""
                    if branch not in refs.branches:
                        branch = instance.env.branches.fallback
                    if branch in refs.branches:
                        found = (f"refs/heads/{branch}", refs.branches[branch])
                elif isinstance(item, Branch) and item.name in refs.branches:
                    found = (f"refs/heads/{item.name}", refs.branches[item.name])
                elif isinstance(item, Tag):
                    tags = sorted(
                        (n for n in refs.tags if fnmatch.fnmatchcase(n, item.pattern)),
                        key=_version_key,
                    )
                    if tags:
                        found = (f"refs/tags/{tags[-1]}", refs.tags[tags[-1]])
            if found is None:
                missing.append(source.key)
            else:
                used[source.key], revision[source.key] = found
        if self.repo is not None and self.repo.commit is not None:
            key = self.repo.settings.name
            if key not in revision and key not in missing:
                revision[key] = self.repo.commit
                used[key] = f"refs/heads/{self.repo.settings.branch}"
        return revision, used, missing

    def _seen(self, instance: _Instance) -> dict[str, Any]:
        """What the triggers compare: followed heads, matching tags."""
        seen: dict[str, Any] = {}
        for source, items in instance.rules:
            refs = self.refs.get(source.key)
            if refs is None:
                continue
            heads: dict[str, str] = {}
            tags: dict[str, str] = {}
            for item in items:
                if item == BRANCH:
                    assert isinstance(instance.env, BranchEnvironments)
                    for branch in (
                        instance.branch or "",
                        instance.env.branches.fallback,
                    ):
                        if branch in refs.branches:
                            heads[branch] = refs.branches[branch]
                            break
                elif isinstance(item, Branch) and item.name in refs.branches:
                    heads[item.name] = refs.branches[item.name]
                elif isinstance(item, Tag):
                    tags.update(
                        {
                            n: sha
                            for n, sha in refs.tags.items()
                            if fnmatch.fnmatchcase(n, item.pattern)
                        }
                    )
            seen[source.key] = {"heads": heads, "tags": dict(sorted(tags.items()))}
        if self.repo is not None and self.repo.commit is not None:
            key = self.repo.settings.name
            if key not in seen:  # the loaded module's commit: a change re-renders
                seen[key] = {
                    "heads": {self.repo.settings.branch: self.repo.commit},
                    "tags": {},
                }
        return seen

    def _desired(self) -> None:
        instances = self._instances()
        known = self.state["env_seen"]
        consumed: set[str] = set()
        try:
            self._desired_of(instances, known, consumed)
        finally:
            for group in consumed:
                self.state["forced"].pop(group, None)
        self._removed_placements(instances)

    def _desired_of(
        self, instances: list[_Instance], known: dict[str, Any], consumed: set[str]
    ) -> None:
        for instance in instances:
            current = self._seen(instance)
            before = known.get(instance.name)
            triggers: list[str] = []
            for key, now in sorted(current.items()):
                old = (before or {}).get(key) or {"heads": {}, "tags": {}}
                for branch, sha in now["heads"].items():
                    if old["heads"].get(branch) != sha:
                        triggers.append(f"push {key}/{branch}")
                if before is not None:  # the first poll is the tags' baseline
                    new = [
                        n for n, sha in now["tags"].items() if old["tags"].get(n) != sha
                    ]
                    if new:
                        latest = sorted(new, key=_version_key)[-1]
                        triggers.append(f"tag {key}/{latest}")
            known[instance.name] = {**(before or {}), **current}
            forced = self.state["forced"].get(instance.group)
            if forced is not None:
                consumed.add(instance.group)
                triggers.append(forced["trigger"])
            if self._stop_reason(instance) is not None:
                # Stopped: never planned or deployed; the start deploys the
                # revision of that moment when it moved.
                if triggers:
                    self.log(f"{instance.name}: stopped; {triggers[0]} not deployed")
                continue
            if not triggers:
                continue
            if instance.branch is not None:
                # A branch environment's run is triggered by its branch's push
                # when it is among them (not by a source it follows on main).
                own = [t for t in triggers if t.endswith(f"/{instance.branch}")]
                triggers = own + [t for t in triggers if t not in own]
            revision, used, missing = self._resolve(instance)
            if missing:
                record = self._envs().setdefault(
                    instance.name, {"branch": instance.name, "state": "pending"}
                )
                unresolved = CompositionError(
                    "composition-ref-unresolved", "a followed ref is missing"
                )
                self._set(record, reason=unresolved.code, missing_sources=missing)
                self._tag(instance)
                self.log(f"{instance.name}: waiting for a ref of {', '.join(missing)}")
                continue
            self._want(instance.name, revision, used, triggers[0])
            self._tag(instance)
        rule = self.composition.branch_rule
        if rule is None:
            return
        alive = {item.name for item in instances}
        fixed = {
            item.name
            for item in self.composition.environments
            if isinstance(item, Environment)
        }
        rule_sources = {s.key for s, items in rule.sources if BRANCH in items}
        complete = rule_sources <= set(self.refs)  # never tear down on a failed poll
        for name, record in self._envs().items():
            if name in fixed or name in alive or record.get("state") == "deleting":
                continue
            if record.get("kind") == "environment":
                continue  # a named environment's cluster: _removed_placements
            if not complete:
                continue
            known.pop(name, None)
            self._set(
                record, state="deleting", attempts=0, next_attempt_at=None, reason=None
            )
            self.log(f"{name}: branch gone; tearing its environment down")

    def _tag(self, instance: _Instance) -> None:
        """A cluster's record names its cluster and environment (0.15)."""
        record = self._envs().get(instance.name)
        if record is None or instance.cluster is None:
            return
        record.update(
            cluster=instance.cluster,
            group=instance.group,
            deploys_as=instance.deploys_as,
            kind="environment" if isinstance(instance.env, Environment) else "branches",
        )

    def _removed_placements(self, instances: list[_Instance]) -> None:
        """Tear down a named environment's cluster that it no longer runs on.

        The record of ``<env>@<cluster>`` is no instance any more (the
        placement, or the cluster, left the composition): it is torn down
        there (:func:`piceli.infra.multicluster.remove_placement`). On the
        home cluster, never while another instance deploys to that namespace.
        """
        alive = {item.name for item in instances}
        home = self.composition.cluster.name if self.composition.cluster else None
        used = {
            (item.cluster or home, item.env.namespace)
            for item in instances
            if isinstance(item.env, Environment)
        }
        for name, record in list(self._envs().items()):
            if (
                name in alive
                or record.get("kind") != "environment"
                or not record.get("cluster")
                or record.get("state") == "deleting"
            ):
                continue
            if (record["cluster"], record.get("namespace")) in used:
                self._forget(name)
                del self._envs()[name]
                self.log(f"{name}: placement removed; its namespace is still deployed")
                continue
            self._set(
                record,
                state="deleting",
                removal=True,
                attempts=0,
                next_attempt_at=None,
                reason=None,
            )
            self.log(f"{name}: no longer placed there; removing what Piceli created")

    # ------------------------------------------------------------ requests
    def _requests(self) -> None:
        try:
            pending = self.channel.requests()
        except GitOpsError as error:
            self.state["last_error"] = error.code
            return
        handled: list[str] = []
        # A stop and a start waiting together: the later one wins.
        ordered = sorted(
            pending.items(), key=lambda item: (str(item[1].get("at") or ""), item[0])
        )
        for key, body in ordered:
            handled.append(key)
            kind = body.get("kind")
            try:
                if kind == "approve":
                    self._approve(body)
                elif kind == "promote":
                    self._promote(body)
                elif kind == "sync":
                    self._sync(body)
                elif kind in {"stop", "start"}:
                    self._stop_request(body, start=kind == "start")
                else:
                    raise GitOpsError("gitops-request-invalid", "unknown request kind")
            except GitOpsError as error:
                remember_rejected(
                    self.state,
                    {
                        "request": key,
                        "kind": str(kind),
                        "reason": error.code,
                        "message": str(error),
                        "at": _iso(self.clock()),
                    },
                )
                self.log(f"request {key}: {error} ({error.code})")
        if handled:
            try:
                self.channel.remove_requests(handled)
            except GitOpsError as error:
                self.state["last_error"] = error.code

    def _approve(self, body: Mapping[str, Any]) -> None:
        name = str(body.get("env"))
        waiting = [
            record
            for key, record in self._envs().items()
            if (key == name or record.get("group") == name)
            and record.get("state") == "approval-required"
            and record.get("plan_hash") == body.get("plan_hash")
        ]
        if not waiting:
            raise GitOpsError(
                "gitops-approval-stale",
                "the environment is not waiting for that plan hash",
            )
        for record in waiting:  # every cluster of it waiting for that plan
            self._approved(record, body)

    def _approved(self, record: dict[str, Any], body: Mapping[str, Any]) -> None:
        self._set(
            record,
            state="pending",
            approved_hash=body["plan_hash"],
            attempts=0,
            next_attempt_at=None,
            reason=None,
            # Recorded as the run's approver in the history (additive).
            approval={
                "via": body.get("via") if body.get("via") in ("cli", "ui") else None,
                "at": _iso(self.clock()),
                # 0.16.0: since when the plan waited (the history's
                # ``approval_wait``).
                "requested_at": record.get("approval_required_since"),
            },
        )
        record.pop("approval_required_since", None)
        self.telemetry.approved(record, body.get("via"))

    def _stop_request(self, body: Mapping[str, Any], *, start: bool) -> None:
        """``piceli env stop|start ENV``: remember (or forget) the owner's stop."""
        name = str(body.get("env") or "")
        group = self._group(name)
        instance = group[0] if group else None
        if instance is None or not isinstance(instance.env, Environment):
            raise GitOpsError(
                "gitops-request-invalid",
                "stop and start name a named environment of the composition",
            )
        if start:
            if instance.env.stopped:
                raise GitOpsError(
                    "gitops-env-stop-declared",
                    f"{name} is declared stopped (Environment(stopped=True)); "
                    "remove the declaration to start it",
                )
            if self.state["stops"].pop(name, None) is not None:
                # Recorded as the start's origin in the history (0.16.0).
                self.state["starts"][name] = {
                    "via": body.get("via")
                    if body.get("via") in ("cli", "ui")
                    else None,
                    "at": str(body.get("at") or _iso(self.clock()))[:32],
                }
            return
        self.state["starts"].pop(name, None)
        via = body.get("via") if body.get("via") in ("cli", "ui") else None
        at = body.get("at") if isinstance(body.get("at"), str) else None
        self.state["stops"][name] = {
            "via": via,
            "at": (at or _iso(self.clock()))[:32],
        }

    def _promote(self, body: Mapping[str, Any]) -> None:
        name = str(body.get("env") or "")
        group = self._group(name)
        instance = group[0] if group else None
        branch, commit = str(body.get("branch")), str(body.get("commit"))
        if instance is None or not isinstance(instance.env, Environment):
            raise GitOpsError(
                "gitops-promote-not-allowed",
                "promote names a named environment of the composition",
            )
        matches: list[tuple[str, str]] = []
        for source, items in instance.rules:
            refs = self.refs.get(source.key)
            if refs is None or not any(isinstance(i, Promote) for i in items):
                continue
            head = refs.branches.get(branch)
            if head is not None and head.startswith(commit):
                matches.append((source.key, head))
        if not any(
            any(isinstance(i, Promote) for i in items) for _, items in instance.rules
        ):
            raise GitOpsError(
                "gitops-promote-not-allowed",
                "the environment follows Promote() for none of its sources",
            )
        if len(matches) != 1:
            raise GitOpsError(
                "gitops-promote-unknown",
                "the commit is not the head of that branch in exactly one source the "
                "environment promotes from",
            )
        key, full = matches[0]
        self.state["promoted"].setdefault(name, {})[key] = {
            "ref": f"refs/heads/{branch}",
            "commit": full,
        }
        self.state["forced"][name] = {"trigger": f"promote {key}/{branch}@{full[:12]}"}

    def _sync(self, body: Mapping[str, Any]) -> None:
        env = body.get("env")
        component = body.get("component")
        instances = self._instances()
        names = list(dict.fromkeys(item.group for item in instances))
        targets = names if not env else [str(env)]
        if env and str(env) not in names:
            raise GitOpsError("gitops-request-invalid", "sync names no environment")
        if component is not None:
            known = {item.name for item in self.composition.components}
            known |= {
                key
                for record in self._envs().values()
                for key in record.get("components") or {}
            }
            if component not in known:
                raise GitOpsError("gitops-request-invalid", "sync names no component")
        for name in targets:
            self.state["forced"][name] = {
                "trigger": "sync" + (f" {component}" if component else "")
            }
            if component is not None:
                rebuild = self.state.setdefault("rebuild", {})
                rebuild.setdefault(name, [])
                if component not in rebuild[name]:
                    rebuild[name].append(component)

    # ------------------------------------------------------------ deploy
    def _repository(self, component: str) -> str:
        registry = self.composition.registry
        prefix = (getattr(registry, "repository", None) or self.composition.name).strip(
            "/"
        )
        return f"{prefix}/{component}"

    def _cache_path(self, component: str, digest: str) -> Path:
        hexa = digest.split(":", 1)[-1]
        return self.state_dir / "components" / component / f"{hexa}.json"

    def _cached(self, component: str, digest: str) -> BuiltImage | None:
        found = read_json(self._cache_path(component, digest))
        if not isinstance(found, Mapping) or not found.get("pull_ref"):
            return None
        return BuiltImage(
            str(found["pull_ref"]), str(found.get("manifest_digest") or "")
        )

    def _remember(self, component: str, digest: str, image: BuiltImage) -> None:
        write_json(self._cache_path(component, digest), image.to_dict())
        self._built.append(component)  # built (or mirrored) in this step
        self.telemetry.built(component, image.manifest_digest)

    def _contracts(
        self, instance: _Instance, revision: Mapping[str, str]
    ) -> dict[str, ComponentContract]:
        contracts: dict[str, ComponentContract] = {}
        for component in self.composition.stack_of(instance.env):
            if component.source is None:
                contracts[component.name] = image_contract(
                    component.name,
                    dict(component.options.get("contract") or {}),  # type: ignore[call-overload]
                )
                continue
            key = component.source.key
            self.sources.fetch(key)
            contracts[component.name] = self.sources.contract(
                key, revision[key], component.name
            )
        return contracts

    def _deploy(self, record: dict[str, Any]) -> None:
        instance = self._instance(record["branch"])
        if instance is None:
            raise CompositionError(
                "composition-invalid", f"no environment {record['branch']!r}"
            )
        name = instance.group
        if hasattr(self.ports.builder, "environment"):
            # Its build Jobs are labelled with it (removed at its teardown).
            self.ports.builder.environment = name
        if instance.env.pipeline is not None:
            self._deploy_pipeline(record, instance)
            return
        ports = self._ports_of(instance)
        revision: dict[str, str] = dict(record["revision"])
        contracts = self._contracts(instance, revision)
        from piceli.infra.render import check_needs

        check_needs(self.composition, instance.env, contracts)
        rebuild = set((self.state.get("rebuild") or {}).get(name) or ())
        previous: dict[str, Any] = dict(record.get("components") or {})
        now = _iso(self.clock())
        components: dict[str, dict[str, Any]] = {}
        images: dict[str, BuiltImage] = {}
        to_build: list[BuildItem] = []
        to_mirror: list[MirrorItem] = []
        for component in self.composition.stack_of(instance.env):
            contract = contracts[component.name]
            if component.source is None:
                digest = str(component.mirrored)
                commit = None
                source = None
            else:
                source = component.source.key
                commit = revision[source]
                digest = self.sources.digest(source, commit, contract)
            cached = (
                None
                if component.name in rebuild
                else self._cached(component.name, digest)
            )
            if cached is not None:
                images[component.name] = cached
            elif component.source is None:
                to_mirror.append(
                    MirrorItem(component.name, digest, self._repository(component.name))
                )
            else:
                assert commit is not None and source is not None
                to_build.append(
                    BuildItem(
                        component.name,
                        source,
                        commit,
                        digest,
                        contract,
                        self._repository(component.name),
                    )
                )
            components[component.name] = {
                "source": source,
                "commit": commit,
                **({"sources": {source: commit}} if source and commit else {}),
                "source_digest": digest,
                "digest": None if cached is None else cached.manifest_digest,
                "image": None if cached is None else cached.pull_ref,
                "state": "building" if cached is None else "pending",
                "health": (previous.get(component.name) or {}).get("health", "unknown"),
                "updated_at": now,
            }
        record["components"] = components
        if to_build or to_mirror:
            self._publish()  # show "building" while it runs
            self.telemetry.build_started(
                [item.component for item in to_mirror]
                + [item.component for item in to_build]
            )
        try:
            if to_mirror:
                mirrored = self._timed(
                    [item.component for item in to_mirror],
                    lambda: self.ports.builder.mirror(to_mirror),
                )
                for key, image in mirrored.items():
                    self._remember(
                        key, str(self.composition.component(key).mirrored), image
                    )
                    images[key] = image
            if to_build:
                built = self._timed(
                    [item.component for item in to_build],
                    lambda: self.ports.builder.build(to_build, self.sources.checkout),
                )
                for item in to_build:
                    image = built[item.component]
                    self._remember(item.component, item.digest, image)
                    images[item.component] = image
        except Exception:
            for entry in components.values():
                if entry["state"] == "building":
                    entry["state"] = "failed"
            raise
        (self.state.get("rebuild") or {}).pop(name, None)
        for key, entry in components.items():
            image = images[key]
            entry["digest"] = image.manifest_digest
            entry["image"] = image.pull_ref
            deployed = (previous.get(key) or {}).get("deployed_image")
            entry["deployed_image"] = deployed
            entry["state"] = "unchanged" if deployed == image.pull_ref else "rolling"
        refs = self._fill(
            instance, ports, {key: image.pull_ref for key, image in images.items()}
        )
        for key, entry in components.items():
            entry["image"] = refs.get(key, entry["image"])
            entry["state"] = (
                "unchanged" if entry["deployed_image"] == entry["image"] else "rolling"
            )
        pipeline = ports.pipeline(self.composition, instance.env, contracts, refs)
        namespace = ports.prepare_env(pipeline, instance.deploys_as)
        if namespace:
            record["namespace"] = namespace
        outcome = EnvOutcome.from_result(
            replan_stale(
                record,
                lambda approve: ports.env_up(
                    pipeline,
                    instance.deploys_as,
                    commit=str(record.get("commit") or ""),
                    receipt=None,
                    digests=None,
                    approve=approve,
                ),
                policy=bool(instance.env.auto_approve),
                log=self.log,
            )
        )
        self._outcome(record, outcome)

    def _deploy_pipeline(self, record: dict[str, Any], instance: _Instance) -> None:
        """Build the changed images of a pipeline environment, mirror, deploy."""
        from piceli.infra.pipelines import (
            SpecBuildRequest,
            image_keys,
            key_of,
            load_spec,
            mirror_list,
            mirror_target,
            spec_paths,
        )

        if self.repo is None or self.repo.checkout is None:
            raise CompositionError(
                "composition-invalid",
                "a pipeline environment needs the composition's repository "
                "(gitops enable records it)",
            )
        name = instance.group
        env = instance.env
        pipeline = env.pipeline
        ports = self._ports_of(instance)
        repo = self.repo.settings.name
        revision: dict[str, str] = dict(record["revision"])
        rebuild = set((self.state.get("rebuild") or {}).get(name) or ())
        previous: dict[str, Any] = dict(record.get("components") or {})
        now = _iso(self.clock())
        platform = self.config.platforms[0]
        components: dict[str, dict[str, Any]] = {}
        images: dict[str, BuiltImage] = {}
        requests: list[SpecBuildRequest] = []
        for path, build in spec_paths(pipeline, self.repo.checkout):
            self.sources.fetch(repo)
            text = self.sources.read(repo, revision[repo], path)
            if text is None:
                raise CompositionError(
                    "composition-invalid",
                    f"the build spec {path} is not in {repo} at {revision[repo][:12]}",
                )
            facts = None if build.node_facts is None else build.node_facts.to_dict()
            spec = load_spec(text, path, repo=repo, facts=facts)
            keys = image_keys(spec, revision, self.sources, platform=platform)
            changed: dict[str, dict[str, str]] = {}
            for image in spec.spec.images:
                if image.name in components:
                    raise CompositionError(
                        "composition-invalid", f"two builds make image {image.name!r}"
                    )
                key = keys[image.name]
                cached = (
                    None if image.name in rebuild else self._cached(image.name, key.key)
                )
                if cached is None:
                    changed[image.name] = {
                        "repository": self._repository(image.name),
                        "key": key.key,
                    }
                else:
                    images[image.name] = cached
                primary, built_at = self._built_from(
                    previous.get(image.name), key, record.get("trigger"), repo, revision
                )
                components[image.name] = {
                    "source": primary,
                    "commit": built_at,
                    "sources": dict(sorted(key.sources.items())),
                    "source_digest": key.key,
                    "digest": None if cached is None else cached.manifest_digest,
                    "image": None if cached is None else cached.pull_ref,
                    "state": "building" if cached is None else "pending",
                    "health": (previous.get(image.name) or {}).get("health", "unknown"),
                    "updated_at": now,
                }
            if changed:
                commits = {
                    source: revision[source] for source, _ in spec.contexts.values()
                }
                commits[repo] = revision[repo]
                urls = {key: self.sources.sources[key].url for key in commits}
                requests.append(
                    SpecBuildRequest(path, repo, commits, changed, facts, urls)
                )
        to_mirror = [
            MirrorItem(ref, ref, mirror_target(self.composition, pipeline, ref))
            for ref in mirror_list(self.composition, pipeline)
            if self._cached("mirror", key_of(ref)) is None
        ]
        record["components"] = components
        if requests or to_mirror:
            self._publish()  # show "building" while it runs
            self.telemetry.build_started(
                [name for request in requests for name in request.images]
            )
        try:
            if to_mirror:
                for ref, image in self.ports.builder.mirror(to_mirror).items():
                    self._remember("mirror", key_of(ref), image)
            for request in requests:
                built = self._timed(
                    sorted(request.images),
                    functools.partial(
                        self.ports.builder.build_spec, request, self.sources.checkout
                    ),
                )
                for image_name, wanted in request.images.items():
                    if image_name not in built:
                        raise CompositionError(
                            "component-build-failed",
                            f"the build returned no image {image_name!r}",
                        )
                    self._remember(image_name, wanted["key"], built[image_name])
                    images[image_name] = built[image_name]
                self._build_times(name, sorted(request.images))
        except Exception:
            for entry in components.values():
                if entry["state"] == "building":
                    entry["state"] = "failed"
            raise
        (self.state.get("rebuild") or {}).pop(name, None)
        for key, entry in components.items():
            image = images[key]
            entry["digest"] = image.manifest_digest
            entry["image"] = image.pull_ref
            deployed = (previous.get(key) or {}).get("deployed_image")
            entry["deployed_image"] = deployed
            entry["state"] = "unchanged" if deployed == image.pull_ref else "rolling"
        refs = self._fill(
            instance,
            ports,
            {key: image.pull_ref for key, image in images.items()},
            mirrors=mirror_list(self.composition, pipeline),
        )
        for key, entry in components.items():
            entry["image"] = refs.get(key, entry["image"])
            entry["state"] = (
                "unchanged" if entry["deployed_image"] == entry["image"] else "rolling"
            )
        target = ports.pipeline_env(self.composition, env, refs)
        namespace = ports.prepare_env(target, instance.deploys_as)
        if namespace:
            record["namespace"] = namespace
        outcome = EnvOutcome.from_result(
            replan_stale(
                record,
                lambda approve: ports.env_up(
                    target,
                    instance.deploys_as,
                    commit=str(record.get("commit") or ""),
                    receipt=None,
                    digests=refs,
                    approve=approve,
                ),
                policy=bool(env.auto_approve),
                log=self.log,
            )
        )
        self._outcome(record, outcome)

    def _timed(self, names: list[str], call: Callable[[], Any]) -> Any:
        """``call()`` (a build or mirror of ``names``), its times in the step's
        ``builds`` (0.16.0; images built by one call share its times)."""
        began = _iso(self.clock())
        state = "failed"
        try:
            result = call()
            state = "done"
            return result
        finally:
            for name in names:
                self._builds[name] = {
                    "started_at": began,
                    "finished_at": _iso(self.clock()),
                    "state": state,
                }

    def _build_times(self, env: str, names: list[str]) -> None:
        """Each image's timings from the builder (0.17.0), in ``builds`` and the log.

        The spec's commands build every image of one Job: ``shared_with``
        names the others, whose ``commands_seconds`` is the same time.
        """
        found = getattr(self.ports.builder, "timings", None)
        if not isinstance(found, Mapping):
            return
        for name in names:
            timings = found.get(name)
            if not isinstance(timings, Mapping) or name not in self._builds:
                continue
            values = {
                key: round(float(timings.get(key) or 0), 3)
                for key in ("commands_seconds", "assemble_seconds", "push_seconds")
            }
            others = [item for item in names if item != name]
            self._builds[name]["timings"] = values
            self._builds[name]["shared_with"] = others
            total = round(sum(values.values()), 3)
            self.log(
                f"{env}: built {name} in {total}s (commands "
                f"{values['commands_seconds']}s"
                + (f", shared with {', '.join(others)}" if others else "")
                + f"; assembly {values['assemble_seconds']}s, push "
                f"{values['push_seconds']}s)"
            )

    @staticmethod
    def _built_from(
        previous: Any,
        key: Any,
        trigger: Any,
        repo: str,
        revision: Mapping[str, str],
    ) -> tuple[str, str | None]:
        """``(source, commit)`` an image was built from: the source whose change
        triggered its build (``sources`` lists every source's commit).

        An image whose key did not change keeps the source and commit it was
        built at. A rebuilt one names the source whose commit changed (the
        run's trigger first when several did); on a first build, the trigger's
        source when the image reads it, else the first source it reads.
        """
        sources = dict(key.sources)
        before = previous if isinstance(previous, Mapping) else {}
        if (
            before.get("source_digest") == key.key
            and before.get("source") in sources
            and isinstance(before.get("commit"), str)
        ):
            return str(before["source"]), str(before["commit"])
        match = re.match(r"(?:push|tag) ([^/ ]+)/", trigger or "")
        triggered = match.group(1) if match else None
        found = before.get("sources")
        old: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
        changed = [
            name
            for name in sorted(sources)
            if old.get(name) not in (None, sources[name])
        ]
        if triggered in changed or (not changed and triggered in sources):
            chosen = str(triggered)
        elif changed:
            chosen = changed[0]
        else:
            chosen = next(iter(sorted(sources)), repo)
        return chosen, sources.get(chosen) or revision.get(chosen)

    def _down_pipeline(self, env: EnvItem, ports: Any = None) -> Any:
        """A pipeline that only knows the environment (for env_down, env_stop)."""
        ports = ports or self.ports
        if env.pipeline is not None:
            return ports.pipeline_env(self.composition, env, {})
        return ports.pipeline(self.composition, env, {}, {})

    # ------------------------------------------------------------ clusters
    @property
    def _home(self) -> str | None:
        cluster = self.composition.cluster
        return None if cluster is None else cluster.name

    def _ports_for(self, cluster: str | None) -> Any:
        """The ports deploying to ``cluster`` (``None``: the 0.14 single cluster)."""
        if cluster is None:
            return self.ports
        get = getattr(self.ports, "for_cluster", None)
        if not callable(get):
            raise CompositionError(
                "composition-invalid",
                "this controller deploys to its own cluster only (several clusters "
                "need piceli gitops run with credentials of each)",
            )
        return get(cluster, home=cluster == self._home)

    def _ports_of(self, instance: _Instance) -> Any:
        return self._ports_for(instance.cluster)

    def _fill(
        self,
        instance: _Instance,
        ports: Any,
        refs: Mapping[str, str],
        mirrors: tuple[str, ...] = (),
    ) -> dict[str, str]:
        """The image references ``instance``'s cluster pulls (0.15).

        Another cluster than the home one pulls from its own registry: the
        images are copied there by digest (``ports.fill_registry``) and its
        references name that registry. ``mirrors`` are the environment's
        third-party images (their home copies are copied too).
        """
        if instance.cluster is None or instance.cluster == self._home:
            return dict(refs)
        cluster = self.composition.cluster_named(instance.cluster)
        registry = getattr(cluster, "registry", None)
        home = self.composition.registry
        if registry is None or home is None:
            raise CompositionError(
                "composition-invalid",
                f"cluster {instance.cluster!r} needs registry=Registry.in_cluster(...)",
            )
        wanted = dict(refs)
        repositories: dict[str, str] = {}
        if mirrors and instance.env.pipeline is not None:
            from piceli.infra.pipelines import mirror_target

            for ref in mirrors:
                digest = ref.rpartition("@")[2]
                source = mirror_target(self.composition, instance.env.pipeline, ref)
                key = f"mirror:{ref}"
                wanted[key] = f"{home.host}/{source}@{digest}"
                repositories[key] = mirror_target(
                    self.composition, instance.env.pipeline, ref, registry=registry
                )
        copied = dict(
            ports.fill_registry(
                wanted, home=home, registry=registry, repositories=repositories
            )
        )
        return {key: copied[key] for key in refs}

    def _outcome(self, record: dict[str, Any], outcome: EnvOutcome) -> None:
        self._last_outcome = outcome
        if outcome.namespace:
            record["namespace"] = outcome.namespace
        now = _iso(self.clock())
        components = record.get("components") or {}
        if outcome.state == "deployed":
            action = outcome.action or "deployed"
            rolled = self._rolled(components, action, self._step_run(outcome))
            for key, entry in components.items():
                if entry["state"] in {"rolling", "synced"}:
                    entry["state"] = "synced" if key in rolled else "unchanged"
                entry["deployed_image"] = entry["image"]
                entry["health"] = "healthy"
                entry["updated_at"] = now
            pruned = pruned_fields(outcome)
            self._set(
                record,
                **pruned,
                state="deployed",
                last_sync=now,
                deployed_commit=record.get("commit"),
                deployed_revision=dict(record.get("revision") or {}),
                attempts=0,
                next_attempt_at=None,
                plan_hash=None,
                approved_hash=None,
                deployed_plan_hash=owner_approved(record),
                reason=None,
                failure=None,
                health="healthy",
                last_action=action,
                verification=(
                    verified(outcome, now)
                    if action != "unchanged"
                    else record.get("verification")
                ),
            )
            if action == "unchanged":
                self._settle_checks(record)
            changed = sorted(rolled)
            if action == "verified":
                self.log(
                    f"{record['branch']}: verified (checks changed); rolled "
                    f"{', '.join(changed) or 'nothing'}"
                )
            else:
                self.log(
                    f"{record['branch']}: deployed; rolled "
                    f"{', '.join(changed) or 'nothing'}"
                )
            line = pruned_line(record["branch"], pruned)
            if line:
                self.log(line)
        elif outcome.state == "approval-required":
            record.setdefault("approval_required_since", now)
            self._set(
                record,
                state="approval-required",
                plan_hash=outcome.plan_hash,
                approved_hash=None,
                next_attempt_at=None,
                reason=outcome.reason,
                failure=None,
            )
            self.log(
                f"{record['branch']}: approval required; piceli gitops approve "
                f"{record['branch']} {outcome.plan_hash}"
            )
        else:
            for entry in components.values():
                if entry["state"] == "rolling":
                    entry["state"] = "failed"
            self._fail(record, outcome.reason or "gitops-step-failed")

    @staticmethod
    def _rolled(
        components: Mapping[str, Any], action: str, run: Mapping[str, Any] | None
    ) -> set[str]:
        """The components a deploy actually rolled (new digest or new template).

        Nothing when it applied nothing (``verified``, ``unchanged``). A
        component rolled when its image digest changed (not its pull
        reference: the same digest under another registry name rolls
        nothing) or, with the run's plan, when its workload changed; a plan
        that changed no workload rolled nothing.
        """
        if action != "deployed":
            return set()

        def digest(reference: Any) -> str | None:
            return reference.rpartition("@")[2] if isinstance(reference, str) else None

        changed = {
            key
            for key, entry in components.items()
            if entry.get("deployed_image") is None
            or digest(entry.get("deployed_image")) != digest(entry.get("image"))
        }
        plan = (run or {}).get("plan")
        if not isinstance(plan, Mapping):
            return changed
        changes = [
            item for item in plan.get("changes") or [] if isinstance(item, Mapping)
        ]
        workloads = {
            str(item.get("name"))
            for item in changes
            if item.get("kind") in _ROLLING_KINDS
            and item.get("operation") not in {"delete", "no-op"}
        }
        complete = len(changes) >= int(plan.get("changes_total") or 0)
        if complete and not workloads:
            return set()
        return changed | (workloads & set(components))

    def _step_run(self, outcome: EnvOutcome | None) -> dict[str, Any] | None:
        """The run journal entry of the current deploy step (``None``: none).

        Its id when the deploy result named it; else the run its environment's
        journal created during the step (one environment deploys at a time).
        """
        from piceli.gitops import history

        record = self._envs().get(self._step_env or "")
        if record is None or self._step_started is None:
            return None
        try:
            if self._step_runs is None:
                self._step_runs = history.RunReader()
            runs = self._step_runs.runs(self._run_dirs(record))
        except (OSError, ValueError):
            return None
        run_id = getattr(outcome, "run_id", None)
        if run_id:
            return next((run for run in runs if run.get("run_id") == run_id), None)
        start = _seconds(self._step_started)
        for run in runs:  # newest first
            began = history._when(run.get("started_at")).timestamp()
            if start is not None and began >= start - 1:
                return run
        return None

    def _run_dirs(self, record: Mapping[str, Any]) -> list[Path]:
        """The run journal directories of one record (its cluster's state)."""
        from piceli.gitops import history

        cluster = record.get("cluster")
        namespace = record.get("namespace")
        return history.run_dirs(
            self._state_of(cluster if isinstance(cluster, str) else None),
            str(record.get("deploys_as") or record["branch"]),
            namespace if isinstance(namespace, str) else None,
        )

    def _checks(
        self, record: dict[str, Any], run: Mapping[str, Any] | None, trigger: Any
    ) -> None:
        """Keep the last checks a run of this environment executed (status ``checks``)."""
        outcome = self._last_outcome
        if (
            outcome is not None
            and getattr(outcome, "action", None) == "unchanged"
            and not getattr(outcome, "run_id", None)
        ):
            return  # a no-op runs no checks: never another run's (0.15.0)
        checks = (run or {}).get("checks")
        if not isinstance(checks, Mapping):
            return
        results = [
            item for item in checks.get("results") or [] if isinstance(item, Mapping)
        ]
        if not results:
            return  # skipped (no checks, release already verified): the last stand
        passed = sum(1 for item in results if item.get("passed"))
        record["checks"] = {
            "state": "passed"
            if checks.get("passed") and passed == len(results)
            else "failed",
            "passed": passed,
            "total": len(results),
            "results": [
                {
                    "name": str(item.get("name") or "check")[:128],
                    "passed": bool(item.get("passed")),
                    **({"code": str(item["code"])[:64]} if item.get("code") else {}),
                }
                for item in results
            ],
            "failed": [
                str(item.get("name") or "check")[:128]
                for item in results
                if not item.get("passed")
            ],
            "at": (run or {}).get("finished_at") or _iso(self.clock()),
            "trigger": trigger if isinstance(trigger, str) else None,
            "run_id": (run or {}).get("run_id"),
            "action": record.get("last_action")
            if record.get("state") == "deployed"
            else None,
            **(
                {"rollback": dict(checks["rollback"])}
                if isinstance(checks.get("rollback"), Mapping)
                else {}
            ),
        }
        if record["checks"]["state"] == "passed" and record.get("state") == "deployed":
            record["release_checks"] = {
                "images": _running_images(record),
                "checks": record["checks"],
            }

    @staticmethod
    def _settle_checks(record: dict[str, Any]) -> None:
        """A no-op deploy settles on the running release: show its checks.

        After a failed release was rolled back, a revert deploys nothing; the
        checks of the failed run are not the running release's (0.15.0). The
        last passing checks of the same images come back, else none.
        """
        checks = record.get("checks")
        if not isinstance(checks, Mapping) or checks.get("state") == "passed":
            return
        kept = record.get("release_checks")
        if isinstance(kept, Mapping) and kept.get("images") == _running_images(record):
            record["checks"] = dict(kept["checks"])
        else:
            record["checks"] = None

    def _degraded(self, record: dict[str, Any], verification: dict[str, Any]) -> None:
        """Failing checks on an unchanged release: keep it running, mark it degraded.

        Nothing was applied (the components' images did not change), so
        nothing is rolled back and no retry runs: the environment waits for
        a new revision or ``piceli gitops sync``, whose passing verification
        clears it.
        """
        now = _iso(self.clock())
        for entry in (record.get("components") or {}).values():
            if entry.get("state") == "rolling":
                entry["state"] = "synced"
            entry["deployed_image"] = entry.get("image")
            entry["updated_at"] = now
        self._set(
            record,
            state="deployed",
            last_sync=now,
            deployed_commit=record.get("commit"),
            deployed_revision=dict(record.get("revision") or {}),
            attempts=0,
            next_attempt_at=None,
            plan_hash=None,
            approved_hash=None,
            deployed_plan_hash=None,
            reason="pipeline-checks-failed",
            failure=None,
            health="degraded",
            last_action="verified",
            verification=verification,
        )
        names = ", ".join(item["check"] for item in verification["failed"]) or "checks"
        self.log(
            f"{record['branch']}: verification failed ({names}); rolled nothing, "
            "the release keeps running"
        )

    def _teardown(self, record: dict[str, Any]) -> None:
        name = record["branch"]
        if record.get("removal"):
            self._remove_placement(record)
            return
        rule = self.composition.branch_rule
        if rule is None:
            self._forget(name)
            del self._envs()[name]
            return
        cluster = record.get("cluster")
        ports = self._ports_for(cluster)
        env: EnvItem = rule
        if cluster is not None:
            from piceli.envs.placement import placed

            found = next(
                (p for p in self.composition.placements_of(rule) if p.name == cluster),
                None,
            )
            env = rule if found is None else placed(rule, found)
        # No contracts: a pipeline that only knows the environment's settings.
        ports.env_down(
            self._down_pipeline(env, ports), record.get("deploys_as") or name
        )
        remove_env_dir(self._branch_dirs(rule, cluster), record.get("namespace"))
        self._lifecycle(record, "teardown", "removed", by="removed", via="controller")
        self._forget(name)
        del self._envs()[name]
        self.log(f"{name}: environment removed")

    def _branch_dirs(
        self, rule: BranchEnvironments, cluster: str | None = None
    ) -> Path:
        """Where the rule's branch environments keep their pipeline state."""
        return self._state_of(cluster) / "pipelines" / rule.name / "branches"

    def _state_of(self, cluster: str | None) -> Path:
        """The state directory of one cluster's pipelines (0.15: ``clusters/<name>``)."""
        return (
            self.state_dir if cluster is None else self.state_dir / "clusters" / cluster
        )

    def _remove_placement(self, record: dict[str, Any]) -> None:
        """Remove what Piceli created for an environment in a cluster it left."""
        name = record["branch"]
        cluster = str(record.get("cluster"))
        namespace = record.get("namespace")
        removed: dict[str, Any] = {"deleted": [], "kept": [], "namespace": "absent"}
        if isinstance(namespace, str) and namespace:
            ports = self._ports_for(cluster)
            removed = dict(
                ports.remove_placement(
                    namespace=namespace,
                    app=self.composition.name,
                    env=str(record.get("deploys_as") or record.get("group") or name),
                )
            )
        removals = self.state.setdefault("removals", [])
        removals.append(
            {
                "env": record.get("group") or name,
                "cluster": cluster,
                "namespace": namespace,
                "at": _iso(self.clock()),
                "deleted": len(removed.get("deleted") or ()),
                "kept": list(removed.get("kept") or ())[:50],
                "namespace_state": removed.get("namespace"),
            }
        )
        del removals[:-10]
        self._forget(name)
        del self._envs()[name]
        self.log(
            f"{name}: removed from cluster {cluster} ({len(removed.get('deleted') or ())} "
            f"object(s) deleted, {len(removed.get('kept') or ())} kept)"
        )

    #: Per-environment memory besides its record (keyed by environment name).
    _MEMORY = (
        "env_seen",
        "promoted",
        "forced",
        "rebuild",
        "stops",
        "declared_stops",
        "starts",
    )

    def _forget(self, name: str) -> None:
        """Drop what the controller remembers of a removed environment."""
        for key in self._MEMORY:
            memory = self.state.get(key)
            if isinstance(memory, dict):
                memory.pop(name, None)

    def _sweep(self) -> None:
        """Once per start: drop the state of environments that no longer exist.

        The memory (triggers, promotions, pending syncs) of environments the
        composition no longer has and no record names is dropped; a record
        of an environment that is no longer declared, with no branch rule
        left to tear it down, is dropped when its namespace is gone; the
        pipeline state of a branch namespace no record names is removed when
        the namespace is gone (``namespace_live`` answers ``False``). A live
        environment's state is never deleted.
        """
        live = getattr(self.ports, "namespace_live", None)
        alive = {item.name for item in self._instances()}
        records = self._envs()
        rule = self.composition.branch_rule
        dropped: list[str] = []
        if rule is None and live is not None:
            for name, record in list(records.items()):
                if name in alive or record.get("state") == "deleting":
                    continue
                namespace = record.get("namespace")
                if isinstance(namespace, str) and live(namespace) is False:
                    del records[name]
                    dropped.append(name)
        # Memory of an environment on several clusters is keyed by its group
        # (``edge``), its records by instance (``edge@<cluster>``): both are known.
        known = alive | {item.group for item in self._instances()} | set(records)
        for key in self._MEMORY:
            memory = self.state.get(key)
            if isinstance(memory, dict):
                for name in [n for n in memory if n not in known]:
                    del memory[name]
        removed: list[str] = []
        if rule is not None:
            keep = {
                str(r.get("namespace")) for r in records.values() if r.get("namespace")
            }
            removed = sweep_env_dirs(self._branch_dirs(rule), keep, live)
        if removed or dropped:
            self.log(
                f"removed the state of {len(removed) + len(dropped)} environment(s) "
                "that no longer exist"
            )

    def _idle(self) -> list[dict[str, Any]]:
        rule = self.composition.branch_rule
        limit = None if rule is None else rule.env_config().idle_stop_seconds
        if limit is None:
            return []
        fixed = {
            item.name
            for item in self.composition.environments
            if isinstance(item, Environment)
        }
        now = self.clock()
        found = []
        for name, record in self._envs().items():
            if name in fixed or record.get("state") != "deployed":
                continue
            pushed = _seconds(record.get("pushed_at"))
            at = record.get("next_attempt_at")
            if pushed is None or now - pushed < limit or (at is not None and at > now):
                continue
            found.append(record)
        return sorted(found, key=lambda r: r["branch"])

    def _stop(self, record: dict[str, Any]) -> None:
        rule = self.composition.branch_rule
        assert rule is not None
        instance = self._instance(record["branch"])
        env: EnvItem = rule if instance is None else instance.env
        try:
            ports = self._ports_for(None if instance is None else instance.cluster)
            ports.env_stop(
                self._down_pipeline(env, ports),
                record["branch"] if instance is None else instance.deploys_as,
            )
        except Exception as error:
            attempts = int(record.get("attempts") or 0) + 1
            self._set(
                record,
                reason=error_code(error),
                attempts=attempts,
                next_attempt_at=self.clock() + backoff(self.config, attempts),  # type: ignore[arg-type]
            )
            return
        self._set(
            record,
            state="stopped",
            deployed_plan_hash=None,
            reason="idle-stop",
            attempts=0,
            next_attempt_at=None,
            stopped_at=_iso(self.clock()),
        )
        self._lifecycle(
            record, "stop", "stopped", by="idle", via="controller", reason="idle-stop"
        )
        self.log(f"{record['branch']}: no push for a while; scaled to zero")

    def _due(self) -> list[dict[str, Any]]:
        now = self.clock()

        def ready(record: dict[str, Any]) -> bool:
            if record.get("state") not in {"pending", "retrying", "deleting"}:
                return False
            if record.get("state") != "deleting" and not record.get("revision"):
                return False
            if record.get("stop_failed"):
                return False  # to be stopped: never deployed meanwhile
            at = record.get("next_attempt_at")
            return at is None or at <= now

        return sorted(
            (r for r in self._envs().values() if ready(r)),
            key=lambda r: (
                r["state"] != "deleting",
                r.get("pushed_at") or "",
                r.get("group") or r["branch"],
                self._wave(r),
                r["branch"],
            ),
        )

    def _step(
        self, record: dict[str, Any], work: Callable[[dict[str, Any]], None]
    ) -> None:
        if self.busy:  # pragma: no cover - guarded by the tests
            raise RuntimeError("controller steps must not overlap")
        self.busy = True
        deploy = work == self._deploy
        started = (_iso(self.clock()), record.get("trigger"), record.get("approval"))
        self._built, self._last_outcome = [], None
        self._builds = {}
        self._step_started, self._step_env = started[0], str(record.get("branch"))
        record.pop("interrupted", None)  # a new step of it runs
        # Added in 0.15.0: the step shows in the status while it runs, not
        # only when the whole poll ends (a long rollout left it minutes stale).
        record["in_progress"] = {
            "action": work.__name__.lstrip("_"),
            "since": started[0],
        }
        self._publish()
        self.telemetry.step_started(record, work.__name__.lstrip("_"))
        try:
            work(record)
        except Exception as error:  # one bad environment never stops the loop
            if isinstance(error, PipelineError | GitOpsError):
                self.log(f"{record['branch']}: {error}")  # registered, no secret
            failed = failed_verification(error, _iso(self.clock()))
            if failed is not None and record.get("state") != "deleting":
                self._degraded(record, failed)
            else:
                self._fail(record, step_reason(error), error)
        finally:
            self.busy = False
            record.pop("in_progress", None)
            if deploy:
                self._record_step(record, *started)
            self.telemetry.step_finished(
                record, work.__name__.lstrip("_"), self._last_outcome
            )
            save_state(self.state_dir, self.state)
            self._publish()

    def _record_step(
        self,
        record: dict[str, Any],
        started: str,
        trigger: Any,
        approval: Any,
    ) -> None:
        """Remember the deploy step in the history; keep its pending plan."""
        from piceli.gitops import history

        outcome = self._last_outcome
        run = self._step_run(outcome)
        self._checks(record, run, trigger)
        if (
            approval is None
            and outcome is not None
            and record.get("state") == "deployed"
            and record.get("last_action") == "deployed"
            and record.get("deployed_plan_hash") is None
        ):
            # Applied without an owner's approval: the policy covered it
            # (the pipeline's auto_approve or the environment's); ``at``
            # (0.16.0) is when the run it approved was created.
            approval = {
                "via": "policy",
                "at": (run or {}).get("approved_at")
                or (run or {}).get("started_at")
                or started,
            }
        if record.get("state") == "approval-required" and outcome is not None:
            record["pending_plan"] = (
                {**dict(outcome.plan), "plan_hash": outcome.plan_hash}
                if outcome.plan is not None
                else {"plan_hash": outcome.plan_hash}
            )
        else:
            record.pop("pending_plan", None)
        history.remember(
            self.state,
            str(record["branch"]),
            history.event(
                record,
                started_at=started,
                finished_at=_iso(self.clock()),
                trigger=trigger if isinstance(trigger, str) else None,
                approval=approval if isinstance(approval, Mapping) else None,
                outcome=outcome,
                built=[item for item in self._built if item != "mirror"],
                builds=self._builds,
            ),
        )
        if record.get("state") != "approval-required":
            record.pop("approval_required_since", None)
        if record.get("state") == "deployed":
            record["failed_attempts"] = []  # kept by this run's history event
        if record.get("approved_hash") is None:
            record.pop("approval", None)  # used (or replaced by a new plan)

    # ------------------------------------------------------------ poll
    def poll_once(self) -> dict[str, Any]:
        """One poll and the work it found; returns the published status."""
        now = self.clock()
        self.state["last_poll"] = _iso(now)
        if not self.recovered:
            self.recovered = True
            self._recover()
        self.sources.new_poll()
        failed: list[str] = []
        self.refs = {}
        for key in sorted(self.sources.remotes):
            failed += self._poll_source(key, now)
        if self.repo is not None:
            failed += self._load_composition(now)
        if failed:
            self.state["last_error"] = "gitops-git-failed"
            self.state["poll_failures"] = int(self.state.get("poll_failures") or 0) + 1
        else:
            self.state["last_error"] = None
            self.state["poll_failures"] = 0
        if self._composition is None:  # nothing to deploy before the first import
            return self._publish()
        self._requests()
        self._desired()
        if not self.swept and not failed:  # only on a complete view of the sources
            self.swept = True
            try:
                self._sweep()
            except OSError as error:  # never fail a poll on it
                self.log(f"stale state not swept ({type(error).__name__})")
        self.state["baseline"] = True
        save_state(self.state_dir, self.state)
        self._stops()
        self._polled = {}
        for record in self._due():
            if not self._reachable(record):
                continue
            if record.get("state") == "deleting":
                self._step(record, self._teardown)
                continue
            gate = self._gate(record)
            if gate is not None:
                self._hold(record, gate)
                continue
            self._step(record, self._deploy)
        for record in self._idle():
            self._step(record, self._stop)
        self._probe_clusters()
        self._registry_usage(now)
        return self._publish()

    # ------------------------------------------------------------ reach, rollout
    def _contact(self, cluster: str, *, fresh: bool = False) -> dict[str, Any]:
        """Whether ``cluster``'s API answers (probed once per poll, or ``fresh``)."""
        polled = self._polled
        if not fresh and cluster in polled:
            return dict(polled[cluster])
        probe = getattr(self.ports, "probe_cluster", None)
        found: Mapping[str, Any] = {"reachable": True, "reason": None}
        if callable(probe):
            try:
                found = probe(cluster)
            except Exception:  # never fail a poll on it
                found = {"reachable": False, "reason": "cluster-unreachable"}
        now = _iso(self.clock())
        memory = self.state.setdefault("clusters", {})
        entry = dict(memory.get(cluster) or {})
        reachable = bool(found.get("reachable"))
        entry.update(
            reachable=reachable,
            reason=None
            if reachable
            else str(found.get("reason") or "cluster-unreachable"),
            checked_at=now,
        )
        if reachable:
            entry["last_contact"] = now
        memory[cluster] = entry
        polled[cluster] = entry
        if not reachable and self._last_reach.get(cluster) is not False:
            self.log(f"cluster {cluster}: unreachable ({entry['reason']})")
        elif reachable and self._last_reach.get(cluster) is False:
            self.log(f"cluster {cluster}: reachable again")
        self._last_reach[cluster] = reachable
        return dict(entry)

    @property
    def _last_reach(self) -> dict[str, bool]:
        return self._reach_memory

    def _remote(self, record: Mapping[str, Any]) -> str | None:
        cluster = record.get("cluster")
        if isinstance(cluster, str) and cluster != self._home:
            return cluster
        return None

    def _reachable(self, record: dict[str, Any]) -> bool:
        """Probe the record's cluster; an unreachable one is put off (not failed)."""
        cluster = self._remote(record)
        if cluster is None:
            return True
        contact = self._contact(cluster)
        if contact.get("reachable"):
            record.pop("unreachable_attempts", None)
            return True
        self._unreachable(record, contact)
        save_state(self.state_dir, self.state)
        return False

    def _unreachable(self, record: dict[str, Any], contact: Mapping[str, Any]) -> None:
        """Retry at the next poll that reaches it, without counting an attempt:
        the environment's other clusters go on, and it converges when back.

        No backoff: every poll probes the cluster anyway (one short request),
        and the record is only stepped once the probe answers.
        """
        tries = int(record.get("unreachable_attempts") or 0) + 1
        record["unreachable_attempts"] = tries
        self._set(
            record,
            state="deleting" if record.get("state") == "deleting" else "retrying",
            reason=str(contact.get("reason") or "cluster-unreachable"),
            next_attempt_at=None,
        )
        if tries == 1:
            self.log(
                f"{record['branch']}: cluster {record.get('cluster')} unreachable "
                f"({record['reason']}); retried when it answers"
            )

    def _probe_clusters(self) -> None:
        """Probe every other cluster this poll did not reach (``last_contact``)."""
        names = {
            cluster
            for record in self._envs().values()
            if (cluster := self._remote(record)) is not None
        }
        names |= {item.name for item in self.composition.remote_clusters}
        for name in sorted(names):
            if name not in self._polled:
                self._contact(name)

    def _wave(self, record: Mapping[str, Any]) -> int:
        """The rollout wave of a cluster's record (0 without a rollout)."""
        group, cluster = record.get("group"), record.get("cluster")
        env = self._declared(group)
        if env is None or env.rollout is None or not isinstance(cluster, str):
            return 0
        from piceli.envs.placement import waves

        names = [p.name for p in self.composition.placements_of(env)]
        for index, wave in enumerate(waves(names, env.rollout)):
            if cluster in wave:
                return index
        return 0

    def _declared(self, group: Any) -> EnvItem | None:
        """The declared environment (or branch rule) of a group of records."""
        if not isinstance(group, str):
            return None
        item = self.composition.environment(group)
        if item is not None:
            return item
        rule = self.composition.branch_rule
        return rule

    def _gate(self, record: Mapping[str, Any]) -> str | None:
        """``None`` (deploy), ``"wait"`` or ``"stop"``: the rollout order.

        A cluster of a later wave deploys a revision only when every cluster
        of the earlier waves runs that revision, healthy (its checks passed);
        a failure there (failed, rolled back, degraded or held) stops it.
        """
        index = self._wave(record)
        if index == 0:
            return None
        from piceli.envs.placement import waves

        group = str(record.get("group"))
        env = self._declared(group)
        assert env is not None
        names = [p.name for p in self.composition.placements_of(env)]
        revision = record.get("revision")
        for wave in waves(names, env.rollout)[:index]:
            for cluster in wave:
                peer = self._envs().get(f"{group}@{cluster}")
                if peer is None or peer.get("revision") != revision:
                    return "wait"
                if peer.get("state") in {"failed", "held"} or (
                    peer.get("state") == "deployed"
                    and peer.get("health") not in {None, "healthy"}
                ):
                    return f"stop:{cluster}"
                if not (
                    peer.get("state") == "deployed"
                    and peer.get("deployed_revision") == revision
                ):
                    return "wait"
        return None

    def _hold(self, record: dict[str, Any], gate: str) -> None:
        if gate == "wait":
            waiting = CompositionError(
                "rollout-waiting", "an earlier wave has not passed this revision"
            )
            if record.get("reason") != waiting.code:
                self._set(record, reason=waiting.code)
            return
        peer = gate.partition(":")[2]
        stopped = CompositionError("rollout-stopped", f"{peer} failed this revision")
        self._set(
            record,
            state="held",
            reason=stopped.code,
            held_by=peer,
            next_attempt_at=None,
        )
        self.log(
            f"{record['branch']}: held; {peer} did not pass this revision "
            "(the rollout stops until a new revision)"
        )

    def _registry_usage(self, now: float) -> None:
        """Measure the in-cluster registry's claim, at most every
        :data:`REGISTRY_USAGE_SECONDS` (published as ``controller.registry_usage``)."""
        measure = getattr(self.ports, "registry_usage", None)
        registry = self.composition.registry
        if (
            not callable(measure)
            or getattr(registry, "kind", None) != "cluster-registry"
        ):
            return
        last = self.state.get("registry_usage")
        last = last if isinstance(last, dict) else {}
        if now - float(last.get("measured_epoch") or 0) < REGISTRY_USAGE_SECONDS:
            return
        try:
            found = measure(registry)
        except Exception as error:  # never fail a poll on it
            self.log(f"registry use not measured ({type(error).__name__})")
            found = None
        self.state["registry_usage"] = {
            **(found or {"used_bytes": None, "used_source": None}),
            "measured_at": _iso(now),
            "measured_epoch": now,
        }

    def _poll_source(self, key: str, now: float) -> list[str]:
        """``git ls-remote`` one source; ``[key]`` when it failed."""
        entry = self.state["sources"].setdefault(key, {})
        try:
            refs = self.sources.ls_remote(key)
        except GitOpsError as error:
            entry["last_error"] = error.code
            return [key]
        self.refs[key] = refs
        entry.update(
            url=self.sources.sources[key].url,
            refs=describe_refs(refs, self._followed_refs(key, refs)),
            last_poll=_iso(now),
            last_error=None,
        )
        return []

    def _load_composition(self, now: float) -> list[str]:
        """Import the composition at its branch's head when it moved; new sources' refs.

        A module that fails to import (or is invalid) is recorded with its
        code and not retried until the branch moves again; the previous
        composition keeps deploying.
        """
        assert self.repo is not None
        settings = self.repo.settings
        entry = self.state["composition"]
        entry.update(source=settings.name, branch=settings.branch, entry=settings.entry)
        refs = self.refs.get(settings.name)
        if refs is None:
            return []
        commit = refs.branches.get(settings.branch)
        if commit is None:
            entry["error"] = "composition-ref-unresolved"
            return []
        if commit == self.repo.commit or entry.get("failed") == commit:
            return []
        try:
            composition = self.repo.load(commit)
            clash = [
                s.key
                for s in composition.sources
                if s.key == settings.name and s.url != settings.url
            ]
            if clash:
                raise CompositionError(
                    "composition-invalid",
                    f"a source is named {settings.name!r} like the composition's "
                    "repository but has another URL",
                )
        except (CompositionError, GitOpsError) as error:
            entry.update(error=error.code)
            if isinstance(error, CompositionError):
                entry["failed"] = commit
            self.log(f"composition at {commit[:12]}: {error} ({error.code})")
            return []
        if self._composition is not None:
            self.log(f"composition {settings.name}@{commit[:12]}: re-rendering")
        self._composition = composition
        entry.update(commit=commit, failed=None, error=None, loaded_at=_iso(now))
        failed: list[str] = []
        for source in composition.sources:
            if source.key not in self.sources.remotes:
                self.sources.add(source)
                failed += self._poll_source(source.key, now)
        return failed

    def _followed_refs(self, key: str, refs: RemoteRefs) -> set[str]:
        wanted: set[str] = set()
        if self.repo is not None and key == self.repo.settings.name:
            wanted.add(f"refs/heads/{self.repo.settings.branch}")
        if self._composition is None:
            return wanted
        for item in self.composition.environments:
            for source, rules in item.sources:
                if source.key != key:
                    continue
                for rule in rules:
                    if rule == BRANCH and isinstance(item, BranchEnvironments):
                        wanted |= {
                            f"refs/heads/{b}"
                            for b in refs.branches
                            if item.branches.matches(b) or b == item.branches.fallback
                        }
                    elif isinstance(rule, Branch):
                        wanted.add(f"refs/heads/{rule.name}")
                    elif isinstance(rule, Promote):
                        # `piceli promote ENV BRANCH@SHA` picks any branch head.
                        wanted |= {f"refs/heads/{b}" for b in refs.branches}
                    elif isinstance(rule, Tag):
                        wanted |= {
                            f"refs/tags/{t}"
                            for t in refs.tags
                            if fnmatch.fnmatchcase(t, rule.pattern)
                        }
        return wanted

    def status(self) -> dict[str, Any]:
        """The published status (``piceli.gitops-status.v1`` with composition keys)."""
        envs = {
            name: {
                k: v
                for k, v in record.items()
                if k not in ("approved_hash", "release_checks")
            }
            for name, record in sorted(self._envs().items())
        }
        for record in envs.values():
            record["components"] = {
                key: {k: v for k, v in entry.items() if k != "deployed_image"}
                for key, entry in (record.get("components") or {}).items()
            }
        envs = self._grouped(envs)
        clusters = self._clusters_status()
        loaded = self._composition
        repo = (
            {"composition_repo": dict(self.state.get("composition") or {})}
            if self.repo is not None
            else {}
        )
        return {
            "schema": STATUS_SCHEMA,
            "controller": {
                "state": "degraded" if self.state.get("last_error") else "running",
                "version": _version(),
                "repo": None,
                "branches": [],
                "composition": None if loaded is None else loaded.name,
                "poll_seconds": self.config.poll_seconds,
                "last_poll": self.state.get("last_poll"),
                # Added in 0.16.0: refreshed while the process lives, also
                # during a long step (see piceli.gitops.heartbeat).
                "heartbeat_at": self.heartbeat_at,
                "heartbeat_seconds": HEARTBEAT_SECONDS,
                "last_error": self.state.get("last_error"),
                "poll_failures": int(self.state.get("poll_failures") or 0),
                # Added in 0.14.6: the registry claim's use measured by the
                # controller (``du`` in the registry pod), for the UI.
                "registry_usage": {
                    k: v
                    for k, v in (self.state.get("registry_usage") or {}).items()
                    if k in ("used_bytes", "used_source", "claim", "measured_at")
                }
                or None,
                "environments": []
                if loaded is None
                else [
                    {
                        "name": item.name,
                        "promote": any(
                            isinstance(rule, Promote)
                            for _, rules in item.sources
                            for rule in rules
                        ),
                        # Added in 0.14.7: declared Environment(stopped=True).
                        "stopped": isinstance(item, Environment) and item.stopped,
                    }
                    for item in loaded.environments
                ],
                **repo,
            },
            "sources": {
                key: {
                    "url": value.get("url"),
                    "refs": value.get("refs") or {},
                    "last_poll": value.get("last_poll"),
                    **(
                        {"error": value["last_error"]}
                        if value.get("last_error")
                        else {}
                    ),
                }
                for key, value in sorted(self.state["sources"].items())
            },
            "envs": envs,
            "rejected_requests": list(self.state.get("rejected") or []),
            # Added in 0.15, only with several clusters: each cluster's reach
            # and the placements removed from them.
            **({"clusters": clusters} if clusters else {}),
            **(
                {"removals": list(self.state["removals"])}
                if self.state.get("removals")
                else {}
            ),
        }

    #: Record keys of a cluster's record that only the controller uses.
    _INTERNAL = (
        "group",
        "deploys_as",
        "kind",
        "removal",
        "unreachable_attempts",
        "approval",
    )
    #: Aggregate state priority of an environment's clusters (first wins).
    _PRIORITY = (
        "failed",
        "approval-required",
        "deleting",
        "retrying",
        "pending",
        "held",
        "stopped",
        "deployed",
    )

    def _grouped(self, envs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """One status entry per environment; a multi-cluster one gains ``clusters``.

        Its entry is the record of its first cluster (by rollout wave) with
        the state, health and reason of the whole environment, and
        ``clusters.<name>``: ``state`` (``unreachable`` while its API does not
        answer), ``health``, ``checks``, ``revision``, ``reason``,
        ``last_contact``, ``namespace``, ``api`` and more.
        """
        groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        result: dict[str, dict[str, Any]] = {}
        for name, record in envs.items():
            group = record.get("group")
            if isinstance(group, str) and record.get("cluster"):
                groups.setdefault(group, []).append((name, record))
            else:
                result[name] = record
        for group, members in groups.items():
            members.sort(key=lambda item: (self._wave(item[1]), item[0]))
            views = {
                str(record["cluster"]): self._cluster_view(record)
                for _, record in members
            }
            first = members[0][1]
            chosen = first
            for state in self._PRIORITY:
                found = [
                    record for _, record in members if record.get("state") == state
                ]
                if found:
                    chosen = found[0]
                    break
            health = "healthy"
            for view in views.values():
                if view.get("state") == "unreachable" or view.get("health") in {
                    "degraded",
                    "unknown",
                    None,
                }:
                    health = (
                        "degraded" if view.get("health") == "degraded" else "unknown"
                    )
                    break
            entry = {k: v for k, v in first.items() if k not in self._INTERNAL}
            entry.update(
                branch=group,
                cluster=None,
                state=chosen.get("state"),
                reason=chosen.get("reason"),
                plan_hash=chosen.get("plan_hash"),
                health=health
                if chosen.get("state") == "deployed"
                else chosen.get("health"),
                clusters=views,
            )
            entry.pop("in_progress", None)  # the cluster at work, not the first
            busy = [
                {**view["in_progress"], "cluster": name}
                for name, view in views.items()
                if view.get("in_progress")
            ]
            if busy:
                entry["in_progress"] = busy[0]
            if chosen.get("state") == "approval-required" and chosen.get(
                "pending_plan"
            ):
                entry["pending_plan"] = chosen["pending_plan"]
            result[group] = entry
        return dict(sorted(result.items()))

    def _cluster_view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        cluster = str(record.get("cluster"))
        contact = (self.state.get("clusters") or {}).get(cluster) or {}
        declared = (
            self.composition.cluster_named(cluster) if self._composition else None
        )
        remote = cluster != self._home
        unreachable = remote and contact.get("reachable") is False
        state = record.get("state")
        return {
            "state": "unreachable" if unreachable and state != "stopped" else state,
            "health": record.get("health"),
            "checks": record.get("checks"),
            "revision": record.get("revision"),
            "deployed_revision": record.get("deployed_revision"),
            "reason": contact.get("reason") if unreachable else record.get("reason"),
            "last_contact": contact.get("last_contact") if remote else None,
            "namespace": record.get("namespace"),
            "api": None if declared is None else declared.api,
            "home": not remote,
            "plan_hash": record.get("plan_hash"),
            "last_sync": record.get("last_sync"),
            "components": record.get("components") or {},
            **({"held_by": record["held_by"]} if record.get("held_by") else {}),
            **({"wave": self._wave(record)} if record.get("group") else {}),
            **(
                {"in_progress": record["in_progress"]}
                if record.get("in_progress")
                else {}
            ),
        }

    def _clusters_status(self) -> dict[str, Any]:
        """``clusters.<name>``: api, home, reachable, reason, last contact (0.15)."""
        if self._composition is None or len(self.composition.clusters) < 2:
            return {}
        memory = self.state.get("clusters") or {}
        found: dict[str, Any] = {}
        for cluster in self.composition.clusters:
            entry = memory.get(cluster.name) or {}
            home = cluster.name == self._home
            found[cluster.name] = {
                "api": cluster.api,
                "home": home,
                "reachable": True if home else entry.get("reachable"),
                "reason": None if home else entry.get("reason"),
                "last_contact": None if home else entry.get("last_contact"),
                "checked_at": None if home else entry.get("checked_at"),
            }
        return found

    def _publish(self) -> dict[str, Any]:
        save_state(self.state_dir, self.state)
        with self.publish_lock:
            self.heartbeat_at = _iso(self.clock())
            status = self.status()
            try:
                self.channel.publish(status)
            except GitOpsError as error:
                self.log(f"status not published ({error.code})")
        self._publish_history()
        return status

    def beat(self) -> None:
        """Publish ``controller.heartbeat_at`` only (the heartbeat thread)."""
        publish = getattr(self.channel, "publish_heartbeat", None)
        with self.publish_lock:
            self.heartbeat_at = _iso(self.clock())
            if callable(publish):
                publish(self.heartbeat_at)

    def _recover(self) -> None:
        """At start: mark the steps and runs the previous process died under."""
        from piceli.gitops import history
        from piceli.gitops.recovery import (
            INTERRUPTED,
            interrupt_runs,
            interrupted_steps,
        )

        at = _iso(self.clock())
        for name, step in interrupted_steps(self._envs(), at):
            record = self._envs()[name]
            self.log(f"{name}: its {step.get('action')} was interrupted (restart)")
            if step.get("action") != "deploy":
                continue
            entry = history.event(
                record,
                started_at=str(step.get("since") or at),
                finished_at=at,
                trigger=record.get("trigger")
                if isinstance(record.get("trigger"), str)
                else None,
                approval=None,
                outcome=None,
                built=[],
            )
            entry.update(state="interrupted", reason=INTERRUPTED, action=None)
            entry.pop("failure", None)
            history.remember(self.state, name, entry)
        try:
            marked = interrupt_runs(self.state_dir)
        except OSError as error:  # never fail a start on it
            self.log(f"interrupted runs not marked ({type(error).__name__})")
            return
        if marked:
            self.log(f"{len(marked)} run(s) interrupted by the restart marked")

    def _lifecycle(
        self,
        record: Mapping[str, Any],
        kind: str,
        state: str,
        *,
        by: Any = None,
        via: Any = None,
        at: Any = None,
        reason: Any = None,
    ) -> None:
        """Remember a stop, start or teardown in the history (0.16.0).

        ``at`` is when it was asked (a request's time), now by default.
        """
        from piceli.gitops import history

        now = _iso(self.clock())
        history.remember(
            self.state,
            str(record["branch"]),
            history.lifecycle(
                kind,
                at=now,
                started_at=self._step_started or now,
                state=state,
                by=by,
                via=via,
                reason=reason,
                namespace=record.get("namespace"),
                cluster=record.get("cluster"),
            )
            | ({"requested_at": str(at)[:32]} if at else {}),
        )

    def _history_document(self) -> dict[str, Any]:
        """``piceli.gitops-history.v1``: an environment on several clusters
        lists the runs of all of them, each with its ``cluster`` (0.15)."""
        from piceli.gitops import history

        records = self._envs()
        events = self.state.get("history") or {}
        plain = {k: v for k, v in records.items() if not v.get("cluster")}
        document = history.history(plain, events, self.state_dir, reader=self._runs)
        merged: dict[str, list[dict[str, Any]]] = {}
        for name, record in sorted(records.items()):
            if not record.get("cluster"):
                continue
            runs = self._runs.runs(self._run_dirs(record))
            own = [item for item in events.get(name) or () if isinstance(item, Mapping)]
            for entry in history.environment_runs(name, own, runs):
                entry["cluster"] = record["cluster"]
                merged.setdefault(str(record.get("group") or name), []).append(entry)
        for group, entries in merged.items():
            entries.sort(
                key=lambda item: str(item.get("started_at") or ""), reverse=True
            )
            document["envs"][group] = {"runs": entries[: history.MAX_RUNS]}
        return history.bounded(document) if merged else document

    def _publish_history(self) -> None:
        """Publish each environment's runs (``piceli-gitops-history``) when they changed."""
        import hashlib

        from piceli.gitops import history

        publish = getattr(self.channel, "publish_history", None)
        if not callable(publish):
            return
        try:
            if self._runs is None:
                self._runs = history.RunReader()
            history.forget_gone(self.state, self._envs())
            document = self._history_document()
            digest = hashlib.sha256(
                json.dumps(document, sort_keys=True).encode()
            ).hexdigest()
            if digest == self._history_digest:
                return
            document["generated_at"] = _iso(self.clock())
            publish(document)
            self._history_digest = digest
        except GitOpsError as error:
            self.log(f"history not published ({error.code})")
        except (OSError, ValueError) as error:  # never fail a poll on it
            self.log(f"history not published ({type(error).__name__})")
