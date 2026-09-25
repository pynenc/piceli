"""Generate pydantic models from a CustomResourceDefinition (``piceli codegen crd``).

Kubernetes requires every served CRD version to have a *structural* OpenAPI
v3 schema: each node states its ``type``, objects list their ``properties``
(or ``additionalProperties`` for maps), arrays their ``items``, and the only
extensions are ``x-kubernetes-int-or-string``,
``x-kubernetes-preserve-unknown-fields``, ``x-kubernetes-embedded-resource``
and list/map hints. That small language maps directly onto pydantic, so this
module is a small in-house generator rather than a general OpenAPI tool:

* the output depends only on the schema (properties are sorted, class names
  come from the property path, no timestamps or tool versions), so the same
  CRD from a file or from a cluster generates byte-identical modules;
* it understands the Kubernetes extensions (``int | str``, open objects,
  embedded resources), which general JSON-schema generators do not;
* it adds no dependency to Piceli.

The generated module holds ``API_VERSION``, ``KIND``, ``SCOPE``, ``CRD`` and
``SCHEMA_SHA256`` constants and one frozen model per object in the schema;
the ``<Kind>Spec`` model carries the resource identity, so
:class:`~piceli.app.resource.Resource` checks that it is used with the right
``apiVersion`` and ``kind``. Fields are snake_case with the Kubernetes name
as alias (either spelling is accepted); unknown fields are refused, except
in objects the schema marks as open. Schema defaults are not copied: the API
server applies them.

Importing this module has no side effects.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Identifies the generator's output format; bumped when the output changes.
GENERATOR = "piceli-codegen-crd/1"
_CRD_API = "apiextensions.k8s.io/v1"
#: BaseModel attributes a field must not shadow (a fixed list, so the output
#: never depends on the installed pydantic).
_SHADOWED = frozenset(
    {
        "construct",
        "copy",
        "dict",
        "fields",
        "from_orm",
        "json",
        "parse_file",
        "parse_obj",
        "parse_raw",
        "schema",
        "schema_json",
        "update_forward_refs",
        "validate",
    }
)


class CodegenError(ValueError):
    """The input is not a usable CRD; ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GeneratedModule:
    """One generated module and what it describes."""

    source: str
    crd: str
    api_version: str
    kind: str
    scope: str
    version: str
    schema_sha256: str
    classes: tuple[str, ...]

    @property
    def module_name(self) -> str:
        """A suggested file stem: the kind in snake_case (``service_monitor``)."""
        return _snake(self.kind)

    def summary(self) -> dict[str, Any]:
        """Public facts for a JSON summary (never the source)."""
        return {
            "crd": self.crd,
            "api_version": self.api_version,
            "kind": self.kind,
            "scope": self.scope,
            "version": self.version,
            "schema_sha256": self.schema_sha256,
            "generator": GENERATOR,
            "classes": len(self.classes),
            "spec_class": f"{self.kind}Spec"
            if f"{self.kind}Spec" in self.classes
            else None,
            "source_sha256": "sha256:"
            + hashlib.sha256(self.source.encode()).hexdigest(),
        }


# ------------------------------------------------------------------ loading


def load_crds(text: str) -> list[dict[str, Any]]:
    """Every ``apiextensions.k8s.io/v1`` CRD in a YAML or JSON text.

    Accepts several documents and ``List`` objects.

    :raises CodegenError: ``crd-invalid`` when the text does not parse or
        holds no CRD.
    """
    import yaml

    try:
        documents = [item for item in yaml.safe_load_all(text) if item is not None]
    except yaml.YAMLError:
        raise CodegenError(
            "crd-invalid", "the input is not valid YAML or JSON"
        ) from None
    found: list[dict[str, Any]] = []
    for document in documents:
        items = (
            document.get("items")
            if isinstance(document, dict)
            and str(document.get("kind", "")).endswith("List")
            else [document]
        )
        for item in items or ():
            if (
                isinstance(item, dict)
                and item.get("kind") == "CustomResourceDefinition"
                and item.get("apiVersion") == _CRD_API
            ):
                found.append(item)
    if not found:
        raise CodegenError(
            "crd-invalid", f"no {_CRD_API} CustomResourceDefinition in the input"
        )
    return found


def select_crd(crds: Sequence[Mapping[str, Any]], name: str | None) -> dict[str, Any]:
    """The CRD named ``name`` (``plural.group`` or its kind), or the only one.

    :raises CodegenError: ``crd-not-found``.
    """
    names = sorted(str((crd.get("metadata") or {}).get("name")) for crd in crds)
    if name is None:
        if len(crds) == 1:
            return dict(crds[0])
        raise CodegenError(
            "crd-not-found", f"the input holds several CRDs; pass --crd NAME: {names}"
        )
    for crd in crds:
        kind = ((crd.get("spec") or {}).get("names") or {}).get("kind")
        if name in ((crd.get("metadata") or {}).get("name"), kind):
            return dict(crd)
    raise CodegenError("crd-not-found", f"no CRD named {name!r}; found: {names}")


def _version(crd: Mapping[str, Any], version: str | None) -> tuple[str, dict[str, Any]]:
    """``(name, openAPIV3Schema)`` of ``version``, else the storage version."""
    versions = [
        item
        for item in (crd.get("spec") or {}).get("versions") or ()
        if isinstance(item, dict)
    ]
    served = [item for item in versions if item.get("served")]
    if version is not None:
        chosen = next((item for item in versions if item.get("name") == version), None)
        if chosen is None:
            raise CodegenError(
                "crd-invalid",
                f"the CRD has no version {version!r}; versions: "
                f"{[item.get('name') for item in versions]}",
            )
    else:
        chosen = next((item for item in served if item.get("storage")), None) or (
            served[0] if served else None
        )
        if chosen is None:
            raise CodegenError("crd-invalid", "the CRD serves no version")
    schema = (chosen.get("schema") or {}).get("openAPIV3Schema")
    if not isinstance(schema, dict):
        raise CodegenError(
            "crd-invalid",
            f"version {chosen.get('name')!r} has no openAPIV3Schema (structural "
            "schemas are required by apiextensions.k8s.io/v1)",
        )
    return str(chosen["name"]), schema


# ------------------------------------------------------------------ naming


def _snake(name: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()


def _camel(name: str) -> str:
    parts = [part for part in re.split(r"[^0-9a-zA-Z]+", name) if part]
    text = "".join(part[0].upper() + part[1:] for part in parts)
    return text if text and not text[0].isdigit() else "F" + text


def _field_name(prop: str) -> str:
    name = _snake(prop) or "field"
    if name[0].isdigit() or name.startswith("model_"):
        name = "f_" + name
    if keyword.iskeyword(name) or name in _SHADOWED:
        name += "_"
    return name


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int | float):
        return repr(value)
    raise CodegenError("crd-invalid", f"unsupported enum value {value!r}")


def _paragraph(text: Any) -> str:
    """The first paragraph of a description, whitespace-normalized."""
    if not isinstance(text, str):
        return ""
    first = re.split(r"\n\s*\n", text.strip(), maxsplit=1)[0]
    return " ".join(first.split())


def _docstring(text: str, indent: str) -> list[str]:
    if not text:
        return []
    text = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if text.endswith('"'):
        text += " "
    lines = textwrap.wrap(
        text,
        width=max(40, 79 - len(indent)),
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(lines) == 1 and len(indent) + len(lines[0]) + 6 <= 88:
        return [f'{indent}"""{lines[0]}"""']
    return [
        f'{indent}"""{lines[0]}',
        *(f"{indent}{line}" for line in lines[1:]),
        f'{indent}"""',
    ]


# ---------------------------------------------------------------- the writer


@dataclass
class _Writer:
    blocks: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)

    def unique(self, name: str) -> str:
        candidate, number = name, 2
        while candidate in self.classes:
            candidate, number = f"{name}{number}", number + 1
        self.classes.append(candidate)
        return candidate

    def constrained(self, base: str, constraints: Mapping[str, Any]) -> str:
        if not constraints:
            return base
        self.imports.update({"Annotated", "Field"})
        args = ", ".join(
            f"{key}={_literal(value)}" for key, value in constraints.items()
        )
        return f"Annotated[{base}, Field({args})]"

    def type_of(self, schema: Mapping[str, Any], hint: str) -> tuple[str, bool]:
        """``(annotation, nullable)`` of one schema node."""
        nullable = bool(schema.get("nullable"))
        kind = schema.get("type")
        constraints: dict[str, Any] = {}
        if schema.get("x-kubernetes-int-or-string"):
            return "int | str", nullable
        if "enum" in schema and kind in ("string", "integer", "number", "boolean"):
            values = [value for value in schema["enum"] if value is not None]
            nullable = nullable or len(values) != len(schema["enum"])
            if values:
                self.imports.add("Literal")
                unique = list(dict.fromkeys(_literal(value) for value in values))
                return f"Literal[{', '.join(unique)}]", nullable
        if kind == "string":
            if isinstance(schema.get("minLength"), int):
                constraints["min_length"] = schema["minLength"]
            if isinstance(schema.get("maxLength"), int):
                constraints["max_length"] = schema["maxLength"]
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and _compiles(pattern):
                constraints["pattern"] = pattern
            return self.constrained("str", constraints), nullable
        if kind in ("integer", "number"):
            base = "int" if kind == "integer" else "float"
            for key, lower in (("minimum", True), ("maximum", False)):
                value = schema.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    exclusive = schema.get(
                        "exclusiveMinimum" if lower else "exclusiveMaximum"
                    )
                    name = (
                        ("gt" if lower else "lt")
                        if exclusive is True
                        else ("ge" if lower else "le")
                    )
                    constraints[name] = value
            return self.constrained(base, constraints), nullable
        if kind == "boolean":
            return "bool", nullable
        if kind == "array":
            items = schema.get("items")
            inner = (
                self.type_of(items, hint + "Item")
                if isinstance(items, Mapping)
                else ("Any", False)
            )
            if inner[0] == "Any":
                self.imports.add("Any")
            item = f"{inner[0]} | None" if inner[1] else inner[0]
            for key, name in (("minItems", "min_length"), ("maxItems", "max_length")):
                if isinstance(schema.get(key), int):
                    constraints[name] = schema[key]
            return self.constrained(f"list[{item}]", constraints), nullable
        if kind == "object" or "properties" in schema:
            properties = schema.get("properties")
            if isinstance(properties, Mapping) and properties:
                return self.model(schema, hint), nullable
            extra = schema.get("additionalProperties")
            for key, name in (
                ("minProperties", "min_length"),
                ("maxProperties", "max_length"),
            ):
                if isinstance(schema.get(key), int):
                    constraints[name] = schema[key]
            if isinstance(extra, Mapping):
                value, value_nullable = self.type_of(extra, hint + "Value")
                if value == "Any":
                    self.imports.add("Any")
                if value_nullable:
                    value += " | None"
                return self.constrained(f"dict[str, {value}]", constraints), nullable
            self.imports.add("Any")
            return self.constrained("dict[str, Any]", constraints), nullable
        self.imports.add("Any")
        return "Any", nullable

    def model(
        self,
        schema: Mapping[str, Any],
        hint: str,
        *,
        docstring: str | None = None,
        class_vars: Sequence[str] = (),
    ) -> str:
        """Write one model class (its nested models first); returns its name."""
        name = self.unique(hint)
        properties: Mapping[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        taken: set[str] = set()
        body: list[str] = []
        for prop in sorted(properties):
            node = properties[prop]
            if not isinstance(node, Mapping):
                continue
            annotation, nullable = self.type_of(node, name + _camel(prop))
            attribute = _field_name(prop)
            base, number = attribute, 2
            while attribute in taken:
                attribute, number = f"{base}_{number}", number + 1
            taken.add(attribute)
            optional = prop not in required
            if nullable or optional:
                annotation += " | None"
            arguments: list[str] = []
            if optional:
                arguments.append("default=None")
            if attribute != prop:
                arguments.append(f"alias={json.dumps(prop)}")
            if arguments == ["default=None"]:
                line = f"    {attribute}: {annotation} = None"
            elif arguments:
                self.imports.add("Field")
                line = f"    {attribute}: {annotation} = Field({', '.join(arguments)})"
            else:
                line = f"    {attribute}: {annotation}"
            body.append(line)
            body += _docstring(_paragraph(node.get("description")), "    ")
            body.append("")
        lines = [f"class {name}(_Model):"]
        lines += _docstring(
            docstring
            if docstring is not None
            else _paragraph(schema.get("description")),
            "    ",
        ) or ['    """(No description in the schema.)"""']
        lines.append("")
        if schema.get("x-kubernetes-preserve-unknown-fields"):
            lines += ['    model_config = ConfigDict(extra="allow")', ""]
        if class_vars:
            self.imports.add("ClassVar")
            lines += [*class_vars, ""]
        lines += body
        while lines[-1] == "":
            lines.pop()
        self.blocks.append("\n".join(lines))
        return name


def _compiles(pattern: str) -> bool:
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def schema_digest(schema: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON of a schema."""
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def generate(crd: Mapping[str, Any], *, version: str | None = None) -> GeneratedModule:
    """Generate the models of one CRD version (default: the storage version).

    :raises CodegenError: ``crd-invalid`` for a CRD without group, kind or a
        structural schema, or an unknown ``version``.
    """
    spec = crd.get("spec") if isinstance(crd.get("spec"), Mapping) else {}
    assert isinstance(spec, Mapping)
    group = spec.get("group")
    kind = (spec.get("names") or {}).get("kind")
    crd_name = (crd.get("metadata") or {}).get("name")
    if not isinstance(group, str) or not group or not isinstance(kind, str):
        raise CodegenError(
            "crd-invalid", "the CRD has no spec.group or spec.names.kind"
        )
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", kind):
        raise CodegenError("crd-invalid", f"unsupported kind name {kind!r}")
    scope = "cluster" if spec.get("scope") == "Cluster" else "namespaced"
    name, schema = _version(crd, version)
    api_version = f"{group}/{name}"
    digest = schema_digest(schema)
    writer = _Writer()
    root = (schema.get("properties") or {}).get("spec")
    if isinstance(root, Mapping):
        if not (isinstance(root.get("properties"), Mapping) and root["properties"]):
            # An open or map-shaped spec still gets a named model to type-check.
            root = {
                **root,
                "properties": {},
                "x-kubernetes-preserve-unknown-fields": True,
            }
        writer.model(
            root,
            f"{kind}Spec",
            docstring=_paragraph(root.get("description"))
            or f"The spec of a {kind} ({api_version}).",
            class_vars=(
                "    piceli_api_version: ClassVar[str] = API_VERSION",
                "    piceli_kind: ClassVar[str] = KIND",
                "    piceli_scope: ClassVar[str] = SCOPE",
            ),
        )
    typing_names = sorted(writer.imports & {"Annotated", "Any", "ClassVar", "Literal"})
    pydantic_names = sorted({"BaseModel", "ConfigDict"} | (writer.imports & {"Field"}))
    spec_class = f"{kind}Spec"
    usage = (
        f"Use with ``app.resource(API_VERSION, KIND, name, {spec_class}(...))``."
        if writer.blocks
        else "The CRD has no spec schema; declare its body with ``app.resource``."
    )
    header = [
        f'"""Typed models for the ``{kind}`` custom resource (``{api_version}``).',
        "",
        "Generated by ``piceli codegen crd``; do not edit. Source:",
        f"CustomResourceDefinition ``{crd_name}``, version ``{name}``.",
        "The output depends only on the schema: the same CRD, from a file or",
        "from a cluster, always generates this file.",
        "",
        usage,
        '"""',
        "",
        "# ruff: noqa",
        "",
        "from __future__ import annotations",
        "",
    ]
    if typing_names:
        header.append(f"from typing import {', '.join(typing_names)}")
        header.append("")
    header += [
        f"from pydantic import {', '.join(pydantic_names)}",
        "",
        f"API_VERSION = {json.dumps(api_version)}",
        f"KIND = {json.dumps(kind)}",
        f"SCOPE = {json.dumps(scope)}",
        f"CRD = {json.dumps(crd_name)}",
        f"SCHEMA_SHA256 = {json.dumps(digest)}",
        f"GENERATOR = {json.dumps(GENERATOR)}",
        "",
        "",
        "class _Model(BaseModel):",
        '    """Frozen; accepts Kubernetes names and snake_case; refuses unknown fields."""',
        "",
        "    model_config = ConfigDict(",
        '        extra="forbid", frozen=True, populate_by_name=True, regex_engine="python-re"',
        "    )",
    ]
    source = (
        "\n".join(header) + "".join("\n\n\n" + block for block in writer.blocks) + "\n"
    )
    return GeneratedModule(
        source=source,
        crd=str(crd_name),
        api_version=api_version,
        kind=kind,
        scope=scope,
        version=name,
        schema_sha256=digest,
        classes=tuple(writer.classes),
    )
