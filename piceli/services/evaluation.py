"""Approval-bound source evaluation in a pinned, credential-free Docker container.

Only explicit files and public render inputs cross the container boundary. The
host never imports consumer code. Docker is the isolation boundary, and its
locally configured daemon and renderer image are trusted installation inputs.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import JsonValue

from piceli.artifacts.process import ProcessLimits, ToolPin, _run_process
from piceli.checks.model import PythonCheck
from piceli.k8s.ops.discovery import public_manifest
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
    ResourceRef,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef, private_directory
from piceli.k8s.release_spec import ImageRef, NodeRef, ReleaseContext, ReleaseSpec
from piceli.services.contracts import EvaluationPreview, SourceRevision


class EvaluationError(ValueError):
    """A bounded public failure; raw process output and paths stay private."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


@dataclass(frozen=True)
class SourceSelection:
    root: Path
    files: tuple[str, ...]
    entrypoint: str
    forbidden_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RendererConfig:
    image_id: str
    platform: str
    docker: ToolPin
    socket: Path
    max_seconds: int = 30
    max_output_bytes: int = 8 * 1024 * 1024
    max_source_bytes: int = 16 * 1024 * 1024
    max_files: int = 256

    def __post_init__(self) -> None:
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.image_id
        ) or self.platform not in {"linux/amd64", "linux/arm64"}:
            raise EvaluationError("evaluation-config")
        if (
            not self.socket.is_absolute()
            or not 1 <= self.max_files <= 1024
            or not 1 <= self.max_source_bytes <= 64 * 1024 * 1024
        ):
            raise EvaluationError("evaluation-config")
        ProcessLimits(self.max_seconds, self.max_output_bytes)


@dataclass(frozen=True)
class RenderInputs:
    namespace: str
    images: Mapping[str, ImageRef]
    values: Mapping[str, JsonValue]
    secret_names: tuple[str, ...]
    nodes: Mapping[str, NodeRef]

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "images": {k: asdict(v) for k, v in self.images.items()},
            "values": dict(self.values),
            "secret_names": list(self.secret_names),
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RenderInputs:
        if set(value) != {"namespace", "images", "values", "secret_names", "nodes"}:
            raise EvaluationError("evaluation-output")
        result = cls(
            value["namespace"],
            {k: ImageRef(**v) for k, v in value["images"].items()},
            value["values"],
            tuple(value["secret_names"]),
            {k: NodeRef(**v) for k, v in value["nodes"].items()},
        )
        if (
            not isinstance(result.namespace, str)
            or not isinstance(result.values, dict)
            or len(set(result.secret_names)) != len(result.secret_names)
            or not all(isinstance(n, str) and n for n in result.secret_names)
        ):
            raise EvaluationError("evaluation-output")
        _json(result.to_dict())
        return result

    def context(self, secrets: Mapping[str, SecretVersionRef]) -> ReleaseContext:
        return ReleaseContext(
            self.namespace, self.images, secrets, self.values, self.nodes
        )


@dataclass(frozen=True)
class RenderedComposition:
    preview_id: str
    digest: str
    source: SourceRevision
    inputs: RenderInputs
    components: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "digest": self.digest,
            "source": self.source.model_dump(),
            "inputs": self.inputs.to_dict(),
            "components": list(self.components),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RenderedComposition:
        if set(value) != {"preview_id", "digest", "source", "inputs", "components"}:
            raise EvaluationError("evaluation-output")
        result = cls(
            value["preview_id"],
            value["digest"],
            SourceRevision.model_validate(value["source"]),
            RenderInputs.from_dict(value["inputs"]),
            tuple(value["components"]),
        )
        from piceli.app.render import placeholder_inputs

        result.composition(
            result.inputs.context(placeholder_inputs(list(result.inputs.secret_names)))
        )
        return result

    def composition(self, context: ReleaseContext) -> DeploymentComposition:
        if (
            context.namespace != self.inputs.namespace
            or dict(context.images) != dict(self.inputs.images)
            or dict(context.nodes) != dict(self.inputs.nodes)
            or dict(context.values) != dict(self.inputs.values)
        ):
            raise EvaluationError("evaluation-approval")
        components = []
        for component in self.components:
            if set(component) != {"name", "dependencies", "resources"}:
                raise EvaluationError("evaluation-output")
            resources = []
            for item in component["resources"]:
                if set(item) != {"manifest", "dependencies", "bindings"}:
                    raise EvaluationError("evaluation-output")
                resource = ResourceIntent.from_manifest(
                    item["manifest"],
                    dependencies=tuple(
                        ResourceRef(**dep) for dep in item["dependencies"]
                    ),
                )
                for binding in item["bindings"]:
                    if (
                        set(binding) != {"pointer", "input_name"}
                        or binding["input_name"] not in self.inputs.secret_names
                    ):
                        raise EvaluationError("evaluation-output")
                    resource = resource.with_secret(
                        binding["pointer"], context.secret(binding["input_name"])
                    )
                public, _ = public_manifest(resource.manifest)
                _validate_public(
                    resource.manifest,
                    public,
                    {binding["pointer"] for binding in item["bindings"]},
                )
                resources.append(resource)
            components.append(
                DeploymentComponent(
                    component["name"],
                    tuple(resources),
                    tuple(component["dependencies"]),
                )
            )
        return DeploymentComposition(tuple(components))


def _validate_public(
    raw: Any, public: Any, bindings: set[str], pointer: str = ""
) -> None:
    if raw == public:
        return
    if pointer in bindings and raw == "<private>":
        return
    if (
        isinstance(raw, dict)
        and isinstance(public, dict)
        and raw.keys() == public.keys()
    ):
        for key in raw:
            _validate_public(
                raw[key],
                public[key],
                bindings,
                pointer + "/" + key.replace("~", "~0").replace("/", "~1"),
            )
        return
    if isinstance(raw, list) and isinstance(public, list) and len(raw) == len(public):
        for index, (left, right) in enumerate(zip(raw, public, strict=True)):
            _validate_public(left, right, bindings, pointer + "/" + str(index))
        return
    raise EvaluationError("evaluation-output")


def derive_render_inputs(
    spec: ReleaseSpec, *, nodes: Mapping[str, NodeRef] | None = None
) -> RenderInputs:
    """Freeze public inputs; never resolve secret values or import source."""
    from piceli.k8s.release_secrets import input_names

    if any(isinstance(check, PythonCheck) for check in spec.model.checks):
        raise EvaluationError("evaluation-unsupported")
    if spec.model.target.nodes and nodes is None:
        raise EvaluationError("evaluation-config")
    return RenderInputs(
        spec.model.target.namespace,
        spec.images(),
        spec.model.values,
        tuple(
            item
            for name, generator in spec.model.secrets.items()
            for item in input_names(name, generator)
        ),
        dict(nodes or {}),
    )


def denied_paths(spec: ReleaseSpec) -> tuple[Path, ...]:
    """Private engine/config inputs cannot enter the source stage."""
    paths = [
        spec.kubeconfig_target().kubeconfig,
        spec.state_dir,
        spec.catalog_path,
        spec.journal_path,
        spec.secret_store_path,
        Path("/var/run/secrets/kubernetes.io/serviceaccount"),
    ]
    if spec.kubeconfig_target().kubeconfig.is_file():
        from piceli.k8s.ops.provider_factory import credential_file_paths

        paths.extend(credential_file_paths(spec.kubeconfig_target()))
    if spec.path is not None:
        paths.append(spec.path)
    for generator in spec.model.secrets.values():
        for name in ("file", "token_file", "ca_file"):
            value = getattr(generator, name, None)
            if isinstance(value, Path):
                paths.append(spec.resolve(value))
    paths.extend(
        Path.home() / name
        for name in (
            ".kube",
            ".ssh",
            ".aws",
            ".docker",
            ".config/gcloud",
            ".config/sops",
        )
    )
    variables = {
        "SOPS_AGE_KEY_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
    }
    for generator in spec.model.secrets.values():
        variables.update(getattr(generator, "pass_env", ()))
    for name in variables:
        value = os.environ.get(name, "")
        for item in value.split(os.pathsep):
            if item and Path(item).is_absolute():
                paths.append(Path(item))
    return tuple(paths)


@dataclass(frozen=True)
class MaterializedReleaseSpec(ReleaseSpec):
    rendered: RenderedComposition | None = field(default=None, repr=False)

    def images(self) -> dict[str, ImageRef]:
        assert self.rendered is not None
        return dict(self.rendered.inputs.images)

    def load_composition(self) -> Callable[[ReleaseContext], DeploymentComposition]:
        assert self.rendered is not None
        return self.rendered.composition


def materialize_spec(
    spec: ReleaseSpec, rendered: RenderedComposition
) -> MaterializedReleaseSpec:
    if any(isinstance(check, PythonCheck) for check in spec.model.checks):
        raise EvaluationError("evaluation-unsupported")
    if spec.model.target.namespace != rendered.inputs.namespace or dict(
        spec.model.values
    ) != dict(rendered.inputs.values):
        raise EvaluationError("evaluation-approval")
    return MaterializedReleaseSpec(spec.model, spec.base, spec.path, rendered)


_DENIED = {
    ".git",
    ".ssh",
    ".aws",
    ".kube",
    ".docker",
    ".venv",
    "node_modules",
    "credentials",
    "kubeconfig",
    "release.toml",
}


def _relative(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or str(path) != name
        or any(p in {"..", "."} for p in path.parts)
        or "\\" in name
        or ":" in name
        or "\x00" in name
    ):
        raise EvaluationError("evaluation-source")
    if any(
        p.lower() in _DENIED
        or p.lower().startswith(".env")
        or p.lower().endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"))
        for p in path.parts
    ):
        raise EvaluationError("evaluation-source")
    return name


def _read_source(root: Path, name: str, maximum: int) -> bytes:
    """Traverse with openat/O_NOFOLLOW, refusing links and special files."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(_relative(name)).parts
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(file_descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size > maximum
            ):
                raise EvaluationError("evaluation-source")
            content = stream.read(maximum + 1)
            if len(content) > maximum:
                raise EvaluationError("evaluation-source")
            return content
    finally:
        os.close(descriptor)


class DockerEvaluator:
    def __init__(self, store: Path, renderer: RendererConfig) -> None:
        if "," in str(store):
            raise EvaluationError("evaluation-config")
        private_directory(store)
        self.store = store.resolve()
        self.renderer = renderer
        self._lock = threading.Lock()
        with self._admission():
            self.recover_interrupted()
            self._prune()

    def _boundary_warning(self) -> str:
        return (
            "Approved Python executes inside the configured Docker isolation boundary."
        )

    def _tool_identity(self) -> str:
        return self.renderer.docker.sha256

    @contextmanager
    def _admission(self) -> Iterator[None]:
        with (self.store / "admission.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def _discard_source(stage: Path) -> None:
        shutil.rmtree(stage / "source", ignore_errors=True)
        (stage / "request.json").unlink(missing_ok=True)

    def _prune(self, *, reserve: bool = False) -> None:
        stages = []
        for stage in self.store.iterdir():
            if (
                not re.fullmatch(r"[0-9a-f]{32}", stage.name)
                or stage.is_symlink()
                or not stage.is_dir()
            ):
                continue
            stages.append(stage)
            with (stage / "lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                record: dict[str, Any] = {}
                try:
                    loaded = json.loads((stage / "record.json").read_bytes())
                    if not isinstance(loaded, dict):
                        raise ValueError("invalid evaluation record")
                    record = loaded
                    expired = datetime.fromisoformat(
                        record["preview"]["expires_at"]
                    ) <= datetime.now(UTC)
                except (OSError, ValueError, KeyError, TypeError):
                    expired = True
                if expired:
                    self._discard_source(stage)
                    # No audit exists for incomplete stages; complete metadata is small.
                    if (
                        not (stage / "record.json").exists()
                        or record.get("state") != "running"
                    ):
                        shutil.rmtree(stage)
        stages = [stage for stage in stages if stage.exists()]
        if len(stages) >= 128:
            for stage in sorted(stages, key=lambda item: item.stat().st_mtime):
                try:
                    loaded = json.loads((stage / "record.json").read_bytes())
                    record = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    record = {}
                if record.get("state") in {"succeeded", "failed", "interrupted"}:
                    shutil.rmtree(stage)
                    stages.remove(stage)
                    if len(stages) < 128:
                        break
        pending = sum((stage / "request.json").exists() for stage in stages)
        if reserve and (pending >= 8 or len(stages) >= 128):
            raise EvaluationError("evaluation-source")

    def recover_interrupted(self) -> None:
        """Fence crash leftovers without replaying approved consumer code."""
        for stage in self.store.iterdir():
            if (
                not re.fullmatch(r"[0-9a-f]{32}", stage.name)
                or stage.is_symlink()
                or not stage.is_dir()
            ):
                continue
            with (stage / "lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    record = json.loads((stage / "record.json").read_bytes())
                except (OSError, ValueError):
                    continue  # Incomplete staging is never an executable preview.
                if record.get("state") != "running":
                    continue
                name = "piceli-evaluation-" + stage.name
                try:
                    identifiers = (
                        self._docker(
                            ["ps", "--all", "--quiet", "--filter", f"name=^/{name}$"]
                        )
                        .decode()
                        .split()
                    )
                    if not identifiers:
                        record["state"] = "interrupted"
                        self._save(stage, record)
                        self._discard_source(stage)
                        continue
                    rows = json.loads(self._docker(["inspect", name]))
                    if (
                        len(rows) != 1
                        or rows[0]["Config"]["Labels"].get("piceli.evaluation")
                        != stage.name
                    ):
                        raise EvaluationError("evaluation-interrupted")
                    self._docker(["rm", "--force", "--volumes", name])
                except Exception:
                    # Keep the running marker so cleanup is retried on restart.
                    # The preview remains unusable in all cases.
                    continue
                record["state"] = "interrupted"
                self._save(stage, record)
                self._discard_source(stage)

    def preview(
        self,
        application_id: str,
        source: SourceSelection,
        inputs: RenderInputs,
        *,
        intent: Literal["deploy", "rollback"] = "deploy",
        release: str | None = None,
    ) -> EvaluationPreview:
        with self._lock, self._admission():
            self._prune(reserve=True)
            return self._preview(
                application_id, source, inputs, intent=intent, release=release
            )

    def _preview(
        self,
        application_id: str,
        source: SourceSelection,
        inputs: RenderInputs,
        *,
        intent: Literal["deploy", "rollback"] = "deploy",
        release: str | None = None,
    ) -> EvaluationPreview:
        stage: Path | None = None
        try:
            if (
                not source.root.is_absolute()
                or not 1 <= len(source.files) <= self.renderer.max_files
                or len(set(source.files)) != len(source.files)
            ):
                raise EvaluationError("evaluation-source")
            module, separator, function = source.entrypoint.partition(":")
            if not separator or not function.isidentifier():
                raise EvaluationError("evaluation-source")
            if module.endswith(".py"):
                selected_module = _relative(module)
            else:
                if not all(part.isidentifier() for part in module.split(".")):
                    raise EvaluationError("evaluation-source")
                choices = (
                    module.replace(".", "/") + ".py",
                    module.replace(".", "/") + "/__init__.py",
                )
                matches = [name for name in choices if name in source.files]
                if len(matches) != 1:
                    raise EvaluationError("evaluation-source")
                selected_module = matches[0]
            if selected_module not in source.files:
                raise EvaluationError("evaluation-source")
            if source.root.is_symlink() or source.root.resolve() != source.root:
                raise EvaluationError("evaluation-source")
            request = {
                "entrypoint": source.entrypoint,
                "inputs": RenderInputs.from_dict(inputs.to_dict()).to_dict(),
            }
            if len(_json(request)) > self.renderer.max_source_bytes:
                raise EvaluationError("evaluation-source")
            identifier = uuid.uuid4().hex
            stage = self.store / identifier
            stage.mkdir(mode=0o700)
            source_dir = stage / "source"
            source_dir.mkdir(mode=0o755)
            source_dir.chmod(0o755)
            hashes = {}
            remaining = self.renderer.max_source_bytes
            for name in sorted(source.files):
                _relative(name)
                candidate = (source.root / name).resolve()
                if any(
                    candidate == path.resolve()
                    or candidate.is_relative_to(path.resolve())
                    for path in (*source.forbidden_paths, self.store)
                ):
                    raise EvaluationError("evaluation-source")
                content = _read_source(source.root, name, remaining)
                remaining -= len(content)
                destination = source_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                for parent in destination.parents:
                    if parent == stage:
                        break
                    parent.chmod(0o755)
                destination.write_bytes(content)
                destination.chmod(0o444)
                hashes[name] = hashlib.sha256(content).hexdigest()
            (stage / "request.json").write_bytes(_json(request))
            (stage / "request.json").chmod(0o444)
            source_revision = SourceRevision(
                kind="local", revision=_digest(hashes), entrypoint=source.entrypoint
            )
            material: dict[str, Any] = {
                "id": identifier,
                "application_id": application_id,
                "source": source_revision.model_dump(),
                "renderer_digest": self.renderer.image_id,
                "input_digest": _digest(request),
                "files": sorted(hashes),
                "intent": intent,
                "release": release,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                "limits": {
                    "seconds": self.renderer.max_seconds,
                    "output_bytes": self.renderer.max_output_bytes,
                    "source_bytes": self.renderer.max_source_bytes,
                    "files": self.renderer.max_files,
                },
                "warnings": [self._boundary_warning()],
                "platform": self.renderer.platform,
                "tool": self._tool_identity(),
            }
            preview = EvaluationPreview(
                **{k: v for k, v in material.items() if k not in {"platform", "tool"}},
                digest=_digest(material),
            )
            self._save(
                stage,
                {
                    "preview": preview.model_dump(),
                    "material": material,
                    "hashes": hashes,
                    "state": "ready",
                },
            )
            return preview
        except Exception:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            raise EvaluationError("evaluation-source") from None

    @staticmethod
    def _save(stage: Path, value: dict[str, Any]) -> None:
        temporary = stage / "record.tmp"
        with temporary.open("wb") as stream:
            stream.write(_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(stage / "record.json")
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _docker(
        self,
        arguments: list[str],
        *,
        cancel: threading.Event | None = None,
        max_seconds: float | None = None,
    ) -> bytes:
        self.renderer.docker.verify()
        with tempfile.TemporaryDirectory(prefix="piceli-render-docker-") as temporary:
            receipt, output, _ = _run_process(
                [
                    str(self.renderer.docker.path),
                    "--host",
                    f"unix://{self.renderer.socket}",
                    "--config",
                    temporary,
                    *arguments,
                ],
                self.store,
                ProcessLimits(
                    max_seconds or self.renderer.max_seconds,
                    self.renderer.max_output_bytes,
                ),
                cancel=cancel,
                environment={"HOME": temporary, "DOCKER_CONFIG": temporary},
            )
        self.renderer.docker.verify()
        if receipt["state"] != "succeeded":
            raise EvaluationError("evaluation-renderer")
        return output

    def available(self) -> bool:
        """Probe the configured isolation boundary without executing source."""
        try:
            if not stat.S_ISSOCK(self.renderer.socket.stat().st_mode):
                return False
            inspection = json.loads(
                self._docker(
                    ["image", "inspect", self.renderer.image_id], max_seconds=3
                )
            )
            return (
                len(inspection) == 1
                and inspection[0]["Id"] == self.renderer.image_id
                and inspection[0]["Os"] + "/" + inspection[0]["Architecture"]
                == self.renderer.platform
            )
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return False

    def render(
        self,
        preview_id: str,
        approved_digest: str,
        *,
        cancel: threading.Event | None = None,
    ) -> RenderedComposition:
        if not re.fullmatch(r"[0-9a-f]{32}", preview_id):
            raise EvaluationError("evaluation-approval")
        with self._lock:
            stage = self.store / preview_id
            if stage.is_symlink() or not stage.is_dir():
                raise EvaluationError("evaluation-approval")
            with (stage / "lock").open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise EvaluationError("evaluation-interrupted") from None
                return self._render(preview_id, approved_digest, cancel)

    def _render(
        self, preview_id: str, approved_digest: str, cancel: threading.Event | None
    ) -> RenderedComposition:
        stage = self.store / preview_id
        name = "piceli-evaluation-" + preview_id
        try:
            record = json.loads((stage / "record.json").read_bytes())
            preview = EvaluationPreview.model_validate(record["preview"])
            if (
                preview.digest != approved_digest
                or _digest(record["material"]) != approved_digest
            ):
                raise EvaluationError("evaluation-approval")
            expected_preview = {
                k: v
                for k, v in record["material"].items()
                if k not in {"platform", "tool"}
            }
            if preview.model_dump(exclude={"digest"}) != expected_preview:
                raise EvaluationError("evaluation-approval")
            if {
                "seconds": self.renderer.max_seconds,
                "output_bytes": self.renderer.max_output_bytes,
                "source_bytes": self.renderer.max_source_bytes,
                "files": self.renderer.max_files,
            } != preview.limits:
                raise EvaluationError("evaluation-approval")
            if datetime.fromisoformat(preview.expires_at) <= datetime.now(UTC):
                raise EvaluationError("evaluation-expired")
            if record["state"] != "ready":
                raise EvaluationError("evaluation-interrupted")
            if (
                preview.renderer_digest != self.renderer.image_id
                or record["material"]["tool"] != self.renderer.docker.sha256
                or record["material"]["platform"] != self.renderer.platform
            ):
                raise EvaluationError("evaluation-approval")
            actual_files = {
                str(path.relative_to(stage / "source"))
                for path in (stage / "source").rglob("*")
                if not path.is_dir()
            }
            if actual_files != set(preview.files) or any(
                path.is_symlink() for path in stage.rglob("*")
            ):
                raise EvaluationError("evaluation-approval")
            hashes = {
                name: hashlib.sha256(
                    _read_source(stage / "source", name, self.renderer.max_source_bytes)
                ).hexdigest()
                for name in preview.files
            }
            request = json.loads((stage / "request.json").read_bytes())
            if (
                hashes != record["hashes"]
                or _digest(hashes) != preview.source.revision
                or _digest(request) != preview.input_digest
            ):
                raise EvaluationError("evaluation-approval")
            if cancel is not None and cancel.is_set():
                raise EvaluationError("evaluation-interrupted")
            if not stat.S_ISSOCK(self.renderer.socket.stat().st_mode):
                raise EvaluationError("evaluation-config")
            inspection = json.loads(
                self._docker(["image", "inspect", self.renderer.image_id])
            )
            if (
                len(inspection) != 1
                or inspection[0]["Id"] != self.renderer.image_id
                or inspection[0]["Os"] + "/" + inspection[0]["Architecture"]
                != self.renderer.platform
            ):
                raise EvaluationError("evaluation-renderer")
            record["state"] = "running"
            self._save(stage, record)
            try:
                self._docker(
                    [
                        "create",
                        "--name",
                        name,
                        "--label",
                        f"piceli.evaluation={preview_id}",
                        "--pull=never",
                        "--platform",
                        self.renderer.platform,
                        "--network=none",
                        "--read-only",
                        "--user=65532:65532",
                        "--cap-drop=ALL",
                        "--security-opt=no-new-privileges",
                        "--pids-limit=64",
                        "--memory=256m",
                        "--memory-swap=256m",
                        "--cpus=1",
                        "--log-driver=none",
                        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                        "--mount",
                        f"type=bind,src={stage / 'source'},dst=/source,readonly",
                        "--mount",
                        f"type=bind,src={stage / 'request.json'},dst=/request.json,readonly",
                        "--env=HOME=/tmp",
                        "--env=PYTHONDONTWRITEBYTECODE=1",
                        "--entrypoint=python",
                        self.renderer.image_id,
                        "-I",
                        "-m",
                        "piceli.services.render_worker",
                        "/request.json",
                    ]
                )
                output = self._docker(["start", "--attach", name], cancel=cancel)
                state = json.loads(
                    self._docker(["inspect", "--format={{json .State}}", name])
                )
                if state["ExitCode"] != 0 or state["OOMKilled"] or state["Running"]:
                    raise EvaluationError("evaluation-renderer")
                result = json.loads(output)
                checks = result.pop("pre_rollout_checks", 0)
                if (
                    set(result) != {"components"}
                    or type(checks) is not int
                    or checks < 0
                ):
                    raise EvaluationError("evaluation-output")
                if checks:
                    # Pre-rollout checks run only in `piceli deploy`; never
                    # plan a release that would skip them.
                    raise EvaluationError("ui-prerollout-unsupported")
                rendered = RenderedComposition.from_dict(
                    {
                        "preview_id": preview_id,
                        "digest": preview.digest,
                        "source": preview.source.model_dump(),
                        "inputs": request["inputs"],
                        "components": result["components"],
                    }
                )
                record["state"] = "succeeded"
                return rendered
            finally:
                self._docker(["rm", "--force", "--volumes", name])
                if record["state"] == "running":
                    record["state"] = "failed"
                self._save(stage, record)
                self._discard_source(stage)
        except EvaluationError:
            raise
        except Exception:
            raise EvaluationError("evaluation-output") from None
