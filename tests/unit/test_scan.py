"""0.94.0: images are scanned for known vulnerabilities (Grype) before they
are pushed, mirrored or copied. No test downloads Grype or its database: a
stand-in script or an injected runner answers instead."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import tarfile
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts import scan
from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.image_copy import Copier, CopyGrant, CopyPlan
from piceli.artifacts.mirror import MirrorSource, mirror_image
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryTarget,
    StreamedOciRegistryClient,
)
from piceli.artifacts.scan import (
    Report,
    ScanError,
    Scanner,
    ScanPolicy,
    Waiver,
    judge,
)
from tests.fake_scanner import DB_BUILT, calls, match, report, write_scanner
from tests.oci_registry import OciRegistry, oci_registry
from tests.unit.host_build_support import FACTS_16K, publish_base, write_project
from tests.unit.test_registry_delivery import FakeRegistry

FIXTURE = Path(__file__).parents[1] / "fixtures" / "grype" / "report.json"
TODAY = dt.date(2026, 11, 27)
CRITICAL_FIXED = match("CVE-2026-0001", "Critical", "libssl3", "3.0.11", ("3.0.15",))


def _report(*matches: dict[str, Any]) -> Report:
    return Report.parse(report(*matches))


# ------------------------------------------------------------------ parsing


def test_a_grype_report_is_parsed_and_duplicate_matches_count_once() -> None:
    parsed = Report.parse(json.loads(FIXTURE.read_text()))
    assert parsed.version == "0.122.1"
    assert parsed.db_built == "2026-11-26T04:12:34Z"
    assert parsed.counts() == {
        "critical": 2,
        "high": 1,
        "medium": 1,
        "low": 0,
        "negligible": 1,
        "unknown": 0,
    }
    first = parsed.findings[0]
    assert (first.id, first.package, first.installed) == (
        "CVE-2026-0001",
        "libssl3",
        "3.0.11-1~deb12u2",
    )
    assert first.fixed_in == ("3.0.15-1~deb12u1",) and first.blocking
    unfixed = next(item for item in parsed.findings if item.id == "CVE-2026-0002")
    assert unfixed.fix_state == "not-fixed" and not unfixed.blocking
    rust = next(item for item in parsed.findings if item.package == "hyper")
    assert rust.related == ("CVE-2026-0003",)
    verdict = judge(parsed, ScanPolicy(), TODAY)
    assert [item["id"] for item in verdict.failing()] == ["CVE-2026-0001"]
    # The .scan.json lists Critical and High findings only.
    assert [item["id"] for item in verdict.listed()] == [
        "CVE-2026-0001",
        "CVE-2026-0002",
        "GHSA-aaaa-bbbb-cccc",
    ]


def test_a_document_that_is_not_a_report_is_refused() -> None:
    with pytest.raises(ScanError) as error:
        Report.parse({"matches": "none"})
    assert error.value.code == "image-scan-failed"


# ------------------------------------------------------------------ policy


@pytest.mark.parametrize(
    ("found", "fails"),
    [
        (CRITICAL_FIXED, True),
        (match("CVE-2026-0002", "Critical", fixed=()), False),
        (match("CVE-2026-0006", "Critical", fixed=(), state="fixed"), False),
        (match("CVE-2026-0007", "Critical", state="wont-fix"), False),
        (match("CVE-2026-0008", "High", fixed=("3.0.15",)), False),
        (match("CVE-2026-0009", "Medium", fixed=("3.0.15",)), False),
    ],
    ids=[
        "critical-fixed",
        "critical-not-fixed",
        "critical-no-version",
        "critical-wont-fix",
        "high-fixed",
        "medium-fixed",
    ],
)
def test_only_a_fixable_critical_fails(found: dict[str, Any], fails: bool) -> None:
    verdict = judge(_report(found), ScanPolicy(), TODAY)
    assert verdict.passed is not fails
    assert verdict.summary()["critical_fixable"] == (1 if fails else 0)


def test_a_waiver_applies_until_its_date_and_then_expires() -> None:
    policy = ScanPolicy(
        waivers=(Waiver("CVE-2026-0001", "not reachable", "2026-12-31"),)
    )
    waived = judge(_report(CRITICAL_FIXED), policy, dt.date(2026, 12, 31))
    assert waived.passed and waived.summary()["waived"] == ["CVE-2026-0001"]
    assert waived.listed()[0]["waived"] is True
    expired = judge(_report(CRITICAL_FIXED), policy, dt.date(2027, 1, 1))
    assert not expired.passed
    [finding] = expired.failing()
    assert finding == {
        "id": "CVE-2026-0001",
        "severity": "critical",
        "package": "libssl3",
        "installed": "3.0.11",
        "fixed_in": ["3.0.15"],
        "waived": False,
        "waiver_expired": "2026-12-31",
    }
    error = scan.vulnerable(
        [{"image": "web", "digest": "sha256:" + "a" * 64, "findings": [finding]}]
    )
    assert error.code == "image-vulnerable"
    assert "waiver expired 2026-12-31" in str(error)
    assert "CVE-2026-0001 (libssl3 3.0.11, fixed in 3.0.15)" in str(error)
    assert error.details["images"][0]["findings"] == [finding]


def test_a_waiver_may_name_a_related_id() -> None:
    advisory = match(
        "GHSA-aaaa-bbbb-cccc",
        "Critical",
        "hyper",
        "0.12.1",
        ("0.12.4",),
        related=("CVE-2026-0003",),
    )
    policy = ScanPolicy(waivers=(Waiver("CVE-2026-0003", "patched", "2027-01-31"),))
    assert judge(_report(advisory), policy, TODAY).passed


def test_the_scan_table_is_parsed_and_checked() -> None:
    document = tomllib.loads(
        "[scan]\nenabled = true\nignore = [\n"
        '  { id = "CVE-2026-0002", reason = "base image, no fix yet", until = "2027-01-31" },\n'
        '  { id = "CVE-2026-0001", reason = "parser unused", until = 2027-02-28 },\n'
        "]\n"
    )
    policy = scan.parse_policy(document["scan"])
    assert [item.id for item in policy.waivers] == ["CVE-2026-0001", "CVE-2026-0002"]
    assert policy.waivers[0].until == "2027-02-28"  # a TOML date literal
    assert policy.to_dict()["ignore"][1] == {
        "id": "CVE-2026-0002",
        "reason": "base image, no fix yet",
        "until": "2027-01-31",
    }
    assert scan.parse_policy({"enabled": False}) == ScanPolicy.off()
    for bad in (
        {"enable": True},
        {"enabled": "no"},
        {"ignore": [{"id": "CVE-2026-0001", "until": "2027-01-31"}]},
        {"ignore": [{"id": "CVE-1", "reason": " ", "until": "2027-01-31"}]},
        {"ignore": [{"id": "CVE-1", "reason": "r", "until": "2027-02-30"}]},
        {"ignore": [{"id": "CVE 1", "reason": "r", "until": "2027-01-31"}]},
        {"ignore": [{"id": "CVE-1", "reason": "r", "until": "2027-01-31"}] * 2},
    ):
        with pytest.raises(ScanError) as error:
            scan.parse_policy(bad)
        assert error.value.code == "scan-policy-invalid", bad


def test_a_policy_file_and_the_mirror_environment(tmp_path: Path) -> None:
    path = tmp_path / "scan-policy.toml"
    path.write_text("[scan]\nenabled = false\n")
    assert ScanPolicy.load(path) == ScanPolicy.off()
    assert ScanPolicy.from_environment({scan.POLICY_ENV: str(path)}) == ScanPolicy.off()
    assert ScanPolicy.from_environment({}) == ScanPolicy()
    path.write_text("[other]\n")
    with pytest.raises(ScanError) as error:
        ScanPolicy.load(path)
    assert error.value.code == "scan-policy-invalid"


# ------------------------------------------------------------------ install


def _tarball(body: bytes = b"#!/bin/sh\necho grype\n") -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in (("LICENSE", b"Apache-2.0\n"), ("grype", body)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def test_every_grype_tarball_is_pinned() -> None:
    assert scan.GRYPE_VERSION == "0.122.1"
    assert set(scan.GRYPE_SHA256) == {
        (system, arch) for system in ("darwin", "linux") for arch in ("amd64", "arm64")
    }
    assert all(len(value) == 64 for value in scan.GRYPE_SHA256.values())


def test_grype_is_installed_once_after_its_sha256_is_checked(tmp_path: Path) -> None:
    data = _tarball()
    urls: list[str] = []

    def opener(url: str) -> io.BytesIO:
        urls.append(url)
        return io.BytesIO(data)

    pins = {("linux", "amd64"): hashlib.sha256(data).hexdigest()}
    path = scan.install_grype(
        tmp_path, system="linux", arch="amd64", opener=opener, checksums=pins
    )
    assert path.read_bytes() == b"#!/bin/sh\necho grype\n"
    assert path.stat().st_mode & 0o111
    assert urls == [
        "https://github.com/anchore/grype/releases/download/v0.122.1/"
        "grype_0.122.1_linux_amd64.tar.gz"
    ]
    again = scan.install_grype(
        tmp_path, system="linux", arch="amd64", opener=opener, checksums=pins
    )
    assert again == path and len(urls) == 1  # the done marker


def test_a_tarball_that_is_not_the_pinned_one_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ScanError) as error:
        scan.install_grype(
            tmp_path,
            system="linux",
            arch="arm64",
            opener=lambda _url: io.BytesIO(_tarball(b"something else")),
        )
    assert error.value.code == "image-scanner-checksum"
    assert scan.GRYPE_SHA256[("linux", "arm64")] in str(error.value)
    assert not (tmp_path / "toolchains" / "grype" / "0.122.1-linux-arm64").exists()


# ------------------------------------------------------------------ running


class Runner:
    """Answers like Grype; records each call."""

    def __init__(self, *answers: tuple[int, bytes, bytes]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self, argv: list[str], environment: dict[str, str], cwd: Path, timeout: float
    ) -> tuple[int, bytes, bytes]:
        self.calls.append((argv, environment))
        return self.answers.pop(0)


def test_the_scanner_owns_its_database_and_ignores_ambient_settings(
    tmp_path: Path,
) -> None:
    runner = Runner((0, json.dumps(report()).encode(), b""))
    environ = {
        scan.SCANNER_ENV: "/opt/grype",
        "GRYPE_DB_AUTO_UPDATE": "false",
        "PATH": "/usr/bin",
    }
    scanner = Scanner(tmp_path, runner=runner, environ=environ)
    found = scanner.scan("oci-archive:/out/images/web.oci.tar")
    assert found.db_built == DB_BUILT and not scanner.pinned
    [(argv, environment)] = runner.calls
    assert argv == ["/opt/grype", "oci-archive:/out/images/web.oci.tar", "-o", "json"]
    assert environment["GRYPE_DB_AUTO_UPDATE"] == "true"
    assert environment["GRYPE_DB_VALIDATE_AGE"] == "true"
    assert environment["GRYPE_DB_CACHE_DIR"] == str(tmp_path / "grype-db")
    assert environment["PATH"] == "/usr/bin" and scan.SCANNER_ENV not in environment


def test_the_database_time_falls_back_to_db_status(tmp_path: Path) -> None:
    runner = Runner(
        (0, json.dumps(report(db_built=None)).encode(), b""),
        (0, json.dumps({"built": "2026-11-20T00:00:00Z"}).encode(), b""),
    )
    found = Scanner(tmp_path, runner=runner, environ={"PICELI_SCANNER": "g"}).scan("x")
    assert found.db_built == "2026-11-20T00:00:00Z"
    assert runner.calls[1][0] == ["g", "db", "status", "-o", "json"]


def test_a_database_that_cannot_be_fetched_fails_closed(tmp_path: Path) -> None:
    runner = Runner(
        (1, b"", b"failed to load vulnerability db: unable to update: no such host\n")
    )
    with pytest.raises(ScanError) as error:
        Scanner(tmp_path, runner=runner, environ={"PICELI_SCANNER": "g"}).scan("x")
    assert error.value.code == "image-scan-failed"
    assert "vulnerability database could not be fetched" in str(error.value)


def test_registry_credentials_reach_grype_only_in_its_environment(
    tmp_path: Path,
) -> None:
    endpoint = RegistryEndpoint(
        "registry.example.org",
        443,
        True,
        credentials=RegistryCredentials(username="ci", password="s3cret-pw"),
    )
    digest = "sha256:" + "b" * 64
    source, environment, secrets = scan.registry_source(endpoint, "shop/api", digest)
    assert source == f"registry:registry.example.org:443/shop/api@{digest}"
    assert environment == {
        "GRYPE_REGISTRY_AUTH_AUTHORITY": "registry.example.org:443",
        "GRYPE_REGISTRY_AUTH_USERNAME": "ci",
        "GRYPE_REGISTRY_AUTH_PASSWORD": "s3cret-pw",
    }
    assert secrets == ("s3cret-pw",)
    token = RegistryEndpoint(
        "127.0.0.1", 5000, credentials=RegistryCredentials(token="t0k")
    )
    _, plain, _ = scan.registry_source(token, "shop/api", digest)
    assert plain["GRYPE_REGISTRY_INSECURE_USE_HTTP"] == "true"
    assert plain["GRYPE_REGISTRY_AUTH_TOKEN"] == "t0k"
    runner = Runner((1, b"", b"GET with s3cret-pw: unauthorized\n"))
    with pytest.raises(ScanError) as error:
        Scanner(tmp_path, runner=runner, environ={"PICELI_SCANNER": "g"}).scan(
            source, environment, secrets
        )
    assert "s3cret-pw" not in str(error.value) and "***" in str(error.value)
    assert runner.calls[0][1]["GRYPE_REGISTRY_AUTH_PASSWORD"] == "s3cret-pw"


def test_a_multi_platform_receipt_keys_scans_like_its_images() -> None:
    summary = {"scanner": "grype", "critical_fixable": 0}
    receipts = {
        "linux/arm64": {"scans": {"web": summary}},
        "linux/amd64": {"scans": {"web": summary}},
    }
    assert scan.combined(receipts) == {
        "scans": {"web/linux-arm64": summary, "web/linux-amd64": summary}
    }
    assert scan.combined({"linux/arm64": {"scan": "off"}}) == {"scan": "off"}


# ------------------------------------------------------------------ host builds


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def project(tmp_path: Path, registry: FakeRegistry) -> Path:
    return write_project(tmp_path / "project", registry.port, publish_base(registry))


def _build(path: Path, tmp_path: Path) -> dict[str, Any]:
    item = HostBuildSpec.from_toml(path).for_node(FACTS_16K)
    item = item.with_cache_dir(tmp_path / "cache")
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    return item.run(grant, tmp_path / "out").to_dict()


def test_each_built_image_is_scanned_and_recorded(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = write_scanner(tmp_path / "scanner")
    monkeypatch.setenv(scan.SCANNER_ENV, str(scanner))
    receipt = _build(project, tmp_path)
    [line] = calls(scanner)
    assert line.startswith("oci-archive:") and line.endswith(
        "images/web.oci.tar -o json"
    )
    assert receipt["scans"]["web"] == {
        "scanner": "grype",
        "version": "0.122.1",
        "pinned": False,
        "db_built": DB_BUILT,
        "fail_on": "critical-fixed",
        "critical_fixable": 0,
        "counts": dict.fromkeys(scan.SEVERITIES, 0),
        "waived": [],
    }
    entry = receipt["outputs"]["images"]["web"]
    assert entry["scan"]["path"] == "attestations/web.scan.json"
    body = (tmp_path / "out" / entry["scan"]["path"]).read_bytes()
    assert entry["scan"]["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
    document = json.loads(body)
    assert document["schema"] == "piceli.image-scan.v1"
    assert document["subject"]["digest"] == entry["digest"]
    assert str(tmp_path) not in body.decode()


def test_a_fixable_critical_fails_the_build_before_it_is_recorded(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = write_scanner(tmp_path / "scanner", report(CRITICAL_FIXED))
    monkeypatch.setenv(scan.SCANNER_ENV, str(scanner))
    with pytest.raises(BuildSpecError) as error:
        _build(project, tmp_path)
    assert error.value.code == "image-vulnerable"
    [image] = error.value.details["images"]  # type: ignore[attr-defined]
    assert image["image"] == "web" and image["digest"].startswith("sha256:")
    assert image["findings"] == [
        {
            "id": "CVE-2026-0001",
            "severity": "critical",
            "package": "libssl3",
            "installed": "3.0.11",
            "fixed_in": ["3.0.15"],
            "waived": False,
        }
    ]
    # A waiver in host-build.toml lets it through, and is recorded.
    project.write_text(
        project.read_text()
        + '\n[scan]\nignore = [{ id = "CVE-2026-0001", reason = "not reachable", '
        'until = "2099-12-31" }]\n'
    )
    receipt = _build(project, tmp_path)
    assert receipt["scans"]["web"]["waived"] == ["CVE-2026-0001"]
    assert receipt["scans"]["web"]["counts"]["critical"] == 1


def test_scanning_off_is_explicit_and_recorded(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = write_scanner(tmp_path / "scanner", report(CRITICAL_FIXED))
    monkeypatch.setenv(scan.SCANNER_ENV, str(scanner))
    plain = HostBuildSpec.from_toml(project).spec_sha256
    project.write_text(project.read_text() + "\n[scan]\nenabled = false\n")
    assert HostBuildSpec.from_toml(project).spec_sha256 != plain  # in the plan
    receipt = _build(project, tmp_path)
    assert calls(scanner) == []
    assert receipt["scan"] == "off" and "scans" not in receipt
    entry = receipt["outputs"]["images"]["web"]
    body = (tmp_path / "out" / entry["provenance"]["path"]).read_bytes()
    assert entry["provenance"]["sha256"] == "sha256:" + hashlib.sha256(body).hexdigest()
    statement = json.loads(body)
    assert (
        statement["predicate"]["buildDefinition"]["externalParameters"]["scan"] == "off"
    )


# ------------------------------------------------------------------ mirrors, copies


@pytest.fixture
def source() -> Iterator[OciRegistry]:
    with oci_registry() as found:
        yield found


@pytest.fixture
def target() -> Iterator[OciRegistry]:
    with oci_registry() as found:
        yield found


def _client(registry: OciRegistry, repository: str) -> StreamedOciRegistryClient:
    return StreamedOciRegistryClient(
        RegistryTarget.parse(f"oci://{registry.host}/{repository}").endpoint()
    )


def _mirror(source: OciRegistry, target: OciRegistry, reference: str) -> dict[str, Any]:
    parsed = MirrorSource.parse(reference)
    repository = parsed.mirrored_repository("mirror")
    return mirror_image(
        parsed,
        _client(source, parsed.repository),
        _client(target, repository),
        repository,
        platform=None,
        target_registry=target.host,
        node_registry="127.0.0.1:5000",
    )


def test_a_mirror_scans_each_platform_of_its_copy_before_it_is_ready(
    source: OciRegistry,
    target: OciRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index, children = source.add_index("library/cache")
    reference = f"{source.host}/library/cache@{index}"
    clean = write_scanner(tmp_path / "clean")
    monkeypatch.setenv(scan.SCANNER_ENV, str(clean))
    receipt = _mirror(source, target, reference)
    assert receipt["state"] == "succeeded", receipt
    repository = f"mirror/127.0.0.1-{source.port}/library/cache"
    assert sorted(calls(clean)) == sorted(
        f"registry:{target.host}/{repository}@{child} -o json"
        for child in children.values()
    )
    assert receipt["scan"]["platforms"] == {
        "linux/amd64": {
            "digest": children["amd64"],
            "critical_fixable": 0,
            "counts": dict.fromkeys(scan.SEVERITIES, 0),
        },
        "linux/arm64/v8": {
            "digest": children["arm64"],
            "critical_fixable": 0,
            "counts": dict.fromkeys(scan.SEVERITIES, 0),
        },
    }
    # Already present: scanned again, and a fixable critical keeps it from
    # being ready.
    monkeypatch.setenv(
        scan.SCANNER_ENV, str(write_scanner(tmp_path / "bad", report(CRITICAL_FIXED)))
    )
    failed = _mirror(source, target, reference)
    assert failed["state"] == "failed" and failed["reason"] == "image-vulnerable"
    assert failed["pull_ref"] is None
    images = failed["vulnerabilities"]["images"]
    assert {item["platform"] for item in images} == {"linux/amd64", "linux/arm64/v8"}
    assert images[0]["findings"][0]["id"] == "CVE-2026-0001"
    # Off through the policy file mirrors read.
    policy = tmp_path / "scan-policy.toml"
    policy.write_text("[scan]\nenabled = false\n")
    monkeypatch.setenv(scan.POLICY_ENV, str(policy))
    assert _mirror(source, target, reference)["scan"] == "off"


def test_a_copy_scans_the_source_before_anything_is_sent(
    source: OciRegistry, target: OciRegistry, tmp_path: Path
) -> None:
    digest, _ = source.add_image("shop/api", arch="arm64")
    plan = CopyPlan.parse(
        f"oci://{source.host}/shop/api@{digest}", f"oci://{target.host}/shop/api:1"
    )
    bad = write_scanner(tmp_path / "bad", report(CRITICAL_FIXED))
    copier = Copier(
        scanner=Scanner(tmp_path / "cache", environ={scan.SCANNER_ENV: str(bad)})
    )
    receipt = copier.copy(plan, CopyGrant(plan.digest, 4e9))
    assert receipt["state"] == "failed" and receipt["reason"] == "image-vulnerable"
    assert calls(bad) == [f"registry:{source.host}/shop/api@{digest} -o json"]
    [image] = receipt["vulnerabilities"]["images"]
    assert image["digest"] == digest and image["image"] == f"{source.host}/shop/api"
    assert target.mutations() == []
    # --no-scan is part of the plan, and recorded.
    off = CopyPlan.parse(
        f"oci://{source.host}/shop/api@{digest}",
        f"oci://{target.host}/shop/api:1",
        scan=ScanPolicy.off(),
    )
    assert off.digest != plan.digest and off.to_dict()["scan"]["enabled"] is False
    receipt = copier.copy(off, CopyGrant(off.digest, 4e9))
    assert receipt["state"] == "copied" and receipt["scan"] == "off"
    assert len(calls(bad)) == 1
