"""Acceptance: a workload rolls when a Secret or ConfigMap it reads changes.

Through ``piceli release`` against the fake API: a TURN-like Deployment reads
a generated Secret once at start (an environment variable), a web
Deployment mounts a ConfigMap. The pod template of each carries a digest of
the content it reads, so:

- an unchanged deploy is a no-op;
- a new release that changes only the ConfigMap rolls only the web
  Deployment: the carried secret value gets a new private version, but the
  same digest;
- a Secret recreated with a rotated value under the same name rolls its
  consumer, and the annotation never holds the value.

The fake API adds server defaults, as a real one does: a workload is a no-op
only when the server dry run of exactly its final pod template (the keyed
digest, not the placeholder build's offline token) confirms it.
"""

from __future__ import annotations

import base64
import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.content_digest import (
    CONFIG_DIGEST_ANNOTATION,
    SECRET_DIGEST_ANNOTATION,
)
from tests.acceptance.fake_api import TARGET, serve

DIGEST = "sha256:" + "4" * 64

COMPOSITION = """
from piceli import App, ConfigVolume


def build(ctx):
    app = App("relay")
    config = app.config("web-config", {"greeting": ctx.values["greeting"]})
    auth = app.secret("turn-auth", {"secret": ctx.secret("turn_secret")})
    app.deployment(
        "turn", image=ctx.image("web"), env={"TURN_SECRET": auth.key("secret")}
    )
    app.deployment(
        "web", image=ctx.image("web"), volumes={"/etc/web": ConfigVolume(config)}
    )
    app.deployment(
        "reloader",
        image=ctx.image("web"),
        env={"TURN_SECRET": auth.key("secret")},
        roll_on_change=False,
    )
    return app
"""


def _run(spec: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result


def _deploy(spec: Path, *args: str) -> dict:
    code, planned, result = _run(spec, "plan", *args)
    assert code == 0, result.output
    code, applied, result = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert applied["execution"]["state"] == "ready", applied
    return planned


def _template(api, name: str) -> dict:
    return api.objects[("Deployment", name)]["spec"]["template"]


def _annotations(api, name: str) -> dict[str, str]:
    return dict(_template(api, name)["metadata"].get("annotations") or {})


def _spec(tmp_path: Path, url: str, greeting: str) -> Path:
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

            [secrets.turn_secret]
            type = "random"

            [values]
            greeting = "{greeting}"
            """
        )
    )
    return spec


def test_a_rotated_secret_rolls_its_consumer_and_an_unchanged_deploy_is_a_no_op(
    tmp_path,
):
    with serve() as (api, url):
        api.server_defaults = True
        spec = _spec(tmp_path, url, "hello")
        _deploy(spec)
        turn = _annotations(api, "turn")
        assert set(turn) == {SECRET_DIGEST_ANNOTATION}
        assert set(_annotations(api, "web")) == {CONFIG_DIGEST_ANNOTATION}
        # Opted out: no digest, and the marker is never applied.
        assert _annotations(api, "reloader") == {}
        reloader = api.objects[("Deployment", "reloader")]["metadata"]
        assert "piceli.io/roll-on-change" not in reloader["annotations"]
        value = base64.b64decode(api.objects[("Secret", "turn-auth")]["data"]["secret"])
        assert value.decode() not in json.dumps(api.objects[("Deployment", "turn")])

        # The same release again: nothing changes.
        code, planned, result = _run(spec, "plan")
        assert code == 0, result.output
        assert set(planned["summary"]) == {"no-op"}, planned["summary"]

        # A new release that changes only the ConfigMap: the secret value is
        # carried over (a new private version, the same value), so only the
        # web Deployment's template changes.
        web = _annotations(api, "web")
        spec.write_text(spec.read_text().replace('"hello"', '"bonjour"'))
        # A diff has no secret values: it keeps the live secret digest.
        code, diffed, result = _run(spec, "diff")
        assert code == 0, result.output
        operations = {
            (item["kind"], item["name"]): item["operation"]
            for item in diffed["actions"]
        }
        assert operations[("Deployment", "turn")] == "no-op"
        assert operations[("Deployment", "web")] == "apply"
        api.requests.clear()
        code, planned, result = _run(spec, "plan")
        assert code == 0, result.output
        assert planned["secrets"]["turn_secret"].startswith("carried:")
        # Regression: the dry runs were captured for the placeholder build's
        # digest only, so the planner ignored them, compared literally and
        # showed the server defaults of every Secret consumer as changes.
        assert planned["dry_run_unavailable"] == []
        # The last dry run of the consumer was captured again with the final
        # digest once the values were known: the live one, unchanged.
        *_, probe = [
            request
            for request in api.requests
            if request["method"] == "PATCH"
            and request["path"].endswith("/deployments/turn")
            and request["query"].get("dryRun") == ["All"]
        ]
        assert probe["body"]["spec"]["template"]["metadata"]["annotations"] == turn
        code, applied, result = _run(spec, "apply", "--approve", planned["plan_hash"])
        assert code == 0, result.output
        assert applied["execution"]["state"] == "ready", applied
        assert _annotations(api, "turn") == turn
        assert _annotations(api, "web") != web
        changed = {
            (action["kind"], action["name"])
            for action in planned["actions"]
            if action["operation"] != "no-op"
        }
        assert changed == {("ConfigMap", "web-config"), ("Deployment", "web")}

        # The Secret is recreated with a rotated value under the same name
        # (removed by hand, then rotated): the TURN Deployment rolls.
        del api.objects[("Secret", "turn-auth")]
        _deploy(spec, "--rotate", "turn_secret")
        rotated = base64.b64decode(
            api.objects[("Secret", "turn-auth")]["data"]["secret"]
        )
        assert rotated != value
        assert _annotations(api, "turn") != turn
        assert set(_annotations(api, "turn")) == {SECRET_DIGEST_ANNOTATION}
        assert rotated.decode() not in json.dumps(api.objects[("Deployment", "turn")])
        assert _annotations(api, "reloader") == {}
