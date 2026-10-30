"""0.41.0: a command naming a path its context does not stage is refused at load.

Contexts are staged as ``<stage>/<into or name>/`` with only the files their
``include`` globs select; a command that runs ``site/scripts/build.sh`` when
``site`` includes only ``src/**`` fails on the build host. The spec load
refuses it (``host-build-path-not-staged``) and names the command, the path
and the include list. The check is conservative: shell syntax, placeholders,
outputs and directories are left alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildSpec


def _spec(
    commands: list[list[str]],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    into: str | None = None,
    workdir: str = ".",
    shared: list[list[str]] | None = None,
    files: dict[str, str] | None = None,
    tools: tuple[str, ...] = ("bash", "sh", "cargo", "cp", "node"),
) -> dict[str, Any]:
    context: dict[str, Any] = {"include": include or ["src/**", "Cargo.toml"]}
    if exclude:
        context["exclude"] = exclude
    if into:
        context["into"] = into
    build: dict[str, Any] = {
        "tools": list(tools),
        "commands": commands,
        "workdir": workdir,
    }
    if shared:
        build["shared_commands"] = shared
    image: dict[str, Any] = {
        "name": "web",
        "repository": "shop/web",
        "target_files": {"release/web": "/usr/local/bin/web"},
    }
    if files:
        image["files"] = files
    return {
        "revision": "piceli.host-build.v1",
        "name": "shop",
        "build": build,
        "context": {"site": context},
        "output": {"image": [image]},
    }


def _load(document: dict[str, Any]) -> HostBuildSpec:
    return HostBuildSpec.from_dict(document, Path("."))


def test_a_script_outside_the_include_list_is_refused_with_what_and_why() -> None:
    with pytest.raises(BuildSpecError) as raised:
        _load(_spec([["bash", "site/scripts/build_site.sh"]]))
    error = raised.value
    assert error.code == "host-build-path-not-staged"
    text = str(error)
    assert "build.commands[0]" in text
    assert "'site/scripts/build_site.sh'" in text
    assert "['src/**', 'Cargo.toml']" in text
    assert "context.site.include" in text


def test_a_staged_path_a_directory_and_other_paths_pass() -> None:
    _load(
        _spec(
            [
                ["bash", "site/src/build.sh"],
                ["cargo", "build", "--manifest-path", "site/Cargo.toml"],
                ["cargo", "build", "site/src"],  # a directory with staged files
                ["cargo", "build", "other/x.sh"],  # not a context directory
                ["cargo", "build", "/abs/x.sh", "plain", "../up/x.sh"],
            ]
        )
    )


def test_excluded_files_and_the_into_directory_are_checked() -> None:
    with pytest.raises(BuildSpecError, match="exclude"):
        _load(_spec([["bash", "site/src/secret.sh"]], exclude=["src/secret.sh"]))
    with pytest.raises(BuildSpecError, match="not staged"):
        _load(_spec([["bash", "app/tools/x.sh"]], into="app"))
    _load(_spec([["bash", "app/src/x.sh"]], into="app"))
    # The context's name is not its directory once it has ``into``.
    _load(_spec([["bash", "site/tools/x.sh"]], into="app"))


def test_the_workdir_is_taken_into_account() -> None:
    with pytest.raises(BuildSpecError, match="not staged"):
        _load(_spec([["bash", "scripts/x.sh"]], workdir="site"))
    _load(_spec([["bash", "src/x.sh"]], workdir="site"))


def test_shell_scripts_check_only_the_programs_they_run() -> None:
    with pytest.raises(BuildSpecError) as raised:
        _load(_spec([["bash", "-c", "set -e; site/scripts/build.sh --fast"]]))
    assert "'site/scripts/build.sh'" in str(raised.value)
    with pytest.raises(BuildSpecError, match="not staged"):
        _load(_spec([["sh", "-c", "cd site && bash site/tools/run.sh"]]))
    with pytest.raises(BuildSpecError, match=r"shared_commands\[0\]"):
        _load(
            _spec(
                [["cargo", "build"]],
                shared=[["sh", "-c", "X=1 site/tools/gen.sh"]],
            )
        )
    # Shell syntax, arguments and outputs are never guessed at.
    _load(
        _spec(
            [
                ["bash", "-c", "site/src/build.sh > site/dist/log.txt"],
                ["bash", "-c", 'echo "$HOME" site/tools/not-run.sh'],
                ["bash", "-c", "$SCRIPT site/tools/x.sh"],
                ["bash", "-c", "site/*/build.sh"],
            ]
        )
    )


def test_placeholders_outputs_and_writers_are_left_alone() -> None:
    _load(
        _spec(
            [
                ["cargo", "build", "site/target/{rust_arch}/x"],
                ["cargo", "build", "--out-dir", "site/dist"],
                ["cargo", "build", "-o", "site/dist/app.js"],
                ["cp", "site/src/a", "site/dist/a"],
                ["node", "site/static/x.js"],  # an image's files path
            ],
            files={"site/static": "/srv/static"},
        )
    )
