"""Environments: typed overrides that turn one App into dev, staging and prod.

An :class:`Environment` names the values that differ between environments
(replicas, images, container resources, config values, hosts, node
selectors, resource specs and which components are enabled). It is declared
on the app with :meth:`App.environment <piceli.app.App.environment>` and
applied with :meth:`App.for_environment <piceli.app.App.for_environment>`,
which returns a new app; the declaring app never changes. ``piceli render
--env``, ``piceli deploy --env`` and ``piceli release … --env`` select one.
See ``docs/environments.md``.

Importing this module has no side effects.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    field_validator,
)

from piceli.app.model import Config, Labels, Name, Resources
from piceli.app.resource import Resource, spec_json

if TYPE_CHECKING:
    from piceli.app.app import App, Declared

Image = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
Host = Annotated[
    str,
    Field(
        pattern=r"^(\*\.)?[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
        max_length=253,
    ),
]


class EnvironmentInvalid(ValueError):
    """An environment names an unknown object or a value the object refuses.

    ``code`` is a registered error code (``environment-invalid``,
    ``environment-unknown``).
    """

    def __init__(self, message: str, code: str = "environment-invalid") -> None:
        super().__init__(message)
        self.code = code


class Environment(BaseModel):
    """Typed overrides of one environment, keyed by the objects they change.

    Keys name declared objects: ``"api"`` or, when two kinds share the name,
    ``"Deployment/api"``. A key that names nothing, or an object that cannot
    take the value, is refused (when the environment is declared on the app,
    and again when it is applied).

    :param name: Environment name, a DNS label (``dev``, ``staging``, ``prod``).
    :param namespace: Namespace ``piceli render --env`` uses when no
        ``--namespace`` or spec gives one. A pipeline deploys each environment
        to its own ``Target``, whose namespace wins.
    :param replicas: Workload → replicas.
    :param images: Workload → image of its main (first) container.
    :param resources: Workload → :class:`~piceli.app.model.Resources` of its
        main container (replaces them).
    :param config: Config → values merged into its data; ``None`` removes a key.
    :param hosts: Object with a ``hosts`` (or ``host``) field, such as a route
        or ingress → its host names.
    :param node_selector: Workload → node labels, merged key by key over the
        workload's own ``node_selector`` (the app's ``pod_defaults`` still
        apply underneath).
    :param specs: :class:`~piceli.app.resource.Resource` → its new ``spec``.
        When the declared spec is typed, a mapping is validated into the same
        model, and another model type is refused.
    :param enabled: Component → ``False`` leaves the whole component out
        (every object in it); ``True`` changes nothing. A workload that still
        reads a config, secret or service account of a disabled component is
        refused.

    Example::

        app.environment(
            "prod",
            namespace="shop-prod",
            replicas={"api": 3},
            resources={"api": Resources(cpu="500m", memory="512Mi")},
            config={"settings": {"LOG_LEVEL": "warning"}},
            enabled={"debug-tools": False},
        )
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: Name
    namespace: Name | None = None
    replicas: dict[str, NonNegativeInt] = Field(default_factory=dict)
    images: dict[str, Image] = Field(default_factory=dict)
    resources: dict[str, Resources] = Field(default_factory=dict)
    config: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    hosts: dict[str, tuple[Host, ...]] = Field(default_factory=dict)
    node_selector: dict[str, Labels] = Field(default_factory=dict)
    #: Resource → a pydantic model or a JSON mapping (checked by ``_specs``).
    specs: dict[str, Any] = Field(default_factory=dict)
    enabled: dict[str, bool] = Field(default_factory=dict)

    def __init__(self, name: str, /, **data: Any) -> None:
        super().__init__(name=name, **data)

    @field_validator("hosts", mode="before")
    @classmethod
    def _hosts(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: (item,) if isinstance(item, str) else item
                for key, item in value.items()
            }
        return value

    @field_validator("specs", mode="before")
    @classmethod
    def _specs(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        result: dict[str, Any] = {}
        for key, spec in value.items():
            if isinstance(spec, BaseModel):
                result[key] = spec
            elif isinstance(spec, Mapping):
                result[key] = spec_json(spec)
            else:
                raise ValueError(f"specs[{key!r}] must be a pydantic model or mapping")
        return result

    def values(self) -> dict[str, Any]:
        """The resolved overrides as plain JSON (sorted; empty ones left out).

        This is what a deploy plan hash covers besides the name, and what
        ``piceli render --diff-env`` compares.
        """
        result: dict[str, Any] = {}
        if self.namespace is not None:
            result["namespace"] = self.namespace
        for field in (
            "replicas",
            "images",
            "resources",
            "config",
            "hosts",
            "node_selector",
            "specs",
            "enabled",
        ):
            table = getattr(self, field)
            if not table:
                continue
            if field == "resources":
                value: Any = {
                    k: v.model_dump(exclude_none=True) for k, v in table.items()
                }
            elif field == "specs":
                value = {k: spec_json(v) for k, v in table.items()}
            elif field == "hosts":
                value = {k: list(v) for k, v in table.items()}
            else:
                value = table
            result[field] = json.loads(json.dumps(value, sort_keys=True))
        return dict(sorted(result.items()))

    def identity(self) -> dict[str, Any]:
        """``{"name", "values"}``: what an approval of an environment binds to."""
        return {"name": self.name, "values": self.values()}


# ------------------------------------------------------------------ applying


def _kind(item: Any) -> str:
    from piceli.app.app import kind_of

    return kind_of(item)


def _has(field: str) -> Callable[[Any], bool]:
    return lambda item: field in type(item).model_fields


def _match(
    objects: Sequence[Declared],
    env: Environment,
    table: str,
    key: str,
    accepts: Callable[[Any], bool],
    what: str,
) -> int:
    """Index of the one declared object ``key`` names that ``accepts`` it."""
    kind, _, name = key.rpartition("/")
    found = [
        index
        for index, item in enumerate(objects)
        if item.name == name and (not kind or _kind(item) == kind) and accepts(item)
    ]
    if len(found) == 1:
        return found[0]
    if found:
        kinds = sorted(f"{_kind(objects[i])}/{name}" for i in found)
        raise EnvironmentInvalid(
            f"environment {env.name!r}: {table}[{key!r}] is ambiguous; use one "
            f"of {kinds}"
        )
    known = sorted({f"{_kind(item)}/{item.name}" for item in objects if accepts(item)})
    raise EnvironmentInvalid(
        f"environment {env.name!r}: {table}[{key!r}] names no declared {what}; "
        f"declared: {known or 'none'}"
    )


def _updated(item: Any, **changes: Any) -> Any:
    """A validated copy of a frozen model with ``changes`` applied."""
    values = {name: getattr(item, name) for name in type(item).model_fields}
    values.update(changes)
    return type(item).model_validate(values)


def _main(item: Any, **changes: Any) -> Any:
    containers = tuple(item.containers)
    return _updated(
        item, containers=(_updated(containers[0], **changes), *containers[1:])
    )


def apply_environment(app: App, env: Environment) -> list[Declared]:
    """The app's objects with ``env`` applied (the app is not changed).

    :raises EnvironmentInvalid: on a key that names no suitable object, or a
        value the object refuses (the message names both).
    """
    from pydantic import ValidationError

    objects: list[Any] = list(app.objects)
    workload = _has("containers")

    def change(
        table: str,
        accepts: Callable[[Any], bool],
        what: str,
        update: Callable[[Any, Any], Any],
    ) -> None:
        for key, value in getattr(env, table).items():
            index = _match(objects, env, table, key, accepts, what)
            try:
                objects[index] = update(objects[index], value)
            except (ValidationError, ValueError) as error:
                raise EnvironmentInvalid(
                    f"environment {env.name!r}: {table}[{key!r}]: {error}"
                ) from None

    # An autoscaler owns its target's replicas: refuse instead of dropping them.
    scaled = {
        (item.target_kind, item.target)
        for item in objects
        if type(item).__name__ == "Autoscaler"
    }

    def replicas(item: Any, value: int) -> Any:
        if (_kind(item), item.name) in scaled:
            raise ValueError(
                f"{_kind(item)} {item.name!r} is autoscaled; set the autoscaler's "
                "min_replicas/max_replicas instead"
            )
        return _updated(item, replicas=value)

    change("replicas", _has("replicas"), "workload", replicas)
    change("images", workload, "workload", lambda o, v: _main(o, image=v))
    change("resources", workload, "workload", lambda o, v: _main(o, resources=v))
    change(
        "node_selector",
        _has("node_selector"),
        "workload",
        lambda o, v: _updated(o, node_selector={**(o.node_selector or {}), **v}),
    )

    def config(item: Config, value: Mapping[str, str | None]) -> Config:
        data = dict(item.data)
        for name, text in value.items():
            if text is None:
                data.pop(name, None)
            else:
                data[name] = text
        return _updated(item, data=data)

    change("config", lambda o: isinstance(o, Config), "config", config)

    def hosts(item: Any, value: tuple[str, ...]) -> Any:
        fields = type(item).model_fields
        if "hosts" in fields:
            return _updated(item, hosts=value)
        if len(value) != 1:
            raise ValueError(f"{_kind(item)} {item.name!r} takes exactly one host")
        return _updated(item, host=value[0])

    change(
        "hosts",
        lambda o: bool({"hosts", "host"} & set(type(o).model_fields)),
        "object with hosts",
        hosts,
    )

    def spec(item: Resource, value: BaseModel | dict[str, Any]) -> Resource:
        declared = item.spec
        if isinstance(declared, BaseModel):
            if isinstance(value, BaseModel) and not isinstance(value, type(declared)):
                raise ValueError(
                    f"the spec must be a {type(declared).__name__}, got "
                    f"{type(value).__name__}"
                )
            if not isinstance(value, BaseModel):
                value = type(declared).model_validate(value)
        return _updated(item, spec=value)

    change("specs", lambda o: isinstance(o, Resource), "resource", spec)
    return _enabled(app, env, objects)


def _enabled(app: App, env: Environment, objects: list[Any]) -> list[Any]:
    """``objects`` without the components ``env`` disables."""
    components = {item.component_name for item in objects} | app.added_components()
    unknown = sorted(set(env.enabled) - components)
    if unknown:
        raise EnvironmentInvalid(
            f"environment {env.name!r}: enabled names unknown components "
            f"{unknown}; components: {sorted(components)}"
        )
    disabled = {name for name, on in env.enabled.items() if not on}
    kept = [item for item in objects if item.component_name not in disabled]
    gone = {
        (_kind(item), item.name) for item in objects if item.component_name in disabled
    }
    for item in kept:
        references = getattr(item, "references", None)
        if not callable(references):
            continue
        for ref in sorted(references() & gone):
            raise EnvironmentInvalid(
                f"environment {env.name!r}: {_kind(item)} {item.name!r} reads "
                f"{ref[0]} {ref[1]!r}, whose component is disabled"
            )
    return kept


def disabled_components(env: Environment | None) -> set[str]:
    """Components ``env`` leaves out (empty without an environment)."""
    if env is None:
        return set()
    return {name for name, on in env.enabled.items() if not on}


# ------------------------------------------------------------------- diffing


def _named(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    names = [item.get("name") if isinstance(item, dict) else None for item in value]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(
        names
    ):
        return None
    return names  # type: ignore[return-value]


def field_changes(a: Any, b: Any, path: str = "") -> list[dict[str, Any]]:
    """Field-level differences between two JSON values.

    Each change is ``{"path", "a", "b"}``; a value missing on one side is
    absent from the change (not ``null``). Paths are dotted; list items with a
    unique ``name`` are matched by name (``containers[api].image``), other
    lists by position.
    """
    if a == b:
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(a) | set(b)):
            where = f"{path}.{key}" if path else str(key)
            if key not in b:
                changes.append({"path": where, "a": a[key]})
            elif key not in a:
                changes.append({"path": where, "b": b[key]})
            else:
                changes += field_changes(a[key], b[key], where)
        return changes
    names_a, names_b = _named(a), _named(b)
    if names_a is not None and names_b is not None:
        by_a = dict(zip(names_a, a, strict=True))
        by_b = dict(zip(names_b, b, strict=True))
        changes = []
        for name in [*names_a, *(n for n in names_b if n not in by_a)]:
            where = f"{path}[{name}]"
            if name not in by_b:
                changes.append({"path": where, "a": by_a[name]})
            elif name not in by_a:
                changes.append({"path": where, "b": by_b[name]})
            else:
                changes += field_changes(by_a[name], by_b[name], where)
        return changes
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        changes = []
        for index, (left, right) in enumerate(zip(a, b, strict=True)):
            changes += field_changes(left, right, f"{path}[{index}]")
        return changes
    return [{"path": path or ".", "a": a, "b": b}]


def _objects(components: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for component in components:
        for resource in component["resources"]:
            manifest = json.loads(json.dumps(resource["manifest"]))
            metadata = manifest.get("metadata") or {}
            metadata.pop("namespace", None)
            annotations = metadata.get("annotations") or {}
            annotations.pop("piceli.io/namespace", None)
            if not annotations:
                metadata.pop("annotations", None)
            key = f"{manifest['apiVersion']}/{manifest['kind']}/{metadata['name']}"
            result[key] = {
                "api_version": manifest["apiVersion"],
                "kind": manifest["kind"],
                "name": metadata["name"],
                "component": component["name"],
                "manifest": manifest,
            }
    return result


def environment_diff(
    first: tuple[str, str, Environment | None, Sequence[Mapping[str, Any]]],
    second: tuple[str, str, Environment | None, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """The typed difference between two rendered environments.

    Each side is ``(name, namespace, environment, rendered components)``, as
    :func:`piceli.app.render.rendered` returns them. Manifests are compared
    without their namespace (reported under ``namespaces``), so only real
    differences show.
    """
    name_a, namespace_a, env_a, components_a = first
    name_b, namespace_b, env_b, components_b = second
    values_a = env_a.values() if env_a is not None else {}
    values_b = env_b.values() if env_b is not None else {}
    for key in set(values_a) ^ set(values_b):  # compare tables entry by entry
        if key != "namespace":
            values_a.setdefault(key, {})
            values_b.setdefault(key, {})
    objects_a, objects_b = _objects(components_a), _objects(components_b)
    objects: list[dict[str, Any]] = []
    summary = {"changed": 0, "only-a": 0, "only-b": 0, "same": 0}
    for key in sorted(set(objects_a) | set(objects_b)):
        left, right = objects_a.get(key), objects_b.get(key)
        base = left or right
        assert base is not None
        entry: dict[str, Any] = {
            "api_version": base["api_version"],
            "kind": base["kind"],
            "name": base["name"],
            "component": base["component"],
        }
        if left is None:
            entry["change"] = "only-b"
        elif right is None:
            entry["change"] = "only-a"
        else:
            fields = field_changes(left["manifest"], right["manifest"])
            entry["change"] = "changed" if fields else "same"
            if fields:
                entry["fields"] = fields
        summary[entry["change"]] += 1
        if entry["change"] != "same":
            objects.append(entry)
    return {
        "environments": {"a": name_a, "b": name_b},
        "namespaces": {"a": namespace_a, "b": namespace_b},
        "values": field_changes(values_a, values_b),
        "objects": objects,
        "summary": summary,
    }


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 80 else text[:77] + "..."


def diff_text(diff: Mapping[str, Any]) -> str:
    """The human form of :func:`environment_diff`."""
    a, b = diff["environments"]["a"], diff["environments"]["b"]
    lines = [f"environments: {a} (a) -> {b} (b)"]
    if diff["namespaces"]["a"] != diff["namespaces"]["b"]:
        lines.append(
            f"namespace: {diff['namespaces']['a']} -> {diff['namespaces']['b']}"
        )
    if diff["values"]:
        lines.append("values:")
        lines += [_change_line(change) for change in diff["values"]]
    for item in diff["objects"]:
        where = f"{item['kind']}/{item['name']} (component {item['component']})"
        if item["change"] == "only-a":
            lines.append(f"- {where}: only in {a}")
        elif item["change"] == "only-b":
            lines.append(f"+ {where}: only in {b}")
        else:
            lines.append(f"~ {where}:")
            lines += [_change_line(change) for change in item["fields"]]
    counts = diff["summary"]
    lines.append(
        f"{counts['changed']} changed, {counts['only-a']} only in {a}, "
        f"{counts['only-b']} only in {b}, {counts['same']} identical"
    )
    return "\n".join(lines) + "\n"


def _change_line(change: Mapping[str, Any]) -> str:
    left = _short(change["a"]) if "a" in change else "(absent)"
    right = _short(change["b"]) if "b" in change else "(absent)"
    return f"    {change['path']}: {left} -> {right}"
