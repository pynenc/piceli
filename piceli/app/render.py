"""Render a typed app or a composition function to manifests, without a cluster.

Used by ``piceli render``. Secret inputs are replaced by placeholder versions
(the same scheme ``piceli release plan`` uses for its preview), so rendering
never reads or generates a secret value.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from piceli.app.app import App
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.ops.secret_versions import SecretVersionRef


class RenderError(ValueError):
    """The target cannot be loaded or rendered."""


def load_target(entry: str, base: Path) -> Any:
    """Import ``module:attr`` or ``path/to/file.py:attr`` (``attr`` may be dotted).

    A file target can import the modules and packages next to it (such as
    generated CRD models): its directory is appended to ``sys.path``, after
    every installed package, so a sibling never shadows one.
    """
    target, sep, attribute = entry.rpartition(":")
    if not sep or not target or not attribute:
        raise RenderError(f"target must be module:attr or file.py:attr, got {entry!r}")
    if target.endswith(".py") or "/" in target:
        path = (base / target).resolve()
        if not path.is_file():
            raise RenderError(f"file not found: {path}")
        name = "_piceli_render_" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise RenderError(f"cannot import {path}")
            module = importlib.util.module_from_spec(spec)
            if str(path.parent) not in sys.path:
                sys.path.append(str(path.parent))
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                del sys.modules[name]
                raise
    else:
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        try:
            module = importlib.import_module(target)
        except ImportError as error:
            raise RenderError(f"cannot import module {target!r}: {error}") from None
    value: Any = module
    for part in attribute.split("."):
        if not hasattr(value, part):
            raise RenderError(f"{entry!r}: no attribute {part!r}")
        value = getattr(value, part)
    return value


def placeholder_inputs(names: list[str]) -> dict[str, SecretVersionRef]:
    """Deterministic, value-free secret versions for each declared input name."""
    return {
        name: SecretVersionRef("0" * 32, hashlib.sha256(name.encode()).hexdigest()[:32])
        for name in names
    }


def spec_context(spec_path: Path, namespace: str | None = None) -> tuple[Any, Any]:
    """A ``ReleaseContext`` from a spec, with placeholder secret inputs.

    Images come from the spec (and its receipts, read locally), nodes from
    ``[target.nodes]`` as declared (not verified against a cluster).
    Returns ``(spec, context)``.
    """
    from piceli.k8s.release_secrets import input_names
    from piceli.k8s.release_spec import NodeRef, ReleaseSpec

    spec = ReleaseSpec.from_toml(spec_path)
    names = [
        item
        for name, generator in spec.model.secrets.items()
        for item in input_names(name, generator)
    ]
    nodes = {
        alias: NodeRef(node.name, node.uid or "")
        for alias, node in spec.model.target.nodes.items()
    }
    context = spec.context(spec.images(), placeholder_inputs(names), nodes)
    if namespace is not None:
        from dataclasses import replace

        context = replace(context, namespace=namespace)
    return spec, context


def empty_context(namespace: str) -> Any:
    """A ``ReleaseContext`` with no images, secrets, values or nodes."""
    from piceli.k8s.release_spec import ReleaseContext

    empty: Mapping[str, Any] = MappingProxyType({})
    return ReleaseContext(namespace=namespace, images=empty, secrets=empty)


def environment_namespace(target: Any, env: str | None) -> str | None:
    """The ``namespace`` the environment ``env`` of an App target declares."""
    if env is None or not isinstance(target, App):
        return None
    for item in target.environments:
        if item.name == env:
            return item.namespace
    return None


def _for_environment(app: App, env: str | None) -> App:
    return app if env is None else app.for_environment(env)


def render_target(
    target: Any, context: Any, env: str | None = None
) -> DeploymentComposition:
    """Turn an App, a composition, or a function of the context into a composition.

    See :func:`render_app_target`, which also returns the rendered App.
    """
    return render_app_target(target, context, env)[0]


def render_app_target(
    target: Any, context: Any, env: str | None = None
) -> tuple[DeploymentComposition, App | None]:
    """``(composition, app)`` of an App, a composition, or a function of the context.

    ``env`` selects an environment of the App (the target itself, or the one
    the function returns); ``app`` is the rendered App (``None`` for a
    composition).

    :raises EnvironmentInvalid: ``environment-unknown``/``environment-invalid``
        for the App's environments, ``environment-unsupported`` when ``env``
        is given and the target yields no App.
    """
    from piceli.app.environment import EnvironmentInvalid

    value = target
    if callable(value) and not isinstance(value, App | DeploymentComposition):
        value = value(context)
    if isinstance(value, App):
        app = _for_environment(value, env)
        return app.composition(context), app
    if isinstance(value, DeploymentComposition) and env is None:
        return value, None
    if env is not None and isinstance(value, DeploymentComposition):
        raise EnvironmentInvalid(
            "--env selects an environment of an App; this target renders a "
            "DeploymentComposition (return the App instead)",
            "environment-unsupported",
        )
    raise RenderError(
        "the target must be an App, a DeploymentComposition, or a function of "
        f"the release context returning one; got {type(value).__name__}"
    )


def rendered(
    composition: DeploymentComposition, inputs: Mapping[str, SecretVersionRef]
) -> list[dict[str, Any]]:
    """Public component documents: redacted manifests and secret binding names."""
    names = {ref: name for name, ref in inputs.items()}
    return [
        {
            "name": component.name,
            "dependencies": list(component.dependencies),
            "resources": [
                {
                    "manifest": resource.redacted_manifest(),
                    "secret_bindings": [
                        {
                            "pointer": binding.json_pointer,
                            "input": names.get(binding.reference),
                        }
                        for binding in resource.secret_bindings
                    ],
                }
                for resource in component.resources
            ],
        }
        for component in composition.components
    ]


def to_yaml(components: list[dict[str, Any]]) -> str:
    """Multi-document YAML, one document per object, grouped by component."""
    import yaml

    documents = []
    for component in components:
        depends = ", ".join(component["dependencies"]) or "-"
        for resource in component["resources"]:
            header = f"# component: {component['name']} (depends on: {depends})\n"
            documents.append(
                header + yaml.safe_dump(resource["manifest"], sort_keys=True)
            )
    return "---\n" + "---\n".join(documents) if documents else ""


def to_json(
    namespace: str, components: list[dict[str, Any]], environment: str | None = None
) -> str:
    body: dict[str, Any] = {
        "state": "rendered",
        "namespace": namespace,
        "components": components,
    }
    if environment is not None:  # added in 0.7.0, only with --env
        body["environment"] = environment
    return json.dumps(body, indent=2, sort_keys=True)
