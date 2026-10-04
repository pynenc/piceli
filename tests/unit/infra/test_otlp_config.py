"""``Controller(telemetry=Otlp(...))``: validation, config hashes, the Deployment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.install import InstallSettings, render_controller
from piceli.gitops.otel import CA_DIR, HEADERS_DIR, resolve
from piceli.infra import Controller, Otlp
from piceli.infra.cluster import ClusterError, describe
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "composition"
IMAGE = "example.com/piceli@sha256:" + "a" * 64


@pytest.mark.parametrize(
    "values",
    [
        {"endpoint": "ftp://collector:4317"},
        {"endpoint": "https://user:pw@collector:4317"},
        {"endpoint": "https://collector:4317?x=1"},
        {"endpoint": "http://collector:4317"},  # plain http needs insecure=True
        {"endpoint": "https://collector:4317", "insecure": True},
        {"endpoint": "https://collector:4317", "protocol": "http/json"},
        {"endpoint": "https://collector:4317", "headers_secret": "Not_A_Label"},
        {"endpoint": "http://collector:4317", "insecure": True, "ca_secret": "ca"},
    ],
)
def test_invalid_settings_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ClusterError) as error:
        Otlp(**values)
    assert error.value.code == "cluster-invalid"  # type: ignore[attr-defined]


def test_valid_settings_and_their_data() -> None:
    otlp = Otlp(
        "https://collector.observability:4317",
        headers_secret="otlp-auth",
        ca_secret="otlp-ca",
    )
    assert Otlp.from_dict(otlp.to_dict()) == otlp
    assert Otlp("http://collector:4318", protocol="http/protobuf", insecure=True)
    assert Otlp("http://127.0.0.1:4317")  # loopback needs no TLS
    with pytest.raises(ClusterError):
        Controller(on="node-a", telemetry={"endpoint": "x"})  # type: ignore[arg-type]


def _config(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> CompositionConfig:
    monkeypatch.setenv("COMPOSITION_SHOP_URL", "https://example.com/shop.git")
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", "https://example.com/catalog.git")
    composition = load_composition(EXAMPLE / "infra.py")
    return CompositionConfig(composition=composition.to_dict(), **kwargs)


def test_config_carries_telemetry_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = _config(monkeypatch)
    assert "telemetry" not in plain.to_dict()  # same config, same hashes
    otlp = Otlp("https://collector:4317", headers_secret="otlp-auth")
    config = _config(monkeypatch, telemetry=otlp.to_dict())
    assert config.to_dict()["telemetry"] == otlp.to_dict()
    assert CompositionConfig.from_dict(config.to_dict()) == config
    from piceli.gitops import GitOpsError

    with pytest.raises(GitOpsError):
        _config(monkeypatch, telemetry={"endpoint": "ftp://x"})


def test_cluster_describe_has_telemetry_only_when_set() -> None:
    from piceli.infra import Cluster

    def cluster(controller: Controller) -> Cluster:
        return Cluster(
            "my-cluster",
            api="https://10.0.0.1:6443",
            credentials="c",
            controller=controller,
        )

    assert "telemetry" not in describe(cluster(Controller(on="node-a")))["controller"]
    otlp = Otlp("https://collector:4317")
    described = describe(cluster(Controller(on="node-a", telemetry=otlp)))
    assert described["controller"]["telemetry"] == otlp.to_dict()


def test_the_deployment_mounts_the_secrets_never_their_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = render_controller(_config(monkeypatch), InstallSettings(image=IMAGE))
    otlp = Otlp(
        "https://collector:4317", headers_secret="otlp-auth", ca_secret="otlp-ca"
    )
    objects = render_controller(
        _config(monkeypatch, telemetry=otlp.to_dict()), InstallSettings(image=IMAGE)
    )
    pod = objects[-1]["spec"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["otlp-headers"]["secret"]["secretName"] == "otlp-auth"
    assert volumes["otlp-ca"]["secret"]["secretName"] == "otlp-ca"
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["otlp-headers"] == {
        "name": "otlp-headers",
        "mountPath": HEADERS_DIR,
        "readOnly": True,
    }
    assert mounts["otlp-ca"]["mountPath"] == CA_DIR
    plain_pod = plain[-1]["spec"]["template"]["spec"]
    assert "otlp-headers" not in {v["name"] for v in plain_pod["volumes"]}
    config_map = json.loads(objects[-2]["data"]["config.json"])
    assert config_map["telemetry"]["headers_secret"] == "otlp-auth"


def test_resolve_reads_the_mounted_secrets(tmp_path: Path) -> None:
    headers, ca = tmp_path / "headers", tmp_path / "ca"
    headers.mkdir()
    ca.mkdir()
    (headers / "Authorization").write_text("Bearer x\n")
    (headers / "..data").mkdir()  # a projected volume's links are skipped
    (ca / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\n")
    settings = resolve(
        Otlp("https://collector:4317", headers_secret="h", ca_secret="c").to_dict(),
        {},
        headers_dir=headers,
        ca_dir=ca,
    )
    assert settings is not None
    assert settings.headers == {"authorization": "Bearer x"}
    assert settings.ca_file == str(ca / "ca.crt")
    (headers / "bad name").write_text("x")
    from piceli.gitops.otel import OtlpConfigError

    with pytest.raises(OtlpConfigError) as error:
        resolve(
            {"endpoint": "https://c:4317", "headers_secret": "h"},
            {},
            headers_dir=headers,
        )
    assert "Bearer" not in str(error.value)
