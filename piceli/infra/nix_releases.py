"""Nix host releases the GitOps controller publishes (0.25.0).

A composition module declares, next to its environments, which
:class:`~piceli.infra.NixHost` the controller releases, from which
:class:`~piceli.infra.Source` (the repository with the flake) and on which
build host::

    from piceli.infra import NixRelease

    web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=True)

When a tag matching the host's ``follow=Tag("web-v*")`` appears in that
source (the version after the prefix, :func:`piceli.hosts.agent.tag_version`,
higher than the last one), the controller plans the publish
(``piceli.nix-release-plan.v1``: the attribute, the commit and the tag, the
cache, the manifest and the tag's pointer) and applies the release's policy:
``auto_approve=True`` goes on, otherwise the release waits in
``approval-required`` until ``piceli gitops approve web-1 sha256:<digest>``.
Then it asks for the publish as a wanted build (``builds_wanted``, kind
``nix-publish``).

**The write keys stay on the build host.** ``piceli builder serve --host
mini --publish infra.py:cache`` claims the publish only for a cache it
holds the signing key (and S3 write keys) of, in its own Piceli secrets:
the claim carries a proof, the cache key's signature over the claim
(``piceli.nix-publish-claim.v1``), which the controller checks with the
cache's public key; never a key. The host fetches the source at the commit
with its own Git access, checks that the tag points at it, builds the
attribute as ``piceli dev nix build`` does, signs and pushes the closure,
uploads the signed manifest and the tag's pointer, and answers
``nix-published`` with the signed manifest and its stages. The controller
records the release only when the manifest is signed by the cache's key and
names the planned build, commit and tag. A host's failure (``build-failed``)
leaves the release ``failed`` with its tail until a newer tag or ``piceli
gitops sync``.

0.26.0: **private flake inputs.** The plan names the inputs of the commit's
``flake.lock`` the build host fetches into its own store before the build
(the gate's input, those whose repository is a ``Source`` of the
composition, ``NixRelease(inputs=)``) and the name of the build host's
credential for each, never a credential; the build host checks them against
its own checkout and fetches them with its own ``git-token`` s
(:mod:`piceli.dev.nix_inputs`), else answers ``build-failed`` with
``infra-nix-input-unreachable`` before any build.

Tags that exist when the controller first sees a release are its baseline
and publish nothing; ``piceli gitops sync web-1`` publishes the newest one.

0.26.0: **more triggers** (:mod:`piceli.infra.builds`). A host that
follows ``Branch("main")`` publishes each new head of the branch (the plan
names the branch and its pointer, no tag; the build host checks the commit
is on the branch). ``NixRelease(schedule="daily")`` (or a cron string)
plans the newest tag or head again at that time. ``piceli gitops build
web-1 --ref TAG|COMMIT`` (and the UI's "Build now") plans that ref. Every
one of them goes through the same gate, plan and approval; the record
names its ``trigger_kind`` and ``requested_by``. ``piceli gitops cancel
web-1`` drops a queued or running publish (``cancelled``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.gitops import GitOpsError

#: The release's state, reason and message while its cache's public key is
#: not declared (``NixCache(key_name=)`` alone): nothing is planned.
UNSET_REASON = "dev-nix-cache-key-unset"
KEY_UNSET = (
    "the cache has no public key yet: run piceli secrets setup MODULE and put "
    "the public key it prints into NixCache(public_key=)"
)
PLAN_SCHEMA = "piceli.nix-release-plan.v1"
CLAIM_SCHEMA = "piceli.nix-publish-claim.v1"
#: The kind of a wanted build that publishes a release.
KIND = "nix-publish"
#: Where the controller's state keeps the releases and their events.
STATE_KEY = "nix_releases"
HISTORY_KEY = "nix_release_history"
#: Events kept per release.
MAX_EVENTS = 20
#: Characters of a failed publish's output tail kept.
TAIL_CHARS = 8000
#: A release's ``state``.
STATES = (
    "idle",  # no matching tag yet
    "baseline",  # tags seen at first sight: nothing to publish
    "approval-required",
    "waiting-build",  # asked for; no build host holds it
    "publishing",  # a build host claimed it
    "published",
    "failed",
    "rejected",  # the promotion gate refused the tag's commit (0.25.0)
    "cancelled",  # piceli gitops cancel (0.26.0)
)
FAILED = "gitops-nix-release-failed"
#: 0.26.0: a hand-started build named no tag, branch or commit of the release.
REF_UNKNOWN = "gitops-build-ref-unknown"
#: 0.26.0: ``piceli gitops cancel`` dropped the publish.
CANCELLED = "gitops-build-cancelled"
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_REV = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _invalid(message: str) -> Exception:
    from piceli.infra import CompositionError

    return CompositionError("composition-invalid", message)


# ---------------------------------------------------------------- declaration


@dataclass(frozen=True)
class NixRelease:
    """A :class:`~piceli.infra.NixHost` whose releases the controller publishes.

    :param host: The host; it follows ``Tag(...)`` (its prefix and version
        order: see :class:`~piceli.infra.Tag`).
    :param source: The :class:`~piceli.infra.Source` holding the host's
        flake; the controller follows its tags.
    :param builder: The build host that builds and publishes it:
        ``piceli builder serve --host BUILDER --publish MODULE:ATTR`` with
        the cache's keys in its own Piceli secrets.
    :param auto_approve: Publish a new tag without waiting for ``piceli
        gitops approve``.
    :param gate: A :class:`~piceli.infra.Promotion`: publish only a commit
        whose ``flake.lock`` pins its input to a commit its environment ran
        with passed release checks (:mod:`piceli.infra.promotion`).
    :param inputs: 0.26.0: private flake inputs the build host fetches
        before the build, ``{input: credential}``: the name of the input in
        ``flake.lock`` and of a ``git-token`` in the build host's Piceli
        secrets. The gate's input and every input whose repository is a
        :class:`~piceli.infra.Source` of the composition are fetched first
        anyway, with the credential named here or ``git``
        (:mod:`piceli.dev.nix_inputs`).
    :param credentials: 0.26.0: whose Git credential fetches those inputs:
        ``"host"`` (default), the build host's own (``piceli secrets
        git-token`` or ``git-ssh-key`` there, named by ``inputs=``, else
        ``git``); ``"controller"``, the composition's (``piceli secrets
        git``), which the controller seals for the one build to the key the
        claiming host claimed it with, and which is used only for inputs on
        the hosts of the composition's sources.
    :param schedule: 0.26.0: plan the newest tag (or branch head) again on
        a schedule: ``hourly``, ``daily``, ``weekly`` or a cron string, UTC
        (:func:`piceli.infra.builds.parse_schedule`).
    """

    host: Any
    source: Any
    builder: str
    auto_approve: bool = False
    gate: Any = None
    inputs: Any = None
    credentials: str = "host"
    schedule: str | None = None

    def __post_init__(self) -> None:
        from piceli.hosts.model import Branch, NixHost, Tag
        from piceli.infra import Source

        if not isinstance(self.host, NixHost):
            raise _invalid("NixRelease(host=) is a piceli.infra.NixHost")
        if not isinstance(self.host.follow, Tag | Branch):
            raise _invalid(
                f"NixRelease {self.host.name}: the controller publishes tags or "
                "a branch's commits; follow=Tag(...) or Branch(...)"
            )
        if self.schedule is not None:
            from piceli.infra.builds import parse_schedule

            try:
                parse_schedule(self.schedule)
            except ValueError as error:
                raise _invalid(f"NixRelease {self.host.name}: {error}") from None
        if not isinstance(self.source, Source):
            raise _invalid("NixRelease(source=) is a piceli.infra.Source")
        if not isinstance(self.builder, str) or not _LABEL.fullmatch(self.builder):
            raise _invalid("NixRelease(builder=) is a build host's name")
        if not isinstance(self.auto_approve, bool):
            raise _invalid("NixRelease(auto_approve=) is True or False")
        from piceli.infra.promotion import Promotion

        if self.gate is not None and not isinstance(self.gate, Promotion):
            raise _invalid("NixRelease(gate=) is a piceli.infra.Promotion")
        from piceli.dev.nix_inputs import check_declared

        try:
            mapped = check_declared(
                self.inputs, f"NixRelease {self.host.name}: inputs="
            )
        except ValueError as error:
            raise _invalid(str(error)) from None
        object.__setattr__(self, "inputs", tuple(mapped.items()))
        from piceli.dev.nix_inputs import MODES

        if self.credentials not in MODES:
            raise _invalid(
                f"NixRelease {self.host.name}: credentials= is 'host' or 'controller'"
            )
        declared = getattr(self.host, "gate", None)
        if self.gate is None and declared is not None:
            object.__setattr__(self, "gate", declared)
        elif declared is not None and declared != self.gate:
            raise _invalid(
                f"NixRelease {self.host.name}: gate= differs from the host's gate"
            )

    @property
    def name(self) -> str:
        return str(self.host.name)

    def target(self) -> ReleaseTarget:
        """The release as plain data (what the controller's config holds).

        A cache declared with ``key_name=`` alone gives ``public_key=None``
        and ``key_unset`` (why): the composition still loads, and the
        controller rejects the release (``dev-nix-cache-key-unset``) without
        planning anything until the key is declared."""
        from piceli.dev.model import DevError

        cache = self.host.cache
        try:
            cache.trusted_key()
            unset = None
        except DevError as error:
            unset = str(error)
        follow = self.host.follow
        return ReleaseTarget(
            name=self.host.name,
            build=str(self.host.build),
            attribute=self.host.attribute,
            pattern=getattr(follow, "pattern", ""),
            prereleases=bool(getattr(follow, "prereleases", False)),
            branch=getattr(follow, "name", None),
            schedule=self.schedule,
            source=self.source,
            builder=self.builder,
            auto_approve=self.auto_approve,
            store_url=self.host.cache.store_url,
            public_key=cache.public_key,
            gate=self.gate,
            key_unset=unset,
            inputs=self.inputs,
            credentials=self.credentials,
        )


@dataclass(frozen=True)
class ReleaseTarget:
    """A :class:`NixRelease` as the controller knows it: no server, no secret."""

    name: str
    build: str
    attribute: str
    pattern: str
    prereleases: bool
    source: Any
    builder: str
    auto_approve: bool
    store_url: str
    #: ``None``: the cache names its key without it yet (``key_unset`` says so).
    public_key: str | None
    #: 0.25.0: the :class:`~piceli.infra.Promotion` gate, or ``None``.
    gate: Any = None
    #: Why the release cannot publish: its cache's public key is not declared.
    key_unset: str | None = None
    #: 0.26.0: the declared private inputs, ``((input, credential), ...)``.
    inputs: tuple[tuple[str, str], ...] = ()
    #: 0.26.0: ``host`` or ``controller`` (whose Git credential).
    credentials: str = "host"
    #: 0.26.0: the followed branch (``follow=Branch``; ``pattern`` is empty).
    branch: str | None = None
    #: 0.26.0: when to plan the newest tag or head again.
    schedule: str | None = None

    def to_dict(self) -> dict[str, Any]:
        gate = {"gate": self.gate.to_dict()} if self.gate is not None else {}
        # 0.26.0, only when declared: other configs (and hashes) unchanged.
        gate |= {"inputs": dict(self.inputs)} if self.inputs else {}
        gate |= {"credentials": self.credentials} if self.credentials != "host" else {}
        gate |= {"schedule": self.schedule} if self.schedule else {}
        follow = (
            {"kind": "branch", "name": self.branch}
            if self.branch is not None
            else {
                "kind": "tag",
                "pattern": self.pattern,
                "prereleases": self.prereleases,
            }
        )
        return gate | {
            "name": self.name,
            "build": self.build,
            "attribute": self.attribute,
            "follow": follow,
            "source": self.source.key,
            "builder": self.builder,
            "auto_approve": self.auto_approve,
            "cache": {"store_url": self.store_url, "public_key": self.public_key}
            | ({"key_unset": self.key_unset} if self.key_unset else {}),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], sources: Mapping[str, Any]) -> Any:
        from piceli.dev.model import DevError
        from piceli.dev.nix import check_public_key, installable
        from piceli.dev.nix_inputs import check_declared
        from piceli.hosts.model import Branch, HostError, Tag
        from piceli.infra.promotion import Promotion

        try:
            follow = value["follow"]
            branch = None
            if follow.get("kind") == "branch":  # 0.26.0
                branch = Branch(str(follow["name"])).name
                tag = Tag()
            else:
                tag = Tag(str(follow["pattern"]), bool(follow.get("prereleases")))
            schedule = value.get("schedule")
            if schedule is not None:
                from piceli.infra.builds import parse_schedule

                parse_schedule(schedule)
            declared = value["cache"]["public_key"]
            public = None if declared is None else check_public_key(str(declared))
            unset = str(value["cache"].get("key_unset") or KEY_UNSET)
            target = cls(
                name=str(value["name"]),
                build=str(value["build"]),
                attribute=installable(str(value["attribute"])),
                pattern="" if branch else tag.pattern,
                prereleases=False if branch else tag.prereleases,
                branch=branch,
                schedule=None if schedule is None else str(schedule),
                source=sources[str(value["source"])],
                builder=str(value["builder"]),
                auto_approve=value.get("auto_approve") is True,
                store_url=str(value["cache"]["store_url"]),
                public_key=public,
                gate=Promotion.from_dict(value["gate"]) if value.get("gate") else None,
                key_unset=None if public else unset,
                inputs=tuple(check_declared(value.get("inputs")).items()),
                credentials=str(value.get("credentials") or "host"),
            )
            if target.credentials not in ("host", "controller"):
                raise ValueError("credentials is host or controller")
        except (KeyError, TypeError, ValueError, HostError, DevError) as error:
            raise _invalid(f"a nix release is invalid: {error}") from None
        for label in (target.name, target.build, target.builder):
            if not _LABEL.fullmatch(label):
                raise _invalid(f"a nix release names {label!r}, not a DNS label")
        return target

    def version(self, tag: str) -> tuple[Any, ...] | None:
        """``tag``'s order under the pattern (``None``: not this release's,
        or a pre-release without ``prereleases``)."""
        from piceli.hosts.agent import tag_version

        if self.branch is not None:
            return None
        found = tag_version(self.pattern, tag)
        if found is None or (found[3] == 0 and not self.prereleases):
            return None
        return found

    def latest(self, tags: Mapping[str, str]) -> tuple[str, str] | None:
        """The newest ``(tag, commit)`` of this release among ``tags``."""
        ranked = [
            (version, tag) for tag in tags if (version := self.version(tag)) is not None
        ]
        if not ranked:
            return None
        tag = max(ranked)[1]
        return tag, str(tags[tag])

    def head(self, refs: Any) -> tuple[str | None, str] | None:
        """The newest ``(tag, commit)`` the release follows in ``refs`` (a
        source's tags and branches): its newest tag, or (0.26.0) its
        branch's head with no tag."""
        if self.branch is not None:
            commit = (getattr(refs, "branches", None) or {}).get(self.branch)
            return None if commit is None else (None, str(commit))
        return self.latest(getattr(refs, "tags", None) or {})

    def describe_follow(self) -> dict[str, Any]:
        if self.branch is not None:
            return {"kind": "branch", "name": self.branch}
        return {"kind": "tag", "pattern": self.pattern,
                "prereleases": self.prereleases}  # fmt: skip


def _position(target: ReleaseTarget, latest: tuple[str | None, str]) -> str:
    """What the release last followed: the tag, or (a branch) the head."""
    return str(latest[1] if target.branch is not None else latest[0])


def release_plan(
    target: ReleaseTarget,
    commit: str,
    tag: str | None,
    pinned: Mapping[str, Any] | None = None,
    inputs: Sequence[Mapping[str, str]] = (),
    hosts: Sequence[str] | None = None,
    *,
    branch: str | None = None,
) -> dict[str, Any]:
    """What publishing ``tag`` does; its digest (:func:`digest`) is what the
    owner approves and what the build host publishes, nothing else.
    ``pinned``: the promotion gate's result (only for a gated release).
    ``inputs``: 0.26.0, the private inputs the build host fetches first
    (:func:`piceli.dev.nix_inputs.covered`; only when there are some).
    ``branch``: 0.26.0, a branch's commit (its pointer; no tag)."""
    from piceli.dev.nix import manifest_key

    extra: dict[str, Any] = {"pinned": dict(pinned)} if pinned is not None else {}
    extra |= {"inputs": [dict(item) for item in inputs]} if inputs else {}
    if inputs and hosts is not None:  # 0.26.0: the controller's credential
        extra |= {"credentials": "controller", "git_hosts": list(hosts)}
    # 0.26.0: a branch's commit (its pointer), or a commit alone (none).
    extra |= {"branch": branch} if branch is not None else {}
    return extra | {
        "schema": PLAN_SCHEMA,
        "release": target.name,
        "attribute": target.attribute,
        "name": target.build,
        "commit": commit,
        "tag": tag,
        "source": target.source.key,
        "builder": target.builder,
        "cache": {"store_url": target.store_url, "public_key": target.public_key},
        "manifest": manifest_key(target.build, commit),
        "pointers": plan_pointers(target.build, tag, branch),
    }


def plan_pointers(build: str, tag: str | None, branch: str | None) -> list[str]:
    """The ref pointers a plan writes: its tag's and (0.26.0) its branch's."""
    from piceli.dev.nix import pointer_key

    return [
        pointer_key(build, kind, ref)
        for kind, ref in (("tag", tag), ("branch", branch))
        if ref is not None
    ]


def plan_refs(plan: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(ref, commit)`` of each pointer the build host answers for ``plan``."""
    return [
        (f"refs/{kind}/{ref}", str(plan["commit"]))
        for kind, ref in (("tags", plan.get("tag")), ("heads", plan.get("branch")))
        if ref is not None
    ]


def digest(plan: Mapping[str, Any]) -> str:
    from piceli.dev.nix import plan_digest

    return plan_digest(plan)


def check_plan(plan: Any) -> dict[str, Any]:
    """A plan a build host received, checked before it builds anything: its
    pointers are exactly the tag's of its build (a controller cannot make the
    host sign another build's pointers). :raises ValueError: otherwise."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import installable, manifest_key, ref_name_ok

    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("not a nix release plan")
    name, commit = (str(plan.get(k) or "") for k in ("name", "commit"))
    tag, branch = plan.get("tag"), plan.get("branch")
    try:
        installable(str(plan.get("attribute") or ""))
    except DevError:
        raise ValueError("the plan's attribute is not .#ATTR") from None
    if (
        not _LABEL.fullmatch(name)
        or not _REV.fullmatch(commit)
        or any(
            ref is not None and (not isinstance(ref, str) or not ref_name_ok(ref))
            for ref in (tag, branch)
        )
        or plan.get("manifest") != manifest_key(name, commit)
        or list(plan.get("pointers") or ()) != plan_pointers(name, tag, branch)
    ):
        raise ValueError("the plan names another build, commit, tag or pointer")
    from piceli.dev.nix_inputs import check_entries
    from piceli.infra.promotion import check_pinned

    check_pinned(plan.get("pinned"))
    check_entries(plan.get("inputs"))
    if plan.get("credentials", "host") not in ("host", "controller") or not all(
        isinstance(host, str) for host in plan.get("git_hosts") or ()
    ):
        raise ValueError("the plan's credentials field is malformed")
    return dict(plan)


def claim_message(build: str, host: str, recipient: str | None = None) -> bytes:
    """What a build host signs with the cache key to claim a publish
    (0.26.0: with the one-claim key the controller seals a credential to,
    for a ``controller`` release)."""
    from piceli.dev.nix_verify import canonical

    body = {"schema": CLAIM_SCHEMA, "id": build, "host": host}
    return canonical(body | ({"recipient": recipient} if recipient else {}))


# ---------------------------------------------------------------- controller


class Releases:
    """The controller's side: each release's record in its state, the
    wanted publish, the approval, the answer, the status and the history.

    :param state: The controller's state (its ``nix_releases`` and the
        wanted builds, :data:`piceli.infra.remote_builds.STATE_KEY`).
    """

    def __init__(
        self,
        state: dict[str, Any],
        *,
        clock: Callable[[], float],
        log: Callable[[str], None] = lambda _m: None,
        telemetry: Any = None,
    ) -> None:
        self.state = state
        self.clock = clock
        self.log = log
        self.telemetry = telemetry
        self._gate: Callable[[ReleaseTarget, str], dict[str, Any]] | None = None
        self._lock: Callable[[ReleaseTarget, str], bytes | None] | None = None
        self._sources: tuple[tuple[str, str], ...] = ()

    def records(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = self.state.setdefault(STATE_KEY, {})
        return found

    def _wanted(self) -> dict[str, Any]:
        from piceli.infra.remote_builds import STATE_KEY as WANTED

        found: dict[str, Any] = self.state.setdefault(WANTED, {})
        return found

    # ------------------------------------------------------------ poll
    def poll(
        self,
        targets: Sequence[ReleaseTarget],
        refs: Mapping[str, Any],
        *,
        gate: Callable[[ReleaseTarget, str], dict[str, Any]] | None = None,
        lock: Callable[[ReleaseTarget, str], bytes | None] | None = None,
        sources: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Follow each release's tags and move it on (one poll).

        ``gate(target, commit)`` checks a gated release's commit: the
        ``pinned`` field, or :class:`~piceli.infra.promotion.PromotionError`.
        A rejected tag is checked again on every poll, so it publishes once
        its environment verified the pinned commit. 0.26.0: ``lock(target,
        commit)`` reads the commit's ``flake.lock`` (``None``: none), whose
        private inputs (the gate's, those of ``sources``, the composition's
        ``(key, url)``, and the declared ones) the plan names."""
        self._gate = gate
        self._lock = lock
        self._sources = tuple(sources)
        records = self.records()
        forced = self.state.setdefault("nix_forced", {})
        names = {target.name for target in targets}
        for gone in [name for name in records if name not in names]:
            self._drop_wanted(records.pop(gone))
            forced.pop(gone, None)
        for target in targets:
            record = records.setdefault(target.name, {"state": "idle", "tag": None})
            if target.public_key is None:
                self._unset(target, record, refs.get(target.source.key))
                continue
            if record.get("reason") == UNSET_REASON:  # the key is declared now
                for key in ("reason", "refusal"):
                    record.pop(key, None)
                record["state"] = "idle"
            record.update(
                host=target.name,
                source=target.source.key,
                builder=target.builder,
                follow=target.describe_follow(),
                auto_approve=target.auto_approve,
                schedule=target.schedule,
            )
            found = refs.get(target.source.key)
            if found is not None:
                latest = target.head(found)
                record["latest_tag"] = latest[0] if latest else None
                record["latest_commit"] = latest[1] if latest else None
                self._scheduled(target, record, forced)
                why = forced.pop(target.name, None)
                if isinstance(why, Mapping):  # 0.26.0: started by hand
                    self._requested(target, record, found, latest, why)
                elif latest is not None:
                    if not record.get("seen") and why is None:
                        record.update(state="baseline", tag=latest[0], commit=latest[1])
                        record["followed"] = _position(target, latest)
                    elif (
                        why is not None
                        or self._newer(target, latest, record)
                        or (
                            record.get("state") == "rejected"
                            and record.get("tag") == latest[0]
                            and record.get("commit") == latest[1]
                        )
                    ):
                        auto = (
                            f"tag {latest[0]}"
                            if target.branch is None
                            else f"branch {target.branch}"
                        )
                        self._attempt(target, record, latest, why or auto,
                                      branch=target.branch)  # fmt: skip
                        record["followed"] = _position(target, latest)
                record["seen"] = True
            self._advance(target, record)

    def _unset(self, target: ReleaseTarget, record: dict[str, Any], found: Any) -> None:
        """The cache's public key is not declared: the release is
        ``rejected`` (``dev-nix-cache-key-unset``), nothing is planned or
        asked for, and its tags are not taken as seen (the first poll with
        the key takes them as the baseline)."""
        again = record.get("reason") == UNSET_REASON
        self._drop_wanted(record)
        for key in ("failure", "claimed", "approval", "stages", "approve_command",
                    "approval_required_since", "pinned"):  # fmt: skip
            record.pop(key, None)
        latest = target.latest(found.tags) if found is not None else None
        record.update(
            host=target.name,
            source=target.source.key,
            builder=target.builder,
            state="rejected",
            reason=UNSET_REASON,
            refusal={"message": target.key_unset or KEY_UNSET},
            plan=None,
            plan_digest=None,
            build_id=None,
            latest_tag=latest[0] if latest else None,
            latest_commit=latest[1] if latest else None,
        )
        if not again:
            self.log(f"nix release {target.name}: {target.key_unset or KEY_UNSET}")

    @staticmethod
    def _newer(
        target: ReleaseTarget, latest: tuple[str | None, str], record: Mapping[str, Any]
    ) -> bool:
        """Whether ``latest`` is past what the release last followed (its
        tag's version; 0.26.0: a branch's other head)."""
        if target.branch is not None:
            return latest[1] != (record.get("followed") or record.get("commit"))
        current = record.get("followed") or record.get("tag")
        if not isinstance(current, str):
            return True
        now, before = target.version(str(latest[0])), target.version(current)
        return now is not None and (before is None or now > before)

    def _scheduled(
        self, target: ReleaseTarget, record: dict[str, Any], forced: dict[str, Any]
    ) -> None:
        """``NixRelease(schedule=)`` (0.26.0): plan the newest tag or head
        again when a scheduled minute passed since the last poll."""
        if target.schedule is None:
            record.pop("schedule_checked_at", None)
            return
        from piceli.infra.builds import schedule_due

        now = self.clock()
        if schedule_due(
            target.schedule, _epoch(record.get("schedule_checked_at")), now
        ):
            forced.setdefault(target.name, "schedule")
            record["scheduled_at"] = _iso(now)
        record["schedule_checked_at"] = _iso(now)

    def _requested(
        self,
        target: ReleaseTarget,
        record: dict[str, Any],
        found: Any,
        latest: tuple[str | None, str] | None,
        why: Mapping[str, Any],
    ) -> None:
        """``piceli gitops build NAME --ref REF`` or the UI's "Build now"
        (0.26.0): plan that ref (a tag of the release, its branch, a commit;
        the newest without one) through the same gate and approval."""
        from piceli.gitops.state import remember_rejected
        from piceli.infra.builds import resolve_ref

        ref = str(why.get("ref") or "")
        via = str(why.get("via") or "cli")
        resolved = (
            resolve_ref(
                ref,
                tags=getattr(found, "tags", None) or {},
                branches=getattr(found, "branches", None) or {},
                tag_ok=lambda tag: target.version(tag) is not None,
                branch=target.branch,
            )
            if ref
            else (latest[0], target.branch, latest[1])
            if latest is not None
            else None
        )
        if resolved is None:
            remember_rejected(
                self.state,
                {
                    "request": f"build.{target.name}",
                    "kind": "build",
                    "reason": REF_UNKNOWN,
                    "message": f"nix release {target.name} has no tag, branch or "
                    f"commit {ref or '(newest)'}",
                    "at": _iso(self.clock()),
                },
            )
            self.log(f"nix release {target.name}: no ref {ref or '(newest)'} to build")
            return
        tag, branch, commit = resolved
        if isinstance(why.get("approve"), str):
            record["approved_digest"] = why["approve"]
        self._attempt(
            target,
            record,
            (tag, commit),
            f"{via} {ref or 'newest'}",
            kind=via,
            by=why.get("by"),
            branch=branch,
        )
        if latest is not None:  # the newest one does not replace it at once
            record["followed"] = _position(target, latest)

    def request(self, body: Mapping[str, Any]) -> bool:
        """A ``build`` request of a release (0.26.0); ``False``: ``NAME`` is
        no release. The next poll plans it (:meth:`_requested`)."""
        name = str(body.get("name") or "")
        if name not in self.records():
            return False
        self.state.setdefault("nix_forced", {})[name] = {
            "ref": body.get("ref"),
            "via": body.get("via") if body.get("via") in ("cli", "ui") else "cli",
            "by": body.get("by"),
            "approve": body.get("approve"),
        }
        return True

    def cancel(self, name: str, by: Any = None) -> bool:
        """``piceli gitops cancel NAME`` (0.26.0): drop a planned, queued or
        running publish (``False``: ``NAME`` is no release). A build host
        that holds it stops at its next stage; its answer is refused.

        :raises GitOpsError: ``gitops-build-not-running``."""
        record = self.records().get(name)
        if record is None:
            return False
        if record.get("state") not in (
            "pending", "approval-required", "waiting-build", "publishing",
        ):  # fmt: skip
            raise GitOpsError(
                "gitops-build-not-running",
                f"nix release {name} has no queued or running publish",
            )
        host = (record.get("claimed") or {}).get("host")
        self._drop_wanted(record)
        for key in ("approve_command", "approval_required_since"):
            record.pop(key, None)
        record["cancelled_by"] = str(by)[:120] if by else None
        self._finish(
            name,
            record,
            "cancelled",
            failure={"host": host, "reason": "cancelled", "log_tail": ""},
            reason=CANCELLED,
        )
        return True

    def _attempt(
        self,
        target: ReleaseTarget,
        record: dict[str, Any],
        latest: tuple[str | None, str],
        trigger: str,
        *,
        kind: str | None = None,
        by: Any = None,
        branch: str | None = None,
    ) -> None:
        tag, commit = latest
        pinned = None
        if target.gate is not None:
            from piceli.infra.promotion import PromotionError

            try:
                if self._gate is None:
                    raise PromotionError(
                        "promotion-history-unreachable", "no history to check"
                    )
                pinned = self._gate(target, commit)
            except PromotionError as error:
                self._refuse(target, record, latest, trigger, error)
                return
        self._drop_wanted(record)
        inputs = self._inputs(target, commit)
        hosts = None
        if target.credentials == "controller":
            from piceli.dev.nix_inputs import CONTROLLER, git_hosts

            hosts = git_hosts(self._sources)
            inputs = [item | {"credential": CONTROLLER} for item in inputs]
        plan = release_plan(target, commit, tag, pinned, inputs, hosts, branch=branch)
        approved = record.get("approved_digest")
        for key in ("failure", "claimed", "published_now", "approval", "stages",
                    "refusal"):  # fmt: skip
            record.pop(key, None)
        record.update(
            pinned=pinned,
            state="pending",
            tag=tag,
            commit=commit,
            trigger=trigger,
            # 0.26.0: what started it (piceli.infra.builds.TRIGGERS), who asked.
            trigger_kind=kind or trigger.split(" ", 1)[0],
            requested_by=str(by)[:120] if by else None,
            reason=None,
            plan=plan,
            plan_digest=digest(plan),
            requested_at=_iso(self.clock()),
            started_at=None,
            finished_at=None,
            seconds=None,
            build_id=None,
            approved_digest=approved,
        )
        self.log(
            f"nix release {target.name}: {trigger} -> "
            f"{tag or branch or 'commit'}@{commit[:12]}"
        )

    def _inputs(self, target: ReleaseTarget, commit: str) -> list[dict[str, str]]:
        """The private inputs the build host fetches first (0.26.0)."""
        from piceli.dev.nix_inputs import covered

        if self._lock is None:
            return []
        return covered(
            self._lock(target, commit),
            gate=target.gate.input if target.gate is not None else None,
            sources=self._sources,
            declared=dict(target.inputs),
        )

    def _refuse(
        self,
        target: ReleaseTarget,
        record: dict[str, Any],
        latest: tuple[str | None, str],
        trigger: str,
        error: Any,
    ) -> None:
        """The gate refused the tag's commit: nothing is planned or asked for;
        the refusal is remembered once (the history, the log)."""
        tag, commit = latest
        again = (
            record.get("state") == "rejected"
            and record.get("tag") == tag
            and record.get("reason") == error.code
        )
        self._drop_wanted(record)
        for key in ("failure", "claimed", "approval", "stages", "approve_command",
                    "approval_required_since"):  # fmt: skip
            record.pop(key, None)
        record.update(
            state="rejected",
            tag=tag,
            commit=commit,
            trigger=record.get("trigger") if again else trigger,
            reason=error.code,
            refusal={"message": str(error), **error.detail},
            pinned=None,
            plan=None,
            plan_digest=None,
            build_id=None,
        )
        if again:
            return
        now = _iso(self.clock())
        record.update(requested_at=now, started_at=None, finished_at=now, seconds=0.0)
        events = self.state.setdefault(HISTORY_KEY, {})
        events[target.name] = [
            *(events.get(target.name) or [])[-(MAX_EVENTS - 1) :],
            self.event(target.name, record),
        ]
        self.log(f"nix release {target.name}: {tag} refused ({error.code}): {error}")

    def _advance(self, target: ReleaseTarget, record: dict[str, Any]) -> None:
        now = self.clock()
        if record.get("state") == "pending":
            if target.auto_approve:
                record["approval"] = {"via": "policy", "at": _iso(now)}
                record["state"] = "waiting-build"
            elif record.get("approved_digest") == record.get("plan_digest"):
                record.setdefault("approval", {"via": "owner", "at": _iso(now)})
                record["state"] = "waiting-build"
            else:
                record.update(
                    state="approval-required",
                    approval_required_since=_iso(now),
                    approve_command=(
                        f"piceli gitops approve {target.name} {record['plan_digest']}"
                    ),
                )
                self.log(
                    f"nix release {target.name}: {record['tag']} waits for "
                    f"piceli gitops approve {target.name} {record['plan_digest']}"
                )
                return
        if record.get("state") not in ("waiting-build", "publishing"):
            return
        from piceli.infra.remote_builds import CLAIM_SECONDS, build_id

        wanted = self._wanted()
        plan = record["plan"]
        found = build_id(KIND, {"plan": plan, "digest": record["plan_digest"]})
        entry = wanted.get(found)
        if entry is None:
            entry = wanted[found] = {
                "id": found,
                "kind": KIND,
                "release": target.name,
                "builder": target.builder,
                "plan": plan,
                "digest": record["plan_digest"],
                "commits": {target.source.key: plan["commit"]},
                "urls": {target.source.key: target.source.url},
                "images": [],
                "platforms": [],
                "requested_at": _iso(now),
                "requested_epoch": now,
                "owner": f"nix:{target.name}",
            }
        entry["asked_epoch"] = now
        record["build_id"] = found
        if entry.get("failed"):
            failure = dict(entry["failed"])
            del wanted[found]
            self._finish(
                target.name,
                record,
                "failed",
                failure={
                    "host": failure.get("host"),
                    "reason": failure.get("reason"),
                    "log_tail": str(failure.get("log_tail") or "")[-TAIL_CHARS:],
                },
            )
            return
        claimed = entry.get("claimed") or {}
        if claimed and now - float(claimed.get("epoch") or 0) < CLAIM_SECONDS:
            record["state"] = "publishing"
            record["claimed"] = {"host": claimed.get("host"), "at": claimed.get("at")}
            record["started_at"] = record.get("started_at") or claimed.get("at")
        else:
            record["state"] = "waiting-build"
            record.pop("claimed", None)

    def _drop_wanted(self, record: Mapping[str, Any]) -> None:
        build = record.get("build_id")
        if isinstance(build, str):
            self._wanted().pop(build, None)

    # ------------------------------------------------------------ requests
    def entry(self, build: Any) -> dict[str, Any] | None:
        """The wanted publish ``build``, ``None`` for another kind of build."""
        found = self._wanted().get(str(build))
        return found if isinstance(found, dict) and found.get("kind") == KIND else None

    def approve(self, body: Mapping[str, Any]) -> bool:
        """``piceli gitops approve NAME DIGEST`` of a release (``False``:
        ``NAME`` is no release). :raises GitOpsError: a stale digest."""
        record = self.records().get(str(body.get("env")))
        if record is None:
            return False
        if record.get("state") != "approval-required" or record.get(
            "plan_digest"
        ) != body.get("plan_hash"):
            raise GitOpsError(
                "gitops-approval-stale",
                "the release is not waiting for that plan digest",
            )
        via = body.get("via") if body.get("via") in ("cli", "ui") else None
        record.update(
            state="pending",
            approved_digest=body["plan_hash"],
            approval={
                "via": via,
                "at": _iso(self.clock()),
                "requested_at": record.pop("approval_required_since", None),
            },
        )
        record.pop("approve_command", None)
        return True

    def sync(self, name: str | None) -> bool:
        """``piceli gitops sync [NAME]``: publish ``NAME``'s newest tag now (a
        baseline, the same tag again); without a name, retry the failed ones.
        ``False``: ``NAME`` is no release."""
        records = self.records()
        forced = self.state.setdefault("nix_forced", {})
        if name is None:
            for key, record in records.items():
                if record.get("state") == "failed":
                    forced[key] = "sync"
            return True
        if name not in records:
            return False
        forced[name] = "sync"
        return True

    def check_claim(self, entry: Mapping[str, Any], body: Mapping[str, Any]) -> None:
        """A build host's claim of a publish: it is the release's builder and
        proves it holds the cache's signing key (the key's signature over
        :func:`claim_message`). :raises GitOpsError: ``gitops-request-invalid``."""
        from piceli.dev.nix_verify import verify

        host = str(body.get("host") or "")
        if host != entry.get("builder"):
            raise GitOpsError(
                "gitops-request-invalid",
                f"nix release {entry.get('release')} is built by "
                f"{entry.get('builder')}, not {host}",
            )
        plan = entry.get("plan") or {}
        public = str(plan.get("cache", {}).get("public_key"))
        proof = body.get("proof")
        recipient = body.get("recipient")
        if plan.get("credentials") == "controller" and not isinstance(recipient, str):
            raise GitOpsError(
                "gitops-request-invalid",
                f"nix release {entry.get('release')} takes the controller's Git "
                "credential: the claim names no key to seal it to",
            )
        if not isinstance(proof, str) or not verify(
            public,
            claim_message(
                str(entry["id"]),
                host,
                recipient if isinstance(recipient, str) else None,
            ),
            proof,
        ):
            raise GitOpsError(
                "gitops-request-invalid",
                f"build host {host} shows no proof it holds the cache's signing key",
            )

    def seal(
        self,
        entry: dict[str, Any],
        body: Mapping[str, Any],
        credential: Mapping[str, str] | None,
    ) -> None:
        """For a ``controller`` release (0.26.0): the composition's Git
        credential sealed to the key of the accepted claim, for that build and
        host only (``sealed`` of the wanted publish; the build host opens it
        with the private key it never sends). Without an HTTPS token the
        entry says so and the build host fetches with its own access."""
        from piceli.dev.nix_inputs import seal, seal_aad

        entry.pop("sealed", None)
        if (entry.get("plan") or {}).get("credentials") != "controller":
            return
        if credential is None:
            entry["sealed"] = {
                "error": "the controller holds no HTTPS Git token (piceli secrets git)"
            }
            return
        host = str(body.get("host") or "")
        entry["sealed"] = seal(
            str(body["recipient"]),
            {"username": credential["username"], "token": credential["token"]},
            seal_aad(str(entry["id"]), host),
        )

    def published(self, body: Mapping[str, Any]) -> None:
        """A build host's ``nix-published`` answer: the manifest signed by the
        cache's key, of the planned build, commit and tag, and the tag's
        pointer. :raises GitOpsError: ``gitops-request-invalid``."""
        from piceli.dev.nix_verify import manifest_digest, verify_manifest

        entry = self.entry(body.get("id"))
        if entry is None:
            raise GitOpsError("gitops-request-invalid", "no such wanted publish")
        host = str(body.get("host") or "")
        if (entry.get("claimed") or {}).get("host") != host:
            raise GitOpsError(
                "gitops-request-invalid", f"build host {host} holds no claim of it"
            )
        plan = entry["plan"]
        manifest = body.get("manifest")
        if not isinstance(manifest, Mapping) or not verify_manifest(
            manifest, str(plan["cache"]["public_key"])
        ):
            raise GitOpsError(
                "gitops-request-invalid",
                "the manifest is not signed by the cache's key",
            )
        for key, expected in (
            ("name", plan["name"]),
            ("commit", plan["commit"]),
            ("tag", plan["tag"]),
            ("attribute", plan["attribute"]),
            ("pinned", plan.get("pinned")),
        ):
            if manifest.get(key) != expected:
                raise GitOpsError(
                    "gitops-request-invalid", f"the manifest names another {key}"
                )
        pointers = [
            item for item in body.get("pointers") or () if isinstance(item, Mapping)
        ]
        if [(item.get("ref"), item.get("commit")) for item in pointers] != plan_refs(
            plan
        ):
            raise GitOpsError(
                "gitops-request-invalid", "the answer names another pointer"
            )
        record = self.records().get(str(entry.get("release")))
        del self._wanted()[entry["id"]]
        if record is None or record.get("build_id") != entry["id"]:
            self.log(f"nix release {entry.get('release')}: a superseded publish ended")
            return
        receipt = (
            body.get("receipt") if isinstance(body.get("receipt"), Mapping) else {}
        )
        assert isinstance(receipt, Mapping)
        stages = {
            str(name): {
                k: item.get(k) for k in ("started_at", "finished_at", "seconds")
            }
            for name, item in (receipt.get("stages") or {}).items()
            if isinstance(item, Mapping)
            and name in ("fetch", "inputs", "build", "publish")
        }
        record["stages"] = stages
        record["published"] = {
            "host": host,
            "manifest": plan["manifest"],
            "manifest_sha256": manifest_digest(manifest),
            "store_path": manifest.get("store_path"),
            "system": manifest.get("system"),
            "nar_hash": manifest.get("nar_hash"),
            "nar_size": manifest.get("nar_size"),
            "closure_size": manifest.get("closure_size"),
            "built_at": manifest.get("built_at"),
            "pointers": [
                {k: item.get(k) for k in ("ref", "commit", "published_at")}
                for item in pointers
            ],
        }
        self._finish(str(entry["release"]), record, "published")

    def _finish(
        self,
        name: str,
        record: dict[str, Any],
        state: str,
        *,
        failure: Mapping[str, Any] | None = None,
        reason: str = FAILED,
    ) -> None:
        now = self.clock()
        record["state"] = state
        record["finished_at"] = _iso(now)
        record.pop("claimed", None)
        began = _epoch(record.get("requested_at"))
        record["seconds"] = None if began is None else round(now - began, 3)
        if failure is not None:
            record["failure"] = dict(failure)
            record["reason"] = reason
        else:
            record["reason"] = None
            record.pop("failure", None)
            record["published_tag"] = record.get("tag")
            record["published_commit"] = record.get("commit")
        record["stages"] = {**self._waits(record), **(record.get("stages") or {})}
        event = self.event(name, record)
        events = self.state.setdefault(HISTORY_KEY, {})
        events[name] = [*(events.get(name) or [])[-(MAX_EVENTS - 1) :], event]
        self.log(
            f"nix release {name}: {record.get('tag')} {state}"
            + (f" ({failure.get('reason')})" if failure else "")
        )
        hook = getattr(self.telemetry, "nix_release", None)
        if callable(hook):
            hook(event)

    @staticmethod
    def _waits(record: Mapping[str, Any]) -> dict[str, Any]:
        """The controller's own stages: the approval wait and the queue (until
        a build host claimed it)."""
        approval = record.get("approval") or {}
        requested = record.get("requested_at")
        policy = approval.get("via") == "policy"
        approved = requested if policy else approval.get("at")
        claimed = record.get("started_at")
        found: dict[str, Any] = {}
        waits = [("queue", approved, claimed)]
        if not policy:
            waits.insert(0, ("approval", requested, approved))
        for name, start, end in waits:
            begin, finish = _epoch(start), _epoch(end)
            if begin is not None and finish is not None:
                found[name] = {
                    "started_at": start,
                    "finished_at": end,
                    "seconds": round(max(0.0, finish - begin), 3),
                }
        return found

    @staticmethod
    def event(name: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """One attempt as a history entry (bounded, no secret)."""
        failure = record.get("failure") or {}
        published = record.get("published") or {}
        return {
            "kind": "nix-release",
            "id": f"{name}@{record.get('requested_at')}",
            "release": name,
            "tag": record.get("tag"),
            "commit": record.get("commit"),
            "state": record.get("state"),
            "reason": record.get("reason"),
            "trigger": record.get("trigger"),
            # 0.26.0: the trigger's kind, who asked, the branch of a branch build.
            "trigger_kind": record.get("trigger_kind"),
            "requested_by": record.get("requested_by"),
            "branch": (record.get("plan") or {}).get("branch"),
            "builder": record.get("builder"),
            "plan_digest": record.get("plan_digest"),
            "approval": record.get("approval"),
            "requested_at": record.get("requested_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "seconds": record.get("seconds"),
            "stages": dict(record.get("stages") or {}),
            "host": published.get("host") or failure.get("host"),
            "manifest_sha256": published.get("manifest_sha256"),
            "store_path": published.get("store_path"),
            "log_tail": str(failure.get("log_tail") or "")[-2000:] or None,
            # 0.25.0: the promotion gate's result, or why it refused.
            "pinned": record.get("pinned"),
            "refusal": record.get("refusal"),
        }

    # ------------------------------------------------------------ documents
    def status(self) -> dict[str, dict[str, Any]]:
        """``nix_releases`` of the status: each release's latest tag, its
        publish state, timings and manifest digest."""
        hidden = {"seen", "approved_digest", "followed", "schedule_checked_at"}
        return {
            name: {k: v for k, v in record.items() if k not in hidden}
            for name, record in sorted(self.records().items())
        }

    def history(self) -> dict[str, dict[str, Any]]:
        """``nix_releases`` of the history: each release's attempts, newest first."""
        events = self.state.get(HISTORY_KEY) or {}
        return {
            name: {"runs": list(reversed(items))}
            for name, items in sorted(events.items())
            if isinstance(items, list)
        }


# ---------------------------------------------------------------- build host


class Publisher:
    """The build host's side: publishes only to the caches it was given
    (``builder serve --publish MODULE:ATTR``), with their keys from this
    machine's Piceli secrets, checked when it starts.

    :param caches: The :class:`~piceli.infra.NixCache` s it publishes to.
    :param build: ``(checkout, attribute, say) -> store path``: builds the
        attribute of the checked-out flake (:func:`dev_build`).
    :param push: ``(request, say) -> outcome``: signs and pushes the
        closure on this host (:func:`dev_push`), the request with the keys
        on the publisher's stdin only; the outcome of
        :func:`piceli.dev.nix.run_publisher`.
    :param host: This build host's name (the manifests' ``host``).
    :param fetch: 0.26.0: ``(pairs, say) -> outcome``: fetches the plan's
        private inputs on this host before the build, with its own
        credentials (:func:`piceli.dev.nix_inputs.fetch_inputs`); ``None``:
        a plan that names inputs is refused.
    :raises DevError, InfraError: a cache's key is missing or is not its key.
    """

    def __init__(
        self,
        caches: Sequence[Any],
        *,
        build: Callable[[Path, str, Callable[[str], None]], str],
        push: Callable[..., Mapping[str, Any]],
        host: str,
        clock: Callable[[], float] | None = None,
        fetch: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        import time

        self.caches = list(caches)
        self.build = build
        self.push = push
        self.fetch = fetch
        self.host = host
        self.clock = clock or time.time
        for cache in self.caches:
            cache_keys(cache)  # refused now, not at the first publish

    def cache_for(self, entry: Mapping[str, Any]) -> Any:
        """The cache of ``entry``'s plan this host holds the keys of, or ``None``."""
        wanted = (entry.get("plan") or {}).get("cache") or {}
        return next(
            (
                cache
                for cache in self.caches
                if cache.public_key == wanted.get("public_key")
                and cache.store_url == wanted.get("store_url")
            ),
            None,
        )

    def proof(
        self, entry: Mapping[str, Any], host: str, recipient: str | None = None
    ) -> str:
        from piceli.dev.nix import sign

        secret = cache_keys(self.cache_for(entry))[0]
        return sign(secret, claim_message(str(entry["id"]), host, recipient))

    def publish(
        self,
        entry: Mapping[str, Any],
        checkout: Path,
        say: Callable[[str], None],
        stage: Callable[[str], Any],
        *,
        controller: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build and publish the plan of ``entry`` from ``checkout`` (the
        commit, the tag checked); the ``nix-published`` fields.
        ``controller``: 0.26.0, the Git credential the controller sealed for
        this build (a ``credentials="controller"`` release)."""
        from piceli.dev.model import DevError
        from piceli.dev.nix import publish_closure

        plan = check_plan(entry.get("plan"))
        cache = self.cache_for(entry)
        if cache is None:
            raise DevError("dev-nix-cache-invalid", "no key for the plan's cache")
        from piceli.tempfiles import temporary_directory

        # 0.26.0: the fetched inputs' GC roots live here until the build ended.
        with temporary_directory("nix-input-roots") as roots:
            self.fetch_inputs(
                plan, checkout, say, stage, controller=controller, roots=Path(roots)
            )
            with stage("build"):
                store_path = self.build(checkout, plan["attribute"], say)
        with stage("publish"):
            secret, s3_keys = cache_keys(cache)
            done = publish_closure(
                cache, plan, store_path, self.host, None, secret, s3_keys,
                say=say, push=self.push,
            )  # fmt: skip
            del secret
        return {"manifest": done["manifest_body"], "pointers": done["pointers"]}

    def fetch_inputs(
        self,
        plan: Mapping[str, Any],
        checkout: Path,
        say: Callable[[str], None],
        stage: Callable[[str], Any],
        *,
        controller: Mapping[str, Any] | None = None,
        roots: Path | None = None,
    ) -> None:
        """Fetch the plan's private inputs on this host, before any build
        (0.26.0): each one as ``flake.lock`` of the checkout locks it.

        :raises DevError: ``infra-nix-input-unreachable``."""
        from piceli.dev.model import DevError
        from piceli.dev.nix_inputs import LOCK_FILE, check_entries, resolve

        entries = check_entries(plan.get("inputs"))
        if not entries:
            return
        with stage("inputs"):
            lock = checkout / LOCK_FILE
            pairs = resolve(entries, lock.read_bytes() if lock.is_file() else None)
            if self.fetch is None:
                first = entries[0]
                raise DevError(
                    "infra-nix-input-unreachable",
                    f"{first['input']} {first['url']}: this build host fetches no "
                    "flake inputs (Publisher(fetch=))",
                )
            options: dict[str, Any] = {"controller": controller} if controller else {}
            if roots is not None:
                options["roots"] = roots
            self.fetch(pairs, say, **options)


def cache_keys(cache: Any) -> tuple[str, tuple[str, str] | None]:
    """The cache's signing key (checked against its public key) and S3 write
    keys, from this machine's Piceli secrets.

    :raises DevError, InfraError: a key is missing or is another cache's."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import public_key
    from piceli.infra.machines.credentials import load_credential

    if cache is None:
        raise DevError("dev-nix-cache-invalid", "no such cache on this host")
    trusted = cache.trusted_key()
    secret = load_credential(cache.signing_credential, "nix-signing-key")
    if public_key(secret) != trusted:
        raise DevError(
            "dev-nix-key-mismatch",
            f"credential {cache.signing_credential} is not the key of "
            f"NixCache(public_key={cache.key_name}:...)",
        )
    pair = None
    if cache.write_credentials:
        access, _, secret_access = load_credential(
            cache.write_credentials, "s3-keys"
        ).partition(":")
        pair = (access, secret_access)
    return secret, pair


def tag_commit(
    url: str,
    tag: str,
    mirrors: Path,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> str | None:
    """The commit ``tag`` of ``url`` points at, fetched into this host's
    mirror (:func:`piceli.infra.builder_serve.export_source`'s) with its own
    Git access; ``None`` when the source has no such tag."""
    from piceli.infra.builder_serve import _slug, fetch_env, fetch_urls

    mirror = mirrors / f"{_slug(url)}.git"
    ref = f"refs/tags/{tag}"
    for source in fetch_urls(url):
        run(
            ["git", "--git-dir", str(mirror), "fetch", "--quiet", source,
             f"+{ref}:{ref}"],
            check=False, capture_output=True, timeout=600, env=fetch_env(),
        )  # fmt: skip
        found = run(
            ["git", "--git-dir", str(mirror), "rev-parse", "--verify", "-q",
             f"{ref}^{{commit}}"],
            check=False, capture_output=True,
        )  # fmt: skip
        if found.returncode == 0:
            return found.stdout.decode().strip()
    return None


def dev_build(dev: Any, host: Any, profile: Any) -> Callable[..., str]:
    """``Publisher(build=)`` through a development run on ``host`` (docker
    or native, ``profile`` with ``nix=True``), as ``piceli dev nix build``."""

    def build(checkout: Path, attribute: str, say: Callable[[str], None]) -> str:
        from piceli.dev.client import RunRequest
        from piceli.dev.client import run as run_on
        from piceli.dev.hosts import DockerPort, NativePort
        from piceli.dev.model import DevError
        from piceli.dev.nix import build_argv, store_path_of
        from piceli.dev.pack import SourceRequest, repository_name
        from piceli.tempfiles import temporary_directory

        seen: list[str] = []

        def log(line: str) -> None:
            seen.append(line)
            say(line)

        rev = _head(checkout)
        port: Any = (
            DockerPort(host, dev) if host.kind == "docker" else NativePort(host, dev)
        )
        with temporary_directory("nix-release") as scratch:
            request = RunRequest(
                root=SourceRequest(repository_name(checkout), checkout, rev),
                command=build_argv(attribute),
                cwd=".",
                profile=profile.name,
                requester="builder-serve",
                artifacts=[],
                artifacts_dir=Path(scratch),
                queue_timeout=3600.0,
            )
            result = run_on(port, dev, request, say=say, log=log, host=host)
        if result.get("state") != "passed":
            for line in result.get("log_tail") or ():
                say(f"  | {line}")
            raise DevError(
                str(result.get("reason") or "dev-run-failed"),
                "the Nix build did not pass",
            )
        found = store_path_of(seen) or store_path_of(result.get("log_tail") or ())
        if found is None:
            raise DevError("dev-nix-no-output", "the build printed no store path")
        return found

    return build


def dev_push(host: Any, image: str) -> Callable[..., Mapping[str, Any]]:
    """``Publisher(push=)``: the publisher step on ``host``, as ``piceli dev
    nix build --publish`` runs it (the keys on its stdin only)."""

    def push(request: Mapping[str, Any], say: Callable[[str], None]) -> Any:
        from piceli.dev import nix

        return nix.run_publisher(nix.publisher_argv(host, image), request, say=say)

    return push


def _head(checkout: Path) -> str:
    done = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    return done.stdout.strip()


def receipt_stages(marks: Mapping[str, tuple[float, float]]) -> dict[str, Any]:
    """``{stage: (start, end)}`` epochs as the receipt's stages."""
    return {
        name: {
            "started_at": _iso(start),
            "finished_at": _iso(end),
            "seconds": round(max(0.0, end - start), 3),
        }
        for name, (start, end) in marks.items()
    }
