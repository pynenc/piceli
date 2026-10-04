"""``piceli bundle``: the files (snapshot), the overlay, image archives, checksums, the CLI."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from piceli.artifacts.build_spec import BuildReceipt
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.image_manifest import scan_image_stream
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from piceli.bundle.build import BundleError, build_bundle, render_files, write_bundle
from piceli.bundle.images import choose_platforms, receipt_images
from piceli.k8s.cli import app as cli
from tests.unit.bundle.fixtures import BUILD, app, components
from tests.unit.host_build_support import publish_base, write_project
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry

SNAPSHOTS = Path(__file__).parent / "snapshots"
HERE = Path(__file__).parent


def _check_snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    text = text.rstrip("\n") + "\n"  # one final newline, as pre-commit keeps it
    if os.environ.get("PICELI_UPDATE_SNAPSHOTS"):
        path.write_text(text)
    assert path.exists(), f"missing snapshot {name}; set PICELI_UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(), (
        f"snapshot {name} changed; review, then set PICELI_UPDATE_SNAPSHOTS=1"
    )


def _bundle(**overrides):
    shop, generators = app(**overrides)
    return build_bundle(
        components(shop, generators),
        name="shop",
        version="1.2.3",
        namespace="shop",
        generators=generators.generators,
        receipt_names=["web"],
        environment="client",
        storage_class="fast",
    )


def test_the_bundle_files_match_the_snapshot() -> None:
    files = render_files(_bundle())
    text = "".join(
        f"==> {name} <==\n{files[name].decode()}\n" for name in sorted(files)
    )
    _check_snapshot("shop-bundle.txt", text)


def test_every_object_is_labelled_part_of_and_has_no_namespace() -> None:
    bundle = _bundle()
    for item in bundle.objects:
        metadata = item.manifest["metadata"]
        assert metadata["labels"]["app.kubernetes.io/part-of"] == "shop"
        assert "namespace" not in metadata
        assert metadata["annotations"]["piceli.io/bundle"] == "shop@1.2.3"
    web = next(item for item in bundle.objects if item.kind == "Deployment")
    template = web.manifest["spec"]["template"]["metadata"]["labels"]
    assert template["app.kubernetes.io/part-of"] == "shop"
    assert (
        "app.kubernetes.io/part-of"
        not in web.manifest["spec"]["selector"]["matchLabels"]
    )
    assert {item.kind for item in bundle.objects if item.cluster} == {
        "ClusterRole",
        "ClusterRoleBinding",
    }
    assert not any(item.kind == "Secret" for item in bundle.objects)


def test_a_bundle_never_holds_a_secret_value() -> None:
    files = render_files(_bundle())
    for body in files.values():
        assert b"<redacted>" not in body and b"<private>" not in body


def test_an_image_without_digest_is_refused() -> None:
    with pytest.raises(BundleError) as caught:
        _bundle(web_image="registry.example/shop/web:latest")
    assert caught.value.code == "bundle-image-unpinned"


def test_a_build_image_needs_the_receipt() -> None:
    shop, generators = app(web_image=BUILD)
    with pytest.raises(BundleError) as caught:
        build_bundle(
            components(shop, generators), name="shop", version="1.0.0",
            namespace="shop", generators=generators.generators,
        )  # fmt: skip
    assert caught.value.code == "bundle-image-unresolved"


@pytest.mark.parametrize(
    ("name", "version"), [("Shop", "1.0.0"), ("shop", "1.0")], ids=["name", "version"]
)
def test_name_and_version_are_checked(name: str, version: str) -> None:
    shop, generators = app()
    with pytest.raises(BundleError) as caught:
        build_bundle(
            components(shop, generators), name=name, version=version,
            namespace="shop", generators=generators.generators,
        )  # fmt: skip
    assert caught.value.code == "bundle-invalid"


def test_write_refuses_a_directory_with_files(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("mine")
    with pytest.raises(BundleError) as caught:
        write_bundle(tmp_path, _bundle())
    assert caught.value.code == "bundle-out-refused"
    assert (tmp_path / "keep.txt").read_text() == "mine"


def _sums_hold(directory: Path) -> None:
    lines = (directory / "SHA256SUMS").read_text().splitlines()
    listed = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    on_disk = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    assert set(listed) == on_disk
    for name, digest in listed.items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest


def test_the_checksums_cover_every_file(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    summary = write_bundle(out, _bundle())
    _sums_hold(out)
    assert "SHA256SUMS" in summary["files"]
    assert os.access(out / "prepare.sh", os.X_OK)
    if shutil.which("sha256sum"):
        done = subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"], cwd=out, capture_output=True, check=False
        )
        assert done.returncode == 0


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_the_overlay_builds_with_kubectl_kustomize(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    write_bundle(out, _bundle())
    namespace = out / "overlays" / "dev" / "namespace.yaml"
    namespace.write_text(
        namespace.read_text().replace("namespace: shop", "namespace: elsewhere")
    )
    done = subprocess.run(
        ["kubectl", "kustomize", str(out / "overlays" / "dev")],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"], "KUBECONFIG": str(tmp_path / "none")},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    documents = [doc for doc in yaml.safe_load_all(done.stdout) if doc]
    for doc in documents:
        if doc["kind"].startswith("Cluster"):
            assert "namespace" not in doc["metadata"]
        else:
            assert doc["metadata"]["namespace"] == "elsewhere"
    binding = next(doc for doc in documents if doc["kind"] == "ClusterRoleBinding")
    assert binding["subjects"][0]["namespace"] == "elsewhere"
    cache = next(doc for doc in documents if doc["kind"] == "StatefulSet")
    assert (
        cache["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"] == "fast"
    )


# ------------------------------------------------------------- images


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def built(tmp_path: Path, registry: FakeRegistry) -> tuple[BuildReceipt, Path]:
    """A two-platform host build of image ``web`` (base from ``registry``)."""
    base = publish_base(registry, architectures=("amd64", "arm64"))
    path = write_project(tmp_path / "project", registry.port, base)
    spec = HostBuildSpec.from_toml(path).with_cache_dir(tmp_path / "cache")
    build = MultiPlatformHostBuild(spec, ("linux/amd64", "linux/arm64"))
    out = tmp_path / "out"
    grant = HostBuildGrant(build.plan().plan_hash, time.time() + 600)
    return build.run(grant, out, runner=Recorder()), out


def _exports(receipt: BuildReceipt, out: Path, wanted):
    grouped = receipt_images(receipt, out)
    return [
        (name, choose_platforms(name, platforms, wanted))
        for name, (_, platforms) in grouped.items()
    ]


def _layout(archive: Path) -> dict:
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        index = json.loads(tar.extractfile("index.json").read())  # type: ignore[union-attr]
        assert json.loads(tar.extractfile("oci-layout").read()) == {
            "imageLayoutVersion": "1.0.0"
        }  # type: ignore[union-attr]
        for name in names:
            if name.startswith("blobs/sha256/") and tar.getmember(name).isfile():
                body = tar.extractfile(name).read()  # type: ignore[union-attr]
                assert hashlib.sha256(body).hexdigest() == name.rsplit("/", 1)[1]
    return index


def test_amd64_archive_by_default_keeps_the_manifest_digest(
    tmp_path: Path, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    exports = _exports(receipt, out, ["linux/amd64"])
    bundle = _bundle(web_image=BUILD)
    summary = write_bundle(tmp_path / "bundle", bundle, exports)
    archive = tmp_path / "bundle" / "images" / "web.oci.tar"
    index = _layout(archive)
    entry = index["manifests"][0]
    assert entry["platform"] == {"architecture": "amd64", "os": "linux"}
    assert entry["annotations"] == {"org.opencontainers.image.ref.name": "1.2.3"}
    assert summary["images"][0]["digest"] == entry["digest"]
    assert entry["digest"] == receipt.images["web/linux-amd64"]["digest"]
    # The layout is a complete, valid image (every layer against its diff_id).
    with open(archive, "rb") as stream:
        plan = scan_image_stream(stream)
    assert plan.manifest_digest == entry["digest"]
    images_yaml = (tmp_path / "bundle" / "overlays" / "dev" / "images.yaml").read_text()
    assert entry["digest"] in images_yaml and "@REGISTRY@/web" in images_yaml
    attestations = sorted(
        path.name
        for path in (tmp_path / "bundle" / "images").iterdir()
        if path.suffix == ".json"
    )
    assert attestations == [
        "web.linux-amd64.provenance.json",
        "web.linux-amd64.sbom.spdx.json",
    ]
    _sums_hold(tmp_path / "bundle")
    manifest = json.loads((tmp_path / "bundle" / "bundle.json").read_text())
    web = next(item for item in manifest["images"] if item["source"] == "receipt")
    assert web["digest"] == entry["digest"]
    assert {item["kind"] for item in manifest["archives"][0]["attestations"]} == {
        "sbom",
        "provenance",
    }


def test_all_platforms_make_an_index_with_the_receipts_digest(
    tmp_path: Path, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    write_bundle(
        tmp_path / "bundle", _bundle(web_image=BUILD), _exports(receipt, out, None)
    )
    index = _layout(tmp_path / "bundle" / "images" / "web.oci.tar")
    top = index["manifests"][0]
    assert top["mediaType"] == "application/vnd.oci.image.index.v1+json"
    assert top["digest"] == receipt.data["outputs"]["indexes"]["web"]["digest"]


def test_a_platform_the_receipt_lacks_is_refused(
    built: tuple[BuildReceipt, Path],
) -> None:
    receipt, out = built
    from piceli.bundle.images import ImageExportError

    with pytest.raises(ImageExportError) as caught:
        _exports(receipt, out, ["linux/riscv64"])
    assert caught.value.code == "bundle-image-platform-missing"


def test_a_tampered_archive_is_refused_and_nothing_is_left(
    tmp_path: Path, built: tuple[BuildReceipt, Path]
) -> None:
    receipt, out = built
    exports = _exports(receipt, out, ["linux/amd64"])
    archive = exports[0][1][0].archive
    with tarfile.open(archive) as tar:
        members = [
            (info, tar.extractfile(info).read() if info.isfile() else None)
            for info in tar.getmembers()
        ]  # type: ignore[union-attr]
    with tarfile.open(archive, "w") as tar:
        for info, body in members:
            if body is not None and info.name.startswith("blobs/") and len(body) > 600:
                body = body[:-1] + bytes([body[-1] ^ 1])
            tar.addfile(info, io.BytesIO(body) if body is not None else None)
    with pytest.raises(BundleError) as caught:
        write_bundle(tmp_path / "bundle", _bundle(web_image=BUILD), exports)
    assert caught.value.code == "bundle-image-mismatch"
    assert not (tmp_path / "bundle").exists()


# ----------------------------------------------------------------- CLI


def test_cli_writes_a_bundle_and_prints_one_json_object(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    result = CliRunner().invoke(
        cli,
        [
            "bundle",
            f"{HERE / 'cli_app.py'}:pipeline",
            "--env",
            "client",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "written" and body["namespace"] == "shop-client"
    assert body["secrets"] == ["shop-private"]
    install = (out / "INSTALL.md").read_text()
    assert 'NAMESPACE="${NAMESPACE:-shop-client}"' in install
    assert "kubectl apply -k overlays/dev" in install
    assert "port-forward svc/web 18080:8080" in install


def test_cli_refuses_with_violations(tmp_path: Path) -> None:
    module = tmp_path / "unsafe.py"
    module.write_text(
        "from piceli import App\n"
        "app = App('shop')\n"
        "app.deployment('web', image='x@sha256:' + 'a' * 64)\n"
    )
    result = CliRunner().invoke(
        cli, ["bundle", f"{module}:app", "--out", str(tmp_path / "b")]
    )
    assert result.exit_code == 2
    body = json.loads(result.stdout.splitlines()[0])
    assert body["reason"] == "bundle-unsafe-non-root"
    assert {item["rule"] for item in body["violations"]} == {
        "non-root",
        "read-only-root",
        "no-privilege",
        "resources",
    }
    assert not (tmp_path / "b").exists()
