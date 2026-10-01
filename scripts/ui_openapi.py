"""Export the actual FastAPI schema without targets, credentials or a listener."""

import argparse
import json
from pathlib import Path

from piceli.server.app import create_app
from piceli.services.query import QueryService


def schema() -> str:
    return (
        json.dumps(create_app(QueryService([])).openapi(), indent=2, sort_keys=True)
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "ui/openapi.json"
    rendered = schema()
    if args.check:
        if not target.exists() or target.read_text() != rendered:
            raise SystemExit(
                "OpenAPI snapshot is stale; run python scripts/ui_openapi.py"
            )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered)
