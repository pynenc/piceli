"""A pipeline's release refusals suggest what ``piceli deploy`` can act on."""

from __future__ import annotations

from piceli.k8s.release_runner import ReleaseError, blocking_message
from piceli.pipeline.runner import app_release_refusal, pipeline_suggestion


def test_release_flags_become_the_pipeline_declaration() -> None:
    assert pipeline_suggestion("--adopt Deployment/web") == (
        'Pipeline(adopt=["Deployment/web"])'
    )
    assert pipeline_suggestion("--replace Job/migrate") == (
        'Pipeline(replace=["Job/migrate"])'
    )
    assert pipeline_suggestion(
        "remove Deployment/web from --replace/[release] replace"
    ) == ('remove "Deployment/web" from Pipeline(replace=...)')
    kept = "change the composition to match the live object"
    assert pipeline_suggestion(kept) == kept


def _blocked() -> ReleaseError:
    blocking = [
        {
            "kind": "Deployment",
            "name": "web",
            "code": "resource-requires-adoption",
            "message": "exists and is not managed by this release's owner",
            "suggest": ["--adopt Deployment/web", "--replace Deployment/web"],
        },
        {
            "kind": "Job",
            "name": "migrate",
            "code": "immutable-field-changed",
            "message": "immutable fields would change (spec.template)",
            "suggest": ["--replace Job/migrate"],
        },
    ]
    return ReleaseError(
        blocking_message(blocking), code="plan-blocked", details={"blocking": blocking}
    )


def test_app_release_refusal_rewrites_suggestions_and_message() -> None:
    error = _blocked()
    assert "--adopt" in str(error)  # the release commands keep their flags
    failure = app_release_refusal(error)
    assert failure.code == "plan-blocked" and not failure.failed
    assert [item["suggest"] for item in failure.details["blocking"]] == [
        ['Pipeline(adopt=["Deployment/web"])', 'Pipeline(replace=["Deployment/web"])'],
        ['Pipeline(replace=["Job/migrate"])'],
    ]
    assert [item["code"] for item in failure.details["blocking"]] == [
        "resource-requires-adoption",
        "immutable-field-changed",
    ]
    message = str(failure)
    assert "--adopt" not in message and "--replace" not in message
    assert "[release]" not in message
    assert "declare them in the Pipeline" in message
    assert "Job/migrate: immutable fields would change" in message


def test_app_release_refusal_without_blocking_is_classify() -> None:
    failure = app_release_refusal(ReleaseError("nope", code="plan-expired"))
    assert failure.code == "plan-expired" and str(failure) == "nope"
