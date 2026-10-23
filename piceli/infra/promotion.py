"""The promotion gate of a NixHost release (0.24.0).

A :class:`~piceli.infra.NixRelease` with ``gate=Promotion(input="app",
environment="main")`` publishes only a commit of its flake whose
``flake.lock`` pins the input ``app`` to a commit the GitOps environment
``main`` of the same composition ran with its release checks passed::

    from piceli.infra import NixRelease, Promotion

    web_release = NixRelease(
        web, source=hosts, builder="mini",
        gate=Promotion(input="app", environment="main"),
    )

The gate runs where a release is planned: the controller's release plan
(it reads ``flake.lock`` from its mirror of the source and its own history)
and a hand-run ``piceli dev nix build --publish ... --release HOST
--composition MODULE`` (``flake.lock`` of the local commit, the history the
composition's controller publishes, reached as ``piceli gitops status``
reaches it). Its result, :func:`verify`'s ``pinned``, goes into the plan
(the approval covers it), the signed build manifest and ``piceli gitops
status``. :func:`newest` is what ``piceli promote`` pins.

A run qualifies when it is a deploy run (``kind`` ``run``) of the
environment in ``state`` ``deployed`` (not degraded, failed or rolled back),
whose checks ran (at least one) and all passed, without a rollback, and
whose ``sources`` name the gated source's commit. An environment without
checks never qualifies.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The file whose input the gate reads, at the root of the flake's source.
LOCK_FILE = "flake.lock"
_INPUT = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]{0,99}")
_ENV = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_REV = re.compile(r"[0-9a-f]{40}")


class PromotionError(Exception):
    """A refused gate: ``code`` is registered, ``detail`` is plain data."""

    def __init__(
        self, code: str, message: str, detail: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


def _invalid(message: str) -> Exception:
    from piceli.infra import CompositionError

    return CompositionError("composition-invalid", message)


@dataclass(frozen=True)
class Promotion:
    """Publish only what a GitOps environment verified.

    :param input: The flake input (its name in ``flake.lock``, e.g. ``app``)
        whose locked commit must be verified.
    :param environment: The environment of the same composition whose
        deploy runs with passed release checks qualify a commit.
    :param source: The :class:`~piceli.infra.Source` (or its name) the
        environment follows for that input (default: the source named like
        the input); kept as its name.
    """

    input: str
    environment: str
    source: Any = None

    def __post_init__(self) -> None:
        from piceli.infra import Source

        if not isinstance(self.input, str) or not _INPUT.fullmatch(self.input):
            raise _invalid("Promotion(input=) is a flake input's name")
        if not isinstance(self.environment, str) or not _ENV.fullmatch(
            self.environment
        ):
            raise _invalid("Promotion(environment=) is an environment's name")
        if self.source is not None and not isinstance(self.source, Source | str):
            raise _invalid("Promotion(source=) is a piceli.infra.Source or its name")
        object.__setattr__(self, "source", self.source_key)

    @property
    def source_key(self) -> str:
        """The name of the source in the environment's runs."""
        if self.source is None:
            return self.input
        return self.source if isinstance(self.source, str) else str(self.source.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "environment": self.environment,
            "source": self.source_key,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Promotion:
        return cls(
            str(value["input"]),
            str(value["environment"]),
            str(value.get("source") or value["input"]),
        )


# ---------------------------------------------------------------- flake.lock


def lock_node(lock: Mapping[str, Any], name: str) -> tuple[str, dict[str, Any]]:
    """The node key and node of the root input ``name`` of a parsed lock.

    :raises PromotionError: ``promotion-input-missing``."""
    nodes = lock.get("nodes") if isinstance(lock, Mapping) else None
    root = (nodes or {}).get(str(lock.get("root") or "root")) if nodes else None
    inputs = (root or {}).get("inputs") or {}
    key = inputs.get(name) if isinstance(inputs, Mapping) else None
    node = (nodes or {}).get(key) if isinstance(key, str) else None
    if not isinstance(node, dict) or not isinstance(node.get("locked"), dict):
        raise PromotionError(
            "promotion-input-missing",
            f"{LOCK_FILE} has no locked root input {name!r}",
            {"input": name},
        )
    return key, node  # type: ignore[return-value]


def locked_rev(lock: bytes | str | None, name: str) -> str:
    """The commit ``flake.lock`` pins the input ``name`` to.

    :raises PromotionError: ``promotion-input-missing`` (no lock, no such
        input, or it pins no Git commit)."""
    if lock is None:
        raise PromotionError(
            "promotion-input-missing", f"the commit has no {LOCK_FILE}", {"input": name}
        )
    try:
        parsed = json.loads(lock)
    except ValueError:
        raise PromotionError(
            "promotion-input-missing", f"{LOCK_FILE} is not JSON", {"input": name}
        ) from None
    _, node = lock_node(parsed, name)
    rev = node["locked"].get("rev")
    if not isinstance(rev, str) or not _REV.fullmatch(rev):
        raise PromotionError(
            "promotion-input-missing",
            f"{LOCK_FILE} pins input {name!r} to no Git commit (locked.rev)",
            {"input": name},
        )
    return rev


# ---------------------------------------------------------------- history


def _passed(run: Mapping[str, Any], source: str) -> str | None:
    """The source's commit of a run that qualifies, else ``None``."""
    if run.get("kind", "run") != "run" or run.get("state") != "deployed":
        return None
    checks = run.get("checks")
    if not isinstance(checks, Mapping) or checks.get("passed") is not True:
        return None
    results = [
        item for item in checks.get("results") or () if isinstance(item, Mapping)
    ]
    if not results or not all(item.get("passed") is True for item in results):
        return None
    if checks.get("rollback") or run.get("failure"):
        return None
    found = ((run.get("sources") or {}).get(source) or {}).get("commit")
    return found if isinstance(found, str) and _REV.fullmatch(found) else None


def passed_runs(history: Any, gate: Promotion) -> list[dict[str, Any]]:
    """The environment's qualifying runs, newest first: ``commit``,
    ``run_id`` and ``verified_at`` (when the run finished).

    :raises PromotionError: ``promotion-history-unreachable`` when
        ``history`` is no ``piceli.gitops-history.v1`` document."""
    from piceli.gitops.history import SCHEMA

    if not isinstance(history, Mapping) or history.get("schema") != SCHEMA:
        raise PromotionError(
            "promotion-history-unreachable",
            "the controller's history could not be read",
            {"environment": gate.environment},
        )
    env = (history.get("envs") or {}).get(gate.environment) or {}
    found = []
    for run in env.get("runs") or ():
        if not isinstance(run, Mapping):
            continue
        commit = _passed(run, gate.source_key)
        if commit is not None:
            found.append(
                {
                    "commit": commit,
                    "run_id": str(run.get("run_id") or run.get("id") or ""),
                    "verified_at": run.get("finished_at") or run.get("started_at"),
                }
            )
    found.sort(key=lambda item: str(item["verified_at"] or ""), reverse=True)
    return found


def newest(history: Any, gate: Promotion) -> dict[str, Any] | None:
    """The newest qualifying run (what ``piceli promote`` pins), or ``None``."""
    runs = passed_runs(history, gate)
    return runs[0] if runs else None


def pinned(gate: Promotion, run: Mapping[str, Any]) -> dict[str, Any]:
    """The ``pinned`` field of a plan, a manifest and the status."""
    return {
        "input": gate.input,
        "commit": run["commit"],
        "environment": gate.environment,
        "verified_run": run["run_id"],
        "verified_at": run["verified_at"],
    }


def verify(
    gate: Promotion, rev: str, history: Any, *, host: str | None = None
) -> dict[str, Any]:
    """``pinned`` of ``rev`` (the newest qualifying run of it).

    :raises PromotionError: ``promotion-not-verified`` with the newest
        qualifying commit as a hint; ``promotion-history-unreachable``."""
    runs = passed_runs(history, gate)
    for run in runs:
        if run["commit"] == rev:
            return pinned(gate, run)
    best = runs[0] if runs else None
    hint = (
        f"; the newest it passed is {best['commit'][:12]} (run {best['run_id']})"
        + (f": piceli promote {host} --from {gate.environment} pins it" if host else "")
        if best
        else "; it has no run with passed release checks"
    )
    raise PromotionError(
        "promotion-not-verified",
        f"input {gate.input} pins {rev[:12]}, which environment "
        f"{gate.environment} has not run with its release checks passed{hint}",
        {
            "input": gate.input,
            "commit": rev,
            "environment": gate.environment,
            "newest_passed": dict(best) if best else None,
        },
    )


def check_pinned(value: Any) -> dict[str, Any] | None:
    """A plan's ``pinned`` as a build host receives it (``None``: ungated).

    :raises ValueError: a malformed one."""
    if value is None:
        return None
    keys = {"input", "commit", "environment", "verified_run", "verified_at"}
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or not _REV.fullmatch(str(value.get("commit")))
    ):
        raise ValueError("the plan's pinned field is malformed")
    return dict(value)
