"""``app.cluster_identity``: the kube-system UID read at runtime, least privilege."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from piceli import App, ClusterIdentity, PodDefaults, Resources, Security
from piceli.app.identity import SCRIPT
from piceli.bundle.safety import check

IMAGE = "registry.example/web@sha256:" + "a" * 64
UID = "5f51b1b5-0000-4000-8000-222bb2c92398"


def _app() -> tuple[App, ClusterIdentity]:
    app = App(
        "shop",
        pod_defaults=PodDefaults(
            security=Security.restricted(read_only_root_filesystem=True)
        ),
    )
    identity = app.cluster_identity(image=IMAGE)
    web = app.deployment(
        "web",
        image=IMAGE,
        init=[identity.init],
        volumes=dict(identity.volumes),
        service_account=identity.service_account,
        env={"CLUSTER_UID_FILE": identity.file},
        resources=Resources(cpu="1m", memory="1Mi", cpu_limit="1m", memory_limit="1Mi"),
    )
    app.network_policy(web, egress=[identity.egress], name="web-api-server")
    return app, identity


def test_identity_renders_least_privilege_rbac_and_passes_the_gate() -> None:
    app, identity = _app()
    manifests = [
        resource.manifest
        for component in app.render("shop").components
        for resource in component.resources
    ]
    role = next(item for item in manifests if item["kind"] == "ClusterRole")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "verbs": ["get"],
            "resourceNames": ["kube-system"],
        }
    ]
    assert not any(item["kind"] in {"Role", "RoleBinding"} for item in manifests)
    deployment = next(item for item in manifests if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "cluster-identity"
    assert [item["name"] for item in pod["initContainers"]] == ["cluster-identity"]
    mounts = {item["mountPath"] for item in pod["containers"][0]["volumeMounts"]}
    assert "/run/cluster-identity" in mounts
    assert identity.file == "/run/cluster-identity/uid"
    assert check(manifests) == []
    assert ClusterIdentity.rule() == identity.service_account.cluster_rules[0]


@pytest.mark.parametrize("tool", ["curl", "wget"])
def test_the_init_script_writes_the_uid(tmp_path: Path, tool: str) -> None:
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("token-value")
    (sa / "ca.crt").write_text("ca")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body = json.dumps(
        {
            "kind": "Namespace",
            "apiVersion": "v1",
            "metadata": {
                "name": "kube-system",
                "uid": UID,
                "labels": {"kubernetes.io/metadata.name": "kube-system"},
                "managedFields": [{"manager": "k3s"}],
            },
        },
        indent=2,
    )
    (tmp_path / "body.json").write_text(body)
    stub = bin_dir / tool
    stub.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" > "{tmp_path}/args"\ncat "{tmp_path}/body.json"\n'
    )
    stub.chmod(0o755)
    for name in ("cat", "tr", "sed", "mv", "printf"):
        found = subprocess.run(
            ["sh", "-c", f"command -v {name}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if found.startswith("/"):
            (bin_dir / name).symlink_to(found)
    done = subprocess.run(
        ["/bin/sh", "-c", SCRIPT],
        env={
            "PATH": str(bin_dir),
            "SERVICE_ACCOUNT_DIR": str(sa),
            "KUBERNETES_SERVICE_HOST": "10.43.0.1",
            "KUBERNETES_SERVICE_PORT": "443",
            "IDENTITY_FILE": str(tmp_path / "uid"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "uid").read_text() == UID + "\n"
    assert "token-value" not in done.stdout + done.stderr
    args = (tmp_path / "args").read_text()
    assert "https://10.43.0.1:443/api/v1/namespaces/kube-system" in args
    if tool == "curl":
        assert "--cacert" in args


def test_the_init_script_fails_without_a_uid(tmp_path: Path) -> None:
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("t")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/sh\necho '{}'\n")
    (bin_dir / "curl").chmod(0o755)
    done = subprocess.run(
        ["/bin/sh", "-c", SCRIPT],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "SERVICE_ACCOUNT_DIR": str(sa),
            "KUBERNETES_SERVICE_HOST": "10.43.0.1",
            "IDENTITY_FILE": str(tmp_path / "uid"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1 and "no kube-system uid" in done.stderr
    assert not (tmp_path / "uid").exists()
