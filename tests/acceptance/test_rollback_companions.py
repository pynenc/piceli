"""Acceptance (0.60.0): a rollback keeps the newest release's tailnet companions.

A release re-applied by a rollback was rendered by an older Piceli. Its
tailnet companions (label ``piceli.io/access``) belong to the environment,
and the older render may be wrong: on a cluster, the rollback to a release
0.58 had rendered brought back TURN's two Ingresses that 0.59 fixed and
deleted its plain companion. The composition here renders the companion by
image, standing in for two Piceli versions.
"""

from __future__ import annotations

from tests.acceptance.test_release_checks import _with_checks
from tests.acceptance.test_release_cli import (  # noqa: F401 (fixture)
    COMPOSITION,
    DIGEST_1,
    DIGEST_2,
    _image,
    _receipt,
    _run,
    release_env,
)

COMPANION = """

_build = build


def build(ctx):
    composition = _build(ctx)
    old = "2" * 64 not in ctx.image("api")
    companion = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "web-old" if old else "web-new",
                      "namespace": ctx.namespace,
                      "labels": {"piceli.io/access": "tailnet"}},
         "data": {"render": "old" if old else "new"}}
    )
    first, *rest = composition.components
    first = DeploymentComponent(first.name, (*first.resources, companion),
                                first.dependencies)
    return DeploymentComposition((first, *rest))
"""


def test_a_rollback_keeps_the_companions_of_the_release_it_leaves(release_env):  # noqa: F811
    api, tmp_path = release_env
    (tmp_path / "compose.py").write_text(COMPOSITION + COMPANION)
    _with_checks(tmp_path, rollback=True)
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            'state_dir = "state"\n', 'state_dir = "state"\nprune = true\n'
        )
    )
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, planned
    code, first, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, result.output
    assert ("ConfigMap", "web-old") in api.objects

    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, broken, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1, result.output
    assert broken["rollback"]["state"] == "rolled-back", broken["rollback"]
    # The app's own objects are the first release's again ...
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    # ... and the companion is the newest render, not the old one.
    assert ("ConfigMap", "web-new") in api.objects
    assert ("ConfigMap", "web-old") not in api.objects
