"""The approval boundary fails closed before importing or invoking anything."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from piceli.app.render import placeholder_inputs
from piceli.artifacts.process import ToolPin
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.release_spec import ReleaseSpec
from piceli.services.evaluation import (
    DockerEvaluator,
    EvaluationError,
    RenderedComposition,
    RendererConfig,
    SourceSelection,
    denied_paths,
    derive_render_inputs,
    materialize_spec,
)
from piceli.services.render_worker import serialize


@pytest.fixture
def spec(tmp_path: Path) -> ReleaseSpec:
    return ReleaseSpec.from_dict(
        {
            "target": {
                "kubeconfig": "private-config",
                "context": "ctx",
                "namespace": "demo",
            },
            "release": {
                "name": "web",
                "owner": "owner",
                "field_manager": "manager",
                "composition": "compose.py:build",
                "state_dir": "state",
            },
            "secrets": {"password": {"type": "random"}},
            "images": {"web": "docker.io/library/nginx@sha256:" + "a" * 64},
        },
        tmp_path,
    )


@pytest.fixture
def evaluator(tmp_path: Path) -> DockerEvaluator:
    return DockerEvaluator(
        tmp_path / "store",
        RendererConfig(
            "sha256:" + "a" * 64,
            "linux/arm64",
            ToolPin(Path("/not/invoked/docker"), "sha256:" + "b" * 64),
            tmp_path / "socket",
        ),
    )


def source(tmp_path: Path) -> SourceSelection:
    root = tmp_path / "source"
    root.mkdir()
    (root / "compose.py").write_text("raise RuntimeError('must not execute on host')")
    return SourceSelection(root, ("compose.py",), "compose.py:build")


def test_preview_is_durable_public_and_does_not_import(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    selection = source(tmp_path)
    preview = evaluator.preview("web", selection, derive_render_inputs(spec))
    assert preview.files == ["compose.py"]
    assert "private-config" not in preview.model_dump_json()
    request = json.loads((evaluator.store / preview.id / "request.json").read_bytes())
    assert request["inputs"]["secret_names"] == ["password"]
    assert "kubeconfig" not in json.dumps(request)
    assert (
        (evaluator.store / preview.id / "source/compose.py")
        .read_text()
        .startswith("raise")
    )
    assert (evaluator.store.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/absolute.py",
        ".env",
        ".aws/config",
        "private.key",
        "release.toml",
        "folder/../compose.py",
    ],
)
def test_private_or_escaping_names_refused(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path, name: str
) -> None:
    selection = replace(source(tmp_path), files=("compose.py", name))
    with pytest.raises(EvaluationError, match="evaluation-source"):
        evaluator.preview("web", selection, derive_render_inputs(spec))
    assert not [path for path in evaluator.store.iterdir() if path.is_dir()]


@pytest.mark.parametrize("hardlink", [False, True])
def test_linked_source_refused(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path, hardlink: bool
) -> None:
    selection = source(tmp_path)
    private = tmp_path / "private"
    private.write_text("PRIVATE")
    path = selection.root / "compose.py"
    path.unlink()
    os.link(private, path) if hardlink else path.symlink_to(private)
    with pytest.raises(EvaluationError):
        evaluator.preview("web", selection, derive_render_inputs(spec))
    assert not [path for path in evaluator.store.iterdir() if path.is_dir()]


def test_explicit_private_paths_and_bounds(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    selection = source(tmp_path)
    selection = replace(selection, forbidden_paths=(selection.root / "compose.py",))
    with pytest.raises(EvaluationError):
        evaluator.preview("web", selection, derive_render_inputs(spec))
    assert spec.kubeconfig_target().kubeconfig in denied_paths(spec)
    assert spec.secret_store_path in denied_paths(spec)
    evaluator.renderer = replace(evaluator.renderer, max_source_bytes=8)
    with pytest.raises(EvaluationError):
        evaluator.preview(
            "web", replace(selection, forbidden_paths=()), derive_render_inputs(spec)
        )


@pytest.mark.parametrize(
    "tamper", ["digest", "stage", "extra", "metadata", "limits", "tool", "request"]
)
def test_approval_and_stage_tampering_refused_before_docker(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = evaluator.preview("web", source(tmp_path), derive_render_inputs(spec))
    stage = evaluator.store / preview.id
    if tamper == "stage":
        path = stage / "source/compose.py"
        path.chmod(0o600)
        path.write_text("changed")
    elif tamper == "extra":
        (stage / "source/extra.py").write_text("changed")
    elif tamper == "metadata":
        record = json.loads((stage / "record.json").read_bytes())
        record["preview"]["application_id"] = "other"
        (stage / "record.json").write_text(json.dumps(record))
    elif tamper == "limits":
        evaluator.renderer = replace(evaluator.renderer, max_seconds=60)
    elif tamper == "tool":
        evaluator.renderer = replace(
            evaluator.renderer, docker=ToolPin(Path("/other"), "sha256:" + "c" * 64)
        )
    elif tamper == "request":
        path = stage / "request.json"
        path.chmod(0o600)
        path.write_text("{}")
    monkeypatch.setattr(
        evaluator, "_docker", lambda *a, **kw: pytest.fail("must refuse before docker")
    )
    with pytest.raises(EvaluationError, match="evaluation-approval"):
        evaluator.render(preview.id, "0" * 64 if tamper == "digest" else preview.digest)


def test_roundtrip_preserves_dependencies_and_symbolic_secret_versions(
    spec: ReleaseSpec,
) -> None:
    inputs = derive_render_inputs(spec)
    first = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "settings", "namespace": "demo"},
            "data": {"setting": "public"},
        }
    )
    secret = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credential", "namespace": "demo"},
            "data": {"password": ""},
        },
        dependencies=(first.ref,),
    ).with_secret("/data/password", placeholder_inputs(["password"])["password"])
    composition = DeploymentComposition((DeploymentComponent("app", (first, secret)),))
    from piceli.services.contracts import SourceRevision

    rendered = RenderedComposition(
        "a" * 32,
        "b" * 64,
        SourceRevision(kind="local", revision="c" * 64, entrypoint="compose.py:build"),
        inputs,
        tuple(serialize(composition, inputs)["components"]),
    )
    restored = RenderedComposition.from_dict(json.loads(json.dumps(rendered.to_dict())))
    materialized = materialize_spec(spec, restored)
    context = materialized.context(
        materialized.images(), placeholder_inputs(["password"])
    )
    assert materialized.load_composition()(context) == composition
    assert "store_id" not in json.dumps(restored.to_dict())
    altered = rendered.to_dict()
    altered["components"][0]["resources"][1]["bindings"][0]["input_name"] = "invented"
    with pytest.raises(EvaluationError):
        RenderedComposition.from_dict(altered)


def test_raw_secret_output_refused(spec: ReleaseSpec) -> None:
    from piceli.services.contracts import SourceRevision

    inputs = derive_render_inputs(spec)
    resource = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "credential", "namespace": "demo"},
            "data": {"password": "PRIVATE"},
        }
    )
    composition = DeploymentComposition((DeploymentComponent("app", (resource,)),))
    rendered = RenderedComposition(
        "a" * 32,
        "b" * 64,
        SourceRevision(kind="local", revision="c" * 64, entrypoint="compose.py:build"),
        inputs,
        tuple(serialize(composition, inputs)["components"]),
    )
    with pytest.raises(EvaluationError, match="evaluation-output"):
        RenderedComposition.from_dict(rendered.to_dict())


def test_docker_arguments_cleanup_and_original_source_freeze(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = source(tmp_path)
    preview = evaluator.preview("web", selection, derive_render_inputs(spec))
    (selection.root / "compose.py").write_text("new unapproved source")
    calls: list[list[str]] = []

    def docker(arguments: list[str], **kwargs: Any) -> bytes:
        calls.append(arguments)
        if arguments[:2] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": evaluator.renderer.image_id,
                        "Os": "linux",
                        "Architecture": "arm64",
                    }
                ]
            ).encode()
        if arguments[0] == "start":
            return b'{"components": []}'
        if arguments[0] == "inspect":
            return b'{"ExitCode": 0,"OOMKilled":false,"Running":false}'
        return b""

    monkeypatch.setattr(evaluator, "_docker", docker)
    evaluator.renderer.socket.touch()
    monkeypatch.setattr("piceli.services.evaluation.stat.S_ISSOCK", lambda mode: True)
    assert evaluator.render(preview.id, preview.digest).components == ()
    create = next(call for call in calls if call[0] == "create")
    for flag in (
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65532:65532",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--pull=never",
    ):
        assert flag in create
    assert calls[-1] == [
        "rm",
        "--force",
        "--volumes",
        "piceli-evaluation-" + preview.id,
    ]
    assert str(spec.kubeconfig_target().kubeconfig) not in " ".join(create)
    with pytest.raises(EvaluationError, match="evaluation-interrupted"):
        evaluator.render(preview.id, preview.digest)


@pytest.mark.parametrize(
    "entrypoint,file",
    [
        ("compose:build", "compose.py"),
        ("package:build", "package/__init__.py"),
        ("package.child:build", "package/child.py"),
    ],
)
def test_existing_module_entrypoints_require_selected_source(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    entrypoint: str,
    file: str,
) -> None:
    root = tmp_path / "code"
    (root / file).parent.mkdir(parents=True)
    (root / file).write_text("raise AssertionError('host import')")
    preview = evaluator.preview(
        "app", SourceSelection(root, (file,), entrypoint), derive_render_inputs(spec)
    )
    assert preview.source.entrypoint == entrypoint


def test_python_checks_refused_without_resolving(spec: ReleaseSpec) -> None:
    from piceli.checks.model import PythonCheck

    checked = replace(
        spec,
        model=spec.model.model_copy(
            update={"checks": (PythonCheck(name="probe", call="do_not_import:check"),)}
        ),
    )
    with pytest.raises(EvaluationError, match="evaluation-unsupported"):
        derive_render_inputs(checked)


def test_restart_cleans_only_owned_label_and_never_replays(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = evaluator.preview("app", source(tmp_path), derive_render_inputs(spec))
    stage = evaluator.store / preview.id
    record = json.loads((stage / "record.json").read_bytes())
    record["state"] = "running"
    evaluator._save(stage, record)
    calls = []

    def docker(self: DockerEvaluator, args: list[str], **kwargs: Any) -> bytes:
        calls.append(args)
        if args[0] == "ps":
            return b"container-id\n"
        if args[0] == "inspect":
            return json.dumps(
                [{"Config": {"Labels": {"piceli.evaluation": preview.id}}}]
            ).encode()
        return b""

    monkeypatch.setattr(DockerEvaluator, "_docker", docker)
    restarted = DockerEvaluator(evaluator.store, evaluator.renderer)
    assert calls[1:] == [
        ["inspect", "piceli-evaluation-" + preview.id],
        ["rm", "--force", "--volumes", "piceli-evaluation-" + preview.id],
    ]
    assert not (stage / "source").exists()
    with pytest.raises(EvaluationError, match="evaluation-interrupted"):
        restarted.render(preview.id, preview.digest)


def test_restart_refuses_foreign_container_cleanup(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = evaluator.preview("app", source(tmp_path), derive_render_inputs(spec))
    stage = evaluator.store / preview.id
    record = json.loads((stage / "record.json").read_bytes())
    record["state"] = "running"
    evaluator._save(stage, record)
    calls = []

    def docker(self: DockerEvaluator, args: list[str], **kwargs: Any) -> bytes:
        calls.append(args)
        if args[0] == "ps":
            return b"container-id\n"
        return b'[{"Config":{"Labels":{"piceli.evaluation":"foreign"}}}]'

    monkeypatch.setattr(DockerEvaluator, "_docker", docker)
    restarted = DockerEvaluator(evaluator.store, evaluator.renderer)
    assert len(calls) == 2
    with pytest.raises(EvaluationError, match="evaluation-interrupted"):
        restarted.render(preview.id, preview.digest)


def test_pending_sources_have_installation_bound(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    selected = source(tmp_path)
    for _ in range(8):
        evaluator.preview("app", selected, derive_render_inputs(spec))
    with pytest.raises(EvaluationError, match="evaluation-source"):
        evaluator.preview("app", selected, derive_render_inputs(spec))
    assert len([path for path in evaluator.store.iterdir() if path.is_dir()]) == 8


def test_restart_missing_container_becomes_interrupted(
    evaluator: DockerEvaluator,
    spec: ReleaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preview = evaluator.preview("app", source(tmp_path), derive_render_inputs(spec))
    stage = evaluator.store / preview.id
    record = json.loads((stage / "record.json").read_bytes())
    record["state"] = "running"
    evaluator._save(stage, record)
    calls = []

    def docker(self: DockerEvaluator, args: list[str], **kwargs: Any) -> bytes:
        calls.append(args)
        assert args[0] == "ps"
        return b""

    monkeypatch.setattr(DockerEvaluator, "_docker", docker)
    restarted = DockerEvaluator(evaluator.store, evaluator.renderer)
    assert len(calls) == 1
    assert json.loads((stage / "record.json").read_bytes())["state"] == "interrupted"
    assert not (stage / "source").exists()
    with pytest.raises(EvaluationError, match="evaluation-interrupted"):
        restarted.render(preview.id, preview.digest)


def test_concurrent_preview_admission_is_bounded_across_instances(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    selected = source(tmp_path)
    second = DockerEvaluator(evaluator.store, evaluator.renderer)
    for _ in range(7):
        evaluator.preview("app", selected, derive_render_inputs(spec))

    def attempt(index: int) -> bool:
        try:
            (second if index % 2 else evaluator).preview(
                "app", selected, derive_render_inputs(spec)
            )
            return True
        except EvaluationError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    # Restart remains possible with a full queue; only new admission is refused.
    DockerEvaluator(evaluator.store, evaluator.renderer)


def test_source_directory_modes_ignore_private_umask(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    root = tmp_path / "source"
    (root / "package").mkdir(parents=True)
    (root / "package/__init__.py").write_text("def build(ctx): pass")
    previous = os.umask(0o077)
    try:
        preview = evaluator.preview(
            "app",
            SourceSelection(root, ("package/__init__.py",), "package:build"),
            derive_render_inputs(spec),
        )
    finally:
        os.umask(previous)
    stage = evaluator.store / preview.id
    assert stage.stat().st_mode & 0o777 == 0o700
    assert (stage / "source").stat().st_mode & 0o777 == 0o755
    assert (stage / "source/package").stat().st_mode & 0o777 == 0o755


def test_expired_sources_are_removed_on_restart(
    evaluator: DockerEvaluator, spec: ReleaseSpec, tmp_path: Path
) -> None:
    preview = evaluator.preview("app", source(tmp_path), derive_render_inputs(spec))
    stage = evaluator.store / preview.id
    record = json.loads((stage / "record.json").read_bytes())
    record["preview"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    evaluator._save(stage, record)
    restarted = DockerEvaluator(evaluator.store, evaluator.renderer)
    assert not stage.exists()
    with pytest.raises(EvaluationError, match="evaluation-approval"):
        restarted.render(preview.id, preview.digest)
