"""``piceli train-init`` (``piceli train init``): a starting spec.

The sample script is made up: a small shop project's merge script with a
cargo check, cargo tests, pytest, npm, make, a docker build and a deploy.
Every written spec is loaded with the train's own loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.k8s.cli.train import subcommand_argv
from piceli.train import CargoCheck, CargoTests, CommandStep
from piceli.train.init import translate
from piceli.train.model import ImagesStep
from piceli.train.spec import load

SCRIPT = """\
#!/usr/bin/env bash
# The shop's merge script: merge the branches, test, build, push.
set -euo pipefail
BASE=${SHOP_BASE:-develop}
repo=$1
shift
git -C "$repo" fetch -q origin
for b in "$@"; do
  git merge --no-ff --no-edit "$b"
done
PACKAGES="shop-core shop-api"
cargo check --offline --all-targets
CARGO_INCREMENTAL=0 cargo test --offline -p shop-core -p shop-api
if ! CARGO_INCREMENTAL=0 cargo test --offline -p shop-core -p shop-api; then
  echo "retry"
fi
uv run pytest -q tests/unit
npm run lint
make docs
docker build -t registry.example.org/shop:dev .
docker push registry.example.org/shop:dev
rsync -a dist/ builder-1:/srv/shop/
./scripts/smoke.sh "$repo"
echo done
"""


def init(cwd: Path, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, ["train-init", *args, "--json"], env=None)
    del cwd
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    body = json.loads(lines[-1]) if lines else {}
    return result.exit_code, body, result.stderr


def steps_of(path: Path) -> list[Any]:
    return list(load(f"{path}:train", path.parent).train.steps)


def test_translate_finds_steps_base_and_todo_lines() -> None:
    found = translate(SCRIPT)
    assert found.base == "develop"
    names = [step.name for step in found.steps]
    assert names == ["cargo-check", "cargo-test", "pytest", "npm-lint", "make-docs"]
    check, tests = found.steps[0], found.steps[1]
    assert check.code == 'CargoCheck(args=("--offline", "--all-targets"))'
    assert tests.code == (
        'CargoTests(select=Impact(), args=("--offline",), '
        'env={"CARGO_INCREMENTAL": "0"})'
    )
    assert tests.packages == ("shop-core", "shop-api")
    assert found.packages == ("shop-core", "shop-api")
    todo = {item.line: item.text for item in found.todo}
    assert todo == {
        20: "docker build -t registry.example.org/shop:dev .",
        21: "docker push registry.example.org/shop:dev",
        22: "rsync -a dist/ builder-1:/srv/shop/",
        23: './scripts/smoke.sh "$repo"',
    }
    hints = {item.line: item.hint for item in found.todo}
    assert "--images" in hints[20]
    assert "Step.publish" in hints[22]
    # The repeated cargo test (the script's own retry) is not a second step.
    assert any(item.line == 14 for item in found.repeated)
    assert {item.line for item in found.handled} == {7, 9}


def test_init_from_script_writes_a_spec_that_loads(tmp_path: Path) -> None:
    script = tmp_path / "merge.sh"
    script.write_text(SCRIPT)
    out = tmp_path / "train_spec.py"
    status, body, stderr = init(
        tmp_path, str(out), "--from", str(script), "--name", "shop"
    )
    assert status == 0, stderr
    assert body["schema"] == "piceli.train-init.v1"
    assert body["path"] == str(out)
    assert body["base"] == "develop"
    assert body["loads"] is True
    assert [s["name"] for s in body["steps"]] == [
        "cargo-check",
        "cargo-test",
        "pytest",
        "npm-lint",
        "make-docs",
    ]
    assert [t["line"] for t in body["todo"]] == [20, 21, 22, 23]
    text = out.read_text()
    # The lines it could not translate stay in the spec as TODO comments.
    assert "# TODO line 20: docker build -t registry.example.org/shop:dev ." in text
    assert "# TODO line 23: ./scripts/smoke.sh" in text
    assert "TODO line 20" in stderr and "4 line(s) left as TODO" in stderr
    loaded = load(f"{out}:train", tmp_path).train
    assert loaded.name == "shop" and loaded.base == "develop"
    kinds = [type(step) for step in loaded.steps]
    assert kinds == [CargoCheck, CargoTests, CommandStep, CommandStep, CommandStep]
    pytest_step = loaded.steps[2]
    assert isinstance(pytest_step, CommandStep)
    assert pytest_step.argv == ("uv", "run", "pytest", "-q", "tests/unit")
    assert dict(loaded.steps[1].env) == {"CARGO_INCREMENTAL": "0"}


def test_template_without_script_loads_and_never_overwrites(tmp_path: Path) -> None:
    out = tmp_path / "train_spec.py"
    status, body, _ = init(tmp_path, str(out), "--name", "shop")
    assert status == 0
    assert body["todo"] == [] and body["source"] is None
    text = out.read_text()
    assert "# Rust: CargoCheck(), CargoTests(select=Impact())," in text
    assert steps_of(out)[0].name == "tests"
    out.write_text("# mine\n")
    status, body, stderr = init(tmp_path, str(out), "--name", "shop")
    assert status == 2
    assert body["reason"] == "train-init-exists"
    assert out.read_text() == "# mine\n"
    status, body, _ = init(tmp_path, str(out), "--name", "shop", "--force")
    assert status == 0
    assert out.read_text() != "# mine\n"


def test_images_and_host_options(tmp_path: Path) -> None:
    script = tmp_path / "merge.sh"
    script.write_text(SCRIPT)
    out = tmp_path / "train_spec.py"
    status, body, stderr = init(
        tmp_path,
        str(out),
        "--from",
        str(script),
        "--name",
        "shop",
        "--images",
        "infra.py:cluster",
        "--repository",
        "https://git.example.org/me/infra.git",
        "--branch",
        "main",
        "--host",
        "builder-1",
    )
    assert status == 0, stderr
    text = out.read_text()
    assert "prebuilt=True" in text
    steps = steps_of(out)
    images = steps[-1]
    assert isinstance(images, ImagesStep)
    assert images.composition == "infra.py"
    assert images.repository == "https://git.example.org/me/infra.git"
    assert images.branch == "main"
    by_name = {step.name: step for step in steps}
    assert by_name["cargo-check"].hosts == ("builder-1",)
    assert by_name["cargo-test"].hosts == ("builder-1",)
    assert by_name["pytest"].hosts == ()
    # The docker lines are covered by the images step: no longer TODO.
    assert [t["line"] for t in body["todo"]] == [22, 23]


def test_bad_inputs_are_rejected(tmp_path: Path) -> None:
    out = tmp_path / "train_spec.py"
    status, body, _ = init(tmp_path, str(out), "--images", "/abs/infra.py")
    assert status == 2 and body["reason"] == "train-init-invalid"
    status, body, _ = init(tmp_path, str(out), "--from", str(tmp_path / "nope.sh"))
    assert status == 2 and body["reason"] == "train-init-invalid"
    status, body, _ = init(tmp_path, str(out), "--repository", "https://x.org/r.git")
    assert status == 2 and body["reason"] == "train-init-invalid"
    assert not out.exists()


def test_train_init_spelling() -> None:
    assert subcommand_argv(["train", "init", "x.py"]) == ["train-init", "x.py"]
    assert subcommand_argv(["train", "s.py", "--branches", "a"]) == [
        "train",
        "s.py",
        "--branches",
        "a",
    ]
    assert subcommand_argv(["trains"]) == ["trains"]
