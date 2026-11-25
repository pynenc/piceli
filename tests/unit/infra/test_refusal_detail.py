"""A refused host build says why a source was refused (0.79.0)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from piceli.artifacts.build_spec import source_refusal
from piceli.artifacts.source_identity import (
    InputsVerification,
    SourceDriftError,
    SourceIdentityError,
)
from piceli.infra.builders import refusal_detail


def test_a_source_refusal_keeps_the_cause_out_of_the_message() -> None:
    error = source_refusal(
        SourceIdentityError("git status failed in /work/src/shop: fatal: unsafe")
    )
    assert (error.code, str(error)) == ("source-identity", "sources not verifiable")
    assert error.reason == "git status failed in /work/src/shop: fatal: unsafe"


def test_a_drift_is_source_drift_with_its_summary() -> None:
    drift = SourceDriftError("sources differ: shop: commit 'a' -> 'b'", None)  # type: ignore[arg-type]
    error = source_refusal(drift)
    assert (error.code, error.reason) == (
        "source-drift",
        "sources differ: shop: commit 'a' -> 'b'",
    )
    assert InputsVerification  # the type the real drift carries


def test_the_reason_is_redacted_and_bounded() -> None:
    error = source_refusal(
        SourceIdentityError("git fetch failed: token=abcdef123456 " + "x" * 900)
    )
    assert "abcdef123456" not in error.reason
    assert len(error.reason) <= 400


@pytest.mark.parametrize(
    ("code", "reason", "expected"),
    [
        ("source-identity", "source 'shop' is dirty", ": source 'shop' is dirty"),
        ("source-drift", "shop: commit a -> b", ": shop: commit a -> b"),
        ("source-identity", "", ""),
        ("plan-not-approved", "ignored", ""),
    ],
)
def test_refusal_detail(code: str, reason: str, expected: str) -> None:
    error = SimpleNamespace(code=code, reason=reason)
    assert refusal_detail(error) == expected


def test_a_dirty_source_names_its_paths_and_the_builder_policy(tmp_path: Path) -> None:
    import subprocess

    from piceli.artifacts.build_spec import BuildSpecError
    from piceli.artifacts.source_identity import (
        InputsSpec,
        SourceSpec,
        record_inputs,
    )

    repo = tmp_path / "shop"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("new\n")
    with pytest.raises(SourceIdentityError) as caught:
        record_inputs(InputsSpec((SourceSpec("shop", str(repo)),), roots={}))
    error: BuildSpecError = source_refusal(caught.value)
    assert error.cause == "source-dirty"
    assert "2 uncommitted change(s) (M a.txt, ?? new.txt)" in error.reason
    detail = refusal_detail(error)
    assert detail.startswith(": source 'shop' has 2 uncommitted change(s) (M a.txt")
    assert "ignore allow_dirty in inputs.toml" in detail
    assert "set allow_dirty = true" not in detail
