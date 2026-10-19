"""``--local-build`` (``LocalBuilder``) builds every wanted platform (0.22.0).

A ``files`` component builds for real (no compiler, no network); the
registry is a fake backend that records each push and index. With one
platform nothing changes: one push, the platform's own digest, no index.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from piceli.infra.builders import BuildItem, LocalBuilder
from piceli.infra.contract import parse_contracts

TOML = '[component.site]\nbuild = { files = { "site" = "/srv/site" } }\n'
INDEX = "sha256:" + "9" * 64


def _digest(archive: Path) -> str:
    if "linux-arm64" in archive.parts:
        return "sha256:" + "b" * 64
    return "sha256:" + "a" * 64


class FakeBackend:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str, Path]] = []
        self.indexed: list[tuple[str, dict[str, str]]] = []

    def registry_deliver(
        self, route: Any, image_id: str, repository: str, *, archive: Path
    ) -> dict[str, Any]:
        assert archive.is_file()  # the host build's archive, not yet removed
        self.pushed.append((image_id, repository, archive))
        return {
            "state": "succeeded",
            "pull_ref": f"registry.example:5000/{repository}@{_digest(archive)}",
        }

    def registry_index(
        self, route: Any, repository: str, digests: Mapping[str, str]
    ) -> str:
        self.indexed.append((repository, dict(digests)))
        return INDEX


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "site").mkdir(parents=True)
    (root / "site" / "index.html").write_text("<h1>hi</h1>\n")
    (root / "piceli.toml").write_text(TOML)
    return root


def _build(
    tmp_path: Path, source: Path, **platforms: Any
) -> tuple[FakeBackend, Any, LocalBuilder]:
    backend = FakeBackend()
    builder = LocalBuilder(
        route=None,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "work",
        backend=backend,
        **platforms,
    )

    @contextmanager
    def checkout(name: str, commit: str, into: Path) -> Iterator[Path]:
        yield source

    item = BuildItem(
        "site",
        "shop",
        "1" * 40,
        "sha256:" + "e" * 64,
        parse_contracts(TOML)["site"],
        "shop/site",
    )
    return backend, builder.build([item], checkout)["site"], builder


def test_two_platforms_build_each_and_push_one_index(
    tmp_path: Path, source: Path
) -> None:
    backend, image, builder = _build(
        tmp_path, source, platforms=("linux/amd64", "linux/arm64")
    )
    assert builder.platforms == ("linux/amd64", "linux/arm64")
    platforms = sorted(
        part for _, _, archive in backend.pushed for part in archive.parts
        if part.startswith("linux-")
    )  # fmt: skip
    assert platforms == ["linux-amd64", "linux-arm64"]
    # Each platform is its own image (another config digest).
    assert len({image_id for image_id, _, _ in backend.pushed}) == 2
    assert backend.indexed == [
        (
            "shop/site",
            {"linux/amd64": "sha256:" + "a" * 64, "linux/arm64": "sha256:" + "b" * 64},
        )
    ]
    assert image.pull_ref == f"registry.example:5000/shop/site@{INDEX}"
    assert image.manifest_digest == INDEX
    assert image.extra == {
        "platforms": {
            "linux/amd64": "sha256:" + "a" * 64,
            "linux/arm64": "sha256:" + "b" * 64,
        }
    }
    assert list((tmp_path / "work").iterdir()) == []  # archives removed


def test_one_platform_is_unchanged(tmp_path: Path, source: Path) -> None:
    backend, image, builder = _build(tmp_path, source, platform="linux/arm64")
    assert builder.platforms == ("linux/arm64",) and builder.platform == "linux/arm64"
    (pushed,) = backend.pushed
    assert not any(part.startswith("linux-") for part in pushed[2].parts)
    assert backend.indexed == []
    assert image.pull_ref == "registry.example:5000/shop/site@sha256:" + "a" * 64
    assert image.manifest_digest == "sha256:" + "a" * 64 and image.extra == {}


def test_build_spec_hands_every_platform_and_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra import pipelines
    from piceli.infra.pipelines import SpecBuildRequest

    seen: dict[str, Any] = {}

    def run_spec_build(request: Any, roots: Any, **options: Any) -> dict[str, Any]:
        seen.update(options)
        digests = {"linux/amd64": "sha256:" + "a" * 64}
        if options["index"] is not None:
            digests["linux/arm64"] = "sha256:" + "b" * 64
            found = options["index"]("shop/app", digests)
            entry = {"pull_ref": f"r/shop/app@{found}", "manifest_digest": found,
                     "platforms": digests}  # fmt: skip
        else:
            entry = {"pull_ref": "r/shop/app@" + digests["linux/amd64"],
                     "manifest_digest": digests["linux/amd64"]}  # fmt: skip
        return {"images": {"app": entry}}

    monkeypatch.setattr(pipelines, "run_spec_build", run_spec_build)

    @contextmanager
    def checkout(name: str, commit: str, into: Path) -> Iterator[Path]:
        yield tmp_path

    request = SpecBuildRequest(
        spec="build.toml",
        repo="shop",
        commits={"shop": "1" * 40},
        images={"app": {"repository": "shop/app", "key": "k"}},
    )
    for platforms, index in (
        (("linux/amd64", "linux/arm64"), True),
        (("linux/amd64",), False),
    ):
        backend = FakeBackend()
        builder = LocalBuilder(
            None,
            platforms=platforms,
            cache_dir=tmp_path / "cache",
            work_dir=tmp_path / "work",
            backend=backend,
        )
        image = builder.build_spec(request, checkout)["app"]
        assert seen["platforms"] == platforms
        assert (seen["index"] is not None) is index
        if index:
            assert backend.indexed == [("shop/app", {"linux/amd64": "sha256:" + "a" * 64,
                                                     "linux/arm64": "sha256:" + "b" * 64})]  # fmt: skip
            assert image.manifest_digest == INDEX and "platforms" in image.extra
        else:
            assert backend.indexed == [] and image.extra == {}
