"""``Build.spec(platform=)`` builds a spec for another platform."""

from pathlib import Path

import pytest

from piceli.pipeline import Build, PipelineError

SPEC = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "builds"
    / "rust-hello"
    / "build.toml"
)


def test_platform_override_replaces_the_spec_platforms_and_changes_its_digest() -> None:
    plain = Build.spec(SPEC).load()
    amd64 = Build.spec(SPEC, platform="linux/amd64").load()
    assert plain.platforms == ("linux/arm64",)
    assert amd64.platforms == ("linux/amd64",)
    assert amd64.platform_override == "linux/amd64"
    assert amd64.spec_sha256 != plain.spec_sha256


def test_the_file_recheck_applies_the_same_override() -> None:
    from piceli.artifacts.build_spec import BuildSpec

    amd64 = Build.spec(SPEC, platform="linux/amd64").load()
    fresh = BuildSpec.from_toml(SPEC).with_platform("linux/amd64")
    assert fresh.spec_sha256 == amd64.spec_sha256


def test_an_unsupported_platform_is_refused() -> None:
    with pytest.raises(PipelineError) as error:
        Build.spec(SPEC, platform="windows/amd64").load()
    assert error.value.code == "pipeline-invalid"
