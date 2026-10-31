"""The Piceli host agent: follow a Git ref from a Nix binary cache (0.24.0).

Standard library only. ``piceli host module`` renders a NixOS module that
puts this file and a copy of :mod:`piceli.dev.nix_verify` into the system,
with the host's configuration (:func:`piceli.hosts.model.agent_config`) as
JSON, and runs it as a systemd oneshot on a timer. Each run:

1. **resolve**: read the signed ref pointer of the followed branch, or list
   the tag pointers and take the highest version that matches; refuse a
   pointer whose signature fails or whose ``published_at`` is older than
   one already seen (a replay), and never move to an older version;
2. **verify**: fetch the build manifest, check its sha256 against the
   pointer, its signature, its attribute and its system, then the store
   path's ``.narinfo``: its signature and its ``NarHash``;
3. **realise**: ``nix copy --from`` the cache, with ``builders`` empty and
   ``max-jobs 0`` (the host never builds), trusting only the cache's key;
4. **activate** (0.26.2): record the attempt as ``pending`` in the state,
   then ``switch-to-configuration test``: the new system runs, the profile
   and the boot entry stay on the previous one;
5. **checks**: each until it passes or its timeout (a hung check fails);
6. **boot**: only then ``nix-env -p /nix/var/nix/profiles/system --set``
   and ``switch-to-configuration boot``, and the pending mark goes;
   on a failed activation, check or boot step, **rollback**: the previous
   system with ``test`` (and its profile and boot entry, when the boot step
   had started), and mark the build bad: it is not tried again until a
   newer pointer. A run that died with an attempt pending (its own switch
   stopped it, a hang, a reboot) is rolled back by the next run before
   anything else (``host-deploy-interrupted``); a reboot comes up on the
   previous system, since the boot default had not moved;
7. after a deploy that passed its checks, **prune**: delete the system
   profile's generations beyond ``generations`` (``nix-env
   --delete-generations``; never the current, the previous or a held
   system), then collect the store (``nix-store --gc``). A failure there
   is reported (``host-prune-failed``) and never fails the deploy.

``piceli host pin``, ``unpin`` and ``rollback`` run it once over SSH as
``piceli-host-agent pin TAG`` (and so on), under the same lock as a poll: a
pin deploys a tag's build (verified like any other, even an older version)
and holds it; a rollback switches back to the previous system, already in
the store, and holds it too, so the next poll does not deploy again what was
rolled away from; ``unpin`` drops the hold. A hold lives in ``state.json``
and survives reboots and switches; while it holds, polls do not deploy.

The status (``status.json`` in the state directory, readable by anyone,
no secret) carries the current and previous builds, the last attempt with
its stages, timings and check results, and a bounded history. Events go to
an OTLP/HTTP endpoint as logs when one is configured; a collector that is
down never blocks or fails a deploy (2 s timeout, bounded queue kept for
the next run).

Importing this module is side-effect free.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

try:
    from piceli.dev import nix_verify as _v
except ImportError:  # pragma: no cover - on the host: the copy next to this file
    import nix_verify as _v  # type: ignore[import-not-found,no-redef]

STATUS_SCHEMA = "piceli.nix-host-status.v1"
STATUS_FILE = "status.json"
STATE_FILE = "state.json"
QUEUE_FILE = "otlp-queue.json"
FEATURES = ("--extra-experimental-features", "nix-command flakes")
DEFAULT_TOOLS = {
    "nix": "/run/current-system/sw/bin/nix",
    "nix_env": "/run/current-system/sw/bin/nix-env",
    "nix_store": "/run/current-system/sw/bin/nix-store",
    "systemctl": "/run/current-system/sw/bin/systemctl",
}
PROFILE = "/nix/var/nix/profiles/system"
CURRENT = "/run/current-system"
CHECK_PATH = "/run/wrappers/bin:/run/current-system/sw/bin"
REALISE_SECONDS = 2 * 3600
#: How long a pin, unpin or rollback waits for a poll in progress.
LOCK_WAIT = 600
ACTIONS = ("poll", "pin", "unpin", "rollback")
_TAG = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]{0,199}")
SWITCH_SECONDS = 1800
#: System generations kept (``NixHost(generations=)``), and the time limits
#: of deleting the others and of the store's garbage collection.
GENERATIONS = 5
DELETE_SECONDS = 600
GC_SECONDS = 3600
_FREED = re.compile(r"(\d+) store paths deleted, ([0-9.]+ [KMGT]?i?B) freed")
QUEUE_MAX = 500
OTLP_TIMEOUT = 2.0
_STORE_PATH = re.compile(r"/nix/store/([0-9a-df-np-sv-z]{32})-[A-Za-z0-9+._?=-]+")
_SEMVER = re.compile(
    r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?"
)

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]
Opener = Callable[..., Any]


class Failure(Exception):
    """A refused or failed step; ``code`` is a registered ``host-*`` code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def now_text(moment: float | None = None) -> str:
    stamp = time.time() if moment is None else moment
    return _dt.datetime.fromtimestamp(stamp, _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def semver(tag: str) -> tuple[Any, ...] | None:
    """A sort key of a semantic version tag (``v1.2.3``, ``1.2.3-rc.1``),
    ``None`` for another tag. A release sorts above its pre-releases."""
    match = _SEMVER.fullmatch(tag)
    if match is None:
        return None
    core = tuple(int(match.group(i)) for i in (1, 2, 3))
    pre = match.group(4)
    if pre is None:
        return (*core, 1, ())
    parts = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in pre.split(".")
    )
    return (*core, 0, parts)


_GLOB = re.compile(r"[*?\[]")


def tag_prefix(pattern: str) -> str:
    """The literal prefix of a ``Tag`` pattern: what comes before its first
    wildcard (``*``, ``?``, ``[``), without the version digits and dots it
    may pin (``web-v1.*`` is ``web-v``). ``v*`` is ``v``, ``*`` is ``""``."""
    return _GLOB.split(pattern, 1)[0].rstrip("0123456789.")


def tag_version(pattern: str, tag: str) -> tuple[Any, ...] | None:
    """The sort key of ``tag`` under ``pattern``: the tag matches the glob and
    what follows the pattern's :func:`tag_prefix` is a semantic version
    (:func:`semver`); ``None`` otherwise. ``Tag("v*")`` never matches
    ``web-v1.2.3``, and ``Tag("web-*")`` never takes ``web-api-1.2.3``.
    A tag with build metadata (``v1.2.3+test``) is a test tag, never a
    release: it is ``None`` too, so no follower, plan or bump counts it."""
    if not fnmatch.fnmatchcase(tag, pattern):
        return None
    prefix = tag_prefix(pattern)
    if not tag.startswith(prefix) or "+" in tag[len(prefix) :]:
        return None
    return semver(tag[len(prefix) :])


def read_env_file(path: str) -> dict[str, str]:
    """``KEY=VALUE`` lines of a root-only file (``host-credentials-exposed``
    when others may read it, ``host-credentials-missing`` when absent)."""
    try:
        info = os.stat(path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        raise Failure(
            "host-credentials-missing", "a credentials file is missing"
        ) from None
    if info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
        # 0600 or 0400 (sops-nix's default), owned by root (the agent's user).
        raise Failure("host-credentials-exposed", "a credentials file is not 0600")
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip("'\"")
    return values


# ------------------------------------------------------------------ cache


class Cache:
    """Read-only access to the cache's objects: a directory, an S3 bucket
    (anonymous, or SigV4 with read-only keys) or an ``https://`` cache."""

    def __init__(
        self,
        objects: Mapping[str, Any],
        keys: tuple[str, str] | None = None,
        *,
        opener: Opener | None = None,
    ) -> None:
        self.objects = dict(objects)
        self.keys = keys
        self.opener = opener or urllib.request.urlopen

    def _url(self, key: str, query: str = "") -> str:
        kind = self.objects["kind"]
        quoted = urllib.parse.quote(key, safe="/-_.~")
        if kind == "s3":
            base = f"https://{self.objects['endpoint']}/{self.objects['bucket']}"
            return f"{base}/{quoted}" if key else f"{base}?{query}"
        return f"{self.objects['base']}/{quoted}"

    def _http(self, url: str) -> bytes | None:
        headers: dict[str, str] = {}
        if self.objects["kind"] == "s3" and self.keys is not None:
            headers = _v.sigv4_headers(
                "GET", url, b"", region=str(self.objects.get("region") or "us-east-1"),
                access_key=self.keys[0], secret_key=self.keys[1],
                now=_dt.datetime.now(_dt.UTC),
            )  # fmt: skip
            headers.pop("host")
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.opener(request, timeout=60) as response:
                return bytes(response.read(64 << 20))
        except urllib.error.HTTPError as error:
            if error.code in (403, 404) and self.keys is None:
                return None  # a public bucket answers 403 for a missing key
            if error.code == 404:
                return None
            raise Failure("host-cache-unreachable", f"HTTP {error.code}") from None
        except (OSError, ValueError):
            raise Failure(
                "host-cache-unreachable", "the cache did not answer"
            ) from None

    def get(self, key: str) -> bytes | None:
        if self.objects["kind"] == "file":
            path = os.path.join(self.objects["root"], key)
            try:
                with open(path, "rb") as handle:
                    return handle.read()
            except FileNotFoundError:
                return None
            except OSError:
                raise Failure(
                    "host-cache-unreachable", "the cache is unreadable"
                ) from None
        return self._http(self._url(key))

    def list(self, prefix: str) -> list[str]:
        """The keys under ``prefix`` (a directory walk, or S3 ListObjectsV2)."""
        if self.objects["kind"] == "file":
            root = os.path.join(self.objects["root"], prefix)
            found = []
            for folder, _, files in os.walk(root):
                for name in files:
                    full = os.path.join(folder, name)
                    found.append(os.path.relpath(full, self.objects["root"]))
            return sorted(key.replace(os.sep, "/") for key in found)
        if self.objects["kind"] != "s3":
            raise Failure("host-cache-unreachable", "an https cache cannot be listed")
        import xml.etree.ElementTree as ElementTree

        keys: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            body = self._http(self._url("", urllib.parse.urlencode(query)))
            try:
                root = ElementTree.fromstring(body or b"")
            except ElementTree.ParseError:
                raise Failure("host-cache-unreachable", "a bad listing") from None
            token, truncated = "", False
            for node in root:
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "Contents":
                    for child in node:
                        if child.tag.rsplit("}", 1)[-1] == "Key" and child.text:
                            keys.append(child.text)
                elif tag == "NextContinuationToken":
                    token = node.text or ""
                elif tag == "IsTruncated":
                    truncated = node.text == "true"
            if not truncated or not token:
                return sorted(keys)


# ------------------------------------------------------------------ OTLP


#: The event name of a release marker (0.38.0), from the host agent and the
#: controller alike.
MARKER_EVENT = "piceli.release"
#: ``deployment.status`` of a marker: the conventions' ``succeeded`` and
#: ``failed``, and ``rolled_back`` when a rollback ended the release.
MARKER_STATUSES = ("succeeded", "failed", "rolled_back")


def _seconds_between(started: Any, finished: Any) -> float | None:
    try:
        begin = _dt.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        end = _dt.datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (end - begin).total_seconds()), 3)


def release_marker(
    *,
    environment: str,
    deployment_id: str,
    status: str,
    version: str | None,
    revision: str | None,
    started_at: str | None,
    finished_at: str | None,
    components: list[str] | tuple[str, ...] = (),
    previous: Mapping[str, Any] | None = None,
    repository_url: str | None = None,
    release_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """The attributes of a release marker (0.38.0): one per finished release.

    OpenTelemetry semantic-convention names where they exist
    (``deployment.*``, ``service.version``, ``vcs.*``); the time window, the
    components and the previous release under ``piceli.release.*``, so a
    backend can compare the hour before with the hour after. ``previous``
    is ``{"id", "version", "revision", "finished_at"}`` of the release that
    ran before (``None`` for the first one). Standard library only: the host
    agent and the controller share it. ``None`` values are left out."""
    if status not in MARKER_STATUSES:
        raise ValueError(f"unknown release status {status!r}")
    before = dict(previous or {})
    found: dict[str, Any] = {
        "deployment.environment.name": environment,
        "deployment.id": deployment_id,
        "deployment.status": status,
        "service.version": version,
        "vcs.ref.head.revision": revision,
        "vcs.repository.url.full": repository_url,
        "piceli.release.id": release_id,
        "piceli.run.id": run_id,
        "piceli.release.components": sorted({str(c) for c in components if c}),
        "piceli.release.started_at": started_at,
        "piceli.release.finished_at": finished_at,
        "piceli.release.seconds": _seconds_between(started_at, finished_at)
        if started_at and finished_at
        else None,
        "piceli.release.previous.id": before.get("id"),
        "piceli.release.previous.version": before.get("version"),
        "piceli.release.previous.revision": before.get("revision"),
        "piceli.release.previous.finished_at": before.get("finished_at"),
    }
    return {k: v for k, v in found.items() if v is not None and v != "" and v != []}


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, list | tuple):  # 0.38.0: list attributes
        return {
            "key": key,
            "value": {
                "arrayValue": {"values": [_attribute(key, v)["value"] for v in value]}
            },
        }
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


class Reporter:
    """OTLP/HTTP JSON logs, one record per event. Never raises: a collector
    that is down costs at most one 2 s attempt per run; the records wait
    in a bounded queue for the next run."""

    def __init__(
        self,
        report: Mapping[str, Any] | None,
        host: str,
        queue_file: str,
        *,
        opener: Opener | None = None,
    ) -> None:
        self.report = dict(report or {})
        self.host = host
        self.queue_file = queue_file
        self.opener = opener or urllib.request.urlopen
        self.down = False
        self.error: str | None = None
        self.sent = 0
        self.queue: list[dict[str, Any]] = []
        if self.report:
            try:
                with open(queue_file, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                self.queue = [r for r in loaded if isinstance(r, dict)][-QUEUE_MAX:]
            except (OSError, ValueError, TypeError):
                self.queue = []

    def event(
        self, name: str, body: str, trace: str, error: bool = False, **attrs: Any
    ) -> None:
        self._queue(name, body, trace, error, attrs)

    def marker(
        self, body: str, trace: str, error: bool, attrs: Mapping[str, Any]
    ) -> None:
        """A release marker (0.38.0): also sets the record's OTLP
        ``eventName`` field."""
        self._queue(MARKER_EVENT, body, trace, error, attrs, event_name=True)

    def _queue(
        self,
        name: str,
        body: str,
        trace: str,
        error: bool,
        attrs: Mapping[str, Any],
        *,
        event_name: bool = False,
    ) -> None:
        if not self.report:
            return
        record: dict[str, Any] = {
            "timeUnixNano": str(time.time_ns()),
            "severityNumber": 17 if error else 9,
            "severityText": "ERROR" if error else "INFO",
            "body": {"stringValue": body},
            "traceId": trace,
            "attributes": [_attribute("event.name", name)]
            + [_attribute(k, v) for k, v in sorted(attrs.items()) if v is not None],
        }
        if event_name:
            record["eventName"] = name
        self.queue = (self.queue + [record])[-QUEUE_MAX:]
        self.flush()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        path = self.report.get("headers_file")
        if path:
            try:
                info = os.stat(path)
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                raise Failure("host-credentials-missing") from None
            if info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid()):
                raise Failure("host-credentials-exposed")
            for line in lines:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    headers[key.strip()] = value.strip()
        return headers

    def flush(self) -> None:
        if not self.report or self.down or not self.queue:
            return
        payload = {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            _attribute("service.name", "piceli-host-agent"),
                            _attribute("host.name", self.host),
                            # 0.38.0: the host is the environment of its releases.
                            _attribute("deployment.environment.name", self.host),
                        ]
                    },
                    "scopeLogs": [
                        {
                            "scope": {"name": "piceli.hosts.agent"},
                            "logRecords": self.queue,
                        }
                    ],
                }
            ]
        }
        try:
            request = urllib.request.Request(
                self.report["endpoint"],
                data=json.dumps(payload).encode(),
                headers=self._headers(),
                method="POST",
            )
            with self.opener(request, timeout=OTLP_TIMEOUT) as response:
                status = int(getattr(response, "status", 200))
            if not 200 <= status < 300:
                raise Failure("host-report-failed", f"HTTP {status}")
        except Failure as failed:
            self.down, self.error = True, failed.code
            return
        except Exception:  # a collector that is down never fails a deploy
            self.down, self.error = True, "host-report-failed"
            return
        self.sent += len(self.queue)
        self.queue = []

    def save(self) -> None:
        if not self.report:
            return
        _write_json(self.queue_file, self.queue[-QUEUE_MAX:], 0o600)

    def describe(self) -> dict[str, Any] | None:
        if not self.report:
            return None
        return {
            "endpoint": self.report["endpoint"],
            "state": "down" if self.down else "ok",
            "reason": self.error,
            "queued": len(self.queue),
            "sent": self.sent,
        }


def _write_json(path: str, value: Any, mode: int) -> None:
    staged = f"{path}.tmp"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
    os.chmod(staged, mode)
    os.replace(staged, path)


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ------------------------------------------------------------------ agent


#: The fields of a promotion gate's pin the status keeps.
PINNED_KEYS = ("input", "commit", "environment", "verified_run", "verified_at")


def _pinned(value: Any) -> dict[str, Any] | None:
    """A manifest's ``pinned`` (its known fields), or ``None``."""
    if not isinstance(value, Mapping) or not value.get("commit"):
        return None
    return {key: value.get(key) for key in PINNED_KEYS}


class Agent:
    """One host's agent; :meth:`run_once` is one poll."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        runner: Runner = subprocess.run,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        check_interval: float = 2.0,
    ) -> None:
        self.config = dict(config)
        self.runner = runner
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.clock = clock
        self.monotonic = monotonic
        self.check_interval = check_interval
        self.dir = str(config.get("state_dir") or "/var/lib/piceli-host")
        self.tools = {**DEFAULT_TOOLS, **(config.get("tools") or {})}
        scope = config.get("scope") or {}
        # 0.45.0: in service mode a Nix profile and its units stand in for
        # the system; the system and its boot entry are never touched.
        self.services: dict[str, Any] | None = (
            dict(scope) if scope.get("kind") == "services" else None
        )
        own = self.services["profile"] if self.services else None
        self.profile = str(config.get("profile") or own or PROFILE)
        self.current_link = str(config.get("current_system") or own or CURRENT)
        self.public = str(config["cache"]["public_key"])
        self.build = str(config["build"])
        self.env: dict[str, str] = {}
        self.status: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.reporter = Reporter(
            config.get("report"), str(config["host"]),
            os.path.join(self.dir, QUEUE_FILE), opener=self.opener,
        )  # fmt: skip

    # ---------------------------------------------------------- helpers

    def _cache(self) -> Cache:
        keys = None
        path = self.config.get("read_credentials_file")
        if path:
            values = read_env_file(str(path))
            access = values.get("AWS_ACCESS_KEY_ID", "")
            secret = values.get("AWS_SECRET_ACCESS_KEY", "")
            if not access or not secret:
                raise Failure("host-credentials-missing", "no AWS_* keys in the file")
            keys = (access, secret)
            self.env = {
                "AWS_ACCESS_KEY_ID": access,
                "AWS_SECRET_ACCESS_KEY": secret,
                "AWS_EC2_METADATA_DISABLED": "true",
            }
        return Cache(self.config["cache"]["objects"], keys, opener=self.opener)

    def _run(
        self, argv: list[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> int:
        base = {
            "PATH": CHECK_PATH,
            "LANG": "C.UTF-8",
            **({k: v for k, v in os.environ.items() if k in ("HOME", "TZ")}),
        }
        try:
            done = self.runner(
                argv,
                env={**base, **(env or {})},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127
        return int(done.returncode)

    def _output(
        self, argv: list[str], timeout: float, *, stderr: bool = False
    ) -> tuple[int, str]:
        """Exit code and stdout (and stderr after it with ``stderr=True``)."""
        try:
            done = self.runner(
                argv,
                env={"PATH": CHECK_PATH},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""
        out = (done.stdout or b"") + ((done.stderr or b"") if stderr else b"")
        return int(done.returncode), out.decode("utf-8", "replace")

    def running(self) -> str | None:
        """The store path that runs: ``/run/current-system``'s, or in service
        mode the profile's (through its generation link)."""
        try:
            target = os.readlink(self.current_link)
            if self.services and not target.startswith("/nix/store/"):
                link = os.path.join(os.path.dirname(self.current_link), target)
                target = os.readlink(link)
        except OSError:
            return None
        return target if _STORE_PATH.fullmatch(target) else None

    def _save(self) -> None:
        self.status["updated_at"] = now_text(self.clock())
        self.status["report"] = self.reporter.describe()
        _write_json(os.path.join(self.dir, STATUS_FILE), self.status, 0o644)
        _write_json(os.path.join(self.dir, STATE_FILE), self.state, 0o600)

    # ---------------------------------------------------------- resolve

    def resolve(self, cache: Cache) -> tuple[dict[str, Any] | None, str | None]:
        """The pointer to deploy (signature checked, not a replay), or
        ``None`` and why not."""
        follow = self.config["follow"]
        base = f"piceli/{self.build}/refs/"
        version: tuple[Any, ...] | None = None
        if follow["kind"] == "branch":
            key = f"{base}heads/{follow['name']}.json"
        else:
            prefix = f"{base}tags/"
            ranked: list[tuple[tuple[Any, ...], str]] = []
            ignored: list[str] = []
            for found in cache.list(prefix):
                if not found.endswith(".json"):
                    continue
                tag = found[len(prefix) : -len(".json")]
                pattern = str(follow["pattern"])
                if not fnmatch.fnmatchcase(tag, pattern):
                    continue
                order = tag_version(pattern, tag)
                if order is None or (order[3] == 0 and not follow.get("prereleases")):
                    ignored.append(tag)
                    continue
                ranked.append((order, tag))
            self.status["ignored_tags"] = sorted(ignored)[:50]
            if not ranked:
                return None, "no-build"
            version, tag = max(ranked)
            key = f"{prefix}{tag}.json"
        body = self.pointer(cache, key)
        if body is None:
            return None, "no-build"
        current = self.state.get("current") or {}
        if version is not None and isinstance(current.get("tag"), str):
            running = tag_version(str(follow["pattern"]), str(current["tag"]))
            if running is not None and version < running:
                return None, "older"
        if current.get("ref") == body["ref"] and str(
            current.get("published_at") or ""
        ) > str(body["published_at"]):
            return None, "older"
        return body, None

    def pointer(self, cache: Cache, key: str) -> dict[str, Any] | None:
        """The ref pointer at ``key``, signature checked and not a replay
        (``None`` when there is none)."""
        raw = cache.get(key)
        if raw is None:
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or not _v.verify_pointer(body, self.public):
            raise Failure("host-signature-invalid", "the ref pointer's signature")
        expected = key[len(f"piceli/{self.build}/") : -len(".json")]
        if (
            body.get("name") != self.build
            or body.get("ref") != expected
            or not isinstance(body.get("store_path"), str)
            or not _STORE_PATH.fullmatch(str(body["store_path"]))
            or not isinstance(body.get("published_at"), str)
        ):
            raise Failure("host-manifest-mismatch", "the pointer names another build")
        seen = self.state.setdefault("seen", {})
        last = seen.get(body["ref"])
        if isinstance(last, str) and str(body["published_at"]) < last:
            raise Failure("host-pointer-replayed", "an older pointer than one seen")
        seen[body["ref"]] = max(str(body["published_at"]), last or "")
        return body

    # ---------------------------------------------------------- verify

    def verify(self, cache: Cache, pointer: Mapping[str, Any]) -> dict[str, Any]:
        """The manifest, once it and the narinfo check out; nothing is realised."""
        raw = cache.get(str(pointer.get("manifest")))
        if raw is None:
            raise Failure("host-cache-unreachable", "the manifest is missing")
        try:
            manifest = json.loads(raw)
        except ValueError:
            manifest = None
        if not isinstance(manifest, dict):
            raise Failure("host-manifest-mismatch", "the manifest is not JSON")
        if _v.manifest_digest(manifest) != pointer.get("manifest_sha256"):
            raise Failure("host-manifest-mismatch", "the manifest's sha256")
        if not _v.verify_manifest(manifest, self.public):
            raise Failure("host-signature-invalid", "the manifest's signature")
        for field in ("name", "commit", "store_path"):
            if manifest.get(field) != pointer.get(field):
                raise Failure("host-manifest-mismatch", f"the manifest's {field}")
        attribute = self.config.get("attribute")
        if attribute and manifest.get("attribute") != attribute:
            raise Failure("host-manifest-mismatch", "a build of another attribute")
        gate = self.config.get("gate")
        if gate:  # 0.25.0: the publish passed the promotion gate
            pinned = manifest.get("pinned")
            if not (
                isinstance(pinned, dict)
                and pinned.get("input") == gate.get("input")
                and pinned.get("environment") == gate.get("environment")
                and re.fullmatch(r"[0-9a-f]{40}", str(pinned.get("commit")))
                and pinned.get("verified_run")
            ):
                raise Failure(
                    "host-promotion-missing", "a build without its verified pin"
                )
        system = self.config.get("system")
        if system and manifest.get("system") != system:
            raise Failure("host-system-mismatch", "a build for another system")
        store_path = str(manifest["store_path"])
        digest = _STORE_PATH.fullmatch(store_path)
        assert digest is not None
        narinfo = cache.get(f"{digest.group(1)}.narinfo")
        if narinfo is None:
            raise Failure("host-cache-unreachable", "the narinfo is missing")
        text = narinfo.decode("utf-8", "replace")
        if not _v.verify_narinfo(text, self.public):
            raise Failure("host-signature-invalid", "the narinfo's signature")
        fields = _v.narinfo_fields(text)
        if fields.get("StorePath") != store_path or fields.get(
            "NarHash"
        ) != manifest.get("nar_hash"):
            raise Failure("host-narinfo-mismatch", "the narinfo is another path's")
        return manifest

    # ---------------------------------------------------------- realise, switch

    def realise_argv(self, store_path: str) -> list[str]:
        """``nix copy`` from the cache only: no builder, no local build, only
        the cache's key trusted, signatures required."""
        cache = self.config["cache"]
        return [
            self.tools["nix"], *FEATURES, "copy",
            "--from", str(cache["store_url"]),
            "--option", "builders", "",
            "--option", "max-jobs", "0",
            "--option", "substituters", "",
            "--option", "trusted-public-keys", self.public,
            "--option", "require-sigs", "true",
            store_path,
        ]  # fmt: skip

    def realise(self, manifest: Mapping[str, Any]) -> None:
        store_path = str(manifest["store_path"])
        if self._run(self.realise_argv(store_path), REALISE_SECONDS, self.env) != 0:
            raise Failure("host-realise-failed", "nix copy from the cache failed")
        code, text = self._output(
            [self.tools["nix"], *FEATURES, "path-info", "--json", store_path], 600
        )
        info: Any = None
        try:
            info = json.loads(text) if code == 0 else None
        except ValueError:
            info = None
        entry: Any = None
        if isinstance(info, dict):
            # Keyed by the path, by its base name (newer formats), or one entry.
            entry = info.get(store_path, info.get(os.path.basename(store_path)))
            if entry is None and len(info) == 1:
                entry = next(iter(info.values()))
        elif isinstance(info, list):
            entry = next((e for e in info if isinstance(e, dict)), None)
        if not isinstance(entry, dict) or _v.nar_hash(
            str(entry.get("narHash"))
        ) != manifest.get("nar_hash"):
            raise Failure("host-narinfo-mismatch", "the realised path's NarHash")

    def _configure(self, store_path: str, verb: str) -> None:
        argv = [f"{store_path}/bin/switch-to-configuration", verb]
        if self._run(argv, SWITCH_SECONDS) != 0:  # a hang is 124: failed
            raise Failure("host-switch-failed", f"switch-to-configuration {verb}")

    def activate(self, store_path: str) -> None:
        """Run ``store_path`` now (``switch-to-configuration test``): the
        profile and the boot entry stay, so a reboot comes up on the system
        the host booted before (0.26.2). In service mode: set the profile to
        it and restart only the declared units."""
        if self.services:
            self._set_profile(store_path)
            units = [str(unit) for unit in self.services["units"]]
            argv = [self.tools["systemctl"], "restart", *units]
            if self._run(argv, SWITCH_SECONDS) != 0:
                raise Failure("host-switch-failed", f"restart of {' '.join(units)}")
            return
        self._configure(store_path, "test")

    def _set_profile(self, store_path: str) -> None:
        argv = [self.tools["nix_env"], "-p", self.profile, "--set", store_path]
        if self._run(argv, 600) != 0:
            raise Failure("host-switch-failed", "nix-env --set failed")

    def make_default(self, store_path: str) -> None:
        """Make ``store_path`` the system profile and the boot default
        (``nix-env --set``, ``switch-to-configuration boot``): only after its
        checks passed (0.26.2). In service mode the profile moved when it was
        activated, and there is no boot entry: nothing to do."""
        if self.services:
            return
        self._set_profile(store_path)
        self._configure(store_path, "boot")

    # ---------------------------------------------------------- checks

    def _check_once(self, check: Mapping[str, Any], left: float) -> bool:
        kind, target = check["kind"], list(check["target"])
        if kind == "unit":
            argv = [self.tools["systemctl"], "is-active", "--quiet", target[0]]
            return self._run(argv, max(1.0, min(left, 30))) == 0
        if kind == "http":
            try:
                with self.opener(
                    urllib.request.Request(target[0]), timeout=max(1.0, min(left, 10))
                ) as response:
                    return 200 <= int(getattr(response, "status", 200)) < 300
            except Exception:
                return False
        return self._run(target, max(1.0, left)) == 0

    def checks(self) -> list[dict[str, Any]]:
        results = []
        for check in self.config.get("checks") or ():
            start = self.monotonic()
            deadline = start + float(check["timeout"])
            stable = float(check.get("stable") or 0)
            tries, passed, since = 0, False, None
            while True:
                tries += 1
                ok = self._check_once(check, deadline - self.monotonic())
                now = self.monotonic()
                since = (now if since is None else since) if ok else None
                passed = since is not None and now - since >= stable
                if passed or now >= deadline:
                    break
                self.sleep(self.check_interval)
            results.append(
                {
                    "name": check["name"],
                    "kind": check["kind"],
                    "state": "passed" if passed else "failed",
                    "tries": tries,
                    "seconds": round(self.monotonic() - start, 3),
                }
            )
            if not passed:
                break  # the rest would run against a system being rolled back
        return results

    # ---------------------------------------------------------- one poll

    def _stage(
        self, attempt: dict[str, Any], name: str, step: Callable[[], Any]
    ) -> Any:
        started = self.monotonic()
        record: dict[str, Any] = {
            "name": name, "state": "running", "started_at": now_text(self.clock()),
        }  # fmt: skip
        attempt["stages"].append(record)
        self.status["in_progress"] = attempt
        self._save()
        try:
            value = step()
        except Failure as failed:
            record.update(state="failed", reason=failed.code)
            raise
        finally:
            record["seconds"] = round(self.monotonic() - started, 3)
            self.reporter.event(
                "piceli.host.stage", f"{name}: {record['state']}", attempt["trace_id"],
                error=record["state"] == "failed", **{
                    "piceli.stage": name, "piceli.state": record["state"],
                    "piceli.seconds": record["seconds"],
                    "piceli.commit": attempt.get("commit"),
                    "piceli.reason": record.get("reason"),
                },
            )  # fmt: skip
        record["state"] = "passed"
        return value

    def _bad(self, pointer: Mapping[str, Any]) -> bool:
        mark = (self.state.get("bad") or {}).get(str(pointer["commit"]))
        return isinstance(mark, dict) and str(pointer["published_at"]) <= str(
            mark.get("published_at") or ""
        )

    def _load(self) -> tuple[str | None, dict[str, Any]]:
        """Read the state and status; ``(running system, current entry)``."""
        os.makedirs(self.dir, mode=0o755, exist_ok=True)
        self.state = _read_json(os.path.join(self.dir, STATE_FILE))
        self.status = _read_json(os.path.join(self.dir, STATUS_FILE))
        self.status.update(
            schema=STATUS_SCHEMA, host=self.config["host"], build=self.build,
            follow=self.config["follow"],
        )  # fmt: skip
        self.status.setdefault("history", [])
        self.status.pop("in_progress", None)
        self.status.pop("pending", None)
        # 0.45.0: what was recorded under another scope (the system before a
        # move to service mode, or another profile) is not this one's: its
        # current, previous, hold, pending attempt and bad builds are dropped.
        scope = self.services["profile"] if self.services else "system"
        if self.state.get("scope", "system") != scope:
            for key in ("current", "previous", "hold", "pending", "bad"):
                self.state.pop(key, None)
            self.status.pop("previous", None)
            self.status.pop("hold", None)
            self.status.pop("current", None)
        self.state["scope"] = scope
        pending = self.state.get("pending")
        if isinstance(pending, dict):
            self._recover(pending)
        running = self.running()
        current = self.state.get("current") or {}
        if current.get("store_path") != running:
            # Switched outside the agent: the first boot, a nixos-rebuild.
            current = {"store_path": running, "commit": None, "since": now_text()}
            self.state["current"] = current
        self.status.setdefault("current", current)
        return running, current

    def _finish(self) -> None:
        for key in ("current", "previous", "hold"):
            self.status[key] = self.state.get(key)
        self.status.pop("in_progress", None)
        self._save()
        self.reporter.save()

    def run_once(self) -> dict[str, Any]:
        """One poll; the poll's summary (also in the status file)."""
        running, current = self._load()
        poll: dict[str, Any] = {"at": now_text(self.clock()), "state": "idle"}
        hold = self.state.get("hold")
        if isinstance(hold, dict):
            # Pinned or rolled back: no newer build until ``piceli host unpin``.
            poll.update(state="held", reason="host-pinned", hold=hold.get("kind"))
            self.status["poll"] = poll
            self._finish()
            return poll
        try:
            cache = self._cache()
            started = self.monotonic()
            pointer, why = self.resolve(cache)
            resolved = round(self.monotonic() - started, 3)
            if pointer is not None:  # ``piceli host wait`` judges the newest seen
                poll.update(commit=pointer["commit"], tag=_tag(pointer),
                            published_at=pointer["published_at"])  # fmt: skip
            if pointer is None:
                poll["reason"] = why
            elif pointer["store_path"] == running:
                current.update(
                    {k: pointer.get(k) for k in ("commit", "ref", "published_at")},
                    tag=_tag(pointer), store_path=running,
                )  # fmt: skip
                poll["state"] = "current"
            elif self._bad(pointer):
                poll.update(state="skipped", reason="host-build-bad",
                            commit=pointer["commit"])  # fmt: skip
            else:
                attempt = self.deploy(cache, pointer, current, resolved)
                poll.update(state=attempt["state"], reason=attempt.get("reason"),
                            commit=attempt["commit"])  # fmt: skip
        except Failure as failed:
            poll.update(state="rejected" if _refusal(failed.code) else "error",
                        reason=failed.code)  # fmt: skip
            if _refusal(failed.code):
                self.reporter.event(
                    "piceli.host.rejected", failed.detail or failed.code,
                    secrets.token_hex(16), error=True, **{"piceli.reason": failed.code},
                )  # fmt: skip
        self.status["poll"] = poll
        self._finish()
        return poll

    # ---------------------------------------------------------- pin, unpin, rollback

    def control(self, action: str, tag: str | None = None) -> dict[str, Any]:
        """``pin TAG``, ``unpin`` or ``rollback``: what ``piceli host`` runs
        over SSH. ``state`` is ``pinned``, ``unpinned`` or ``rolled-back``;
        ``failed`` (the host is back on the system it ran) or ``rejected``
        (nothing changed), with a ``reason``."""
        running, current = self._load()
        trace = secrets.token_hex(16)
        asked: dict[str, Any] = {"piceli.action": action, "piceli.tag": tag}
        self.reporter.event(
            f"piceli.host.{action}", f"{action} {tag or ''}".strip(), trace, **asked
        )
        result: dict[str, Any] = {"action": action, "state": "rejected",
                                  "reason": None, "attempt": None}  # fmt: skip
        try:
            if action == "unpin":
                result.update(state="unpinned", was=self.state.pop("hold", None))
                # The next poll follows the declared ref now, not in poll_seconds.
                self._run([self.tools["systemctl"], "start", "--no-block",
                           "piceli-host-agent.service"], 30)  # fmt: skip
            elif action == "pin":
                result.update(self._pin(str(tag or ""), current, running, trace))
            elif action == "rollback":
                result.update(self._back(current, running, trace))
            else:
                raise Failure("host-control-unsupported", f"unknown action {action}")
        except Failure as failed:
            result.update(state="rejected", reason=failed.code)
        self.reporter.event(
            f"piceli.host.{action}", f"{action}: {result['state']}", trace,
            error=result["state"] in ("failed", "rejected"),
            **{"piceli.action": action, "piceli.state": result["state"],
               "piceli.reason": result["reason"], "piceli.tag": tag},
        )  # fmt: skip
        self._finish()
        return {**result, **{k: self.state.get(k) for k in ("hold", "current")}}

    def _hold(self, kind: str, entry: Mapping[str, Any]) -> None:
        self.state["hold"] = {
            "kind": kind, "at": now_text(self.clock()),
            **{k: entry.get(k) for k in ("tag", "commit", "ref", "store_path",
                                         "published_at")},
        }  # fmt: skip

    def _outcome(self, attempt: Mapping[str, Any], done: str) -> dict[str, Any]:
        if attempt["state"] == "deployed":
            return {"state": done, "attempt": attempt}
        state = "rejected" if attempt["state"] == "rejected" else "failed"
        return {"state": state, "reason": attempt.get("reason"), "attempt": attempt}

    def _pin(
        self, tag: str, current: dict[str, Any], running: str | None, trace: str
    ) -> dict[str, Any]:
        """Deploy the build of ``tag`` (an older one too) and hold it."""
        if not _TAG.fullmatch(tag) or ".." in tag:
            raise Failure("host-pin-not-found", "a pin names a tag")
        cache = self._cache()
        started = self.monotonic()
        pointer = self.pointer(cache, f"piceli/{self.build}/refs/tags/{tag}.json")
        if pointer is None:
            raise Failure("host-pin-not-found", f"no published build of {tag}")
        if pointer["store_path"] == running:
            current.update(
                {k: pointer.get(k) for k in ("commit", "ref", "published_at")},
                tag=tag, store_path=running,
            )  # fmt: skip
            self._hold("pin", current)
            return {"state": "pinned"}
        attempt = self.deploy(
            cache, pointer, current, round(self.monotonic() - started, 3),
            trigger="pin", trace=trace,
        )  # fmt: skip
        if attempt["state"] == "deployed":
            self._hold("pin", self.state["current"])
        return self._outcome(attempt, "pinned")

    def _back(
        self, current: dict[str, Any], running: str | None, trace: str
    ) -> dict[str, Any]:
        """Switch to the previous system (in the store) and hold it."""
        previous = self.state.get("previous") or {}
        path = str(previous.get("store_path") or "")
        if not _STORE_PATH.fullmatch(path) or path == running:
            raise Failure("host-rollback-unavailable", "no previous system recorded")
        code, _ = self._output(
            [self.tools["nix"], *FEATURES, "path-info", "--json", path], 600
        )
        if code != 0:
            raise Failure("host-rollback-unavailable", "the previous system is gone")
        attempt = self.deploy(None, previous, current, trigger="rollback", trace=trace)
        if attempt["state"] == "deployed":
            self._hold("rollback", self.state["current"])
        return self._outcome(attempt, "rolled-back")

    def deploy(
        self,
        cache: Cache | None,
        pointer: Mapping[str, Any],
        current: Mapping[str, Any],
        resolved: float = 0.0,
        *,
        trigger: str = "poll",
        trace: str | None = None,
    ) -> dict[str, Any]:
        """Verify, realise, switch, check; switch back on a failure.

        ``trigger`` is ``poll``, ``pin`` or ``rollback``; a rollback goes to
        a system already in the store, verified when it was deployed: it
        only switches and checks, and never marks a build bad."""
        local = trigger == "rollback"
        attempt: dict[str, Any] = {
            "id": secrets.token_hex(8),
            "trace_id": trace or secrets.token_hex(16),
            "trigger": trigger,
            "commit": pointer.get("commit"),
            "tag": _tag(pointer),
            "ref": pointer.get("ref"),
            "store_path": pointer["store_path"],
            "published_at": pointer.get("published_at"),
            "from": current.get("store_path"),
            "started_at": now_text(self.clock()),
            "state": "running",
            "stages": []
            if local
            else [
                {
                    "name": "resolve",
                    "state": "passed",
                    "seconds": resolved,
                    "started_at": now_text(self.clock()),
                }
            ],
            "checks": [],
            "rollback": None,
            "prune": None,
            # 0.25.0: the promotion gate's pin of the build (a rollback's: the
            # one recorded when it was deployed; else the verified manifest's).
            "pinned": _pinned(pointer.get("pinned")) if local else None,
        }
        started = self.monotonic()
        trace = attempt["trace_id"]
        self.reporter.event(
            "piceli.host.deploy.start", f"deploy {str(pointer.get('commit'))[:12]}",
            trace, **{"piceli.commit": pointer.get("commit"),
                      "piceli.ref": pointer.get("ref"), "piceli.trigger": trigger,
                      "piceli.store_path": pointer["store_path"]},
        )  # fmt: skip
        switched = booting = False
        try:
            if not local:
                assert cache is not None
                manifest = self._stage(
                    attempt, "verify", lambda: self.verify(cache, pointer)
                )
                attempt["pinned"] = _pinned(manifest.get("pinned"))
                self._stage(attempt, "realise", lambda: self.realise(manifest))
            # 0.26.2: the pending attempt is on disk before the activation:
            # if this process dies (its own switch, a hang, a reboot), the
            # next run rolls it back before anything else.
            attempt["phase"] = "activate"
            self.state["pending"] = self.status["pending"] = attempt
            switched = True
            self._stage(
                attempt, "activate", lambda: self.activate(str(pointer["store_path"]))
            )

            def run_checks() -> None:
                attempt["checks"] = self.checks()
                for result in attempt["checks"]:
                    self.reporter.event(
                        "piceli.host.check", f"{result['name']}: {result['state']}",
                        trace, error=result["state"] != "passed", **{
                            "piceli.check": result["name"],
                            "piceli.state": result["state"],
                            "piceli.seconds": result["seconds"],
                        },
                    )  # fmt: skip
                if any(r["state"] != "passed" for r in attempt["checks"]):
                    raise Failure("host-check-failed", "a check did not pass")

            self._stage(attempt, "checks", run_checks)
            booting = True
            attempt["phase"] = "boot"
            self._stage(
                attempt, "boot", lambda: self.make_default(str(pointer["store_path"]))
            )
        except Failure as failed:
            attempt.update(state="failed", reason=failed.code)
            if _refusal(failed.code):
                attempt["state"] = "rejected"
            if switched and current.get("store_path"):
                attempt["rollback"] = self._rollback(
                    attempt, str(current["store_path"]), profile=booting
                )
                attempt["state"] = "rolled-back"
            if not local and (
                _refusal(failed.code)
                or failed.code in ("host-switch-failed", "host-check-failed")
            ):
                self.state.setdefault("bad", {})[str(pointer["commit"])] = {
                    "published_at": pointer["published_at"], "reason": failed.code,
                }  # fmt: skip
        else:
            attempt["state"] = "deployed"
            self.state["previous"] = dict(current)
            self.state["current"] = {
                "store_path": pointer["store_path"], "commit": pointer.get("commit"),
                "tag": attempt["tag"], "ref": pointer.get("ref"),
                "published_at": pointer.get("published_at"),
                "since": now_text(self.clock()), "pinned": attempt["pinned"],
                # 0.38.0: the release marker's id, the next one's previous.
                "deployment_id": f"{self.config['host']}/{attempt['id']}",
            }  # fmt: skip
        attempt.pop("phase", None)
        self.state.pop("pending", None)
        self.status.pop("pending", None)
        attempt["ended_at"] = now_text(self.clock())
        attempt["seconds"] = round(self.monotonic() - started, 3)
        self.reporter.event(
            "piceli.host.deploy.result", f"{attempt['state']}", trace,
            error=attempt["state"] != "deployed", **{
                "piceli.commit": pointer.get("commit"), "piceli.state": attempt["state"],
                "piceli.trigger": trigger,
                "piceli.reason": attempt.get("reason"),
                "piceli.seconds": attempt["seconds"],
            },
        )  # fmt: skip
        self._marker(attempt, current)
        if attempt["state"] == "deployed":
            attempt["prune"] = self.status["prune"] = self.prune(trace)
        self._record(attempt)
        return attempt

    def _marker(self, attempt: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        """The release marker of a finished attempt (0.38.0): ``succeeded``
        when it runs, ``rolled_back`` when a rollback ended it (a failed
        switch or check, an interrupted run, or ``piceli host rollback``).
        An attempt refused or failed before its switch released nothing."""
        state = attempt.get("state")
        if state == "deployed":
            status = (
                "rolled_back" if attempt.get("trigger") == "rollback" else "succeeded"
            )
        elif state == "rolled-back":
            status = "rolled_back"
        else:
            return
        host = str(self.config["host"])
        previous = {
            "id": before.get("deployment_id"),
            "version": before.get("tag") or before.get("commit"),
            "revision": before.get("commit"),
            "finished_at": before.get("since"),
        }
        attributes = release_marker(
            environment=host,
            deployment_id=f"{host}/{attempt.get('id')}",
            status=status,
            version=attempt.get("tag") or attempt.get("commit"),
            revision=attempt.get("commit"),
            started_at=attempt.get("started_at"),
            finished_at=attempt.get("ended_at"),
            components=[self.build],
            previous=previous if any(previous.values()) else None,
        )
        self.reporter.marker(
            f"release {attributes.get('service.version', '?')}: {status}",
            str(attempt.get("trace_id")),
            status != "succeeded",
            {
                "piceli.trigger": attempt.get("trigger"),
                "host.name": host,
                "piceli.state": state,
                "piceli.reason": attempt.get("reason"),
                **attributes,
            },
        )

    # ---------------------------------------------------------- prune

    def generations(self) -> tuple[dict[int, str], int | None]:
        """The system profile's generations (number -> store path) and the
        current one (``None`` when the profile is not a generation link)."""
        folder, base = os.path.split(self.profile)
        link = re.compile(re.escape(base) + r"-([0-9]+)-link")
        found: dict[int, str] = {}
        try:
            names = os.listdir(folder or ".")
        except OSError:
            return {}, None
        for name in names:
            match = link.fullmatch(name)
            if match is None:
                continue
            try:
                found[int(match.group(1))] = os.readlink(os.path.join(folder, name))
            except OSError:
                continue
        try:
            match = link.fullmatch(os.path.basename(os.readlink(self.profile)))
        except OSError:
            match = None
        current = int(match.group(1)) if match else None
        return found, current if current in found else None

    def _plan_prune(
        self, keep: int
    ) -> tuple[list[int], list[int], list[int], int | None, str | None]:
        """``(delete, kept, protected, current, argument)``: what ``nix-env
        --delete-generations +KEEP`` would delete, minus the newest
        generation of each protected system (current, previous, held) it
        would take; ``argument`` is ``+KEEP`` when nothing had to be
        spared, else ``None`` (the numbers are named)."""
        found, current = self.generations()
        if current is None:
            return [], sorted(found), [], None, None
        ordered = sorted(found, reverse=True)
        # Nix keeps the generations newer than the current one, the
        # current one and KEEP - 1 older ones.
        newest = set(ordered[: ordered.index(current) + keep])
        systems = {
            str(entry.get("store_path"))
            for entry in (self.state.get(k) for k in ("current", "previous", "hold"))
            if isinstance(entry, dict) and entry.get("store_path")
        }
        covered = {found[n] for n in newest}
        protected = sorted(
            max(n for n in ordered if found[n] == path)
            for path in systems - covered
            if path in found.values()
        )
        kept = newest | set(protected)
        delete = sorted(n for n in ordered if n not in kept)
        return (
            delete,
            sorted(kept),
            protected,
            current,
            (None if protected else f"+{keep}"),
        )

    def prune(self, trace: str) -> dict[str, Any]:
        """Delete the generations beyond ``generations`` and collect the
        store; a failure is in the record (``host-prune-failed``), never
        raised."""
        started = self.monotonic()
        keep = max(2, int(self.config.get("generations") or GENERATIONS))
        delete, kept, protected, current, argument = self._plan_prune(keep)
        record: dict[str, Any] = {
            "state": "done", "reason": None, "keep": keep, "current": current,
            "deleted": [], "kept": kept, "protected": protected, "gc": None,
        }  # fmt: skip
        if current is None:
            record.update(state="skipped", reason="host-prune-failed", kept=[])
        elif delete:
            spec = [argument] if argument else [str(n) for n in delete]
            argv = [self.tools["nix_env"], "-p", self.profile,
                    "--delete-generations", *spec]  # fmt: skip
            if self._run(argv, DELETE_SECONDS) == 0:
                record["deleted"] = delete
            else:
                record.update(state="failed", reason="host-prune-failed")
                record["kept"] = sorted(set(kept) | set(delete))
        gc_started = self.monotonic()
        code, text = self._output(
            [self.tools["nix_store"], "--gc"], GC_SECONDS, stderr=True
        )
        freed = _FREED.search(text) if code == 0 else None
        record["gc"] = {
            "state": "done" if code == 0 else "failed",
            "reason": None if code == 0 else "host-prune-failed",
            "paths": int(freed.group(1)) if freed else None,
            "freed": freed.group(2) if freed else None,
            "seconds": round(self.monotonic() - gc_started, 3),
        }
        if code != 0:
            record.update(state="failed", reason="host-prune-failed")
        record["seconds"] = round(self.monotonic() - started, 3)
        self.reporter.event(
            "piceli.host.prune",
            f"prune {record['state']}: {len(record['deleted'])} generations deleted",
            trace, error=record["state"] == "failed", **{
                "piceli.state": record["state"], "piceli.reason": record["reason"],
                "piceli.keep": keep, "piceli.deleted": len(record["deleted"]),
                "piceli.generations_deleted": ",".join(map(str, record["deleted"])),
                "piceli.gc": record["gc"]["state"],
                "piceli.gc_paths": record["gc"]["paths"],
                "piceli.gc_freed": record["gc"]["freed"],
                "piceli.seconds": record["seconds"],
            },
        )  # fmt: skip
        return record

    def _record(self, attempt: dict[str, Any]) -> None:
        self.status["previous"] = self.state.get("previous")
        self.status["last_attempt"] = attempt
        history = [attempt, *self.status.get("history", [])]
        self.status["history"] = history[: int(self.config.get("history") or 20)]

    def _recover(self, pending: dict[str, Any]) -> None:
        """Roll back an attempt a previous run left pending (0.26.2).

        The run died between the activation and the boot step: its own
        switch stopped it, it hung, or the host rebooted. While the new
        system still runs, activate the previous one again (and its boot
        entry, when the boot step had started); after a reboot it already
        runs. The build is marked bad (``host-deploy-interrupted``)."""
        attempt = dict(pending)
        phase = attempt.pop("phase", None)
        back, new = str(attempt.get("from") or ""), str(attempt.get("store_path"))
        if self.running() == new and _STORE_PATH.fullmatch(back):
            record = self._rollback(attempt, back, profile=phase == "boot")
        else:
            record = {"to": back or None, "state": "not-needed", "seconds": 0.0,
                      "reason": None}  # fmt: skip
        for stage in attempt.get("stages") or ():
            if isinstance(stage, dict) and stage.get("state") == "running":
                stage["state"] = "interrupted"
        attempt.update(
            state="rolled-back", reason="host-deploy-interrupted", rollback=record,
            ended_at=now_text(self.clock()),
        )  # fmt: skip
        if attempt.get("trigger") != "rollback" and attempt.get("commit"):
            self.state.setdefault("bad", {})[str(attempt["commit"])] = {
                "published_at": attempt.get("published_at"),
                "reason": "host-deploy-interrupted",
            }  # fmt: skip
        self.reporter.event(
            "piceli.host.deploy.result", "interrupted", str(attempt.get("trace_id")),
            error=True, **{"piceli.commit": attempt.get("commit"),
                           "piceli.state": "rolled-back",
                           "piceli.reason": "host-deploy-interrupted",
                           "piceli.rollback": record["state"]},
        )  # fmt: skip
        self.state.pop("pending", None)
        current = self.state.get("current") or {}
        self._marker(attempt, current)
        self._record(attempt)
        self._save()

    def _rollback(
        self, attempt: dict[str, Any], previous: str, *, profile: bool = False
    ) -> dict[str, Any]:
        """Activate ``previous`` again (``test``); with ``profile`` (the
        boot step had started), make it the profile and boot default too."""
        started = self.monotonic()
        try:
            self.activate(previous)
            if profile:
                self.make_default(previous)
            state = "done"
        except Failure:
            state = "failed"
        record = {
            "to": previous,
            "state": state,
            "seconds": round(self.monotonic() - started, 3),
            "reason": None if state == "done" else "host-rollback-failed",
        }
        self.reporter.event(
            "piceli.host.rollback", f"rollback {state}", attempt["trace_id"],
            error=True, **{"piceli.store_path": previous, "piceli.state": state},
        )  # fmt: skip
        return record


def _tag(pointer: Mapping[str, Any]) -> str | None:
    ref = str(pointer.get("ref") or "")
    return ref[len("refs/tags/") :] if ref.startswith("refs/tags/") else None


def _refusal(code: str) -> bool:
    return code in (
        "host-signature-invalid",
        "host-pointer-replayed",
        "host-manifest-mismatch",
        "host-narinfo-mismatch",
        "host-system-mismatch",
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - on the host
    import fcntl

    parser = argparse.ArgumentParser(prog="piceli-host-agent")
    parser.add_argument("--config", required=True)
    parser.add_argument("action", nargs="?", default="poll", choices=ACTIONS)
    parser.add_argument("tag", nargs="?")
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    os.makedirs(config.get("state_dir") or "/var/lib/piceli-host", exist_ok=True)
    lock = os.path.join(config.get("state_dir") or "/var/lib/piceli-host", "lock")
    with open(lock, "w") as held:
        # A poll skips a busy run; a command waits for the poll in progress.
        deadline = time.monotonic() + (0 if args.action == "poll" else LOCK_WAIT)
        while True:
            try:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    busy = {"state": "busy"} if args.action == "poll" else {
                        "action": args.action, "state": "rejected",
                        "reason": "host-busy",
                    }  # fmt: skip
                    print(json.dumps(busy))
                    return 0
                time.sleep(2)
        agent = Agent(config)
        if args.action == "poll":
            result = agent.run_once()
        else:
            result = agent.control(args.action, args.tag)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - runs on the host
    sys.exit(main())
