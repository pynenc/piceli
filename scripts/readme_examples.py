"""Keep the README quick start identical to the files the tests run.

A README code block that must stay executable is preceded by a marker comment
naming the file it mirrors::

    <!-- readme-example: examples/readme/app.py -->

    ```python
    ...
    ```

``check`` fails when a marked block differs from its file (a unit test and a
pre-commit hook run it); ``sync`` copies the files into the README. The files
themselves are run against the fake Kubernetes API by
``tests/acceptance/test_readme_quickstart.py``.

    python scripts/readme_examples.py check
    python scripts/readme_examples.py sync
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
MARKER = re.compile(
    r"<!-- readme-example: (?P<path>\S+) -->\n+```(?P<lang>\w+)\n(?P<body>.*?)^```",
    re.S | re.M,
)


def marked_examples(readme: Path = README) -> list[tuple[Path, str]]:
    """``(example file, README block body)`` for every marked block."""
    return [
        (ROOT / match["path"], match["body"])
        for match in MARKER.finditer(readme.read_text())
    ]


def check(readme: Path = README) -> list[str]:
    """One problem per marked block that is missing or differs from its file."""
    examples = marked_examples(readme)
    if not examples:
        return [f"{readme.name}: no <!-- readme-example: ... --> markers"]
    problems = []
    for path, body in examples:
        rel = path.relative_to(ROOT)
        if not path.is_file():
            problems.append(f"{readme.name}: {rel} does not exist")
        elif path.read_text() != body:
            problems.append(f"{readme.name}: block differs from {rel}; run `sync`")
    return problems


def sync(readme: Path = README) -> None:
    """Rewrite every marked README block from the file it mirrors."""

    def replace(match: re.Match[str]) -> str:
        body = (ROOT / match["path"]).read_text()
        marker = f"<!-- readme-example: {match['path']} -->"
        return f"{marker}\n\n```{match['lang']}\n{body}```"

    readme.write_text(MARKER.sub(replace, readme.read_text()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "sync"])
    if parser.parse_args().command == "sync":
        sync()
    problems = check()
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
