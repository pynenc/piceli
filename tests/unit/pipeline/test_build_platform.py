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


HOST_SPEC = SPEC.parent / "host-build.toml"


def test_host_builder_is_opt_in_and_checked_against_the_spec_revision() -> None:
    assert Build.spec(SPEC).builder == "docker"
    host = Build.spec(HOST_SPEC, builder="host")
    assert host.load().builder_kind == "host"
    assert host.image_names() == ("rust-hello",)
    with pytest.raises(PipelineError) as error:
        Build.spec(HOST_SPEC).load()
    assert error.value.code == "build-builder-mismatch"
    with pytest.raises(PipelineError) as error:
        Build.spec(SPEC, builder="host").load()
    assert error.value.code == "build-builder-mismatch"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"builder": "podman"},
        {"builder": "host", "platform": "linux/arm64"},
        {"node": "primary"},
        {"cache_dir": "cache"},
    ],
)
def test_host_builder_options_are_validated(kwargs: dict[str, str]) -> None:
    with pytest.raises(PipelineError) as error:
        Build.spec(HOST_SPEC, **kwargs)  # type: ignore[arg-type]
    assert error.value.code == "pipeline-invalid"
