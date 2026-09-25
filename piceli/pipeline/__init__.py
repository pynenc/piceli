"""Deploy a typed app from source with one command: ``piceli deploy MODULE:ATTR``.

Maturity: **preview** (the API may change before 1.0).

A :class:`Pipeline` names an :class:`~piceli.app.App`, a :class:`Target`,
optional :class:`Build` objects whose images the app uses as
``build["name"]``, a delivery strategy (:class:`NodeLoopbackRegistry`,
:class:`NodeImport` or :class:`Registry`), secret generators
(:class:`Secrets`) and post-deploy checks. ``piceli deploy`` runs
``inputs → build → deliver → plan → apply → checks`` as one journaled,
resumable run in which every stage is skipped when its content is unchanged.
See ``docs/deploy.md``.

Importing this package reads no file and contacts nothing; the runner and
its tools load on first use.
"""

from typing import TYPE_CHECKING, Any

from piceli.pipeline.checks import CheckContext, CheckReportLike, CheckRunner
from piceli.pipeline.errors import PipelineError
from piceli.pipeline.model import (
    STAGES,
    Build,
    ImageHandle,
    NodeImport,
    NodeLoopbackRegistry,
    Pipeline,
    Registry,
    Smoke,
    Target,
    TargetNode,
)
from piceli.pipeline.secrets import (
    AwsSecret,
    Random,
    Secrets,
    Sops,
    Static,
    Template,
    TlsCa,
    Vault,
)

if TYPE_CHECKING:
    from piceli.pipeline.runner import CombinedPlan, PipelineRunner

__all__ = [
    "STAGES",
    "AwsSecret",
    "Build",
    "CheckContext",
    "CheckReportLike",
    "CheckRunner",
    "CombinedPlan",
    "ImageHandle",
    "NodeImport",
    "NodeLoopbackRegistry",
    "Pipeline",
    "PipelineError",
    "PipelineRunner",
    "Random",
    "Registry",
    "Secrets",
    "Smoke",
    "Sops",
    "Static",
    "Target",
    "TargetNode",
    "Template",
    "TlsCa",
    "Vault",
]


def __getattr__(name: str) -> Any:
    if name in {"PipelineRunner", "CombinedPlan"}:
        from piceli.pipeline import runner

        return getattr(runner, name)
    raise AttributeError(f"module 'piceli.pipeline' has no attribute {name!r}")
