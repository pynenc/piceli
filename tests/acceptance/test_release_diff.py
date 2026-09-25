"""Acceptance: field-level diffs and true no-op plans (server dry runs).

The fake API models server defaulting (``server_defaults = True``): stored
objects gain defaulted fields, canonical quantities, an allocated clusterIP and
controller bookkeeping, so a live object never equals the manifest that
created it, as on a real cluster.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from tests.acceptance.fake_api import TARGET, serve

DIGEST_1 = "sha256:" + "1" * 64
DIGEST_2 = "sha256:" + "2" * 64
SECRET_VALUE_MARKER = "server-password-do-not-publish"

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    config = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("settings"),
         "data": {"mode": "blue"}}
    )
    token = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret", "metadata": meta("credential"),
         "data": {"password": "<private>"}}
    ).with_secret("/data/password", ctx.secret("password"))
    claim = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta("data"),
         "spec": {"accessModes": ["ReadWriteOnce"],
                  "resources": {"requests": {"storage": "1Gi"}}}}
    )
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api"),
                                    "resources": {"requests": {"cpu": "0.5"}}}],
                                        "volumes": [{"name": "data",
                                                     "persistentVolumeClaim":
                                                         {"claimName": "data"}}]}}}}
    )
    service = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Service", "metadata": meta("worker"),
         "spec": {"selector": {"app": "worker"}, "ports": [{"port": 80}]}}
    )
    return DeploymentComposition((
        DeploymentComponent("config", (config, token, claim)),
        DeploymentComponent("worker", (worker, service), dependencies=("config",)),
    ))
"""


def _receipt(path: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "revision": "piceli.build-receipt.v1",
                "status": "passed",
                "outputs": {
                    "images": {
                        "api": {
                            "image_id": "sha256:" + "a" * 64,
                            "digest": digest,
                            "platform": "linux/amd64",
                            "ref": "registry.example/app/api:build-7",
                        }
                    }
                },
            }
        )
    )


@pytest.fixture
def release_env(tmp_path):
    with serve() as (api, url):
        api.server_defaults = True
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters:
                - name: fake
                  cluster: {{server: "{url}"}}
                users:
                - name: nobody
                  user: {{}}
                contexts:
                - name: fake
                  context: {{cluster: fake, user: nobody}}
                - name: must-not-be-used
                  context: {{cluster: fake, user: nobody}}
                """
            )
        )
        (tmp_path / "compose.py").write_text(COMPOSITION)
        _receipt(tmp_path / "build.receipt.json", DIGEST_1)
        (tmp_path / "release.toml").write_text(
            textwrap.dedent(
                f"""
                images_from = "build.receipt.json"

                [target]
                kubeconfig = "kubeconfig"
                context = "fake"
                namespace = "{TARGET.namespace}"
                cluster_uid = "cluster-uid"
                transport = "loopback-http"

                [release]
                name = "app"
                owner = "acceptance-owner"
                field_manager = "piceli-acceptance"
                composition = "compose.py:build"
                state_dir = "state"

                [execution]
                max_seconds = 30
                readiness_seconds = 1
                poll_seconds = 0.05

                [secrets.password]
                type = "random"
                bytes = 24
                """
            )
        )
        yield api, tmp_path


def _run(tmp_path: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(tmp_path / "release.toml")])
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result


def _operations(payload) -> dict[str, str]:
    return {
        f"{item['kind']}/{item['name']}": item["operation"]
        for item in payload["actions"]
    }


def _versions(api) -> dict[tuple[str, str], str]:
    return {
        key: value["metadata"]["resourceVersion"] for key, value in api.objects.items()
    }


def _dry_runs(api) -> list[dict]:
    return [
        request
        for request in api.requests
        if request["method"] == "PATCH" and request["query"].get("dryRun") == ["All"]
    ]


def test_unchanged_release_plans_all_noop_despite_server_defaults(release_env):
    api, tmp_path = release_env
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    live = api.objects[("Deployment", "worker")]["spec"]
    # The problem this reproduces: the live object carries server defaults.
    assert live["revisionHistoryLimit"] == 10
    assert live["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "500m"}
    }
    before = _versions(api)
    api.requests.clear()

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["mode"] == "reapply"
    assert _operations(planned) == {
        "ConfigMap/settings": "no-op",
        "PersistentVolumeClaim/data": "no-op",
        # The bound Secret is compared privately (in-process, keyed digests).
        "Secret/credential": "no-op",
        "Deployment/worker": "no-op",
        "Service/worker": "no-op",
    }
    assert planned["dry_run_unavailable"] == []
    assert planned["diffs"] == []
    password = api.objects[("Secret", "credential")]["data"]["password"]
    assert password not in json.dumps(planned) + result.stderr

    # One dry run per managed, comparable object, all persisted nothing.
    probes = _dry_runs(api)
    assert sorted(request["path"].rsplit("/", 2)[1] for request in probes) == [
        "configmaps",
        "deployments",
        "persistentvolumeclaims",
        "services",
    ]
    for request in probes:
        assert request["content_type"] == "application/merge-patch+json"
        assert request["query"]["fieldManager"] == ["piceli-acceptance"]
        assert "force" not in request["query"]
    assert not [
        request
        for request in api.requests
        if request["method"] != "GET" and request["query"].get("dryRun") != ["All"]
    ]
    assert _versions(api) == before

    # The stored evidence rebuilds the same plan hash, and the no-ops pass the
    # executor's content check although cpu "0.5" is stored as "500m".
    code, outcome, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert outcome["execution"]["state"] == "ready"
    after = _versions(api)
    for key in (
        ("ConfigMap", "settings"),
        ("Secret", "credential"),
        ("Deployment", "worker"),
        ("Service", "worker"),
        ("PersistentVolumeClaim", "data"),
    ):
        assert after[key] == before[key], key


def test_changed_image_diff_shows_exactly_that_field(release_env):
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    api.requests.clear()

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert planned["mode"] == "create"
    (probe,) = [
        request
        for request in _dry_runs(api)
        if request["path"].endswith("/deployments/worker")
    ]
    assert _operations(planned) == {
        "ConfigMap/settings": "no-op",
        "PersistentVolumeClaim/data": "no-op",
        # A new release carries the value over, so its Secret is unchanged.
        "Secret/credential": "no-op",
        "Deployment/worker": "apply",
        "Service/worker": "no-op",
    }
    diffs = {item["resource"]["kind"]: item for item in planned["diffs"]}
    assert set(diffs) == {"Deployment"}
    worker = diffs["Deployment"]
    assert worker["basis"] == "server-dry-run"
    assert worker["changes"] == [
        {
            "path": "/spec/template/spec/containers/0/image",
            "op": "replace",
            "before": f"registry.example/app/api@{DIGEST_1}",
            "after": f"registry.example/app/api@{DIGEST_2}",
        }
    ]
    assert f"-      - image: registry.example/app/api@{DIGEST_1}" in (worker["unified"])
    assert f"+      - image: registry.example/app/api@{DIGEST_2}" in (worker["unified"])
    assert "~ /spec/template/spec/containers/0/image" in result.stderr
    # Secret values never appear, not even as "before" values.
    password = api.objects[("Secret", "credential")]["data"]["password"]
    assert password not in result.stdout + result.stderr

    code, outcome, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert outcome["execution"]["state"] == "ready"
    # The dry run was exactly the write the executor then sent.
    (write,) = [
        request
        for request in api.requests
        if request["method"] == "PATCH"
        and request["path"].endswith("/deployments/worker")
        and "dryRun" not in request["query"]
    ]
    assert write["body"] == probe["body"]
    assert write["content_type"] == probe["content_type"]
    image = api.objects[("Deployment", "worker")]["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    assert image == f"registry.example/app/api@{DIGEST_2}"


def test_release_diff_is_read_only(release_env):
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    state = tmp_path / "state"
    files = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}
    versions = _versions(api)

    code, value, result = _run(tmp_path, "diff")
    assert code == 0, result.output
    assert value["state"] == "diffed"
    assert value["changes"] is True
    assert value["summary"] == {"apply": 2, "no-op": 3}
    kinds = [item["resource"]["kind"] for item in value["diffs"]]
    # ``diff`` never materializes secret inputs of a new release, so the
    # bound Secret of a changed composition is not compared.
    assert kinds == ["Secret", "Deployment"]
    assert "release/Deployment/worker" in result.stderr
    assert f"+      - image: registry.example/app/api@{DIGEST_2}" in result.stderr

    assert "reason" not in value

    code, pending, result = _run(tmp_path, "diff", "--exit-code")
    assert code == 1
    # Exit 1 names its registered code, like every "ran but did not succeed".
    assert pending["state"] == "diffed"
    assert pending["reason"] == "release-changes-pending"
    assert pending["summary"] == value["summary"]
    assert "[release-changes-pending]" in result.stderr
    assert {
        path: path.read_bytes() for path in state.rglob("*") if path.is_file()
    } == files
    assert _versions(api) == versions


def test_denied_dry_run_falls_back_to_a_literal_comparison(release_env):
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    api.inject("PATCH", "/deployments/worker", status=403, dry_run=True)

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    # Without evidence the defaulted live object differs: never a guessed no-op.
    assert _operations(planned)["Deployment/worker"] == "apply"
    assert planned["dry_run_unavailable"] == [
        {
            "resource": {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": TARGET.namespace,
                "name": "worker",
            },
            "reason": "rbac-denied",
        }
    ]
    worker = next(
        item for item in planned["diffs"] if item["resource"]["kind"] == "Deployment"
    )
    assert worker["basis"] == "client"
    assert SECRET_VALUE_MARKER not in result.stdout + result.stderr


def test_object_changed_between_discovery_and_dry_run_is_observed_again(
    release_env,
):
    """Regression: a status write between discovery and the dry run (a rollout
    finishing) made the dry run conflict, and the unchanged release planned
    ``apply`` from a literal comparison (seen on Kubernetes 1.37)."""
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    api.inject("PATCH", "/deployments/worker", status=409, dry_run=True)

    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert set(_operations(planned).values()) == {"no-op"}, planned["diffs"]
    assert planned["dry_run_unavailable"] == []
    assert len([r for r in _dry_runs(api) if "deployments" in r["path"]]) == 2


def _drop(tmp_path: Path) -> None:
    """Drop a ConfigMap key and label and a container env var from the module."""
    module = (tmp_path / "compose.py").read_text()
    for old, new in (
        (
            '"data": {"mode": "blue"}}',
            '"data": {"mode": "blue", "extra": "x"},\n'
            '         "metadata": {**meta("settings"), "labels": {"tier": "front"}}}',
        ),
        (
            '"resources": {"requests": {"cpu": "0.5"}}}',
            '"resources": {"requests": {"cpu": "0.5"}},\n'
            '                                    "env": [{"name": "A", "value": "1"},\n'
            '                                            {"name": "B", "value": "2"}]}',
        ),
    ):
        assert old in module
        module = module.replace(old, new)
    # Composition modules are cached per path: the earlier release is a copy.
    (tmp_path / "compose_v1.py").write_text(module)
    _use(tmp_path, "compose_v1.py")


def _use(tmp_path: Path, module: str) -> None:
    spec = tmp_path / "release.toml"
    text = spec.read_text()
    for name in ("compose.py", "compose_v1.py"):
        text = text.replace(f'composition = "{name}:build"', "composition = @")
    spec.write_text(text.replace("composition = @", f'composition = "{module}:build"'))


def test_keys_dropped_from_the_composition_are_removed_live(release_env):
    api, tmp_path = release_env
    _drop(tmp_path)
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    settings = api.objects[("ConfigMap", "settings")]
    assert settings["data"]["extra"] == "x"
    assert settings["metadata"]["labels"] == {"tier": "front"}
    # Another writer adds a key the release never declared.
    settings["data"]["foreign"] = "keep"

    _use(tmp_path, "compose.py")
    api.requests.clear()
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    operations = _operations(planned)
    assert operations["ConfigMap/settings"] == "apply"
    assert operations["Deployment/worker"] == "apply"
    assert operations["Secret/credential"] == "no-op"
    diffs = {item["resource"]["kind"]: item for item in planned["diffs"]}
    assert [
        (change["path"], change["op"]) for change in diffs["ConfigMap"]["changes"]
    ] == [("/data/extra", "remove"), ("/metadata/labels/tier", "remove")]
    env = [
        change for change in diffs["Deployment"]["changes"] if "/env" in change["path"]
    ]
    assert env and all(change["op"] == "remove" for change in env)
    (action,) = [item for item in planned["actions"] if item["name"] == "settings"]
    assert action["removes"] == ["/data/extra", "/metadata/labels/tier"]

    code, outcome, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert outcome["execution"]["state"] == "ready"
    (write,) = [
        request
        for request in api.requests
        if request["method"] == "PATCH"
        and request["path"].endswith("/configmaps/settings")
        and "dryRun" not in request["query"]
    ]
    assert write["body"]["data"]["extra"] is None
    assert write["body"]["metadata"]["labels"] == {"tier": None}
    settings = api.objects[("ConfigMap", "settings")]
    assert settings["data"] == {"mode": "blue", "foreign": "keep"}
    assert not settings["metadata"].get("labels")
    container = api.objects[("Deployment", "worker")]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert "env" not in container

    code, again, result = _run(tmp_path, "plan")
    assert code == 0, result.output
    assert set(_operations(again).values()) == {"no-op"}, again["diffs"]


def test_diff_of_an_unchanged_release_compares_secrets_privately(release_env):
    api, tmp_path = release_env
    code, _, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    state = tmp_path / "state"
    files = {path: path.read_bytes() for path in state.rglob("*") if path.is_file()}

    code, value, result = _run(tmp_path, "diff", "--exit-code")
    assert code == 0, result.output
    assert value["summary"] == {"no-op": 5} and value["diffs"] == []
    password = api.objects[("Secret", "credential")]["data"]["password"]
    assert password not in result.stdout + result.stderr
    assert {
        path: path.read_bytes() for path in state.rglob("*") if path.is_file()
    } == files
