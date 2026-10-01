"""0.13 and 0.14 declarations keep their descriptions (and so their plan hashes).

Compositions add fields to ``Environment``; an environment, an ``EnvConfig``
or a controller config that does not use them must describe exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from piceli.envs import Branch, EnvConfig, Environment, Promote, Stack, Tag
from piceli.gitops.config import ControllerConfig, EnvRule


def _hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _configs() -> tuple[EnvConfig, EnvConfig]:
    old = EnvConfig(
        prefix="shop-",
        branches=["main", "wp-*"],
        max_envs=3,
        claim_sizes={"db": "1Gi"},
        seed_from="main",
    )
    named = EnvConfig(
        prefix="shop-",
        branches=["wp-*"],
        environments=[
            Environment(
                "main",
                namespace="shop-main",
                follow=Branch("main"),
                stack=Stack("full", ["api", "db"]),
                on_nodes=["n1"],
                quota={"pods": "10"},
                auto_approve=True,
            ),
            Environment("rc", namespace="shop", follow=[Tag("v*-rc*"), Promote()]),
        ],
        branch_stack=Stack("small", ["api"]),
        branch_nodes={"role": "edge"},
        idle_stop="24h",
    )
    return old, named


def test_env_config_descriptions_are_unchanged() -> None:
    old, named = _configs()
    assert _hash(old.describe()) == (
        "6687c15b6f6dd39d88f35e71b700b6f3b2b6878310785829bf093f872c4373e0"
    )
    assert _hash(named.describe()) == (
        "0c4f2bfce03cd58402b1740e30a41fe7d598275958e4e14d83e2430980e79cc6"
    )


def test_controller_configs_are_unchanged() -> None:
    _, named = _configs()
    single = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/shop.git",
        branches=("main", "wp-*"),
    )
    rules = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/shop.git",
        branches=("wp-*",),
        environments=tuple(EnvRule.from_environment(e) for e in named.environments),
        idle_stop_seconds=86400,
    )
    assert _hash(single.to_dict()) == (
        "ae6d5bb3200f75861b751f4f8de5f4de4ebec2c3735680e36fb3f99cf09424ef"
    )
    assert _hash(rules.to_dict()) == (
        "1b5f53061f76847ff662005fc319bf386e73642a62f679724b6ad0d6434abff0"
    )
