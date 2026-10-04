"""A pipeline for ``piceli bundle`` CLI tests (no build, an external image)."""

from __future__ import annotations

from piceli import Pipeline, Target
from tests.unit.bundle.fixtures import app as make_app

app, secrets = make_app()
app.environment("client", namespace="shop-client")
pipeline = Pipeline(
    app,
    {"client": Target.profile("client", namespace="shop-client")},
    secrets=secrets,
)
