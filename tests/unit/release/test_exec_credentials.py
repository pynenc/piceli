"""Exec credential plugins: opt-in, pinning, minimal environment and refresh.

A fake plugin (a small Python script) prints an ExecCredential; it records its
run count and environment in files next to it so the tests can observe them.
No test reaches a real cluster: HTTP goes to the loopback fake API only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from piceli.errors import ERRORS
from piceli.k8s.ops import exec_credentials as ec
from piceli.k8s.ops.discovery import EvidenceSource
from piceli.k8s.ops.exec_credentials import (
    ClusterInfo,
    ExecAuthError,
    ExecCredentialSource,
    ExecPolicy,
    approved_refresh,
    parse_credential,
    resolve_plugin,
)
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider
from piceli.k8s.ops.provider_factory import (
    KubeconfigTarget,
    ProviderFactoryError,
    api_client_from_kubeconfig,
    build_provider,
)
from tests.acceptance.fake_api import TARGET, serve

V1 = "client.authentication.k8s.io/v1"

PLUGIN = """\
#!{python}
import json, os, sys, time
from datetime import datetime, timedelta, timezone
here = os.path.dirname(os.path.abspath(__file__))
count_file = os.path.join(here, "runs")
runs = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
open(count_file, "w").write(str(runs))
json.dump(dict(os.environ), open(os.path.join(here, "env.json"), "w"))
mode = open(os.path.join(here, "mode")).read().strip() if os.path.exists(
    os.path.join(here, "mode")) else "token"
if mode == "fail":
    sys.stderr.write("please log in\\n")
    sys.exit(3)
if mode == "hang":
    time.sleep(30)
if mode == "garbage":
    print("not json")
    sys.exit(0)
status = {{}}
expires = float(os.environ.get("FAKE_EXPIRES_IN", "0"))
if expires:
    at = datetime.now(timezone.utc) + timedelta(seconds=expires)
    status["expirationTimestamp"] = at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
if mode == "expired":
    at = datetime.now(timezone.utc) - timedelta(seconds=5)
    status["expirationTimestamp"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
if mode == "cert":
    status["clientCertificateData"] = open(os.path.join(here, "c.pem")).read()
    status["clientKeyData"] = open(os.path.join(here, "k.pem")).read()
else:
    status["token"] = "tok-%d" % runs
print(json.dumps({{"apiVersion": "{api}", "kind": "ExecCredential", "status": status}}))
"""


def make_plugin(directory: Path, *, api: str = V1, name: str = "fake-plugin") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(PLUGIN.format(python=sys.executable, api=api))
    path.chmod(0o700)
    return path


def runs(plugin: Path) -> int:
    counter = plugin.parent / "runs"
    return int(counter.read_text()) if counter.exists() else 0


def seen_env(plugin: Path) -> dict[str, str]:
    return json.loads((plugin.parent / "env.json").read_text())


def set_mode(plugin: Path, mode: str) -> None:
    (plugin.parent / "mode").write_text(mode)


def sha(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def exec_user(command: str, *, env: dict[str, str] | None = None, **extra) -> str:
    value = {"apiVersion": V1, "command": command, **extra}
    if env:
        value["env"] = [{"name": k, "value": v} for k, v in env.items()]
    return json.dumps(value)


def kubeconfig(path: Path, server: str, user: str) -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            current-context: ambient
            clusters:
            - name: c
              cluster: {{server: "{server}"}}
            users:
            - name: u
              user: {{exec: {user}}}
            contexts:
            - name: explicit
              context: {{cluster: c, user: u}}
            """
        )
    )
    return path


def target(path: Path, **kwargs) -> KubeconfigTarget:
    values = {
        "kubeconfig": path,
        "context": "explicit",
        "namespace": TARGET.namespace,
        "transport": "loopback-http",
    }
    values.update(kwargs)
    return KubeconfigTarget(**values)


def fixed_clock(start: datetime):
    now = [start]

    def clock() -> datetime:
        return now[0]

    return now, clock


# --------------------------------------------------------------- opt-in


def test_exec_user_is_refused_without_opt_in_and_plugin_never_runs(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    path = kubeconfig(tmp_path / "kc", "http://127.0.0.1:1", exec_user(str(plugin)))
    with pytest.raises(ExecAuthError) as caught:
        build_provider(target(path), field_manager="m", owner_id="o")
    assert caught.value.code == "exec-auth-not-allowed"
    with pytest.raises(ExecAuthError):
        api_client_from_kubeconfig(path, "explicit")
    assert runs(plugin) == 0


def test_auth_provider_is_refused_even_with_opt_in(tmp_path):
    path = tmp_path / "kc"
    kubeconfig(path, "https://127.0.0.1:1", "{}")
    path.write_text(
        path.read_text().replace(
            "user: {exec: {}}", "user: {auth-provider: {name: oidc}}"
        )
    )
    with pytest.raises(ExecAuthError) as caught:
        api_client_from_kubeconfig(path, "explicit", exec_policy=ExecPolicy(True))
    assert caught.value.code == "auth-provider-refused"


def test_policy_fields_require_opt_in():
    with pytest.raises(ValueError, match="allow_exec"):
        ExecPolicy(False, sha256="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="allow_exec"):
        ExecPolicy(False, pass_env=("AWS_PROFILE",))
    with pytest.raises(ValueError, match="variable names"):
        ExecPolicy(True, pass_env=("KUBERNETES_EXEC_INFO",))
    with pytest.raises(ProviderFactoryError, match="allow_exec"):
        KubeconfigTarget(Path("kc"), "c", "ns", exec_pass_env=("AWS_PROFILE",))


def test_exec_pin_on_static_user_is_refused(tmp_path):
    path = kubeconfig(tmp_path / "kc", "https://127.0.0.1:1", "{}")
    path.write_text(path.read_text().replace("user: {exec: {}}", "user: {token: t}"))
    policy = ExecPolicy(True, sha256="sha256:" + "0" * 64)
    with pytest.raises(ExecAuthError) as caught:
        api_client_from_kubeconfig(path, "explicit", exec_policy=policy)
    assert caught.value.code == "exec-config-invalid"


# -------------------------------------------------------------- pinning


def test_command_resolves_to_real_path_and_is_pinned(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    (link_dir / "gke-auth").symlink_to(plugin)
    resolved = resolve_plugin(
        {"apiVersion": V1, "command": "gke-auth"},
        kubeconfig_dir=tmp_path,
        policy=ExecPolicy(True),
        environ={"PATH": str(link_dir)},
    )
    assert resolved.command == plugin.resolve()
    assert resolved.sha256 == sha(plugin)
    assert resolved.summary() == {
        "command": str(plugin.resolve()),
        "sha256": sha(plugin),
        "api_version": V1,
    }
    relative = resolve_plugin(
        {"apiVersion": V1, "command": "bin/fake-plugin"},
        kubeconfig_dir=tmp_path,
        policy=ExecPolicy(True),
        environ={},
    )
    assert relative.command == plugin.resolve()


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"apiVersion": V1, "command": "no-such-plugin"}, "exec-command-not-found"),
        ({"apiVersion": V1, "command": "./absent"}, "exec-command-not-found"),
        (
            {"apiVersion": "client.authentication.k8s.io/v1alpha1", "command": "x"},
            "exec-config-invalid",
        ),
        ({"apiVersion": V1}, "exec-config-invalid"),
        ({"apiVersion": V1, "command": "x", "args": [1]}, "exec-config-invalid"),
        (
            {"apiVersion": V1, "command": "x", "env": [{"name": "A"}]},
            "exec-config-invalid",
        ),
        ({"apiVersion": V1, "command": "x", "shell": "sh"}, "exec-config-invalid"),
        (
            {"apiVersion": V1, "command": "x", "interactiveMode": "Always"},
            "exec-interactive-required",
        ),
    ],
)
def test_invalid_exec_blocks_are_refused(tmp_path, config, code):
    with pytest.raises(ExecAuthError) as caught:
        resolve_plugin(
            config,
            kubeconfig_dir=tmp_path,
            policy=ExecPolicy(True),
            environ={"PATH": str(tmp_path)},
        )
    assert caught.value.code == code
    assert code in ERRORS


def test_writable_command_is_unsafe(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    plugin.chmod(0o777)
    with pytest.raises(ExecAuthError) as caught:
        resolve_plugin(
            {"apiVersion": V1, "command": str(plugin)},
            kubeconfig_dir=tmp_path,
            policy=ExecPolicy(True),
        )
    assert caught.value.code == "exec-command-unsafe"


def test_spec_pin_mismatch_is_refused_before_running(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    path = kubeconfig(tmp_path / "kc", "http://127.0.0.1:1", exec_user(str(plugin)))
    with pytest.raises(ExecAuthError) as caught:
        build_provider(
            target(path, allow_exec=True, exec_sha256="sha256:" + "0" * 64),
            field_manager="m",
            owner_id="o",
        )
    assert caught.value.code == "exec-pin-mismatch"
    assert sha(plugin) in str(caught.value)
    assert runs(plugin) == 0


def test_binary_replaced_after_pinning_is_refused_on_refresh(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    now, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock, expires_in=600)
    plugin.write_text(plugin.read_text() + "\n# changed\n")
    now[0] += timedelta(seconds=599)
    with pytest.raises(ExecAuthError) as caught:
        source.ensure_fresh()
    assert caught.value.code == "exec-pin-mismatch"
    assert runs(plugin) == 1


# ---------------------------------------------------------- environment


def test_plugin_environment_is_minimal_and_explicit(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path / "bin")
    monkeypatch.setenv("AMBIENT_SECRET", "must-not-leak")
    monkeypatch.setenv("AWS_PROFILE", "shop")
    monkeypatch.setenv("KUBECONFIG", "/somewhere/else")
    monkeypatch.setenv("HOME", str(tmp_path))
    path = kubeconfig(
        tmp_path / "kc",
        "https://127.0.0.1:1",
        exec_user(str(plugin), env={"PLUGIN_REGION": "eu"}, provideClusterInfo=True),
    )
    client = api_client_from_kubeconfig(
        path, "explicit", exec_policy=ExecPolicy(True, pass_env=("AWS_PROFILE",))
    )
    client.close()
    env = seen_env(plugin)
    # LC_CTYPE (PEP 538 locale coercion) and __CF_USER_TEXT_ENCODING (macOS)
    # are added by the fake plugin's own interpreter, not passed by Piceli.
    assert set(env) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
        "PATH",
        "HOME",
        "LANG",
        "AWS_PROFILE",
        "PLUGIN_REGION",
        "KUBERNETES_EXEC_INFO",
    }
    assert env["AWS_PROFILE"] == "shop"
    assert env["HOME"] == str(tmp_path)
    info = json.loads(env["KUBERNETES_EXEC_INFO"])
    assert info == {
        "apiVersion": V1,
        "kind": "ExecCredential",
        "spec": {"interactive": False, "cluster": {"server": "https://127.0.0.1:1"}},
    }


# ------------------------------------------------------ plugin outcomes


def _attached(tmp_path, plugin, clock, *, expires_in=0.0, api_client=None):
    from kubernetes.client import ApiClient, Configuration

    configuration = Configuration()
    configuration.host = "http://127.0.0.1:1"
    client = api_client or ApiClient(configuration)
    config = {"apiVersion": V1, "command": str(plugin)}
    if expires_in:
        config["env"] = [{"name": "FAKE_EXPIRES_IN", "value": str(expires_in)}]
    resolved = resolve_plugin(
        config, kubeconfig_dir=tmp_path, policy=ExecPolicy(True, timeout_seconds=5)
    )
    environ = {"PATH": os.environ.get("PATH", ""), "FAKE_UNLISTED": "dropped"}
    return ExecCredentialSource.attach(
        client,
        resolved,
        ExecPolicy(True, timeout_seconds=2),
        cluster=ClusterInfo("http://127.0.0.1:1"),
        environ=environ,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("fail", "exec-plugin-failed"),
        ("hang", "exec-plugin-timed-out"),
        ("garbage", "exec-credential-invalid"),
        ("expired", "exec-credential-invalid"),
    ],
)
def test_plugin_failures_have_codes(tmp_path, mode, code):
    plugin = make_plugin(tmp_path / "bin")
    set_mode(plugin, mode)
    _, clock = fixed_clock(datetime.now(UTC))
    with pytest.raises(ExecAuthError) as caught:
        _attached(tmp_path, plugin, clock)
    assert caught.value.code == code
    assert ERRORS[code].area == "target"


def test_credential_output_is_validated_without_quoting_it(tmp_path):
    plugin = resolve_plugin(
        {"apiVersion": V1, "command": str(make_plugin(tmp_path / "bin"))},
        kubeconfig_dir=tmp_path,
        policy=ExecPolicy(True),
    )
    now = datetime.now(UTC)

    def parse(status, **top):
        document = {"apiVersion": V1, "kind": "ExecCredential", "status": status}
        document.update(top)
        return parse_credential(json.dumps(document).encode(), plugin, now)

    for bad in (
        {"token": "secret-value\nX"},
        {},
        {"clientCertificateData": "secret-value"},
        {"token": "secret-value", "expirationTimestamp": "tomorrow"},
        {"token": "secret-value", "expirationTimestamp": "2020-01-01T00:00:00"},
    ):
        with pytest.raises(ExecAuthError) as caught:
            parse(bad)
        assert caught.value.code == "exec-credential-invalid"
        assert "secret-value" not in str(caught.value)
    with pytest.raises(ExecAuthError):
        parse({"token": "t"}, apiVersion="client.authentication.k8s.io/v1beta1")
    credential = parse({"token": "secret-value"})
    assert credential.token == "secret-value"
    assert "secret-value" not in repr(credential)


# -------------------------------------------------------------- refresh


def test_token_refreshes_near_expiry_through_the_owned_hook(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    now, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock, expires_in=120)
    configuration = source.configuration
    assert configuration.api_key["BearerToken"] == "Bearer tok-1"
    assert configuration.get_api_key_with_prefix("BearerToken") == "Bearer tok-1"
    now[0] += timedelta(seconds=60)  # well before expiry - skew
    assert configuration.get_api_key_with_prefix("BearerToken") == "Bearer tok-1"
    assert runs(plugin) == 1
    now[0] += timedelta(seconds=31)  # within 30 s of expiry
    assert configuration.get_api_key_with_prefix("BearerToken") == "Bearer tok-2"
    assert runs(plugin) == 2


def test_token_without_expiry_is_reused(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    now, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock)
    now[0] += timedelta(days=1)
    source.ensure_fresh()
    assert runs(plugin) == 1


def test_provider_requests_carry_refreshed_tokens(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path / "bin")
    now, clock = fixed_clock(datetime.now(UTC))
    monkeypatch.setattr(ec, "_utc_now", clock)
    with serve() as (api, url):
        path = kubeconfig(
            tmp_path / "kc",
            url,
            exec_user(str(plugin), env={"FAKE_EXPIRES_IN": "600"}),
        )
        binding = build_provider(
            target(path, allow_exec=True, exec_sha256=sha(plugin)),
            field_manager="m",
            owner_id="o",
        )
        try:
            assert binding.credential_plugin == {
                "command": str(plugin.resolve()),
                "sha256": sha(plugin),
                "api_version": V1,
            }
            first = {r["authorization"] for r in api.requests}
            assert first == {"Bearer tok-1"}
            binding.provider.verify_target()
            assert api.requests[-1]["authorization"] == "Bearer tok-1"
            now[0] += timedelta(seconds=580)  # within 30 s of expiry
            binding.provider.verify_target()
            assert api.requests[-1]["authorization"] == "Bearer tok-2"
            assert runs(plugin) == 2
        finally:
            binding.close()


# ------------------------------------------------------ provider gating


def test_provider_accepts_only_factory_bound_exec_clients(tmp_path):
    from kubernetes.client import ApiClient, Configuration

    plugin = make_plugin(tmp_path / "bin")
    _, clock = fixed_clock(datetime.now(UTC))

    def provider(client):
        return KubernetesProvider(
            client,
            target=TARGET,
            field_manager="m",
            owner_id="o",
            source=EvidenceSource.LOOPBACK,
            cluster_uid="cluster-uid",
            namespace_uid="namespace-uid",
        )

    source = _attached(tmp_path, plugin, clock)
    client = ApiClient(source.configuration)
    client.rest_client.pool_manager = source.pool_manager
    assert approved_refresh(client)
    provider(client)

    # An arbitrary refresh hook (such as the SDK's own loaders install).
    other = Configuration()
    other.host = "http://127.0.0.1:1"
    other.refresh_api_key_hook = lambda configuration: None
    assert not approved_refresh(ApiClient(other))
    with pytest.raises(ValueError, match="explicit credentials"):
        provider(ApiClient(other))

    # A source's hook copied onto another client is not approved.
    copied = Configuration()
    copied.host = "http://127.0.0.1:1"
    copied.refresh_api_key_hook = source.configuration.refresh_api_key_hook
    with pytest.raises(ValueError, match="explicit credentials"):
        provider(ApiClient(copied))

    # The approved hook on a client with a different connection pool.
    swapped = ApiClient(source.configuration)
    assert not approved_refresh(swapped)
    with pytest.raises(ValueError, match="explicit credentials"):
        provider(swapped)

    # A source that was never attached (issued) is not approved.
    loose = ExecCredentialSource(
        source.plugin,
        source.policy,
        cluster=ClusterInfo("http://127.0.0.1:1"),
        ca_data=None,
        ca_file=None,
        environ={},
    )
    loose_config = Configuration()
    loose_config.host = "http://127.0.0.1:1"
    loose.configuration = loose_config
    loose_config.refresh_api_key_hook = loose._refresh_hook
    loose_client = ApiClient(loose_config)
    loose.pool_manager = loose_client.rest_client.pool_manager
    assert not approved_refresh(loose_client)


def test_proxy_stays_refused_for_exec_clients(tmp_path):
    plugin = make_plugin(tmp_path / "bin")
    _, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock)
    source.configuration.proxy = "http://proxy.test:3128"
    client = exec_client_from(source)
    with pytest.raises(ValueError, match="direct transport"):
        KubernetesProvider(
            client,
            target=TARGET,
            field_manager="m",
            owner_id="o",
            source=EvidenceSource.LOOPBACK,
            cluster_uid="c",
            namespace_uid="n",
        )


def exec_client_from(source):
    from kubernetes.client import ApiClient

    client = ApiClient(source.configuration)
    client.rest_client.pool_manager = source.pool_manager
    return client


# ------------------------------------------------ client certificates


@pytest.fixture
def key_pair(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not available")
    directory = tmp_path / "bin"
    directory.mkdir()
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=exec-test",
            "-days",
            "1",
            "-keyout",
            str(directory / "k.pem"),
            "-out",
            str(directory / "c.pem"),
        ],
        check=True,
        capture_output=True,
    )
    return directory


def test_client_certificates_are_loaded_without_temporary_files(
    tmp_path, key_pair, monkeypatch
):
    plugin = make_plugin(key_pair)
    set_mode(plugin, "cert")

    def no_temp_files(*args, **kwargs):
        raise AssertionError("credentials must not be written to disk")

    monkeypatch.setattr(tempfile, "mkstemp", no_temp_files)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_temp_files)
    _, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock)
    context = source.pool_manager.connection_pool_kw["ssl_context"]
    assert context.verify_mode.name == "CERT_REQUIRED"
    assert "BearerToken" not in source.configuration.api_key


def test_invalid_client_certificate_is_refused(tmp_path, key_pair):
    (key_pair / "k.pem").write_text(
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
    )
    plugin = make_plugin(key_pair)
    set_mode(plugin, "cert")
    _, clock = fixed_clock(datetime.now(UTC))
    with pytest.raises(ExecAuthError) as caught:
        _attached(tmp_path, plugin, clock)
    assert caught.value.code == "exec-credential-invalid"


def test_rotated_certificate_replaces_the_tls_context(tmp_path, key_pair):
    plugin = make_plugin(key_pair)
    set_mode(plugin, "cert")
    now, clock = fixed_clock(datetime.now(UTC))
    source = _attached(tmp_path, plugin, clock, expires_in=1000)
    before = source.pool_manager.connection_pool_kw["ssl_context"]
    now[0] += timedelta(seconds=980)  # within 30 s of expiry
    source.ensure_fresh()
    assert runs(plugin) == 2
    # Same PEM again: the context is kept.
    assert source.pool_manager.connection_pool_kw["ssl_context"] is before
    (key_pair / "c.pem").write_text((key_pair / "c.pem").read_text() + "\n")
    now[0] += timedelta(seconds=15)  # lifetime is now 20 s, skew 10 s
    source.ensure_fresh()
    assert runs(plugin) == 3
    assert source.pool_manager.connection_pool_kw["ssl_context"] is not before


# ------------------------------------------------------------ [target]


def _spec(tmp_path, **target_keys):
    from piceli.k8s.release_spec import ReleaseSpec

    return ReleaseSpec.from_dict(
        {
            "target": {
                "kubeconfig": "kc",
                "context": "ctx",
                "namespace": "demo",
                **target_keys,
            },
            "release": {
                "name": "web",
                "owner": "owner",
                "field_manager": "manager",
                "composition": "compose.py:build",
                "state_dir": "state",
            },
            "images": {"web": "docker.io/library/nginx@sha256:" + "a" * 64},
        },
        tmp_path,
    )


def test_target_exec_keys_reach_the_factory(tmp_path):
    pin = "sha256:" + "c" * 64
    target = _spec(
        tmp_path,
        allow_exec=True,
        exec_sha256=pin,
        exec_pass_env=["AWS_PROFILE"],
        exec_timeout_seconds=20,
    ).kubeconfig_target()
    assert target.exec_policy == ExecPolicy(True, pin, ("AWS_PROFILE",), 20)
    assert _spec(tmp_path).kubeconfig_target().exec_policy == ExecPolicy()


@pytest.mark.parametrize(
    "keys",
    [
        {"exec_sha256": "sha256:" + "c" * 64},
        {"exec_pass_env": ["AWS_PROFILE"]},
        {"exec_timeout_seconds": 5},
        {"allow_exec": True, "exec_sha256": "c" * 64},
        {"allow_exec": True, "exec_pass_env": ["BAD NAME"]},
        {"allow_exec": True, "exec_timeout_seconds": 301},
        {"allow_exec": "yes"},
    ],
)
def test_target_exec_keys_are_validated(tmp_path, keys):
    from piceli.k8s.release_spec import ReleaseSpecError

    with pytest.raises(ReleaseSpecError):
        _spec(tmp_path, **keys)
