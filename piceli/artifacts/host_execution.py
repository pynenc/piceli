"""One run of a host build plan: sync the stage, run the tools, assemble images.

See :mod:`piceli.artifacts.host_build`. The stage of a build lives at the
same path every run (``<cache>/stage/<name>``) and is *synchronised*, not
recreated: a file whose content is unchanged keeps its mtime, a changed or
new file gets the current time, and a file no longer declared is removed. The
compiler's freshness checks then see exactly the edited files, and the shared
target directory keeps everything else.

Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.artifacts.build_context import (
    BuildContextError,
    ContextManifest,
    _open_relative,
    verify_staged,
)
from piceli.artifacts.build_spec import (
    PROCESS_OUTPUT_BUDGET,
    BuildSpecError,
    Runner,
    _BuildLog,
)
from piceli.artifacts.host_build import (
    _PASSTHROUGH,
    HostBuildGrant,
    HostBuildPlan,
    HostBuildSpec,
    HostImage,
    lock_file,
)
from piceli.artifacts.image_manifest import scan_image_stream
from piceli.artifacts.oci_layers import (
    BaseImage,
    BlobStore,
    Descriptor,
    ImageSettings,
    LayerEntry,
    LayerError,
    assemble,
    collect,
    pull_base,
    write_archive,
    write_layer,
)
from piceli.artifacts.process import ProcessLimits

MAX_ARCHIVE_BYTES = 16 * 1024**3


def sync_context(root: Path, manifest: ContextManifest, destination: Path) -> int:
    """Make ``destination`` hold exactly the manifest's files; returns how many changed.

    Unchanged files (same size and sha256) are left alone, so they keep their
    mtime; others are copied (re-hashed against the manifest while copied)
    and get the current time. Anything else below ``destination`` is removed.
    """
    destination.mkdir(parents=True, exist_ok=True)
    wanted = {item.path: item for item in manifest.files}
    changed = 0
    for item in manifest.files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        if _same(target, item.sha256, item.size):
            _chmod(target, item.executable)
            continue
        for parent in reversed(target.parents):
            if parent == destination or destination not in parent.parents:
                continue
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                parent.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_dir():
            _remove(target)
        temporary = target.with_name(f".piceli-{os.urandom(6).hex()}")
        fd = _open_relative(root, item.path)
        hasher, size = hashlib.sha256(), 0
        try:
            with os.fdopen(os.dup(fd), "rb") as source, open(temporary, "xb") as sink:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > item.size:
                        break
                    hasher.update(block)
                    sink.write(block)
            if size != item.size or "sha256:" + hasher.hexdigest() != item.sha256:
                raise BuildContextError(
                    "context-changed", "context file changed while staging"
                )
            _chmod(temporary, item.executable)
            os.replace(temporary, target)
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()
        changed += 1
    keep = {
        str(parent)
        for path in wanted
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    } | set(wanted)
    for current, directories, files in os.walk(destination, topdown=False):
        here = Path(current)
        for name in [*files, *directories]:
            path = here / name
            relative = path.relative_to(destination).as_posix()
            if relative not in keep:
                _remove(path)
    return changed


def _same(path: Path, sha256: str, size: int) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        with path.open("rb") as stream:
            return (
                "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest() == sha256
            )
    except OSError:
        return False


def _chmod(path: Path, executable: bool) -> None:
    mode = 0o755 if executable else 0o644
    if stat.S_IMODE(path.lstat().st_mode) != mode:
        os.chmod(path, mode)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


class HostExecution:
    """Runs a `HostBuildPlan`; see the module docstring."""

    def __init__(
        self,
        plan: HostBuildPlan,
        grant: HostBuildGrant,
        cache: Path,
        runner: Runner,
        log: _BuildLog,
        cancel: threading.Event | None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.plan = plan
        self.spec: HostBuildSpec = plan.spec
        self.grant = grant
        self.cache = cache
        self.runner = runner
        self.log = log
        self.cancel = cancel
        self.client_factory = client_factory
        self.steps: list[dict[str, Any]] = []
        self.store = BlobStore(cache)
        self.stage = cache / "stage" / self.spec.name
        # One target directory per architecture and page size: a cache shared
        # by nodes with other page sizes would rebuild jemalloc and everything
        # above it on each switch (B28).
        facts = plan.facts
        self.target = cache / "target" / f"{facts.architecture}-{facts.page_size}"
        self.total = len(plan.commands)

    # ---------------------------------------------------------------- run
    def execute(self, output_dir: Path) -> dict[str, Any]:
        guard = lock_file(self.cache / "stage" / f"{self.spec.name}.lock")
        try:
            changed = self._stage()
            self.log.say(f"[piceli] stage: {changed} file(s) changed since last build")
            self.target.mkdir(parents=True, exist_ok=True)
            environment = self._environment()
            cwd = self.stage.joinpath(*PurePosixPath(self.spec.workdir).parts)
            for index, command in enumerate(self.plan.commands, start=1):
                self._command(index, command, cwd, environment)
            self._verify_consumed()
            images_dir = output_dir / "images"
            if images_dir.exists():
                shutil.rmtree(images_dir)
            images_dir.mkdir(parents=True)
            bases: dict[str, BaseImage] = {}
            images = {
                image.name: self._image(image, images_dir, bases)
                for image in self.plan.images
            }
        except LayerError as error:
            raise BuildSpecError(
                error.code, str(error), steps=tuple(self.steps)
            ) from None
        finally:
            guard.close()
        return {"files": {}, "images": images}

    def _stage(self) -> int:
        self.stage.mkdir(parents=True, exist_ok=True)
        wanted = {item.target or item.name for item in self.spec.contexts}
        for entry in list(self.stage.iterdir()):
            if entry.name not in wanted:
                _remove(entry)
        changed = 0
        for context in self.spec.contexts:
            try:
                changed += sync_context(
                    self.plan.context_roots[context.name],
                    self.plan.contexts[context.name],
                    self.stage.joinpath(
                        *PurePosixPath(context.target or context.name).parts
                    ),
                )
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
        return changed

    def _environment(self) -> dict[str, str]:
        environment = {
            key: os.environ[key] for key in _PASSTHROUGH if key in os.environ
        }
        directories: list[str] = []
        for path, _ in self.plan.tools.values():
            if str(path.parent) not in directories:
                directories.append(str(path.parent))
        environment["PATH"] = os.pathsep.join([*directories, os.defpath])
        environment["CARGO_TARGET_DIR"] = str(self.target)
        environment["SOURCE_DATE_EPOCH"] = str(self.spec.source_date_epoch)
        environment.update(self.plan.env)
        return environment

    def _command(
        self, index: int, command: tuple[str, ...], cwd: Path, env: Mapping[str, str]
    ) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise BuildSpecError(
                "cancelled", "build cancelled", steps=tuple(self.steps)
            )
        if self.grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        path, pin = self.plan.tools[command[0]]
        self._verify_tools()
        step = {
            "platform": self.plan.platform,
            "kind": f"command-{index}",
            "tool": command[0],
        }
        label = f"{self.plan.platform} command {index}/{self.total} ({command[0]})"
        self.log.line(f"### {label} started")
        self.log.say(f"[piceli] {label}: running")
        receipt, _, _ = self.runner(
            [str(path), *command[1:]],
            cwd,
            ProcessLimits(self.spec.timeout_seconds, PROCESS_OUTPUT_BUDGET),
            dict(env),
            self.grant.expires_at,
            on_output=self.log.feed,
        )
        self._verify_tools()
        state = receipt["state"]
        seconds = round(float(receipt["seconds"]), 3)
        self.steps.append(
            {
                **step,
                "state": state,
                "exit_code": receipt["exit_code"],
                "seconds": seconds,
            }
        )
        self.log.end_step()
        self.log.line(f"### {label} {state} {seconds}s")
        self.log.say(f"[piceli] {label}: {state} in {seconds}s")
        if state != "succeeded":
            code = "build-timed-out" if state == "timed-out" else "build-failed"
            raise BuildSpecError(
                code, "host build step failed", steps=tuple(self.steps)
            )

    def _verify_tools(self) -> None:
        for _, pin in self.plan.tools.values():
            try:
                pin.verify()
            except (OSError, ValueError):
                raise BuildSpecError(
                    "tool-pin-mismatch", "a host tool changed", steps=tuple(self.steps)
                ) from None

    def _verify_consumed(self) -> None:
        for context in self.spec.contexts:
            try:
                verify_staged(
                    self.plan.context_roots[context.name],
                    self.plan.contexts[context.name],
                )
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
        origin = self.spec.origin
        if origin is not None:
            try:
                same = (
                    HostBuildSpec.from_toml(origin).spec_sha256 == self.spec.spec_sha256
                )
            except BuildSpecError:
                same = False
            if not same:
                raise BuildSpecError("spec-changed", "build spec changed")

    # -------------------------------------------------------------- images
    def _source(self, root: Path, path: str) -> Path:
        source = root.joinpath(*PurePosixPath(path).parts)
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            raise BuildSpecError(
                "host-output-missing",
                f"the build produced no {path!r} for its image",
                steps=tuple(self.steps),
            ) from None
        if not resolved.is_relative_to(root.resolve()):
            raise BuildSpecError(
                "host-output-missing", "an image file leaves its directory"
            )
        return source

    def _image(
        self, image: HostImage, images_dir: Path, bases: dict[str, BaseImage]
    ) -> dict[str, Any]:
        platform, epoch = self.plan.platform, self.spec.source_date_epoch
        base = None
        if image.base is not None:
            key = image.base.ref
            if key not in bases:
                self.log.say(f"[piceli] base {image.base.image}: pulling by digest")
                bases[key] = pull_base(
                    key,
                    platform,
                    self.store,
                    client_factory=self.client_factory,
                    cancel=self.cancel,
                )
            base = bases[key]
        layers: list[tuple[Descriptor, str]] = []
        files: list[tuple[str, str, int]] = []
        if image.dirs:
            entries = [
                LayerEntry(str(parent), "dir", 0o755)
                for item in image.dirs
                for parent in reversed(PurePosixPath(item.path).parents)
                if str(parent) != "."
            ] + [
                LayerEntry(item.path, "dir", item.mode, uid=item.uid, gid=item.gid)
                for item in image.dirs
            ]
            written = write_layer(self.store, entries, mtime=epoch, cancel=self.cancel)
            layers.append((written.descriptor, "directories"))
        sources = [(self.stage, src, dest) for src, dest in image.files] + [
            (self.target, src, dest) for src, dest in image.target_files
        ]
        for root, src, dest in sorted(sources, key=lambda item: item[2]):
            entries = collect(self._source(root, src), dest)
            written = write_layer(self.store, entries, mtime=epoch, cancel=self.cancel)
            layers.append((written.descriptor, f"copy {dest}"))
            files.extend(written.files)
        built = assemble(
            self.store,
            base,
            layers,
            ImageSettings(
                entrypoint=image.entrypoint,
                cmd=image.cmd,
                user=image.user,
                workdir=image.workdir,
                env=image.env,
            ),
            platform,
            epoch=epoch,
        )
        archive = write_archive(self.store, built, images_dir / f"{image.name}.oci.tar")
        with archive.open("rb") as stream:
            push = scan_image_stream(stream, max_bytes=MAX_ARCHIVE_BYTES)
        if (
            push.config_digest != built.config.digest
            or push.manifest_digest != built.manifest.digest
            or "/".join(push.platform.split("/")[:2]) != platform
        ):
            raise BuildSpecError(
                "host-build-invalid", "assembled image is inconsistent"
            )
        own = {item.digest for item, _ in layers}
        self.log.say(
            f"[piceli] image {image.name}: {built.manifest.digest[7:19]} "
            f"({len(built.layers)} layers, {len(own)} built here)"
        )
        return {
            "image_id": built.config.digest,
            "digest": built.manifest.digest,
            "manifest_size": built.manifest.size,
            "platform": platform,
            "ref": f"{image.repository}@{built.manifest.digest}",
            "archive": f"images/{image.name}.oci.tar",
            "size_bytes": sum(item.size for item in built.layers) + built.config.size,
            "base": (
                {"reference": base.reference, "manifest": base.manifest_digest}
                if base is not None
                else None
            ),
            "layers": [
                {
                    "digest": item.digest,
                    "diff_id": item.diff_id,
                    "size": item.size,
                    "origin": "build" if item.digest in own else "base",
                }
                for item in built.layers
            ],
            "files": [
                {"path": "/" + path, "sha256": sha, "size": size}
                for path, sha, size in sorted(files)
            ],
        }
