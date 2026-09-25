"""Piceli's public Python API and CLI, used to flag wrong or nonexistent API use.

The surface is introspected from the installed ``piceli`` package: the exports of
the public modules, the attributes, parameters and return types of their classes
and functions, every module name, and the CLI from ``piceli help-json`` (commands,
flags, error codes). A snapshot (``evals/api_surface.json``) is committed so a
baseline records what it was measured against; ``python evals/run.py api-surface
--write`` refreshes it and the harness tests fail when it is stale.
"""

from __future__ import annotations

import ast
import inspect
import json
import pkgutil
import re
import shlex
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).resolve().parent.parent / "api_surface.json"

# Modules whose exports are checked name by name. Other ``piceli.*`` modules are
# only checked for existence.
PUBLIC_MODULES = (
    "piceli",
    "piceli.app",
    "piceli.pipeline",
    "piceli.checks",
    "piceli.testing",
    "piceli.k8s.release_spec",
)
# ``**kwargs`` parameters whose allowed keys are the fields of a model.
KWARGS_FROM = {"App.environment": "Environment"}
_OBJECT_ATTRS = set(dir(object))


# ----------------------------------------------------------------- introspection


def _params(fn: Any) -> list[str] | None:
    """Parameter names, or ``None`` when the callable takes ``**kwargs``."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    names = []
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if name not in {"self", "cls"} and param.kind is not param.VAR_POSITIONAL:
            names.append(name)
    return sorted(names)


def _known(annotation: Any, classes: set[str]) -> str | None:
    """The first known class name in an annotation (``Iterator[X]`` gives ``X``)."""
    text = str(annotation) if isinstance(annotation, str) else repr(annotation)
    if isinstance(annotation, type):
        text = annotation.__name__
    if "tuple[" in text.lower():
        return None  # a tuple of values is not one of them
    for word in re.findall(r"[A-Za-z_]\w*", text):
        if word in classes:
            return str(word)
    return None


def _returns(fn: Any, classes: set[str]) -> str | None:
    try:
        annotation = inspect.signature(fn).return_annotation
    except (TypeError, ValueError):
        return None
    if annotation is inspect.Signature.empty:
        return None
    return _known(annotation, classes)


def _collect_classes() -> dict[str, type]:
    """Every public class reachable from the public modules and ``App``'s methods."""
    import importlib

    found: dict[str, type] = {}
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        for name in _exports(module):
            value = getattr(module, name, None)
            if isinstance(value, type) and value.__module__.startswith("piceli"):
                found.setdefault(value.__name__, value)
    # Classes only reached through return annotations (handles, builders).
    pending = list(found.values())
    while pending:
        cls = pending.pop()
        try:
            hints = typing.get_type_hints(cls)
        except Exception:
            hints = {}
        candidates = list(hints.values())
        for name, member in inspect.getmembers(cls):
            if name.startswith("_"):
                continue
            if isinstance(member, type):
                candidates.append(member)
            elif callable(member):
                try:
                    candidates.append(typing.get_type_hints(member).get("return"))
                except Exception:
                    pass
        for candidate in candidates:
            for arg in (candidate, *typing.get_args(candidate)):
                if (
                    isinstance(arg, type)
                    and arg.__module__.startswith("piceli")
                    and arg.__name__ not in found
                    and not arg.__name__.startswith("_")
                ):
                    found[arg.__name__] = arg
                    pending.append(arg)
    return found


def _exports(module: Any) -> list[str]:
    names = getattr(module, "__all__", None) or dir(module)
    return sorted(n for n in names if not n.startswith("_"))


def _describe_class(
    cls: type, names: set[str], pydantic_attrs: set[str]
) -> dict[str, Any]:
    is_model = False
    try:
        import pydantic

        is_model = issubclass(cls, pydantic.BaseModel)
    except ImportError:
        pass
    attrs = {a for a in dir(cls) if not a.startswith("_")}
    try:
        hints = typing.get_type_hints(cls)
    except Exception:
        hints = {}
    attrs |= {a for a in hints if not a.startswith("_")}
    if is_model:
        attrs -= pydantic_attrs
    attr_types = {}
    for attr in sorted(attrs):
        value = inspect.getattr_static(cls, attr, None)
        if isinstance(value, type) and value.__name__ in names:
            attr_types[attr] = value.__name__
        elif attr in hints and (known := _known(hints[attr], names)):
            attr_types[attr] = known
    methods: dict[str, Any] = {}
    returns: dict[str, str] = {}
    for attr in sorted(attrs):
        if isinstance(inspect.getattr_static(cls, attr, None), property):
            continue
        try:
            # Class-level access resolves descriptors (``Target.kubeconfig``).
            value = getattr(cls, attr)
        except AttributeError:
            continue
        if not callable(value) or isinstance(value, type):
            continue
        methods[attr] = _params(value)
        if (known := _returns(value, names)) is not None:
            returns[attr] = known
    described: dict[str, Any] = {
        "attrs": sorted(attrs),
        "init": _params(cls),
        "methods": methods,
    }
    if is_model:
        described["pydantic"] = True
    if attr_types:
        described["attr_types"] = attr_types
    if returns:
        described["returns"] = returns
    if "__getattr__" in vars(cls):
        described["open"] = True
    return described


def introspect_python() -> dict[str, Any]:
    """The Python half of the surface."""
    import importlib

    import piceli

    pydantic_attrs: set[str] = set()
    try:
        import pydantic

        pydantic_attrs = {a for a in dir(pydantic.BaseModel) if not a.startswith("_")}
    except ImportError:
        pass

    modules = sorted(
        {"piceli"}
        | {
            info.name
            for info in pkgutil.walk_packages(
                piceli.__path__, "piceli.", onerror=lambda _: None
            )
        }
    )
    classes = _collect_classes()
    names = set(classes)
    exports: dict[str, dict[str, Any]] = {}
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module(module_name)
        entries: dict[str, Any] = {}
        for name in _exports(module):
            value = getattr(module, name, None)
            if isinstance(value, type):
                entries[name] = {"class": value.__name__}
            elif callable(value):
                entry: dict[str, Any] = {"params": _params(value)}
                if (known := _returns(value, names)) is not None:
                    entry["returns"] = known
                entries[name] = entry
            else:
                entries[name] = {}
        exports[module_name] = entries
    described = {
        name: _describe_class(cls, names, pydantic_attrs)
        for name, cls in sorted(classes.items())
    }
    for qualified, model in KWARGS_FROM.items():
        owner, method = qualified.split(".")
        fields = described[model]["attrs"]
        base = described[owner]["methods"].get(method) or []
        described[owner]["methods"][method] = sorted({*base, "environment", *fields})
    return {
        "version": piceli.__version__,
        "modules": modules,
        "exports": exports,
        "pydantic_attrs": sorted(pydantic_attrs),
        "classes": described,
    }


def introspect_cli() -> dict[str, Any]:
    """Commands, flags and error codes from ``piceli help-json``."""
    output = subprocess.run(
        [sys.executable, "-m", "piceli", "help-json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(output)
    commands: dict[str, Any] = {}

    def walk(node: dict[str, Any]) -> None:
        flags = sorted({f for p in node.get("params", []) for f in p.get("flags", [])})
        arguments = [
            p["name"] for p in node.get("params", []) if p.get("kind") == "argument"
        ]
        children = node.get("commands") or []
        commands[node.get("path", "")] = {
            "flags": flags,
            "arguments": arguments,
            "subcommands": sorted(c["name"] for c in children),
        }
        for child in children:
            walk(child)

    walk(data["root"])
    return {"commands": commands, "error_codes": sorted(data["error_codes"])}


def introspect() -> dict[str, Any]:
    """The whole surface of the installed ``piceli``."""
    surface = introspect_python()
    surface["cli"] = introspect_cli()
    return surface


def load(prefer_installed: bool = True) -> dict[str, Any]:
    """The installed package's surface, or the committed snapshot without it."""
    if prefer_installed:
        try:
            return introspect()
        except (ImportError, subprocess.CalledProcessError):
            pass
    data: dict[str, Any] = json.loads(SNAPSHOT.read_text())
    return data


def dumps(surface: dict[str, Any]) -> str:
    """The snapshot file's text."""
    return json.dumps(surface, indent=1, sort_keys=True) + "\n"


# ------------------------------------------------------------------ Python code


@dataclass
class ApiReport:
    """Wrong or nonexistent API uses found in one piece of code."""

    errors: list[str] = field(default_factory=list)
    parsed: bool = True


def check_python(code: str, surface: dict[str, Any]) -> ApiReport:
    """Flag Piceli Python API uses in ``code`` that the surface does not have.

    Recognized: imports from ``piceli`` modules, constructor and method keyword
    arguments, and attributes of values whose type follows from the code
    (``app = App(...)``, ``web = app.deployment(...)``, ``with fake_cluster() as
    cluster``, ``app.probe.http(...)``). Anything else is not judged, so the
    count is a lower bound.
    """
    report = ApiReport()
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        report.parsed = False
        report.errors.append(f"syntax error: {error.msg} (line {error.lineno})")
        return report
    _PythonChecker(surface, report).visit(tree)
    return report


class _PythonChecker(ast.NodeVisitor):
    def __init__(self, surface: dict[str, Any], report: ApiReport) -> None:
        self.surface = surface
        self.report = report
        self.modules = set(surface["modules"])
        self.exports: dict[str, dict[str, Any]] = surface["exports"]
        self.classes: dict[str, dict[str, Any]] = surface["classes"]
        self.pydantic = set(surface.get("pydantic_attrs", []))
        # name -> ("class", ClassName) | ("instance", ClassName)
        # | ("function", export entry) | ("module", module name)
        self.env: dict[str, tuple[str, Any]] = {}
        self.reported: set[tuple[int, int, str]] = set()

    def _error(self, node: ast.AST, message: str) -> None:
        key = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0), message)
        if key not in self.reported:
            self.reported.add(key)
            self.report.errors.append(f"line {key[0]}: {message}")

    # -- imports ---------------------------------------------------------

    def _bind_export(self, alias: str, module: str, name: str) -> None:
        entry = self.exports.get(module, {}).get(name)
        if entry is None:
            submodule = f"{module}.{name}"
            if submodule in self.modules:
                self.env[alias] = ("module", submodule)
            return
        if "class" in entry:
            self.env[alias] = ("class", entry["class"])
        elif "params" in entry:
            self.env[alias] = ("function", entry)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] != "piceli":
                continue
            if alias.name not in self.modules:
                self._error(node, f"no module {alias.name!r}")
                continue
            if alias.asname:
                self.env[alias.asname] = ("module", alias.name)
            else:
                self.env["piceli"] = ("module", "piceli")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level or module.split(".")[0] != "piceli":
            return
        if module not in self.modules:
            self._error(node, f"no module {module!r}")
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            if module in self.exports:
                known = alias.name in self.exports[module] or (
                    f"{module}.{alias.name}" in self.modules
                )
                if not known:
                    self._error(node, f"{module} has no export {alias.name!r}")
                    continue
            self._bind_export(bound, module, alias.name)

    # -- types -----------------------------------------------------------

    def _type_of(self, node: ast.AST) -> tuple[str, Any] | None:
        if isinstance(node, ast.Name):
            return self.env.get(node.id)
        if isinstance(node, ast.Attribute):
            owner = self._type_of(node.value)
            if owner is None:
                return None
            kind, value = owner
            if kind == "module":
                if value in self.exports and node.attr in self.exports[value]:
                    entry = self.exports[value][node.attr]
                    if "class" in entry:
                        return ("class", entry["class"])
                    if "params" in entry:
                        return ("function", entry)
                return None
            if kind in {"class", "instance"}:
                cls = self.classes.get(value, {})
                target = cls.get("attr_types", {}).get(node.attr)
                if target is not None:
                    # An attribute holding a class (``app.probe``) acts as it.
                    return ("class", target)
            return None
        if isinstance(node, ast.Call):
            func = self._type_of(node.func)
            if func is not None:
                kind, value = func
                if kind == "class":
                    return ("instance", value)
                if kind == "function" and value.get("returns"):
                    return ("instance", value["returns"])
                if kind == "method" and value:
                    return ("instance", value)
            if isinstance(node.func, ast.Attribute):
                owner = self._type_of(node.func.value)
                if owner is not None and owner[0] in {"class", "instance"}:
                    cls = self.classes.get(owner[1], {})
                    returned = cls.get("returns", {}).get(node.func.attr)
                    if returned is not None:
                        return ("instance", returned)
        return None

    def _bind(self, target: ast.AST, value: ast.AST | None) -> None:
        if not isinstance(target, ast.Name):
            return
        typed = self._type_of(value) if value is not None else None
        if typed is not None:
            self.env[target.id] = typed
        else:
            self.env.pop(target.id, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        self._bind(node.target, node.value)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
        for statement in node.body:
            self.visit(statement)

    # -- checks ----------------------------------------------------------

    def _check_kwargs(
        self, node: ast.Call, allowed: list[str] | None, label: str
    ) -> None:
        if allowed is None:
            return
        for keyword in node.keywords:
            if keyword.arg is not None and keyword.arg not in allowed:
                self._error(node, f"{label}() has no parameter {keyword.arg!r}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        typed = self._type_of(func)
        if typed is not None:
            kind, value = typed
            if kind == "class":
                cls = self.classes.get(value)
                if cls is not None:
                    self._check_kwargs(node, cls.get("init"), value)
            elif kind == "function":
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "?")
                )
                self._check_kwargs(node, value.get("params"), name)
        if isinstance(func, ast.Attribute):
            owner = self._type_of(func.value)
            if owner is not None and owner[0] in {"class", "instance"}:
                cls = self.classes.get(owner[1], {})
                methods = cls.get("methods", {})
                if func.attr in methods:
                    self._check_kwargs(
                        node, methods[func.attr], f"{owner[1]}.{func.attr}"
                    )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        owner = self._type_of(node.value)
        if owner is not None:
            kind, value = owner
            if kind == "module" and value in self.exports:
                if (
                    node.attr not in self.exports[value]
                    and f"{value}.{node.attr}" not in self.modules
                ):
                    self._error(node, f"{value} has no attribute {node.attr!r}")
            elif kind in {"class", "instance"} and value in self.classes:
                cls = self.classes[value]
                attrs = set(cls["attrs"]) | _OBJECT_ATTRS
                if cls.get("pydantic"):
                    attrs |= self.pydantic
                if not cls.get("open") and node.attr not in attrs:
                    self._error(node, f"{value} has no attribute {node.attr!r}")
        self.generic_visit(node)


# ---------------------------------------------------------------------- the CLI


@dataclass
class ShellCommand:
    """One ``piceli`` invocation found in an answer's shell code."""

    line: str
    args: list[str]  # arguments after ``piceli``


_PREFIXES = {"uv", "run", "uvx", "python", "python3", "-m", "--frozen", "exec", "sudo"}
_PLACEHOLDER = re.compile(r"^<[^<>\s]+>$")  # ``<hash>``: a value, not a redirection
_REDIRECT = re.compile(r"^(\d?>>?|\d?<|&>|\d?>&\d)(.*)$")


def shell_blocks(answer: str) -> list[str]:
    """Fenced blocks that hold shell commands (bash/sh/console or untagged)."""
    blocks = []
    for lang, body in re.findall(r"```([\w+-]*)[ \t]*\n(.*?)```", answer, re.S):
        if lang.lower() in {
            "",
            "bash",
            "sh",
            "shell",
            "console",
            "zsh",
            "text",
            "terminal",
        }:
            blocks.append(body)
    return blocks


def shell_lines(block: str) -> list[str]:
    """Logical command lines: continuations joined, prompts and comments removed."""
    lines: list[str] = []
    pending = ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if pending:
            line = pending + " " + line.lstrip()
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        stripped = line.strip()
        if stripped.startswith("$ "):
            stripped = stripped[2:]
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    if pending.strip():
        lines.append(pending.strip())
    return lines


def _segments(line: str) -> list[str]:
    """Split a line on ``&&``, ``||`` and ``;``; keep the first stage of a pipe."""
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", line)
    return [re.split(r"\s+\|\s+", part)[0].strip() for part in parts if part.strip()]


def piceli_commands(answer: str) -> tuple[list[ShellCommand], list[str], list[str]]:
    """``piceli`` commands, other commands, and lines that could not be parsed."""
    commands: list[ShellCommand] = []
    others: list[str] = []
    unparsed: list[str] = []
    for block in shell_blocks(answer):
        for line in shell_lines(block):
            for segment in _segments(line):
                try:
                    tokens = shlex.split(segment, comments=True)
                except ValueError:
                    unparsed.append(segment)
                    continue
                # Leading VAR=value assignments.
                while tokens and re.match(r"^[A-Za-z_]\w*=", tokens[0]):
                    tokens = tokens[1:]
                index = next((i for i, t in enumerate(tokens) if t == "piceli"), None)
                if index is None or any(t not in _PREFIXES for t in tokens[:index]):
                    if tokens:
                        others.append(segment)
                    continue
                args: list[str] = []
                skip = False
                for token in tokens[index + 1 :]:
                    if skip:
                        skip = False
                        continue
                    match = (
                        None if _PLACEHOLDER.match(token) else _REDIRECT.match(token)
                    )
                    if match:
                        skip = not match[2]
                        continue
                    args.append(token)
                commands.append(ShellCommand(segment, args))
    return commands, others, unparsed


def check_cli(commands: list[ShellCommand], surface: dict[str, Any]) -> list[str]:
    """Nonexistent commands, flags or error codes in ``piceli`` invocations."""
    cli = surface["cli"]
    table: dict[str, Any] = cli["commands"]
    codes = set(cli["error_codes"])
    errors: list[str] = []
    for command in commands:
        path = ""
        args = list(command.args)
        positional: list[str] = []
        while args:
            token = args[0]
            node = table[path]
            if token.startswith("-"):
                break
            if node["subcommands"]:
                if token not in node["subcommands"]:
                    shown = f"piceli {path} {token}".replace("  ", " ")
                    errors.append(f"no command {shown!r}")
                    path = ""
                    args = []
                    break
                path = f"{path} {token}".strip()
                args.pop(0)
                continue
            break
        if not args and path == "" and not command.args:
            continue
        node = table.get(path)
        if node is None:
            continue
        if node["subcommands"] and args and not args[0].startswith("-"):
            continue  # already reported
        allowed = set(node["flags"]) | {"--help", "-h"}
        if path == "":
            allowed |= {"--help-json", "--version"}
        for token in args:
            if token.startswith("--"):
                flag = token.split("=", 1)[0]
                if flag not in allowed:
                    errors.append(
                        f"piceli {path}: no option {flag!r}".replace("  ", " ")
                    )
            elif not token.startswith("-"):
                positional.append(token)
        if path == "explain":
            for code in positional[:1]:
                if not code.startswith("<") and code not in codes:
                    errors.append(f"piceli explain: no error code {code!r}")
    return errors
