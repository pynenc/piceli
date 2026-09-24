"""A tiny, deterministic Python source emitter formatted like ``ruff format``.

The importer builds expressions from these nodes and prints them with the
same line-splitting rules as ``ruff format`` (Black style, 88 columns): an
expression stays on one line when it fits; otherwise its brackets open and the
contents go on one indented line when they fit, else one item per line with a
trailing comma. The output is stable under ``ruff format``.
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

WIDTH = 88
INDENT = "    "


class Expr:
    """A Python expression."""

    def flat(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class Atom(Expr):
    """Verbatim source: a name, a number or a pre-rendered literal."""

    text: str

    def flat(self) -> str:
        return self.text


@dataclass(frozen=True)
class Bracketed(Expr):
    """``open item, item close`` with an optional prefix (a call's function)."""

    prefix: str
    open: str
    close: str
    items: tuple[tuple[str, Expr], ...] = field(default_factory=tuple)
    collection: bool = False
    one_tuple: bool = False

    def flat(self) -> str:
        body = ", ".join(head + value.flat() for head, value in self.items)
        if self.one_tuple:
            body += ","
        return f"{self.prefix}{self.open}{body}{self.close}"


def one_tuple(item: Expr) -> Bracketed:
    """``(item,)``."""
    return Bracketed("", "(", ")", (("", item),), collection=True, one_tuple=True)


def _ascii(text: str) -> str:
    return "".join(
        char
        if ord(char) < 128
        else f"\\x{ord(char):02x}"
        if ord(char) < 256
        else f"\\u{ord(char):04x}"
        if ord(char) < 0x10000
        else f"\\U{ord(char):08x}"
        for char in text
    )


def string(value: str) -> str:
    """An ASCII-only string literal with the quotes ``ruff format`` prefers."""
    text = repr(value)
    if text.startswith("'") and not ('"' in value and "'" not in value):
        body = text[1:-1].replace("\\'", "'").replace('"', '\\"')
        text = f'"{body}"'
    return _ascii(text)


def literal(value: Any) -> Expr:
    """An expression for plain JSON-like data (dicts keep their order)."""
    if value is None or isinstance(value, bool | int | float):
        return Atom(repr(value))
    if isinstance(value, str):
        return Atom(string(value))
    if isinstance(value, Mapping):
        return Bracketed(
            "",
            "{",
            "}",
            tuple(
                (string(str(key)) + ": ", literal(item)) for key, item in value.items()
            ),
            collection=True,
        )
    if isinstance(value, Sequence):
        return Bracketed(
            "", "[", "]", tuple(("", literal(item)) for item in value), collection=True
        )
    raise TypeError(f"cannot write {type(value).__name__} as a literal")


def call(function: str, *args: Expr, **kwargs: Expr | None) -> Bracketed:
    """``function(args..., key=value...)``; ``None`` keyword values are omitted."""
    items = [("", arg) for arg in args]
    items += [(f"{key}=", value) for key, value in kwargs.items() if value is not None]
    return Bracketed(function, "(", ")", tuple(items))


def lines(expr: Expr, head: str = "", tail: str = "", depth: int = 0) -> list[str]:
    """``head + expr + tail`` at ``depth`` indentation, split to fit."""
    indent = INDENT * depth
    flat = indent + head + expr.flat() + tail
    if len(flat) <= WIDTH or not isinstance(expr, Bracketed) or not expr.items:
        return [flat]
    opening = indent + head + expr.prefix + expr.open
    closing = indent + expr.close + tail
    if len(expr.items) == 1 and not expr.one_tuple:
        # A sole item goes on its own line(s), without a trailing comma.
        item_head, value = expr.items[0]
        return [opening, *lines(value, item_head, "", depth + 1), closing]
    body = INDENT * (depth + 1) + ", ".join(
        item_head + value.flat() for item_head, value in expr.items
    )
    if len(body) <= WIDTH and not expr.collection:
        # Call arguments may share one line; a literal collection with
        # several items is always split one item per line (Black's rule).
        return [opening, body, closing]
    result = [opening]
    for item_head, value in expr.items:
        result += lines(value, item_head, ",", depth + 1)
    result.append(closing)
    return result


def statement(expr: Expr, target: str | None = None, depth: int = 1) -> list[str]:
    """An expression statement, or ``target = expr``."""
    return lines(expr, f"{target} = " if target else "", "", depth)


_IDENTIFIER = re.compile(r"[^a-z0-9_]+")


def identifier(name: str, taken: set[str], suffix: str = "") -> str:
    """A unique snake_case Python name for ``name``; added to ``taken``."""
    base = _IDENTIFIER.sub("_", name.lower()).strip("_") or "item"
    if base[0].isdigit():
        base = "_" + base
    candidates = [base, f"{base}_{suffix}"] if suffix else [base]
    for candidate in candidates:
        if candidate not in taken and not keyword.iskeyword(candidate):
            taken.add(candidate)
            return candidate
    number = 2
    while f"{candidates[-1]}_{number}" in taken:
        number += 1
    chosen = f"{candidates[-1]}_{number}"
    taken.add(chosen)
    return chosen


def dict_of(items: Sequence[tuple[str, Expr]]) -> Bracketed:
    """``{"key": expr, ...}`` from string keys and expressions."""
    return Bracketed(
        "",
        "{",
        "}",
        tuple((string(key) + ": ", value) for key, value in items),
        collection=True,
    )


def list_of(items: Sequence[Expr]) -> Bracketed:
    """``[expr, ...]``."""
    return Bracketed("", "[", "]", tuple(("", item) for item in items), collection=True)


def names_in(expr: Expr) -> set[str]:
    """Identifiers an expression reads (``var`` in ``var`` or ``var.key(...)``)."""
    found: set[str] = set()
    if isinstance(expr, Atom):
        if expr.text.isidentifier():
            found.add(expr.text)
    elif isinstance(expr, Bracketed):
        head = expr.prefix.split(".", 1)[0]
        if head.isidentifier():
            found.add(head)
        for _, item in expr.items:
            found |= names_in(item)
    return found
