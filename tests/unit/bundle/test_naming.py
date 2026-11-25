"""``piceli bundle`` names: the operator sees the owner's names, not the tool's.

The check of the feature: a bundle rendered with ``--input-prefix``,
``--tool-name`` and ``--annotation-domain`` has no file, path or text that
holds the product's own name; with the defaults nothing changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import Secrets, Sops
from piceli.bundle.build import build_bundle, manifest, render_files
from piceli.bundle.naming import Naming
from piceli.bundle.prepare import SecretPlan, client_inputs, input_variable, script
from piceli.bundle.support import bundle_marker
from piceli.k8s.cli import app as cli
from tests.unit.bundle.fixtures import BUILD, app, components
from tests.unit.bundle.test_prepare import _run

HERE = Path(__file__).parent
ROOT = Path(__file__).resolve().parents[3]
OPTIONS = (
    "--input-prefix",
    "SHOP_INPUT_",
    "--tool-name",
    "shop bundle",
    "--annotation-domain",
    "shop.example",
)
CUSTOM = Naming("SHOP_INPUT_", "shop bundle", "shop.example")
#: What may stay in an output after a re-render with the options above.
#: Nothing: if something unavoidable shows up, list it here and say why.
ALLOWED_LEFTOVERS: frozenset[str] = frozenset()


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _leaks(files: dict[str, bytes]) -> list[str]:
    """Every path or line that names the product (case-insensitive)."""
    found = [name for name in files if "piceli" in name.lower()]
    for name, body in files.items():
        found += [
            f"{name}: {line.strip()[:120]}"
            for line in body.decode(errors="replace").splitlines()
            if "piceli" in line.lower()
        ]
    return sorted(set(found) - ALLOWED_LEFTOVERS)


def _bundle(naming: Naming | None = None, **overrides: Any):
    shop, generators = app(web_image=BUILD)
    items = components(shop, generators)
    # A rule waived on one object: its annotation is under the domain too.
    first = items[0]["resources"][0]["manifest"]
    first.setdefault("metadata", {}).setdefault("annotations", {})[
        "safety.piceli.io/allow-network"
    ] = "the client network is flat"
    return build_bundle(
        items,
        name="shop",
        version="1.2.3",
        namespace="shop",
        generators=generators.generators,
        receipt_names=["web"],
        environment="client",
        storage_class="fast",
        naming=naming,
        **overrides,
    )


def _files(bundle) -> dict[str, bytes]:
    files = render_files(bundle)
    files["bundle.json"] = json.dumps(manifest(bundle), sort_keys=True).encode()
    return files


# ------------------------------------------------------------ the names


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_prefix", "shop_input_"),
        ("input_prefix", "1SHOP_"),
        ("input_prefix", ""),
        ("input_prefix", "SHOP-INPUT_"),
        ("tool_name", ""),
        ("tool_name", "shop\nbundle"),
        ("tool_name", "$(shop)"),
        ("annotation_domain", "Shop.Example"),
        ("annotation_domain", "shop..example"),
        ("annotation_domain", "-shop.example"),
        ("annotation_domain", "shop.example/"),
        ("annotation_domain", "https://shop.example"),
        ("annotation_domain", "kubernetes.io"),
        ("annotation_domain", "x.k8s.io"),
    ],
)
def test_a_bad_name_is_refused(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="--"):
        Naming(**{field: value})


def test_the_defaults_are_the_names_of_every_earlier_bundle() -> None:
    naming = Naming()
    assert naming.input_prefix == "PICELI_INPUT_"
    assert naming.tool_name == "piceli bundle"
    assert naming.key("bundle") == "piceli.io/bundle"
    assert naming.safety_prefix == "safety.piceli.io/allow-"
    assert naming.image_host == "images.piceli.invalid"
    assert naming.schema == "piceli.client-bundle.v1"
    assert naming.secrets_template == "templates/piceli-secrets.yaml"
    assert naming.tool_with_helm == "piceli bundle --helm"
    manifest_ = {"metadata": {"annotations": {"piceli.io/bundle": "x"}}}
    assert naming.apply(manifest_) is manifest_


def test_apply_renames_keys_selectors_and_field_paths_not_the_input() -> None:
    original = {
        "metadata": {
            "labels": {"piceli.io/part": "a", "app": "web"},
            "annotations": {
                "piceli.io/bundle": "b",
                "safety.piceli.io/allow-rbac": "why",
                "other.io/x": "piceli.io/bundle",
            },
        },
        "spec": {
            "selector": {"matchLabels": {"piceli.io/part": "a"}},
            "nodeSelector": {"piceli.io/builder": "true"},
            "env": [
                {
                    "valueFrom": {
                        "fieldRef": {
                            "fieldPath": "metadata.annotations['piceli.io/helm-release']"
                        }
                    }
                }
            ],
        },
    }
    copy = json.loads(json.dumps(original))
    done = CUSTOM.apply(original)
    assert original == copy
    assert done["metadata"]["labels"] == {"shop.example/part": "a", "app": "web"}
    assert done["metadata"]["annotations"] == {
        "shop.example/bundle": "b",
        "safety.shop.example/allow-rbac": "why",
        "other.io/x": "piceli.io/bundle",
    }
    # A selector keeps matching the pod labels it selects.
    assert done["spec"]["selector"]["matchLabels"] == {"shop.example/part": "a"}
    # A node label belongs to the cluster, not to the bundle.
    assert done["spec"]["nodeSelector"] == {"piceli.io/builder": "true"}
    path = done["spec"]["env"][0]["valueFrom"]["fieldRef"]["fieldPath"]
    assert path == "metadata.annotations['shop.example/helm-release']"


def test_the_input_variable_takes_the_prefix() -> None:
    assert input_variable("api-key.v2") == "PICELI_INPUT_API_KEY_V2"
    assert input_variable("api-key.v2", CUSTOM) == "SHOP_INPUT_API_KEY_V2"


def test_prepare_reads_the_prefixed_variable(tmp_path: Path) -> None:
    generators = Secrets(db=Sops("secrets.enc.yaml", "db.password")).generators
    plans = [SecretPlan("db", "Opaque", (("password", "db"),))]
    assert client_inputs(plans, generators, CUSTOM)[0]["variable"] == "SHOP_INPUT_DB"
    text = script("shop", plans, generators, CUSTOM)
    assert "written by shop bundle" in text
    assert "set SHOP_INPUT_DB to a readable file" in text
    refused = _run(tmp_path, text, "shop", PICELI_INPUT_DB="/dev/null")
    assert refused.returncode == 1 and "SHOP_INPUT_DB" in refused.stderr
    value = tmp_path / "db.txt"
    value.write_text("from-the-client")
    done = _run(tmp_path, text, "shop", SHOP_INPUT_DB=str(value))
    assert done.returncode == 0, done.stderr
    assert not _leaks({"prepare.sh": text.encode()})


# -------------------------------------------------- the defaults hold


def test_default_names_change_nothing() -> None:
    assert _files(_bundle()) == _files(_bundle(Naming()))
    files = _files(_bundle())
    assert (
        b"piceli.io/bundle: shop@1.2.3"
        in files["base/web_deployment.apps_shop_web.yaml"]
    )
    assert b"safety.piceli.io/allow-network" in b"".join(files.values())
    assert (
        b"images.piceli.invalid/web" in files["base/web_deployment.apps_shop_web.yaml"]
    )
    assert b"written by piceli bundle)." in files["prepare.sh"]


def test_the_custom_names_only_rename_never_restructure() -> None:
    """Undoing the renames gives the default output: nothing else differs."""
    default = _files(_bundle())
    custom = _files(_bundle(CUSTOM))
    assert sorted(default) == sorted(custom)
    undo = (
        ("safety.shop.example/", "safety.piceli.io/"),
        ("shop.example/", "piceli.io/"),
        ("images.shop.invalid", "images.piceli.invalid"),
        ("shop.client-bundle.v1", "piceli.client-bundle.v1"),
        ("shop bundle", "piceli bundle"),
        ("SHOP_INPUT_", "PICELI_INPUT_"),
    )
    for name, body in custom.items():
        text = body.decode()
        for new, old in undo:
            text = text.replace(new, old)
        # YAML keys are sorted: a renamed key may move, so compare the lines.
        assert sorted(text.splitlines()) == sorted(
            default[name].decode().splitlines()
        ), name


def test_a_kustomize_bundle_has_no_product_name() -> None:
    files = _files(_bundle(CUSTOM))
    assert _leaks(files) == []
    assert (
        b"shop.example/bundle: shop@1.2.3"
        in files["base/web_deployment.apps_shop_web.yaml"]
    )
    assert (
        b"shop.example/secrets-digest"
        in files["base/cache_statefulset.apps_shop_cache.yaml"]
    )
    assert b"safety.shop.example/allow-network" in b"".join(files.values())
    assert json.loads(files["bundle.json"])["schema"] == "shop.client-bundle.v1"


def test_the_gate_reads_the_waiver_before_the_rename() -> None:
    """A rule waived with the default key is still waived under another domain."""
    bundle = _bundle(CUSTOM)
    assert [item.rule for item in bundle.exceptions] == ["network"]


# ------------------------------------------------------------------ CLI


def _bundle_cli(*args: str) -> Any:
    return CliRunner().invoke(cli, ["bundle", *args])


def _written(tmp_path: Path, name: str, *args: str) -> dict[str, bytes]:
    out = tmp_path / name
    result = _bundle_cli(*args, "--out", str(out))
    assert result.exit_code == 0, result.output
    return _tree(out)


def test_cli_kustomize_bundle_has_no_product_name(tmp_path: Path) -> None:
    target = f"{HERE / 'cli_app.py'}:pipeline"
    plain = _written(tmp_path, "plain", target, "--env", "client")
    defaults = _written(
        tmp_path,
        "defaults",
        target,
        "--env",
        "client",
        "--input-prefix",
        "PICELI_INPUT_",
        "--tool-name",
        "piceli bundle",
        "--annotation-domain",
        "piceli.io",
    )
    assert defaults == plain
    assert _leaks(plain)  # the default output does name the tool
    named = _written(tmp_path, "named", target, "--env", "client", *OPTIONS)
    assert sorted(named) == sorted(plain)
    assert _leaks(named) == []
    assert b"written by shop bundle)." in named["prepare.sh"]
    # SHA256SUMS still covers the files it lists.
    assert b"prepare.sh" in named["SHA256SUMS"]


def test_cli_helm_chart_has_no_product_name(tmp_path: Path) -> None:
    import os

    target = "tests/unit/bundle/helm_app.py:chart"
    cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        plain = _written(tmp_path, "plain", target, "--helm", "--version", "1.2.3")
        named = _written(
            tmp_path, "named", target, "--helm", "--version", "1.2.3", *OPTIONS
        )
    finally:
        os.chdir(cwd)
    assert _leaks(plain)
    assert _leaks(named) == []
    assert "templates/shop-secrets.yaml" in named
    assert "templates/piceli-secrets.yaml" in plain
    assert b"(written by shop bundle)." in named["values.yaml"]
    assert b"(written by piceli bundle --helm)." in plain["values.yaml"]
    deployed = named["templates/agent_statefulset.apps_agent_agent.yaml"].decode()
    for key in ("configmaps-digest", "secrets-digest", "helm-release"):
        assert f"shop.example/{key}:" in deployed
    # The pod reads the annotation it writes.
    assert "fieldPath: metadata.annotations['shop.example/helm-release']" in deployed
    # Moving a chart to another domain changes only keys and the tool's name.
    undo = (
        ("shop.example/", "piceli.io/"),
        ("shop bundle", "piceli bundle"),
        ("shop-secrets", "piceli-secrets"),
        ("(written by piceli bundle).", "(written by piceli bundle --helm)."),
        ("`piceli bundle`", "`piceli bundle --helm`"),
    )
    for name, body in named.items():
        if name in {"SHA256SUMS"}:
            continue
        text = body.decode()
        for new, old in undo:
            text = text.replace(new, old)
        expected = name.replace("shop-secrets", "piceli-secrets")
        assert text == plain[expected].decode(), name


@pytest.mark.parametrize(
    "option",
    [
        ("--input-prefix", "shop_input_"),
        ("--tool-name", "bad\nname"),
        ("--annotation-domain", "Shop.Example"),
        ("--annotation-domain", "kubernetes.io"),
    ],
)
def test_cli_refuses_a_bad_name_and_writes_nothing(
    tmp_path: Path, option: tuple[str, str]
) -> None:
    out = tmp_path / "bundle"
    result = _bundle_cli(
        f"{HERE / 'cli_app.py'}:pipeline", "--env", "client", "--out", str(out), *option
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout.splitlines()[0])["reason"] == "bundle-invalid"
    assert not out.exists()


# --------------------------------------------------------------- support


def test_the_support_bundle_reads_the_marker_under_any_domain() -> None:
    assert bundle_marker({"piceli.io/bundle": "shop@1"}) == "shop@1"
    assert bundle_marker({"shop.example/bundle": "shop@1.2.3"}) == "shop@1.2.3"
    assert bundle_marker({"shop.example/bundle": "not a marker"}) is None
    assert bundle_marker({"other/x": "shop@1"}) is None
    assert bundle_marker(None) is None
