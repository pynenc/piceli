"""Public telemetry imports must not initialize Kubernetes execution code."""


def test_telemetry_cold_import_is_available():
    from piceli.telemetry import NoopTelemetry, OtlpOptions

    assert NoopTelemetry().operation
    assert OtlpOptions("http://127.0.0.1:4318", "private-token").capacity == 256
