"""Encrypted backup and restore of the local secret store (0.25.0).

``piceli secrets backup --to PATH`` streams one tar of every file under
``$PICELI_CREDENTIALS_DIR`` (whatever its kind: the ``NAME.json``
credentials, the registered clusters' ``kubeconfigs/``, kinds a later
version adds) and of ``$PICELI_PROFILES_DIR`` into ``age``, to the
recipients the owner chose. Nothing unencrypted touches the disk: the tar is
written into age's stdin, and age writes ciphertext only. ``piceli secrets
restore PATH`` decrypts with age (identity file, plugin or passphrase, as age
handles them) into memory, validates the archive (no absolute paths, no
``..``, no links or special files), and writes ``0600`` files in ``0700``
directories; a store that is not empty gets a plan first, written only with
``--approve DIGEST``.

A backup needs at least two recipients, kept apart: with one, losing that
key loses the store and its backup together (``secrets-backup-single-recipient``).
``--single-recipient`` allows it, and the manifest, the remembered marker
and :func:`staleness` record it. The recommended pair: an age key whose
identity lives in the password manager, plus an offline age key (paper or
USB; :func:`generate_backup_key`, ``piceli secrets backup-key``) or an
``age-plugin-yubikey`` identity.

The non-secret backup config sits next to the store
(``~/.config/piceli/credentials.backup.json``): the remembered recipients
and the marker of the last backup (time, the archive's sha256, the
recipients, the count and names of what it held). :func:`staleness` compares
it with the store's modification times.

Values are never printed: results carry names, kinds and digests. Importing
this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.infra.machines.credentials import credentials_dir
from piceli.infra.machines.model import InfraError

__all__ = [
    "SCHEMA",
    "backup",
    "config_path",
    "generate_backup_key",
    "incomplete_credentials",
    "parse_archive",
    "parse_recipients",
    "plan_restore",
    "record_backup",
    "remember_recipients",
    "restore",
    "staleness",
    "store_entries",
]

SCHEMA = "piceli.secrets-backup.v1"
CONFIG_SCHEMA = "piceli.secrets-backup-config.v1"
MANIFEST = "manifest.json"
AGE_ENV = "PICELI_AGE"
AGE_KEYGEN_ENV = "PICELI_AGE_KEYGEN"
#: The decrypted archive is held in memory, never on disk: a cap on it.
MAX_ARCHIVE = 64 * 1024 * 1024
AREAS = ("credentials", "profiles")
#: ``save_credential``'s staging files: half-written, never backed up.
_STAGING = ".piceli-credential-"
_AGE_HINT = (
    "install age (nix profile install nixpkgs#age, brew install age, "
    "apt install age) or set PICELI_AGE to its path"
)
#: X25519 (``age1…``), post-quantum and plugin (``age1yubikey1…``) recipients.
_AGE_RECIPIENT = re.compile(r"age1[0-9a-z]{3,4000}")
_SSH_RECIPIENT = re.compile(r"(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]{16,4096}={0,3}")


@dataclass(frozen=True)
class Entry:
    """One file of the store: what the JSON shows (never the value)."""

    area: str
    name: str
    kind: str
    path: str  # inside the archive: ``credentials/…`` or ``profiles/…``
    mtime: float

    def show(self) -> dict[str, str]:
        return {"area": self.area, "name": self.name, "kind": self.kind}


def config_path() -> Path:
    """The non-secret backup config, next to the store: ``<store>.backup.json``."""
    store = credentials_dir()
    return store.with_name(store.name + ".backup.json")


def _profiles_dir() -> Path:
    from piceli.profiles import profiles_dir

    return profiles_dir()


def _roots() -> dict[str, Path]:
    return {"credentials": credentials_dir(), "profiles": _profiles_dir()}


def _iso(at: float | None) -> str | None:
    if at is None:
        return None
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind(area: str, relative: PurePosixPath, data: bytes) -> str:
    if area == "profiles":
        return "profile"
    if relative.parts[0] == "kubeconfigs":
        return "kubeconfig"
    if len(relative.parts) == 1 and relative.suffix == ".json":
        try:
            kind = json.loads(data).get("kind")
        except (ValueError, AttributeError, UnicodeDecodeError):
            kind = None
        if isinstance(kind, str) and re.fullmatch(r"[a-z0-9-]{1,64}", kind):
            return kind
    return "file"


def _name(area: str, relative: PurePosixPath) -> str:
    if len(relative.parts) == 1 and relative.suffix == ".json":
        return relative.stem
    return relative.as_posix()


def _walk(root: Path) -> Iterable[tuple[PurePosixPath, Path | None]]:
    """Every file under ``root`` (``None``: a link or special file, skipped)."""
    if not root.is_dir():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        here = Path(directory)
        for name in sorted(files) + sorted(d for d in dirs if (here / d).is_symlink()):
            path = here / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if name.startswith(_STAGING):
                continue
            regular = not path.is_symlink() and path.is_file()
            yield relative, path if regular else None


def store_entries() -> tuple[list[tuple[Entry, bytes]], list[dict[str, str]]]:
    """Every file of the store and the profiles with its bytes, and the paths
    skipped (links, special files). The bytes stay in memory."""
    found: list[tuple[Entry, bytes]] = []
    skipped: list[dict[str, str]] = []
    for area, root in _roots().items():
        for relative, path in _walk(root):
            if path is None:
                skipped.append({"area": area, "path": relative.as_posix(),
                                "why": "not a regular file"})  # fmt: skip
                continue
            data = path.read_bytes()
            entry = Entry(
                area,
                _name(area, relative),
                _kind(area, relative, data),
                f"{area}/{relative.as_posix()}",
                path.stat().st_mtime,
            )
            found.append((entry, data))
    return found, skipped


# ------------------------------------------------------------- config


def load_config() -> dict[str, Any]:
    try:
        body = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _save_config(body: Mapping[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps({**body, "schema": CONFIG_SCHEMA},
                                    sort_keys=True, indent=2) + "\n")  # fmt: skip
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def record_backup(
    *,
    recipients: Sequence[str],
    sha256: str,
    names: Sequence[str],
    at: float,
    single_recipient: bool = False,
) -> dict[str, Any]:
    """Remember ``recipients`` and the marker of a backup made at ``at``
    (``single_recipient``: made with ``--single-recipient``)."""
    marker = {
        "at": _iso(at),
        "timestamp": at,
        "sha256": sha256,
        "recipients": list(recipients),
        "count": len(names),
        "names": sorted(names),
        "single_recipient": single_recipient,
    }
    _save_config({**load_config(), "recipients": list(recipients),
                  "last_backup": marker})  # fmt: skip
    return marker


def remember_recipients(recipients: Sequence[str]) -> list[str]:
    """Add ``recipients`` to the remembered ones (each once, in order)."""
    config = load_config()
    found = [str(r) for r in config.get("recipients") or ()]
    for recipient in parse_recipients(list(recipients), None):
        if recipient not in found:
            found.append(recipient)
    _save_config({**config, "recipients": found})
    return found


def staleness() -> dict[str, Any]:
    """The last backup's time, the newest file's modification time, whether
    the backup is stale, which names are newer than it (or not in it), and
    whether it was made for a single recipient (``--single-recipient``).
    Reads names and times only, never a value."""
    marker = load_config().get("last_backup")
    marker = marker if isinstance(marker, dict) else {}
    at = marker.get("timestamp")
    at = float(at) if isinstance(at, int | float) else None
    known = set(marker.get("names") or ())
    newest: float | None = None
    newer: set[str] = set()
    for area, root in _roots().items():
        for relative, path in _walk(root):
            if path is None:
                continue
            mtime = path.stat().st_mtime
            newest = mtime if newest is None else max(newest, mtime)
            name = _name(area, relative)
            label = name if area == "credentials" else f"profile:{name}"
            if at is None or mtime > at or label not in known:
                newer.add(label)
    return {
        "last_backup": _iso(at),
        "newest_secret": _iso(newest),
        "stale": bool(newer),
        "newer": sorted(newer),
        "single_recipient": marker.get("single_recipient") is True,
    }


def incomplete_credentials() -> list[dict[str, Any]]:
    """The stored credentials that lack a part (a Porkbun key pair stored as
    one value), as :func:`~.credential_parts.credential_report` gives them."""
    from piceli.infra.machines.credential_parts import credential_reports

    return [report for report in credential_reports() if not report["complete"]]


def _label(entry: Entry) -> str:
    return entry.name if entry.area == "credentials" else f"profile:{entry.name}"


# ------------------------------------------------------------- backup


def age_binary() -> str:
    """``$PICELI_AGE`` or ``age`` on ``PATH`` (``secrets-age-missing``)."""
    found = os.environ.get(AGE_ENV) or shutil.which("age")
    if not found or not Path(found).is_file():
        raise InfraError("secrets-age-missing", f"age is not available; {_AGE_HINT}")
    return found


def parse_recipients(values: Sequence[str], file: Path | None) -> list[str]:
    """``--recipient`` values, then the lines of ``--recipients-file`` (``#``
    comments and blank lines skipped, an SSH key's comment dropped)."""
    lines = list(values)
    if file is not None:
        try:
            text = file.read_text()
        except (OSError, UnicodeDecodeError):
            raise InfraError(
                "secrets-backup-recipient-invalid", "the recipients file is unreadable"
            ) from None
        lines += [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    found: list[str] = []
    for line in lines:
        parts = line.split()
        if parts and parts[0].startswith("ssh-"):
            line = " ".join(parts[:2])
        if not (_AGE_RECIPIENT.fullmatch(line) or _SSH_RECIPIENT.fullmatch(line)):
            # Never echo it: a pasted identity would land in a log.
            raise InfraError(
                "secrets-backup-recipient-invalid",
                f"recipient {len(found) + 1} is not an age (age1...) or SSH "
                "(ssh-ed25519 ...) public key",
            )
        if line not in found:
            found.append(line)
    return found


def _manifest(
    entries: Sequence[Entry], recipients: Sequence[str], at: float, single: bool
) -> bytes:
    from piceli import __version__

    return json.dumps(
        {
            "schema": SCHEMA,
            "created": _iso(at),
            "timestamp": at,
            "piceli": __version__,
            "credentials_dir": str(credentials_dir()),
            "profiles_dir": str(_profiles_dir()),
            "recipients": list(recipients),
            "single_recipient": single,
            "entries": [{**e.show(), "path": e.path} for e in entries],
        },
        sort_keys=True,
        indent=2,
    ).encode()


def _add(tar: tarfile.TarFile, name: str, data: bytes, mtime: float) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o600, int(mtime)
    tar.addfile(info, io.BytesIO(data))


def backup(
    to: Path,
    recipients: Sequence[str] = (),
    *,
    force: bool = False,
    single_recipient: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Write the encrypted archive to ``to``; returns names, kinds and digests.

    At least two recipients, unless ``single_recipient`` (recorded in the
    manifest and the marker)."""
    age = age_binary()
    config = load_config()
    chosen = list(recipients) or list(config.get("recipients") or ())
    entries, skipped = store_entries()
    if not any(e.area == "credentials" for e, _ in entries):
        raise InfraError("secrets-backup-empty", "the secret store holds no file")
    if not chosen:
        raise InfraError(
            "secrets-backup-recipients-missing",
            "no recipients: pass --recipient age1... (repeatable) or "
            "--recipients-file FILE once; they are remembered",
        )
    chosen = parse_recipients(chosen, None)
    if len(chosen) < 2 and not single_recipient:
        raise InfraError(
            "secrets-backup-single-recipient",
            "one recipient: losing that one key loses the store and its backup "
            "together; add a second kept apart (an offline key: piceli secrets "
            "backup-key --out PATH --add), or pass --single-recipient to accept it",
        )
    single = len(chosen) < 2
    target = to.expanduser().absolute()
    for root in _roots().values():
        if target.is_relative_to(root.absolute()):
            raise InfraError(
                "secrets-backup-invalid",
                "the backup cannot be written inside the store",
            )
    if not target.parent.is_dir():
        raise InfraError(
            "secrets-backup-invalid", "the backup's directory does not exist"
        )
    if (target.exists() or target.is_symlink()) and not force:
        raise InfraError(
            "secrets-backup-exists",
            "the backup file exists; pass --force to replace it",
        )
    at = time.time() if now is None else now
    staged = target.with_name(f".{target.name}.partial")
    argv = [
        age,
        "--encrypt",
        *(a for r in chosen for a in ("-r", r)),
        "-o",
        str(staged),
    ]
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
        )
        assert process.stdin is not None
        try:
            # A stream tar written straight into age: no plaintext on disk.
            with tarfile.open(fileobj=process.stdin, mode="w|",
                              format=tarfile.PAX_FORMAT) as tar:  # fmt: skip
                _add(tar, MANIFEST,
                     _manifest([e for e, _ in entries], chosen, at, single), at)  # fmt: skip
                for entry, data in entries:
                    _add(tar, entry.path, data, entry.mtime)
            process.stdin.close()
        except BrokenPipeError:
            pass
        code = process.wait()
        if code != 0 or not staged.is_file():
            raise InfraError(
                "secrets-backup-failed",
                f"age refused to encrypt (exit {code}); check the recipients and "
                "their plugins; nothing was written",
            )
        digest = hashlib.sha256()
        with staged.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        os.chmod(staged, 0o600)
        os.replace(staged, target)
    finally:
        if staged.exists():
            staged.unlink()
    sha256 = "sha256:" + digest.hexdigest()
    marker = record_backup(
        recipients=chosen, sha256=sha256, names=[_label(e) for e, _ in entries],
        at=at, single_recipient=single,
    )  # fmt: skip
    return {
        "state": "backed-up",
        "path": str(target),
        "sha256": sha256,
        "at": marker["at"],
        "recipients": chosen,
        "single_recipient": single,
        "count": len(entries),
        "entries": [e.show() for e, _ in entries],
        "skipped": skipped,
        "config": str(config_path()),
        "staleness": staleness(),
        "incomplete": incomplete_credentials(),
    }


# ------------------------------------------------------------- offline key


def age_keygen_binary() -> str:
    """``$PICELI_AGE_KEYGEN``, else ``age-keygen`` next to ``$PICELI_AGE``,
    else on ``PATH`` (``secrets-age-missing``)."""
    found = os.environ.get(AGE_KEYGEN_ENV)
    if not found and os.environ.get(AGE_ENV):
        sibling = Path(os.environ[AGE_ENV]).with_name("age-keygen")
        found = str(sibling) if sibling.is_file() else None
    found = found or shutil.which("age-keygen")
    if not found or not Path(found).is_file():
        raise InfraError(
            "secrets-age-missing", f"age-keygen is not available; {_AGE_HINT}"
        )
    return found


def generate_backup_key(
    out: Path, *, force: bool = False, add: bool = False
) -> dict[str, Any]:
    """Create an age X25519 identity (with ``age-keygen``) and write it to
    ``out`` (``0600``; never inside the store, never over a file without
    ``force``). Returns its ``age1…`` recipient, never the identity; ``add``
    appends the recipient to the remembered ones."""
    target = out.expanduser().absolute()
    for root in _roots().values():
        if target.is_relative_to(root.absolute()):
            raise InfraError(
                "secrets-backup-key-invalid",
                "the offline key cannot be written inside the store it protects",
            )
    if not target.parent.is_dir():
        raise InfraError(
            "secrets-backup-key-invalid", "the key file's directory does not exist"
        )
    if (target.exists() or target.is_symlink()) and not force:
        raise InfraError(
            "secrets-backup-key-exists",
            "the key file exists (it may be the only copy of a key); choose "
            "another path, or pass --force to replace it",
        )
    binary = age_keygen_binary()
    # The identity comes back on stdout, in memory; it is written once, 0600.
    process = subprocess.run(
        [binary], capture_output=True, text=True, check=False, timeout=60
    )
    identity = next((line for line in process.stdout.splitlines()
                     if line.startswith("AGE-SECRET-KEY-1")), None)  # fmt: skip
    recipient = next((line.split(":", 1)[1].strip()
                      for line in process.stdout.splitlines()
                      if line.startswith("# public key:")), "")  # fmt: skip
    if (
        process.returncode != 0
        or identity is None
        or not _AGE_RECIPIENT.fullmatch(recipient)
    ):
        raise InfraError(
            "secrets-backup-key-failed",
            f"age-keygen did not create a key (exit {process.returncode}); "
            "nothing was written",
        )
    descriptor, staged = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.partial"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(process.stdout)
        os.replace(staged, target)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    del identity, process
    remembered = remember_recipients([recipient]) if add else None
    return {
        "state": "created",
        "path": str(target),
        "recipient": recipient,
        "added": add,
        "recipients": remembered
        if remembered is not None
        else list(load_config().get("recipients") or ()),
    }


# ------------------------------------------------------------- restore


def _invalid(message: str) -> InfraError:
    return InfraError("secrets-restore-archive-invalid", message)


def parse_archive(data: bytes) -> tuple[dict[str, Any], dict[str, tuple[bytes, float]]]:
    """The manifest and the files of a decrypted archive, after validation:
    regular files only, under ``credentials/`` or directly under
    ``profiles/``, no absolute path, ``..``, backslash or duplicate."""
    files: dict[str, tuple[bytes, float]] = {}
    manifest: dict[str, Any] | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for info in tar:
                name = info.name
                path = PurePosixPath(name)
                if info.isdir() and name.rstrip("/") in AREAS:
                    continue
                if (
                    not name
                    or "\\" in name
                    or "\x00" in name
                    or path.is_absolute()
                    or any(part in ("..", ".", "") for part in name.split("/"))
                ):
                    raise _invalid("the archive holds an unsafe path")
                if not info.isreg():
                    raise _invalid("the archive holds a link or special file")
                if name in files or (name == MANIFEST and manifest is not None):
                    raise _invalid("the archive holds a name twice")
                handle = tar.extractfile(info)
                body = handle.read() if handle is not None else b""
                if name == MANIFEST:
                    manifest = json.loads(body)
                    continue
                area = path.parts[0]
                if (
                    area not in AREAS
                    or len(path.parts) < 2
                    or (area == "profiles" and len(path.parts) != 2)
                ):
                    raise _invalid("the archive holds a file outside the store")
                files[name] = (body, float(info.mtime))
    except (tarfile.TarError, ValueError, UnicodeDecodeError, EOFError):
        raise _invalid("the archive is not a Piceli secret-store backup") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise _invalid("the archive is not a Piceli secret-store backup")
    return manifest, files


def decrypt(path: Path, identity: Path | None) -> tuple[bytes, str]:
    """The decrypted bytes of ``path`` (in memory) and its sha256."""
    age = age_binary()
    source = path.expanduser().absolute()
    if not source.is_file():
        raise _invalid("the backup file does not exist")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    argv = [age, "--decrypt"]
    if identity is not None:
        argv += ["-i", str(identity.expanduser().absolute())]
    # stderr stays the user's: age and its plugins ask there (touch, PIN).
    process = subprocess.Popen([*argv, str(source)], stdout=subprocess.PIPE)
    assert process.stdout is not None
    data = process.stdout.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        process.kill()
        process.wait()
        raise _invalid("the decrypted archive is larger than a secret store")
    if process.wait() != 0:
        raise InfraError(
            "secrets-restore-decrypt-failed",
            "age could not decrypt the backup with this identity; nothing was written",
        )
    return data, "sha256:" + digest


def _target(name: str, manifest: Mapping[str, Any]) -> tuple[Path, Entry]:
    area, _, rest = name.partition("/")
    relative = PurePosixPath(rest)
    root = _roots()[area]
    kinds = {e.get("path"): e.get("kind") for e in manifest.get("entries") or ()
             if isinstance(e, dict)}  # fmt: skip
    kind = kinds.get(name)
    entry = Entry(area, _name(area, relative),
                  kind if isinstance(kind, str) else "file", name, 0.0)  # fmt: skip
    return root.joinpath(*relative.parts), entry


def _rebased(name: str, data: bytes, manifest: Mapping[str, Any]) -> bytes:
    """A profile whose kubeconfig was in the old store points at the new one."""
    if not name.startswith("profiles/"):
        return data
    try:
        body = json.loads(data)
        old = PurePosixPath(str(manifest.get("credentials_dir")))
        kubeconfig = PurePosixPath(str(body["kubeconfig"]))
        inside = kubeconfig.relative_to(old)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return data
    moved = str(credentials_dir().joinpath(*inside.parts))
    if moved == body["kubeconfig"]:
        return data
    body["kubeconfig"] = moved
    return (json.dumps(body, sort_keys=True) + "\n").encode()


def plan_restore(
    manifest: Mapping[str, Any], files: Mapping[str, tuple[bytes, float]], sha256: str
) -> dict[str, Any]:
    """What a restore would add, find identical (by digest) and replace."""
    add: list[Entry] = []
    same: list[Entry] = []
    differ: list[Entry] = []
    for name in sorted(files):
        path, entry = _target(name, manifest)
        data = _rebased(name, files[name][0], manifest)
        if not path.is_file():
            add.append(entry)
        elif (
            hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest()
        ):
            same.append(entry)
        else:
            differ.append(entry)
    shown = {
        "archive_sha256": sha256,
        "add": [e.show() for e in add],
        "identical": [e.show() for e in same],
        "differ": [e.show() for e in differ],
    }
    canonical = json.dumps(shown, sort_keys=True).encode()
    empty = not store_entries()[0]
    return {**shown, "digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
            "empty_store": empty}  # fmt: skip


def _write(path: Path, root: Path, data: bytes, mtime: float) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for parent in reversed(path.relative_to(root).parents[:-1]):
        directory = root / parent
        if directory.is_symlink():
            raise _invalid("a directory of the store is a link")
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=_STAGING)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.utime(staged, (mtime, mtime))
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def restore(
    path: Path, identity: Path | None = None, approve: str | None = None
) -> dict[str, Any]:
    """Restore the archive at ``path``; into a store that is not empty only
    with ``approve`` equal to the plan's digest (else the plan is returned
    with ``state: approval-required``)."""
    data, sha256 = decrypt(path, identity)
    manifest, files = parse_archive(data)
    del data
    plan = plan_restore(manifest, files, sha256)
    if not plan["empty_store"]:
        if approve is None:
            return {**plan, "state": "approval-required"}
        if approve != plan["digest"]:
            raise InfraError(
                "secrets-restore-plan-changed",
                "the store or the archive changed since that plan; run the restore "
                "without --approve and approve the new digest",
            )
    names = {(e["area"], e["name"]) for e in plan["add"] + plan["differ"]}
    for name in sorted(files):
        target, entry = _target(name, manifest)
        if (entry.area, entry.name) in names:
            body, mtime = files[name]
            _write(target, _roots()[entry.area], _rebased(name, body, manifest), mtime)
    if not config_path().exists() and manifest.get("recipients"):
        # The new machine remembers the recipients and when this backup was made.
        timestamp = manifest.get("timestamp")
        record_backup(
            recipients=[str(r) for r in manifest["recipients"]],
            sha256=sha256,
            names=[_label(_target(name, manifest)[1]) for name in files],
            at=float(timestamp) if isinstance(timestamp, int | float) else time.time(),
            single_recipient=manifest.get("single_recipient") is True,
        )  # fmt: skip
    return {
        "state": "restored",
        "archive_sha256": sha256,
        "added": plan["add"],
        "replaced": plan["differ"],
        "identical": plan["identical"],
        "credentials_dir": str(credentials_dir()),
        "profiles_dir": str(_profiles_dir()),
    }
