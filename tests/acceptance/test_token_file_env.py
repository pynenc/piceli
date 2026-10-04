"""B25 through the whole deploy: a ``*TOKEN*_FILE`` env value that is a path applies."""

from __future__ import annotations

from typing import Any

from tests.acceptance.test_deploy_pipeline import PIPELINE, deploy

ENV = {
    "METRICS_TOKEN_FILE": "/run/private/target-token",
    "CONTROL_TOKEN_PATH": "/run/private/control-token",
}


def test_token_file_paths_in_env_apply_unredacted(shop: Any) -> None:
    api, tmp_path = shop
    source = PIPELINE.replace(
        'app.deployment("web", image=images["web"], ports=[8080])',
        f'app.deployment("web", image=images["web"], ports=[8080], env={ENV!r})',
    )
    assert source != PIPELINE
    (tmp_path / "app.py").write_text(source)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    container = api.objects[("Deployment", "web")]["spec"]["template"]["spec"][
        "containers"
    ][0]
    assert {item["name"]: item["value"] for item in container["env"]} == ENV
