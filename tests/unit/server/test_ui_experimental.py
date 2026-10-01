"""Installed UI paths validate their real inputs without an opt-in gate."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.ui import app as ui_app


def _cluster_args(tmp_path: Path) -> list[str]:
    return [
        "--api-server",
        "https://kubernetes.example.test",
        "--ca-file",
        str(tmp_path / "ca"),
        "--token-file",
        str(tmp_path / "token"),
        "--namespace",
        "shop",
        "--control-dir",
        str(tmp_path / "state"),
        "--origin",
        "https://piceli.example.test",
        "--oidc-issuer",
        "https://id.example.test",
        "--oidc-metadata-url",
        "https://id.example.test/.well-known/openid-configuration",
        "--oidc-client-id",
        "piceli-test",
        "--authorized-sub",
        "alice",
    ]


def test_legacy_experimental_environment_does_not_change_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = []
    for value in ("0", "1"):
        monkeypatch.setenv("PICELI_UI_EXPERIMENTAL", value)
        results.append(
            CliRunner().invoke(
                ui_app,
                ["cluster-serve", *_cluster_args(tmp_path), "--renderer-image", "bad"],
            )
        )
    assert all(result.exit_code == 2 for result in results)
    assert all('"reason": "ui-invalid-request"' in result.stdout for result in results)


@pytest.mark.parametrize("command", ["cluster-serve", "cluster-observe"])
@pytest.mark.parametrize(
    "extra",
    [
        ["--authorized-deploy-sub", "alice"],
        ["--authorized-access-sub", "alice"],
        ["--renderer-image", "registry.example.test/r@sha256:" + "a" * 64],
    ],
)
def test_cluster_delivery_and_access_reach_input_validation_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra: list[str],
) -> None:
    monkeypatch.delenv("PICELI_UI_EXPERIMENTAL", raising=False)
    result = CliRunner().invoke(ui_app, [command, *_cluster_args(tmp_path), *extra])
    assert result.exit_code == 2
    assert '"reason": "ui-experimental-disabled"' not in result.stdout
    assert '"reason": "ui-invalid-request"' in result.stdout


def test_connect_reaches_kubeconfig_validation_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PICELI_UI_EXPERIMENTAL", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(
        ui_app,
        [
            "connect",
            "--server",
            "https://piceli.example.test",
            "--ticket",
            "ticket-1",
            "--kubeconfig",
            str(tmp_path / "kubeconfig"),
            "--context",
            "scratch",
            "--local-port",
            "18080",
        ],
    )
    assert result.exit_code == 2
    assert '"reason": "ui-invalid-request"' in result.stdout


def test_legacy_opt_in_is_accepted_but_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_UI_EXPERIMENTAL", "1")
    result = CliRunner().invoke(
        ui_app,
        [
            "cluster-serve",
            *_cluster_args(tmp_path),
            "--authorized-access-sub",
            "alice",
        ],
    )
    assert '"reason": "ui-invalid-request"' in result.stdout
