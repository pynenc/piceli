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

Tags that exist when the controller first sees a release are its baseline
and publish nothing; ``piceli gitops sync web-1`` publishes the newest one.

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
)
FAILED = "gitops-nix-release-failed"
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
    """

    host: Any
    source: Any
    builder: str
    auto_approve: bool = False
    gate: Any = None

    def __post_init__(self) -> None:
        from piceli.hosts.model import NixHost, Tag
        from piceli.infra import Source

        if not isinstance(self.host, NixHost):
            raise _invalid("NixRelease(host=) is a piceli.infra.NixHost")
        if not isinstance(self.host.follow, Tag):
            raise _invalid(
                f"NixRelease {self.host.name}: the controller publishes tags; "
                "follow=Tag(...) (publish a Branch with piceli dev nix build "
                "--publish)"
            )
        if not isinstance(self.source, Source):
            raise _invalid("NixRelease(source=) is a piceli.infra.Source")
        if not isinstance(self.builder, str) or not _LABEL.fullmatch(self.builder):
            raise _invalid("NixRelease(builder=) is a build host's name")
        if not isinstance(self.auto_approve, bool):
            raise _invalid("NixRelease(auto_approve=) is True or False")
        from piceli.infra.promotion import Promotion

        if self.gate is not None and not isinstance(self.gate, Promotion):
            raise _invalid("NixRelease(gate=) is a piceli.infra.Promotion")
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
        """The release as plain data (what the controller's config holds)."""
        return ReleaseTarget(
            name=self.host.name,
            build=str(self.host.build),
            attribute=self.host.attribute,
            pattern=self.host.follow.pattern,
            prereleases=self.host.follow.prereleases,
            source=self.source,
            builder=self.builder,
            auto_approve=self.auto_approve,
            store_url=self.host.cache.store_url,
            public_key=self.host.cache.public_key,
            gate=self.gate,
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
    public_key: str
    #: 0.25.0: the :class:`~piceli.infra.Promotion` gate, or ``None``.
    gate: Any = None

    def to_dict(self) -> dict[str, Any]:
        gate = {"gate": self.gate.to_dict()} if self.gate is not None else {}
        return gate | {
            "name": self.name,
            "build": self.build,
            "attribute": self.attribute,
            "follow": {
                "kind": "tag",
                "pattern": self.pattern,
                "prereleases": self.prereleases,
            },
            "source": self.source.key,
            "builder": self.builder,
            "auto_approve": self.auto_approve,
            "cache": {"store_url": self.store_url, "public_key": self.public_key},
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], sources: Mapping[str, Any]) -> Any:
        from piceli.dev.model import DevError
        from piceli.dev.nix import check_public_key, installable
        from piceli.hosts.model import HostError, Tag
        from piceli.infra.promotion import Promotion

        try:
            follow = value["follow"]
            tag = Tag(str(follow["pattern"]), bool(follow.get("prereleases")))
            target = cls(
                name=str(value["name"]),
                build=str(value["build"]),
                attribute=installable(str(value["attribute"])),
                pattern=tag.pattern,
                prereleases=tag.prereleases,
                source=sources[str(value["source"])],
                builder=str(value["builder"]),
                auto_approve=value.get("auto_approve") is True,
                store_url=str(value["cache"]["store_url"]),
                public_key=check_public_key(str(value["cache"]["public_key"])),
                gate=Promotion.from_dict(value["gate"]) if value.get("gate") else None,
            )
        except (KeyError, TypeError, HostError, DevError) as error:
            raise _invalid(f"a nix release is invalid: {error}") from None
        for label in (target.name, target.build, target.builder):
            if not _LABEL.fullmatch(label):
                raise _invalid(f"a nix release names {label!r}, not a DNS label")
        return target

    def version(self, tag: str) -> tuple[Any, ...] | None:
        """``tag``'s order under the pattern (``None``: not this release's,
        or a pre-release without ``prereleases``)."""
        from piceli.hosts.agent import tag_version

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


def release_plan(
    target: ReleaseTarget,
    commit: str,
    tag: str,
    pinned: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What publishing ``tag`` does; its digest (:func:`digest`) is what the
    owner approves and what the build host publishes, nothing else.
    ``pinned``: the promotion gate's result (only for a gated release)."""
    from piceli.dev.nix import manifest_key, pointer_key

    extra = {"pinned": dict(pinned)} if pinned is not None else {}
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
        "pointers": [pointer_key(target.build, "tag", tag)],
    }


def digest(plan: Mapping[str, Any]) -> str:
    from piceli.dev.nix import plan_digest

    return plan_digest(plan)


def check_plan(plan: Any) -> dict[str, Any]:
    """A plan a build host received, checked before it builds anything: its
    pointers are exactly the tag's of its build (a controller cannot make the
    host sign another build's pointers). :raises ValueError: otherwise."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import installable, manifest_key, pointer_key, ref_name_ok

    if not isinstance(plan, Mapping) or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError("not a nix release plan")
    name, commit, tag = (str(plan.get(k) or "") for k in ("name", "commit", "tag"))
    try:
        installable(str(plan.get("attribute") or ""))
    except DevError:
        raise ValueError("the plan's attribute is not .#ATTR") from None
    if (
        not _LABEL.fullmatch(name)
        or not _REV.fullmatch(commit)
        or not ref_name_ok(tag)
        or plan.get("manifest") != manifest_key(name, commit)
        or list(plan.get("pointers") or ()) != [pointer_key(name, "tag", tag)]
    ):
        raise ValueError("the plan names another build, commit, tag or pointer")
    from piceli.infra.promotion import check_pinned

    check_pinned(plan.get("pinned"))
    return dict(plan)


def claim_message(build: str, host: str) -> bytes:
    """What a build host signs with the cache key to claim a publish."""
    from piceli.dev.nix_verify import canonical

    return canonical({"schema": CLAIM_SCHEMA, "id": build, "host": host})


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
    ) -> None:
        """Follow each release's tags and move it on (one poll).

        ``gate(target, commit)`` checks a gated release's commit: the
        ``pinned`` field, or :class:`~piceli.infra.promotion.PromotionError`.
        A rejected tag is checked again on every poll, so it publishes once
        its environment verified the pinned commit."""
        self._gate = gate
        records = self.records()
        forced = self.state.setdefault("nix_forced", {})
        names = {target.name for target in targets}
        for gone in [name for name in records if name not in names]:
            self._drop_wanted(records.pop(gone))
            forced.pop(gone, None)
        for target in targets:
            record = records.setdefault(target.name, {"state": "idle", "tag": None})
            record.update(
                host=target.name,
                source=target.source.key,
                builder=target.builder,
                follow={"kind": "tag", "pattern": target.pattern,
                        "prereleases": target.prereleases},
                auto_approve=target.auto_approve,
            )  # fmt: skip
            found = refs.get(target.source.key)
            if found is not None:
                latest = target.latest(found.tags)
                record["latest_tag"] = latest[0] if latest else None
                record["latest_commit"] = latest[1] if latest else None
                why = forced.pop(target.name, None)
                if latest is not None:
                    if not record.get("seen") and why is None:
                        record.update(state="baseline", tag=latest[0], commit=latest[1])
                    elif (
                        why is not None
                        or self._newer(target, latest[0], record)
                        or (
                            record.get("state") == "rejected"
                            and record.get("tag") == latest[0]
                        )
                    ):
                        self._attempt(target, record, latest, why or f"tag {latest[0]}")
                record["seen"] = True
            self._advance(target, record)

    @staticmethod
    def _newer(target: ReleaseTarget, tag: str, record: Mapping[str, Any]) -> bool:
        current = record.get("tag")
        if not isinstance(current, str):
            return True
        now, before = target.version(tag), target.version(current)
        return now is not None and (before is None or now > before)

    def _attempt(
        self,
        target: ReleaseTarget,
        record: dict[str, Any],
        latest: tuple[str, str],
        trigger: str,
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
        plan = release_plan(target, commit, tag, pinned)
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
        self.log(f"nix release {target.name}: {trigger} -> {tag}@{commit[:12]}")

    def _refuse(
        self,
        target: ReleaseTarget,
        record: dict[str, Any],
        latest: tuple[str, str],
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
        public = str((entry.get("plan") or {}).get("cache", {}).get("public_key"))
        proof = body.get("proof")
        if not isinstance(proof, str) or not verify(
            public, claim_message(str(entry["id"]), host), proof
        ):
            raise GitOpsError(
                "gitops-request-invalid",
                f"build host {host} shows no proof it holds the cache's signing key",
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
        if [(item.get("ref"), item.get("commit")) for item in pointers] != [
            (f"refs/tags/{plan['tag']}", plan["commit"])
        ]:
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
            if isinstance(item, Mapping) and name in ("fetch", "build", "publish")
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
    ) -> None:
        now = self.clock()
        record["state"] = state
        record["finished_at"] = _iso(now)
        record.pop("claimed", None)
        began = _epoch(record.get("requested_at"))
        record["seconds"] = None if began is None else round(now - began, 3)
        if failure is not None:
            record["failure"] = dict(failure)
            record["reason"] = FAILED
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
        hidden = {"seen", "approved_digest"}
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
    ) -> None:
        import time

        self.caches = list(caches)
        self.build = build
        self.push = push
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

    def proof(self, entry: Mapping[str, Any], host: str) -> str:
        from piceli.dev.nix import sign

        secret = cache_keys(self.cache_for(entry))[0]
        return sign(secret, claim_message(str(entry["id"]), host))

    def publish(
        self,
        entry: Mapping[str, Any],
        checkout: Path,
        say: Callable[[str], None],
        stage: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Build and publish the plan of ``entry`` from ``checkout`` (the
        commit, the tag checked); the ``nix-published`` fields."""
        from piceli.dev.model import DevError
        from piceli.dev.nix import publish_closure

        plan = check_plan(entry.get("plan"))
        cache = self.cache_for(entry)
        if cache is None:
            raise DevError("dev-nix-cache-invalid", "no key for the plan's cache")
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


def cache_keys(cache: Any) -> tuple[str, tuple[str, str] | None]:
    """The cache's signing key (checked against its public key) and S3 write
    keys, from this machine's Piceli secrets.

    :raises DevError, InfraError: a key is missing or is another cache's."""
    from piceli.dev.model import DevError
    from piceli.dev.nix import public_key
    from piceli.infra.machines.credentials import load_credential

    if cache is None:
        raise DevError("dev-nix-cache-invalid", "no such cache on this host")
    secret = load_credential(cache.signing_credential, "nix-signing-key")
    if public_key(secret) != cache.public_key:
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
