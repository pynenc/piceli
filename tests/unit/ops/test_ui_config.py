from pathlib import Path

import pytest
from pydantic import ValidationError

from piceli.k8s.observe import ObservationRef, ObservedObject
from piceli.k8s.operator import build_operator_report
from piceli.k8s.ui_config import UI_CONFIG_ENV, UiConfig, load_ui_config


def test_default_config_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UI_CONFIG_ENV, raising=False)
    config = load_ui_config()
    assert config == UiConfig()
    assert config.shortcuts == ()
    assert config.public_dict() == {"topology_subtitle": "", "badges": [], "tiers": []}


def test_config_loads_from_env_and_normalizes_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ui.toml"
    path.write_text(
        """
[[shortcuts]]
id = "web"
label = "Web"
target = "service/web"
local_port = 3000
remote_port = 8080
path = "/login"

[[tiers]]
name = "App Tier"
components = ["worker", { name = "web", shortcut = "web" }]
"""
    )
    monkeypatch.setenv(UI_CONFIG_ENV, str(path))
    config = load_ui_config()
    assert config.shortcut("web").url == "http://127.0.0.1:3000/login"
    (tier,) = config.tiers
    assert tier.id == "app-tier"
    assert [c.name for c in tier.components] == ["worker", "web"]


@pytest.mark.parametrize(
    "data",
    [
        {"shortcuts": [{"id": "x", "label": "x", "target": "deployment/x", "local_port": 1, "remote_port": 1}]},
        {"shortcuts": [{"id": "x", "label": "x", "target": "service/x", "local_port": 0, "remote_port": 1}]},
        {"shortcuts": [{"id": "x", "label": "x", "target": "service/x", "local_port": 1, "remote_port": 1, "path": "javascript:x"}]},
        {"tiers": [{"name": "t", "components": [{"name": "a", "shortcut": "missing"}]}]},
        {"unknown_field": True},
    ],
)
def test_invalid_config_is_rejected(data: dict) -> None:
    with pytest.raises(ValidationError):
        UiConfig.model_validate(data)


def test_consumer_example_config_is_valid() -> None:
    example = (
        Path(__file__).parents[5] / "docs" / "infinite-haiku" / "piceli-ui.toml"
    )
    if not example.exists():
        pytest.skip("workspace consumer config not present")
    config = load_ui_config(example)
    assert len(config.shortcuts) == 5
    assert len(config.tiers) == 4


class _Reader:
    def get(self, ref: ObservationRef) -> None:
        return None

    def list(self, api_version: str, kind: str, namespace: str) -> list[ObservedObject]:
        if kind != "Deployment":
            return []
        return [
            ObservedObject(
                ref=ObservationRef("apps/v1", "Deployment", namespace, "api"),
                labels=(("app.kubernetes.io/part-of", "shop"), ("shop/revision", "r7")),
            )
        ]


def test_operator_report_uses_configured_managed_labels() -> None:
    default = build_operator_report(_Reader(), "demo", include_common_types=True)
    assert [r.ref.name for r in default.unmanaged] == ["api"]
    configured = build_operator_report(
        _Reader(),
        "demo",
        managed_labels={"app.kubernetes.io/part-of": "shop"},
        revision_label="shop/revision",
    )
    (managed,) = configured.managed
    assert managed.ref.name == "api"
    assert managed.release_name == "r7"
