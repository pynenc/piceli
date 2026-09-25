"""Every claim on the decision pages links a file or test that exists.

``docs/comparisons.md`` and ``docs/when_to_use.md`` link repository files
with the ``{src}`` role (``{src}`path``` or ``{src}`test_name <path>```).
Each path must exist, and a title that names a test must be a test function
in that file, so a renamed or deleted test breaks this test instead of the
page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.comparisons.test_comparisons import SOURCES, count_lines, source_files

ROOT = Path(__file__).resolve().parents[2]
PAGES = ("docs/comparisons.md", "docs/when_to_use.md")
ROLE = re.compile(
    r"\{src\}`(?:(?P<title>[^`<]+?)\s*<(?P<target>[^`>]+)>|(?P<path>[^`]+))`"
)


def _links(page: str) -> list[tuple[str | None, str]]:
    text = (ROOT / page).read_text()
    return [(m["title"], m["target"] or m["path"]) for m in ROLE.finditer(text)]


@pytest.mark.parametrize("page", PAGES)
def test_every_src_link_exists(page: str) -> None:
    links = _links(page)
    assert len(links) >= 10
    for title, target in links:
        path = ROOT / target
        assert path.exists(), f"{page}: {target} does not exist"
        if title and title.startswith("test_"):
            assert re.search(
                rf"^(async )?def {re.escape(title)}\(", path.read_text(), re.M
            ), f"{page}: {target} has no test {title}"


def test_when_to_use_quotes_the_measured_line_counts() -> None:
    lines = {
        tool: sum(count_lines(path) for path in source_files(tool)) for tool in SOURCES
    }
    others = [count for tool, count in lines.items() if tool != "Piceli"]
    text = " ".join((ROOT / "docs" / "when_to_use.md").read_text().split())
    assert (
        f"takes {lines['Piceli']} lines in Piceli against {min(others)} to "
        f"{max(others)} in the other four tools"
    ) in text
