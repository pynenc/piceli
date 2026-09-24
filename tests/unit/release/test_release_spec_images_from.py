"""``images_from`` is a top-level key; a misplaced one gets a helpful error."""

from __future__ import annotations

from pathlib import Path

import pytest

from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError


@pytest.mark.parametrize("table", ["images", "execution", "release"])
def test_images_from_inside_a_table_is_explained(table: str, tmp_path: Path) -> None:
    with pytest.raises(ReleaseSpecError, match=rf"top-level key .*\[{table}\]"):
        ReleaseSpec.from_dict({table: {"images_from": "build.receipt.json"}}, tmp_path)


def test_example_spec_places_images_from_at_top_level() -> None:
    example = Path(__file__).parents[3] / "examples" / "release" / "release.toml"
    lines = example.read_text().splitlines()
    first_table = next(i for i, line in enumerate(lines) if line.startswith("["))
    mentions = [i for i, line in enumerate(lines) if "images_from =" in line]
    assert mentions and all(i < first_table for i in mentions)
