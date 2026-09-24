"""The emitter prints what ``ruff format`` would print."""

from __future__ import annotations

import random
from typing import Any

import pytest

from piceli.importing.codegen import (
    Atom,
    call,
    identifier,
    literal,
    one_tuple,
    statement,
    string,
)

TEXTS = [
    "abc",
    "x" * 70,
    "y" * 95,
    'say "hi"',
    "it's",
    "both ' and \"",
    "line\nbreak\ttab",
    "caf\u00e9 \u2028 \U0001f600",
    "back\\slash",
    "\x00\x7f",
]


def _value(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth > 3 or roll < 0.4:
        return rng.choice([*TEXTS, 7, 65535, True, None, 0.5])
    if roll < 0.7:
        return {
            f"key{index}" * rng.randint(1, 3): _value(rng, depth + 1)
            for index in range(rng.randint(0, 5))
        }
    return [_value(rng, depth + 1) for _ in range(rng.randint(0, 5))]


@pytest.mark.parametrize("value", TEXTS)
def test_string_literals_round_trip_and_are_ascii(value: str) -> None:
    text = string(value)
    assert eval(text) == value
    assert text.isascii()


def test_identifiers_are_unique_and_valid() -> None:
    taken = {"app"}
    names = [
        identifier(name, taken, "config")
        for name in ("app", "api", "api", "1-x", "class")
    ]
    assert names == ["app_config", "api", "api_config", "_1_x", "class_config"]


def test_random_statements_are_stable_under_ruff_format(ruff_clean) -> None:
    rng = random.Random(20260924)
    body = ["def build(app, cache):"]
    for _ in range(150):
        roll = rng.random()
        if roll < 0.3:
            body += statement(call("app.override", Atom("cache"), literal(_value(rng))))
        elif roll < 0.6:
            body += statement(
                call(
                    "app.deployment",
                    literal("name" * rng.randint(1, 5)),
                    image=literal(_value(rng)),
                    ports=literal(
                        [rng.randint(1, 9999) for _ in range(rng.randint(0, 6))]
                    ),
                    env=literal(_value(rng)),
                ),
                "item",
            )
        elif roll < 0.8:
            body += statement(call("app.probe.exec", literal([_value(rng)])), "probe")
        else:
            body += statement(
                call("app.add", one_tuple(call("app.wrap", literal(_value(rng))))), None
            )
    body.append("    return item, probe")
    ruff_clean("\n".join(body) + "\n")
