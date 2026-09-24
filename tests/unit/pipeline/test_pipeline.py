"""Unit tests of the pipeline declarations, composition and CLI surface (no cluster)."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import App
from piceli.k8s.cli import app as cli
from piceli.k8s.release import ReleaseRecord, ReleaseSource
from piceli.k8s.release_spec import ImageRef, NodeRef, ReleaseContext
from piceli.pipeline import (
    Build,
    ImageHandle,
    NodeImport,
    NodeLoopbackRegistry,
    Pipeline,
    PipelineError,
    Random,
    Registry,
    Secrets,
    Target,
    TargetNode,
    Template,
)
from piceli.pipeline.checks import default_runner, describe_check
from piceli.pipeline.compose import (
    composition_function,
    model_fingerprint,
    pinned_images,
    release_spec,
    resolve,
)
from piceli.pipeline.journal import Journal
from piceli.pipeline.model import handle_image
from piceli.pipeline.secrets import placeholder

ROOT = Path(__file__).resolve().parents[3]
DIGEST = "sha256:" + "a" * 64
CACHE = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64


def _target(**kwargs: Any) -> Target:
    return Target.kubeconfig(
        "cluster.kubeconfig",
        context="kind-shop",
        namespace="shop",
        nodes={"primary": ("shop-node", "uid-1")},
        **kwargs,
    )


def _pipeline(deliver: Any = None, **kwargs: Any) -> Pipeline:
    app = App("shop")
    secrets = Secrets(
        password=Random(24),
        url=Template("redis://:{password}@cache:6379/0"),
    )
    build = Build.spec("build.toml")
    credentials = app.secret(
        "credentials", {"url": secrets.ref("url"), "password": secrets.ref("password")}
    )
    app.deployment("cache", image=CACHE, env={"P": credentials.key("password")})
    app.deployment("web", image=build["web"], env={"URL": credentials.key("url")})
    return Pipeline(
        app,
        _target(),
        build=build,
        deliver=deliver or NodeLoopbackRegistry(),
        secrets=secrets,
        **kwargs,
    )


# ------------------------------------------------------------------ target


def test_target_kubeconfig_resolves_from_the_declaring_file() -> None:
    target = _target()
    assert target.kubeconfig == Path(__file__).parent / "cluster.kubeconfig"
    assert target.context == "kind-shop" and target.namespace == "shop"
    assert target.nodes == {"primary": TargetNode("shop-node", "uid-1")}
    assert target.nodes["primary"] == ("shop-node", "uid-1")
    assert target.node(None) == ("primary", TargetNode("shop-node", "uid-1"))
    with pytest.raises(AttributeError):
        target.context = "other"  # type: ignore[misc]
    assert "kubeconfig" not in json.dumps(target.identity())


def test_target_refuses_invalid_values() -> None:
    with pytest.raises(PipelineError) as raised:
        Target.kubeconfig("k", context="c", namespace="Not_Valid")
    assert raised.value.code == "pipeline-invalid"
    with pytest.raises(PipelineError):
        Target.kubeconfig("k", context="c", namespace="ns", nodes={"Bad": "n"})
    target = Target.kubeconfig("k", context="c", namespace="ns", nodes={"a": "n"})
    assert target.nodes["a"] == TargetNode("n", None)
    with pytest.raises(PipelineError, match="not declared"):
        target.node("other")
    two = Target.kubeconfig(
        "k", context="c", namespace="ns", nodes={"a": "n", "b": "m"}
    )
    with pytest.raises(PipelineError, match="exactly one node"):
        two.node(None)
    absolute = Target.kubeconfig("/tmp/k", context="c", namespace="ns")
    assert absolute.kubeconfig == Path("/tmp/k")


# ------------------------------------------------------------------ build


def test_image_handles_render_as_placeholders() -> None:
    handle = Build.spec("build.toml")["api"]
    assert isinstance(handle, ImageHandle) and handle.image == "api"
    assert handle_image(str(handle)) == "api"
    assert handle_image("docker.io/library/redis:7") is None
    with pytest.raises(PipelineError):
        ImageHandle("Bad Name")


def test_build_dockerfile_plans_one_image_per_target(tmp_path: Path) -> None:
    module = tmp_path / "decl.py"
    (tmp_path / "Containerfile").write_text(
        textwrap.dedent(
            """
            ARG BUILDER
            FROM ${BUILDER} AS api
            COPY main.txt /main.txt
            FROM ${BUILDER} AS worker
            COPY main.txt /worker.txt
            """
        )
    )
    (tmp_path / "main.txt").write_text("hello\n")
    module.write_text(
        textwrap.dedent(
            f"""
            from piceli.pipeline import Build
            images = Build.dockerfile(
                "Containerfile", builder="docker.io/library/busybox@{DIGEST}",
                targets=["api", "worker"], include=["Containerfile", "main.txt"],
                platform="linux/arm64",
            )
            """
        )
    )
    spec = importlib.util.spec_from_file_location("decl_pipeline", module)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    images: Build = loaded.images
    assert images["api"] == ImageHandle("api")
    with pytest.raises(PipelineError) as raised:
        images["missing"]
    assert raised.value.code == "pipeline-image-unknown"
    build_spec = images.load()
    assert images.image_names() == ("api", "worker")
    assert build_spec.images[0].repository == "piceli-build/api/api"
    plan = build_spec.plan()
    assert plan.plan_hash.startswith("sha256:")
    assert [item["kind"] for item in plan.invocations] == ["image:api", "image:worker"]
    with pytest.raises(PipelineError):
        Build.dockerfile("C", builder="busybox:latest", targets=["api"])


def test_multi_platform_builds_are_refused(tmp_path: Path) -> None:
    spec = ROOT / "examples" / "builds" / "rust-hello" / "build.toml"
    text = spec.read_text().replace(
        'platforms = ["linux/arm64"]', 'platforms = ["linux/arm64", "linux/amd64"]'
    )
    (tmp_path / "build.toml").write_text(text)
    with pytest.raises(PipelineError, match="exactly one"):
        Build(path=tmp_path / "build.toml").load()


# ---------------------------------------------------------------- pipeline


def test_pipeline_validation() -> None:
    app = App("shop")
    with pytest.raises(PipelineError, match="deliver="):
        Pipeline(app, _target(), build=Build.spec("build.toml"))
    with pytest.raises(PipelineError):
        Pipeline("app", _target())  # type: ignore[arg-type]
    pipeline = _pipeline(checks=object())
    assert len(pipeline.checks) == 1
    assert pipeline.owner == "shop" and pipeline.field_manager == "shop"
    assert pipeline.state_dir == Path(__file__).parent / ".piceli-deploy"
    assert list(pipeline.handles()) == ["web"]


def test_secrets_refs_are_the_release_placeholders() -> None:
    secrets = Secrets(password=Random(24))
    assert secrets.ref("password") == placeholder("password")
    with pytest.raises(PipelineError):
        secrets.ref("other")
    with pytest.raises(PipelineError):
        Secrets(url=Template("{missing}"))


def test_resolve_replaces_handles_pins_nodes_and_rebinds_secrets() -> None:
    pipeline = _pipeline()
    web = ImageRef(
        "web",
        DIGEST,
        "127.0.0.1:5000/shop/web",
        None,
        DIGEST,
        image_id="sha256:" + "c" * 64,
    )
    real = {name: placeholder(name + "-real") for name in ("password", "url")}
    context = ReleaseContext(
        namespace="shop",
        images={"web": web},
        secrets=real,
        nodes={"primary": NodeRef("shop-node", "uid-1")},
    )
    composition = composition_function(pipeline)(context)
    manifests = {
        (r.ref.kind, r.ref.name): r
        for component in composition.components
        for r in component.resources
    }
    web_pod = manifests[("Deployment", "web")].manifest["spec"]["template"]["spec"]
    assert web_pod["containers"][0]["image"] == f"127.0.0.1:5000/shop/web@{DIGEST}"
    assert web_pod["nodeSelector"] == {"kubernetes.io/hostname": "shop-node"}
    cache_pod = manifests[("Deployment", "cache")].manifest["spec"]["template"]["spec"]
    assert "nodeSelector" not in cache_pod  # no built image: no pin
    bindings = {
        b.json_pointer: b.reference
        for b in manifests[("Secret", "credentials")].secret_bindings
    }
    assert bindings == {"/data/password": real["password"], "/data/url": real["url"]}


def test_resolve_keeps_an_explicit_node_choice_and_refuses_unknown_images() -> None:
    app = App("shop")
    build = Build.spec("build.toml")
    app.deployment("web", image=build["web"], node="primary")
    app.deployment("api", image=build["api"])
    context = ReleaseContext(
        namespace="shop",
        images={},
        secrets={},
        nodes={"primary": NodeRef("shop-node", "")},
    )
    with pytest.raises(PipelineError) as raised:
        resolve(app.composition(context), images={}, secrets={}, pin_node="x")
    assert raised.value.code == "pipeline-image-unknown"
    ref = ImageRef("web", DIGEST, "r/web", None, DIGEST)
    resolved = resolve(
        app.composition(context),
        images={"web": ref, "api": ref},
        secrets={},
        pin_node="other-node",
    )
    pods = {
        r.ref.name: r.manifest["spec"]["template"]["spec"]
        for c in resolved.components
        for r in c.resources
    }
    assert pods["web"]["nodeSelector"] == {"kubernetes.io/hostname": "shop-node"}
    assert pods["api"]["nodeSelector"] == {"kubernetes.io/hostname": "other-node"}


def test_pinned_images_and_unpinned_refusal() -> None:
    pipeline = _pipeline(deliver=Registry("oci://registry.example/shop"))
    images = pinned_images(pipeline, {"web": None})
    assert list(images) == ["cache"]
    assert images["cache"].ref == CACHE
    assert images["cache"].identity == "sha256:" + "d" * 64
    app = App("shop")
    app.deployment("cache", image="redis:7")
    moving = Pipeline(app, _target())
    with pytest.raises(PipelineError) as raised:
        pinned_images(moving, {})
    assert raised.value.code == "pipeline-image-not-pinned"


def test_release_spec_is_built_in_memory() -> None:
    pipeline = _pipeline(adopt=["Deployment/web"], execution={"max_seconds": 30})
    web = ImageRef("web", DIGEST, "r/web", None, DIGEST)
    spec = release_spec(pipeline, {"web": web, **pinned_images(pipeline, {"web": 1})})
    model = spec.model
    assert model.release.name == "shop" and model.release.adopt == ("Deployment/web",)
    assert model.release.composition == "piceli.pipeline:compose"
    assert spec.state_dir == pipeline.state_dir / "release"
    assert model.target.nodes["primary"].name == "shop-node"
    assert set(spec.images()) == {"cache", "web"}
    assert callable(spec.load_composition())
    assert model.execution.max_seconds == 30
    assert set(model.secrets) == {"password", "url"}


def test_model_fingerprint_is_stable_and_tracks_the_app() -> None:
    first = model_fingerprint(_pipeline())
    assert first == model_fingerprint(_pipeline())
    changed = _pipeline()
    changed.app.deployment("extra", image=CACHE)
    assert model_fingerprint(changed) != first


def test_delivery_strategies_describe_themselves() -> None:
    assert NodeLoopbackRegistry(port=5001).describe()["port"] == 5001
    assert NodeImport(target="docker://n?runtime=containerd").describe()[
        "strategy"
    ] == ("node-import")
    registry = Registry("oci://registry.example/shop", credentials=Path("creds.json"))
    assert registry.credentials == Path(__file__).parent / "creds.json"
    assert "credentials" not in registry.describe()
    with pytest.raises(PipelineError):
        Registry("https://registry.example")


# ------------------------------------------------------------------ checks


def test_default_check_runner_needs_piceli_checks(monkeypatch) -> None:
    def missing(name: str, *args: Any) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(PipelineError) as raised:
        default_runner()
    assert raised.value.code == "pipeline-checks-unavailable"


def test_describe_check_prefers_describe_then_dataclass() -> None:
    class Described:
        def describe(self) -> dict[str, Any]:
            return {"type": "http", "path": Path("/x")}

    assert describe_check(Described()) == {"type": "http", "path": "/x"}
    assert describe_check(object())["type"] == "object"


# ----------------------------------------------------------------- journal


def test_journal_lock_refuses_a_second_run(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "state")
    with journal.locked():
        with pytest.raises(PipelineError) as raised:
            with Journal(tmp_path / "state").locked():
                pass
        assert raised.value.code == "pipeline-locked"
    run = journal.create(
        pipeline={"app": "shop"},
        combined_hash="h",
        until="checks",
        approval="h",
        plan={},
    )
    run.set_stage("inputs", state="running")
    run.set_stage("inputs", state="done", output={"x": 1})
    assert journal.latest() is not None
    latest = journal.latest()
    assert latest is not None and latest.output("inputs") == {"x": 1}
    assert (run.path.stat().st_mode & 0o777) == 0o600


# ------------------------------------------------------- release source set


def test_release_source_image_set_roundtrip() -> None:
    images = {"web": DIGEST, "cache": "sha256:" + "d" * 64}
    source = ReleaseSource.image_set(images)
    assert source.kind == "oci-set"
    assert list(source.to_dict()["images"]) == ["cache", "web"]
    assert ReleaseSource(**source.to_dict()) == source
    with pytest.raises(ValueError, match="digest of its images"):
        ReleaseSource("oci-set", DIGEST, images=images)
    with pytest.raises(ValueError, match="exactly for oci-set"):
        ReleaseSource("oci", DIGEST, artifact_digest=DIGEST, images=images)
    with pytest.raises(ValueError):
        ReleaseSource.image_set({"web": "latest"})
    assert ReleaseRecord.from_dict  # records carry the set through to_dict/from_dict


# --------------------------------------------------------------------- cli


def _invoke(*args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(cli, ["deploy", *args])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result


def test_cli_rejects_missing_targets_and_flag_conflicts(tmp_path: Path) -> None:
    code, body, _ = _invoke(str(tmp_path / "missing.py:pipeline"))
    assert (code, body["state"], body["reason"]) == (
        2,
        "rejected",
        "pipeline-not-found",
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')\n")
    code, body, _ = _invoke(str(tmp_path / "broken.py:pipeline"))
    assert body["reason"] == "pipeline-load-failed"
    (tmp_path / "other.py").write_text("pipeline = 1\n")
    code, body, _ = _invoke(str(tmp_path / "other.py:pipeline"))
    assert body["reason"] == "pipeline-not-found"
    (tmp_path / "invalid.py").write_text(
        "from piceli.pipeline import Target\n"
        "target = Target.kubeconfig('k', context='c', namespace='NO')\n"
    )
    code, body, _ = _invoke(str(tmp_path / "invalid.py:target"))
    assert body["reason"] == "pipeline-invalid"
    code, body, _ = _invoke("x.py:p", "--resume", "--plan")
    assert body["reason"] == "deploy-flags-conflict"
    code, body, _ = _invoke("x.py:p", "--approve", "0" * 64, "--auto-approve")
    assert body["reason"] == "deploy-flags-conflict"


def test_shop_example_renders_and_declares_its_checks() -> None:
    result = CliRunner().invoke(
        cli, ["render", str(ROOT / "examples/shop/app.py:app"), "--namespace", "shop"]
    )
    assert result.exit_code == 0, result.output
    assert "pipeline.piceli.invalid/rust-hello:unresolved" in result.stdout
    assert "cache-state" in result.stdout
    from piceli.app.render import load_target

    pipeline = load_target(str(ROOT / "examples/shop/app.py:pipeline"), Path.cwd())
    assert type(pipeline).__name__ == "Pipeline"
    assert len(pipeline.checks) >= 1  # piceli.checks ships with the release engine
    assert list(pipeline.handles()) == ["rust-hello"]


def test_pipeline_import_is_side_effect_free() -> None:
    code = (
        "import sys; import piceli.pipeline; "
        "print(','.join(m for m in ('kubernetes', 'typer', 'piceli.pipeline.runner') "
        "if m in sys.modules))"
    )
    output = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == ""
