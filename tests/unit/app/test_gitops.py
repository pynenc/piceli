"""GitOps handoff: file content rules, the Flux OCI layout, ``render --out`` and ``publish``."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from piceli.gitops import (
    FLUX_CONFIG,
    FLUX_CONTENT,
    MARKER,
    OCI_MANIFEST,
    GitOpsError,
    ManifestFile,
    artifact_annotations,
    file_name,
    flux_artifact,
    handoff,
)
from piceli.k8s.cli import app as cli
from tests.oci_registry import PASSWORD, USER, oci_registry

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "release"
IMAGE = "nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
SHOP = f"""
from piceli import App

app = App("shop")
web = app.deployment("web", image="{IMAGE}", ports=[80])
app.service(web, port=80)
app.config("settings", {{"greeting": "hello"}})
"""
# Golden values: a change here changes every published digest; bump on purpose.
GOLDEN_FILES = [
    "settings_configmap.core_shop_settings.yaml",
    "web_deployment.apps_shop_web.yaml",
    "web_service.core_shop_web.yaml",
]


def _component(name, *manifests, bindings=()):
    return {
        "name": name,
        "dependencies": [],
        "resources": [
            {"manifest": manifest, "secret_bindings": list(bindings)}
            for manifest in manifests
        ],
    }


def _config(name="settings", namespace="shop"):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": {"greeting": "hello"},
    }


def _secret():
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "db", "namespace": "shop"},
        "data": {"password": "<redacted>"},
    }


def _run(*args, cwd=None):
    return CliRunner().invoke(cli, list(args))


@pytest.fixture
def shop(tmp_path):
    path = tmp_path / "shop.py"
    path.write_text(textwrap.dedent(SHOP))
    return str(path) + ":app"


# ------------------------------------------------------------------ content


def test_file_names_are_unique_and_stable():
    assert (
        file_name(
            "web",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "web", "namespace": "shop"},
            },
        )
        == "web_deployment.apps_shop_web.yaml"
    )
    assert (
        file_name(
            "rbac",
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": "shop:api"},
            },
        )
        == "rbac_clusterrole.rbac.authorization.k8s.io_cluster_shop-api.yaml"
    )


def test_secret_objects_are_refused_or_left_out():
    components = [_component("config", _config(), _secret())]
    with pytest.raises(GitOpsError) as refused:
        handoff(components)
    assert refused.value.code == "gitops-secrets-present"
    result = handoff(components, "external")
    assert result.omitted_secrets == ("shop/db",)
    assert [item.path for item in result.files] == [
        "config_configmap.core_shop_settings.yaml"
    ]
    assert b"<redacted>" not in result.files[0].content
    with pytest.raises(GitOpsError) as empty:
        handoff([_component("config", _secret())], "external")
    assert empty.value.code == "gitops-empty"


def test_redacted_values_bindings_and_placeholder_images_are_refused():
    leaked = _config()
    leaked["data"] = {"api_token": "<redacted>"}
    with pytest.raises(GitOpsError) as redacted:
        handoff([_component("c", leaked)], "external")
    assert redacted.value.code == "gitops-secret-value"
    bound = [{"pointer": "/data/x", "input": "token"}]
    with pytest.raises(GitOpsError) as binding:
        handoff([_component("c", _config(), bindings=bound)], "external")
    assert binding.value.code == "gitops-secret-value"
    for image in (
        "pipeline.piceli.invalid/api:unresolved",
        "pending-build.piceli.invalid/api@sha256:" + "0" * 64,
        "docker.io/library/nginx@sha256:" + "0" * 64,
    ):
        pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "p", "namespace": "shop"},
            "spec": {"containers": [{"name": "c", "image": image}]},
        }
        with pytest.raises(GitOpsError) as unresolved:
            handoff([_component("c", pod)])
        assert unresolved.value.code == "gitops-image-unresolved"


def test_an_external_secret_passes_with_its_public_fields_declared():
    from piceli.k8s.ops.discovery import public_manifest

    external = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {
            "name": "db",
            "namespace": "shop",
            "annotations": {
                "piceli.io/public-fields": "spec.secretStoreRef,spec.data.*.secretKey"
            },
        },
        "spec": {
            "secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"},
            "target": {"name": "db"},
            "data": [{"secretKey": "password", "remoteRef": {"key": "shop/db"}}],
        },
    }
    rendered = public_manifest(external)[0]
    result = handoff([_component("db", rendered)])
    assert yaml.safe_load(result.files[0].content) == external


# ------------------------------------------------------------------- layout


def test_flux_layout_media_types_and_deterministic_digest():
    files = handoff([_component("config", _config("b"), _config("a"))]).files
    first = flux_artifact(files, artifact_annotations(namespace="shop"))
    again = flux_artifact(
        tuple(reversed(files)), artifact_annotations(namespace="shop")
    )
    assert first.digest == again.digest and first.manifest == again.manifest
    manifest = json.loads(first.manifest)
    assert (
        manifest["mediaType"]
        == OCI_MANIFEST
        == "application/vnd.oci.image.manifest.v1+json"
    )
    assert (
        manifest["config"]["mediaType"]
        == FLUX_CONFIG
        == "application/vnd.cncf.flux.config.v1+json"
    )
    assert (
        [layer["mediaType"] for layer in manifest["layers"]]
        == [FLUX_CONTENT]
        == ["application/vnd.cncf.flux.content.v1.tar+gzip"]
    )
    assert manifest["annotations"] == {"io.piceli.render.namespace": "shop"}
    assert manifest["config"]["digest"] == first.config.digest
    assert manifest["layers"][0] == first.layer.descriptor()
    # the layer is a reproducible gzip'd tar of the sorted files
    assert first.layer.body[4:8] == b"\0\0\0\0"  # gzip mtime
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(first.layer.body))) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == sorted(item.path for item in files)
        assert {(m.mtime, m.uid, m.gid, m.uname, m.mode) for m in members} == {
            (0, 0, 0, "", 0o644)
        }
        assert tar.extractfile(members[0]).read() == files[0].content
    config = json.loads(first.config.body)
    assert config["rootfs"]["type"] == "layers" and "created" not in config
    # annotations are part of the digest; the content changes it too
    assert (
        flux_artifact(files, artifact_annotations(namespace="other")).digest
        != first.digest
    )
    changed = [ManifestFile(files[0].path, files[0].content + b"# x\n")]
    assert flux_artifact(changed).digest != flux_artifact(files[:1]).digest


def test_golden_digest_of_a_typed_app(shop):
    result = _run(
        "publish", shop, "--namespace", "shop", "--to", "oci://127.0.0.1:9/shop/app:v1"
    )
    assert result.exit_code == 3, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "approval-required"
    assert body["files"] == GOLDEN_FILES
    assert body["layer"]["mediaType"] == FLUX_CONTENT
    assert body["config"]["mediaType"] == FLUX_CONFIG
    assert body["digest"] == (
        "sha256:8b83fcb7980671f3836393f86dd4e3de14499ac864c010c247ade03731840d44"
    )
    assert "approve with: --approve " + body["digest"] in result.stderr


def test_annotations_are_validated():
    with pytest.raises(ValueError):
        artifact_annotations(revision="bad\nvalue")
    assert artifact_annotations(
        source="https://git.example/shop", revision="main@sha1:abc"
    ) == {
        "org.opencontainers.image.source": "https://git.example/shop",
        "org.opencontainers.image.revision": "main@sha1:abc",
    }


# -------------------------------------------------------------- render --out


def test_render_out_writes_files_and_rewrites_only_its_own(tmp_path, shop):
    out = tmp_path / "deploy"
    result = _run("render", shop, "--namespace", "shop", "--out", str(out))
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "written" and body["files"] == GOLDEN_FILES
    assert sorted(p.name for p in out.iterdir()) == sorted([MARKER, *GOLDEN_FILES])
    (out / "README.md").write_text("mine\n")
    # a re-render replaces its files and leaves others alone
    smaller = tmp_path / "smaller.py"  # a new file: modules are cached per path
    smaller.write_text(
        textwrap.dedent(SHOP).replace(
            'app.config("settings", {"greeting": "hello"})\n', ""
        )
    )
    result = _run("render", f"{smaller}:app", "--namespace", "shop", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] == [GOLDEN_FILES[0]]
    assert (out / "README.md").read_text() == "mine\n"
    assert not (out / GOLDEN_FILES[0]).exists()

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "x.yaml").write_text("{}\n")
    result = _run("render", shop, "--namespace", "shop", "--out", str(foreign))
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "render-out-refused"
    assert (foreign / "x.yaml").read_text() == "{}\n"


def test_render_out_refuses_secrets_unless_external(tmp_path):
    out = tmp_path / "out"
    result = _run("render", "--spec", str(EXAMPLE / "release.toml"), "--out", str(out))
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "gitops-secrets-present"
    assert not out.exists()
    result = _run(
        "render",
        "--spec",
        str(EXAMPLE / "release.toml"),
        "--out",
        str(out),
        "--secrets",
        "external",
    )
    # the example pins its image to a placeholder digest: refused too
    assert json.loads(result.stdout)["reason"] == "gitops-image-unresolved"


# ------------------------------------------------------------------ publish


def test_publish_pushes_by_digest_then_tag_to_a_registry(tmp_path, shop):
    with oci_registry(auth="bearer") as registry:
        target = f"oci://{registry.host}/shop/app:v1"
        credentials = tmp_path / "creds.json"
        credentials.write_text(json.dumps({"username": USER, "password": PASSWORD}))
        os.chmod(credentials, 0o600)
        preview = _run("publish", shop, "--namespace", "shop", "--to", target)
        assert preview.exit_code == 3 and registry.mutations() == []
        digest = json.loads(preview.stdout)["digest"]

        wrong = _run(
            "publish",
            shop,
            "--namespace",
            "shop",
            "--to",
            target,
            "--approve",
            "sha256:" + "1" * 64,
            "--credentials",
            str(credentials),
        )
        assert wrong.exit_code == 2
        assert json.loads(wrong.stdout)["reason"] == "gitops-artifact-changed"
        assert registry.mutations() == []

        result = _run(
            "publish",
            shop,
            "--namespace",
            "shop",
            "--to",
            target,
            "--approve",
            digest,
            "--credentials",
            str(credentials),
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert body["state"] == "published" and body["digest"] == digest
        assert body["reference"] == f"{registry.host}/shop/app@{digest}"
        assert body["blobs_pushed"] == 2 and body["tag"] == "v1"
        assert PASSWORD not in result.output and USER not in result.output
        manifest, media = registry.manifests[("shop/app", digest)]
        assert media == OCI_MANIFEST
        assert registry.manifests[("shop/app", "v1")][0] == manifest
        layer = json.loads(manifest)["layers"][0]
        assert json.loads(manifest)["config"]["mediaType"] == FLUX_CONFIG
        blob = registry.blobs[("shop/app", layer["digest"])]
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as tar:
            assert tar.getnames() == GOLDEN_FILES
        # the manifest went up by digest before the tag
        puts = [p for m, p in registry.requests if m == "PUT" and "/manifests/" in p]
        assert puts == [f"/v2/shop/app/manifests/{digest}", "/v2/shop/app/manifests/v1"]

        again = _run(
            "publish",
            shop,
            "--namespace",
            "shop",
            "--to",
            target,
            "--approve",
            digest,
            "--credentials",
            str(credentials),
        )
        assert again.exit_code == 0, again.output
        assert json.loads(again.stdout)["blobs_present"] == 2


def test_publish_refusals_and_failures(tmp_path, shop):
    result = _run("publish", shop, "--namespace", "shop")
    assert json.loads(result.stdout)["reason"] == "gitops-target-invalid"
    result = _run("publish", shop, "--to", "oci://registry.example:80/x?tls=false")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "plain-http-not-loopback"
    result = _run(
        "publish",
        "--spec",
        str(EXAMPLE / "release.toml"),
        "--to",
        "oci://127.0.0.1:9/x",
    )
    assert json.loads(result.stdout)["reason"] == "gitops-secrets-present"

    open_creds = tmp_path / "open.json"
    open_creds.write_text(json.dumps({"token": "t"}))
    os.chmod(open_creds, 0o644)
    preview = _run(
        "publish", shop, "--namespace", "shop", "--to", "oci://127.0.0.1:9/x"
    )
    digest = json.loads(preview.stdout)["digest"]
    result = _run(
        "publish",
        shop,
        "--namespace",
        "shop",
        "--to",
        "oci://127.0.0.1:9/x",
        "--approve",
        digest,
        "--credentials",
        str(open_creds),
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "credentials-file-not-private"

    with oci_registry() as registry:
        port = registry.port
    # the registry is gone: the push fails (exit 1) with a registered code
    result = _run(
        "publish",
        shop,
        "--namespace",
        "shop",
        "--to",
        f"oci://127.0.0.1:{port}/x",
        "--approve",
        digest,
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["reason"] == "registry-unreachable"
