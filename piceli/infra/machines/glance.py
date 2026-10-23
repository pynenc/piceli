"""Upload an :class:`~piceli.infra.machines.model.Image` to OpenStack Glance (0.25.3).

OpenTofu's ``openstack_images_image_v2`` (provider 3.4.0) creates the image
record and sends the file in one ``PUT`` from the machine that runs
``tofu apply``, with no retry, no progress and, when the upload fails, a
half-made image left behind (status ``saving``, tainted in the state).
Glance itself offers no resumable upload: ``PUT /v2/images/{id}/file`` and
the import API's ``glance-direct`` staging (``PUT .../stage``) are each one
stream, so an upload that fails starts over.

Piceli therefore uploads the image itself, before ``tofu apply``, and
OpenTofu adopts it (``tofu import``):

1. the file is checked where it is (size, sha256): this machine, or the
   build host that built it (:class:`~piceli.infra.machines.model.ImageHost`)
   over the container CLI's SSH transport;
2. Piceli creates the image record (the same name, formats, properties and
   ownership tags OpenTofu would give it) and streams the file
   (:mod:`piceli.infra.machines.image_sender`) with a fresh Keystone token
   that is revoked after each attempt; the build host gets that token on
   its stdin, never in an argument, a log or the state;
3. an attempt that fails is retried (a bounded number of times): on the
   same record when Glance put it back to ``queued``, else on a new one
   after deleting the old;
4. when Glance reports ``active``, its ``checksum`` (MD5) and
   ``os_hash_value`` must be the hashes of the bytes sent, and those bytes'
   sha256 the declared one (``infra-image-checksum``);
5. a failed or interrupted upload (``Ctrl-C`` included) deletes every image
   record it created, once its tags show it is Piceli's.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from piceli.infra.machines import image_sender
from piceli.infra.machines.model import ImageHost, InfraError

__all__ = [
    "ACTIVE",
    "Glance",
    "HostSender",
    "LocalSender",
    "ProgressLine",
    "Uploaded",
    "format_bytes",
    "upload_image",
]

ACTIVE = "active"
#: Statuses of an image whose upload never finished.
UNFINISHED = ("queued", "saving", "uploading", "importing", "killed")
#: Attempts of one upload (each starts over: Glance cannot resume).
ATTEMPTS = 3
#: Seconds before the second and third attempts.
BACKOFF = (10.0, 30.0)
#: Seconds Glance may take to make a sent image ``active``.
ACTIVE_SECONDS = 900.0
#: A token Piceli keeps for its own calls is renewed this long before expiry.
_RENEW = 600.0
#: The hashes Glance may report in ``os_hash_algo`` that the sender computes.
_ALGOS = ("sha512", "sha256", "md5")
Opener = Callable[..., Any]


class GlanceError(Exception):
    """A Glance or Keystone call failed: its HTTP status (0: no answer)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}" if status else "no answer")
        self.status = status


def _expires(text: Any) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 3600


@dataclass
class Glance:
    """Glance (and Keystone for its tokens) with an application credential."""

    auth_url: str
    region: str
    credential_id: str = field(repr=False)
    secret: str = field(repr=False)
    opener: Opener = urllib.request.urlopen
    _token: str | None = field(default=None, repr=False)
    _expires_at: float = 0.0
    _url: str | None = None

    # ------------------------------------------------------------ tokens
    def _authenticate(self) -> tuple[str, float, Mapping[str, Any]]:
        body = {"auth": {"identity": {
            "methods": ["application_credential"],
            "application_credential": {"id": self.credential_id, "secret": self.secret},
        }}}  # fmt: skip
        request = urllib.request.Request(
            self.auth_url.rstrip("/") + "/auth/tokens",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=60) as answer:
                token = str(answer.headers["X-Subject-Token"])
                document = json.load(answer).get("token") or {}
        except urllib.error.HTTPError as error:
            raise GlanceError(error.code) from None
        except (OSError, ValueError, KeyError):
            raise GlanceError(0) from None
        return token, _expires(document.get("expires_at")), document

    def token(self) -> str:
        """A token for Piceli's own calls (renewed before it expires)."""
        if self._token is None or self._expires_at - time.time() < _RENEW:
            token, expires, document = self._authenticate()
            self._token, self._expires_at = token, expires
            if self._url is None:
                self._url = self._endpoint(document)
        return self._token

    def fresh_token(self) -> str:
        """A token of its own for one upload attempt; :meth:`revoke` it after."""
        token, _expires_at, document = self._authenticate()
        if self._url is None:
            self._url = self._endpoint(document)
        return token

    def revoke(self, token: str) -> None:
        """Revoke ``token`` (best effort: it expires anyway)."""
        request = urllib.request.Request(
            self.auth_url.rstrip("/") + "/auth/tokens",
            headers={"x-auth-token": token, "x-subject-token": token},
            method="DELETE",
        )
        try:
            with self.opener(request, timeout=30):
                pass
        except (OSError, urllib.error.HTTPError):
            pass

    def _endpoint(self, document: Mapping[str, Any]) -> str:
        for service in document.get("catalog") or ():
            if service.get("type") != "image":
                continue
            for endpoint in service.get("endpoints") or ():
                if endpoint.get("interface") == "public" and self.region in (
                    endpoint.get("region"),
                    endpoint.get("region_id"),
                ):
                    return str(endpoint["url"]).rstrip("/")
        raise InfraError(
            "infra-image-upload-failed",
            f"the cloud's catalog has no image service in region {self.region}",
        )

    @property
    def url(self) -> str:
        self.token()
        assert self._url is not None
        return self._url

    # ------------------------------------------------------------ calls
    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        content_type: str = "application/json",
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers={"x-auth-token": self.token(), "content-type": content_type,
                     "accept": "application/json"},
            method=method,
        )  # fmt: skip
        try:
            with self.opener(request, timeout=120) as answer:
                text = answer.read()
        except urllib.error.HTTPError as error:
            raise GlanceError(error.code) from None
        except OSError:
            raise GlanceError(0) from None
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            return {}

    def create(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self.call("POST", "/v2/images", dict(body)))

    def get(self, image_id: str) -> dict[str, Any] | None:
        try:
            return dict(self.call("GET", f"/v2/images/{image_id}"))
        except GlanceError as error:
            if error.status == 404:
                return None
            raise

    def delete(self, image_id: str) -> bool:
        """Delete ``image_id``; ``False`` when it was already gone."""
        try:
            self.call("DELETE", f"/v2/images/{image_id}")
        except GlanceError as error:
            if error.status == 404:
                return False
            raise
        return True

    def find(self, tags: Sequence[str]) -> list[dict[str, Any]]:
        """The project's images that carry every one of ``tags``."""
        query = urllib.parse.urlencode(
            [("tag", tag) for tag in tags] + [("limit", "100")]
        )
        found = self.call("GET", f"/v2/images?{query}").get("images") or []
        return [dict(item) for item in found if isinstance(item, dict)]

    def file_url(self, image_id: str) -> str:
        return f"{self.url}/v2/images/{image_id}/file"

    def import_methods(self) -> list[str]:
        """The cloud's import methods (``GET /v2/info/import``), if it says."""
        try:
            found = self.call("GET", "/v2/info/import")
        except GlanceError:
            return []
        value = (found.get("import-methods") or {}).get("value")
        return [str(item) for item in value] if isinstance(value, list) else []


# ------------------------------------------------------------------ senders


class Sender(Protocol):
    @property
    def where(self) -> str:
        """Where the file is sent from: ``this machine`` or ``build host NAME``."""

    def check(self) -> dict[str, Any]: ...

    def send(
        self, url: str, token: str, progress: Callable[[int], None]
    ) -> dict[str, Any]: ...


@dataclass
class LocalSender:
    """The file on this machine, sent from this process."""

    path: Path
    where: str = "this machine"

    def check(self) -> dict[str, Any]:
        return image_sender.check(str(self.path))

    def send(
        self, url: str, token: str, progress: Callable[[int], None]
    ) -> dict[str, Any]:
        return image_sender.send(
            {"file": str(self.path), "url": url, "token": token}, progress
        )


Popen = Callable[..., "subprocess.Popen[bytes]"]
#: How the sender reaches a docker host's Nix store (as ``dev nix build``).
NIX_MOUNT = ("-v", "/nix:/nix:ro")


def host_argv(host: ImageHost) -> list[str]:
    """The container that runs the sender on ``host``: the run image, the
    host's ``/nix`` read-only, no capabilities; its input on stdin."""
    prefix = [host.cli]
    prefix += (
        ["--host", host.context]
        if "://" in host.context
        else ["--context", host.context]
    )
    return [
        *prefix, "run", "--rm", "-i", "--user", "65532:65532",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *NIX_MOUNT,
        "--entrypoint", "python3", host.image, "-I", "-c", sender_source(),
    ]  # fmt: skip


def sender_source() -> str:
    """The sender's source, run with ``python3 -I -c`` on a build host."""
    return Path(image_sender.__file__).read_text()


@dataclass
class HostSender:
    """The file on a build host, sent from there (the container CLI over SSH)."""

    host: ImageHost
    popen: Popen = subprocess.Popen
    #: The sender's error output, last lines (never holds the token).
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"build host {self.host.name}"

    def _run(
        self,
        request: Mapping[str, Any],
        progress: Callable[[int], None],
        secret: str = "",
    ) -> dict[str, Any]:
        try:
            process = self.popen(
                host_argv(self.host),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError:
            return {"state": "failed", "why": "host"}
        result: dict[str, Any] = {}
        try:
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps(dict(request)).encode())
                process.stdin.close()
            except OSError:
                pass
            for raw in iter(process.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith(image_sender.PROGRESS_MARKER):
                    try:
                        sent = json.loads(line[len(image_sender.PROGRESS_MARKER) :])
                        progress(int(sent["sent"]))
                    except (ValueError, KeyError, TypeError):
                        pass
                elif line.startswith(image_sender.RESULT_MARKER):
                    try:
                        result = json.loads(line[len(image_sender.RESULT_MARKER) :])
                    except ValueError:
                        result = {}
            error = process.stderr.read() if process.stderr is not None else b""
            process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        text = error.decode("utf-8", "replace")
        if secret:
            text = text.replace(secret, "<redacted>")
        self.stderr_tail = [line for line in text.splitlines() if line.strip()][-20:]
        if not isinstance(result, dict) or not result.get("state"):
            return {"state": "failed", "why": "host"}
        return result

    def check(self) -> dict[str, Any]:
        return self._run({"mode": "check", "file": self.host.path}, lambda _s: None)

    def send(
        self, url: str, token: str, progress: Callable[[int], None]
    ) -> dict[str, Any]:
        request = {"mode": "send", "file": self.host.path, "url": url, "token": token}
        return self._run(request, progress, secret=token)


# ------------------------------------------------------------------ progress


def format_bytes(count: float) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{int(count)} B"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


@dataclass
class ProgressLine:
    """Bytes sent, total, rate and ETA, at most once per ``interval``."""

    write: Callable[[str, bool], None]
    clock: Callable[[], float] = time.monotonic
    interval: float = 1.0
    _start: float | None = None
    _last: float = 0.0
    _attempt: int = 0

    def __call__(self, event: Mapping[str, Any]) -> None:
        now = self.clock()
        if event.get("attempt") != self._attempt:
            self._attempt = int(event.get("attempt") or 0)
            self._start, self._last = now, 0.0
        assert self._start is not None
        sent, total = int(event.get("sent") or 0), int(event.get("total") or 0)
        done = total > 0 and sent >= total
        if not done and now - self._last < self.interval:
            return
        self._last = now
        self.write(self.text(event, now - self._start), done)

    @staticmethod
    def text(event: Mapping[str, Any], elapsed: float) -> str:
        sent, total = int(event.get("sent") or 0), int(event.get("total") or 0)
        rate = sent / elapsed if elapsed > 0 else 0.0
        share = f" ({100 * sent // total}%)" if total else ""
        eta = (
            f", ETA {_duration((total - sent) / rate)}"
            if rate > 0 and total > sent
            else ""
        )
        attempt = int(event.get("attempt") or 1)
        retry = f" [attempt {attempt}]" if attempt > 1 else ""
        speed = f", {format_bytes(rate)}/s" if elapsed >= 0.5 else ""
        return (
            f"  image {event.get('image')}: {format_bytes(sent)} of "
            f"{format_bytes(total)}{share}{speed}{eta}{retry}"
        )


# ------------------------------------------------------------------ upload


@dataclass(frozen=True)
class Uploaded:
    """A verified, ``active`` image Piceli uploaded."""

    id: str
    bytes: int
    seconds: float
    attempts: int
    where: str
    deleted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        rate = self.bytes / self.seconds if self.seconds > 0 else None
        return {
            "id": self.id,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 1),
            "bytes_per_second": None if rate is None else int(rate),
            "attempts": self.attempts,
            "from": self.where,
        }


def ours(record: Mapping[str, Any] | None, tags: Sequence[str]) -> bool:
    """Whether ``record`` carries every ownership tag in ``tags``."""
    found = set(record.get("tags") or ()) if isinstance(record, Mapping) else set()
    return bool(tags) and set(tags) <= found


def _delete_ours(glance: Glance, image_id: str, tags: Sequence[str]) -> bool:
    """Delete ``image_id`` if its tags show it is Piceli's; ``True`` if deleted."""
    try:
        record = glance.get(image_id)
        if record is None or not ours(record, tags):
            return False
        return glance.delete(image_id)
    except GlanceError:
        return False


def remove_stale(
    glance: Glance, tags: Sequence[str], keep: Sequence[str] = ()
) -> list[dict[str, str]]:
    """Delete the images carrying ``tags`` (this image's ownership tags)
    that the state does not hold (``keep``): half-made uploads of an earlier
    run. What was deleted: id and status."""
    removed = []
    for record in glance.find(tags):
        image_id = str(record.get("id") or "")
        if image_id and image_id not in keep and ours(record, tags):
            if glance.delete(image_id):
                removed.append({"id": image_id, "status": str(record.get("status"))})
    return removed


def _verify(record: Mapping[str, Any], sent: Mapping[str, Any]) -> str | None:
    """Why Glance's copy is not the bytes sent (``None``: it is)."""
    if record.get("size") is not None and int(record["size"]) != int(sent["bytes"]):
        return "size"
    checksum = record.get("checksum")
    if checksum and checksum != sent.get("md5"):
        return "checksum"
    algo, value = record.get("os_hash_algo"), record.get("os_hash_value")
    if algo in _ALGOS and value and value != sent.get(str(algo)):
        return "os_hash_value"
    if not checksum and not (algo in _ALGOS and value):
        return "no-hash"
    return None


def upload_image(
    glance: Glance,
    *,
    name: str,
    sha256: str,
    body: Mapping[str, Any],
    sender: Sender,
    tags: Sequence[str],
    attempts: int = ATTEMPTS,
    progress: Callable[[Mapping[str, Any]], None] = lambda _event: None,
    log: Callable[[str], None] = lambda _line: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    active_seconds: float = ACTIVE_SECONDS,
) -> Uploaded:
    """Create the image record, send the file, verify it; the ``active`` image.

    ``body`` is the record (name, formats, visibility, tags, properties);
    ``tags`` are the ownership tags a record must carry to be deleted.
    """
    checked = sender.check()
    log(f"check on {sender.where}: {checked.get('state')}")
    for line in getattr(sender, "stderr_tail", ()) or ():
        log(f"  | {line}")
    if checked.get("state") == "failed":
        raise InfraError(
            "infra-image-unavailable",
            f"image {name}: {sender.where} could not be reached or could not run "
            "the upload (see the log)",
        )
    if checked.get("state") != "ok":
        raise InfraError(
            "infra-image-unavailable",
            f"image {name}: the file is not on {sender.where} any more; build it "
            "again, or upload the local copy (Image.from_build(..., upload='local'))",
        )
    if checked.get("sha256") != sha256:
        raise InfraError(
            "infra-image-checksum",
            f"image {name}: the file on {sender.where} does not have the declared "
            "sha256; nothing was uploaded",
        )
    total = int(checked["bytes"])
    created: list[str] = []
    record: dict[str, Any] | None = None
    started = clock()
    outcome: dict[str, Any] = {}
    try:
        attempt = 0
        for attempt in range(1, attempts + 1):
            if record is None:
                record = glance.create(body)
                created.append(str(record["id"]))
                log(f"attempt {attempt}: created image record {record['id']}")
            image_id = str(record["id"])

            def sent_now(count: int, attempt: int = attempt) -> None:
                progress({"image": name, "sent": count, "total": total,
                          "attempt": attempt, "from": sender.where})  # fmt: skip

            token = glance.fresh_token()
            try:
                outcome = sender.send(glance.file_url(image_id), token, sent_now)
            finally:
                glance.revoke(token)
                del token
            log(
                f"attempt {attempt}: "
                + " ".join(
                    str(outcome.get(k)) for k in ("state", "why") if outcome.get(k)
                )
                + f" after {outcome.get('bytes', 0)} bytes"
            )
            for line in getattr(sender, "stderr_tail", ()) or ():
                log(f"  | {line}")
            if outcome.get("state") == "sent":
                break
            if outcome.get("why") in ("missing", "changed"):
                raise InfraError(
                    "infra-image-unavailable",
                    f"image {name}: the file on {sender.where} could not be read",
                )
            now = glance.get(image_id)
            status = None if now is None else now.get("status")
            if status == "queued":
                log(f"attempt {attempt}: Glance put {image_id} back to queued; reused")
            else:
                log(f"attempt {attempt}: {image_id} is {status}; deleted")
                if glance.delete(image_id):
                    created.remove(image_id)
                record = None
            if attempt < attempts:
                pause = BACKOFF[min(attempt, len(BACKOFF)) - 1]
                log(f"retrying in {pause:.0f}s")
                sleep(pause)
        else:
            raise InfraError(
                "infra-image-upload-failed",
                f"image {name}: the upload from {sender.where} failed {attempts} "
                f"times ({outcome.get('why') or 'unknown'}); the half-made image "
                "was deleted",
            )
        assert record is not None
        image_id = str(record["id"])
        deadline = clock() + active_seconds
        while True:
            now = glance.get(image_id) or {}
            status = now.get("status")
            if status == ACTIVE:
                break
            if status not in ("saving", "queued", "importing") or clock() > deadline:
                raise InfraError(
                    "infra-image-upload-failed",
                    f"image {name}: Glance did not make the upload active ({status})",
                )
            sleep(3.0)
        why = None if outcome.get("sha256") == sha256 else "sha256"
        why = why or _verify(now, outcome)
        if why is not None:
            log(f"verification failed: {why}")
            raise InfraError(
                "infra-image-checksum",
                f"image {name}: Glance's copy does not match what was sent ({why}); "
                "it was deleted",
            )
        log(f"active: {image_id} ({now.get('os_hash_algo')} verified)")
        created.remove(image_id)
        return Uploaded(image_id, total, clock() - started, attempt, sender.where)
    except BaseException:
        for image_id in list(created):
            if _delete_ours(glance, image_id, tags):
                log(f"deleted the image record {image_id} this upload made")
            else:
                log(f"could not delete the image record {image_id}")
        raise


def write_stderr(text: str, final: bool = False) -> None:
    """A progress line on stderr: overwritten in place on a terminal."""
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K" + text + ("\n" if final else ""))
    else:
        sys.stderr.write(text + "\n")
    sys.stderr.flush()


def stderr_progress() -> ProgressLine:
    """Progress on stderr: once a second on a terminal, else every 10 s."""
    return ProgressLine(write_stderr, interval=1.0 if sys.stderr.isatty() else 10.0)
