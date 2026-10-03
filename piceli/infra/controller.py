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

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import json
import re
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
    _iso,
    _seconds,
    _version,
    _version_key,
    error_code,
    failed_verification,
    failure_detail,
    owner_approved,
    replan_stale,
    verified,
)
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
        "component-need-unmet",
        "component-contract-invalid",
        "component-contract-missing",
        "component-build-unsupported",
        "composition-invalid",
    }
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

    def __post_init__(self) -> None:
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
    ) -> None:
        self.envs = envs
        self.builder = builder
        self.kubeconfig = kubeconfig
        self.context = context
        self.state_dir = state_dir
        self.transport = transport
        self.log: Callable[[str], None] = getattr(envs, "log", None) or (lambda _: None)

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

    def env_stop(self, pipeline: Any, name: str) -> None:
        self.envs.env_stop(pipeline, name)

    def namespace_live(self, namespace: str) -> bool | None:
        live = getattr(self.envs, "namespace_live", None)
        return live(namespace) if callable(live) else None


# ---------------------------------------------------------------- the loop


@dataclass
class _Instance:
    """One deployable environment: a named one, or one branch of the rule."""

    name: str
    env: EnvItem
    branch: str | None = None

    @property
    def rules(self) -> tuple[tuple[Any, tuple[Any, ...]], ...]:
        return self.env.sources


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
        for key in ("sources", "env_seen", "promoted", "forced", "composition"):
            self.state.setdefault(key, {})
        self.refs: dict[str, RemoteRefs] = {}
        self.busy = False
        self.swept = False  # stale state is swept once, at the first full poll

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
            "approved_hash": None,
            "reason": None,
            "failure": None,
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
        attempts = int(record.get("attempts") or 0) + 1
        deleting = record.get("state") == "deleting"
        record["failure"] = None if error is None else failure_detail(error)
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

    # ------------------------------------------------------------ instances
    def _instances(self) -> list[_Instance]:
        found = [
            _Instance(item.name, item)
            for item in self.composition.environments
            if isinstance(item, Environment)
        ]
        rule = self.composition.branch_rule
        if rule is not None:
            for branch in self._rule_branches(rule):
                found.append(
                    _Instance(self._branch_env_name(rule, branch), rule, branch)
                )
        return found

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
        promoted = self.state["promoted"].get(instance.name) or {}
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
            forced = self.state["forced"].pop(instance.name, None)
            if forced is not None:
                triggers.append(forced["trigger"])
            if not triggers:
                continue
            revision, used, missing = self._resolve(instance)
            if missing:
                record = self._envs().setdefault(
                    instance.name, {"branch": instance.name, "state": "pending"}
                )
                unresolved = CompositionError(
                    "composition-ref-unresolved", "a followed ref is missing"
                )
                self._set(record, reason=unresolved.code, missing_sources=missing)
                self.log(f"{instance.name}: waiting for a ref of {', '.join(missing)}")
                continue
            self._want(instance.name, revision, used, triggers[0])
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
            if not complete:
                continue
            known.pop(name, None)
            self._set(
                record, state="deleting", attempts=0, next_attempt_at=None, reason=None
            )
            self.log(f"{name}: branch gone; tearing its environment down")

    # ------------------------------------------------------------ requests
    def _requests(self) -> None:
        try:
            pending = self.channel.requests()
        except GitOpsError as error:
            self.state["last_error"] = error.code
            return
        handled: list[str] = []
        for key, body in sorted(pending.items()):
            handled.append(key)
            kind = body.get("kind")
            try:
                if kind == "approve":
                    self._approve(body)
                elif kind == "promote":
                    self._promote(body)
                elif kind == "sync":
                    self._sync(body)
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
        record = self._envs().get(str(body.get("env")))
        if (
            record is None
            or record.get("state") != "approval-required"
            or record.get("plan_hash") != body.get("plan_hash")
        ):
            raise GitOpsError(
                "gitops-approval-stale",
                "the environment is not waiting for that plan hash",
            )
        self._set(
            record,
            state="pending",
            approved_hash=body["plan_hash"],
            attempts=0,
            next_attempt_at=None,
            reason=None,
        )

    def _promote(self, body: Mapping[str, Any]) -> None:
        name = str(body.get("env") or "")
        instance = self._instance(name)
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
        names = [item.name for item in self._instances()]
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
        name = record["branch"]
        if hasattr(self.ports.builder, "environment"):
            # Its build Jobs are labelled with it (removed at its teardown).
            self.ports.builder.environment = name
        instance = self._instance(name)
        if instance is None:
            raise CompositionError("composition-invalid", f"no environment {name!r}")
        if instance.env.pipeline is not None:
            self._deploy_pipeline(record, instance)
            return
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
        try:
            if to_mirror:
                for key, image in self.ports.builder.mirror(to_mirror).items():
                    self._remember(
                        key, str(self.composition.component(key).mirrored), image
                    )
                    images[key] = image
            if to_build:
                built = self.ports.builder.build(to_build, self.sources.checkout)
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
        pipeline = self.ports.pipeline(
            self.composition,
            instance.env,
            contracts,
            {key: image.pull_ref for key, image in images.items()},
        )
        namespace = self.ports.prepare_env(pipeline, name)
        if namespace:
            record["namespace"] = namespace
        outcome = EnvOutcome.from_result(
            replan_stale(
                record,
                lambda approve: self.ports.env_up(
                    pipeline,
                    name,
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
        name = record["branch"]
        env = instance.env
        pipeline = env.pipeline
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
                primary = next(iter(sorted(key.sources)), repo)
                components[image.name] = {
                    "source": primary,
                    "commit": revision.get(primary),
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
        try:
            if to_mirror:
                for ref, image in self.ports.builder.mirror(to_mirror).items():
                    self._remember("mirror", key_of(ref), image)
            for request in requests:
                built = self.ports.builder.build_spec(request, self.sources.checkout)
                for image_name, wanted in request.images.items():
                    if image_name not in built:
                        raise CompositionError(
                            "component-build-failed",
                            f"the build returned no image {image_name!r}",
                        )
                    self._remember(image_name, wanted["key"], built[image_name])
                    images[image_name] = built[image_name]
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
        refs = {key: image.pull_ref for key, image in images.items()}
        target = self.ports.pipeline_env(self.composition, env, refs)
        namespace = self.ports.prepare_env(target, name)
        if namespace:
            record["namespace"] = namespace
        outcome = EnvOutcome.from_result(
            replan_stale(
                record,
                lambda approve: self.ports.env_up(
                    target,
                    name,
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

    def _down_pipeline(self, env: EnvItem) -> Any:
        """A pipeline that only knows the environment (for env_down, env_stop)."""
        if env.pipeline is not None:
            return self.ports.pipeline_env(self.composition, env, {})
        return self.ports.pipeline(self.composition, env, {}, {})

    def _outcome(self, record: dict[str, Any], outcome: EnvOutcome) -> None:
        if outcome.namespace:
            record["namespace"] = outcome.namespace
        now = _iso(self.clock())
        components = record.get("components") or {}
        if outcome.state == "deployed":
            for entry in components.values():
                if entry["state"] == "rolling":
                    entry["state"] = "synced"
                entry["deployed_image"] = entry["image"]
                entry["health"] = "healthy"
                entry["updated_at"] = now
            action = outcome.action or "deployed"
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
            changed = sorted(k for k, v in components.items() if v["state"] == "synced")
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
        elif outcome.state == "approval-required":
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
        rule = self.composition.branch_rule
        if rule is None:
            self._forget(name)
            del self._envs()[name]
            return
        # No contracts: a pipeline that only knows the environment's settings.
        self.ports.env_down(self._down_pipeline(rule), name)
        remove_env_dir(self._branch_dirs(rule), record.get("namespace"))
        self._forget(name)
        del self._envs()[name]
        self.log(f"{name}: environment removed")

    def _branch_dirs(self, rule: BranchEnvironments) -> Path:
        """Where the rule's branch environments keep their pipeline state."""
        return self.state_dir / "pipelines" / rule.name / "branches"

    #: Per-environment memory besides its record (keyed by environment name).
    _MEMORY = ("env_seen", "promoted", "forced", "rebuild")

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
        known = alive | set(records)
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
        try:
            self.ports.env_stop(self._down_pipeline(rule), record["branch"])
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
        self.log(f"{record['branch']}: no push for a while; scaled to zero")

    def _due(self) -> list[dict[str, Any]]:
        now = self.clock()

        def ready(record: dict[str, Any]) -> bool:
            if record.get("state") not in {"pending", "retrying", "deleting"}:
                return False
            if record.get("state") != "deleting" and not record.get("revision"):
                return False
            at = record.get("next_attempt_at")
            return at is None or at <= now

        return sorted(
            (r for r in self._envs().values() if ready(r)),
            key=lambda r: (
                r["state"] != "deleting",
                r.get("pushed_at") or "",
                r["branch"],
            ),
        )

    def _step(
        self, record: dict[str, Any], work: Callable[[dict[str, Any]], None]
    ) -> None:
        if self.busy:  # pragma: no cover - guarded by the tests
            raise RuntimeError("controller steps must not overlap")
        self.busy = True
        try:
            work(record)
        except Exception as error:  # one bad environment never stops the loop
            if isinstance(error, PipelineError | GitOpsError):
                self.log(f"{record['branch']}: {error}")  # registered, no secret
            failed = failed_verification(error, _iso(self.clock()))
            if failed is not None and record.get("state") != "deleting":
                self._degraded(record, failed)
            else:
                self._fail(record, error_code(error), error)
        finally:
            self.busy = False
            save_state(self.state_dir, self.state)

    # ------------------------------------------------------------ poll
    def poll_once(self) -> dict[str, Any]:
        """One poll and the work it found; returns the published status."""
        now = self.clock()
        self.state["last_poll"] = _iso(now)
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
        for record in self._due():
            if record.get("state") == "deleting":
                self._step(record, self._teardown)
            else:
                self._step(record, self._deploy)
        for record in self._idle():
            self._step(record, self._stop)
        return self._publish()

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
            name: {k: v for k, v in record.items() if k != "approved_hash"}
            for name, record in sorted(self._envs().items())
        }
        for record in envs.values():
            record["components"] = {
                key: {k: v for k, v in entry.items() if k != "deployed_image"}
                for key, entry in (record.get("components") or {}).items()
            }
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
                "last_error": self.state.get("last_error"),
                "poll_failures": int(self.state.get("poll_failures") or 0),
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
        }

    def _publish(self) -> dict[str, Any]:
        save_state(self.state_dir, self.state)
        status = self.status()
        try:
            self.channel.publish(status)
        except GitOpsError as error:
            self.log(f"status not published ({error.code})")
        return status
