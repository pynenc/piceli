"""Unit: ``--ref`` parsing, checkout roots and SIGTERM cleanup."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from piceli.artifacts.source_identity import InputsSpec, SourceSpec
from piceli.k8s.cli.deploy_pipeline import _terminate_as_interrupt
from piceli.pipeline import PipelineError
from piceli.pipeline.refs import (
    RefRequest,
    SourceCheckout,
    SourceCheckouts,
    parse_refs,
)


def test_parse_refs() -> None:
    assert parse_refs([]) == ()
    assert parse_refs(["main"]) == (RefRequest(None, "main"),)
    assert parse_refs(["api=v1.2", "web=HEAD~1"]) == (
        RefRequest("api", "v1.2"),
        RefRequest("web", "HEAD~1"),
    )
    assert str(RefRequest("api", "main")) == "api=main"


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (["main", "api=main"], "deploy-ref-ambiguous"),
        (["main", "main"], "deploy-ref-ambiguous"),
        (["api=main", "api=v2"], "deploy-ref-invalid"),
        (["api="], "deploy-ref-invalid"),
        (["=main"], "deploy-ref-invalid"),
        (["-rf"], "deploy-ref-invalid"),
        (["a..b"], "deploy-ref-invalid"),
        (["main branch"], "deploy-ref-invalid"),
        (["HEAD:path"], "deploy-ref-invalid"),
        (["bad name=main"], "deploy-ref-invalid"),
    ],
)
def test_parse_refs_refusals(values: list[str], code: str) -> None:
    with pytest.raises(PipelineError) as caught:
        parse_refs(values)
    assert caught.value.code == code


def test_roots_redirect_a_source_but_keep_its_declaration(tmp_path: Path) -> None:
    spec = InputsSpec((SourceSpec("app", ".."),), tmp_path / "deploy")
    pinned = InputsSpec(spec.sources, spec.base, roots={"app": tmp_path / "tree"})
    assert pinned == spec and pinned.spec_sha256 == spec.spec_sha256
    assert pinned.resolve(spec.sources[0]) == tmp_path / "tree"
    assert pinned.declared(spec.sources[0]) == tmp_path / "deploy" / ".."


def test_remap_prefers_the_innermost_repository(tmp_path: Path) -> None:
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "vendor" / "lib"
    checkouts = SourceCheckouts(
        {
            "outer": SourceCheckout("outer", outer.resolve(), "main", "a" * 40),
            "lib": SourceCheckout("lib", inner.resolve(), "main", "b" * 40),
        }
    )
    checkouts.checkouts["outer"].root = tmp_path / "t0"
    checkouts.checkouts["lib"].root = tmp_path / "t1"
    assert checkouts.remap(outer / "deploy" / "build.toml") == (
        tmp_path / "t0" / "deploy" / "build.toml"
    )
    assert checkouts.remap(inner / "x.txt") == tmp_path / "t1" / "x.txt"
    elsewhere = tmp_path / "other" / "file"
    assert checkouts.remap(elsewhere) == elsewhere
    assert checkouts.hashed() == {"lib": "b" * 40, "outer": "a" * 40}
    checkouts.close()  # nothing materialised: a no-op
    assert checkouts.roots() == {}


def test_sigterm_becomes_an_interrupt_and_the_handler_is_restored() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt), _terminate_as_interrupt():
        os.kill(os.getpid(), signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == before
