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

0.75.0: the ``build-failed`` request names its ``cause`` (:func:`early_cause`;
a builder before 0.75.0 names none: a fetch failure names its source, an
engine failure none). A container engine that could not start the build
(``engine``) quarantines no source: the host is backed off for its image
builds, :data:`ENGINE_BACKOFF_SECONDS` and twice as long at each next
failure in a row, and blocked at :data:`ENGINE_BLOCK_AFTER`
(:data:`ENGINE_KEY`, ``controller.engine_backoff``); its next build that
starts clears the count. A source quarantine ends after
:data:`SOURCE_QUARANTINE_SECONDS`. Image builds of an environment that went
to the in-cluster fallback in a row are counted (:data:`FALLBACKS_KEY`,
``controller.builds_fallback``) for the ``builds-fallback`` alarm.

0.76.0: a host's ``built`` and ``build-failed`` answers carry ``steps``,
each command's seconds as its build container's receipt lists them (a
``build-failed`` only when the build ran commands: the steps that ran).
The controller keeps the valid ones (:func:`host_steps`) where an
in-cluster build's go, so ``piceli gitops builds`` shows them for every
build. A builder before 0.76.0 sends none: the build is recorded without
steps. A controller before 0.76.0 ignores the field.

0.71.0: the fallback no longer waits ``host_wait`` for hosts that cannot
build. At once when no declared host may take the build, also without a
hand-over in this build (a host quarantined for a source by an earlier
build never claims it); within :data:`HANDOVER_SECONDS` when none that may
is live (heard from within :data:`CLAIM_STALE_SECONDS`, or standing by): a
serving host claims within its poll, an offline laptop is not waited for.
The waiting step says why. A host whose mirror of a source is corrupt
re-clones it once before it hands the build over
(:func:`piceli.infra.builder_serve.export_source`).

0.89.0: an idle build host is live, not dead. ``builder serve`` renews a
lease (``build-host-lease``) every :data:`LEASE_SECONDS` while it polls
with nothing to claim, so "not heard from for :data:`CLAIM_STALE_SECONDS`"
means the host is gone. A host that reports another Piceli version than
the controller's (``build-host-version``; it claims nothing until it
restarts on that version) is coming for :data:`UPGRADE_GRACE_SECONDS`:
the fallback waits ``host_wait`` for it. A host that claims a build while
its in-cluster fallback Job runs takes it over: the build stays wanted
(``fallback`` on the entry) while the Job runs; on the step's next look the
Job is stopped when it has not finished, and the host's answer is the
build's only result (when the Job finished first, its result is used and
the host's answer is refused, the build being no longer wanted).

Standing by (0.29.0, ``piceli builder serve --once --take-next``): a host
that waits to take the next build says so (``build-standby``); while it
stands by, the in-cluster fallback waits for it at most
:data:`STANDBY_SECONDS` past its usual time, so a host started by hand wins
the build instead of racing ``host_wait``.

Delivery into the nodes (0.22.0, ``Controller(delivery="nodes")``): the
wanted build also names every declared node and its platform
(``deliver.nodes``); 0.68.0: for a pipeline environment, only the nodes its
pods can run on, built for their platforms only
(:func:`piceli.infra.pipelines.placement`). The host imports each image into each node's
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
#: 0.75.0: a source quarantine ends this long after it began (the host
#: tries the source again then; ``release-host`` ends it sooner).
SOURCE_QUARANTINE_SECONDS = 24 * 3600.0
#: 0.75.0: hosts whose container engine could not start a build,
#: ``{host: {failures, reason, at, since, build, until, until_at, blocked}}``;
#: cleared by the host's next build that started.
ENGINE_KEY = "host_engine_failures"
#: 0.75.0: the first engine failure backs a host off this long, each next
#: one in a row twice as long, at most :data:`ENGINE_BACKOFF_MAX`.
ENGINE_BACKOFF_SECONDS = 300.0
ENGINE_BACKOFF_MAX = 3600.0
#: 0.75.0: this many engine failures in a row block the host until
#: ``piceli gitops release-host``.
ENGINE_BLOCK_AFTER = 3
#: 0.75.0: why a host handed a build over (``cause`` of ``build-failed``).
CAUSES = ("engine", "source")
#: 0.75.0: image builds of an environment that went to the in-cluster
#: fallback in a row, ``{env: {builds, since, since_epoch, last_at}}``;
#: cleared when a host delivers one of its builds.
FALLBACKS_KEY = "builds_fallback"
#: 0.29.0: after a host handed a build over, how long the in-cluster
#: fallback leaves it to another declared host that may take it.
HANDOVER_SECONDS = 60.0
#: 0.29.0: hosts standing by for the next build (``build-standby``).
STANDBY_KEY = "build_standby"
#: 0.29.0: a standby lives this long after its last report, and holds the
#: fallback at most this long past its usual time.
STANDBY_SECONDS = 120.0
#: 0.89.0: an idle ``builder serve`` renews its lease (``build-host-lease``)
#: this often, well under :data:`CLAIM_STALE_SECONDS` (one request per
#: renewal; a lost one or two never makes a host stale).
LEASE_SECONDS = 120.0
#: 0.89.0: a host that reported another Piceli version than the
#: controller's counts as coming this long after its first such report: it
#: restarts on the controller's version, then claims.
UPGRADE_GRACE_SECONDS = 1800.0
#: What each answer says about the build it names.
_OUTCOMES = {
    "build-claim": "claimed",
    "built": "built",
    "nix-published": "published",
    "build-failed": "failed",
}
#: 0.76.0: steps kept of a host's answer (as the history keeps).
MAX_STEPS = 64
#: 0.76.0: a step longer than this (seconds) is not a real step.
_MAX_STEP_SECONDS = 7 * 24 * 3600.0
#: 0.76.0: each text field of a step and its longest value.
_STEP_TEXTS = {"platform": 32, "kind": 64, "tool": 64, "state": 32}


def host_steps(value: Any) -> list[dict[str, Any]]:
    """0.76.0: the valid steps of a host's answer, at most :data:`MAX_STEPS`.

    A build host is less trusted than the controller: only a list of
    objects is read, and only their ``platform``, ``kind``, ``tool``,
    ``state`` (text, bounded), ``exit_code`` (an integer) and ``seconds``
    (a finite number from 0 to a week). A step with a field of another
    type, a text too long, no ``kind`` or no ``seconds`` is dropped;
    anything else of it is ignored. 0.78.0: ``start_seconds`` (when the
    step started, from the build's start; same bounds) is kept when valid."""
    import math

    if not isinstance(value, list):
        return []
    found: list[dict[str, Any]] = []
    for item in value[: MAX_STEPS * 4]:
        if not isinstance(item, Mapping):
            continue
        step: dict[str, Any] = {}
        for name, limit in _STEP_TEXTS.items():
            text = item.get(name)
            if text is None:
                continue
            if not isinstance(text, str) or not 0 < len(text) <= limit:
                break
            step[name] = text
        else:
            code = item.get("exit_code")
            seconds = item.get("seconds")
            if (
                "kind" not in step
                or isinstance(seconds, bool)
                or not isinstance(seconds, int | float)
                or not math.isfinite(seconds)
                or not 0 <= seconds <= _MAX_STEP_SECONDS
            ):
                continue
            if code is not None:
                if isinstance(code, bool) or not isinstance(code, int):
                    continue
                if not -(2**31) <= code < 2**31:
                    continue
                step["exit_code"] = code
            step["seconds"] = round(float(seconds), 3)
            start = item.get("start_seconds")
            if (
                isinstance(start, int | float)
                and not isinstance(start, bool)
                and math.isfinite(start)
                and 0 <= start <= _MAX_STEP_SECONDS
            ):
                step["start_seconds"] = round(float(start), 3)
            found.append(step)
            if len(found) == MAX_STEPS:
                break
    return found


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


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _source_until(record: Mapping[str, Any]) -> float | None:
    """When a source quarantine ends (0.75.0); one an older controller
    wrote (no ``until``) ends :data:`SOURCE_QUARANTINE_SECONDS` after it
    began; ``None``: its time is unreadable (it holds)."""
    if record.get("until") is not None:
        try:
            return float(record["until"])
        except (TypeError, ValueError):
            return None
    began = _epoch(record.get("at"))
    return None if began is None else began + SOURCE_QUARANTINE_SECONDS


def _source_held(record: Any, now: float | None) -> bool:
    if not isinstance(record, Mapping):
        return False
    until = _source_until(record)
    return now is None or until is None or now < until


def blocked_sources(
    state: Mapping[str, Any],
    host: str,
    entry: Mapping[str, Any],
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """The sources of ``entry`` that ``host`` is quarantined for (0.29.0);
    with ``now`` (0.75.0) only the quarantines that have not ended."""
    held = (state.get(SOURCE_QUARANTINE_KEY) or {}).get(host) or {}
    return {
        source: dict(held[source])
        for source in sorted(sources_of(entry))
        if source in held and _source_held(held[source], now)
    }


def describe_blocked(host: str, blocked: Mapping[str, Mapping[str, Any]]) -> str:
    source, record = next(iter(sorted(blocked.items())))
    return (
        f"build host {host} cannot fetch source {source} (since {record.get('at')}: "
        f"{record.get('reason')}); piceli gitops release-host {host} clears it"
    )


def early_cause(body: Mapping[str, Any]) -> str:
    """Why a host handed a build over (0.75.0): ``engine`` (its container
    engine could not start the build) or ``source`` (it could not fetch
    one). A builder before 0.75.0 sends no ``cause``: a fetch failure names
    its source, an engine failure names none."""
    cause = body.get("cause")
    if cause in CAUSES:
        return str(cause)
    return "source" if body.get("source") else "engine"


def engine_hold(
    state: Mapping[str, Any], host: str, now: float
) -> dict[str, Any] | None:
    """``host``'s engine record while it holds the host's image builds back
    (0.75.0): blocked, or backed off until a time still ahead."""
    record = (state.get(ENGINE_KEY) or {}).get(host)
    if not isinstance(record, Mapping):
        return None
    if record.get("blocked") or now < float(record.get("until") or 0):
        return dict(record)
    return None


def describe_engine(host: str, record: Mapping[str, Any]) -> str:
    failures = int(record.get("failures") or 1)
    what = (
        f"build host {host}: its container engine could not start a build "
        f"{failures} time(s) in a row (last at {record.get('at')}: "
        f"{record.get('reason')})"
    )
    if record.get("blocked"):
        return f"{what}; blocked, piceli gitops release-host {host} clears it"
    return (
        f"{what}; backed off until {record.get('until_at')} "
        f"(piceli gitops release-host {host} clears it sooner)"
    )


def refused(
    state: Mapping[str, Any], host: str, entry: Mapping[str, Any], now: float
) -> str | None:
    """Why ``host`` may not take ``entry`` now, or ``None``: a source it
    could not fetch (0.29.0), or (0.75.0, image builds only) its container
    engine is backed off or blocked."""
    blocked = blocked_sources(state, host, entry, now)
    if blocked:
        return describe_blocked(host, blocked)
    if entry.get("kind") != "nix-publish":
        record = engine_hold(state, host, now)
        if record is not None:
            return describe_engine(host, record)
    return None


def engine_failed(
    state: dict[str, Any], host: str, reason: str, build: str, now: float
) -> dict[str, Any]:
    """One more engine failure of ``host`` in a row (0.75.0): backed off
    :data:`ENGINE_BACKOFF_SECONDS`, doubled at each next one (at most
    :data:`ENGINE_BACKOFF_MAX`), blocked at :data:`ENGINE_BLOCK_AFTER`."""
    found = state.setdefault(ENGINE_KEY, {})
    before = found.get(host) if isinstance(found.get(host), Mapping) else {}
    failures = int((before or {}).get("failures") or 0) + 1
    wait = min(ENGINE_BACKOFF_SECONDS * 2 ** (failures - 1), ENGINE_BACKOFF_MAX)
    found[host] = record = {
        "failures": failures,
        "reason": reason,
        "cause": "engine",
        "at": _iso(now),
        "since": (before or {}).get("since") or _iso(now),
        "build": build,
        "until": now + wait,
        "until_at": _iso(now + wait),
        "blocked": failures >= ENGINE_BLOCK_AFTER,
    }
    return record


def engine_started(state: dict[str, Any], host: str) -> bool:
    """``host`` started a build (0.75.0: it built, or its build ran and
    failed): its engine works, its engine failures are forgotten."""
    return (state.get(ENGINE_KEY) or {}).pop(host, None) is not None


def public_engine(state: Mapping[str, Any], now: float) -> dict[str, Any]:
    """``controller.engine_backoff`` in the status (0.75.0): the hosts whose
    engine holds their image builds back, with ``state`` ``backoff`` or
    ``blocked``."""
    found: dict[str, Any] = {}
    for host in sorted(state.get(ENGINE_KEY) or {}):
        record = engine_hold(state, host, now)
        if record is None:
            continue
        found[host] = {
            "state": "blocked" if record.get("blocked") else "backoff",
            "cause": "engine",
            **{
                key: record.get(key)
                for key in ("failures", "reason", "at", "since", "build")
            },
            **({} if record.get("blocked") else {"until": record.get("until_at")}),
        }
    return found


def fell_back(state: dict[str, Any], owner: str | None, now: float) -> None:
    """``owner``'s image build went to the in-cluster fallback (0.75.0)."""
    if not owner:
        return
    found = state.setdefault(FALLBACKS_KEY, {})
    before = found.get(owner) if isinstance(found.get(owner), Mapping) else {}
    found[owner] = {
        "builds": int((before or {}).get("builds") or 0) + 1,
        "since": (before or {}).get("since") or _iso(now),
        "since_epoch": float((before or {}).get("since_epoch") or now),
        "last_at": _iso(now),
    }


def host_delivered(state: dict[str, Any], host: str, owner: str | None) -> None:
    """A host delivered ``owner``'s build (0.75.0): its engine works and the
    environment's run of fallback builds ends."""
    engine_started(state, host)
    if owner:
        (state.get(FALLBACKS_KEY) or {}).pop(owner, None)


def public_fallbacks(state: Mapping[str, Any]) -> dict[str, Any]:
    """``controller.builds_fallback`` in the status (0.75.0): per
    environment, how many image builds in a row went to the in-cluster
    fallback and since when."""
    return {
        owner: {k: v for k, v in item.items() if k != "since_epoch"}
        for owner, item in sorted((state.get(FALLBACKS_KEY) or {}).items())
        if isinstance(item, Mapping)
    }


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
    """``piceli gitops release-host``: ``host`` may fetch every source again
    (0.75.0: and its container engine failures are forgotten)."""
    engine = engine_started(state, host)
    return (state.get(SOURCE_QUARANTINE_KEY) or {}).pop(
        host, None
    ) is not None or engine


def public_source_quarantine(
    state: Mapping[str, Any], now: float | None = None
) -> dict[str, Any]:
    """``controller.source_quarantine`` in the status (0.29.0); with ``now``
    (0.75.0) only the quarantines that have not ended, each with its end
    (``until``) and ``cause``."""
    found: dict[str, Any] = {}
    for host, held in sorted((state.get(SOURCE_QUARANTINE_KEY) or {}).items()):
        items = {}
        for source, item in sorted((held or {}).items()):
            if not _source_held(item, now):
                continue
            until = _source_until(item)
            items[source] = {
                **{k: v for k, v in item.items() if k not in ("until", "until_at")},
                "cause": "source",
                **({"until": _iso(until)} if until is not None else {}),
            }
        if items:
            found[host] = items
    return found


def build_id(kind: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, **body}, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]


def spec_body(request: Any) -> dict[str, Any]:
    """What a host needs to build a pipeline spec's changed images."""
    return {
        "spec": request.spec,
        "repo": request.repo,
        "commits": dict(request.commits),
        "urls": dict(request.urls),
        "facts": request.facts,
        "keys": {name: dict(item) for name, item in request.images.items()},
    }


def components_body(items: Sequence[Any], urls: Mapping[str, str]) -> dict[str, Any]:
    """What a host needs to build changed components."""
    return {
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
        "urls": {item.source: urls.get(item.source, "") for item in items},
    }


def identity(kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a wanted build that names it (0.42.0): each image's
    change key (staged content, see docs/compositions.md), never the
    commits it is fetched at. A commit that changes no input of a build
    asks for the same build: a running claim or Job continues and its
    images serve the new revision."""
    if kind == "components":
        found: dict[str, Any] = {
            "components": [
                {k: v for k, v in dict(item).items() if k != "commit"}
                for item in body.get("components") or ()
            ]
        }
    elif kind == "spec":
        found = {k: body.get(k) for k in ("spec", "repo", "facts", "keys")}
    else:
        found = {k: v for k, v in body.items() if k != "deliver"}
    if body.get("deliver") is not None:
        found["deliver"] = body["deliver"]
    return found


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

    def _named(
        self,
        kind: str,
        body: dict[str, Any],
        platforms: Sequence[str] = (),
        placed: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], tuple[str, ...], str]:
        """The body a host gets (with the nodes it delivers into), the
        platforms built and the wanted build's id (see :meth:`_ask`)."""
        built = tuple(platforms or self.platforms)
        if self.nodes:
            nodes = {
                node: platform
                for node, platform in self.nodes.items()
                if placed is None or node in placed
            } or dict(self.nodes)
            missing = sorted(set(nodes.values()) - set(built))
            if missing:
                raise CompositionError(
                    "composition-invalid",
                    f"the nodes run {', '.join(missing)} but the controller builds "
                    f"{', '.join(self.platforms)}: enable it with every node's "
                    "platform (piceli gitops enable --platform ...)",
                )
            body = {**body, "deliver": {"mode": "nodes", "nodes": nodes}}
        found = build_id(kind, {**identity(kind, body), "platforms": list(built)})
        return body, built, found

    def _ask(
        self,
        kind: str,
        body: dict[str, Any],
        names: list[str],
        *,
        stopped_jobs: Sequence[str] = (),
        platforms: Sequence[str] = (),
        placed: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """The wanted entry for this build (new, or the one already asked);
        raises the failure a host reported, or returns for the fallback.

        0.42.0: the entry is named by :func:`identity` (the change keys),
        so a commit that changes none of them finds the same entry and its
        claim. A new entry that replaces a build of the same environment
        is offered first to whoever held that build (``prefer``): the live
        host that had claimed it, or the cluster when ``stopped_jobs`` (its
        superseded build Jobs) is not empty.

        0.68.0: ``platforms`` (default: every enabled one) are the platforms
        built, and ``placed`` the nodes the pods can run on (``None``: every
        node): only those get the images, and only their platforms must be
        enabled."""
        wanted = self._wanted()
        body, built, found = self._named(kind, body, platforms, placed)
        now = self.clock()
        owner = self.owner()
        failure = (self.state.get(FAILED_KEY) or {}).get(found)
        if failure is not None:
            # 0.22.0: the same inputs failed on a host: not offered again
            # until they change or `piceli gitops sync` retries them.
            raise _failure_error(failure, found)
        prefer: dict[str, Any] | None = None
        if stopped_jobs:
            prefer = {"host": "cluster", "jobs": list(stopped_jobs)[:4]}
        if owner is not None:
            # 0.22.0: a newer build of this environment replaces the older
            # one (its inputs changed, or the environment moved on).
            for key in [
                key
                for key, item in wanted.items()
                if key != found and item.get("owner") == owner
            ]:
                old = wanted.pop(key)
                held = live_claim(old, now)
                if held is not None and set(old.get("images") or ()) & set(names):
                    # 0.42.0: offered first to the live host that held it.
                    prefer = {
                        "host": held.get("host"),
                        "replaces": key,
                        "epoch": max(
                            float(held.get("epoch") or 0),
                            float((old.get("progress") or {}).get("epoch") or 0),
                        ),
                    }
        entry = wanted.get(found)
        if entry is None:
            entry = wanted[found] = {
                "id": found,
                "kind": kind,
                **body,
                "platforms": list(built),
                "images": names,
                "requested_at": _iso(now),
                "requested_epoch": now,
                **({"prefer": prefer} if prefer is not None else {}),
            }
        elif live_claim(entry, now) is None and not entry.get("failed"):
            # 0.42.0: the same inputs at a newer commit: a host that claims
            # it from now on fetches the newest commits.
            entry.update({k: v for k, v in body.items() if k != "deliver"})
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
        now = self.clock()
        return [
            host
            for host in self.hosts
            if host not in handed and refused(self.state, host, entry, now) is None
        ]

    def _live(self, host: str, now: float) -> bool:
        """0.71.0: whether the controller heard from ``host`` (a claim, a
        report, a standby; 0.89.0: the lease an idle host renews) within
        :data:`CLAIM_STALE_SECONDS`, or it stands by now."""
        seen = (self.state.get(HOSTS_KEY) or {}).get(host) or {}
        if now - float(seen.get("seen_epoch") or 0) < CLAIM_STALE_SECONDS:
            return True
        return host in live_standby(self.state, now)

    def _upgrading(self, host: str, now: float) -> dict[str, Any] | None:
        """0.89.0: ``host`` reported another Piceli version than the
        controller's less than :data:`UPGRADE_GRACE_SECONDS` ago (it claims
        nothing until it restarts on the controller's version), or ``None``."""
        seen = (self.state.get(HOSTS_KEY) or {}).get(host) or {}
        record = seen.get("upgrading")
        if not isinstance(record, Mapping):
            return None
        if now - float(record.get("since_epoch") or 0) >= UPGRADE_GRACE_SECONDS:
            return None
        return dict(record)

    def _soon(self, entry: Mapping[str, Any]) -> str | None:
        """0.71.0: why the in-cluster fallback does not wait ``host_wait``
        for ``entry``, or ``None``: no declared host may take it (handed
        over, quarantined for one of its sources, also by an earlier
        build), or none that may is live (0.89.0: or restarts on the
        controller's version, :meth:`_upgrading`)."""
        eligible = self._eligible(entry)
        if eligible == []:
            return "no declared host may take it"
        now = self.clock()
        if eligible and not any(
            self._live(h, now) or self._upgrading(h, now) is not None for h in eligible
        ):
            return (
                f"no live host may take it (none reported in the last "
                f"{int(CLAIM_STALE_SECONDS // 60)} min)"
            )
        return None

    def _fallback_time(self, entry: Mapping[str, Any]) -> float:
        """When the in-cluster fallback may take ``entry``: ``host_wait``
        after the request; after a hand-over (0.29.0) at once when no other
        host may take it, else within :data:`HANDOVER_SECONDS`.

        0.71.0: at once also when no declared host may take it without a
        hand-over in this build (a host quarantined for a source by an
        earlier build never claims it), and within :data:`HANDOVER_SECONDS`
        when none that may is live: a serving host claims within its poll,
        an offline one is not waited for. The ``builds=("hosts",)`` failure
        keeps waiting for an offline host (:meth:`_eligible`)."""
        requested = float(entry.get("requested_epoch") or 0)
        at = requested + self.host_wait
        if (entry.get("prefer") or {}).get("host") == "cluster":
            # 0.42.0: it replaces a build the cluster was running: it goes
            # back to the cluster at once (no host held it).
            at = requested
        handed = entry.get("handed_epoch")
        since = requested if handed is None else float(handed)
        if self._eligible(entry) == []:
            return min(at, since)
        if self._soon(entry) is not None:
            return min(at, since + HANDOVER_SECONDS)
        if handed is None:
            return at
        return min(at, float(handed) + HANDOVER_SECONDS)

    def _preferred_hold(self, entry: Mapping[str, Any]) -> tuple[str, float] | None:
        """The live host that held the build ``entry`` replaces (0.42.0) and
        until when it counts as alive, or ``None``.

        Alive: its last report (a claim, a ``build-progress``, any request
        the controller saw from it) is under :data:`CLAIM_STALE_SECONDS`
        old, the threshold that already decides a claim is dead. It no
        longer holds the build once it may not take it (handed over,
        quarantined for a source) or it builds another wanted build."""
        host = (entry.get("prefer") or {}).get("host")
        if not host or host == "cluster":
            return None
        now = self.clock()
        eligible = self._eligible(entry)
        if eligible is not None and host not in eligible:
            return None
        if host in {str(i.get("host")) for i in entry.get("handed_over") or ()}:
            return None
        if refused(self.state, host, entry, now) is not None:
            return None
        for other in self._wanted().values():
            if other is entry or other.get("id") == entry.get("id"):
                continue
            if (live_claim(other, now) or {}).get("host") == host:
                return None
        seen = (self.state.get(HOSTS_KEY) or {}).get(host) or {}
        last = max(
            float((entry.get("prefer") or {}).get("epoch") or 0),
            float(seen.get("seen_epoch") or 0),
        )
        until = last + CLAIM_STALE_SECONDS
        return (str(host), until) if now < until else None

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
            if refused(self.state, host, entry, now) is not None:
                continue
            return host, min(limit, float(item.get("until") or 0))
        return None

    def _no_fallback(self, entry: Mapping[str, Any], method: str) -> str | None:
        """Why the in-cluster fallback may not take ``entry`` now, or ``None``."""
        if "cluster" not in self.order:
            return 'builds=("hosts",): no in-cluster fallback'
        builder = getattr(self.inner, "inner", self.inner)  # 0.42.0: ClusterBuilds
        if not callable(getattr(builder, method, None)) or not getattr(
            builder, "builds_images", True
        ):
            return "no in-cluster builder image (piceli gitops enable --builder-image)"
        blocked = self.cluster_blocked()
        if blocked is not None:
            return blocked
        claimed = live_claim(entry, self.clock())
        if claimed is not None:
            return f"claimed by {claimed.get('host')} since {claimed.get('at')}"
        preferred = self._preferred_hold(entry)
        if preferred is not None:
            return (
                f"offered first to build host {preferred[0]}, which held the build "
                f"these inputs replace, while it reports (until {_iso(preferred[1])})"
            )
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
        if live_claim(entry, self.clock()) is None:
            preferred = self._preferred_hold(entry)
            if preferred is not None:
                return preferred[1]  # 0.42.0: looked at again when it expires
        held = self._standby_hold(entry)
        if held is not None and live_claim(entry, self.clock()) is None:
            return held[1]  # 0.29.0: looked at again when the standby ends
        return None

    def _fallback_due(self, entry: Mapping[str, Any], method: str) -> bool:
        if self._no_fallback(entry, method) is not None:
            return False
        at, now = self._fallback_time(entry), self.clock()
        # A hand-over (and, 0.42.0, a replacement of a cluster build) is due
        # at its time; host_wait once it has passed.
        at_once = (
            entry.get("handed_epoch") is not None
            or (entry.get("prefer") or {}).get("host") == "cluster"
            or self._eligible(entry) == []  # 0.71.0
        )
        return now > at or (at_once and now >= at)

    def waiting(
        self, entry: Mapping[str, Any], method: str, names: Sequence[str]
    ) -> str:
        """What the step waits for (0.27.1), for example ``waiting for a build
        host (docker) to build web: offered to builder-1; not claimed;
        in-cluster fallback at 2026-01-01T00:00:20Z``."""
        now = self.clock()
        claimed = live_claim(entry, now)
        if claimed is not None:  # 0.56.0: it builds; it no longer waits
            text = f"building {', '.join(names)} on {claimed.get('host')} (docker)"
        else:
            text = f"waiting for a build host (docker) to build {', '.join(names)}"
        text += ": offered to " + (", ".join(self.hosts) or "any build host")
        if claimed is not None:
            text += f"; claimed by {claimed.get('host')} at {claimed.get('at')}"
            stage = (entry.get("progress") or {}).get("stage")
            if stage:
                text += f" ({stage})"
            taken = entry.get("taken_over")
            if isinstance(taken, Mapping):  # 0.89.0
                text += (
                    f"; it took the build over from the in-cluster fallback "
                    f"(Job {taken.get('job') or '?'} stopped at {taken.get('at')})"
                )
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
            soon = self._soon(entry)
            if soon is not None:  # 0.71.0: why it does not wait host_wait
                text += f" ({soon})"
            for host in self._eligible(entry) or ():
                coming = self._upgrading(host, now)
                if coming is not None and not self._live(host, now):
                    # 0.89.0: why it waits for a host that claims nothing yet
                    text += (
                        f"; build host {host} runs piceli {coming.get('version')} "
                        f"and restarts on {coming.get('to')} (since "
                        f"{coming.get('since')})"
                    )
        elif why.startswith("offered first"):
            text += f"; {why}"
        elif claimed is None or not why.startswith("claimed by"):
            text += f"; no in-cluster fallback: {why}"
        return text

    def _jobs(self) -> Any:
        """The in-cluster builder when it runs Jobs without waiting (0.42.0)."""
        found = self.inner
        return found if callable(getattr(found, "running", None)) else None

    def _build(
        self,
        kind: str,
        work: Any,
        body: dict[str, Any],
        names: list[str],
        method: str,
        fallback: Callable[[], Any],
        **placement: Any,
    ) -> Any:
        jobs = self._jobs()
        stopped: list[str] = []
        if jobs is not None:
            jobs.cancelled(kind, work)
            if jobs.running(kind, work):
                # 0.42.0: the fallback's Job runs: looked at, not offered again
                # (0.89.0: unless a build host claimed it meanwhile).
                found = self._named(kind, body, **placement)[2]
                taken, result = self._look_at_fallback(
                    found, kind, work, jobs, fallback
                )
                if not taken:
                    return result
            stopped = jobs.supersede(kind, work)
        entry = self._ask(kind, body, names, stopped_jobs=stopped, **placement)
        if self._fallback_due(entry, method):
            now = self.clock()
            # 0.75.0: counted, so a run of fallback builds raises an alarm.
            fell_back(self.state, self.owner(), now)
            # 0.89.0: still wanted while its Job runs, so a host that claims
            # it takes it over (:meth:`_look_at_fallback`).
            entry.pop("taken_over", None)
            entry["fallback"] = {"at": _iso(now), "epoch": now}
            return self._fallen_back(str(entry["id"]), fallback)
        raise BuildPending(
            self.waiting(entry, method, names),
            retry_at=self._fallback_at(entry, method),
        )

    def _fallen_back(self, found: str, fallback: Callable[[], Any]) -> Any:
        """Run or look at the in-cluster fallback of wanted build ``found``.

        0.89.0: while its Job runs (``BuildPending``) the build stays wanted;
        once the fallback answers (images or a failure) it is no longer
        wanted, so a host's later answer is refused: one result per build."""
        try:
            result = fallback()
        except BuildPending:
            raise
        except BaseException:
            self._drop_fallback(found)
            raise
        self._drop_fallback(found)
        return result

    def _drop_fallback(self, found: str) -> None:
        wanted = self._wanted()
        if (wanted.get(found) or {}).get("fallback") is not None:
            del wanted[found]

    def _look_at_fallback(
        self,
        found: str,
        kind: str,
        work: Any,
        jobs: Any,
        fallback: Callable[[], Any],
    ) -> tuple[bool, Any]:
        """0.89.0: look at the running in-cluster fallback Job of wanted
        build ``found``: ``(False, its result)``, or ``(True, None)`` when a
        build host took the build over.

        The Job is looked at first: finished, its result (or its failure)
        stands, whoever claimed. Still running, and a host holds a live
        claim (the controller took its ``build-claim``): the Job is stopped
        and the host's answer is the build's result. Without such a claim
        nothing is stopped: the step keeps waiting for the Job."""
        entry = self._wanted().get(found)
        if entry is not None and entry.get("fallback") is None:
            entry = None  # not this fallback's (left by a controller before 0.89.0)
        now = self.clock()
        if entry is not None:
            entry["asked_epoch"] = now  # not pruned while its Job runs
        try:
            result = self._fallen_back(found, fallback)
        except BuildPending:
            held = live_claim(entry, now) if entry is not None else None
            if entry is None or held is None:
                raise
        else:
            return False, result
        stop = getattr(jobs, "stop_build", None)
        job = stop(kind, work) if callable(stop) else None
        since = entry.pop("fallback")
        entry["taken_over"] = {
            "host": held.get("host"),
            "job": job,
            "fallback_at": since.get("at"),
            "at": _iso(now),
        }
        return True, None

    def build_spec(self, request: Any, checkout: Any) -> dict[str, Any]:
        return self._build(
            "spec",
            request,
            spec_body(request),
            sorted(request.images),
            "build_spec",
            lambda: self.inner.build_spec(request, checkout),
            # 0.68.0: the environment's platforms and nodes (pipelines.placement).
            platforms=tuple(getattr(request, "platforms", ()) or ()),
            placed=getattr(request, "nodes", None),
        )

    def build(self, items: Sequence[Any], checkout: Any) -> dict[str, Any]:
        return self._build(
            "components",
            items,
            components_body(items, self.urls),
            sorted(item.component for item in items),
            "build",
            lambda: dict(self.inner.build(items, checkout)),
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
        (
            f"the in-cluster build: {failure.get('reason')}"
            if failure.get("host") == "cluster"  # 0.42.0: a cancelled Job
            else f"build host {failure.get('host')}: {failure.get('reason')}"
        ),
        details={
            "outcome": outcome,
            "host": str(failure.get("host") or "?")[:64],
            **({"change_key": build} if build else {}),
        },
    )
    error.output_tail = str(failure.get("log_tail") or "")  # type: ignore[attr-defined]
    error.from_host = str(failure.get("host") or "?")  # type: ignore[attr-defined]
    steps = host_steps(failure.get("steps"))
    if steps:  # 0.76.0: as a failed build Job's (the controller keeps them)
        error.steps = steps  # type: ignore[attr-defined]
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
        if isinstance(item.get("prefer"), Mapping):  # 0.42.0
            item["prefer"] = {k: v for k, v in item["prefer"].items() if k != "epoch"}
        if isinstance(item.get("fallback"), Mapping):  # 0.89.0: its Job runs
            item["fallback"] = {
                k: v for k, v in item["fallback"].items() if k != "epoch"
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
    why = refused(state, host, entry, now)
    if why is not None:  # 0.29.0: a source it could not fetch; 0.75.0: its engine
        raise GitOpsError("gitops-request-invalid", why)
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
    claim goes; returns whether it was handed over.

    0.75.0: by its ``cause`` (:func:`early_cause`). ``source``: the host is
    quarantined for ``source`` (every source of the build when it names
    none) for :data:`SOURCE_QUARANTINE_SECONDS`. ``engine``: no source is
    quarantined; the host is backed off (:func:`engine_failed`). A failure
    after the first command proves the engine works (:func:`engine_started`)."""
    entry = (state.get(STATE_KEY) or {}).get(str(body.get("id")))
    if entry is None:
        raise GitOpsError("gitops-request-invalid", "no such wanted build")
    host = str(body.get("host") or "?")[:64]
    if body.get("early") and now is not None:
        reason = str(body.get("reason") or "the build failed")[:300]
        cause = early_cause(body)
        if cause == "engine":
            engine_failed(state, host, reason, str(entry["id"]), now)
        else:
            source = body.get("source")
            sources = sources_of(entry)
            quarantined = [str(source)] if source in sources else sorted(sources)
            held = state.setdefault(SOURCE_QUARANTINE_KEY, {}).setdefault(host, {})
            for name in quarantined:
                held[name] = {
                    "reason": reason, "cause": "source", "at": _iso(now),
                    "build": entry["id"], "until": now + SOURCE_QUARANTINE_SECONDS,
                    "until_at": _iso(now + SOURCE_QUARANTINE_SECONDS),
                }  # fmt: skip
        if (entry.get("claimed") or {}).get("host") == host:
            entry.pop("claimed", None)
            entry.pop("progress", None)
        entry["handed_over"] = [
            *[i for i in entry.get("handed_over") or () if i.get("host") != host],
            {
                "host": host,
                "reason": reason,
                "cause": cause,  # 0.75.0
                "log_tail": str(body.get("log_tail") or "")[-2000:],
                "at": _iso(now),
            },
        ][-8:]
        entry["handed_epoch"] = now
        return True
    engine_started(state, host)  # 0.75.0: the build ran: the engine works
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
    steps = host_steps(body.get("steps"))
    if steps:  # 0.76.0: the steps that ran, for gitops builds
        entry["failed"]["steps"] = steps
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
    claim is a sighting, never a build of its own. 0.89.0: so is the lease
    an idle host renews (``build-host-lease``); a request on another
    version than the one it was upgrading from ends ``upgrading``."""
    host = str(body.get("host") or "")[:64]
    if not host or kind not in (
        *_OUTCOMES, "build-progress", "build-standby", "build-host-version",
        "build-host-lease",
    ):  # fmt: skip
        return
    hosts = state.setdefault(HOSTS_KEY, {})
    entry = dict(hosts.get(host) or {})
    if body.get("version"):
        entry["version"] = str(body["version"])[:40]
        upgrading = entry.get("upgrading")
        if (
            isinstance(upgrading, Mapping)
            and upgrading.get("version") != (entry["version"])
        ):
            entry.pop("upgrading")  # 0.89.0: it restarted on another version
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


def host_version(
    state: dict[str, Any], body: Mapping[str, Any], version: str, now: float
) -> bool:
    """``build-host-version`` (0.33.0) seen by a controller on ``version``
    (0.89.0): whether the host runs another version. Its first report of
    another version starts ``upgrading`` (``{version, to, since}`` in
    ``controller.build_hosts``): the host claims nothing until it restarts
    on ``version``, and counts as coming for :data:`UPGRADE_GRACE_SECONDS`.
    A report of the same version ends it."""
    host = str(body.get("host") or "")[:64]
    reported = str(body.get("version") or "")[:40]
    if not host:
        return False
    hosts = state.setdefault(HOSTS_KEY, {})
    entry = dict(hosts.get(host) or {})
    other = bool(reported) and version not in ("", "unknown") and reported != version
    before = entry.get("upgrading")
    if not other:
        entry.pop("upgrading", None)
    elif not isinstance(before, Mapping) or (
        before.get("version"),
        before.get("to"),
    ) != (reported, version):
        entry["upgrading"] = {
            "version": reported,
            "to": version,
            "since": _iso(now),
            "since_epoch": now,
        }
    hosts[host] = entry
    return other


def public_hosts(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``controller.build_hosts`` in the status (0.28.0)."""
    found: dict[str, dict[str, Any]] = {}
    for name, entry in sorted((state.get(HOSTS_KEY) or {}).items()):
        item = {k: v for k, v in entry.items() if k != "seen_epoch"}
        if isinstance(item.get("upgrading"), Mapping):  # 0.89.0
            item["upgrading"] = {
                k: v for k, v in item["upgrading"].items() if k != "since_epoch"
            }
        found[name] = item
    return found


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
