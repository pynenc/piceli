"""Set the release version everywhere it is pinned: ``make release-bump VERSION=X.Y.Z``.

Rewrites ``pyproject.toml`` and ``uv.lock`` (Piceli's own entry), the agent
skill's pins (``skills/piceli/SKILL.md`` front matter and requirement line,
``skills/piceli/scripts/_piceli.py``) and the skill test's versions, then
refreshes the API-surface snapshot and the mock eval baselines
(``evals/baselines/<version>/``). It checks, without writing them, that the
changelog has a ``## Version X.Y.Z`` section and the README a ``What's new in
X.Y`` section. Run ``make docs-fresh`` afterwards (a docs build without
the cache).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _sub(path: str, pattern: str, replacement: str, count: int = 1) -> None:
    file = ROOT / path
    text = file.read_text()
    new, found = re.subn(pattern, replacement, text, count=count, flags=re.M)
    if not found:
        raise SystemExit(f"release-bump: {path}: no match for {pattern!r}")
    file.write_text(new)


def main(version: str) -> int:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise SystemExit("release-bump: VERSION is X.Y.Z")
    major, minor, patch = (int(part) for part in match.groups())
    short, after = f"{major}.{minor}", f"{major}.{minor + 1}"
    previous = f"{major}.{minor - 1}.0" if minor else f"{major}.{minor}.0"
    _sub("pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
    _sub(
        "uv.lock",
        r'(^name = "piceli"\nversion = )"[^"]+"',
        rf'\g<1>"{version}"',
    )
    _sub(
        "skills/piceli/SKILL.md",
        r'piceli-version: "[^"]+"',
        f'piceli-version: "{short}"',
    )
    _sub(
        "skills/piceli/SKILL.md",
        r"`piceli` \*\*[0-9.]+x\*\*, CPython 3\.12\+: `pip install \"piceli>=[0-9.]+,<[0-9.]+\"`",
        f'`piceli` **{short}.x**, CPython 3.12+: `pip install "piceli>={short},<{after}"`',
    )
    _sub(
        "skills/piceli/scripts/_piceli.py",
        r'pip install "piceli>=[0-9.]+,<[0-9.]+"',
        f'pip install "piceli>={short},<{after}"',
    )
    _sub(
        "tests/acceptance/test_agent_skill.py",
        r'(front_matter_problems\(text, )"[0-9.]+"(\) == \[\]\n'
        r'    assert harness\.front_matter_problems\(text, )"[0-9.]+"(\) != \[\]\n'
        r'    assert harness\.front_matter_problems\(text, )"[0-9.]+"',
        rf'\g<1>"{version}"\g<2>"{after}.0"\g<3>"{previous}"',
    )
    run = [sys.executable, "evals/run.py"]
    subprocess.run(["uv", "lock", "--offline"], cwd=ROOT, check=False)
    subprocess.run(["uv", "run", "--frozen", "python", *run[1:], "api-surface", "--write"],
                   cwd=ROOT, check=True)  # fmt: skip
    subprocess.run(
        ["uv", "run", "--frozen", "python", "evals/run.py", "run", "--model",
         "mock:reference", "--model", "mock:naive", "--execute", "--out",
         "evals/baselines"],
        cwd=ROOT, check=True,
    )  # fmt: skip
    missing = []
    if f"## Version {version}" not in (ROOT / "docs/changelog.md").read_text():
        missing.append(f"docs/changelog.md: a '## Version {version}' section")
    if f"## What's new in {short}" not in (ROOT / "README.md").read_text():
        missing.append(f'README.md: a "## What\'s new in {short}" section')
    for item in missing:
        print(f"release-bump: write {item}", file=sys.stderr)
    print(f"release-bump: {version} pinned; now run make docs-fresh", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_bump.py X.Y.Z")
    raise SystemExit(main(sys.argv[1]))
