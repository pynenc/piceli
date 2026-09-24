"""Acceptance: secret generators through ``piceli release`` against the fake API.

Covers imports from a live Secret, templates (a DSN and a config file), a
private CA with a leaf, carry-over, rotation, ``secret show`` and a refused
plan that must leave no secret versions behind.
"""

# ruff: noqa: F811  (tests take the imported ``release_env`` fixture)

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from tests.acceptance.fake_api import manifest
from tests.acceptance.test_release_cli import (  # noqa: F401 (pytest fixture)
    DIGEST_2,
    _receipt,
    _run,
    release_env,
)

OPENSSL = Path("/usr/bin/openssl")
needs_openssl = pytest.mark.skipif(not OPENSSL.exists(), reason="needs openssl")

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    keys = {
        "password": "cache_password",
        "url": "cache_url",
        "redis.conf": "cache_conf",
        "ca.crt": "ca.ca.crt",
        "tls.crt": "ca.cache.crt",
        "tls.key": "ca.cache.key",
    }
    secret = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret",
         "metadata": meta(f"cache-auth-{ctx.values['generation']}"),
         "data": {key: "<private>" for key in keys}}
    )
    for key, name in keys.items():
        secret = secret.with_secret("/data/" + key.replace("/", "~1"), ctx.secret(name))
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api")}]}}}}
    )
    return DeploymentComposition((
        DeploymentComponent("secrets", (secret,)),
        DeploymentComponent("worker", (worker,), dependencies=("secrets",)),
    ))
"""

SECRETS = '''
[secrets.cache_password]
type = "import"
secret = { name = "legacy-cache", key = "password" }

[secrets.cache_url]
type = "template"
template = "rediss://:{cache_password}@cache:6380/0"

[secrets.cache_conf]
type = "template"
template = """
tls-port 6380
requirepass {cache_password}
tls-ca-cert-file /run/private/ca.crt
"""

[secrets.ca]
type = "tls-ca"
days = 30
ca_days = 60
leaves = { cache = ["cache", "cache.piceli-test.svc"] }

[values]
generation = 1'''

LEGACY_PASSWORD = "existing-cache-password"


def _configure(tmp_path: Path, secrets: str = SECRETS) -> None:
    (tmp_path / "compose.py").write_text(COMPOSITION)
    spec = tmp_path / "release.toml"
    text = spec.read_text()
    old = '[secrets.password]\ntype = "random"\nbytes = 24\n\n[values]'
    assert old in text
    spec.write_text(text.replace(old, secrets.strip()))


def _legacy_secret(api) -> None:
    api.put(
        manifest(
            "Secret",
            "legacy-cache",
            value=base64.b64encode(LEGACY_PASSWORD.encode()).decode(),
        )
    )


def _data(api, key: str, generation: int = 1) -> str:
    stored = api.objects[("Secret", f"cache-auth-{generation}")]
    return base64.b64decode(stored["data"][key]).decode()


def _versions(tmp_path: Path) -> int:
    path = tmp_path / "state" / "secrets.sqlite"
    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def openssl_calls(monkeypatch):
    import piceli.k8s.release_secrets as module

    calls: list[tuple[str, ...]] = []
    real = module._openssl

    def recording(openssl, *arguments):
        calls.append(arguments)
        return real(openssl, *arguments)

    monkeypatch.setattr(module, "_openssl", recording)
    return calls


def _secret_show(tmp_path: Path, *args: str):
    result = CliRunner().invoke(
        app, ["secret", "show", *args, "--spec", str(tmp_path / "release.toml")]
    )
    return result.exit_code, result


# ------------------------------------------------------------ feedback 9
@needs_openssl
def test_refused_plan_creates_no_secret_versions_and_runs_no_openssl(
    release_env, openssl_calls
):
    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)
    api.put(manifest("Deployment", "worker"))  # unmanaged: needs adoption
    for _ in range(2):
        code, refused, _ = _run(tmp_path, "plan")
        assert code == 2
        assert "Deployment/worker" in refused["reason"]
        assert _versions(tmp_path) == 0
    assert openssl_calls == []


@needs_openssl
def test_plan_refused_by_the_planner_generates_nothing(
    release_env, openssl_calls, monkeypatch
):
    """A refusal found while building the plan comes before any generator."""
    import piceli.k8s.release_runner as runner

    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)

    def refuse(*_args, **_kwargs):
        raise ValueError("resource requires explicit adoption: Deployment/worker")

    monkeypatch.setattr(runner, "build_plan", refuse)
    for _ in range(2):
        code, refused, _ = _run(tmp_path, "plan")
        assert code == 2 and "explicit adoption" in refused["reason"]
    assert _versions(tmp_path) == 0
    assert openssl_calls == []
    assert not list((tmp_path / "state").glob("releases/*.json"))


@needs_openssl
def test_session_failure_after_generation_discards_its_versions(
    release_env, monkeypatch
):
    import piceli.k8s.ops.session as session

    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)

    def refuse(*_args, **_kwargs):
        raise ValueError("resource requires explicit adoption: Deployment/worker")

    monkeypatch.setattr(session, "build_plan", refuse)
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2
    assert _versions(tmp_path) == 0


# ------------------------------------------------------------ generators
@needs_openssl
def test_imported_password_template_files_and_ca_bundle_reach_the_cluster(
    release_env, tmp_path_factory
):
    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["secrets"] == {
        "ca": "generated",
        "cache_conf": "rendered",
        "cache_password": "imported",
        "cache_url": "rendered",
    }
    assert LEGACY_PASSWORD not in json.dumps(planned) + result.stderr
    code, applied, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    first = applied["release"]

    # A service with existing data keeps working: the same password.
    assert _data(api, "password") == LEGACY_PASSWORD
    assert _data(api, "url") == f"rediss://:{LEGACY_PASSWORD}@cache:6380/0"
    assert f"\nrequirepass {LEGACY_PASSWORD}\n" in _data(api, "redis.conf")
    # The CA bundle verifies the leaf; the CA key never leaves the store.
    work = tmp_path_factory.mktemp("pem")
    (work / "ca.crt").write_text(_data(api, "ca.crt"))
    (work / "tls.crt").write_text(_data(api, "tls.crt"))
    verified = subprocess.run(
        [
            str(OPENSSL),
            "verify",
            "-CAfile",
            str(work / "ca.crt"),
            str(work / "tls.crt"),
        ],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr
    assert "PRIVATE KEY" in _data(api, "tls.key")
    stored = json.dumps(api.objects[("Secret", "cache-auth-1")])
    catalog = (tmp_path / "state" / "catalog.json").read_text()
    assert LEGACY_PASSWORD not in catalog

    # The next release carries every value over, even with the source gone.
    del api.objects[("Secret", "legacy-cache")]
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, planned, _ = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["secrets"]["cache_password"] == f"carried:{first}"
    assert planned["secrets"]["ca"] == f"carried:{first}"
    code, applied, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert json.dumps(api.objects[("Secret", "cache-auth-1")]) == stored

    # Rotating the import generates a new value; templates follow it. A
    # rotated value goes into a new Secret (Secrets are retained objects).
    spec = tmp_path / "release.toml"
    spec.write_text(spec.read_text().replace("generation = 1", "generation = 2"))
    code, planned, _ = _run(tmp_path, "plan", "--rotate", "cache_password")
    assert code == 0, planned
    assert planned["secrets"]["cache_password"] == "rotated"
    code, applied, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    rotated = _data(api, "password", 2)
    assert rotated != LEGACY_PASSWORD and len(rotated) >= 32
    assert _data(api, "url", 2) == f"rediss://:{rotated}@cache:6380/0"
    assert (
        _data(api, "ca.crt", 2)
        == base64.b64decode(json.loads(stored)["data"]["ca.crt"]).decode()
    )


@needs_openssl
def test_rotating_a_template_is_refused(release_env):
    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)
    code, refused, _ = _run(tmp_path, "plan", "--rotate", "cache_url")
    assert code == 2
    assert refused["code"] == "secret-rotation-refused"
    assert _versions(tmp_path) == 0


def test_missing_import_source_is_refused_with_a_code(release_env):
    api, tmp_path = release_env
    _configure(
        tmp_path,
        """
[secrets.token]
type = "import"
secret = { name = "absent", key = "token" }

[values]
generation = 1""",
    )
    (tmp_path / "compose.py").write_text(
        (tmp_path / "compose.py")
        .read_text()
        .replace(
            """        "password": "cache_password",
        "url": "cache_url",
        "redis.conf": "cache_conf",
        "ca.crt": "ca.ca.crt",
        "tls.crt": "ca.cache.crt",
        "tls.key": "ca.cache.key",""",
            '        "token": "token",',
        )
    )
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2, refused
    assert refused["code"] == "secret-import-unavailable"
    assert "secret:absent/token" in refused["reason"]
    assert _versions(tmp_path) == 0


# ------------------------------------------------------------ secret show
@needs_openssl
def test_secret_show_reveals_only_on_request(release_env):
    api, tmp_path = release_env
    _configure(tmp_path)
    _legacy_secret(api)
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, applied

    code, result = _secret_show(tmp_path, "cache_password")
    assert code == 2  # no terminal, no --reveal
    assert json.loads(result.stdout)["code"] == "secret-reveal-required"
    assert LEGACY_PASSWORD not in result.output

    code, result = _secret_show(tmp_path, "cache_password", "--json")
    assert code == 0
    metadata = json.loads(result.stdout)
    assert LEGACY_PASSWORD not in result.output
    assert metadata["type"] == "import"
    assert metadata["source"] == "secret:legacy-cache/password"
    assert metadata["origin"] == metadata["first_origin"] == "imported"
    assert metadata["first_release"] == applied["release"]
    assert "values" not in metadata

    code, result = _secret_show(tmp_path, "cache_password", "--reveal")
    assert code == 0
    assert result.stdout == LEGACY_PASSWORD
    assert LEGACY_PASSWORD not in result.stderr

    code, result = _secret_show(tmp_path, "ca", "--reveal")
    assert code == 2
    assert json.loads(result.stdout)["code"] == "secret-key-required"
    code, result = _secret_show(tmp_path, "ca", "--reveal", "--key", "ca.crt")
    assert code == 0
    assert result.stdout == _data(api, "ca.crt")
    code, result = _secret_show(tmp_path, "ca", "--json", "--reveal")
    assert code == 0
    revealed = json.loads(result.stdout)
    assert sorted(revealed["values"]) == ["ca.crt", "ca.key", "cache.crt", "cache.key"]
    assert revealed["internal"] == ["ca.ca.key"]

    code, result = _secret_show(tmp_path, "nope", "--json")
    assert code == 2
    assert json.loads(result.stdout)["code"] == "secret-not-found"
