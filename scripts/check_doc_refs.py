"""Refuse a ``{ref}`label``` without a title to a label above no heading.

Sphinx takes such a reference's text from the heading under the label; a
label above a paragraph has none, and the docs build (``-W``) fails with
"A title or caption not found". This catches it in pre-commit, in a
fraction of a second, instead of in the CI docs job.

Usage: ``python3 scripts/check_doc_refs.py [FILE ...]`` (default: every
``docs/**/*.md``). Labels are read from every docs page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
LABEL = re.compile(r"^\(([A-Za-z0-9_.:-]+)\)=\s*$")
#: ``{ref}`name``` only: ``{ref}`text <name>``` carries its own title.
BARE_REF = re.compile(r"\{ref\}`([^`<>]+)`")


def labels() -> dict[str, tuple[Path, int, bool]]:
    """``{label: (file, line, above a heading)}`` of every docs page."""
    found: dict[str, tuple[Path, int, bool]] = {}
    for page in sorted(DOCS.rglob("*.md")):
        if "_build" in page.parts:
            continue
        lines = page.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            match = LABEL.match(line)
            if not match:
                continue
            following = next((x for x in lines[number + 1 :] if x.strip()), "")
            found[match.group(1)] = (page, number + 1, following.startswith("#"))
    return found


def main(argv: list[str]) -> int:
    known = labels()
    pages = [Path(arg) for arg in argv] or sorted(DOCS.rglob("*.md"))
    problems = []
    for page in pages:
        if page.suffix != ".md" or not page.exists():
            continue
        text = page.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for match in BARE_REF.finditer(line):
                name = match.group(1).strip()
                target = known.get(name)
                if target is not None and not target[2]:
                    where = f"{target[0].relative_to(DOCS.parent)}:{target[1]}"
                    problems.append(
                        f"{page}:{number}: {{ref}}`{name}` points at a label above "
                        f"no heading ({where}); write {{ref}}`some text <{name}>`"
                    )
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
