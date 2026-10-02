"""The archive transport lists saved plans without evaluation or execution."""

from pathlib import Path

from fastapi.testclient import TestClient

from piceli.server.app import create_app
from tests.acceptance.test_ui_operations import API, ORIGIN, launch
from tests.unit.services.test_plan_archive import archive_service, stored_plan


def test_plan_archive_http_is_paginated_read_only_and_validates_requests(
    tmp_path: Path,
) -> None:
    operations = archive_service(tmp_path)
    for id in ("first", "second", "third"):
        stored_plan(operations, id, "2026-10-02T10:00:00Z")
    stored_plan(operations, "foreign", "2026-10-03T10:00:00Z", application="other")
    with TestClient(
        create_app(operations.query, operations=operations), base_url=ORIGIN
    ) as client:
        assert launch(client).status_code == 200
        before = operations.store.cursor()
        first = client.get(API + "/applications/shop/plans", params={"limit": 2})
        assert first.status_code == 200
        assert [item["id"] for item in first.json()["items"]] == ["third", "second"]
        page = first.json()["next_page"]
        assert page
        second = client.get(
            API + "/applications/shop/plans", params={"limit": 2, "page": page}
        )
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["items"]] == ["first"]
        assert second.json()["next_page"] is None
        for params in (
            {"page": "invalid"},
            {"limit": 0},
            {"limit": 101},
            {"limit": "invalid"},
        ):
            refused = client.get(API + "/applications/shop/plans", params=params)
            assert refused.status_code in {409, 422}
            assert refused.json()["code"] == "ui-invalid-request"
        assert client.get(API + "/applications/missing/plans").status_code == 404
        assert operations.store.cursor() == before
        assert operations.store.records("operation") == []
        operations.evaluator.preview.assert_not_called()
        operations.evaluator.render.assert_not_called()
