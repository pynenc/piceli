"""0.94.0: an ``[[output.image]]`` with a ``collector`` table is an
OpenTelemetry Collector built with OCB (OpenTelemetry Collector Builder).

Piceli writes the OCB manifest with only the listed components, installs OCB
and builds the binary with the pinned Go, writes ``/THIRD-PARTY-NOTICES``
from the module cache, and lists the linked Go modules in the SBOM. No
network and no Go: the go.dev download comes from a fake opener, OCB from a
fake runner, ``go version -m`` from a fake.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts import attestations, host_collector
from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.host_collector import GoModule
from piceli.dev import toolchains
from tests.unit.host_build_support import FACTS_16K
from tests.unit.test_host_toolchains_go import GO, go_opener

CONTRIB = "github.com/open-telemetry/opentelemetry-collector-contrib"
K8S = f"{CONTRIB}/processor/k8sattributesprocessor v1.5.0"
COLLECTOR = f"""version = "0.166.0"
receivers = ["otlp", "statsd", "mongodb", "rabbitmq"]
processors = ["{K8S}", "resource", "memory_limiter"]
extensions = ["file_storage", "health_check"]
exporters = ["otlp_http"]
"""
SPEC = """
revision = "piceli.host-build.v1"
name = "otel"

[build]
toolchains = { go = "1.26.11" }

[context.conf]
path = "conf"
include = ["*.yaml"]

[[output.image]]
name = "collector"
repository = "shop/collector"
base = "scratch"
user = "10001:10001"
cmd = ["--config", "/etc/otelcol/config.yaml"]
files = { "conf/config.yaml" = "/etc/otelcol/config.yaml" }
@IMAGE@
[output.image.collector]
@COLLECTOR@
"""
MANIFEST = f"""# Written by piceli for a host build; changes are overwritten.
dist:
  name: "otelcol"
  module: "piceli.dev/otelcol"
  description: "OpenTelemetry Collector"
  version: "0.166.0"
  output_path: "./build"
extensions:
  - gomod: "{CONTRIB}/extension/storage/filestorage v0.166.0"
  - gomod: "{CONTRIB}/extension/healthcheckextension v0.166.0"
receivers:
  - gomod: "go.opentelemetry.io/collector/receiver/otlpreceiver v0.166.0"
  - gomod: "{CONTRIB}/receiver/statsdreceiver v0.166.0"
  - gomod: "{CONTRIB}/receiver/mongodbreceiver v0.166.0"
  - gomod: "{CONTRIB}/receiver/rabbitmqreceiver v0.166.0"
processors:
  - gomod: "{K8S}"
  - gomod: "{CONTRIB}/processor/resourceprocessor v0.166.0"
  - gomod: "go.opentelemetry.io/collector/processor/memorylimiterprocessor v0.166.0"
exporters:
  - gomod: "go.opentelemetry.io/collector/exporter/otlphttpexporter v0.166.0"
"""
RULE = "=" * 72


def write(
    root: Path,
    collector: str = COLLECTOR,
    image: str = "",
    build: str | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "conf").mkdir(exist_ok=True)
    (root / "conf" / "config.yaml").write_text("receivers: {}\n")
    text = SPEC.replace("@COLLECTOR@", collector).replace("@IMAGE@", image)
    if build is not None:
        text = text.replace('toolchains = { go = "1.26.11" }', build)
    path = root / "host-build.toml"
    path.write_text(text)
    return path


def load(tmp_path: Path, **kwargs: Any) -> HostBuildSpec:
    return HostBuildSpec.from_toml(write(tmp_path / "p", **kwargs))


def refused(tmp_path: Path, **kwargs: Any) -> str:
    with pytest.raises(BuildSpecError) as raised:
        load(tmp_path, **kwargs)
    assert raised.value.code == "invalid-spec"
    return str(raised.value)


# ------------------------------------------------------------------ parsing


def test_a_collector_table_names_short_names_and_full_pins(tmp_path: Path) -> None:
    spec = load(tmp_path)
    assert spec.commands == () and spec.tools == ()
    image = spec.images[0]
    collector = image.collector
    assert collector is not None
    assert collector.version == "0.166.0"
    assert collector.binary == "/otelcol"
    assert collector.notices == "/THIRD-PARTY-NOTICES"
    assert image.entrypoint == ("/otelcol",)
    assert image.cmd == ("--config", "/etc/otelcol/config.yaml")
    assert collector.gomods("processors")[0] == K8S
    assert collector.manifest == MANIFEST
    # The spec digest and a composition's change key cover the table.
    declared = image.to_dict()["collector"]
    assert declared["components"]["exporters"] == [
        "go.opentelemetry.io/collector/exporter/otlphttpexporter v0.166.0"
    ]
    other = load(tmp_path / "b", collector=COLLECTOR.replace(', "rabbitmq"', ""))
    assert other.spec_sha256 != spec.spec_sha256


def test_renamed_types_take_both_spellings(tmp_path: Path) -> None:
    old = COLLECTOR.replace('"memory_limiter"', '"memorylimiter"')
    old = old.replace('"file_storage", "health_check"', '"filestorage", "healthcheck"')
    old = old.replace('"otlp_http"', '"otlphttp"')
    first = load(tmp_path / "a").images[0].collector
    second = load(tmp_path / "b", collector=old).images[0].collector
    assert first is not None and second is not None
    assert first.manifest == second.manifest


def test_custom_binary_and_notices_paths(tmp_path: Path) -> None:
    table = COLLECTOR + 'binary = "/usr/bin/otelcol"\nnotices = "/licenses/NOTICES"\n'
    image = load(tmp_path, collector=table).images[0]
    assert image.entrypoint == ("/usr/bin/otelcol",)
    assert host_collector.mappings(image.name, image.collector) == (  # type: ignore[arg-type]
        ("piceli-collector/collector/build/otelcol", "/usr/bin/otelcol"),
        ("piceli-collector/collector/THIRD-PARTY-NOTICES", "/licenses/NOTICES"),
    )


def test_an_unknown_short_name_lists_the_known_ones(tmp_path: Path) -> None:
    message = refused(
        tmp_path, collector=COLLECTOR.replace('"resource"', '"groupbytrace"')
    )
    assert "unknown component 'groupbytrace'" in message
    assert (
        "known short names: attributes, batch, filter, memory_limiter, "
        "memorylimiter, resource, transform" in message
    )
    assert "full pin" in message


@pytest.mark.parametrize("name", ["k8sattributes", "k8s_attributes"])
def test_a_stable_module_needs_its_full_pin(tmp_path: Path, name: str) -> None:
    message = refused(tmp_path, collector=COLLECTOR.replace(f'"{K8S}"', f'"{name}"'))
    assert "stable module" in message
    assert K8S in message


@pytest.mark.parametrize(
    ("collector", "words"),
    [
        (COLLECTOR.replace('"0.166.0"', '"0.166"'), "collector.version"),
        (COLLECTOR.replace('"0.166.0"', '"latest"'), "collector.version"),
        (
            COLLECTOR.replace('["otlp_http"]', '["otlp_http", "otlphttp"]'),
            "twice",
        ),
        (COLLECTOR.replace('["otlp_http"]', "[]"), "one exporter"),
        (
            COLLECTOR.replace(f'"{K8S}"', f'"{CONTRIB}/processor/x 1.0.0"'),
            "is not '<module path> v<semver>'",
        ),
        (COLLECTOR.replace('"statsd"', '"Statsd"'), "not a component name"),
        (COLLECTOR + 'binary = "otelcol"\n', "collector.binary"),
        (COLLECTOR + 'notices = "/otelcol"\n', "one path"),
        (COLLECTOR + 'pipelines = ["x"]\n', "expected fields"),
    ],
)
def test_a_collector_table_that_cannot_be_read_is_refused(
    tmp_path: Path, collector: str, words: str
) -> None:
    assert words in refused(tmp_path, collector=collector)


@pytest.mark.parametrize("key", ["target_files", "entrypoint", "shared_files"])
def test_a_collector_image_makes_its_own_binary_and_entrypoint(
    tmp_path: Path, key: str
) -> None:
    value = '["/x"]' if key == "entrypoint" else '{ "x" = "/x" }'
    message = refused(tmp_path, image=f"{key} = {value}")
    assert f"cannot set {key!r}" in message


def test_files_cannot_land_on_the_binary(tmp_path: Path) -> None:
    path = write(tmp_path / "p")
    path.write_text(
        path.read_text().replace('"/etc/otelcol/config.yaml" }', '"/otelcol" }', 1)
    )
    with pytest.raises(BuildSpecError) as raised:
        HostBuildSpec.from_toml(path)
    assert "where the collector writes" in str(raised.value)


def test_a_collector_needs_a_pinned_go(tmp_path: Path) -> None:
    message = refused(
        tmp_path, build='tools = ["sh"]\ncommands = [["sh", "-c", "true"]]'
    )
    assert "build.toolchains.go" in message


def test_a_spec_without_a_collector_still_needs_commands(tmp_path: Path) -> None:
    path = tmp_path / "host-build.toml"
    text = write(tmp_path / "p").read_text()
    path.write_text(text.split("[output.image.collector]")[0])
    with pytest.raises(BuildSpecError) as raised:
        HostBuildSpec.from_toml(path)
    assert raised.value.code == "invalid-spec"
    assert "expected fields" in str(raised.value)


# ------------------------------------------------------------------ plan


def test_the_plan_adds_the_collector_files_and_hashes_the_manifest(
    tmp_path: Path,
) -> None:
    spec = load(tmp_path / "a").for_node(FACTS_16K)
    plan = spec.plan()
    image = plan.images[0]
    assert image.target_files == (
        ("piceli-collector/collector/build/otelcol", "/otelcol"),
        ("piceli-collector/collector/THIRD-PARTY-NOTICES", "/THIRD-PARTY-NOTICES"),
    )
    assert image.collector is not None
    manifest_sha = image.collector.manifest_sha256
    assert plan.collectors == {
        "collector": {
            "version": "0.166.0",
            "manifest_sha256": manifest_sha,
            "components": 10,
        }
    }
    assert plan.preview()["collectors"]["collector"]["manifest_sha256"] == manifest_sha
    assert plan.preview()["images"]["collector"]["layers"] == 3
    assert plan.toolchains == {"go": {"version": GO, "file": None}}
    other = load(
        tmp_path / "b", collector=COLLECTOR.replace('"0.166.0"', '"0.167.0"', 1)
    )
    assert other.for_node(FACTS_16K).plan().plan_hash != plan.plan_hash


# ------------------------------------------------------------------ notices


def module(root: Path, name: str, files: dict[str, str]) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    for file, body in files.items():
        (directory / file).write_text(body)


def test_notices_list_go_and_each_module_in_order(tmp_path: Path) -> None:
    goroot, cache = tmp_path / "go", tmp_path / "mod"
    goroot.mkdir()
    (goroot / "LICENSE").write_text("Go licence\n")
    module(
        cache,
        "example.org/lib@v1.0.0",
        {"NOTICE": "lib notice\n", "LICENSE": "MIT lib\n", "README.md": "x"},
    )
    module(
        cache, "example.org/lib/sub@v1.0.0", {"go.mod": "module example.org/lib/sub\n"}
    )
    text = host_collector.notices(
        [
            GoModule("example.org/lib/sub", "v1.0.0"),
            GoModule("example.org/lib", "v1.0.0"),
        ],
        "go1.26.11",
        goroot,
        cache,
        "shop/collector",
    )
    lib = ["", "--- LICENSE ---", "", "MIT lib", "", "--- NOTICE ---", "", "lib notice"]
    assert (
        text
        == "\n".join(
            [
                "Third-party notices for shop/collector",
                "",
                "The licences and notices of the Go modules linked into this image's",
                "OpenTelemetry Collector (from 'go version -m'), and of go1.26.11.",
                "",
                RULE,
                "go1.26.11 (Go standard library and runtime)",
                RULE,
                "",
                "--- LICENSE ---",
                "",
                "Go licence",
                "",
                RULE,
                "example.org/lib v1.0.0",
                RULE,
                *lib,
                "",
                RULE,
                "example.org/lib/sub v1.0.0",
                RULE,
                "This module has no licence file of its own; these are the files of "
                "its parent module example.org/lib v1.0.0.",
                *lib,
            ]
        )
        + "\n"
    )


def test_upper_case_module_paths_are_escaped_in_the_cache(tmp_path: Path) -> None:
    goroot, cache = tmp_path / "go", tmp_path / "mod"
    goroot.mkdir()
    (goroot / "LICENSE").write_text("Go\n")
    assert host_collector.escape("github.com/BurntSushi/toml") == (
        "github.com/!burnt!sushi/toml"
    )
    module(cache, "github.com/!burnt!sushi/toml@v1.4.0", {"COPYING": "MIT toml\n"})
    text = host_collector.notices(
        [GoModule("github.com/BurntSushi/toml", "v1.4.0")],
        "go1.26.11",
        goroot,
        cache,
        "x",
    )
    assert "github.com/BurntSushi/toml v1.4.0\n" in text
    assert "--- COPYING ---\n\nMIT toml\n" in text


def test_a_module_without_any_licence_fails(tmp_path: Path) -> None:
    goroot, cache = tmp_path / "go", tmp_path / "mod"
    goroot.mkdir()
    (goroot / "LICENSE").write_text("Go\n")
    module(cache, "example.org/alone@v1.0.0", {"main.go": "package alone\n"})
    with pytest.raises(BuildSpecError) as raised:
        host_collector.notices(
            [GoModule("example.org/alone", "v1.0.0")], "go1.26.11", goroot, cache, "x"
        )
    assert raised.value.code == "collector-licence-missing"
    assert "example.org/alone v1.0.0" in str(raised.value)
    with pytest.raises(BuildSpecError) as raised:
        host_collector.notices(
            [GoModule("example.org/gone", "v1.0.0")], "go1.26.11", goroot, cache, "x"
        )
    assert raised.value.code == "collector-notices-failed"


BUILDINFO = """{binary}: go1.26.11
\tpath\tpiceli.dev/otelcol
\tmod\tpiceli.dev/otelcol\t(devel)\t
\tdep\tgo.opentelemetry.io/collector/receiver/otlpreceiver\tv0.166.0\th1:otlp=
\tdep\tgithub.com/BurntSushi/toml\tv1.4.0\th1:toml=
\tdep\texample.org/old\tv1.0.0\th1:old=
\t=>\texample.org/new\tv1.1.0\th1:new=
\tbuild\t-buildmode=exe
\tbuild\tCGO_ENABLED=0
"""


def test_buildinfo_lists_the_linked_modules_sorted() -> None:
    go, modules = host_collector.parse_buildinfo(BUILDINFO.format(binary="/b/otelcol"))
    assert go == "go1.26.11"
    assert modules == [
        GoModule("example.org/new", "v1.1.0", "h1:new="),
        GoModule("github.com/BurntSushi/toml", "v1.4.0", "h1:toml="),
        GoModule(
            "go.opentelemetry.io/collector/receiver/otlpreceiver",
            "v0.166.0",
            "h1:otlp=",
        ),
    ]
    local = BUILDINFO.replace(
        "\t=>\texample.org/new\tv1.1.0\th1:new=", "\t=>\t../new\t"
    )
    with pytest.raises(BuildSpecError) as raised:
        host_collector.parse_buildinfo(local)
    assert raised.value.code == "collector-notices-failed"
    with pytest.raises(BuildSpecError):
        host_collector.parse_buildinfo("not a binary\n")


# ------------------------------------------------------------------ SBOM


def test_the_sbom_lists_go_modules_by_purl() -> None:
    packages = attestations.go_packages(
        {
            "go": "go1.26.11",
            "modules": [
                {
                    "path": "github.com/BurntSushi/toml",
                    "version": "v1.4.0",
                    "sum": "h1:toml=",
                },
                {"path": "example.org/legacy", "version": "v2.0.0+incompatible"},
            ],
        }
    )
    assert [item["externalRefs"][0]["referenceLocator"] for item in packages] == [
        "pkg:golang/stdlib@1.26.11",
        "pkg:golang/github.com/BurntSushi/toml@v1.4.0",
        "pkg:golang/example.org/legacy@v2.0.0%2Bincompatible",
    ]
    assert packages[1]["comment"] == "go.sum h1:toml="
    assert "comment" not in packages[2] and "checksums" not in packages[1]
    assert attestations.go_packages(None) == []
    document = attestations.sbom(
        "collector",
        "shop/collector",
        {"digest": "sha256:" + "a" * 64, "files": []},
        [],
        0,
        go_modules=packages,
    )
    names = [item["name"] for item in document["packages"]]
    assert names == [
        "shop/collector",
        "stdlib",
        "github.com/BurntSushi/toml",
        "example.org/legacy",
    ]
    contained = {
        item["relatedSpdxElement"]
        for item in document["relationships"]
        if item["relationshipType"] == "CONTAINS"
    }
    assert {item["SPDXID"] for item in packages} <= contained


# ------------------------------------------------------------------ build


class FakeOcb:
    """Stands in for ``go install`` and OCB: records argv, cwd and env, and
    writes the builder and the collector binary where they would land."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self, argv, cwd, limits, environment, expires_at, /, *, on_output=None
    ):  # type: ignore[no-untyped-def]
        self.calls.append((list(argv), Path(cwd), dict(environment)))
        if argv[1:2] == ["install"]:
            gobin = Path(environment["GOBIN"])
            gobin.mkdir(parents=True, exist_ok=True)
            (gobin / "builder").write_text("#!/bin/sh\n")
        else:
            assert (Path(cwd) / argv[2]).read_text() == MANIFEST
            out = Path(cwd) / "build"
            out.mkdir(exist_ok=True)
            (out / "otelcol").write_bytes(b"ELF " + environment["GOARCH"].encode())
            (out / "otelcol").chmod(0o755)
        return {"state": "succeeded", "exit_code": 0, "seconds": 0.01}, b"", b""


def test_a_collector_image_is_built_noticed_and_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(toolchains, "_open_url", go_opener())
    monkeypatch.setattr(toolchains, "host_platform", lambda: ("linux", "amd64"))
    monkeypatch.setattr(
        "piceli.artifacts.scan.scan_build", lambda *_a, **_k: {"scan": "off"}
    )
    cache, out = tmp_path / "cache", tmp_path / "out"
    modcache = cache / "go" / "mod"
    module(modcache, "github.com/!burnt!sushi/toml@v1.4.0", {"COPYING": "MIT toml\n"})
    module(
        modcache,
        "go.opentelemetry.io/collector/receiver/otlpreceiver@v0.166.0",
        {"LICENSE": "Apache otlp\n"},
    )
    module(modcache, "example.org/new@v1.1.0", {"LICENSE.md": "BSD new\n"})
    seen: list[dict[str, Any]] = []

    class Done:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def version_m(argv: list[str], **kwargs: Any) -> Done:
        seen.append({"argv": list(argv), "env": dict(kwargs["env"])})
        return Done(BUILDINFO.format(binary=argv[-1]))

    monkeypatch.setattr(host_collector, "_run", version_m)
    item = HostBuildSpec.from_toml(write(tmp_path / "p")).for_node(FACTS_16K)
    item = item.with_cache_dir(cache)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    ocb = FakeOcb()
    receipt = item.run(grant, out, runner=ocb).to_dict()

    go_root = cache / "toolchains" / "go" / f"{GO}-linux-amd64"
    work = cache / "target" / "arm64-16384" / "piceli-collector" / "collector"
    gobin = cache / "go" / "ocb" / "0.166.0-linux-amd64"
    (install, install_cwd, install_env), (build, build_cwd, build_env) = ocb.calls
    assert install == [
        str(go_root / "bin" / "go"),
        "install",
        "go.opentelemetry.io/collector/cmd/builder@v0.166.0",
    ]
    assert install_cwd == work and install_env["GOBIN"] == str(gobin)
    assert "GOOS" not in install_env  # OCB runs on this machine
    assert build == [str(gobin / "builder"), "--config", "manifest.yaml"]
    assert build_cwd == work
    assert (build_env["CGO_ENABLED"], build_env["GOOS"], build_env["GOARCH"]) == (
        "0",
        "linux",
        "arm64",
    )
    for env in (install_env, build_env):
        assert env["GOTOOLCHAIN"] == "local"
        assert env["GOMODCACHE"] == str(modcache)
        assert env["GOCACHE"] == str(cache / "go" / "build")
        assert env["PATH"].split(":")[0] == str(go_root / "bin")
    assert (work / "manifest.yaml").read_text() == MANIFEST
    assert seen[0]["argv"][1:3] == ["version", "-m"]
    assert "GOFLAGS" not in seen[0]["env"]

    notices = (work / "THIRD-PARTY-NOTICES").read_text()
    assert "go1.26.11 (Go standard library and runtime)" in notices
    assert "--- LICENSE.md ---\n\nBSD new\n" in notices
    assert notices.index("example.org/new") < notices.index("github.com/BurntSushi")

    kinds = [step["kind"] for step in receipt["steps"]]
    assert kinds == [
        "toolchains",
        "collector-collector-install",
        "collector-collector-build",
        "collector-collector-notices",
        "image-collector",
    ]
    image = receipt["outputs"]["images"]["collector"]
    assert [entry["path"] for entry in image["files"]] == [
        "/THIRD-PARTY-NOTICES",
        "/etc/otelcol/config.yaml",
        "/otelcol",
    ]
    spec_collector = item.images[0].collector
    assert spec_collector is not None
    assert image["collector"] == {
        "version": "0.166.0",
        "manifest_sha256": spec_collector.manifest_sha256,
        "components": spec_collector.to_dict()["components"],
        "go": "go1.26.11",
        "go_modules": 3,
    }
    assert (
        receipt["toolchains"]["go"]["url"]
        == f"https://go.dev/dl/go{GO}.linux-amd64.tar.gz"
    )

    sbom = json.loads((out / "attestations" / "collector.spdx.json").read_text())
    purls = {
        ref["referenceLocator"]
        for package in sbom["packages"]
        for ref in package.get("externalRefs", ())
    }
    assert {
        "pkg:golang/stdlib@1.26.11",
        "pkg:golang/example.org/new@v1.1.0",
        "pkg:golang/github.com/BurntSushi/toml@v1.4.0",
        "pkg:golang/go.opentelemetry.io/collector/receiver/otlpreceiver@v0.166.0",
    } <= purls
    provenance = json.loads(
        (out / "attestations" / "collector.provenance.json").read_text()
    )
    parameters = provenance["predicate"]["buildDefinition"]["externalParameters"]
    assert parameters["collector"] == {
        "version": "0.166.0",
        "components": spec_collector.to_dict()["components"],
        "manifest_sha256": spec_collector.manifest_sha256,
    }
    assert parameters["toolchains"] == {"go": {"version": GO, "file": None}}


def test_the_example_collector_spec_parses() -> None:
    root = Path(__file__).resolve().parents[2]
    spec = HostBuildSpec.from_toml(
        root / "examples" / "builds" / "otel-collector" / "host-build.toml"
    )
    collector = spec.images[0].collector
    assert collector is not None and collector.version == "0.166.0"
    assert spec.declared_platforms == ("linux/amd64", "linux/arm64")
    assert spec.commands == ()


def test_an_image_may_read_no_context(tmp_path: Path) -> None:
    """0.95.0: ``contexts = []`` keys a collector image on its tables only."""
    image = load(tmp_path, image="contexts = []").images[0]
    assert image.contexts == ()
    assert image.to_dict()["contexts"] == []
