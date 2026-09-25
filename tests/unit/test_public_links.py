"""Public links to the documentation point at the stable release.

README, ``llms.txt``, the package metadata and the other absolute links link
``/en/stable/`` so that readers, search engines and agents land on the docs
of the release they install. A page that the stable release does not have
yet links ``/en/latest/`` and is listed in ``LATEST_ONLY``; move it to
``/en/stable/`` once a release containing it is published.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS_URL = "https://docs.pynenc.org/projects/piceli/"
# Pages missing from the current stable docs (checked 2026-09-25).
LATEST_ONLY = {
    "comparisons",
    "compatibility",
    "contributing/evals",
    "crds",
    "environments",
    "gitops",
    "maintenance",
    "reference_app",
    "state",
    "when_to_use",
}
# JSON Schema ``$id``s are identifiers, not links, and never change.
EXEMPT = ("docs/schemas/",)


def _links() -> list[tuple[str, str]]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    found = []
    for name in listed:
        path = ROOT / name
        if name.startswith(EXEMPT) or name.startswith("tests/") or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        found += [
            (name, match)
            for match in re.findall(re.escape(DOCS_URL) + r"[^\s)\"'<>`]*", text)
        ]
    return found


def test_public_docs_links_use_the_stable_version() -> None:
    links = _links()
    assert any(name == "README.md" for name, _ in links)
    wrong = []
    for name, url in links:
        version, _, page = url.removeprefix(DOCS_URL + "en/").partition("/")
        stem = re.split(r"[.#/]", page, maxsplit=1)[0] if page else ""
        # A page under a directory (``contributing/evals``) may be listed alone.
        path = re.split(r"[.#]", page, maxsplit=1)[0] if page else ""
        latest_only = stem in LATEST_ONLY or path in LATEST_ONLY
        if version == "stable":
            if latest_only:
                wrong.append(f"{name}: {url} is not in the stable docs yet")
        elif version == "latest":
            if not latest_only:
                wrong.append(f"{name}: {url} should link /en/stable/")
        else:
            wrong.append(f"{name}: {url} names no version")
    assert wrong == []


def test_latest_only_pages_exist() -> None:
    for page in LATEST_ONLY:
        assert (ROOT / "docs" / f"{page}.md").is_file(), page
