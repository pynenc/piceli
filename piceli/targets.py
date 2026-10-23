"""One status view of every target: each GitOps environment and each NixHost (0.24.0).

``piceli targets`` and the UI's Targets view build the same document,
``piceli.targets.v1``, with :func:`build`, from:

- the GitOps controller's published status (the body of ``piceli gitops
  status``), read once; ``None`` with the reason when it cannot be read;
- each NixHost's agent status, read over SSH in parallel with a timeout
  (:func:`read_hosts`); the in-cluster UI cannot reach hosts and shows what
  the controller knows of their releases instead;
- the targets a composition or infra module declares (:func:`declared`),
  so a target the controller does not report still has a row.

One row per target: ``kind`` (``environment`` or ``nix-host``), ``name``,
``where``, the deployed ``revision``, ``pinned`` (the product commit it
runs), ``checks``, ``state``, ``as_of`` and ``facts`` (which source told
each fact: ``controller`` or ``host-agent``). One unreachable source never
fails the view: its rows say ``unreachable`` with the reason, and
:func:`exit_code` is ``1`` when any row failed, rolled back or was
unreachable, or its checks failed.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "BAD_STATES",
    "SCHEMA",
    "STATES",
    "build",
    "declared",
    "exit_code",
    "read_hosts",
    "table",
]

SCHEMA = "piceli.targets.v1"
#: A row's ``state``.
STATES = (
    "deployed",
    "deploying",
    "waiting",  # an approval, or a new build not seen yet
    "held",  # pinned or rolled back by the owner, or held by the controller
    "stopped",
    "failed",
    "rolled-back",
    "unreachable",
    "unknown",  # the source that knows was not read
)
#: The states that make :func:`exit_code` ``1``.
BAD_STATES = frozenset({"failed", "rolled-back", "unreachable"})
CHECK_STATES = ("passed", "failed", "running", "none")
#: The keys of a release's ``pinned`` (the promotion gate writes them).
PINNED_KEYS = ("input", "commit", "environment", "verified_run", "verified_at")
_ENV_STATES = {
    "deployed": "deployed",
    "pending": "deploying",
    "retrying": "deploying",
    "deleting": "deploying",
    "approval-required": "waiting",
    "held": "held",
    "stopped": "stopped",
    "failed": "failed",
    "interrupted": "failed",
    "unreachable": "unreachable",
}
_ROLLED_BACK = "checks-failed-rolled-back"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


# ------------------------------------------------------------------ inputs


def declared(values: Iterable[Any]) -> dict[str, Any]:
    """The targets among a module's values: ``environments`` (name to
    ``cluster``, ``namespace`` and ``sources``), ``hosts`` (name to
    :class:`~piceli.hosts.NixHost`) and ``releases`` (the hosts a
    :class:`~piceli.infra.NixRelease` publishes)."""
    from piceli.envs.model import Environment
    from piceli.hosts.model import NixHost
    from piceli.infra.nix_releases import NixRelease

    found: dict[str, Any] = {"environments": {}, "hosts": {}, "releases": set()}
    items = list(values)
    for value in items:
        if isinstance(value, list | tuple):
            items.extend(value)
            continue
        if isinstance(value, Environment):  # a branch rule's are the controller's
            found["environments"][value.name] = {
                "cluster": getattr(value.cluster, "name", None),
                "namespace": value.namespace,
                "sources": sorted(str(source.key) for source, _ in value.sources),
            }
        elif isinstance(value, NixRelease):
            found["hosts"].setdefault(value.host.name, value.host)
            found["releases"].add(value.host.name)
        elif isinstance(value, NixHost):
            found["hosts"].setdefault(value.name, value)
    return found


def read_hosts(
    reads: Mapping[str, Callable[[], Mapping[str, Any]]], timeout: float
) -> dict[str, dict[str, Any]]:
    """Run each host's read in parallel; give up on any after ``timeout``
    seconds. Each name maps to ``{"status": …}`` or ``{"reason": code,
    "detail": text}`` (never an exception)."""
    from piceli.hosts.model import HostError

    if not reads:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(16, len(reads)))
    try:
        futures = {name: pool.submit(read) for name, read in reads.items()}
        wait(futures.values(), timeout=timeout)
        found: dict[str, dict[str, Any]] = {}
        for name, future in futures.items():
            if not future.done():
                found[name] = {"reason": "host-unreachable",
                               "detail": f"no answer within {timeout:g}s"}  # fmt: skip
                continue
            try:
                found[name] = {"status": dict(future.result())}
            except HostError as error:
                found[name] = {"reason": error.code, "detail": str(error)}
            except Exception as error:  # a read never fails the view
                found[name] = {"reason": "host-unreachable",
                               "detail": type(error).__name__}  # fmt: skip
        return found
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------- rows


def _row(kind: str, name: str, where: str | None, source: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": name,
        "where": where,
        "revision": None,
        "pinned": None,
        "checks": {"state": "none", "at": None, "passed": None, "total": None},
        "state": "unknown",
        "reason": None,
        "detail": None,
        "as_of": None,
        "facts": dict.fromkeys(("revision", "pinned", "checks", "state"), source),
    }


def _where(cluster: Any, namespace: Any) -> str | None:
    parts = [str(item) for item in (cluster, namespace) if item]
    return "/".join(parts) or None


def _env_checks(record: Mapping[str, Any]) -> dict[str, Any]:
    progress = _dict(record.get("in_progress"))
    if progress.get("stage") == "checks":
        at = progress.get("stage_since") or progress.get("since")
        return {"state": "running", "at": at, "passed": None, "total": None}
    verification = _dict(record.get("verification"))
    if record.get("health") == "degraded" and verification.get("failed"):
        return {"state": "failed", "at": verification.get("at"),
                "passed": None, "total": None}  # fmt: skip
    checks = _dict(record.get("checks"))
    state = checks.get("state") if checks.get("state") in CHECK_STATES else "none"
    return {"state": state, "at": checks.get("at"),
            "passed": checks.get("passed"), "total": checks.get("total")}  # fmt: skip


def _product(name: str, commits: Mapping[str, str], gates: Mapping[str, Any]) -> Any:
    """The product source of environment ``name``: the input a promotion
    gate verified in it; else its only source; else ``None``."""
    gate = gates.get(name)
    if gate is not None and gate.get("input") in commits:
        return gate["input"]
    return next(iter(commits)) if len(commits) == 1 else None


def _env_row(
    name: str,
    record: Mapping[str, Any],
    gates: Mapping[str, Any],
    as_of: Any,
    cluster: Any = None,
) -> dict[str, Any]:
    row = _row("environment", name, _where(cluster or record.get("cluster"),
                                           record.get("namespace")), "controller")  # fmt: skip
    state = str(record.get("state") or "")
    deployed = _dict(record.get("deployed_revision")) or (
        _dict(record.get("revision")) if state == "deployed" else {}
    )
    commits = {str(k): str(v) for k, v in sorted(deployed.items()) if v}
    product = _product(name, commits, gates)
    commit = commits.get(product) if product else record.get("deployed_commit")
    row["revision"] = {"commit": commit, "tag": None, "commits": commits}
    row["pinned"] = {"source": product, "commit": commits.get(product) if product
                     else None, "commits": commits}  # fmt: skip
    row["checks"] = _env_checks(record)
    row["state"] = _ENV_STATES.get(state, "unknown")
    if row["state"] == "failed" and record.get("reason") == _ROLLED_BACK:
        row["state"] = "rolled-back"
    if record.get("in_progress") and row["state"] in ("deployed", "unknown"):
        row["state"] = "deploying"
    row["reason"] = record.get("reason")
    row["as_of"] = record.get("last_contact") if state == "unreachable" else as_of
    return row


def _gate(release: Mapping[str, Any]) -> dict[str, Any] | None:
    """A release's ``pinned`` (the promotion gate's), else ``None``."""
    for value in (release.get("pinned"), _dict(release.get("published")).get("pinned")):
        if isinstance(value, Mapping) and value.get("commit"):
            return {key: value.get(key) for key in PINNED_KEYS}
    return None


def _release_view(release: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state": release.get("state"),
        "tag": release.get("published_tag"),
        "commit": release.get("published_commit"),
        "at": release.get("finished_at"),
        "latest_tag": release.get("tag"),
    }


def _host_state(status: Mapping[str, Any]) -> tuple[str, Any]:
    from piceli.hosts.remote import matches

    current = _dict(status.get("current"))
    hold = _dict(status.get("hold"))
    attempt = _dict(status.get("last_attempt"))
    if status.get("in_progress"):
        return "deploying", None
    if hold.get("kind"):
        return "held", "host-pinned" if hold["kind"] == "pin" else "host-rolled-back"
    tried = attempt.get("commit") or attempt.get("tag")
    if tried and not matches(current, str(tried)):
        if attempt.get("state") == "rolled-back":
            return "rolled-back", attempt.get("reason") or "host-rolled-back"
        if attempt.get("state") in ("failed", "rejected"):
            return "failed", attempt.get("reason") or "host-deploy-failed"
    return "deployed", None


def _host_checks(status: Mapping[str, Any]) -> dict[str, Any]:
    progress = _dict(status.get("in_progress"))
    for stage in progress.get("stages") or ():
        if isinstance(stage, Mapping) and stage.get("name") == "checks":
            if stage.get("state") == "running":
                return {"state": "running", "at": stage.get("started_at"),
                        "passed": None, "total": None}  # fmt: skip
    attempt = _dict(status.get("last_attempt"))
    results = [
        item for item in attempt.get("checks") or () if isinstance(item, Mapping)
    ]
    if not results:
        return {"state": "none", "at": None, "passed": None, "total": None}
    passed = sum(1 for item in results if item.get("state") == "passed")
    return {
        "state": "passed" if passed == len(results) else "failed",
        "at": attempt.get("ended_at"),
        "passed": passed,
        "total": len(results),
    }


def _host_row(
    name: str,
    where: str | None,
    read: Mapping[str, Any] | None,
    release: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """A NixHost: its agent's facts when ``read`` holds its status; else
    what the controller published of its release."""
    status = _dict((read or {}).get("status"))
    gate = _gate(release) if release is not None else None
    view = _release_view(release) if release is not None else None
    if not status:
        row = _row("nix-host", name, where, "controller")
        if view is not None and view["commit"]:
            row["revision"] = {"commit": view["commit"], "tag": view["tag"]}
        row["pinned"] = gate
        row["as_of"] = view["at"] if view else None
        if read is not None:
            row["state"] = "unreachable"
            row["reason"], row["detail"] = read.get("reason"), read.get("detail")
            row["facts"]["state"] = row["facts"]["checks"] = "host-agent"
        row["release"] = view
        return row
    row = _row("nix-host", name, where, "host-agent")
    current = _dict(status.get("current"))
    row["revision"] = {"commit": current.get("commit"), "tag": current.get("tag")}
    own = current.get("pinned")
    if isinstance(own, Mapping) and own.get("commit"):
        row["pinned"] = {key: own.get(key) for key in PINNED_KEYS}
    elif (
        gate is not None
        and view is not None
        and current.get("commit")
        in (
            view["commit"],
            (release or {}).get("commit"),
        )
    ):
        row["pinned"], row["facts"]["pinned"] = gate, "controller"
    row["checks"] = _host_checks(status)
    row["state"], row["reason"] = _host_state(status)
    row["as_of"] = _dict(status.get("poll")).get("at") or _dict(
        status.get("last_attempt")
    ).get("ended_at")
    row["release"] = view
    return row


# ----------------------------------------------------------------- document


def build(
    controller: Mapping[str, Any] | None,
    *,
    controller_reason: str | None = None,
    controller_detail: str | None = None,
    targets: Mapping[str, Any] | None = None,
    hosts: Mapping[str, Mapping[str, Any]] | None = None,
    where: Mapping[str, str | None] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """The ``piceli.targets.v1`` document.

    :param controller: ``piceli gitops status``'s body, or ``None`` when it
        could not be read (``controller_reason`` says why).
    :param targets: :func:`declared` of the modules, or ``None`` (the
        controller's targets only).
    :param hosts: :func:`read_hosts`'s result; ``None`` when hosts are not
        read (the in-cluster UI): their rows show the controller's release.
    :param where: Each host's SSH address.
    """
    targets = targets or {"environments": {}, "hosts": {}, "releases": set()}
    status = _dict(controller)
    meta = _dict(status.get("controller"))
    as_of = meta.get("heartbeat_at") or meta.get("last_poll")
    releases = {
        str(key): _dict(value)
        for key, value in _dict(status.get("nix_releases")).items()
    }
    gates = {
        str(gate["environment"]): gate
        for gate in (_gate(release) for release in releases.values())
        if gate is not None and gate.get("environment")
    }
    rows: list[dict[str, Any]] = []
    envs = {str(k): _dict(v) for k, v in _dict(status.get("envs")).items()}
    for name, record in sorted(envs.items()):
        clusters = _dict(record.get("clusters"))
        if not clusters:
            home = _dict(_dict(targets.get("environments")).get(name)).get("cluster")
            rows.append(_env_row(name, record, gates, as_of, home))
        for cluster, view in sorted(clusters.items()):
            rows.append(_env_row(name, _dict(view), gates, as_of, cluster))
    for name, item in sorted(_dict(targets.get("environments")).items()):
        if name in envs:
            continue
        row = _row("environment", name, _where(item.get("cluster"),
                                               item.get("namespace")), "controller")  # fmt: skip
        if controller is None:
            row["state"] = "unreachable"
            row["reason"], row["detail"] = controller_reason, controller_detail
        rows.append(row)
    names = sorted(set(_dict(targets.get("hosts"))) | set(releases) | set(hosts or {}))
    for name in names:
        read = None if hosts is None else hosts.get(name)
        release = releases.get(name)
        if release is None and controller is None and name in targets["releases"]:
            release = {}
        row = _host_row(name, (where or {}).get(name), read, release)
        if release == {} and row["state"] == "unknown":
            row["state"], row["reason"] = "unreachable", controller_reason
        rows.append(row)
    bad = [
        row for row in rows
        if row["state"] in BAD_STATES or row["checks"]["state"] == "failed"
    ]  # fmt: skip
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "schema": SCHEMA,
        "generated_at": now or _now(),
        "state": "failed" if bad else "ok",
        "controller": {
            "read": controller is not None,
            "health": status.get("health") if controller is not None else None,
            "as_of": as_of,
            "reason": controller_reason,
            "detail": controller_detail,
        },
        "hosts_read": hosts is not None,
        "counts": dict(sorted(counts.items())),
        "targets": rows,
    }


def exit_code(document: Mapping[str, Any]) -> int:
    """``0`` when every target is reachable and none failed; else ``1``."""
    return 1 if document.get("state") == "failed" else 0


def _short(value: Any) -> str:
    return str(value)[:12] if value else "-"


def table(document: Mapping[str, Any]) -> list[str]:
    """The compact human table: one line per target."""
    head = ("KIND", "NAME", "WHERE", "REVISION", "PINNED", "CHECKS", "STATE", "AS OF")
    lines = [head]
    for row in document.get("targets") or ():
        revision = _dict(row.get("revision"))
        pinned = _dict(row.get("pinned"))
        rev = _short(revision.get("commit"))
        if revision.get("tag"):
            rev = f"{revision['tag']} {rev}"
        elif not revision.get("commit") and revision.get("commits"):
            rev = f"{len(revision['commits'])} sources"
        pin = "-"
        if not pinned.get("commit") and pinned.get("commits"):
            pin = ",".join(f"{k}@{str(v)[:7]}" for k, v in pinned["commits"].items())
        elif pinned.get("commit"):
            label = pinned.get("source") or pinned.get("input")
            pin = (
                f"{label}@{_short(pinned['commit'])[:7]}"
                if label
                else _short(pinned["commit"])
            )
        checks = _dict(row.get("checks"))
        mark = str(checks.get("state") or "none")
        if checks.get("total"):
            mark += f" {checks.get('passed')}/{checks.get('total')}"
        state = str(row.get("state"))
        if row.get("reason"):
            state += f" ({row['reason']})"
        lines.append((str(row.get("kind")), str(row.get("name")),
                      str(row.get("where") or "-"), rev, pin, mark, state,
                      str(row.get("as_of") or "-")))  # fmt: skip
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    return [
        "  ".join(
            cell.ljust(width) for cell, width in zip(line, widths, strict=True)
        ).rstrip()
        for line in lines
    ]
