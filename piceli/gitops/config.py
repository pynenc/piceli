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
from dataclasses import dataclass, field
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
    :param platforms: Build platforms passed to the cluster build.
    :param namespace: The controller's namespace.
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
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False)

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
        for platform in self.platforms:
            if not _PLATFORM.fullmatch(platform):
                raise GitOpsError(
                    "gitops-config-invalid", f"invalid platform {platform!r}"
                )

    def watches(self, branch: str) -> bool:
        """Whether ``branch`` matches one of the branch globs."""
        return any(fnmatch.fnmatchcase(branch, glob) for glob in self.branches)

    @property
    def deploys_main(self) -> bool:
        return self.watches(self.main_branch)

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
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ControllerConfig:
        if value.get("schema") != CONFIG_SCHEMA:
            raise GitOpsError(
                "gitops-config-invalid", "unknown controller config schema"
            )
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
