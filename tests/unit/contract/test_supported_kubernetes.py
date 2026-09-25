"""The published Kubernetes range is exactly what CI tests on kind."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
NODES = json.loads((ROOT / ".github" / "kind-nodes.json").read_text())


def test_ci_tests_the_last_four_minors_pinned_by_digest() -> None:
    nodes = NODES["nodes"]
    minors = [tuple(int(part) for part in node["minor"].split(".")) for node in nodes]
    assert len(nodes) == 4
    # Newest first (pull requests test nodes[0]), consecutive minors.
    assert minors == [(1, minors[0][1] - offset) for offset in range(4)]
    for node in nodes:
        assert re.fullmatch(
            rf"kindest/node:v{re.escape(node['minor'])}\.\d+@sha256:[0-9a-f]{{64}}",
            node["image"],
        ), node
        assert node["image"].split(":")[1].split("@")[0] == node["kubectl"]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", NODES["kind"])


def test_docs_and_readme_state_the_tested_range() -> None:
    page = (ROOT / "docs" / "compatibility.md").read_text()
    rows = re.findall(r"^\| (1\.\d+) \| `(kindest/node:v[0-9.]+)` \|", page, re.M)
    assert rows == [
        (node["minor"], node["image"].split("@")[0]) for node in NODES["nodes"]
    ]
    assert f"(kind {NODES['kind']})" in page
    newest, oldest = NODES["nodes"][0]["minor"], NODES["nodes"][-1]["minor"]
    readme = (ROOT / "README.md").read_text()
    assert f"Kubernetes {oldest} to {newest}" in readme
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert ".github/kind-nodes.json" in workflow
