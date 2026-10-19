"""Acceptance: generated Secrets adopted back from the cluster into a new state.

A release state that starts empty (a new state directory, a GitOps controller
on another node) finds the retained Secrets an earlier release wrote. Their
generated values are adopted: recorded as if generated, shown in the plan,
bound to its hash, never rotated. Secrets without this release's ownership
markers are refused as before.
"""

# ruff: noqa: F811  (tests take the imported ``release_env`` fixture)

from __future__ import annotations

import base64
import json
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.fake_api import manifest
from tests.acceptance.test_release_cli import (  # noqa: F401 (pytest fixture)
    DIGEST_2,
    _receipt,
    _run,
    release_env,
)
from tests.acceptance.test_release_secrets_cli import _secret_show, _versions

OPENSSL = Path("/usr/bin/openssl")
needs_openssl = pytest.mark.skipif(not OPENSSL.exists(), reason="needs openssl")


def _password(api: Any) -> str:
    return str(api.objects[("Secret", "credential")]["data"]["password"])


def _fresh_state(tmp_path: Path) -> None:
    """What a controller on a new node starts with: no state at all."""
    shutil.rmtree(tmp_path / "state")


def _operations(planned: dict[str, Any]) -> dict[str, str]:
    return {item["name"]: item["operation"] for item in planned["actions"]}


def _live_credential(api: Any, password: str, **put: Any) -> None:
    value = base64.b64encode(password.encode()).decode()
    api.put(manifest("Secret", "credential", value=value), **put)


def test_fresh_state_adopts_the_live_generated_secret(release_env):
    api, tmp_path = release_env
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    password = _password(api)
    version = api.objects[("Secret", "credential")]["metadata"]["resourceVersion"]
    _fresh_state(tmp_path)

    out = tmp_path / "plan.json"
    code, planned, result = _run(tmp_path, "plan", "--out", str(out))
    assert code == 0, result.output
    assert planned["secrets"] == {"password": "adopted"}
    assert planned["secret_adoptions"] == [
        {"secret": "password", "from": ["credential"]}
    ]
    # The adopted value is the live one: the Secret is not written.
    assert _operations(planned)["credential"] == "no-op"
    assert (
        "secret password: adopted from Secret credential (kept, not rotated)"
        in result.stderr
    )
    # The adoption is part of what the hash approves.
    full = json.loads(out.read_text())
    assert full["plan_hash"] == planned["plan_hash"]
    assert full["plan"]["secret_adoptions"] == [
        {
            "generator": "password",
            "resource": {
                "api_version": "v1",
                "kind": "Secret",
                "namespace": "piceli-test",
                "name": "credential",
            },
            "uid": api.objects[("Secret", "credential")]["metadata"]["uid"],
        }
    ]

    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert applied["execution"]["state"] == "ready"
    assert _password(api) == password
    live = api.objects[("Secret", "credential")]["metadata"]["resourceVersion"]
    assert live == version
    code, result = _secret_show(tmp_path, "password", "--reveal")
    assert code == 0, result.output
    assert result.stdout.encode() == base64.b64decode(password)
    code, result = _secret_show(tmp_path, "password", "--json")
    meta = json.loads(result.stdout)
    assert meta["origin"] == "adopted" and meta["first_origin"] == "adopted"

    # A later release carries the adopted value like any other: no
    # re-adoption, no rotation.
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    adopted_release = applied["release"]
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["secrets"] == {"password": f"carried:{adopted_release}"}
    assert planned["secret_adoptions"] == []
    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert _password(api) == password


def test_an_unlabelled_secret_is_refused_as_before(release_env):
    api, tmp_path = release_env
    _live_credential(api, secrets.token_urlsafe(24))
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "resource-requires-adoption"
    assert "secret_adoptions" not in refused
    assert _versions(tmp_path) == 0


def test_a_secret_of_another_owner_is_refused_as_before(release_env):
    api, tmp_path = release_env
    _live_credential(api, secrets.token_urlsafe(24))
    api.objects[("Secret", "credential")]["metadata"]["annotations"] = {
        "piceli.io/owner": "another-app"
    }
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "resource-requires-adoption"
    assert _versions(tmp_path) == 0


def test_a_secret_written_by_another_field_manager_is_not_adopted(release_env):
    """Same owner annotation, but not written by this release's field manager
    (another environment or tool): generated as before, and the retained
    Secret refuses the new value at apply."""
    api, tmp_path = release_env
    live = secrets.token_urlsafe(24)
    _live_credential(
        api,
        live,
        owned=True,
        managers=[("another-environment", "Apply", {"data": {"password": "x"}})],
    )
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["secrets"] == {"password": "generated"}
    assert planned["secret_adoptions"] == []
    code, failed, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code != 0
    assert failed["reason"] == "retained-content-precondition-failed"
    assert base64.b64decode(_password(api)).decode() == live


def test_a_live_secret_with_another_key_set_is_refused(release_env):
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    api.objects[("Secret", "credential")]["data"]["extra"] = "eA=="
    _fresh_state(tmp_path)
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "secret-adoption-invalid"
    assert "'extra'" in refused["message"]
    assert _versions(tmp_path) == 0


def test_a_value_the_generator_cannot_make_is_refused(release_env):
    api, tmp_path = release_env
    # random(24) makes 32 URL-safe characters; this is 12.
    _live_credential(api, "not-a-random", owned=True)
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "secret-adoption-invalid"
    assert "not-a-random" not in result.output  # never the value
    assert _versions(tmp_path) == 0


def test_a_rotation_is_never_replaced_by_an_adoption(release_env):
    api, tmp_path = release_env
    _live_credential(api, secrets.token_urlsafe(24), owned=True)
    code, planned, result = _run(tmp_path, "plan", "--rotate", "password")
    assert code == 0, result.output
    assert planned["secrets"] == {"password": "rotated"}
    assert planned["secret_adoptions"] == []


TLS_COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    keys = {
        "password": "password",
        "ca.crt": "ca.ca.crt",
        "tls.crt": "ca.turn.crt",
        "tls.key": "ca.turn.key",
    }
    keys.update(ctx.values.get("extra_keys", {}))
    split = ctx.values.get("leaf_secret")
    leaf = {key: keys.pop(key) for key in ("tls.crt", "tls.key")} if split else {}

    def secret(name, keys):
        value = ResourceIntent.from_manifest(
            {"apiVersion": "v1", "kind": "Secret", "metadata": meta(name),
             "data": {key: "<private>" for key in keys}}
        )
        for key, output in keys.items():
            value = value.with_secret("/data/" + key, ctx.secret(output))
        return value

    secrets = (secret("turn-auth", keys),) + ((secret(split, leaf),) if split else ())
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api")}]}}}}
    )
    return DeploymentComposition((
        DeploymentComponent("secrets", secrets),
        DeploymentComponent("worker", (worker,), dependencies=("secrets",)),
    ))
"""

TLS_SECRETS = """
[secrets.password]
type = "random"
bytes = 24

[secrets.ca]
type = "tls-ca"
days = 30
ca_days = 60
leaves = { turn = ["turn", "turn.piceli-test.svc"] }

[values]
mode = "blue"
"""


def _configure_tls(tmp_path: Path) -> None:
    (tmp_path / "compose.py").write_text(TLS_COMPOSITION)
    spec = tmp_path / "release.toml"
    text = spec.read_text()
    head = text[: text.index("[secrets.password]")]
    spec.write_text(head + TLS_SECRETS.lstrip())


@needs_openssl
def test_a_private_ca_and_its_leaf_are_adopted_without_the_ca_key(release_env):
    api, tmp_path = release_env
    _configure_tls(tmp_path)
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    before = dict(api.objects[("Secret", "turn-auth")]["data"])
    _fresh_state(tmp_path)

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["secrets"] == {"ca": "adopted", "password": "adopted"}
    assert planned["secret_adoptions"] == [
        {"secret": "ca", "from": ["turn-auth"]},
        {"secret": "password", "from": ["turn-auth"]},
    ]
    assert _operations(planned)["turn-auth"] == "no-op"
    assert planned["dry_run_unavailable"] == []
    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert api.objects[("Secret", "turn-auth")]["data"] == before
    # The leaf still verifies against the kept CA certificate.
    certificate = tmp_path / "ca.pem"
    certificate.write_bytes(base64.b64decode(before["ca.crt"]))
    leaf = tmp_path / "leaf.pem"
    leaf.write_bytes(base64.b64decode(before["tls.crt"]))
    subprocess.run(
        [str(OPENSSL), "verify", "-CAfile", str(certificate), str(leaf)],
        check=True,
        capture_output=True,
    )
    # The CA key was never in a Secret, so it was not adopted.
    code, result = _secret_show(tmp_path, "ca", "--reveal", "--key", "ca.key")
    assert code == 2 and json.loads(result.stdout)["reason"] == "secret-not-found"

    # A later release carries the adopted CA as it is.
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["secrets"]["ca"] == f"carried:{applied['release']}"
    code, _, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert api.objects[("Secret", "turn-auth")]["data"] == before

    # A new leaf needs the CA key: refused with a code, nothing generated.
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text()
        .replace(
            'leaves = { turn = ["turn", "turn.piceli-test.svc"] }',
            'leaves = { turn = ["turn", "turn.piceli-test.svc"], api = ["api"] }',
        )
        .replace(
            'mode = "blue"',
            'mode = "blue"\nextra_keys = { "api.crt" = "ca.api.crt", '
            '"api.key" = "ca.api.key" }',
        )
    )
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "secret-adoption-incomplete", refused


@needs_openssl
def test_a_ca_partly_live_is_refused(release_env):
    """The CA certificate is live but its leaf is not (bound in a Secret that
    does not exist yet): keeping the CA would need its key to sign the leaf."""
    api, tmp_path = release_env
    _configure_tls(tmp_path)
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    _fresh_state(tmp_path)
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            'mode = "blue"', 'mode = "blue"\nleaf_secret = "turn-tls"'
        )
    )
    data = dict(api.objects[("Secret", "turn-auth")]["data"])
    del data["tls.crt"], data["tls.key"]
    api.put(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "turn-auth", "namespace": "piceli-test"},
            "data": data,
        },
        owned=True,
    )
    code, refused, result = _run(tmp_path, "plan")
    assert code == 2, result.output
    assert refused["reason"] == "secret-adoption-incomplete", refused
    assert "ca.turn.crt" in refused["message"]
    assert _versions(tmp_path) == 0


# ------------------------------------------------------------ piceli deploy
def test_deploy_with_an_empty_state_dir_adopts_the_generated_secret(shop) -> None:
    from tests.acceptance.test_deploy_pipeline import deploy

    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    password = api.objects[("Secret", "credentials")]["data"]["password"]
    shutil.rmtree(tmp_path / "state")

    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    # The image is built again (its receipt was in the state), so the plan is
    # a preview; it shows the adoption and the combined hash covers it.
    stage = events[-1]["stages"]["plan"]
    assert stage["state"] == "pending"
    assert stage["preview"]["secret_adoptions"] == [
        {"secret": "password", "from": ["credentials"]}
    ]
    combined = events[-1]["combined_hash"]
    code, events, result = deploy(tmp_path, "--approve", combined, "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert "[plan] secret password: adopted from Secret credentials" in result.stderr
    assert api.objects[("Secret", "credentials")]["data"]["password"] == password
    run = sorted((tmp_path / "state" / "runs").glob("*.json"))[-1]
    planned = json.loads(run.read_text())["stages"]["plan"]["output"]
    assert planned["secret_adoptions"] == [
        {"secret": "password", "from": ["credentials"]}
    ]


# ------------------------------------------------------------ GitOps controller
CONTROLLER_PIPELINE = """
from piceli import (
    App, Build, EnvConfig, NodeLoopbackRegistry, Pipeline, Random, Secrets, Target,
)

images = Build.spec("build.toml")
app = App("shop")
secrets = Secrets(password=Random(24))
credentials = app.secret("credentials", {"password": secrets.ref("password")})
app.deployment("api", image=images["api"], ports=[8080],
               env={"PASSWORD": credentials.key("password")})

pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="author", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    secrets=secrets,
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
    envs=EnvConfig(prefix="shop-", branches=["wp-*"], auto_approve=True),
)
"""


def test_a_controller_with_a_new_state_dir_keeps_the_generated_secrets(
    tmp_path: Path,
) -> None:
    """The controller moved to another node: its state starts empty, and its
    first deploy adopts the Secrets the previous controller generated."""
    import textwrap

    from piceli.gitops.config import ControllerConfig
    from piceli.gitops.controller import Controller
    from piceli.gitops.ports import DefaultPorts
    from piceli.gitops.repo import GitRemote
    from piceli.gitops.state import DirectoryChannel
    from piceli.k8s.cli.env_push import configmap_name
    from piceli.testing import TYPES, FakeAPI, serve, write_kubeconfig
    from tests.unit.test_gitops_controller import Repo

    branch, namespace = "wp-login", "shop-wp-login"
    repo = Repo(tmp_path)
    repo.git("checkout", "--quiet", "-B", branch, "main")
    (repo.work / "deploy" / "app.py").write_text(textwrap.dedent(CONTROLLER_PIPELINE))
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", "pipeline")
    sha = repo.git("rev-parse", "HEAD")
    repo.git("push", "--quiet", "origin", branch)

    api = FakeAPI(
        types={**TYPES, "resourcequotas": ("v1", "ResourceQuota", True)},
        namespace=namespace,
    )
    del api.objects[("Namespace", namespace)]
    api.add_node("node-a")
    pushed = manifest("ConfigMap", configmap_name(branch))
    pushed["data"] = {
        "images": json.dumps({"api": {"digest": "sha256:" + "5" * 64}}),
        "commit": sha,
    }
    api.put(pushed)
    with serve(api) as (api, url):
        kubeconfig = write_kubeconfig(
            url, tmp_path / "controller.kubeconfig", context="fake"
        )
        config = ControllerConfig(
            pipeline="deploy/app.py:pipeline",
            repo=str(repo.remote),
            branches=("main", "wp-*"),
        )

        def poll(state: Path) -> dict[str, Any]:
            controller = Controller(
                config,
                state_dir=state,
                source=GitRemote(config.repo, state / "mirror"),
                ports=DefaultPorts(
                    Path(kubeconfig),
                    "fake",
                    state,
                    namespace="piceli-system",
                    config=config,
                    transport="loopback-http",
                ),
                channel=DirectoryChannel(state),
            )
            return dict(controller.poll_once()["envs"][branch])

        entry = poll(tmp_path / "node-a-state")
        assert entry["state"] == "deployed", entry
        password = api.objects[("Secret", "credentials")]["data"]["password"]

        entry = poll(tmp_path / "node-b-state")
        assert entry["state"] == "deployed", entry
        assert api.objects[("Secret", "credentials")]["data"]["password"] == password
