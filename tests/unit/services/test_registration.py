"""Existing definitions are registered without importing their application code."""

import json
from pathlib import Path

from piceli.k8s.release_spec import ReleaseSpec
from piceli.services.query import QueryService
from piceli.services.registration import Registration


def test_registration_and_saved_plan_reads_do_not_load_source(tmp_path: Path) -> None:
    spec = ReleaseSpec.from_dict(
        {
            "target": {"kubeconfig": "kc", "context": "local", "namespace": "shop"},
            "release": {
                "name": "shop",
                "owner": "owner",
                "field_manager": "manager",
                "composition": "missing.py:build",
                "state_dir": "state",
            },
            "images": {"api": "registry.example/api@sha256:" + "a" * 64},
        },
        tmp_path,
    )
    registered = Registration.from_release("shop", "Shop", spec)
    assert registered.source is not None
    assert str(tmp_path) not in registered.source.model_dump_json()
    assert not spec.state_dir.exists()
    service = QueryService([registered])
    assert service.saved_plans("shop") == []
    assert not spec.state_dir.exists()
    directory = spec.state_dir / "plans"
    directory.mkdir(parents=True)
    path = directory / ("a" * 64 + ".json")
    path.write_text(
        json.dumps(
            {
                "release": "shop-a",
                "intent": "apply",
                "expires_at": "2030-01-01T00:00:00Z",
                "discovery": "private-secret-manifest",
            }
        )
    )
    before = path.read_bytes()
    plans = service.saved_plans("shop")
    assert plans[0]["digest"] == "a" * 64
    assert "private-secret-manifest" not in json.dumps(plans)
    assert path.read_bytes() == before
    assert service.application("shop").source == registered.source
