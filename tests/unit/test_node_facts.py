"""Target node facts (architecture, kernel page size) for host builds."""

from __future__ import annotations

from typing import Any

import pytest

from piceli.artifacts.node_facts import (
    PAGE_SIZE_LABEL,
    NodeFacts,
    NodeFactsError,
    page_size,
)


def node(
    arch: str = "arm64",
    kernel: str = "6.8.0-45-generic",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "metadata": {"name": "worker-1", "labels": labels or {}},
        "status": {
            "nodeInfo": {
                "operatingSystem": "linux",
                "architecture": arch,
                "kernelVersion": kernel,
            }
        },
    }


@pytest.mark.parametrize(
    ("arch", "kernel", "expected"),
    [
        ("amd64", "6.8.0-45-generic", (4096, "architecture")),
        ("amd64", "6.8.0-64k", (4096, "architecture")),  # x86 is always 4 KiB
        ("arm64", "6.8.0-45-generic", (4096, "architecture")),
        ("arm64", "6.6.51+rpt-rpi-2712", (16384, "kernel")),
        ("arm64", "6.6.51+rpt-rpi-v8", (4096, "architecture")),
        ("arm64", "6.11.3-400.asahi.fc40.aarch64+16k", (16384, "kernel")),
        ("arm64", "6.8.0-1012-generic-64k", (65536, "kernel")),
        ("arm64", "5.14.0-427.el9.aarch64+64k", (65536, "kernel")),
    ],
)
def test_page_size_follows_the_kernel_release(
    arch: str, kernel: str, expected: tuple[int, str]
) -> None:
    assert page_size(arch, kernel) == expected


def test_the_label_wins_and_is_validated() -> None:
    facts = NodeFacts.from_node(node(labels={PAGE_SIZE_LABEL: "16384"}))
    assert (facts.page_size, facts.page_size_source) == (16384, "label")
    with pytest.raises(NodeFactsError) as error:
        NodeFacts.from_node(node(labels={PAGE_SIZE_LABEL: "8192"}))
    assert error.value.code == "node-page-size-invalid"


def test_facts_give_the_build_placeholders() -> None:
    facts = NodeFacts.from_node(node(kernel="6.6.51+rpt-rpi-2712"))
    assert facts.platform == "linux/arm64"
    assert facts.placeholders() == {
        "platform": "linux/arm64",
        "arch": "arm64",
        "rust_arch": "aarch64",
        "page_size": "16384",
        "page_size_log2": "14",
    }
    assert NodeFacts.from_dict(facts.to_dict()) == facts


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"metadata": {"name": "n"}, "status": {}},
        node(arch="s390x"),
        node(kernel="bad kernel with spaces"),
    ],
)
def test_unsupported_nodes_are_refused(document: dict[str, Any]) -> None:
    with pytest.raises(NodeFactsError) as error:
        NodeFacts.from_node(document)
    assert error.value.code == "node-facts-unavailable"
