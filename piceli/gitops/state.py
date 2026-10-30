"""The GitOps controller's state, its published status and its request inbox.

Three documents, none of them holding a secret:

- **state** (``<state_dir>/state.json``, private to the controller, on its
  volume): per branch the wanted and deployed commit, the step state, the
  pending plan hash, attempts and the next retry; the tags already seen.
- **status** (``piceli.gitops-status.v1``): the part of the state others
  read. In the cluster it is the ConfigMap ``piceli-gitops-status`` (key
  ``status.json``) in the controller's namespace; ``piceli gitops status``
  and ``piceli envs`` read it (:func:`read_status`).
- **history** (``piceli.gitops-history.v1``, :mod:`piceli.gitops.history`):
  each environment's recent deploy runs, bounded, for the in-cluster UI.
  In the cluster it is the ConfigMap ``piceli-gitops-history`` (key
  ``history.json``); locally ``history.json`` in the state directory.
- **requests**: what people ask the controller to do: approve a plan hash
  (``piceli gitops approve``) or promote a commit to main (``piceli
  promote``). In the cluster it is the
  ConfigMap ``piceli-gitops-requests``, one key per request; the controller
  removes a request once it handled it.

Locally (``piceli gitops run --once --state-dir DIR`` without a namespace)
both live as files in the state directory (:class:`DirectoryChannel`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from piceli.gitops import GitOpsError

STATE_SCHEMA = "piceli.gitops-state.v1"
STATUS_SCHEMA = "piceli.gitops-status.v1"
REQUEST_SCHEMA = "piceli.gitops-request.v1"
STATUS_CONFIGMAP = "piceli-gitops-status"
REQUESTS_CONFIGMAP = "piceli-gitops-requests"
STATUS_KEY = "status.json"
HISTORY_CONFIGMAP = "piceli-gitops-history"
HISTORY_KEY = "history.json"
#: Who sent an approval (``via`` of an approve request; recorded as its approver).
APPROVAL_VIA = ("cli", "ui")
#: Request kinds (the prefix of their key).
REQUEST_KINDS = (
    "approve", "promote", "sync", "stop", "start", "cache-clear",
    # 0.21.0: build hosts (piceli builder serve)
    "build-claim", "built", "build-failed",
    # 0.22.0: rebuild what a host built; release a quarantined host
    "rebuild", "host-release",
    # 0.25.0: a build host published a NixRelease
    "nix-published",
    # 0.26.0: a merge train offers images built before they were asked for
    "prebuilt",
    # 0.26.0: start a build by hand, cancel one, a build host's progress
    "build", "build-cancel", "build-progress",
    # 0.28.0: piceli env up|down BRANCH (a preview, now)
    "preview",
    # 0.29.0: a build host waits to take the next build (--take-next)
    "build-standby",
    # 0.33.0: a build host says its Piceli version (it differs from the
    # controller's, or from the one the controller last saw)
    "build-host-version",
)  # fmt: skip
#: Env states a status may carry.
ENV_STATES = (
    "pending",
    "retrying",
    "approval-required",
    "deployed",
    "failed",
    "deleting",
    "stopped",
)
_KEEP_REJECTED = 20


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise GitOpsError(
            "gitops-state-invalid", f"cannot read {path.name} in the state directory"
        ) from None


def new_state() -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "envs": {},
        "tags": {},
        "baseline": False,
        "rejected": [],
        "last_poll": None,
        "last_error": None,
        "poll_failures": 0,
    }


def load_state(state_dir: Path) -> dict[str, Any]:
    document = read_json(state_dir / "state.json")
    if document is None:
        return new_state()
    if document.get("schema") != STATE_SCHEMA:
        raise GitOpsError("gitops-state-invalid", "unknown controller state schema")
    merged = new_state()
    merged.update(document)
    return merged


def save_state(state_dir: Path, state: Mapping[str, Any]) -> None:
    write_json(state_dir / "state.json", state)


@contextmanager
def controller_lock(state_dir: Path) -> Iterator[None]:
    """One controller per state directory (an OS lock the kernel releases)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "controller.lock", "a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise GitOpsError(
                "gitops-controller-locked",
                "another controller process holds this state directory",
            ) from None
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


_NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


def remove_env_dir(directory: Path, namespace: Any) -> bool:
    """Remove ``directory/<namespace>`` (a branch environment's local state).

    Only a DNS-label ``namespace`` names a child (never a path); ``True``
    when something was removed.
    """
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        return False
    target = directory / namespace
    if not target.is_dir() or target.is_symlink():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def sweep_env_dirs(
    directory: Path,
    keep: set[str],
    live: Callable[[str], bool | None] | None,
) -> list[str]:
    """Remove the local state of branch environments nobody knows any more.

    A child ``directory/<namespace>`` stays when ``namespace`` is in ``keep``
    (an environment the controller still has), or unless ``live`` answers
    ``False`` for it: a live environment's state, or one whose namespace
    cannot be looked up, is never deleted. Returns the removed namespaces.
    """
    if live is None or not directory.is_dir():
        return []
    removed = []
    for child in sorted(directory.iterdir()):
        name = child.name
        if name in keep or not child.is_dir() or not _NAMESPACE.fullmatch(name):
            continue
        try:
            answer = live(name)
        except Exception:  # unknown: keep it
            answer = None
        if answer is False and remove_env_dir(directory, name):
            removed.append(name)
    return removed


def remember_rejected(state: dict[str, Any], entry: Mapping[str, Any]) -> None:
    rejected = list(state.get("rejected") or [])
    rejected.append(dict(entry))
    state["rejected"] = rejected[-_KEEP_REJECTED:]


# ---------------------------------------------------------------- requests


def request(kind: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    """A request document and its key (``<kind>.<digest>``).

    ``approve``: ``env``, ``plan_hash``. ``promote``: ``branch``, ``commit``
    and, for a named environment, ``env``. ``sync``: optional ``env`` and
    ``component``. ``stop`` / ``start``: ``env``, ``via`` and ``at``.
    """
    if kind not in REQUEST_KINDS:
        raise GitOpsError("gitops-request-invalid", f"unknown request kind {kind!r}")
    body = {"schema": REQUEST_SCHEMA, "kind": kind, **fields}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    return f"{kind}.{digest}", body


def approve_request(
    env: str, plan_hash: str, via: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Approve ``plan_hash`` for ``env``; ``via`` (``cli`` or ``ui``) is recorded
    by the controller as the approval's origin in the environment's history."""
    if not plan_hash.startswith("sha256:") or len(plan_hash) != 71:
        raise GitOpsError(
            "gitops-request-invalid", "the plan hash must be sha256:<64 hex>"
        )
    if via is None:
        return request("approve", env=env, plan_hash=plan_hash)
    if via not in APPROVAL_VIA:
        raise GitOpsError("gitops-request-invalid", "unknown approval origin")
    return request("approve", env=env, plan_hash=plan_hash, via=via)


def promote_request(target: str, env: str | None = None) -> tuple[str, dict[str, Any]]:
    """``branch@sha`` → a promotion request (to main, or to the environment ``env``)."""
    branch, sep, commit = target.rpartition("@")
    if (
        not sep
        or not branch
        or not 7 <= len(commit) <= 40
        or any(ch not in "0123456789abcdef" for ch in commit)
    ):
        raise GitOpsError(
            "gitops-request-invalid",
            "promote takes BRANCH@SHA (a hex commit id of 7 to 40 characters)",
        )
    if env is None:
        return request("promote", branch=branch, commit=commit)
    if (
        not env
        or len(env) > 63
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in env)
    ):
        raise GitOpsError(
            "gitops-request-invalid", "the environment name is not a DNS label"
        )
    return request("promote", branch=branch, commit=commit, env=env)


def sync_request(
    env: str | None = None, component: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Deploy ``env`` (every environment when ``None``) at its revision now.

    ``component`` (a composition only) also rebuilds that component even
    when its source digest has an image already. The body is ``{schema,
    kind: "sync", env?, component?}`` under the key ``sync.<digest>`` (the
    same request twice is one request until the controller handles it); the
    in-cluster UI writes the same shape through this function.
    """
    for value, what in ((env, "environment"), (component, "component")):
        if value is not None and (
            not value
            or len(value) > 63
            or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in value)
        ):
            raise GitOpsError(
                "gitops-request-invalid", f"the {what} is not a DNS label"
            )
    fields: dict[str, Any] = {}
    if env is not None:
        fields["env"] = env
    if component is not None:
        fields["component"] = component
    return request("sync", **fields)


def cache_clear_request(
    crates: list[str] | tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    """Empty the cluster build cache's cargo targets, or only ``crates``'
    artifacts (``piceli gitops cache clear``, 0.19.0)."""
    import re
    from datetime import UTC, datetime

    names = sorted(set(crates))
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names):
        raise GitOpsError(
            "gitops-request-invalid", "a crate name is letters, digits, - and _"
        )
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return request("cache-clear", crates=names, at=at)


def _build_id(value: str) -> str:
    import re

    if not re.fullmatch(r"[0-9a-f]{20}", value):
        raise GitOpsError("gitops-request-invalid", "not a wanted build's id")
    return value


def build_claim_request(
    build: str, host: str, proof: str | None = None, recipient: str | None = None
) -> tuple[str, dict[str, Any]]:
    """A build host takes the wanted build ``build`` (``piceli builder serve``).

    ``proof`` (0.25.0, a ``nix-publish``): the cache key's signature over
    the claim (:func:`piceli.infra.nix_releases.claim_message`), never a key.
    ``recipient`` (0.26.0): the one-claim public key the controller seals
    its Git credential to, for a ``credentials="controller"`` release.
    0.28.0: ``version``, the host's Piceli version (``piceli builder status``)."""
    if proof is None:
        return request(
            "build-claim", id=_build_id(build), host=str(host)[:64],
            version=_piceli_version(),
        )  # fmt: skip
    extra = {"recipient": str(recipient)[:64]} if recipient else {}
    return request(
        "build-claim", id=_build_id(build), host=str(host)[:64],
        proof=str(proof)[:200], version=_piceli_version(), **extra,
    )  # fmt: skip


def _piceli_version() -> str:
    from piceli import __version__

    return str(__version__)[:40]


def nix_published_request(
    build: str,
    host: str,
    manifest: Mapping[str, Any],
    pointers: Sequence[Mapping[str, Any]],
    receipt: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """The wanted publish ``build`` is in the cache (0.25.0): the signed
    manifest, the pointers written (ref, commit, ``published_at``) and a
    receipt (host, stages with their times)."""
    return request(
        "nix-published",
        id=_build_id(build),
        host=str(host)[:64],
        manifest=dict(manifest),
        pointers=[dict(item) for item in pointers],
        receipt=dict(receipt),
    )


def built_request(
    build: str,
    host: str,
    images: Mapping[str, Mapping[str, Any]],
    receipt: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """The wanted build ``build`` is delivered: each image's key, pull
    reference, digest and platforms, and a short receipt (host, times,
    toolchain, commits)."""
    return request(
        "built",
        id=_build_id(build),
        host=str(host)[:64],
        images={name: dict(item) for name, item in images.items()},
        receipt=dict(receipt),
    )


def prebuilt_request(
    host: str,
    images: Mapping[str, Mapping[str, Any]],
    receipt: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Images built and pushed before the controller asked (0.26.0, a merge
    train's ``Step.images``): each component's key (the controller's change
    key), source, commit, pull reference, digest and platforms, and a
    receipt (host, times, builder image, commits, the train run)."""
    return request(
        "prebuilt",
        host=str(host)[:64],
        images={name: dict(item) for name, item in images.items()},
        receipt=dict(receipt),
    )


def build_failed_request(
    build: str,
    host: str,
    reason: str,
    log_tail: str,
    *,
    early: bool = False,
    source: str | None = None,
    stage: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """The wanted build ``build`` failed on ``host`` (its output's tail).

    0.29.0: ``early``, it failed before the build's first command (a source
    it cannot fetch, named by ``source``, or an engine that cannot start the
    build): the controller hands the build to another host or its fallback.
    0.41.0: ``stage``, the host's stage when it failed (``fetch``, ``build``,
    ``push``)."""
    extra: dict[str, Any] = {}
    if early:
        extra["early"] = True
        if source:
            extra["source"] = str(source)[:100]
    if stage:
        extra["stage"] = str(stage)[:16]
    return request(
        "build-failed",
        id=_build_id(build),
        host=str(host)[:64],
        reason=str(reason)[:300],
        log_tail=str(log_tail)[-8000:],
        **extra,
    )


def build_standby_request(host: str, seconds: float) -> tuple[str, dict[str, Any]]:
    """``host`` waits to take the next wanted build for ``seconds`` (0.29.0,
    ``piceli builder serve --once --take-next``; ``0`` ends it)."""
    return request(
        "build-standby",
        host=str(host)[:64],
        seconds=float(seconds),
        version=_piceli_version(),
        at=_now(),
    )


def build_host_version_request(
    host: str, version: str | None = None
) -> tuple[str, dict[str, Any]]:
    """``host`` says which Piceli version it runs (0.33.0, ``piceli builder
    serve``): sent when it differs from the controller's (the host then
    claims nothing and exits 75) or from the one the controller last saw,
    so ``controller.build_hosts`` and the ``build-host-version`` alarm show
    the version it really runs. ``version``: the running one by default."""
    return request(
        "build-host-version",
        host=str(host)[:64],
        version=str(version)[:40] if version else _piceli_version(),
        at=_now(),
    )


def _host_name(value: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9](?:[-a-z0-9.]{0,61}[a-z0-9])?", value
    ):
        raise GitOpsError(
            "gitops-request-invalid", "a build host or node name is a DNS name"
        )
    return value


def rebuild_request(host: str, since: str | None = None) -> tuple[str, dict[str, Any]]:
    """Rebuild every deployed image ``host`` (a build host or the builder
    node) built, since the ISO time ``since`` (0.22.0, ``piceli gitops
    rebuild``). The controller drops their cached build results and deploys
    each environment that runs one, with its usual approval."""
    from datetime import UTC, datetime

    from piceli.infra.provenance import parse_since

    fields: dict[str, Any] = {"host": _host_name(host)}
    if since is not None:
        try:
            parse_since(since)
        except ValueError as error:
            raise GitOpsError("gitops-request-invalid", str(error)) from None
        fields["since"] = since
    fields["at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return request("rebuild", **fields)


def _label(value: Any, what: str) -> str:
    import re

    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value
    ):
        raise GitOpsError("gitops-request-invalid", f"the {what} is not a DNS label")
    return value


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_request(
    name: str,
    ref: str | None = None,
    *,
    via: str = "cli",
    by: str | None = None,
    approve: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Start a build of ``name`` by hand (0.26.0, ``piceli gitops build``,
    the UI's "Build now"): a NixRelease plans ``ref`` (a tag of it, its
    branch or a full commit; the newest without one), an environment
    rebuilds its images. Either goes through the gate, plan and approval;
    ``approve`` approves the plan in one go when its digest is that one."""
    from piceli.dev.nix import ref_name_ok

    if ref is not None and not (isinstance(ref, str) and ref_name_ok(ref)):
        raise GitOpsError(
            "gitops-request-invalid", "the ref is not a tag, branch or commit"
        )
    if approve is not None and (
        not approve.startswith("sha256:") or len(approve) != 71
    ):
        raise GitOpsError(
            "gitops-request-invalid", "the plan digest must be sha256:<64 hex>"
        )
    if via not in APPROVAL_VIA:
        raise GitOpsError("gitops-request-invalid", "unknown request origin")
    fields: dict[str, Any] = {"name": _label(name, "target"), "via": via, "at": _now()}
    fields |= {"ref": ref} if ref else {}
    fields |= {"by": str(by)[:120]} if by else {}
    fields |= {"approve": approve} if approve else {}
    return request("build", **fields)


def build_cancel_request(
    name: str, *, via: str = "cli", by: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Cancel ``name``'s queued or running build (0.26.0, ``piceli gitops
    cancel``): unclaimed, it is dropped; claimed, the build host stops at
    its next stage and its answer is refused."""
    if via not in APPROVAL_VIA:
        raise GitOpsError("gitops-request-invalid", "unknown request origin")
    fields: dict[str, Any] = {"name": _label(name, "target"), "via": via, "at": _now()}
    fields |= {"by": str(by)[:120]} if by else {}
    return request("build-cancel", **fields)


def build_progress_request(
    build: str, host: str, stage: str, log_tail: str, at: str
) -> tuple[str, dict[str, Any]]:
    """A build host's stage and log tail while it builds (0.26.0), and
    (0.28.0) its Piceli version."""
    import re

    if not re.fullmatch(r"[a-z]{1,16}", stage):
        raise GitOpsError("gitops-request-invalid", "not a build stage")
    return request(
        "build-progress",
        id=_build_id(build),
        host=str(host)[:64],
        stage=stage,
        log_tail=str(log_tail)[-4000:],
        at=str(at)[:40],
        version=_piceli_version(),
    )


def host_release_request(host: str) -> tuple[str, dict[str, Any]]:
    """Forget the health events the controller recorded for ``host`` (a build
    host or the builder node): it is no longer quarantined (0.22.0)."""
    from datetime import UTC, datetime

    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return request("host-release", host=_host_name(host), at=at)


def stop_request(
    env: str, *, start: bool = False, via: str = "cli", at: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Stop (or start) the named environment ``env`` (``piceli env stop|start``).

    ``at`` (an ISO time with microseconds, now by default) orders a stop and
    a start that wait together: the controller handles them oldest first, so
    the later one wins.
    """
    if (
        not isinstance(env, str)
        or not env
        or len(env) > 63
        or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in env)
    ):
        raise GitOpsError(
            "gitops-request-invalid", "the environment name is not a DNS label"
        )
    if via not in APPROVAL_VIA:
        raise GitOpsError("gitops-request-invalid", "unknown request origin")
    if at is None:
        from datetime import UTC, datetime

        at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return request("start" if start else "stop", env=env, via=via, at=at)


def preview_request(
    branch: str, *, action: str = "up", at: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Deploy (``up``) or tear down (``down``) the preview of ``branch`` now
    (``piceli env up|down BRANCH``, 0.28.0).

    ``at`` (now by default, with microseconds) makes each request its own
    key: ``env up --wait`` follows that key in the controller's status.
    """
    from piceli.infra.previews import check_branch

    check_branch(branch)
    if action not in ("up", "down"):
        raise GitOpsError("gitops-request-invalid", "a preview is up or down")
    if at is None:
        from datetime import UTC, datetime

        at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    nonce = os.urandom(4).hex()  # two requests in one microsecond stay two
    return request("preview", branch=branch, action=action, at=at, nonce=nonce)


class Channel(Protocol):
    """Where the status is published and requests arrive."""

    def requests(self) -> dict[str, dict[str, Any]]: ...

    def add_request(self, key: str, body: Mapping[str, Any]) -> None: ...

    def remove_requests(self, keys: list[str]) -> None: ...

    def publish(self, status: Mapping[str, Any]) -> None: ...

    def read_status(self) -> dict[str, Any] | None: ...


class DirectoryChannel:
    """Status and requests as files in a directory (local runs, tests)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @contextmanager
    def _locked(self) -> Iterator[Path]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / "requests.lock", "a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield self.directory / "requests.json"
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def requests(self) -> dict[str, dict[str, Any]]:
        with self._locked() as path:
            return dict(read_json(path) or {})

    def add_request(self, key: str, body: Mapping[str, Any]) -> None:
        with self._locked() as path:
            document = dict(read_json(path) or {})
            document[key] = dict(body)
            write_json(path, document)

    def remove_requests(self, keys: list[str]) -> None:
        with self._locked() as path:
            document = dict(read_json(path) or {})
            for key in keys:
                document.pop(key, None)
            write_json(path, document)

    def publish(self, status: Mapping[str, Any]) -> None:
        write_json(self.directory / "status.json", status)

    def read_status(self) -> dict[str, Any] | None:
        value = read_json(self.directory / "status.json")
        return dict(value) if isinstance(value, Mapping) else None

    def publish_heartbeat(self, at: str) -> None:
        """Set only ``controller.heartbeat_at`` of the published status."""
        status = self.read_status()
        if status is not None:
            write_json(self.directory / "status.json", with_heartbeat(status, at))

    def publish_history(self, history: Mapping[str, Any]) -> None:
        write_json(self.directory / HISTORY_KEY, history)

    def read_history(self) -> dict[str, Any] | None:
        value = read_json(self.directory / HISTORY_KEY)
        return dict(value) if isinstance(value, Mapping) else None


class ConfigMapChannel:
    """Status and requests as ConfigMaps in the controller's namespace.

    :param client: A Kubernetes ``ApiClient`` (explicit kubeconfig).
    :param namespace: The controller's namespace.
    """

    def __init__(
        self, client: Any, namespace: str, *, request_seconds: float = 10.0
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.request_seconds = request_seconds

    def _path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self.namespace}/configmaps"
        return f"{base}/{name}" if name else base

    def _call(
        self,
        path: str,
        method: str,
        body: Any = None,
        content: str = "application/json",
    ) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            response = self.client.call_api(
                path,
                method,
                query_params=(
                    [("fieldManager", "piceli-gitops")]
                    if method in {"POST", "PATCH"}
                    else []
                ),
                header_params={"Accept": "application/json", "Content-Type": content},
                body=body,
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            if error.status == 409:
                raise _Conflict() from None
            raise GitOpsError(
                "gitops-cluster-failed",
                f"the API refused a ConfigMap {method} (HTTP {error.status})",
            ) from None
        except OSError:
            raise GitOpsError(
                "gitops-cluster-failed", "the API server is unreachable"
            ) from None
        raw = response[0] if isinstance(response, tuple) else response
        return json.loads(raw.data or b"null")

    def _read(self, name: str) -> dict[str, Any] | None:
        value = self._call(self._path(name), "GET")
        return value if isinstance(value, dict) else None

    def _update(self, name: str, change: Any) -> None:
        """Read-modify-write one ConfigMap's ``data`` (retried on conflict)."""
        for _ in range(5):
            current = self._read(name)
            data = dict((current or {}).get("data") or {})
            new = change(data)
            if new == data and current is not None:
                return
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": name,
                    "namespace": self.namespace,
                    "labels": {
                        "app.kubernetes.io/managed-by": "piceli",
                        "piceli.io/component": "gitops",
                    },
                },
                "data": new,
            }
            try:
                if current is None:
                    self._call(self._path(), "POST", body)
                else:
                    body["metadata"]["uid"] = current["metadata"]["uid"]
                    body["metadata"]["resourceVersion"] = current["metadata"][
                        "resourceVersion"
                    ]
                    removed = {key: None for key in data if key not in new}
                    body["data"] = {**removed, **new}
                    self._call(
                        self._path(name), "PATCH", body, "application/merge-patch+json"
                    )
                return
            except _Conflict:
                continue
        raise GitOpsError("gitops-cluster-failed", f"ConfigMap {name} kept changing")

    def requests(self) -> dict[str, dict[str, Any]]:
        current = self._read(REQUESTS_CONFIGMAP)
        found: dict[str, dict[str, Any]] = {}
        for key, text in ((current or {}).get("data") or {}).items():
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            found[key] = value if isinstance(value, dict) else {"kind": "invalid"}
        return found

    def add_request(self, key: str, body: Mapping[str, Any]) -> None:
        text = json.dumps(dict(body), sort_keys=True)
        self._update(REQUESTS_CONFIGMAP, lambda data: {**data, key: text})

    def remove_requests(self, keys: list[str]) -> None:
        self._update(
            REQUESTS_CONFIGMAP,
            lambda data: {k: v for k, v in data.items() if k not in keys},
        )

    def publish(self, status: Mapping[str, Any]) -> None:
        text = json.dumps(dict(status), sort_keys=True)
        self._update(STATUS_CONFIGMAP, lambda data: {STATUS_KEY: text})

    def read_status(self) -> dict[str, Any] | None:
        return self._document(STATUS_CONFIGMAP, STATUS_KEY)

    def publish_heartbeat(self, at: str) -> None:
        """Set only ``controller.heartbeat_at`` of the published status
        (a read-modify-write of the one key; nothing when there is none)."""

        def change(data: dict[str, Any]) -> dict[str, Any]:
            try:
                status = json.loads(data.get(STATUS_KEY) or "null")
            except ValueError:
                return data
            if not isinstance(status, dict):
                return data
            text = json.dumps(with_heartbeat(status, at), sort_keys=True)
            return {**data, STATUS_KEY: text}

        if self._read(STATUS_CONFIGMAP) is not None:
            self._update(STATUS_CONFIGMAP, change)

    def publish_history(self, history: Mapping[str, Any]) -> None:
        text = json.dumps(dict(history), sort_keys=True)
        self._update(HISTORY_CONFIGMAP, lambda data: {HISTORY_KEY: text})

    def read_history(self) -> dict[str, Any] | None:
        return self._document(HISTORY_CONFIGMAP, HISTORY_KEY)

    def _document(self, name: str, key: str) -> dict[str, Any] | None:
        current = self._read(name)
        text = ((current or {}).get("data") or {}).get(key)
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


class _Conflict(Exception):
    pass


def with_heartbeat(status: Mapping[str, Any], at: str) -> dict[str, Any]:
    """``status`` with ``controller.heartbeat_at`` set to ``at`` (0.16.0)."""
    document = dict(status)
    controller = dict(document.get("controller") or {})
    controller["heartbeat_at"] = at
    document["controller"] = controller
    return document


def read_status(client: Any, namespace: str = "piceli-system") -> dict[str, Any] | None:
    """The controller's published status (``piceli.gitops-status.v1``) or ``None``.

    For ``piceli envs``: ``status["envs"][branch]`` holds ``branch``,
    ``commit`` (wanted), ``deployed_commit``, ``state`` (one of
    :data:`ENV_STATES`), ``plan_hash`` (when approval is required),
    ``reason``, ``attempts``, ``next_attempt_at``, ``pushed_at``,
    ``updated_at``, ``namespace`` and ``trigger``. ``status["controller"]``
    has ``heartbeat_at`` (0.16.0; see :mod:`piceli.gitops.heartbeat`).

    The schema changes only additively within v1: fields are added, never
    removed or renamed.
    """
    return ConfigMapChannel(client, namespace).read_status()


def env_status(status: Mapping[str, Any] | None, branch: str) -> dict[str, Any] | None:
    """One branch's entry of a status document, or ``None``."""
    if not status:
        return None
    value = (status.get("envs") or {}).get(branch)
    return dict(value) if isinstance(value, Mapping) else None
