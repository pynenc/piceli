"""Pure installation rendering and deployment-boundary checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from piceli.server.cluster_install import (
    ClusterInstallConfig,
    DeployResourceRule,
    ManualDeliveryConfig,
    cluster_install_yaml,
    render_cluster_install,
)


def _config() -> ClusterInstallConfig:
    return ClusterInstallConfig(
        namespace="shop",
        origin="https://piceli.example.test",
        api_server="https://kubernetes.default.svc:443",
        oidc_issuer="https://id.example.test",
        oidc_metadata_url="https://id.example.test/.well-known/openid-configuration",
        oidc_client_id="piceli-ui",
        authorized_subjects=("operator-subject",),
        ui_image="registry.example.test/piceli@sha256:" + "a" * 64,
        gateway_image="registry.example.test/caddy@sha256:" + "b" * 64,
        tls_secret="piceli-ui-tls",
        ingress_class="nginx",
        ingress_namespace="ingress-nginx",
        ingress_pod_labels={"app.kubernetes.io/name": "ingress-nginx"},
        backend_tls_annotation_key="nginx.ingress.kubernetes.io/backend-protocol",
        backend_tls_annotation_value="HTTPS",
        api_egress_cidrs=("10.0.0.1/32",),
        oidc_egress_cidrs=("192.0.2.12/32",),
    )


def _find(objects: tuple[dict, ...], kind: str, name: str) -> dict:
    return next(
        item
        for item in objects
        if item["kind"] == kind and item["metadata"]["name"] == name
    )


def _manual() -> ManualDeliveryConfig:
    return ManualDeliveryConfig(
        release_definition_toml="""[target]
kubeconfig = "/var/lib/piceli/control/target.kubeconfig"
context = "piceli-incluster"
namespace = "shop"

[release]
name = "shop"
owner = "shop"
field_manager = "piceli-ui"
composition = "composition.py:build"
state_dir = "/var/lib/piceli/control/release-state"
""",
        source_files={"composition.py": "def build(ctx):\n    return ctx\n"},
        source_file_allowlist=("composition.py",),
        renderer_image="registry.example.test/piceli-renderer@sha256:" + "c" * 64,
        renderer_platform="linux/arm64",
        authorized_deploy_subjects=("operator-subject",),
        deploy_resources=(
            DeployResourceRule(
                "apps", "deployments", ("get", "list", "watch", "create", "patch")
            ),
        ),
    )


def test_install_is_single_replica_private_and_explicitly_targeted() -> None:
    config = _config()
    objects = render_cluster_install(config)
    assert tuple(yaml.safe_load_all(cluster_install_yaml(config))) == objects
    assert all(
        item["metadata"].get("namespace") == "shop"
        for item in objects
        if item["kind"] not in {"ClusterRole", "ClusterRoleBinding"}
    )
    deployment = _find(objects, "Deployment", "piceli-ui")
    spec = deployment["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "piceli-ui"
    assert pod["automountServiceAccountToken"] is False
    ui, gateway = pod["containers"]
    assert gateway["command"] == ["sh", "-c"]
    assert gateway["args"] == [
        "cp /usr/bin/caddy /tmp/caddy && "
        "exec /tmp/caddy run --config /etc/piceli/Caddyfile"
    ]
    command = ui["command"]
    assert command[:3] == ["piceli", "ui", "cluster-observe"]
    assert command[command.index("--namespace") + 1] == "shop"
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--origin") + 1] == config.origin
    assert "--authorized-access-sub" not in command
    assert "--token-file" in command and "--ca-file" in command
    assert "KUBECONFIG" not in str(pod)
    assert "--renderer-image" not in command
    assert all(
        container["securityContext"]["readOnlyRootFilesystem"]
        for container in (ui, gateway)
    )
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["state"]["persistentVolumeClaim"]["claimName"] == config.state_claim
    projection = volumes["credentials"]["projected"]["sources"]
    assert projection[0]["serviceAccountToken"]["path"] == "token"
    assert projection[1]["configMap"]["name"] == "kube-root-ca.crt"
    assert _find(objects, "PersistentVolumeClaim", config.state_claim)["spec"][
        "accessModes"
    ] == ["ReadWriteOnce"]


def test_tls_service_ingress_and_network_policy_have_explicit_boundaries() -> None:
    objects = render_cluster_install(_config())
    service = _find(objects, "Service", "piceli-ui")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "https", "port": 443, "targetPort": 8443}
    ]
    ingress = _find(objects, "Ingress", "piceli-ui")
    assert ingress["spec"]["rules"][0]["host"] == "piceli.example.test"
    assert ingress["spec"]["tls"] == [
        {"hosts": ["piceli.example.test"], "secretName": "piceli-ui-tls"}
    ]
    assert (
        ingress["metadata"]["annotations"][
            "nginx.ingress.kubernetes.io/backend-protocol"
        ]
        == "HTTPS"
    )
    gateway = _find(objects, "ConfigMap", "piceli-ui-gateway")["data"]["Caddyfile"]
    assert "tls /etc/piceli/tls/tls.crt /etc/piceli/tls/tls.key" in gateway
    assert "reverse_proxy 127.0.0.1:8000" in gateway
    policy = _find(objects, "NetworkPolicy", "piceli-ui-network")["spec"]
    ingress_from = policy["ingress"][0]["from"][0]
    assert ingress_from["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ingress-nginx"
    }
    assert ingress_from["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "ingress-nginx"
    }
    assert policy["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    egress_cidrs = {
        destination["ipBlock"]["cidr"]
        for rule in policy["egress"]
        for destination in rule["to"]
        if "ipBlock" in destination
    }
    assert egress_cidrs == {"10.0.0.1/32", "192.0.2.12/32"}


def test_renderer_identity_cannot_access_api_or_network() -> None:
    objects = render_cluster_install(_config())
    renderer = _find(objects, "ServiceAccount", "piceli-renderer")
    assert renderer["automountServiceAccountToken"] is False
    assert all(
        not any(
            subject.get("name") == "piceli-renderer"
            for subject in item.get("subjects", [])
        )
        for item in objects
        if item["kind"] in {"RoleBinding", "ClusterRoleBinding"}
    )
    policy = _find(objects, "NetworkPolicy", "piceli-renderer-deny-egress")["spec"]
    assert policy["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/component": "renderer"
    }
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    assert policy["ingress"] == []
    assert policy["egress"] == []
    role = _find(objects, "Role", "piceli-ui")
    permissions = {
        (group, resource, verb)
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
    }
    assert ("", "pods/log", "get") in permissions
    assert ("networking.k8s.io", "networkpolicies", "list") in permissions
    assert not any(
        verb in {"create", "patch", "update", "delete"} for _, _, verb in permissions
    )
    assert not any(resource == "secrets" for _, resource, _ in permissions)
    assert not any(
        resource == "deployments" and verb in {"create", "patch", "update", "delete"}
        for _, resource, verb in permissions
    )
    identity = next(item for item in objects if item["kind"] == "ClusterRole")
    assert identity["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "resourceNames": ["kube-system", "shop"],
            "verbs": ["get"],
        }
    ]


def test_manual_profile_mounts_reviewed_source_and_only_configured_write_kinds() -> (
    None
):
    manual = _manual()
    objects = render_cluster_install(
        replace(_config(), experimental=True, manual=manual)
    )
    deployment = _find(objects, "Deployment", "piceli-ui")
    pod = deployment["spec"]["template"]["spec"]
    command = pod["containers"][0]["command"]
    assert command[:3] == ["piceli", "ui", "cluster-serve"]
    assert (
        command[command.index("--definition") + 1] == "/opt/piceli/source/release.toml"
    )
    assert command[command.index("--source-root") + 1] == "/opt/piceli/source"
    assert command[command.index("--source-file") + 1] == "composition.py"
    assert command[command.index("--renderer-image") + 1] == manual.renderer_image
    assert command[command.index("--renderer-platform") + 1] == "linux/arm64"
    assert command[command.index("--authorized-deploy-sub") + 1] == "operator-subject"
    source_mount = next(
        item
        for item in pod["containers"][0]["volumeMounts"]
        if item["name"] == "release-source"
    )
    assert source_mount["readOnly"] is True
    source_volume = next(
        item for item in pod["volumes"] if item["name"] == "release-source-configmap"
    )
    assert source_volume["configMap"]["items"] == [
        {"key": "release.toml", "path": "release.toml"},
        {"key": "src-000", "path": "composition.py"},
    ]
    source_map = _find(objects, "ConfigMap", "piceli-ui-source")["data"]
    assert source_map == {
        "release.toml": manual.release_definition_toml,
        "src-000": manual.source_files["composition.py"],
    }
    assert next(
        item for item in pod["volumes"] if item["name"] == "release-source"
    ) == {
        "name": "release-source",
        "emptyDir": {},
    }
    init = pod["initContainers"][0]
    assert init["name"] == "materialize-release-source"
    assert init["image"] == _config().ui_image
    assert init["securityContext"]["readOnlyRootFilesystem"] is True
    assert init["command"][:3] == ["python", "-I", "-c"]
    assert [mount["name"] for mount in init["volumeMounts"]] == [
        "release-source-configmap",
        "release-source",
    ]
    assert "credentials" not in {mount["name"] for mount in init["volumeMounts"]}
    rules = _find(objects, "Role", "piceli-ui")["rules"]
    writes = {
        (group, resource, verb)
        for rule in rules
        for group in rule["apiGroups"]
        for resource in rule["resources"]
        for verb in rule["verbs"]
        if verb in {"create", "patch", "update", "delete"}
    }
    assert writes == {
        ("", "configmaps", "create"),
        ("", "configmaps", "delete"),
        ("batch", "jobs", "create"),
        ("batch", "jobs", "delete"),
        ("apps", "deployments", "create"),
        ("apps", "deployments", "patch"),
    }
    assert any(
        "batch" in rule["apiGroups"]
        and "jobs" in rule["resources"]
        and "get" in rule["verbs"]
        for rule in rules
    )


def test_materialized_nested_configmap_source_is_regular_and_digest_checked(
    tmp_path: Path,
) -> None:
    manual = replace(
        _manual(),
        source_files={
            "package/__init__.py": "",
            "package/composition.py": "def build(ctx): return ctx\n",
        },
        source_file_allowlist=("package/__init__.py", "package/composition.py"),
    )
    pod = _find(
        render_cluster_install(replace(_config(), experimental=True, manual=manual)),
        "Deployment",
        "piceli-ui",
    )["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    names = dict(json.loads(init["args"][2]))
    projected = tmp_path / "projected"
    data = projected / "..data"
    data.mkdir(parents=True)
    for name, content in {
        "release.toml": manual.release_definition_toml,
        **manual.source_files,
    }.items():
        destination = data / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        link = projected / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(Path(os.path.relpath(destination, link.parent)))
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    command = [
        sys.executable,
        "-I",
        "-c",
        init["command"][3],
        str(projected),
        str(materialized),
        init["args"][2],
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert set(names) == {
        "release.toml",
        "package/__init__.py",
        "package/composition.py",
    }
    for name in names:
        copied = materialized / name
        assert copied.is_file() and not copied.is_symlink()
        assert copied.read_bytes() == (data / name).read_bytes()
        assert copied.stat().st_mode & 0o777 == 0o444
    assert (materialized / "package").stat().st_mode & 0o777 == 0o755
    retried = subprocess.run(command, capture_output=True, text=True, check=False)
    assert retried.returncode == 0, retried.stderr

    (data / "package/composition.py").write_text("tampered")
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "does not match" in rejected.stderr
    assert (materialized / "package/composition.py").read_text() == manual.source_files[
        "package/composition.py"
    ]


def test_manual_profile_cannot_escape_source_or_expand_resource_scope() -> None:
    manual = _manual()
    with pytest.raises(ValueError, match="safe relative"):
        replace(
            manual,
            source_files={"../secret.py": "content"},
            source_file_allowlist=("../secret.py",),
        )
    with pytest.raises(ValueError, match="source allowlist"):
        replace(manual, source_file_allowlist=("other.py",))
    with pytest.raises(ValueError, match="parent file"):
        replace(
            manual,
            source_files={"package.py": "pass", "package.py/nested.py": "pass"},
            source_file_allowlist=("package.py", "package.py/nested.py"),
        )
    with pytest.raises(ValueError, match="target and subjects"):
        replace(
            _config(),
            experimental=True,
            manual=replace(manual, authorized_deploy_subjects=("other",)),
        )
    with pytest.raises(ValueError, match="target and subjects"):
        replace(
            _config(),
            experimental=True,
            manual=replace(
                manual,
                release_definition_toml=manual.release_definition_toml.replace(
                    'namespace = "shop"', 'namespace = "other"'
                ),
            ),
        )
    with pytest.raises(ValueError, match="namespaced write"):
        DeployResourceRule("", "namespaces", ("create",))
    with pytest.raises(ValueError, match="namespaced write"):
        DeployResourceRule("apps", "*", ("create",))


def test_remote_local_client_grant_is_opt_in_and_adds_no_write_role() -> None:
    config = replace(
        _config(), experimental=True, authorized_access_subjects=("operator-subject",)
    )
    objects = render_cluster_install(config)
    command = _find(objects, "Deployment", "piceli-ui")["spec"]["template"]["spec"][
        "containers"
    ][0]["command"]
    assert command[command.index("--authorized-access-sub") + 1] == "operator-subject"
    assert "--authorized-deploy-sub" not in command
    assert "--experimental" not in command
    role = _find(objects, "Role", "piceli-ui")
    assert all(
        verb not in {"create", "patch", "update", "delete"}
        for rule in role["rules"]
        for verb in rule["verbs"]
    )
    with pytest.raises(ValueError, match="observation subjects"):
        replace(_config(), authorized_access_subjects=("other",))


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"origin": "http://piceli.example.test"}, "HTTPS"),
        ({"origin": "https://piceli.example.test:8443"}, "port 443"),
        ({"origin": "https://127.0.0.2"}, "public HTTPS DNS"),
        ({"ui_image": "registry.example.test/piceli:latest"}, "sha256"),
        ({"api_egress_cidrs": ("0.0.0.0/0",)}, "unrestricted"),
        ({"authorized_subjects": ()}, "at least one"),
        ({"state_size": "1"}, "quantity"),
        ({"backend_tls_annotation_value": "HTTP"}, "HTTPS backend"),
    ],
)
def test_unsafe_or_unusable_configuration_is_rejected(change: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        replace(_config(), **change)


def test_oidc_issuer_may_have_a_realm_path() -> None:
    config = replace(
        _config(),
        oidc_issuer="https://id.example.test/realms/shop",
        oidc_metadata_url=(
            "https://id.example.test/realms/shop/.well-known/openid-configuration"
        ),
    )
    command = _find(render_cluster_install(config), "Deployment", "piceli-ui")["spec"][
        "template"
    ]["spec"]["containers"][0]["command"]
    assert command[command.index("--oidc-issuer") + 1] == config.oidc_issuer


def test_restored_claim_and_url_prefix_change_the_targeted_installation() -> None:
    config = replace(
        _config(), state_claim="piceli-ui-state-restored", url_prefix="/piceli"
    )
    objects = render_cluster_install(config)
    deployment = _find(objects, "Deployment", "piceli-ui")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == config.state_claim
    assert _find(objects, "PersistentVolumeClaim", config.state_claim)
    assert not any(
        item["kind"] == "PersistentVolumeClaim"
        and item["metadata"]["name"] == "piceli-ui-state"
        for item in objects
    )
    command = pod["containers"][0]["command"]
    assert command[command.index("--url-prefix") + 1] == "/piceli"
    ingress = _find(objects, "Ingress", "piceli-ui")
    assert ingress["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/piceli"


def test_manual_and_local_client_profiles_need_no_experimental_opt_in() -> None:
    manual = replace(_config(), manual=_manual())
    access = replace(_config(), authorized_access_subjects=("operator-subject",))
    assert _find(render_cluster_install(manual), "Deployment", "piceli-ui")
    assert _find(render_cluster_install(access), "Deployment", "piceli-ui")
    command = _find(render_cluster_install(_config()), "Deployment", "piceli-ui")[
        "spec"
    ]["template"]["spec"]["containers"][0]["command"]
    assert "--experimental" not in command
