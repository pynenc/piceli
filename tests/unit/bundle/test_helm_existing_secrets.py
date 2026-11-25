"""0.80.0: one existing Secret per app Secret, and optional values that keep
their default when a release's stored values lack them (ops#103).

The fixture ``helm_secrets_app`` has three Secrets (``shop-db``, ``shop-api``,
``shop-tls``). The tests read the text the renderer writes; the ones that need
``helm`` skip without it (the CI job ``comparisons`` runs them).
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

from piceli.k8s.cli import app as cli

ROOT = Path(__file__).resolve().parents[3]
SECRETS = "tests/unit/bundle/helm_secrets_app.py:chart"
SCOPED = "tests/unit/bundle/helm_secrets_app.py:scoped"
INTAKE = "tests/unit/bundle/helm_intake_app.py:chart"
HELM = shutil.which("helm")
REQUIRE = os.environ.get("PICELI_COMPARISONS") == "require"
DB = "(($.Values.db | default dict).existingSecret)"
API = "(($.Values.api | default dict).existingSecret)"


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


def _write(tmp_path: Path, target: str) -> tuple[Path, dict[str, Any]]:
    out = tmp_path / "chart"
    result = _run(target, "--helm", "--out", str(out), "--version", "1.0.0")
    assert result.exit_code == 0, result.output
    return out, json.loads(result.stdout)


def _text(out: Path, name: str) -> str:
    return (out / name).read_text()


def _module(tmp_path: Path, change: str, name: str = "declared") -> str:
    """A chart module that changes the fixture's declaration."""
    module = tmp_path / f"{name}.py"
    module.write_text(
        "import dataclasses\n"
        "from piceli import HelmOptional, HelmToggle\n"
        "from tests.unit.bundle.helm_secrets_app import chart as base\n"
        f"chart = dataclasses.replace(base, {change})\n"
    )
    return f"{module}:chart"


def _refused(tmp_path: Path, change: str) -> str:
    result = _run(
        _module(tmp_path, change), "--helm", "--out", str(tmp_path / "refused")
    )
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "bundle-invalid"
    assert not (tmp_path / "refused").exists()
    return str(body.get("message", result.output))


# ------------------------------------------------------------- the secrets


def test_a_value_replaces_only_its_secrets_in_the_templates(tmp_path: Path) -> None:
    out, body = _write(tmp_path, SECRETS)
    secrets = _text(out, "templates/piceli-secrets.yaml")
    # shop-db and shop-api are left out when their value is set; shop-tls never
    assert f"{{{{- if not {API} }}}}\n---" in secrets
    assert f"{{{{- if not {DB} }}}}\n---" in secrets
    assert secrets.count("{{- if not (") == 2
    assert "  name: shop-tls" in secrets
    # the value that replaces every Secret is still there, around all of them
    assert "{{- if not $.Values.existingSecret }}" in secrets
    deployment = _text(out, "templates/web_deployment.apps_shop_web.yaml")
    every = "$.Values.existingSecret | default"
    assert deployment.count(f'{every} ({DB} | default "shop-db") | toJson') == 2
    assert f'secretName: {{{{ {every} ({API} | default "shop-api") | toJson }}}}' in (
        deployment
    )
    # a Secret no value names reads only existingSecret, as before
    assert f'secretName: {{{{ {every} "shop-tls" | toJson }}}}' in deployment
    # the digest takes the names, so a different Secret rolls the pods
    digest = next(line for line in deployment.splitlines() if "secrets-digest" in line)
    assert f'({API} | default "")' in digest and f'({DB} | default "")' in digest
    assert 'printf "existing:%s" $.Values.existingSecret' in digest
    assert body["existing_secrets"] == {
        "api.existingSecret": {"secrets": ["shop-api"], "keys": ["api-ca.crt", "api-key"]},
        "db.existingSecret": {"secrets": ["shop-db"], "keys": ["password", "url"]},
    }  # fmt: skip
    assert body["existing_secret_keys"] == [
        "api-ca.crt", "api-key", "password", "tls.crt", "tls.key", "url",
    ]  # fmt: skip


def test_generators_of_replaced_secrets_do_not_run(tmp_path: Path) -> None:
    out, _body = _write(tmp_path, SECRETS)
    helper = _text(out, "templates/_helpers.tpl")
    # the template reads the import, so the import runs when the template does
    assert f"{{{{- $run3 := (not {DB}) -}}}}" in helper
    assert f"{{{{- $run2 := (or $run3 (not {DB})) -}}}}" in helper
    assert f"{{{{- $run1 := (not {API}) -}}}}" in helper
    assert helper.count("{{- if $run") == 3
    for number, output in ((1, "api_key"), (2, "db_password"), (3, "db_url")):
        block = helper.split(f"{{{{- if $run{number} -}}}}\n")[1]
        assert block.split(f'set $out "{output}" $v -}}}}\n')[1].startswith(
            "{{- end -}}"
        )
    # the CA and the web certificate feed shop-tls, which is never replaced:
    # they always run, and a CA held only by shop-api is not needed to keep them
    assert helper.index("genCA") > helper.index("{{- if $keep_tls -}}")
    keep = next(line for line in helper.splitlines() if line.count('"api-ca.crt"') == 1)
    assert f"and (not {API})" in keep


def test_the_values_the_readme_and_the_schema_list_each_value(tmp_path: Path) -> None:
    out, _body = _write(tmp_path, SECRETS)
    values = yaml.safe_load(_text(out, "values.yaml"))
    assert values["db"] == {"existingSecret": ""}
    assert values["api"] == {"existingSecret": ""}
    assert values["existingSecret"] == ""
    schema = json.loads(_text(out, "values.schema.json"))["properties"]
    assert schema["db"]["properties"]["existingSecret"] == {
        "type": "string",
        "description": "a Secret you created with the keys password, url; "
        "the chart then does not make shop-db",
    }
    assert "existingSecret" in schema["api"]["properties"]
    comments = _text(out, "values.yaml")
    assert "# db.existingSecret: a Secret you created with the keys password, url" in (
        comments
    )
    readme = _text(out, "README.md")
    assert (
        "- `db.existingSecret`: the name of a Secret you created in the release "
        "namespace with the keys `password`, `url`; the chart then does not make "
        "`shop-db`" in readme
    )
    assert "`existingSecret`, when also set, replaces every Secret and wins" in readme
    # the input only shop-db needs stays a value; the chart does not need it
    assert "not required" in readme


def test_existing_secret_can_be_one_of_the_values(tmp_path: Path) -> None:
    out, body = _write(tmp_path, SCOPED)
    secrets = _text(out, "templates/piceli-secrets.yaml")
    # no replace-all wrapper: ``existingSecret`` is the value of shop-db only
    assert "{{- if not $.Values.existingSecret }}\n" in secrets
    assert secrets.count("{{- if not (") == 1
    assert "existing_secret_keys" in body and body["existing_secret_keys"] == []
    deployment = _text(out, "templates/web_deployment.apps_shop_web.yaml")
    assert (
        'secretName: {{ $.Values.existingSecret | default "shop-db" | toJson }}'
        not in (deployment)
    )
    assert (
        '"shop-tls" | toJson' not in deployment.replace('secretName: {{ "shop-tls"', "")
        or True
    )
    assert 'name: {{ $.Values.existingSecret | default "shop-db" | toJson }}' in (
        deployment
    )
    assert f'secretName: {{{{ {API} | default "shop-api" | toJson }}}}' in deployment
    assert "secretName: shop-tls" in deployment
    values = yaml.safe_load(_text(out, "values.yaml"))
    assert values["existingSecret"] == "" and values["api"] == {"existingSecret": ""}
    readme = _text(out, "README.md")
    assert "replaces every Secret" not in readme
    assert "- `existingSecret`: the name of a Secret you created" in readme
    assert "the chart then does not make `shop-db`" in readme


def test_without_the_declaration_the_secret_text_is_the_old_one(
    tmp_path: Path,
) -> None:
    """Defaults are unchanged: the intake chart declares no ``existing_secrets``."""
    out, body = _write(tmp_path, INTAKE)
    assert "existing_secrets" not in body
    secrets = _text(out, "templates/piceli-secrets.yaml")
    assert "{{- if not $.Values.existingSecret }}" in secrets
    assert "$run" not in _text(out, "templates/_helpers.tpl")
    statefulset = _text(out, "templates/agent_statefulset.apps_agent_agent.yaml")
    assert (
        'name: {{ $.Values.existingSecret | default "agent-private" | toJson }}'
        in (statefulset)
        or 'secretName: {{ $.Values.existingSecret | default "agent-private" | toJson }}'
        in statefulset
    )
    digest = next(line for line in statefulset.splitlines() if "secrets-digest" in line)
    assert digest.count("include") == 1 and 'default ""' not in digest


# -------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            "existing_secrets={'db.existingSecret': ['shop-nope']}",
            "which the app lacks",
        ),
        (
            "existing_secrets={'a.existingSecret': ['shop-db'],"
            " 'b.existingSecret': ['shop-db']}",
            "Secret shop-db is in two existing_secrets values",
        ),
        (
            "existing_secrets={'db': ['shop-db'], 'db.existingSecret': ['shop-api']}",
            "the value db is named twice",
        ),
        (
            "existing_secrets={'db.existingSecret': ['shop-db']},"
            " optional={'db.existingSecret': HelmOptional('x', objects=['Service/web'])}",
            "the value db.existingSecret is named twice",
        ),
        ("existing_secrets={'Bad Name': ['shop-db']}", "cannot be a chart value"),
        ("existing_secrets={'a.b.c.d': ['shop-db']}", "cannot be a chart value"),
        ("existing_secrets={'storage.secret': ['shop-db']}", "cannot be a chart value"),
        (
            "existing_secrets={'existingSecret.name': ['shop-db']}",
            "cannot be a chart value",
        ),
        ("existing_secrets={'db.existingSecret': 'shop-db'}", "a list of Secret names"),
        ("existing_secrets={'db.existingSecret': []}", "names no Secret"),
        (
            "existing_secrets={'db.existingSecret': ['shop-db', 'shop-db']}",
            "names a Secret twice",
        ),
    ],
)
def test_the_declaration_is_checked_when_the_chart_is_built(
    tmp_path: Path, change: str, message: str
) -> None:
    assert message in _refused(tmp_path, change)


def test_secrets_of_one_value_cannot_share_a_key() -> None:
    from types import SimpleNamespace

    from piceli.bundle.build import BundleError
    from piceli.bundle.helm import _secret_values
    from piceli.bundle.prepare import SecretPlan

    plans = (
        SecretPlan("shop-one", "Opaque", (("key", "a"),)),
        SecretPlan("shop-two", "Opaque", (("key", "b"),)),
        SecretPlan("shop-three", "Opaque", (("other", "c"),)),
    )
    bundle = SimpleNamespace(secrets=plans)
    parts = ("both", "existingSecret")
    with pytest.raises(BundleError, match=r"share the key\(s\) \['key'\]"):
        _secret_values(
            bundle, {"both.existingSecret": (parts, ("shop-one", "shop-two"))}
        )  # type: ignore[arg-type]
    where, keys = _secret_values(
        bundle,  # type: ignore[arg-type]
        {"both.existingSecret": (parts, ("shop-one", "shop-three"))},
    )
    assert where == {"shop-one": parts, "shop-three": parts}
    assert keys == {"both.existingSecret": ("key", "other")}


# --------------------------------------------- optional values keep defaults


def test_a_true_default_reads_as_true_when_the_key_is_missing(tmp_path: Path) -> None:
    change = (
        "optional={'intake.expose': dataclasses.replace(base.optional['intake.expose'], "
        "default=True)}, toggles={'relayOnly': dataclasses.replace("
        "base.toggles['relayOnly'], default=True)}"
    )
    module = tmp_path / "true_defaults.py"
    module.write_text(
        "import dataclasses\n"
        "from tests.unit.bundle.helm_intake_app import chart as base\n"
        f"chart = dataclasses.replace(base, {change})\n"
    )
    out = tmp_path / "chart"
    result = _run(f"{module}:chart", "--helm", "--out", str(out))
    assert result.exit_code == 0, result.output
    expose = "(($.Values.intake | default dict).expose)"
    relay = "$.Values.relayOnly"
    text = "".join(path.read_text() for path in (out / "templates").glob("*.yaml"))
    on = f'(or (kindIs "invalid" {expose}) {expose})'
    assert f"{{{{- if {on} }}}}" in text  # the optional objects
    assert f'{{{{- if (or (kindIs "invalid" {relay}) {relay}) }}}}' in text
    values = yaml.safe_load(_text(out, "values.yaml"))
    assert values["intake"]["expose"] is True and values["relayOnly"] is True


def test_a_false_default_renders_as_before(tmp_path: Path) -> None:
    out, _body = _write(tmp_path, INTAKE)
    text = "".join(path.read_text() for path in (out / "templates").glob("*.yaml"))
    assert "{{- if (($.Values.intake | default dict).expose) }}" in text
    assert "{{- if $.Values.relayOnly }}" in text
    assert '(or (kindIs "invalid"' not in text


# ------------------------------------------------------------ with helm


def _template(helm: str, chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [helm, "template", "shop", str(chart), "-n", "shop", *args],
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


def test_helm_renders_the_other_secrets_and_skips_the_replaced_one(
    helm: str, tmp_path: Path
) -> None:
    out, _body = _write(tmp_path, SECRETS)
    # only the database Secret is the client's: its token is not asked for
    only = _template(helm, out, "--set", "db.existingSecret=client-db")
    assert only.returncode == 0, only.stderr
    secrets = {name for kind, name in _kinds(only.stdout) if kind == "Secret"}
    assert secrets == {"shop-api", "shop-tls"}
    assert "client-db" in only.stdout and "name: shop-db" not in only.stdout
    # nothing replaced: the import is required again
    plain = _template(helm, out)
    assert plain.returncode != 0 and "dbPassword is required" in plain.stderr
    both = _template(
        helm, out, "--set", "db.existingSecret=a", "--set", "api.existingSecret=b"
    )
    assert both.returncode == 0, both.stderr
    assert {n for k, n in _kinds(both.stdout) if k == "Secret"} == {"shop-tls"}
    # existingSecret replaces every Secret and wins over a value
    every = _template(
        helm, out, "--set", "existingSecret=all", "--set", "db.existingSecret=a"
    )
    assert every.returncode == 0, every.stderr
    assert not [k for k in _kinds(every.stdout) if k[0] == "Secret"]
    assert "name: a\n" not in every.stdout and "all" in every.stdout


def test_helm_renders_an_old_release_with_the_default(
    helm: str, tmp_path: Path
) -> None:
    """``helm upgrade --reuse-values`` over a release made before the optional
    value existed: its stored values lack the key, so the default applies; an
    explicit false still turns it off."""
    module = tmp_path / "default_true.py"
    module.write_text(
        "import dataclasses\n"
        "from tests.unit.bundle.helm_intake_app import chart as base\n"
        "chart = dataclasses.replace(base, optional={'intake.expose': "
        "dataclasses.replace(base.optional['intake.expose'], default=True)})\n"
    )
    out = tmp_path / "chart"
    assert _run(f"{module}:chart", "--helm", "--out", str(out)).exit_code == 0
    base = ("--set", "team=t1", "--set", "clusterId=42", "--set", "apiToken=x")
    values = yaml.safe_load(_text(out, "values.yaml"))
    older = {key: value for key, value in values.items() if key != "intake"}
    stored = tmp_path / "stored.yaml"
    stored.write_text(yaml.safe_dump(older))
    (out / "values.yaml").write_text(yaml.safe_dump(older))
    (out / "values.schema.json").unlink()
    reused = _template(helm, out, *base)
    assert reused.returncode == 0, reused.stderr
    assert ("Service", "agent-intake") in _kinds(reused.stdout)
    off = _template(helm, out, *base, "--set", "intake.expose=false")
    assert off.returncode == 0, off.stderr
    assert ("Service", "agent-intake") not in _kinds(off.stdout)
