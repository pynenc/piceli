"""Code generation: typed Python models from Kubernetes schemas.

Maturity: **preview**. :mod:`piceli.codegen.crd` turns a
CustomResourceDefinition's OpenAPI v3 schema into a module of pydantic models
for :meth:`App.resource <piceli.app.App.resource>` (``piceli codegen crd``,
see ``docs/crds.md``).

Importing this package has no side effects.
"""

from piceli.codegen.crd import CodegenError, GeneratedModule, generate, load_crds

__all__ = ["CodegenError", "GeneratedModule", "generate", "load_crds"]
