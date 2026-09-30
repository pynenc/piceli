"""The GitOps controller's state, its published status and its request inbox.

Three documents, none of them holding a secret:

- **state** (``<state_dir>/state.json``, private to the controller, on its
  volume): per branch the wanted and deployed commit, the step state, the
  pending plan hash, attempts and the next retry; the tags already seen.
- **status** (``piceli.gitops-status.v1``): the part of the state others
  read. In the cluster it is the ConfigMap ``piceli-gitops-status`` (key
  ``status.json``) in the controller's namespace; ``piceli gitops status``
  and ``piceli envs`` read it (:func:`read_status`).
- **requests**: what people ask the controller to do: approve a plan hash
  (``piceli gitops approve``), promote a commit to main (``piceli promote``),
  use a laptop-built image (``piceli env push``). In the cluster it is the
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
import tempfile
from collections.abc import Iterator, Mapping
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
#: Request kinds (the prefix of their key).
REQUEST_KINDS = ("approve", "promote", "push")
#: Env states a status may carry.
ENV_STATES = (
    "pending",
    "retrying",
    "approval-required",
    "deployed",
    "failed",
    "deleting",
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
        "pushes": {},
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


def remember_rejected(state: dict[str, Any], entry: Mapping[str, Any]) -> None:
    rejected = list(state.get("rejected") or [])
    rejected.append(dict(entry))
    state["rejected"] = rejected[-_KEEP_REJECTED:]


# ---------------------------------------------------------------- requests


def request(kind: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    """A request document and its key (``<kind>.<digest>``).

    ``approve``: ``env``, ``plan_hash``. ``promote``: ``branch``, ``commit``.
    ``push``: ``branch``, ``commit`` and ``digests`` (image name to
    ``sha256:…``) or ``receipt`` (a build receipt).
    """
    if kind not in REQUEST_KINDS:
        raise GitOpsError("gitops-request-invalid", f"unknown request kind {kind!r}")
    body = {"schema": REQUEST_SCHEMA, "kind": kind, **fields}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    return f"{kind}.{digest}", body


def approve_request(env: str, plan_hash: str) -> tuple[str, dict[str, Any]]:
    if not plan_hash.startswith("sha256:") or len(plan_hash) != 71:
        raise GitOpsError(
            "gitops-request-invalid", "the plan hash must be sha256:<64 hex>"
        )
    return request("approve", env=env, plan_hash=plan_hash)


def promote_request(target: str) -> tuple[str, dict[str, Any]]:
    """``branch@sha`` → a promotion request."""
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
    return request("promote", branch=branch, commit=commit)


def push_request(
    branch: str,
    commit: str,
    *,
    digests: Mapping[str, str] | None = None,
    receipt: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Use images built elsewhere (``piceli env push``) for ``branch@commit``."""
    if (digests is None) == (receipt is None):
        raise GitOpsError(
            "gitops-request-invalid", "a push names digests or a receipt, not both"
        )
    fields: dict[str, Any] = {"branch": branch, "commit": commit}
    if digests is not None:
        fields["digests"] = dict(sorted(digests.items()))
    else:
        fields["receipt"] = dict(receipt or {})
    return request("push", **fields)


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
        current = self._read(STATUS_CONFIGMAP)
        text = ((current or {}).get("data") or {}).get(STATUS_KEY)
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None


class _Conflict(Exception):
    pass


def read_status(client: Any, namespace: str = "piceli-system") -> dict[str, Any] | None:
    """The controller's published status (``piceli.gitops-status.v1``) or ``None``.

    For ``piceli envs``: ``status["envs"][branch]`` holds ``branch``,
    ``commit`` (wanted), ``deployed_commit``, ``state`` (one of
    :data:`ENV_STATES`), ``plan_hash`` (when approval is required),
    ``reason``, ``attempts``, ``next_attempt_at``, ``pushed_at``,
    ``updated_at``, ``namespace`` and ``trigger``.
    """
    return ConfigMapChannel(client, namespace).read_status()


def env_status(status: Mapping[str, Any] | None, branch: str) -> dict[str, Any] | None:
    """One branch's entry of a status document, or ``None``."""
    if not status:
        return None
    value = (status.get("envs") or {}).get(branch)
    return dict(value) if isinstance(value, Mapping) else None
