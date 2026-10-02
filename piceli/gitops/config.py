"""Settings of Piceli's GitOps controller (``piceli gitops enable|run``).

The configuration is plain data: it is rendered into the controller's
ConfigMap by ``piceli gitops enable`` (so it is part of the install plan
hash) and read back by ``piceli gitops run``. It never holds credentials:
the Git credentials come from a Secret mounted as files.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.gitops import GitOpsError

CONFIG_SCHEMA = "piceli.gitops-config.v1"
#: The namespace the controller runs in unless ``--namespace`` says otherwise.
DEFAULT_NAMESPACE = "piceli-system"
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 3600
MAX_BACKOFF_SECONDS = 3600

_GLOB = re.compile(r"[A-Za-z0-9._/*?\[\]!-]{1,200}")
_ENTRY = re.compile(r"[A-Za-z0-9_./-]+(?:\.py)?:[A-Za-z_][A-Za-z0-9_.]*")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_DURATION = re.compile(r"(\d+)\s*([smh]?)")
_SCHEMES = ("https://", "ssh://", "file://")
_SCP = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^/].*|[A-Za-z0-9.-]+:[^/].*")
_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?")
_PINNED = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
_QUANTITY = re.compile(r"[0-9]+(?:Ki|Mi|Gi|Ti)")
_SELECTOR_KEY = re.compile(r"(?:[a-z0-9.-]{1,253}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
_SELECTOR_VALUE = re.compile(r"[A-Za-z0-9._-]{0,63}")


def parse_selector(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """``key=value`` labels (``--builder-selector``)."""
    pairs: list[tuple[str, str]] = []
    for item in values:
        key, sep, value = item.partition("=")
        if not sep:
            raise GitOpsError(
                "gitops-config-invalid",
                f"--builder-selector takes key=value, got {item!r}",
            )
        pairs.append((key.strip(), value.strip()))
    return tuple(sorted(pairs))


def parse_duration(value: str | int) -> int:
    """Seconds from ``60``, ``60s``, ``5m`` or ``1h``.

    :raises GitOpsError: ``gitops-config-invalid`` for anything else.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _DURATION.fullmatch(str(value).strip())
    if match is None:
        raise GitOpsError(
            "gitops-config-invalid",
            f"invalid duration {value!r}; use seconds or 30s, 5m, 1h",
        )
    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def check_repo_url(url: str) -> str:
    """Refuse a repository URL that carries credentials or is not a Git remote.

    Credentials belong in the Secret named by ``--credentials-secret``; a URL
    with ``user:password@`` would end up in the ConfigMap, the status and
    process lists (``gitops-repo-invalid``). ``git@host:path`` (scp syntax) is
    allowed: the part before ``@`` is the SSH user, not a secret.
    """
    text = url.strip()
    if not text or any(ch.isspace() for ch in text) or text.startswith("-"):
        raise GitOpsError(
            "gitops-repo-invalid", "the repository URL is empty or malformed"
        )
    if text.startswith(_SCHEMES):
        authority = text.split("://", 1)[1].split("/", 1)[0]
        if "@" in authority and not text.startswith("ssh://"):
            raise GitOpsError(
                "gitops-repo-invalid",
                "the repository URL carries credentials; put them in the Secret "
                "named by --credentials-secret",
            )
        if text.startswith("ssh://") and ":" in authority.split("@", 1)[0]:
            raise GitOpsError(
                "gitops-repo-invalid",
                "the repository URL carries a password; use an SSH key in the "
                "Secret named by --credentials-secret",
            )
        return text
    if text.startswith("/") or _SCP.fullmatch(text):
        return text
    raise GitOpsError(
        "gitops-repo-invalid",
        "the repository must be an https://, ssh://, file:// or git@host:path URL",
    )


def parse_globs(value: str | Sequence[str]) -> tuple[str, ...]:
    """Branch globs from ``'main,wp-*'`` or a list."""
    items = value.split(",") if isinstance(value, str) else list(value)
    globs = tuple(dict.fromkeys(item.strip() for item in items if item.strip()))
    if not globs:
        raise GitOpsError("gitops-config-invalid", "--branches names no branch")
    for glob in globs:
        if not _GLOB.fullmatch(glob) or ".." in glob:
            raise GitOpsError("gitops-config-invalid", f"invalid branch glob {glob!r}")
    return globs


@dataclass(frozen=True)
class EnvRule:
    """When the controller deploys one named environment (from ``Environment.follow``).

    :param name: The environment's name (its key in the status).
    :param branches: Exact branches whose every push deploys it.
    :param tags: Tag globs whose new tags deploy it.
    :param promote: ``piceli promote NAME BRANCH@SHA`` may target it.
    :param auto_approve: The owner's opt-in to deploy it without a hash
        approval when the plan is inside the pipeline's ``auto_approve``.
    """

    name: str
    branches: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    promote: bool = False
    auto_approve: bool = False

    def __post_init__(self) -> None:
        if not _LABEL.fullmatch(self.name or ""):
            raise GitOpsError("gitops-config-invalid", "invalid environment name")
        object.__setattr__(self, "branches", tuple(self.branches))
        object.__setattr__(self, "tags", tuple(self.tags))
        for value in (*self.branches, *self.tags):
            if not isinstance(value, str) or not _GLOB.fullmatch(value):
                raise GitOpsError(
                    "gitops-config-invalid",
                    f"environment {self.name}: invalid branch or tag {value!r}",
                )

    @classmethod
    def from_environment(cls, environment: Any) -> EnvRule:
        """The rule of a :class:`piceli.envs.Environment`."""
        return cls(
            name=environment.name,
            branches=tuple(environment.branches),
            tags=tuple(environment.tags),
            promote=bool(environment.promote),
            auto_approve=bool(environment.auto_approve),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "branches": list(self.branches),
            "tags": list(self.tags),
            "promote": self.promote,
            "auto_approve": self.auto_approve,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EnvRule:
        return cls(
            name=str(value["name"]),
            branches=tuple(value.get("branches") or ()),
            tags=tuple(value.get("tags") or ()),
            promote=bool(value.get("promote", False)),
            auto_approve=bool(value.get("auto_approve", False)),
        )


@dataclass(frozen=True)
class ControllerConfig:
    """What the controller watches and deploys.

    :param pipeline: ``path/to/file.py:ATTR`` (or ``module:ATTR``) of the
        ``Pipeline``, relative to the repository root.
    :param repo: The Git remote (no credentials in the URL).
    :param branches: Branch globs; ``main_branch`` must be one of them to
        deploy main at all.
    :param env: The pipeline environment (``--env``), if it has several.
    :param poll_seconds: Seconds between polls of the remote.
    :param main_branch: The branch that deploys only on tags or promotions.
    :param tags: Glob of the tags that deploy ``main_branch`` (``v*``).
    :param main_auto_approve: The owner's opt-in to deploy main without a
        hash approval when the plan is inside the pipeline's ``auto_approve``
        policy. Off: every main deploy waits for ``piceli gitops approve``.
    :param max_attempts: Failed attempts of one commit before it is left
        ``failed`` until the next push.
    :param backoff_seconds: First retry delay; doubles per attempt (capped).
    :param platforms: Build platforms passed to the cluster build (default
        ``linux/arm64`` and ``linux/amd64``).
    :param builder_image: The cluster build's image, pinned by digest; without
        it a branch deploys only images pushed with ``piceli env push``.
    :param build_git_secret: The Secret (``username``/``password``) the build
        Job fetches the repository with (default ``piceli-build-git``).
    :param builder_selector: Labels of the builder node (default: the
        cluster build's, ``piceli.io/builder=true`` on ``amd64``).
    :param build_storage: Size of a branch's build cache claim.
    :param build_registry: ``oci://host[:port]/prefix`` the build pushes to
        (default: the pipeline's delivery).
    :param node_registry: ``host[:port]`` nodes pull from, when different.
    :param namespace: The controller's namespace.
    :param environments: Named environments and their triggers (read from
        the pipeline's ``EnvConfig(environments=...)`` by ``gitops enable``).
        One named ``main_branch`` replaces the implicit main rule (tags
        ``tags`` and promotions).
    :param idle_stop_seconds: Scale a branch environment to zero after no
        push for this long (``EnvConfig(idle_stop=...)``); ``None``: never.
    """

    pipeline: str
    repo: str
    branches: tuple[str, ...]
    env: str | None = None
    poll_seconds: int = 60
    main_branch: str = "main"
    tags: str = "v*"
    main_auto_approve: bool = False
    max_attempts: int = 5
    backoff_seconds: int = 30
    platforms: tuple[str, ...] = ()
    namespace: str = DEFAULT_NAMESPACE
    builder_image: str | None = None
    build_git_secret: str | None = None
    builder_selector: tuple[tuple[str, str], ...] = ()
    build_storage: str = "20Gi"
    build_registry: str | None = None
    node_registry: str | None = None
    environments: tuple[EnvRule, ...] = ()
    idle_stop_seconds: int | None = None

    def __post_init__(self) -> None:
        if not _ENTRY.fullmatch(self.pipeline) or ".." in self.pipeline.split(":")[0]:
            raise GitOpsError(
                "gitops-config-invalid",
                f"pipeline must be path/to/file.py:ATTR or module:ATTR, got {self.pipeline!r}",
            )
        object.__setattr__(self, "repo", check_repo_url(self.repo))
        object.__setattr__(self, "branches", parse_globs(self.branches))
        if not MIN_POLL_SECONDS <= self.poll_seconds <= MAX_POLL_SECONDS:
            raise GitOpsError(
                "gitops-config-invalid",
                f"--poll must be {MIN_POLL_SECONDS}s to {MAX_POLL_SECONDS}s",
            )
        if not 1 <= self.max_attempts <= 100 or not 1 <= self.backoff_seconds <= 3600:
            raise GitOpsError(
                "gitops-config-invalid", "max_attempts 1-100, backoff_seconds 1-3600"
            )
        if not _GLOB.fullmatch(self.main_branch) or any(
            ch in self.main_branch for ch in "*?[]"
        ):
            raise GitOpsError("gitops-config-invalid", "invalid main branch name")
        if not _GLOB.fullmatch(self.tags):
            raise GitOpsError("gitops-config-invalid", "invalid tag glob")
        if not _LABEL.fullmatch(self.namespace):
            raise GitOpsError("gitops-config-invalid", "invalid controller namespace")
        if self.builder_image is not None and not _PINNED.fullmatch(self.builder_image):
            raise GitOpsError(
                "gitops-image-unpinned",
                "--builder-image must be pinned by digest: repo@sha256:<64 hex>",
            )
        if not _QUANTITY.fullmatch(self.build_storage):
            raise GitOpsError(
                "gitops-config-invalid", "--build-storage must be like 20Gi"
            )
        for key, value in self.builder_selector:
            if not _SELECTOR_KEY.fullmatch(key) or not _SELECTOR_VALUE.fullmatch(value):
                raise GitOpsError(
                    "gitops-config-invalid", f"invalid builder selector {key}={value}"
                )
        for platform in self.platforms:
            if not _PLATFORM.fullmatch(platform):
                raise GitOpsError(
                    "gitops-config-invalid", f"invalid platform {platform!r}"
                )
        object.__setattr__(self, "environments", tuple(self.environments))
        names = [rule.name for rule in self.environments]
        if len(set(names)) != len(names) or not all(
            isinstance(rule, EnvRule) for rule in self.environments
        ):
            raise GitOpsError(
                "gitops-config-invalid", "environment names must be unique"
            )
        if self.idle_stop_seconds is not None and (
            not isinstance(self.idle_stop_seconds, int) or self.idle_stop_seconds < 60
        ):
            raise GitOpsError(
                "gitops-config-invalid", "idle_stop must be at least a minute"
            )

    def watches(self, branch: str) -> bool:
        """Whether ``branch`` matches one of the branch globs."""
        return any(fnmatch.fnmatchcase(branch, glob) for glob in self.branches)

    @property
    def deploys_main(self) -> bool:
        return self.watches(self.main_branch)

    def rules(self) -> tuple[EnvRule, ...]:
        """Every named environment's rule, the implicit main one first.

        The implicit main rule (tags ``tags``, promotions, ``main_auto_approve``)
        applies when no declared environment is named ``main_branch`` and the
        branch globs include it, as in 0.13.
        """
        declared = {rule.name for rule in self.environments}
        implicit: tuple[EnvRule, ...] = ()
        if self.main_branch not in declared and self.deploys_main:
            implicit = (
                EnvRule(
                    self.main_branch,
                    tags=(self.tags,),
                    promote=True,
                    auto_approve=self.main_auto_approve,
                ),
            )
        return implicit + self.environments

    def rule(self, name: str) -> EnvRule | None:
        return next((rule for rule in self.rules() if rule.name == name), None)

    def fixed(self, name: str) -> bool:
        """``name`` is main or a named environment: never a branch environment."""
        return name == self.main_branch or any(
            rule.name == name for rule in self.environments
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CONFIG_SCHEMA,
            "pipeline": self.pipeline,
            "repo": self.repo,
            "branches": list(self.branches),
            "env": self.env,
            "poll_seconds": self.poll_seconds,
            "main_branch": self.main_branch,
            "tags": self.tags,
            "main_auto_approve": self.main_auto_approve,
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "platforms": list(self.platforms),
            "namespace": self.namespace,
            "build": {
                "builder_image": self.builder_image,
                "git_secret": self.build_git_secret,
                "selector": dict(self.builder_selector),
                "storage": self.build_storage,
                "registry": self.build_registry,
                "node_registry": self.node_registry,
            },
            # 0.14 keys only when used: a 0.13 install plan hashes the same.
            **(
                {"environments": [rule.to_dict() for rule in self.environments]}
                if self.environments
                else {}
            ),
            **(
                {"idle_stop_seconds": self.idle_stop_seconds}
                if self.idle_stop_seconds is not None
                else {}
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControllerConfig:
        if value.get("schema") != CONFIG_SCHEMA:
            raise GitOpsError(
                "gitops-config-invalid", "unknown controller config schema"
            )
        build = value.get("build") or {}
        try:
            return cls(
                pipeline=value["pipeline"],
                repo=value["repo"],
                branches=tuple(value["branches"]),
                env=value.get("env"),
                poll_seconds=int(value.get("poll_seconds", 60)),
                main_branch=value.get("main_branch", "main"),
                tags=value.get("tags", "v*"),
                main_auto_approve=bool(value.get("main_auto_approve", False)),
                max_attempts=int(value.get("max_attempts", 5)),
                backoff_seconds=int(value.get("backoff_seconds", 30)),
                platforms=tuple(value.get("platforms") or ()),
                namespace=value.get("namespace", DEFAULT_NAMESPACE),
                builder_image=build.get("builder_image"),
                build_git_secret=build.get("git_secret"),
                builder_selector=tuple(sorted((build.get("selector") or {}).items())),
                build_storage=build.get("storage") or "20Gi",
                build_registry=build.get("registry"),
                node_registry=build.get("node_registry"),
                environments=tuple(
                    EnvRule.from_dict(item) for item in value.get("environments") or ()
                ),
                idle_stop_seconds=value.get("idle_stop_seconds"),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, GitOpsError):
                raise
            raise GitOpsError(
                "gitops-config-invalid", f"invalid controller config: {error}"
            ) from None

    @classmethod
    def load(cls, path: Path) -> ControllerConfig:
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            raise GitOpsError(
                "gitops-config-invalid", "cannot read the controller config file"
            ) from None
        return cls.from_dict(document)


def backoff(config: ControllerConfig, attempts: int) -> int:
    """Seconds to wait before attempt ``attempts + 1`` (doubling, capped)."""
    return min(config.backoff_seconds * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SECONDS)
