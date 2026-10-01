"""Credential profiles: a name for a kubeconfig file and context, kept outside the repo.

``piceli login NAME --kubeconfig FILE [--context CTX]`` stores a *reference*
(the absolute path of the kubeconfig and the context name) in
``$PICELI_PROFILES_DIR`` (default ``~/.config/piceli/profiles``) as
``NAME.json`` (mode ``0600`` in a ``0700`` directory). The kubeconfig's
contents and any token are never copied. A declared target then names the
profile (``Target.profile("NAME", ...)``, ``credentials = "NAME"`` in
``[target]``) and the path never reaches Git.

A profile is resolved when it is used, never at import or plan-hash time.
Inside a cluster (``PICELI_IN_CLUSTER=1`` or a mounted service-account token)
every profile name resolves to the pod's service account instead.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from piceli.k8s.ops.exec_credentials import ProviderFactoryError

__all__ = [
    "IN_CLUSTER_CONTEXT",
    "Profile",
    "ProfileError",
    "ResolvedCredentials",
    "list_profiles",
    "load_profile",
    "override",
    "profiles_dir",
    "remove_profile",
    "resolve",
    "save_profile",
    "set_override",
]

PROFILES_ENV = "PICELI_PROFILES_DIR"
IN_CLUSTER_ENV = "PICELI_IN_CLUSTER"
SERVICE_ACCOUNT_ENV = "PICELI_SERVICE_ACCOUNT_DIR"
DEFAULT_SERVICE_ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
IN_CLUSTER_CONTEXT = "piceli-incluster"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62})")
_SCHEMA = "piceli.profile.v1"


class ProfileError(ProviderFactoryError):
    """A profile is missing, invalid or conflicts with another option."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True)
class Profile:
    """A stored reference: the kubeconfig file and the context in it."""

    name: str
    kubeconfig: Path
    context: str

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kubeconfig": str(self.kubeconfig),
            "context": self.context,
            "kubeconfig_present": self.kubeconfig.is_file(),
        }


@dataclass(frozen=True)
class ResolvedCredentials:
    """What a profile name stands for right now."""

    kubeconfig: Path
    context: str
    in_cluster: bool = False


def profiles_dir() -> Path:
    """``$PICELI_PROFILES_DIR`` or ``~/.config/piceli/profiles`` (read on each call)."""
    value = os.environ.get(PROFILES_ENV)
    if value:
        return Path(value).expanduser().absolute()
    return Path.home() / ".config" / "piceli" / "profiles"


def check_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.endswith("."):
        raise ProfileError(
            "a profile name is lowercase letters, digits, '.', '_' or '-' "
            "(at most 63 characters, starting with a letter or digit)",
            code="profile-invalid",
        )
    return name


def _path(name: str) -> Path:
    return profiles_dir() / f"{check_name(name)}.json"


def save_profile(name: str, kubeconfig: Path, context: str | None = None) -> Profile:
    """Store a profile after checking the file exists and names the context.

    Without ``context`` the kubeconfig must define exactly one (never its
    ``current-context``).
    """
    from piceli.k8s.ops.provider_factory import _named, _read_kubeconfig

    check_name(name)
    path = Path(kubeconfig).expanduser().absolute()
    if not path.is_file():
        raise ProfileError("the kubeconfig file does not exist", code="profile-invalid")
    try:
        document = _read_kubeconfig(path)
        names = sorted(
            str(item["name"])
            for item in document.get("contexts") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        if context is None:
            if len(names) != 1:
                raise ProfileError(
                    "the kubeconfig defines several contexts (or none): pass "
                    f"--context; available: {names}",
                    code="profile-invalid",
                )
            context = names[0]
        _named(document, "contexts", context)
    except ProfileError:
        raise
    except ProviderFactoryError as error:
        raise ProfileError(str(error), code="profile-invalid") from None
    directory = profiles_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    body = {"schema": _SCHEMA, "kubeconfig": str(path), "context": context}
    descriptor, staged = tempfile.mkstemp(dir=directory, prefix=".piceli-profile-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(body, sort_keys=True) + "\n")
        os.chmod(staged, 0o600)
        os.replace(staged, _path(name))
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return Profile(name, path, context)


def load_profile(name: str) -> Profile:
    """The stored profile ``name`` (a registered error when missing or damaged)."""
    path = _path(name)
    try:
        body = json.loads(path.read_text())
    except FileNotFoundError:
        raise ProfileError(
            f"no credential profile named {name!r}; run "
            f"`piceli login {name} --kubeconfig FILE --context CTX`",
            code="profile-not-found",
        ) from None
    except (OSError, ValueError):
        raise ProfileError(
            f"profile {name!r} is unreadable", code="profile-invalid"
        ) from None
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("kubeconfig"), str)
        or not isinstance(body.get("context"), str)
        or not body["context"]
    ):
        raise ProfileError(f"profile {name!r} is malformed", code="profile-invalid")
    return Profile(name, Path(body["kubeconfig"]), body["context"])


def list_profiles() -> list[Profile]:
    """Every readable profile, sorted by name (a damaged file is skipped)."""
    directory = profiles_dir()
    if not directory.is_dir():
        return []
    found: list[Profile] = []
    for item in sorted(directory.glob("*.json")):
        try:
            found.append(load_profile(item.stem))
        except ProfileError:
            continue
    return found


def remove_profile(name: str) -> bool:
    """Delete the stored reference (never the kubeconfig); False when absent."""
    path = _path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _service_account_dir() -> Path:
    return Path(os.environ.get(SERVICE_ACCOUNT_ENV) or DEFAULT_SERVICE_ACCOUNT)


def in_cluster() -> bool:
    """Whether this process runs as a pod: the flag, or a mounted token."""
    if os.environ.get(IN_CLUSTER_ENV) == "1":
        return True
    return (_service_account_dir() / "token").is_file()


_incluster_config: Path | None = None


def _in_cluster_credentials() -> ResolvedCredentials:
    """The pod's service account as an explicit kubeconfig (file references only)."""
    global _incluster_config
    if _incluster_config is not None and _incluster_config.is_file():
        return ResolvedCredentials(_incluster_config, IN_CLUSTER_CONTEXT, True)
    from piceli import tempfiles
    from piceli.server.incluster import InClusterCredential

    directory = _service_account_dir()
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    try:
        namespace = (directory / "namespace").read_text().strip()
        if ":" in host:
            host = f"[{host}]"
        credential = InClusterCredential(
            f"https://{host}:{port}",
            (directory / "ca.crt").absolute(),
            (directory / "token").absolute(),
            namespace,
        )
        work = tempfiles.make_directory("incluster")
        credential.write_kubeconfig(work / "config")
    except (OSError, ValueError):
        raise ProfileError(
            "the in-cluster service account is unavailable "
            "(needs KUBERNETES_SERVICE_HOST and the mounted token, CA and namespace)",
            code="profile-invalid",
        ) from None
    _incluster_config = work / "config"
    return ResolvedCredentials(_incluster_config, IN_CLUSTER_CONTEXT, True)


_override: str | None = None


def set_override(name: str | None) -> None:
    """Use profile ``name`` instead of the credentials a pipeline or spec declares.

    Set by ``--profile NAME`` on commands that read their target from a file;
    ``None`` clears it.
    """
    global _override
    _override = None if name is None else check_name(name)


def override() -> str | None:
    return _override


def resolve(name: str) -> ResolvedCredentials:
    """The kubeconfig and context ``name`` stands for, now.

    In a cluster the pod's service account; elsewhere the stored profile.
    """
    check_name(name)
    if in_cluster():
        return _in_cluster_credentials()
    stored = load_profile(name)
    return ResolvedCredentials(stored.kubeconfig, stored.context)
