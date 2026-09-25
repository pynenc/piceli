"""Acceptance: ``piceli deploy --plan`` previews the release before images exist.

The preview uses placeholder digests (``pending-build`` / ``pending-delivery``)
for structure and ownership only. It is never approvable, never written to
the cluster (not even as a server dry run), and after delivery the real plan
may not adopt, replace or delete anything the approved preview did not show.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from piceli.testing import manifest
from tests.acceptance.test_deploy_pipeline import SCHEMA, FakeBackend, deploy, image

WRITES = {"POST", "PUT", "PATCH", "DELETE"}


def _adopt(tmp_path: Path, *entries: str) -> None:
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(
        text.replace("state_dir=", f"adopt={list(entries)!r}, state_dir=")
    )


def _unmanaged_web(api: Any) -> None:
    """A Deployment ``web`` created by kubectl, with the app's pod selector."""
    api.field_ownership = True
    web = manifest("Deployment", "web")
    labels = {"app.kubernetes.io/name": "web"}
    web["spec"]["selector"]["matchLabels"] = labels
    web["spec"]["template"]["metadata"]["labels"] = labels
    api.put(web, managers=[("kubectl-client-side-apply", "Update", web)])


def _writes(api: Any) -> list[dict[str, Any]]:
    return [request for request in api.requests if request["method"] in WRITES]


def _placeholder_sent(api: Any) -> bool:
    return any("piceli.invalid" in json.dumps(r["body"]) for r in api.requests)


def test_first_plan_lists_blocking_objects_before_any_build(shop) -> None:
    api, tmp_path = shop
    api.put(manifest("Deployment", "web"))  # unmanaged, same name as the app's
    api.requests.clear()
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    final = events[-1]
    assert final["state"] == "rejected"
    assert final["reason"] == "resource-requires-adoption"
    assert final["stage"] == "plan"
    assert [(b["kind"], b["name"]) for b in final["blocking"]] == [
        ("Deployment", "web")
    ]
    assert final["blocking"][0]["suggest"] == [
        "--adopt Deployment/web",
        "--replace Deployment/web",
    ]
    assert final["preview"] == {
        "approvable": False,
        "placeholders": {"web": "pending-build"},
    }
    assert "blocking Deployment/web" in result.stderr
    assert "preview" in result.stderr
    # Nothing was built or delivered; the cluster only received reads.
    assert FakeBackend.calls == []
    assert _writes(api) == []
    # Approving anything is refused the same way, before any build.
    for args in (("--auto-approve",), ("--approve", "0" * 64)):
        code, events, _ = deploy(tmp_path, *args)
        assert code == 2 and events[-1]["reason"] == "resource-requires-adoption"
    assert FakeBackend.calls == []


def test_preview_shows_adoption_and_is_never_approvable(shop) -> None:
    api, tmp_path = shop
    _unmanaged_web(api)
    _adopt(tmp_path, "Deployment/web")
    api.requests.clear()
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    plan = final["stages"]["plan"]
    # Existing fields keep their meaning: the release plan is still pending.
    assert plan["state"] == "pending"
    preview = plan["preview"]
    assert preview["approvable"] is False
    assert preview["state"] == "previewed"
    assert preview["placeholders"] == {"web": "pending-build"}
    changes = {(c["operation"], c["kind"], c["name"]) for c in preview["changes"]}
    assert ("adopt", "Deployment", "web") in changes
    assert ("create", "Deployment", "cache") in changes
    assert ("create", "Secret", "credentials") in changes
    assert preview["authorized"]["adopt"] == ["Deployment/web"]
    assert len(preview["preview_hash"]) == 64
    jsonschema.validate(preview, {"$defs": SCHEMA["$defs"], "$ref": "#/$defs/preview"})
    assert preview["preview_hash"] != final["combined_hash"]
    assert "preview" in result.stderr and "adopt Deployment/web" in result.stderr
    assert "not approvable" in result.stderr
    assert _writes(api) == [] and not _placeholder_sent(api)
    assert FakeBackend.calls == []

    # The preview's hash is not an approval.
    code, events, _ = deploy(tmp_path, "--approve", preview["preview_hash"])
    assert code == 2
    assert events[-1]["reason"] == "pipeline-preview-not-approvable"
    assert FakeBackend.calls == []

    # The combined hash approves build and delivery; the real plan follows.
    code, events, result = deploy(tmp_path, "--approve", final["combined_hash"])
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert image(api).startswith("registry.example:5000/shop/web@sha256:")
    assert "piceli.invalid" not in json.dumps(api.objects[("Deployment", "web")])


def test_real_plan_may_not_adopt_more_than_the_approved_preview(shop) -> None:
    api, tmp_path = shop
    _adopt(tmp_path, "Deployment/web")
    code, events, _ = deploy(tmp_path, "--plan")
    assert code == 0
    planned = events[-1]
    changes = planned["stages"]["plan"]["preview"]["changes"]
    assert {"operation": "create", "kind": "Deployment", "name": "web"} in changes

    # An unmanaged Deployment/web appears while the image builds.
    build = FakeBackend.build

    def build_and_race(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _unmanaged_web(api)
        return build(self, *args, **kwargs)

    FakeBackend.build = build_and_race  # type: ignore[method-assign]
    try:
        code, events, result = deploy(tmp_path, "--approve", planned["combined_hash"])
    finally:
        FakeBackend.build = build  # type: ignore[method-assign]
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-preview-changed"
    assert events[-1]["stage"] == "plan"
    assert events[-1]["stages"]["deliver"] == "done"
    assert events[-1]["stages"]["apply"] == "pending"
    # Nothing was applied: the unmanaged object is untouched.
    live = api.objects[("Deployment", "web")]
    assert "piceli.io/owner" not in live["metadata"].get("annotations", {})

    # Plan again after delivery: the real plan (with digests) is approvable.
    code, events, result = deploy(tmp_path, "--plan")
    assert code == 0, result.stdout + result.stderr
    plan = events[-1]["stages"]["plan"]
    assert plan["state"] == "planned" and "preview" not in plan
    assert {"operation": "adopt", "kind": "Deployment", "name": "web"} in plan[
        "changes"
    ]
    code, events, result = deploy(tmp_path, "--approve", events[-1]["combined_hash"])
    assert code == 0, result.stdout + result.stderr
    assert FakeBackend.calls.count("build") == 1


def test_preview_of_a_changed_image_skips_its_dry_run(shop) -> None:
    api, tmp_path = shop
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    api.requests.clear()
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    preview = events[-1]["stages"]["plan"]["preview"]
    assert preview["placeholders"] == {"web": "pending-build"}
    changes = {(c["operation"], c["kind"], c["name"]) for c in preview["changes"]}
    assert ("apply", "Deployment", "web") in changes
    assert preview["dry_run_skipped"] == [
        {"kind": "Deployment", "name": "web", "reason": "dry-run-placeholder-image"}
    ]
    # Only dry runs were sent, and never with a placeholder image.
    assert all(r["query"].get("dryRun") == ["All"] for r in _writes(api))
    assert not _placeholder_sent(api)


def test_built_but_undelivered_image_is_pending_delivery(shop) -> None:
    api, tmp_path = shop
    code, _, result = deploy(tmp_path, "--auto-approve", "--until", "build")
    assert code == 0, result.stdout + result.stderr
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    preview = events[-1]["stages"]["plan"]["preview"]
    assert preview["placeholders"] == {"web": "pending-delivery"}
