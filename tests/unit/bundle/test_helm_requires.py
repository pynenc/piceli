"""0.95.0: chart values that need other values (ops#116).

``HelmOptional(when=[...])`` gates objects (and patches) on several values,
all of which must hold; ``HelmChart(requires=...)`` fails the install when a
value is true and one it needs is not. The charts change the fixture
``helm_intake_app``: ``collector.enabled`` (default on) installs the intake
NetworkPolicy, which is useful only with ``intake.expose`` (the Service).
The tests read the text the renderer writes; the ones that need ``helm``
skip without it (the CI job ``comparisons`` runs them).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from piceli.bundle.helm import HelmChart, HelmOptional
from piceli.k8s.cli import app as cli

ROOT = Path(__file__).resolve().parents[3]
INTAKE = "tests/unit/bundle/helm_intake_app.py:chart"
HELM = shutil.which("helm")
REQUIRE = os.environ.get("PICELI_COMPARISONS") == "require"
BASE = ("--set", "team=t1", "--set", "clusterId=42", "--set", "apiToken=x")
EXPOSE = "(($.Values.intake | default dict).expose)"
COLLECTOR = "(($.Values.collector | default dict).enabled)"
POLICY = "templates/agent_networkpolicy.networking.k8s.io_agent_agent-intake.yaml"
SERVICE = "templates/agent_service.core_agent_agent-intake.yaml"
AGENT = "templates/agent_statefulset.apps_agent_agent.yaml"
REQUIRES = "templates/piceli-requires.yaml"
INTAKE_EXPOSE = (
    "'intake.expose': HelmOptional('Expose the intake', objects=[intake], "
    "patches=[(agent, LISTEN_EVERYWHERE)])"
)
GATED = (
    "optional={"
    + INTAKE_EXPOSE
    + ", 'collector.enabled': HelmOptional('Run the collector', "
    "objects=[intake_policy], default=True, default_on_reuse=True, "
    "when=['intake.expose'])}"
)
REQUIRED = (
    "optional={"
    + INTAKE_EXPOSE
    + ", 'collector.enabled': HelmOptional('Run the collector', "
    "objects=[intake_policy])}, "
    "requires={'collector.enabled': ['intake.expose']}"
)


@pytest.fixture
def helm() -> str:
    if HELM is None:
        if REQUIRE:
            pytest.fail("PICELI_COMPARISONS=require but helm is not on PATH")
        pytest.skip("needs helm on PATH")
    return HELM


def _run(*args: str) -> Any:
    cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        return CliRunner().invoke(cli, ["bundle", *args])
    finally:
        os.chdir(cwd)


def _module(tmp_path: Path, change: str, name: str) -> str:
    """A chart module that changes the fixture's declaration."""
    module = tmp_path / f"{name}.py"
    module.write_text(
        "import dataclasses\n"
        "from piceli import HelmOptional, HelmToggle\n"
        "from tests.unit.bundle.helm_intake_app import (\n"
        "    LISTEN_EVERYWHERE, agent, chart as base, intake, intake_policy,\n"
        ")\n"
        f"chart = dataclasses.replace(base, {change})\n"
    )
    return f"{module}:chart"


def _write(tmp_path: Path, change: str, name: str = "chart") -> Path:
    out = tmp_path / name
    target = _module(tmp_path, change, name)
    result = _run(target, "--helm", "--out", str(out), "--version", "1.0.0")
    assert result.exit_code == 0, result.output
    return out


def _refused(tmp_path: Path, change: str, name: str) -> str:
    target = _module(tmp_path, change, name)
    result = _run(target, "--helm", "--out", str(tmp_path / f"{name}-out"))
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "bundle-invalid"
    assert not (tmp_path / f"{name}-out").exists()
    return str(body["message"])


def _template(helm: str, chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [helm, "template", "agent", str(chart), "-n", "edge", *BASE, *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(chart.parent)},
    )


def _kinds(text: str) -> set[tuple[str, str]]:
    return {
        (item["kind"], item["metadata"]["name"])
        for item in yaml.safe_load_all(text)
        if item
    }


# ------------------------------------------------------------ the text


def test_a_gate_on_two_values_is_one_if_and(tmp_path: Path) -> None:
    out = _write(tmp_path, GATED)
    policy = (out / POLICY).read_text().splitlines()
    collector = f'(or (kindIs "invalid" {COLLECTOR}) {COLLECTOR})'
    assert policy[1] == f"{{{{- if and {collector} {EXPOSE} }}}}"
    assert policy[-1] == "{{- end }}"
    assert (out / SERVICE).read_text().splitlines()[1] == f"{{{{- if {EXPOSE} }}}}"
    assert not (out / REQUIRES).exists()
    values = (out / "values.yaml").read_text()
    assert (
        "# collector.enabled: Run the collector; only with intake.expose=true "
        "(true installs its objects)"
    ) in values
    assert yaml.safe_load(values)["collector"] == {"enabled": True}
    schema = json.loads((out / "values.schema.json").read_text())["properties"]
    node = schema["collector"]["properties"]["enabled"]
    assert node == {
        "type": "boolean",
        "description": "Run the collector; only with intake.expose=true",
    }
    readme = (out / "README.md").read_text()
    assert (
        "- `collector.enabled` (true or false): Run the collector; true installs "
        "NetworkPolicy/agent-intake; only with intake.expose=true"
    ) in readme


def test_a_gate_on_a_toggle_also_gates_the_patches(tmp_path: Path) -> None:
    change = (
        "optional={'intake.expose': HelmOptional('Expose the intake', "
        "objects=[intake, intake_policy], patches=[(agent, LISTEN_EVERYWHERE)], "
        "when='relayOnly')}"
    )
    out = _write(tmp_path, change)
    gate = f"{{{{- if and {EXPOSE} ($.Values.relayOnly) }}}}"
    agent = (out / AGENT).read_text()
    assert agent.splitlines()[1] == gate and "{{- else }}" in agent
    assert (out / SERVICE).read_text().splitlines()[1] == gate
    assert (out / POLICY).read_text().splitlines()[1] == gate


def test_requires_renders_a_check_and_notes_it(tmp_path: Path) -> None:
    out = _write(tmp_path, REQUIRED)
    assert (out / REQUIRES).read_text() == (
        "# piceli-requires.yaml: values that need other values "
        "(piceli bundle agent 1.0.0)\n"
        f"{{{{- if and {COLLECTOR} (not {EXPOSE}) }}}}\n"
        '{{- fail "collector.enabled=true needs intake.expose=true" }}\n'
        "{{- end }}\n"
    )
    assert (out / POLICY).read_text().splitlines()[1] == f"{{{{- if {COLLECTOR} }}}}"
    note = "needs intake.expose=true (else the install fails)"
    schema = json.loads((out / "values.schema.json").read_text())["properties"]
    assert schema["collector"]["properties"]["enabled"]["description"] == (
        f"Run the collector; {note}"
    )
    assert (
        f"# collector.enabled: Run the collector; {note}"
        in (out / "values.yaml").read_text()
    )
    assert f"NetworkPolicy/agent-intake; {note}" in (out / "README.md").read_text()


def test_a_chart_without_either_renders_as_before(tmp_path: Path) -> None:
    out = tmp_path / "plain"
    result = _run(INTAKE, "--helm", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert not (out / REQUIRES).exists()
    text = "".join(path.read_text() for path in (out / "templates").glob("*.yaml"))
    assert "{{- if and" not in text and "{{- fail" not in text
    assert (out / SERVICE).read_text().splitlines()[1] == f"{{{{- if {EXPOSE} }}}}"
    assert (out / POLICY).read_text().splitlines()[1] == f"{{{{- if {EXPOSE} }}}}"
    for name in ("values.yaml", "values.schema.json", "README.md"):
        body = (out / name).read_text()
        assert "only with" not in body and "else the install fails" not in body


# ------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "optional={'collector.enabled': HelmOptional('c', objects=[intake], "
            "when=['intake.nope'])}",
            "optional collector.enabled when names ['intake.nope'], which are not "
            "toggles or optional values",
        ),
        (
            "optional={'collector.enabled': HelmOptional('c', objects=[intake], "
            "when=['collector.enabled'])}",
            "optional collector.enabled cannot wait on itself (when)",
        ),
        (
            "optional={'collector.enabled': HelmOptional('c', objects=[intake], "
            "when=['relayOnly', 'relayOnly'])}",
            "when names a value twice",
        ),
        (
            "requires={'relayOnly': ['team']}",
            "requires names ['team'], which are not toggles or optional values",
        ),
        ("requires={'relayOnly': ['relayOnly']}", "requires: relayOnly needs itself"),
        ("requires={'relayOnly': 'intake.expose'}", "a list of value names"),
        ("requires={'relayOnly': []}", "requires 'relayOnly' names no value"),
        (
            "requires={'relayOnly': ['intake.expose'], 'intake.expose': ['relayOnly']}",
            "requires has a cycle: intake.expose -> relayOnly -> intake.expose",
        ),
        (
            "optional={"
            + INTAKE_EXPOSE
            + ", 'collector.enabled': HelmOptional('c', objects=[intake_policy], "
            "default=True)}, requires={'collector.enabled': ['intake.expose']}",
            "requires: collector.enabled defaults to true and intake.expose to "
            "false, so the default install would fail",
        ),
    ],
    ids=[
        "when-unknown", "when-self", "when-twice", "requires-setting",
        "requires-self", "requires-string", "requires-empty", "requires-cycle",
        "requires-defaults",
    ],
)  # fmt: skip
def test_bad_dependencies_are_refused(
    tmp_path: Path, change: str, message: str
) -> None:
    assert message in _refused(tmp_path, change, "refused")


def test_a_longer_cycle_is_refused(tmp_path: Path) -> None:
    change = (
        "optional={'a.on': HelmOptional('a', objects=[intake_policy]), "
        "'b.on': HelmOptional('b', objects=[intake])}, "
        "requires={'a.on': ['b.on'], 'b.on': ['relayOnly'], 'relayOnly': ['a.on']}"
    )
    message = _refused(tmp_path, change, "cycle")
    assert "requires has a cycle: a.on -> b.on -> relayOnly -> a.on" in message


def test_the_declarations_check_their_shape() -> None:
    assert HelmOptional("x", when="a.on").conditions() == ("a.on",)
    assert HelmOptional("x", when=["a.on", "b.on"]).conditions() == ("a.on", "b.on")
    assert HelmOptional("x").conditions() == ()
    with pytest.raises(ValueError, match="when must list value names"):
        HelmOptional("x", when=[1]).conditions()  # type: ignore[list-item]
    chart = HelmChart(None, requires={"a.on": ["b.on", "c.on"]})
    assert chart.requirements() == {"a.on": ("b.on", "c.on")}
    with pytest.raises(ValueError, match="names a value twice"):
        HelmChart(None, requires={"a.on": ["b.on", "b.on"]}).requirements()
    assert HelmChart(None).requirements() == {}


# ------------------------------------------------------------ with helm


@pytest.mark.parametrize(
    ("expose", "collector", "service", "policy"),
    [
        ("true", "true", True, True),
        ("true", "false", True, False),
        ("false", "true", False, False),
        ("false", "false", False, False),
    ],
)
def test_helm_renders_the_gated_objects_only_when_both_hold(
    helm: str,
    tmp_path: Path,
    expose: str,
    collector: str,
    service: bool,
    policy: bool,
) -> None:
    out = _write(tmp_path, GATED)
    result = _template(
        helm, out, "--set", f"intake.expose={expose}",
        "--set", f"collector.enabled={collector}",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    kinds = _kinds(result.stdout)
    assert (("Service", "agent-intake") in kinds) is service
    assert (("NetworkPolicy", "agent-intake") in kinds) is policy


def test_helm_reads_a_missing_gate_value_as_its_default(
    helm: str, tmp_path: Path
) -> None:
    """``collector.enabled`` has ``default_on_reuse``: stored values without
    it (an older release) read as on, so only ``intake.expose`` decides."""
    out = _write(tmp_path, GATED)
    values = yaml.safe_load((out / "values.yaml").read_text())
    (out / "values.yaml").write_text(
        yaml.safe_dump({k: v for k, v in values.items() if k != "collector"})
    )
    (out / "values.schema.json").unlink()
    on = _template(helm, out, "--set", "intake.expose=true")
    assert on.returncode == 0, on.stderr
    assert ("NetworkPolicy", "agent-intake") in _kinds(on.stdout)
    off = _template(helm, out)
    assert off.returncode == 0, off.stderr
    assert ("NetworkPolicy", "agent-intake") not in _kinds(off.stdout)


@pytest.mark.parametrize(
    ("expose", "collector", "fails"),
    [
        ("true", "true", False),
        ("true", "false", False),
        ("false", "true", True),
        ("false", "false", False),
    ],
)
def test_helm_refuses_a_value_without_the_one_it_needs(
    helm: str, tmp_path: Path, expose: str, collector: str, fails: bool
) -> None:
    out = _write(tmp_path, REQUIRED)
    result = _template(
        helm, out, "--set", f"intake.expose={expose}",
        "--set", f"collector.enabled={collector}",
    )  # fmt: skip
    if fails:
        assert result.returncode != 0
        assert "collector.enabled=true needs intake.expose=true" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    kinds = _kinds(result.stdout)
    assert (("NetworkPolicy", "agent-intake") in kinds) is (collector == "true")


# ------------------------------------------------------------ install notes

NOTE = "The intake is at https://agent-intake.{{ .Release.Namespace }}.svc:4318"
NOTED = (
    "optional={"
    + INTAKE_EXPOSE
    + ", 'collector.enabled': HelmOptional('Run the collector', "
    "objects=[intake_policy], when=['intake.expose'], "
    f"note={NOTE!r})}}"
)


def test_a_note_prints_only_when_its_objects_render(tmp_path: Path) -> None:
    """0.95.0 (``HelmOptional(note=...)``): one NOTES.txt line under the
    value's own test, ``when`` included; listed in the README."""
    out = _write(tmp_path, NOTED)
    notes = (out / "templates" / "NOTES.txt").read_text().splitlines()
    assert notes[-3:] == [
        f"{{{{- if and {COLLECTOR} {EXPOSE} }}}}",
        NOTE,
        "{{- end }}",
    ]
    readme = (out / "README.md").read_text()
    assert f"; the install notes then say: `{NOTE}`" in readme


def test_a_note_of_several_lines_is_refused(tmp_path: Path) -> None:
    change = NOTED.replace(repr(NOTE), repr("one\ntwo"))
    assert "note must be one line" in _refused(tmp_path, change, "lines")


@pytest.mark.parametrize(("expose", "shown"), [("true", True), ("false", False)])
def test_helm_prints_the_note_only_when_the_value_holds(
    helm: str, tmp_path: Path, expose: str, shown: bool
) -> None:
    out = _write(tmp_path, NOTED)
    result = subprocess.run(
        [helm, "install", "agent", str(out), "-n", "edge", "--dry-run=client",
         *BASE, "--set", f"intake.expose={expose}",
         "--set", "collector.enabled=true"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": os.environ["PATH"], "HOME": str(out.parent)},
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert (
        "The intake is at https://agent-intake.edge.svc:4318" in result.stdout
    ) is shown
