"""Check that the installed piceli is the version this skill documents.

    python scripts/check_install.py

Exit 0 when ``piceli --version`` matches the ``piceli-version`` of SKILL.md
and ``piceli help-json`` has every command and option the skill uses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from _piceli import command, run

#: Commands and options the skill's scripts rely on.
NEEDED = {
    "deploy": ("--plan", "--approve", "--approve-if-policy", "--resume", "--json"),
    "status": ("--json",),
    "explain": ("--json",),
    "release rollback": ("--spec", "--approve"),
}


def required_version() -> str:
    text = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text()
    match = re.search(r'piceli-version:\s*"([0-9]+\.[0-9]+)"', text)
    if match is None:
        raise SystemExit("SKILL.md has no metadata piceli-version")
    return match.group(1)


def main() -> int:
    wanted = required_version()
    status, document, _ = run("help-json", quiet=True)
    if status != 0:
        print(f"`{' '.join(command())} help-json` failed; is piceli installed?")
        return 1
    installed = str(document["version"])
    if ".".join(installed.split(".")[:2]) != wanted:
        print(f"piceli {installed} is installed; this skill documents {wanted}.x")
        print(f'install it with: pip install "piceli>={wanted},<{_next(wanted)}"')
        return 1

    def walk(node: dict) -> list[dict]:
        return [node, *(n for child in node.get("commands", ()) for n in walk(child))]

    commands = {node["path"]: node for node in walk(document["root"])}
    missing = [
        f"{name} {flag}"
        for name, flags in NEEDED.items()
        for flag in flags
        if name not in commands
        or flag not in {f for p in commands[name]["params"] for f in p.get("flags", ())}
    ]
    if missing:
        print("this piceli lacks: " + ", ".join(missing))
        return 1
    print(json.dumps({"piceli": installed, "skill": wanted, "ok": True}))
    return 0


def _next(version: str) -> str:
    major, minor = version.split(".")
    return f"{major}.{int(minor) + 1}"


if __name__ == "__main__":
    sys.exit(main())
