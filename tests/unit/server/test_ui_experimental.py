"""Unfinished in-cluster UI paths are refused unless explicitly opted in."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.ui import app as ui_app
from piceli.k8s.ui_experimental import EXPERIMENTAL_ENV, experimental_enabled


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


def test_opt_in_reads_flag_or_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    assert not experimental_enabled()
    assert experimental_enabled(True)
    monkeypatch.setenv(EXPERIMENTAL_ENV, "0")
    assert not experimental_enabled()
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    assert experimental_enabled()


@pytest.mark.parametrize("command", ["cluster-serve", "cluster-observe"])
@pytest.mark.parametrize(
    "extra",
    [
        ["--authorized-deploy-sub", "alice"],
        ["--authorized-access-sub", "alice"],
        ["--renderer-image", "registry.example.test/r@sha256:" + "a" * 64],
    ],
)
def test_cluster_delivery_and_access_are_disabled_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra: list[str],
) -> None:
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
    result = CliRunner().invoke(ui_app, [command, *_cluster_args(tmp_path), *extra])
    assert result.exit_code == 2
    assert '"reason": "ui-experimental-disabled"' in result.stdout
    assert not (tmp_path / "state").exists()


def test_connect_is_disabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EXPERIMENTAL_ENV, raising=False)
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
    assert '"reason": "ui-experimental-disabled"' in result.stdout


def test_opt_in_passes_the_gate_to_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    result = CliRunner().invoke(
        ui_app,
        [
            "cluster-serve",
            *_cluster_args(tmp_path),
            "--authorized-access-sub",
            "alice",
        ],
    )
    assert '"reason": "ui-experimental-disabled"' not in result.stdout
