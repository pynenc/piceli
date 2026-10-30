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
delivered within ``host_wait`` seconds, unless the builder node is
quarantined (0.22.0, :mod:`piceli.dev.health`): then the build keeps
waiting for a host. A quarantined host's ``build-claim`` is refused.

A claim lives while its host reports (0.27.1): a host that claimed an image
build and sent no claim or ``build-progress`` for :data:`CLAIM_STALE_SECONDS`
(builders report at each stage and every few seconds while the build logs)
is taken for dead: the fallback may take the build and another host may
claim it. Before, a dead host's claim blocked both for hours. The waiting
step says what it waits for (:meth:`HostBuilds.waiting`): the hosts it is
offered to, who holds it, and when the fallback starts or why it never does.

Hand-over (0.29.0): a host that fails before its build's first command
(it cannot fetch a source, or its container engine cannot start the build)
answers ``build-failed`` with ``early``. The build is not failed: the claim
is dropped, the host is quarantined for that source
(:data:`SOURCE_QUARANTINE_KEY`; its claims of builds that need the source
are refused until ``piceli gitops release-host``) and the build goes to the
next declared host, or to the in-cluster fallback at once when no other
host may take it (within :data:`HANDOVER_SECONDS` when one may). With
``builds=("hosts",)`` and no host left, the step fails with the host's reason.

Standing by (0.29.0, ``piceli builder serve --once --take-next``): a host
that waits to take the next build says so (``build-standby``); while it
stands by, the in-cluster fallback waits for it at most
:data:`STANDBY_SECONDS` past its usual time, so a host started by hand wins
the build instead of racing ``host_wait``.

Delivery into the nodes (0.22.0, ``Controller(delivery="nodes")``): the
wanted build also names every declared node and its platform
(``deliver.nodes``). The host imports each image into each node's
containerd over SSH (``ctr images import``, through the validating relay of
:mod:`piceli.artifacts.delivery`), the platform of the node's architecture,
under one name for every node: ``node.piceli.invalid/<repository>:sha256-<hex>``,
a content tag of the image's config digests (:func:`node_reference`). It
answers with each node's delivery receipt: the reference and the config
digest the node itself reports after the import. The controller checks
that every node it asked for is in the answer, that each node got its own
platform and that each config digest is the one built for it
(:func:`delivered`). The controller cannot reach the nodes' containerd (no
SSH, no node credentials), and the API's ``node.status.images`` carries no
config digest, lags the kubelet's status updates and lists at most 50
images, so the node receipts are the evidence: the import itself verified
the config digest on the node before answering. A node that lacks the
image anyway cannot pull it from anywhere (the ``.invalid`` domain never
resolves): its pod fails to start and the rollout's checks fail.

Prebuilt images (0.26.0, ``Controller(prebuilt=True)``): a merge train
that promoted a commit may build its images before the controller sees the
commit (``Step.images``: built from the tested commit, pushed to this
registry) and offer them with a ``prebuilt`` request. :func:`offered`
checks each offer (a declared component, a reference into the component's
own repository of this registry, pinned by its digest, the digest present)
and keeps it by component and key. When a step later needs a component and
has no cached image, :func:`take_offer` hands it the offer **whose key is
the change key the controller computed itself**; the controller checks the
digest is still in its registry, then records the image as its own build,
with the train's provenance. A build is never asked for then: no host wait,
no Job. An offer under any other key is never used.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from piceli.gitops import GitOpsError
from piceli.infra import CompositionError

#: Where the controller's state keeps the wanted builds.
STATE_KEY = "host_builds"
#: Builds a host reported failed, by id: the same inputs are not offered
#: again until ``piceli gitops sync`` (0.22.0).
FAILED_KEY = "host_builds_failed"
#: Prebuilt offers by component and key (0.26.0).
OFFERS_KEY = "prebuilt"
#: Offers kept per component (the newest).
OFFERS_PER_COMPONENT = 20
_KEY = re.compile(r"[A-Za-z0-9:._-]{1,100}")
#: A claim with no answer for this long may be claimed again.
CLAIM_SECONDS = 4 * 3600
#: 0.27.1: an image build's claim with no report (the claim itself or a
#: ``build-progress``) for this long is dead: the in-cluster fallback may
#: take the build and another host may claim it.
CLAIM_STALE_SECONDS = 600.0
WAITING = "build-waiting"
#: 0.28.0: each build host's last sighting (version, when, last build).
HOSTS_KEY = "build_hosts"
#: Build hosts remembered (the most recently seen).
HOSTS_KEPT = 32
#: 0.29.0: sources a host could not fetch, ``{host: {source: {reason, at,
#: build}}}``: its claims of builds that need them are refused until
#: ``piceli gitops release-host``.
SOURCE_QUARANTINE_KEY = "host_sources_quarantined"
#: 0.29.0: after a host handed a build over, how long the in-cluster
#: fallback leaves it to another declared host that may take it.
HANDOVER_SECONDS = 60.0
#: 0.29.0: hosts standing by for the next build (``build-standby``).
STANDBY_KEY = "build_standby"
#: 0.29.0: a standby lives this long after its last report, and holds the
#: fallback at most this long past its usual time.
STANDBY_SECONDS = 120.0
#: What each answer says about the build it names.
_OUTCOMES = {
    "build-claim": "claimed",
    "built": "built",
    "nix-published": "published",
    "build-failed": "failed",
}
#: The registry domain of images imported into the nodes: ``.invalid``
#: never resolves, so a node without the image never pulls one from elsewhere.
NODE_DOMAIN = "node.piceli.invalid"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class BuildPending(Exception):
    """The build was handed to the build hosts; the step waits for it.

    ``retry_at`` (0.23.0): when the step should look again at the latest,
    for example when ``host_wait`` ends and the in-cluster fallback may
    build it; ``None``: at the next poll."""

    def __init__(self, message: str, retry_at: float | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def node_identity(configs: Mapping[str, str]) -> str:
    """The identity of an image imported into the nodes: its config digest
    for one platform, else the SHA-256 of ``platform=config`` lines (sorted)."""
    if len(configs) == 1:
        return next(iter(configs.values()))
    lines = "\n".join(f"{key}={value}" for key, value in sorted(configs.items()))
    return "sha256:" + hashlib.sha256(lines.encode()).hexdigest()


def node_reference(repository: str, configs: Mapping[str, str]) -> str:
    """The name every node knows an imported image by (a content tag of
    :func:`node_identity`, so it can never name other content)."""
    from piceli.k8s.release_spec import content_tag

    return f"{NODE_DOMAIN}/{repository}:{content_tag(node_identity(configs), 64)}"


def live_claim(entry: Mapping[str, Any], now: float) -> dict[str, Any] | None:
    """The claim on ``entry`` while its host is alive, else ``None``.

    0.27.1: an image build's claim lives :data:`CLAIM_STALE_SECONDS` after
    the host's last report (its claim, or its last ``build-progress``); a
    NixRelease publish's claim keeps the longer :data:`CLAIM_SECONDS`."""
    claimed = entry.get("claimed") or {}
    if not claimed:
        return None
    last = max(
        float(claimed.get("epoch") or 0),
        float((entry.get("progress") or {}).get("epoch") or 0),
    )
    limit = CLAIM_SECONDS if entry.get("kind") == "nix-publish" else CLAIM_STALE_SECONDS
    return dict(claimed) if now - last < limit else None


def sources_of(entry: Mapping[str, Any]) -> dict[str, str]:
    """``{source: commit}`` a wanted build fetches (0.29.0)."""
    if entry.get("kind") == "components":
        return {
            str(item.get("source")): str(item.get("commit"))
            for item in entry.get("components") or ()
            if isinstance(item, Mapping)
        }
    return {str(k): str(v) for k, v in (entry.get("commits") or {}).items()}


def blocked_sources(
    state: Mapping[str, Any], host: str, entry: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The sources of ``entry`` that ``host`` is quarantined for (0.29.0)."""
    held = (state.get(SOURCE_QUARANTINE_KEY) or {}).get(host) or {}
    return {
        source: dict(held[source])
        for source in sorted(sources_of(entry))
        if source in held
    }


def describe_blocked(host: str, blocked: Mapping[str, Mapping[str, Any]]) -> str:
    source, record = next(iter(sorted(blocked.items())))
    return (
        f"build host {host} cannot fetch source {source} (since {record.get('at')}: "
        f"{record.get('reason')}); piceli gitops release-host {host} clears it"
    )


def live_standby(state: Mapping[str, Any], now: float) -> dict[str, dict[str, Any]]:
    """The hosts standing by for the next build (0.29.0), by name."""
    return {
        name: dict(item)
        for name, item in sorted((state.get(STANDBY_KEY) or {}).items())
        if isinstance(item, Mapping) and float(item.get("until") or 0) > now
    }


def standby(state: dict[str, Any], body: Mapping[str, Any], now: float) -> None:
    """``build-standby`` (0.29.0): ``host`` waits to take the next build for
    ``seconds`` (at most :data:`STANDBY_SECONDS`; ``0`` ends it)."""
    host = str(body.get("host") or "")[:64]
    if not host:
        raise GitOpsError("gitops-request-invalid", "a standby names no build host")
    try:
        seconds = min(max(float(body.get("seconds") or 0), 0.0), STANDBY_SECONDS)
    except (TypeError, ValueError):
        raise GitOpsError("gitops-request-invalid", "standby seconds") from None
    found = state.setdefault(STANDBY_KEY, {})
    if seconds <= 0:
        found.pop(host, None)
        return
    found[host] = {
        "since": (found.get(host) or {}).get("since") or _iso(now),
        "until": now + seconds,
        "until_at": _iso(now + seconds),
    }
    for name in [k for k, v in found.items() if float(v.get("until") or 0) <= now]:
        del found[name]


def release_sources(state: dict[str, Any], host: str) -> bool:
    """``piceli gitops release-host``: ``host`` may fetch every source again."""
    return (state.get(SOURCE_QUARANTINE_KEY) or {}).pop(host, None) is not None


def public_source_quarantine(state: Mapping[str, Any]) -> dict[str, Any]:
    """``controller.source_quarantine`` in the status (0.29.0)."""
    return {
        host: {source: dict(item) for source, item in sorted(held.items())}
        for host, held in sorted((state.get(SOURCE_QUARANTINE_KEY) or {}).items())
        if held
    }


def build_id(kind: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, **body}, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]


class HostBuilds:
    """A builder that hands builds to the build hosts (see the module doc).

    :param inner: The in-cluster builder (also the mirror), for the
        fallback and for third-party mirrors.
    :param order: ``("hosts",)`` or ``("hosts", "cluster")``.
    :param nodes: ``{node: platform}`` when the hosts deliver into the
        nodes (0.22.0, ``Controller(delivery="nodes")``).
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
        nodes: Mapping[str, str] | None = None,
        cluster_blocked: Callable[[], str | None] = lambda: None,
        owner: Callable[[], str | None] = lambda: None,
        hosts: Sequence[str] = (),
    ) -> None:
        #: 0.27.1: the declared build hosts that build images (shown in the
        #: waiting step's status: who it is offered to).
        self.hosts = tuple(hosts)
        #: 0.22.0: the environment whose step asks (its key), so a newer
        #: build it asks for replaces the older one.
        self.owner = owner
        #: 0.22.0: why the in-cluster builder may not take the fallback (its
        #: node is quarantined), or ``None``.
        self.cluster_blocked = cluster_blocked
        self.urls = dict(urls or {})
        self.nodes = dict(nodes or {})
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
        if self.nodes:
            missing = sorted(set(self.nodes.values()) - set(self.platforms))
            if missing:
                raise CompositionError(
                    "composition-invalid",
                    f"the nodes run {', '.join(missing)} but the controller builds "
                    f"{', '.join(self.platforms)}: enable it with every node's "
                    "platform (piceli gitops enable --platform ...)",
                )
            body = {**body, "deliver": {"mode": "nodes", "nodes": dict(self.nodes)}}
        found = build_id(kind, {**body, "platforms": list(self.platforms)})
        now = self.clock()
        owner = self.owner()
        failure = (self.state.get(FAILED_KEY) or {}).get(found)
        if failure is not None:
            # 0.22.0: the same inputs failed on a host: not offered again
            # until they change or `piceli gitops sync` retries them.
            raise _failure_error(failure, found)
        if owner is not None:
            # 0.22.0: a newer build of this environment replaces the older
            # one (a new commit, or the environment moved on).
            for key in [
                key
                for key, item in wanted.items()
                if key != found and item.get("owner") == owner
            ]:
                del wanted[key]
        entry = wanted.get(found)
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
        entry["asked_epoch"] = now
        if owner is not None:
            entry["owner"] = owner
        if (
            entry.get("handed_over")
            and "cluster" not in self.order
            and self._eligible(entry) == []
        ):
            # 0.29.0: every declared host handed it over and there is no
            # in-cluster fallback: the step fails with the last host's reason.
            last = dict(entry["handed_over"][-1])
            entry["failed"] = {
                "host": last.get("host"),
                "reason": last.get("reason"),
                "log_tail": last.get("log_tail") or last.get("reason"),
            }
        failure = entry.get("failed")
        if failure:
            del wanted[found]
            failures = self.state.setdefault(FAILED_KEY, {})
            failures[found] = {**failure, "owner": owner, "at": _iso(now)}
            for key in sorted(failures, key=lambda k: str(failures[k].get("at")))[:-50]:
                del failures[key]
            raise _failure_error(failure, found)
        return entry

    def _eligible(self, entry: Mapping[str, Any]) -> list[str] | None:
        """The declared hosts that may still take ``entry`` (0.29.0): not
        handed over by them, not quarantined for one of its sources;
        ``None`` when the composition declares no build host (any may)."""
        if not self.hosts:
            return None
        handed = {str(item.get("host")) for item in entry.get("handed_over") or ()}
        return [
            host
            for host in self.hosts
            if host not in handed and not blocked_sources(self.state, host, entry)
        ]

    def _fallback_time(self, entry: Mapping[str, Any]) -> float:
        """When the in-cluster fallback may take ``entry``: ``host_wait``
        after the request; after a hand-over (0.29.0) at once when no other
        host may take it, else within :data:`HANDOVER_SECONDS`."""
        at = float(entry.get("requested_epoch") or 0) + self.host_wait
        handed = entry.get("handed_epoch")
        if handed is None:
            return at
        if self._eligible(entry) == []:
            return min(at, float(handed))
        return min(at, float(handed) + HANDOVER_SECONDS)

    def _standby_hold(self, entry: Mapping[str, Any]) -> tuple[str, float] | None:
        """A host standing by that may take ``entry`` (0.29.0) and until when
        it holds the fallback, or ``None``."""
        now = self.clock()
        eligible = self._eligible(entry)
        limit = self._fallback_time(entry) + STANDBY_SECONDS
        if now >= limit:
            return None
        for host, item in live_standby(self.state, now).items():
            if eligible is not None and host not in eligible:
                continue
            if blocked_sources(self.state, host, entry):
                continue
            return host, min(limit, float(item.get("until") or 0))
        return None

    def _no_fallback(self, entry: Mapping[str, Any], method: str) -> str | None:
        """Why the in-cluster fallback may not take ``entry`` now, or ``None``."""
        if "cluster" not in self.order:
            return 'builds=("hosts",): no in-cluster fallback'
        if not callable(getattr(self.inner, method, None)) or not getattr(
            self.inner, "builds_images", True
        ):
            return "no in-cluster builder image (piceli gitops enable --builder-image)"
        blocked = self.cluster_blocked()
        if blocked is not None:
            return blocked
        claimed = live_claim(entry, self.clock())
        if claimed is not None:
            return f"claimed by {claimed.get('host')} since {claimed.get('at')}"
        held = self._standby_hold(entry)
        if held is not None:
            return (
                f"standing by: build host {held[0]} takes it "
                f"(piceli builder serve --take-next) until {_iso(held[1])}"
            )
        return None

    def _fallback_at(self, entry: Mapping[str, Any], method: str) -> float | None:
        """When the in-cluster fallback may take ``entry`` (``None``: never,
        or not while a live host holds it)."""
        if self._no_fallback(entry, method) is None:
            return self._fallback_time(entry)
        held = self._standby_hold(entry)
        if held is not None and live_claim(entry, self.clock()) is None:
            return held[1]  # 0.29.0: looked at again when the standby ends
        return None

    def _fallback_due(self, entry: Mapping[str, Any], method: str) -> bool:
        if self._no_fallback(entry, method) is not None:
            return False
        at, now = self._fallback_time(entry), self.clock()
        # A hand-over is due at its time; host_wait once it has passed.
        return now > at or (entry.get("handed_epoch") is not None and now >= at)

    def waiting(
        self, entry: Mapping[str, Any], method: str, names: Sequence[str]
    ) -> str:
        """What the step waits for (0.27.1), for example ``waiting for a build
        host (docker) to build web: offered to builder-1; not claimed;
        in-cluster fallback at 2026-01-01T00:00:20Z``."""
        now = self.clock()
        text = f"waiting for a build host (docker) to build {', '.join(names)}"
        text += ": offered to " + (", ".join(self.hosts) or "any build host")
        claimed = live_claim(entry, now)
        if claimed is not None:
            text += f"; claimed by {claimed.get('host')} at {claimed.get('at')}"
            stage = (entry.get("progress") or {}).get("stage")
            if stage:
                text += f" ({stage})"
        elif entry.get("claimed"):
            held = entry["claimed"]
            text += (
                f"; the claim of {held.get('host')} at {held.get('at')} expired "
                f"(no report for {int(CLAIM_STALE_SECONDS // 60)} min)"
            )
        else:
            text += "; not claimed"
        for item in entry.get("handed_over") or ():
            # 0.29.0: hosts that failed before the build's first command.
            text += f"; handed over by {item.get('host')} ({item.get('reason')})"
        why = self._no_fallback(entry, method)
        if why is None:
            text += f"; in-cluster fallback at {_iso(self._fallback_time(entry))}"
        elif claimed is None or not why.startswith("claimed by"):
            text += f"; no in-cluster fallback: {why}"
        return text

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
            self.waiting(entry, "build_spec", sorted(request.images)),
            retry_at=self._fallback_at(entry, "build_spec"),
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
            self.waiting(entry, "build", sorted(item.component for item in items)),
            retry_at=self._fallback_at(entry, "build"),
        )


def _failure_error(failure: Mapping[str, Any], build: str = "") -> CompositionError:
    """``component-build-failed`` with the host's output tail, its stage and
    (0.41.0) the wanted build's id: a hash of its inputs, the change key the
    controller compares to tell an identical failure."""
    outcome: dict[str, Any] = {"log_tail": str(failure.get("log_tail") or "")}
    if failure.get("stage"):
        outcome["stage"] = str(failure["stage"])[:16]
    error = CompositionError(
        "component-build-failed",
        f"build host {failure.get('host')}: {failure.get('reason')}",
        details={
            "outcome": outcome,
            "host": str(failure.get("host") or "?")[:64],
            **({"change_key": build} if build else {}),
        },
    )
    error.output_tail = str(failure.get("log_tail") or "")  # type: ignore[attr-defined]
    error.from_host = str(failure.get("host") or "?")  # type: ignore[attr-defined]
    return error


def settle(state: dict[str, Any], owner: str) -> list[str]:
    """``owner``'s step ended without waiting for a build (deployed, failed,
    stopped): the builds it asked for are no longer wanted (0.22.0)."""
    wanted = state.get(STATE_KEY) or {}
    gone = [key for key, item in wanted.items() if item.get("owner") == owner]
    for key in gone:
        del wanted[key]
    return gone


def prune(state: dict[str, Any], now: float, max_age: float) -> list[str]:
    """Drop the wanted builds no step asked for in ``max_age`` seconds
    (0.22.0). A waiting step asks again at every poll, so only leftovers go:
    builds of a superseded commit or of an environment that moved on, and
    entries an older controller left without an owner."""
    wanted = state.get(STATE_KEY) or {}
    gone = [
        key
        for key, item in wanted.items()
        if now - float(item.get("asked_epoch") or item.get("requested_epoch") or 0)
        > max_age
    ]
    for key in gone:
        del wanted[key]
    return gone


def clear_failures(state: dict[str, Any]) -> None:
    """``piceli gitops sync``: failed builds may be offered again (0.22.0)."""
    state.pop(FAILED_KEY, None)


def public(state: Mapping[str, Any], now: float) -> list[dict[str, Any]]:
    """``builds_wanted`` in the controller's status (oldest first)."""
    found = []
    for entry in sorted(
        (state.get(STATE_KEY) or {}).values(),
        key=lambda item: float(item.get("requested_epoch") or 0),
    ):
        claimed = live_claim(entry, now) or {}
        item = {
            key: value
            for key, value in entry.items()
            if key not in {"requested_epoch", "asked_epoch", "failed", "handed_epoch"}
        } | {"claimed": {k: v for k, v in claimed.items() if k != "epoch"} or None}
        if entry.get("claimed") and not claimed:
            # 0.27.1: a dead host's claim, shown (any host may claim it).
            item["claim_expired"] = {
                k: v for k, v in entry["claimed"].items() if k != "epoch"
            }
        if isinstance(item.get("progress"), Mapping):
            item["progress"] = {
                k: v for k, v in item["progress"].items() if k != "epoch"
            }
        failure = entry.get("failed")
        if failure:
            # 0.22.0: shown, so hosts never offer it again and people see why.
            item["failed"] = {
                "host": failure.get("host"),
                "reason": failure.get("reason"),
                "log_tail": str(failure.get("log_tail") or "")[-2000:],
                **({"stage": failure["stage"]} if failure.get("stage") else {}),
            }
        found.append(item)
    return found


def claim(state: dict[str, Any], body: Mapping[str, Any], now: float) -> None:
    """A host takes a wanted build (``build-claim``).

    :raises GitOpsError: ``gitops-request-invalid`` (no such build, or
        another host holds a live claim).
    """
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    held = live_claim(entry, now) or {}
    host = str(body.get("host") or "")
    blocked = blocked_sources(state, host, entry)
    if blocked:  # 0.29.0: it could not fetch one of them
        raise GitOpsError("gitops-request-invalid", describe_blocked(host, blocked))
    if held and held.get("host") != host:
        raise GitOpsError(
            "gitops-request-invalid",
            f"build host {held.get('host')} already builds it (claimed at "
            f"{held.get('at')})",
        )
    if held.get("host") != host:
        entry.pop("progress", None)  # an earlier holder's
    at = held.get("at") if held.get("host") == host else None
    entry["claimed"] = {"host": host, "at": at or _iso(now), "epoch": now}
    (state.get(STANDBY_KEY) or {}).pop(host, None)  # 0.29.0: it took one


def failed(
    state: dict[str, Any], body: Mapping[str, Any], now: float | None = None
) -> bool:
    """A host could not build it (``build-failed``): the step fails with its
    tail. 0.29.0: with ``early`` (it failed before the build's first
    command) the build is handed over instead (see the module doc): the
    claim goes, the host is quarantined for ``source`` (every source of the
    build when it names none); returns whether it was handed over."""
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    host = str(body.get("host") or "?")[:64]
    if body.get("early") and now is not None:
        reason = str(body.get("reason") or "the build failed")[:300]
        source = body.get("source")
        sources = sources_of(entry)
        quarantined = [str(source)] if source in sources else sorted(sources)
        held = state.setdefault(SOURCE_QUARANTINE_KEY, {}).setdefault(host, {})
        for name in quarantined:
            held[name] = {"reason": reason, "at": _iso(now), "build": entry["id"]}
        if (entry.get("claimed") or {}).get("host") == host:
            entry.pop("claimed", None)
            entry.pop("progress", None)
        entry["handed_over"] = [
            *[i for i in entry.get("handed_over") or () if i.get("host") != host],
            {
                "host": host,
                "reason": reason,
                "log_tail": str(body.get("log_tail") or "")[-2000:],
                "at": _iso(now),
            },
        ][-8:]
        entry["handed_epoch"] = now
        return True
    entry["failed"] = {
        "host": str(body.get("host") or "?"),
        "reason": str(body.get("reason") or "the build failed")[:300],
        "log_tail": str(body.get("log_tail") or "")[-8000:],
        # 0.41.0: the host's stage (its answer's, else its last report's).
        "stage": str(
            body.get("stage") or (entry.get("progress") or {}).get("stage") or ""
        )[:16]
        or None,
    }
    return False


def progress(
    state: dict[str, Any], body: Mapping[str, Any], now: float | None = None
) -> None:
    """A build host's stage and log tail (0.26.0, ``build-progress``): kept
    on the wanted build it holds (``progress`` in ``builds_wanted``); one of
    a build no longer wanted (cancelled, answered) is dropped quietly."""
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None or (entry.get("claimed") or {}).get("host") != body.get("host"):
        return
    entry["progress"] = {
        "stage": str(body.get("stage") or "")[:16],
        "at": str(body.get("at") or "")[:40],
        "log_tail": str(body.get("log_tail") or "")[-4000:],
        # 0.27.1: when the controller heard it (keeps the claim alive).
        **({"epoch": now} if now is not None else {}),
    }


def seen(
    state: dict[str, Any], kind: str, body: Mapping[str, Any], now: float, ok: bool
) -> None:
    """Remember that a build host sent ``body`` (0.28.0, ``piceli builder
    status``): its Piceli version, when and what; with ``ok`` (the
    controller took it) also the build it names and the outcome. A refused
    claim is a sighting, never a build of its own."""
    host = str(body.get("host") or "")[:64]
    if not host or kind not in (
        *_OUTCOMES, "build-progress", "build-standby", "build-host-version",
    ):  # fmt: skip
        return
    hosts = state.setdefault(HOSTS_KEY, {})
    entry = dict(hosts.get(host) or {})
    if body.get("version"):
        entry["version"] = str(body["version"])[:40]
    entry.update(seen_at=_iso(now), seen_epoch=now, seen=kind)
    outcome = _OUTCOMES.get(kind)
    if ok and outcome is not None:
        build = str(body.get("id") or "")[:20]
        before = entry.get("last_build") or {}
        # An answered build has left the wanted builds: its kind is the claim's.
        kind_ = ((state.get(STATE_KEY) or {}).get(build) or {}).get("kind") or (
            before.get("kind") if before.get("id") == build else None
        )
        entry["last_build"] = {
            "id": build, "kind": kind_, "outcome": outcome, "at": _iso(now),
        }  # fmt: skip
    hosts[host] = entry
    for name in sorted(hosts, key=lambda k: float(hosts[k].get("seen_epoch") or 0))[
        :-HOSTS_KEPT
    ]:
        del hosts[name]


def public_hosts(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``controller.build_hosts`` in the status (0.28.0)."""
    return {
        name: {k: v for k, v in entry.items() if k != "seen_epoch"}
        for name, entry in sorted((state.get(HOSTS_KEY) or {}).items())
    }


def cancel(state: dict[str, Any], build: str, by: Any, now: float) -> None:
    """``piceli gitops cancel`` of an image build (0.26.0): it is no longer
    wanted (a host that holds it stops at its next stage; its answer is
    refused) and its inputs count as failed, so the waiting step fails with
    ``cancelled`` instead of asking again, until ``piceli gitops sync``."""
    wanted = state.get(STATE_KEY) or {}
    entry = wanted.pop(build, None)
    if entry is None:
        return
    failures = state.setdefault(FAILED_KEY, {})
    failures[build] = {
        "host": (entry.get("claimed") or {}).get("host") or "none",
        "reason": "cancelled" + (f" by {str(by)[:120]}" if by else ""),
        "log_tail": str((entry.get("progress") or {}).get("log_tail") or ""),
        "owner": entry.get("owner"),
        "at": _iso(now),
        "cancelled": True,
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
        item["component"]: {"key": item["digest"], "repository": item["repository"]}
        for item in entry.get("components") or ()
    }
    nodes = dict((entry.get("deliver") or {}).get("nodes") or {})
    found: dict[str, dict[str, Any]] = {}
    for name, image in images.items():
        if not isinstance(image, Mapping):
            raise GitOpsError("gitops-request-invalid", f"image {name}: not an object")
        key = str((keys.get(name) or {}).get("key"))
        if str(image.get("key")) != key:
            raise GitOpsError(
                "gitops-request-invalid", f"image {name}: built from other inputs"
            )
        if nodes or image.get("delivery") == "nodes":
            found[name] = _node_delivered(
                name,
                image,
                key=key,
                repository=str((keys.get(name) or {}).get("repository") or ""),
                nodes=nodes,
                platforms=list(entry.get("platforms") or ()),
            )
            continue
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


def _node_delivered(
    name: str,
    image: Mapping[str, Any],
    *,
    key: str,
    repository: str,
    nodes: Mapping[str, str],
    platforms: Sequence[str],
) -> dict[str, Any]:
    """Check one image a host imported into the nodes (see the module doc).

    :raises GitOpsError: ``gitops-request-invalid``.
    """

    def refuse(reason: str) -> GitOpsError:
        return GitOpsError("gitops-request-invalid", f"image {name}: {reason}")

    if not nodes:
        raise refuse(
            "delivered into the nodes, but the controller delivers to its registry"
        )
    if image.get("delivery") != "nodes":
        raise refuse("the controller asked for a delivery into the nodes")
    configs = image.get("platforms")
    if (
        not isinstance(configs, Mapping)
        or not configs
        or not all(
            isinstance(digest, str) and _DIGEST.fullmatch(digest)
            for digest in configs.values()
        )
        or not set(configs) <= set(platforms)
    ):
        raise refuse(
            "its platforms are not the config digests of the platforms asked for"
        )
    configs = {str(k): str(v) for k, v in configs.items()}
    reference = node_reference(repository, configs)
    identity = node_identity(configs)
    if image.get("pull_ref") != reference or image.get("manifest_digest") != identity:
        raise refuse("its reference is not the content tag of its config digests")
    receipts = image.get("nodes")
    if not isinstance(receipts, Mapping):
        raise refuse("no node receipts")
    for node, platform in sorted(nodes.items()):
        receipt = receipts.get(node)
        if not isinstance(receipt, Mapping):
            raise refuse(f"not delivered into node {node}")
        if receipt.get("platform") != platform:
            raise refuse(f"node {node} runs {platform}, not {receipt.get('platform')}")
        if receipt.get("result") not in {"imported", "already-present"}:
            raise refuse(f"the import into node {node} ended {receipt.get('result')}")
        if receipt.get("reference") != reference:
            raise refuse(f"node {node} holds it under another name")
        if receipt.get("config_digest") != configs.get(platform):
            raise refuse(
                f"node {node} holds config {receipt.get('config_digest')}, "
                f"not the {platform} image built ({configs.get(platform)})"
            )
    return {
        "key": key,
        "pull_ref": reference,
        "manifest_digest": identity,
        "platforms": dict(sorted(configs.items())),
        "nodes": sorted(nodes),
    }


def offered(
    state: dict[str, Any],
    body: Mapping[str, Any],
    *,
    present: Callable[[str], bool],
    repository: Callable[[str], str],
    components: Sequence[str],
    registry: str | None,
    now: float,
) -> list[str]:
    """Check a ``prebuilt`` request and keep its offers (see the module doc);
    the components offered.

    :param repository: The component's repository in this registry.
    :param components: The images an offer may name: the composition's
        components and its pipeline build specs' images.
    :param registry: The registry's host (``None``: not checked).
    :raises GitOpsError: ``gitops-request-invalid``.
    """
    images = body.get("images")
    if not isinstance(images, Mapping) or not images:
        raise GitOpsError("gitops-request-invalid", "a prebuilt offer holds no images")
    receipt = body.get("receipt")
    receipt = receipt if isinstance(receipt, Mapping) else {}
    from piceli.infra import provenance

    record = provenance.from_receipt(receipt, str(body.get("host") or "?"))
    checked: dict[str, dict[str, Any]] = {}
    for name, image in images.items():
        if not isinstance(image, Mapping):
            raise GitOpsError("gitops-request-invalid", f"image {name}: not an object")
        if name not in components:
            raise GitOpsError(
                "gitops-request-invalid",
                f"image {name}: not a component or build-spec image of the composition",
            )
        key = str(image.get("key") or "")
        if not _KEY.fullmatch(key):
            raise GitOpsError("gitops-request-invalid", f"image {name}: no change key")
        pull_ref = str(image.get("pull_ref") or "")
        digest = str(image.get("manifest_digest") or "")
        if not _DIGEST.fullmatch(digest) or not pull_ref.endswith("@" + digest):
            raise GitOpsError(
                "gitops-request-invalid", f"image {name}: not pinned by its digest"
            )
        where = pull_ref.rsplit("@", 1)[0]
        host, _, path = where.partition("/")
        if path != repository(name) or (registry is not None and host != registry):
            raise GitOpsError(
                "gitops-request-invalid",
                f"image {name}: not in this registry's repository {repository(name)}",
            )
        if not present(pull_ref):
            raise GitOpsError(
                "gitops-request-invalid",
                f"image {name}: {digest} is not in the registry",
            )
        platforms = image.get("platforms")
        sources = image.get("sources")
        checked[name] = {
            "key": key,
            "pull_ref": pull_ref,
            "manifest_digest": digest,
            "platforms": dict(platforms) if isinstance(platforms, Mapping) else {},
            "source": str(image.get("source") or "")[:100],
            "commit": str(image.get("commit") or "")[:64],
            # Every source's commit the key was made with (status explains
            # an offer the controller computed another key for).
            "sources": {
                str(k)[:100]: str(v)[:64] for k, v in sorted(sources.items())[:16]
            }
            if isinstance(sources, Mapping)
            else {},
            "host": str(body.get("host") or "?")[:64],
            "provenance": record,
            "at": _iso(now),
            "epoch": now,
        }
    offers = state.setdefault(OFFERS_KEY, {})
    for name, item in checked.items():
        kept = offers.setdefault(name, {})
        kept[item["key"]] = item
        for old in sorted(kept, key=lambda k: float(kept[k].get("epoch") or 0))[
            :-OFFERS_PER_COMPONENT
        ]:
            del kept[old]
    return sorted(checked)


def take_offer(
    state: Mapping[str, Any], component: str, key: str
) -> dict[str, Any] | None:
    """The offer of ``component`` under ``key`` (the controller's own change
    key), or ``None``. The offer stays until it is replaced or aged out."""
    found = ((state.get(OFFERS_KEY) or {}).get(component) or {}).get(key)
    return dict(found) if isinstance(found, Mapping) else None


def unused_offer(
    state: Mapping[str, Any],
    component: str,
    key: str,
    sources: Mapping[str, str],
) -> dict[str, Any] | None:
    """Why the newest offer of ``component`` was not used for a step whose
    own change key is ``key``, made with ``sources`` (``{source: commit}``):
    ``{"used": False, "key": the offered key, "differs": {source:
    {"offered", "here"}}}`` naming every source whose commit differs (none:
    the same commits, other inputs such as the platforms or the spec), or
    ``None`` when no offer of it is kept or the newest has this key."""
    kept = (state.get(OFFERS_KEY) or {}).get(component) or {}
    if not kept:
        return None
    newest = max(kept.values(), key=lambda item: float(item.get("epoch") or 0))
    if newest.get("key") == key:
        return None
    offered = dict(newest.get("sources") or {})
    if not offered and newest.get("source"):
        offered = {str(newest["source"]): str(newest.get("commit") or "")}
    differs = {
        name: {"offered": offered.get(name), "here": sources.get(name)}
        for name in sorted(set(offered) | set(sources))
        if offered.get(name) != sources.get(name)
    }
    return {"used": False, "key": str(newest.get("key")), "differs": differs}


def public_offers(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``prebuilt`` in the controller's status: each offer, newest first."""
    found = []
    for component, items in sorted((state.get(OFFERS_KEY) or {}).items()):
        for key, item in items.items():
            found.append(
                {
                    "component": component,
                    "key": key,
                    "image": item.get("pull_ref"),
                    "host": item.get("host"),
                    "train": (item.get("provenance") or {}).get("train"),
                    **({"sources": item["sources"]} if item.get("sources") else {}),
                    "at": item.get("at"),
                }
            )
    return sorted(found, key=lambda item: str(item.get("at")), reverse=True)
