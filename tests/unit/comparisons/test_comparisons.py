"""The comparison app (``examples/comparisons``) renders the same in every tool.

The same web + api app is written with Piceli, Helm, Kustomize, cdk8s and
Pulumi. For ``dev``, ``staging`` and ``prod`` each tool's output is compared
with ``piceli render --env <env>``: the same objects (kind, name, namespace)
with the same labels, data and spec. Annotations are ignored, and integral
numbers compare equal whatever their JSON type (Pulumi's mocks send floats).

Each tool is optional locally: its test is skipped when the tool (``helm``,
``kustomize`` or ``kubectl``, the ``cdk8s`` packages plus ``node``, the
``pulumi`` packages) is missing. The CI job ``comparisons`` installs the
pinned versions and sets ``PICELI_COMPARISONS=require``, which turns every
skip into a failure. No test contacts a cluster: Helm and Kustomize only
render, cdk8s only synthesizes and Pulumi runs against its own mocks.

The file and line counts in ``docs/comparisons.md`` are checked here too.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "examples" / "comparisons"
DOCS = ROOT / "docs" / "comparisons.md"
ENVIRONMENTS = ("dev", "staging", "prod")
REQUIRE = os.environ.get("PICELI_COMPARISONS") == "require"
Objects = dict[tuple[str, str], dict[str, Any]]

# The files that make up each version of the app (dependency pins excluded).
SOURCES = {
    "Piceli": ["piceli/app.py"],
    "Helm": ["helm/webapp"],
    "Kustomize": ["kustomize"],
    "cdk8s": ["cdk8s/main.py", "cdk8s/cdk8s.yaml"],
    "Pulumi": [
        "pulumi/__main__.py",
        "pulumi/Pulumi.yaml",
        "pulumi/Pulumi.dev.yaml",
        "pulumi/Pulumi.staging.yaml",
        "pulumi/Pulumi.prod.yaml",
    ],
}


def _missing(reason: str) -> None:
    if REQUIRE:
        pytest.fail(f"PICELI_COMPARISONS=require but {reason}")
    pytest.skip(reason)


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(  # fixed argv; renders only
        cmd, cwd=cwd, capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _objects(documents: list[Any]) -> Objects:
    objects: Objects = {}
    for doc in documents:
        if not doc:
            continue
        doc = _normalize(doc)
        doc["metadata"].pop("annotations", None)
        key = (doc["kind"], doc["metadata"]["name"])
        assert key not in objects, f"{key} rendered twice"
        objects[key] = doc
    return objects


def _yaml(text: str) -> Objects:
    return _objects(list(yaml.safe_load_all(text)))


def render_piceli(env: str) -> Objects:
    result = CliRunner().invoke(
        cli, ["render", f"{EXAMPLES / 'piceli' / 'app.py'}:app", "--env", env]
    )
    assert result.exit_code == 0, result.output
    return _yaml(result.stdout)


@pytest.fixture(scope="module")
def piceli() -> dict[str, Objects]:
    return {env: render_piceli(env) for env in ENVIRONMENTS}


def _assert_same(piceli: Objects, other: Objects, tool: str) -> None:
    assert sorted(other) == sorted(piceli), f"{tool} renders other objects"
    for key, expected in piceli.items():
        assert other[key] == expected, f"{tool}: {key[0]}/{key[1]} differs"


def test_piceli_renders_the_comparison_objects(piceli: dict[str, Objects]) -> None:
    for env in ENVIRONMENTS:
        assert sorted(piceli[env]) == [
            ("ConfigMap", "settings"),
            ("ConfigMap", "site"),
            ("Deployment", "api"),
            ("Deployment", "web"),
            ("HorizontalPodAutoscaler", "web"),
            ("NetworkPolicy", "internal"),
            ("PodDisruptionBudget", "web"),
            ("Service", "api"),
            ("Service", "web"),
        ]
        assert {o["metadata"]["namespace"] for o in piceli[env].values()} == {
            f"webapp-{env}"
        }
    hpa = {env: piceli[env][("HorizontalPodAutoscaler", "web")] for env in piceli}
    assert [
        (h["spec"]["minReplicas"], h["spec"]["maxReplicas"]) for h in hpa.values()
    ] == [
        (1, 2),
        (2, 6),
        (3, 10),
    ]


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_helm_chart_matches_piceli(env: str, piceli: dict[str, Objects]) -> None:
    helm = shutil.which("helm")
    if helm is None:
        _missing("helm is not on PATH")
    chart = EXAMPLES / "helm" / "webapp"
    out = _run(
        [
            str(helm),
            "template",
            "webapp",
            str(chart),
            "--namespace",
            f"webapp-{env}",
            "--values",
            str(chart / f"values-{env}.yaml"),
        ],
        ROOT,
    )
    _assert_same(piceli[env], _yaml(out), "Helm")


def _kustomize() -> list[str]:
    if found := shutil.which("kustomize"):
        return [found, "build"]
    if found := shutil.which("kubectl"):  # `kubectl kustomize` never contacts a cluster
        return [found, "kustomize"]
    _missing("neither kustomize nor kubectl is on PATH")
    raise AssertionError  # unreachable


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_kustomize_overlays_match_piceli(env: str, piceli: dict[str, Objects]) -> None:
    command = _kustomize()
    out = _run([*command, str(EXAMPLES / "kustomize" / "overlays" / env)], ROOT)
    _assert_same(piceli[env], _yaml(out), "Kustomize")


def _python_packages(requirements: Path) -> None:
    """Skip unless every pinned package of ``requirements`` is importable."""
    for line in requirements.read_text().splitlines():
        name = line.split("#")[0].split("==")[0].strip()
        if name and importlib.util.find_spec(name.replace("-", "_")) is None:
            _missing(f"{name} is not installed (see {requirements.relative_to(ROOT)})")


@pytest.mark.timeout(300)
def test_cdk8s_charts_match_piceli(tmp_path: Path, piceli: dict[str, Objects]) -> None:
    _python_packages(EXAMPLES / "cdk8s" / "requirements.txt")
    if shutil.which("node") is None:
        _missing("cdk8s needs node on PATH")
    project = tmp_path / "cdk8s"
    shutil.copytree(EXAMPLES / "cdk8s", project)
    _run([sys.executable, "main.py"], project)
    for env in ENVIRONMENTS:
        synthesized = (project / "dist" / f"webapp-{env}.k8s.yaml").read_text()
        _assert_same(piceli[env], _yaml(synthesized), "cdk8s")


@pytest.mark.timeout(300)
@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_pulumi_program_matches_piceli(env: str, piceli: dict[str, Objects]) -> None:
    _python_packages(EXAMPLES / "pulumi" / "requirements.txt")
    renderer = Path(__file__).with_name("pulumi_render.py")
    out = _run([sys.executable, str(renderer), str(EXAMPLES / "pulumi"), env], ROOT)
    _assert_same(piceli[env], _objects(json.loads(out)), "Pulumi")


# ------------------------------------------------------------ counted in docs
def _python_lines(text: str) -> set[int]:
    """Line numbers holding code (no blank lines, comments or docstrings)."""
    docstrings: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.update(range(body[0].lineno, (body[0].end_lineno or 0) + 1))
    skip = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
    }
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in skip:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines - docstrings


def count_lines(path: Path) -> int:
    """Non-blank, non-comment lines (Python docstrings are comments too)."""
    text = path.read_text()
    if path.suffix == ".py":
        return len(_python_lines(text))
    return sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def source_files(tool: str) -> list[Path]:
    files: list[Path] = []
    for entry in SOURCES[tool]:
        path = EXAMPLES / entry
        files += (
            sorted(p for p in path.rglob("*") if p.is_file())
            if path.is_dir()
            else [path]
        )
    return files


def _table_row(label: str) -> list[int]:
    for line in DOCS.read_text().splitlines():
        if line.startswith(f"| {label} |"):
            return [int(cell.strip()) for cell in line.strip("|").split("|")[1:]]
    raise AssertionError(f"docs/comparisons.md has no '{label}' row")


def test_docs_table_counts_files_and_lines() -> None:
    header = next(
        line
        for line in DOCS.read_text().splitlines()
        if line.startswith("| Measured |")
    )
    assert [c.strip() for c in header.strip("|").split("|")[1:]] == list(SOURCES)
    files = [len(source_files(tool)) for tool in SOURCES]
    lines = [sum(count_lines(p) for p in source_files(tool)) for tool in SOURCES]
    assert _table_row("Files") == files
    assert _table_row("Lines of configuration") == lines


def test_count_lines_ignores_comments_and_docstrings(tmp_path: Path) -> None:
    module = tmp_path / "m.py"
    module.write_text('"""Doc\nstring."""\n\n# comment\nx = [\n    1,\n]\n')
    assert count_lines(module) == 3
    manifest = tmp_path / "m.yaml"
    manifest.write_text("# comment\n\na: 1\n  # indented comment\nb: 2\n")
    assert count_lines(manifest) == 2


def test_every_comparison_pins_its_tool() -> None:
    for requirements in ("cdk8s/requirements.txt", "pulumi/requirements.txt"):
        pins = [
            line
            for line in (EXAMPLES / requirements).read_text().splitlines()
            if line and not line.startswith("#")
        ]
        assert pins and all(re.fullmatch(r"[a-z0-9-]+==[0-9.]+", p) for p in pins)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert re.search(r"HELM_VERSION: v\d+\.\d+\.\d+", workflow)
    assert re.search(r"KUSTOMIZE_VERSION: v\d+\.\d+\.\d+", workflow)
