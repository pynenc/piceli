"""Facts about the node a build targets, read from the cluster at plan time.

A host build (:mod:`piceli.artifacts.host_build`) compiles for one node. What
the compiler must know about that node, its CPU architecture and its kernel
page size, is read from the Kubernetes ``Node`` object when the pipeline
plans, recorded in the plan and covered by its hash. A node that changes (a
new kernel with another page size) therefore changes the plan.

The API server reports ``status.nodeInfo.architecture`` and
``kernelVersion``, not the page size. The page size is taken, in order, from:

1. the node label ``piceli.io/page-size`` (``4096``, ``16384`` or ``65536``),
   which an administrator sets once after checking ``getconf PAGESIZE`` on
   the node;
2. the kernel release: a ``16k`` or ``64k`` suffix (Asahi, Ubuntu and RHEL
   large-page kernels) or the Raspberry Pi 5 kernel (``rpi-2712``, 16 KiB);
3. the architecture: ``amd64`` is always 4 KiB, and ``arm64`` kernels without
   such a marker use 4 KiB.

The plan records which rule gave the value (``page_size_source``). Importing
this module runs nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

PAGE_SIZE_LABEL = "piceli.io/page-size"
PAGE_SIZES = (4096, 16384, 65536)
_ARCH = {"arm64": "arm64", "aarch64": "arm64", "amd64": "amd64", "x86_64": "amd64"}
_RUST_ARCH = {"arm64": "aarch64", "amd64": "x86_64"}
_KERNEL = re.compile(r"[A-Za-z0-9._+~-]{1,128}")
_NODE = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_LARGE_PAGES = (
    (re.compile(r"(?<![0-9a-z])64k(?![0-9a-z])", re.IGNORECASE), 65536),
    (re.compile(r"(?<![0-9a-z])16k(?![0-9a-z])", re.IGNORECASE), 16384),
    (re.compile(r"rpi-2712"), 16384),
)
#: Names usable as ``{name}`` in a host build's commands, env and paths.
PLACEHOLDERS = (
    "platform",
    "arch",
    "rust_arch",
    "page_size",
    "page_size_log2",
)


class NodeFactsError(ValueError):
    """The node's facts are missing or contradictory. ``code`` is registered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NodeFacts:
    """What a build needs to know about its target node.

    :param node: The node name.
    :param os: ``status.nodeInfo.operatingSystem`` (``linux``).
    :param architecture: ``amd64`` or ``arm64``.
    :param kernel_version: ``status.nodeInfo.kernelVersion``.
    :param page_size: Kernel page size in bytes.
    :param page_size_source: ``label``, ``kernel`` or ``architecture``.
    """

    node: str
    os: str
    architecture: str
    kernel_version: str
    page_size: int
    page_size_source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.node, str)
            or not _NODE.fullmatch(self.node)
            or self.os != "linux"
            or self.architecture not in _RUST_ARCH
            or not isinstance(self.kernel_version, str)
            or not _KERNEL.fullmatch(self.kernel_version)
            or self.page_size not in PAGE_SIZES
            or self.page_size_source not in {"label", "kernel", "architecture"}
        ):
            raise NodeFactsError("node-facts-unavailable", "invalid node facts")

    @property
    def platform(self) -> str:
        return f"{self.os}/{self.architecture}"

    @property
    def rust_arch(self) -> str:
        return _RUST_ARCH[self.architecture]

    @property
    def page_size_log2(self) -> int:
        return self.page_size.bit_length() - 1

    def placeholders(self) -> dict[str, str]:
        """The ``{name}`` substitutions a host build may use."""
        return {
            "platform": self.platform,
            "arch": self.architecture,
            "rust_arch": self.rust_arch,
            "page_size": str(self.page_size),
            "page_size_log2": str(self.page_size_log2),
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> NodeFacts:
        if not isinstance(value, Mapping) or set(value) != {
            "node",
            "os",
            "architecture",
            "kernel_version",
            "page_size",
            "page_size_source",
        }:
            raise NodeFactsError("node-facts-unavailable", "invalid node facts")
        return cls(**dict(value))

    @classmethod
    def from_node(cls, node: Mapping[str, Any]) -> NodeFacts:
        """Facts from a ``Node`` object as the API server returns it."""
        metadata = node.get("metadata") if isinstance(node, Mapping) else None
        status = node.get("status") if isinstance(node, Mapping) else None
        info = status.get("nodeInfo") if isinstance(status, Mapping) else None
        if not isinstance(metadata, Mapping) or not isinstance(info, Mapping):
            raise NodeFactsError(
                "node-facts-unavailable", "the node reports no nodeInfo"
            )
        name = metadata.get("name")
        labels = metadata.get("labels") or {}
        system = info.get("operatingSystem") or "linux"
        arch = _ARCH.get(str(info.get("architecture")))
        kernel = info.get("kernelVersion")
        if not isinstance(name, str) or arch is None or not isinstance(kernel, str):
            raise NodeFactsError(
                "node-facts-unavailable",
                "the node reports no supported architecture or kernel version",
            )
        if not isinstance(labels, Mapping):
            labels = {}
        size, source = page_size(arch, kernel, labels.get(PAGE_SIZE_LABEL))
        return cls(name, str(system), arch, kernel, size, source)


def page_size(arch: str, kernel: str, label: Any = None) -> tuple[int, str]:
    """``(page size, rule)`` for a node; see the module docstring."""
    if label is not None:
        text = str(label)
        if not text.isdigit() or int(text) not in PAGE_SIZES:
            raise NodeFactsError(
                "node-page-size-invalid",
                f"label {PAGE_SIZE_LABEL} must be one of "
                f"{', '.join(str(item) for item in PAGE_SIZES)}",
            )
        return int(text), "label"
    if arch == "arm64":
        for pattern, size in _LARGE_PAGES:
            if pattern.search(kernel):
                return size, "kernel"
    return 4096, "architecture"
