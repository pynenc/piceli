"""GitOps builds on build hosts (0.21.0): the controller asks, a host builds.

A controller in the cluster cannot reach a laptop, and its in-cluster
builder node may be down. With ``Controller(builds=("hosts", ...))`` the
controller does not run a build Job: it publishes what it waits for
(``builds_wanted`` in its status: the spec or components, every source at
its commit, the platforms, each image's repository and key) and the step
waits (``retrying``, reason ``build-waiting``; no attempt is counted).

``piceli builder serve`` on a build host (:mod:`piceli.infra.builder_serve`)
reads the wanted builds, claims one (request ``build-claim``), builds it with
the same host-build code as the in-cluster Job, delivers the images (and one
OCI index per image when there are several platforms) and answers with a
``built`` request (or ``build-failed`` with its log tail). The controller
checks that the request matches a wanted build (its id, every image's key)
and that each digest is in its registry, then records the images exactly
as its own builds: the next step deploys them through the same plan,
approval and checks.

``("hosts", "cluster")`` falls back to the in-cluster Job when no host has
delivered within ``host_wait`` seconds.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from piceli.gitops import GitOpsError
from piceli.infra import CompositionError

#: Where the controller's state keeps the wanted builds.
STATE_KEY = "host_builds"
#: A claim with no answer for this long may be claimed again.
CLAIM_SECONDS = 4 * 3600
WAITING = "build-waiting"


class BuildPending(Exception):
    """The build was handed to the build hosts; the step waits for it."""


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_id(kind: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, **body}, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]


class HostBuilds:
    """A builder that hands builds to the build hosts (see the module doc).

    :param inner: The in-cluster builder (also the mirror), for the
        fallback and for third-party mirrors.
    :param order: ``("hosts",)`` or ``("hosts", "cluster")``.
    """

    def __init__(
        self,
        inner: Any,
        state: dict[str, Any],
        *,
        platforms: Sequence[str],
        order: Sequence[str] = ("hosts",),
        host_wait: float = 1800.0,
        clock: Callable[[], float],
        urls: Mapping[str, str] | None = None,
    ) -> None:
        self.urls = dict(urls or {})
        self.inner = inner
        self.state = state
        self.platforms = tuple(platforms)
        self.order = tuple(order)
        self.host_wait = host_wait
        self.clock = clock

    # The controller labels the in-cluster Jobs through these.
    @property
    def environment(self) -> Any:
        return getattr(self.inner, "environment", None)

    @environment.setter
    def environment(self, value: Any) -> None:
        if hasattr(self.inner, "environment"):
            self.inner.environment = value

    @property
    def attempt(self) -> Any:
        return getattr(self.inner, "attempt", 1)

    @attempt.setter
    def attempt(self, value: Any) -> None:
        if hasattr(self.inner, "attempt"):
            self.inner.attempt = value

    def mirror(self, items: Any) -> Any:
        return self.inner.mirror(items)

    def forget(self, environment: str) -> list[str]:
        forget = getattr(self.inner, "forget", None)
        return forget(environment) if callable(forget) else []

    def _wanted(self) -> dict[str, Any]:
        return self.state.setdefault(STATE_KEY, {})

    def _ask(self, kind: str, body: dict[str, Any], names: list[str]) -> dict[str, Any]:
        """The wanted entry for this build (new, or the one already asked);
        raises the failure a host reported, or returns for the fallback."""
        wanted = self._wanted()
        found = build_id(kind, {**body, "platforms": list(self.platforms)})
        entry = wanted.get(found)
        now = self.clock()
        if entry is None:
            entry = wanted[found] = {
                "id": found,
                "kind": kind,
                **body,
                "platforms": list(self.platforms),
                "images": names,
                "requested_at": _iso(now),
                "requested_epoch": now,
            }
        failure = entry.get("failed")
        if failure:
            del wanted[found]
            error = CompositionError(
                "component-build-failed",
                f"build host {failure.get('host')}: {failure.get('reason')}",
                details={"outcome": {"log_tail": str(failure.get("log_tail") or "")}},
            )
            error.output_tail = str(failure.get("log_tail") or "")  # type: ignore[attr-defined]
            raise error
        return entry

    def _fallback_due(self, entry: Mapping[str, Any], method: str) -> bool:
        return (
            "cluster" in self.order
            and callable(getattr(self.inner, method, None))
            and self.clock() - float(entry.get("requested_epoch") or 0) > self.host_wait
            and not entry.get("claimed")
        )

    def build_spec(self, request: Any, checkout: Any) -> dict[str, Any]:
        body = {
            "spec": request.spec,
            "repo": request.repo,
            "commits": dict(request.commits),
            "urls": dict(request.urls),
            "facts": request.facts,
            "keys": {name: dict(item) for name, item in request.images.items()},
        }
        entry = self._ask("spec", body, sorted(request.images))
        if self._fallback_due(entry, "build_spec"):
            del self._wanted()[entry["id"]]
            return self.inner.build_spec(request, checkout)
        raise BuildPending(
            f"waiting for a build host to build {', '.join(sorted(request.images))}"
        )

    def build(self, items: Sequence[Any], checkout: Any) -> dict[str, Any]:
        body = {
            "components": [
                {
                    "component": item.component,
                    "source": item.source,
                    "commit": item.commit,
                    "digest": item.digest,
                    "repository": item.repository,
                }
                for item in items
            ],
            "urls": {item.source: self.urls.get(item.source, "") for item in items},
        }
        entry = self._ask("components", body, sorted(item.component for item in items))
        if self._fallback_due(entry, "build"):
            del self._wanted()[entry["id"]]
            return dict(self.inner.build(items, checkout))
        raise BuildPending(
            "waiting for a build host to build "
            + ", ".join(sorted(item.component for item in items))
        )


def public(state: Mapping[str, Any], now: float) -> list[dict[str, Any]]:
    """``builds_wanted`` in the controller's status (oldest first)."""
    found = []
    for entry in sorted(
        (state.get(STATE_KEY) or {}).values(),
        key=lambda item: float(item.get("requested_epoch") or 0),
    ):
        claimed = entry.get("claimed") or {}
        if claimed and now - float(claimed.get("epoch") or 0) > CLAIM_SECONDS:
            claimed = {}
        found.append(
            {
                key: value
                for key, value in entry.items()
                if key not in {"requested_epoch", "failed"}
            }
            | {"claimed": {k: v for k, v in claimed.items() if k != "epoch"} or None}
        )
    return found


def claim(state: dict[str, Any], body: Mapping[str, Any], now: float) -> None:
    """A host takes a wanted build (``build-claim``).

    :raises GitOpsError: ``gitops-request-invalid`` (no such build, or
        another host holds a live claim).
    """
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    held = entry.get("claimed") or {}
    host = str(body.get("host") or "")
    if (
        held
        and held.get("host") != host
        and now - float(held.get("epoch") or 0) < CLAIM_SECONDS
    ):
        raise GitOpsError(
            "gitops-request-invalid", f"build host {held.get('host')} already builds it"
        )
    entry["claimed"] = {"host": host, "at": _iso(now), "epoch": now}


def failed(state: dict[str, Any], body: Mapping[str, Any]) -> None:
    """A host could not build it (``build-failed``): the step fails with its tail."""
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    entry["failed"] = {
        "host": str(body.get("host") or "?"),
        "reason": str(body.get("reason") or "the build failed")[:300],
        "log_tail": str(body.get("log_tail") or "")[-8000:],
    }


def delivered(
    state: dict[str, Any],
    body: Mapping[str, Any],
    present: Callable[[str], bool],
) -> dict[str, dict[str, Any]]:
    """Check a ``built`` request against its wanted build; the images to
    record, ``{name: {"key", "pull_ref", "manifest_digest", "platforms"}}``.

    :raises GitOpsError: ``gitops-request-invalid`` (no such build, an
        image it did not ask for or a different key, a digest not in the
        registry).
    """
    wanted = state.get(STATE_KEY) or {}
    entry = wanted.get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    images = body.get("images")
    if not isinstance(images, Mapping) or sorted(images) != sorted(entry["images"]):
        raise GitOpsError(
            "gitops-request-invalid", "the build does not hold the images asked for"
        )
    keys = entry.get("keys") or {
        item["component"]: {"key": item["digest"]}
        for item in entry.get("components") or ()
    }
    found: dict[str, dict[str, Any]] = {}
    for name, image in images.items():
        if not isinstance(image, Mapping):
            raise GitOpsError("gitops-request-invalid", f"image {name}: not an object")
        key = str((keys.get(name) or {}).get("key"))
        if str(image.get("key")) != key:
            raise GitOpsError(
                "gitops-request-invalid", f"image {name}: built from other inputs"
            )
        pull_ref = str(image.get("pull_ref") or "")
        digest = str(image.get("manifest_digest") or "")
        if not digest.startswith("sha256:") or not pull_ref.endswith("@" + digest):
            raise GitOpsError(
                "gitops-request-invalid", f"image {name}: not pinned by its digest"
            )
        if not present(pull_ref):
            raise GitOpsError(
                "gitops-request-invalid",
                f"image {name}: {digest} is not in the registry",
            )
        found[name] = {
            "key": key,
            "pull_ref": pull_ref,
            "manifest_digest": digest,
            "platforms": dict(image.get("platforms") or {}),
        }
    del wanted[entry["id"]]
    return found
