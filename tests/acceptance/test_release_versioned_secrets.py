"""Acceptance: versioned Secrets get content-addressed names (0.27.0).

Through ``piceli release`` against the fake API: a relay Deployment mounts a
config file that carries a shared secret (a ``template`` over a ``random``),
declared with ``app.secret("turn", ..., versioned=True)``. So:

- the Secret is named ``turn-<hash>``, immutable, and the Deployment refers
  to that name; an unchanged release is a no-op;
- a changed value is a new Secret and the Deployment follows it in the same
  apply; the old one is kept while the running release (the rollback
  target) uses it, and deleted at the next change once nothing mounts it;
- a rollback re-applies the previous name, which was kept;
- no value appears in a name, a plan or the output;
- a generated value bound directly into a versioned Secret is refused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.secret_names import VERSIONED_SECRET_ANNOTATION
from tests.acceptance.fake_api import TARGET, serve

DIGEST = "sha256:" + "4" * 64

COMPOSITION = """
from piceli import App, SecretVolume


def build(ctx):
    app = App("relay")
    turn = app.secret(
        "turn", {"turnserver.conf": ctx.secret(ctx.values["bound"])}, versioned=True
    )
    app.deployment(
        "relay", image=ctx.image("web"), volumes={"/etc/turn": SecretVolume(turn)}
    )
    return app
"""


def _run(spec: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result


def _plan(spec: Path, *args: str):
    code, planned, result = _run(spec, "plan", *args)
    assert code == 0, result.output
    return planned, result


def _apply(spec: Path, planned: dict) -> None:
    code, applied, result = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert applied["execution"]["state"] == "ready", applied


def _spec(tmp_path: Path, url: str, version: str, bound: str = "turn_conf") -> Path:
    (tmp_path / "composition.py").write_text(COMPOSITION)
    (tmp_path / "kubeconfig").write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
            users: [{{name: nobody, user: {{}}}}]
            contexts: [{{name: fake, context: {{cluster: fake, user: nobody}}}}]
            """
        )
    )
    spec = tmp_path / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{TARGET.namespace}"
            transport = "loopback-http"

            [release]
            name = "relay"
            owner = "relay"
            field_manager = "relay"
            composition = "composition.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 30
            readiness_seconds = 1
            poll_seconds = 0.05

            [images]
            web = "docker.io/library/nginx@{DIGEST}"

            [secrets.shared]
            type = "random"

            [secrets.turn_conf]
            type = "template"
            template = "listening-port=3478 static-auth-secret={{shared}} # {version}"

            [values]
            bound = "{bound}"
            """
        )
    )
    return spec


def _secrets(api) -> dict[str, dict]:
    return {
        name: manifest
        for (kind, name), manifest in api.objects.items()
        if kind == "Secret" and name.startswith("turn")
    }


def _mounted(api) -> str:
    template = api.objects[("Deployment", "relay")]["spec"]["template"]
    (volume,) = template["spec"]["volumes"]
    return volume["secret"]["secretName"]


def _value(manifest: dict) -> str:
    return base64.b64decode(manifest["data"]["turnserver.conf"]).decode()


def _no_value(values: list[str], *texts: str) -> None:
    for value in values:
        secret = value.split("static-auth-secret=")[1].split(" ")[0]
        for text in texts:
            assert secret not in text


def test_a_changed_value_is_a_new_secret_and_the_old_one_goes_later(tmp_path):
    with serve() as (api, url):
        api.server_defaults = True
        spec = _spec(tmp_path, url, "v1")
        planned, result = _plan(spec)
        _apply(spec, planned)
        (first,) = _secrets(api)
        assert re.fullmatch(r"turn-[0-9a-f]{10}", first)
        assert api.objects[("Secret", first)]["immutable"] is True
        assert _mounted(api) == first
        assert planned["versioned_secrets"] == [
            {
                "secret": "turn",
                "name": first,
                "operation": "create",
                "why": "new value",
            }
        ]
        assert f"secret turn → {first} (new value)" in result.stderr
        outputs = [result.stdout, result.stderr]

        # Unchanged: a no-op, nothing renamed or deleted.
        planned, result = _plan(spec)
        assert set(planned["summary"]) == {"no-op"}, planned["summary"]
        assert planned["versioned_secrets"] == []

        # A new value: a new Secret, the Deployment follows it; the first
        # is kept, the running release (the rollback target) mounts it.
        spec.write_text(spec.read_text().replace("# v1", "# v2"))
        planned, result = _plan(spec)
        operations = {
            (item["kind"], item["name"]): item["operation"]
            for item in planned["actions"]
        }
        assert operations[("Deployment", "relay")] == "apply"
        assert ("Secret", first) not in operations
        _apply(spec, planned)
        second = _mounted(api)
        assert second != first
        assert set(_secrets(api)) == {first, second}
        assert [item["name"] for item in planned["versioned_secrets"]] == [second]
        outputs += [result.stdout, result.stderr]

        # The same release again: no-op, the first version is still kept.
        planned, result = _plan(spec)
        assert set(planned["summary"]) == {"no-op"}, planned["summary"]
        assert set(_secrets(api)) == {first, second}

        # A look-alike Secret that Piceli did not write is never deleted.
        api.put(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": "turn-0000000000",
                    "namespace": TARGET.namespace,
                    "annotations": {VERSIONED_SECRET_ANNOTATION: "turn"},
                },
                "data": {},
            }
        )

        # Another value: the first, which nothing mounts now, is deleted;
        # the second (the new rollback target) is kept.
        spec.write_text(spec.read_text().replace("# v2", "# v3"))
        planned, result = _plan(spec)
        third = next(
            item["name"]
            for item in planned["versioned_secrets"]
            if item["operation"] == "create"
        )
        assert {
            (item["name"], item["operation"]) for item in planned["versioned_secrets"]
        } == {(third, "create"), (first, "delete")}
        assert f"delete {first} (no longer mounted)" in result.stderr
        values = [_value(api.objects[("Secret", name)]) for name in (first, second)]
        _apply(spec, planned)
        assert _mounted(api) == third
        assert set(_secrets(api)) == {second, third, "turn-0000000000"}
        outputs += [result.stdout, result.stderr]
        values += [_value(api.objects[("Secret", third)])]
        assert len(set(values)) == 3

        # A rollback mounts the kept second version again, unchanged.
        code, pending, result = _run(spec, "rollback", "previous")
        assert code == 3, result.output
        operations = {
            (item["kind"], item["name"]): item["operation"]
            for item in pending["actions"]
        }
        assert operations[("Secret", second)] == "no-op"
        assert operations[("Deployment", "relay")] == "apply"
        code, rolled, result = _run(
            spec, "rollback", "previous", "--approve", pending["plan_hash"]
        )
        assert code == 0, result.output
        assert rolled["execution"]["state"] == "ready"
        assert _mounted(api) == second
        assert set(_secrets(api)) == {second, third, "turn-0000000000"}
        outputs += [result.stdout, result.stderr]

        # No value in a name, a plan or the output.
        _no_value(values, *outputs, *_secrets(api))
        # The fingerprints are keyed: the name is not a plain hash of a value.
        assert all(
            hashlib.sha256(value.encode()).hexdigest()[:10] not in name
            for value in values
            for name in _secrets(api)
        )


def test_a_generated_value_in_a_versioned_secret_is_refused(tmp_path):
    with serve() as (api, url):
        spec = _spec(tmp_path, url, "v1", bound="shared")
        code, refused, result = _run(spec, "plan")
        assert code == 2, result.output
        assert refused["reason"] == "versioned-secret-generated"
        assert _secrets(api) == {}
