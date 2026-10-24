"""Previews of branches, on request (0.28.0).

A composition's branch rule (``Environment.per_branch(...)``) gives every
matching branch an environment. ``piceli env up BRANCH --cluster ...``
records a ``preview`` request; the GitOps controller wakes for it (no wait
for the next poll), reads the remotes, and deploys the branch's head at
once, taking the images a merge train or a ``Step.images`` push offered for
that commit instead of building them. A branch outside the rule's patterns
gets a preview too, on request only. ``piceli env down BRANCH`` tears it
down; it stays down until the branch's next push (a branch on request only
stays down).

``Environment.per_branch(down_when_merged=True)`` also tears down a branch
whose own commits reached the fallback branch (``main``): its head was not
in ``main`` when first seen, and now is. A squash or rebase merge leaves the
head out of ``main``: delete the branch then (most Git hosts can do it on
merge), which always tears its environment down.

This module keeps the requests' bookkeeping in the controller's state
(``state["previews"]``): the branches up on request, the branches held down
(and why), the requests served, and each branch's head seen and whether it
was ahead of ``main``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from piceli.gitops import GitOpsError

#: The controller state's key.
STATE_KEY = "previews"
#: How many served requests the status keeps (``env up --wait`` reads them).
KEEP_SERVED = 50
#: The request kinds that wake the controller's run loop at once.
WAKE_KINDS = frozenset(
    {"preview", "sync", "approve", "promote", "build", "start", "stop"}
)

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def check_branch(branch: Any) -> str:
    """A Git branch name a preview request may carry (``gitops-request-invalid``)."""
    if (
        not isinstance(branch, str)
        or not _BRANCH.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith((".", "/", ".lock"))
    ):
        raise GitOpsError(
            "gitops-request-invalid",
            "the branch is not a Git branch name (letters, digits, . _ / -)",
        )
    return branch


def _state(state: dict[str, Any]) -> dict[str, Any]:
    found = state.setdefault(STATE_KEY, {})
    for key in ("up", "down", "heads"):
        found.setdefault(key, {})
    found.setdefault("served", [])
    return found


def on_request(state: Mapping[str, Any]) -> set[str]:
    """The branches up on request (outside the rule's patterns)."""
    return set((state.get(STATE_KEY) or {}).get("up") or {})


def held_down(state: Mapping[str, Any], branch: str, head: str | None) -> bool:
    """Whether ``branch`` at ``head`` is held down (``env down`` or merged)."""
    entry = ((state.get(STATE_KEY) or {}).get("down") or {}).get(branch)
    return entry is not None and entry.get("commit") == head


def down_reason(state: Mapping[str, Any], branch: str) -> str | None:
    entry = ((state.get(STATE_KEY) or {}).get("down") or {}).get(branch)
    return None if entry is None else str(entry.get("reason"))


def serve(state: dict[str, Any], key: str) -> None:
    served = _state(state)["served"]
    if key not in served:
        served.append(key)
    del served[:-KEEP_SERVED]


def up(state: dict[str, Any], branch: str, *, listed: bool, at: str) -> None:
    """Bring ``branch`` up: no longer held down; kept on request when the
    rule's patterns do not list it."""
    previews = _state(state)
    previews["down"].pop(branch, None)
    if not listed:
        previews["up"][branch] = {"at": at}


def down(
    state: dict[str, Any], branch: str, head: str | None, *, reason: str, at: str
) -> None:
    """Hold ``branch`` down at ``head`` (until its next push)."""
    previews = _state(state)
    previews["up"].pop(branch, None)
    previews["down"][branch] = {"commit": head, "reason": reason, "at": at}


def forget(state: dict[str, Any], heads: Mapping[str, str]) -> None:
    """Drop what no longer applies: a branch gone, or a held branch that moved.

    :param heads: Every branch head of the sources the rule follows with
        ``"{branch}"`` (a complete view only).
    """
    previews = state.get(STATE_KEY)
    if not previews:
        return
    for branch in list(previews.get("up") or {}):
        if branch not in heads:
            del previews["up"][branch]
    for branch, entry in list((previews.get("down") or {}).items()):
        if heads.get(branch) != entry.get("commit"):
            del previews["down"][branch]
    for branch, entry in list((previews.get("heads") or {}).items()):
        if heads.get(branch) != entry.get("commit"):
            del previews["heads"][branch]


def merged(
    state: dict[str, Any],
    branches: Iterable[str],
    heads: Mapping[str, str],
    main: str | None,
    contained: Callable[[str, str], bool],
) -> list[str]:
    """The branches whose own commits reached ``main`` since first seen.

    A head is checked once when first seen (``ahead``: not in ``main``);
    afterwards, while it does not move, every time ``main`` moves.
    ``contained(commit, main)`` answers through the source's mirror and may
    raise :class:`GitOpsError` (that branch is checked again next poll).
    """
    if main is None:
        return []
    seen = _state(state)["heads"]
    found: list[str] = []
    for branch in branches:
        head = heads.get(branch)
        if head is None:
            continue
        entry = seen.get(branch)
        try:
            if entry is None or entry.get("commit") != head:
                seen[branch] = {
                    "commit": head,
                    "ahead": head != main and not contained(head, main),
                    "main": main,
                }
                continue
            if not entry.get("ahead") or entry.get("main") == main:
                continue
            entry["main"] = main
            if contained(head, main):
                found.append(branch)
        except GitOpsError:
            continue
    return found


def url(template: str | None, branch_slug: str, namespace: str) -> str | None:
    """The preview's URL from ``Environment.per_branch(url=...)``."""
    if not template:
        return None
    return template.format(branch=branch_slug, namespace=namespace)


def public(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """The status's ``controller.previews`` (only when any)."""
    previews = state.get(STATE_KEY) or {}
    body = {
        "on_request": sorted(previews.get("up") or {}),
        "down": {
            branch: {k: entry.get(k) for k in ("commit", "reason", "at")}
            for branch, entry in sorted((previews.get("down") or {}).items())
        },
        "served": list(previews.get("served") or ()),
    }
    return body if any(body.values()) else None
