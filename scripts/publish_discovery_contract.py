"""Generate Piceli v2 schema/fixture pins from the retained historical v1 inputs."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(relative: str, value: dict) -> dict[str, str]:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main() -> None:
    schema = json.loads(
        (ROOT / "docs/schemas/piceli-discovery-v1.schema.json").read_text()
    )
    schema[
        "$id"
    ] = "https://docs.piceli.pynenc.org/schemas/piceli-discovery-v2.schema.json"
    schema["title"] = "Piceli bounded discovery artifact v2"
    props = schema["properties"]
    props["schema_version"] = {"type": "integer", "const": 2}
    props["captured_at"] = {"type": "string", "format": "date-time"}
    schema["required"].append("provenance")
    props["provenance"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source", "endpoint_id", "cluster_uid", "namespace_uid"],
        "properties": {
            "source": {"enum": ["synthetic", "loopback", "live"]},
            "endpoint_id": {"type": "string", "minLength": 1, "maxLength": 4096},
            "cluster_uid": {"type": "string", "maxLength": 4096},
            "namespace_uid": {"type": "string", "maxLength": 4096},
        },
        "allOf": [
            {
                "if": {"properties": {"source": {"enum": ["loopback", "live"]}}},
                "then": {
                    "properties": {
                        "cluster_uid": {"minLength": 1},
                        "namespace_uid": {"minLength": 1},
                    }
                },
            }
        ],
    }
    limits = props["limits"]
    for key, maximum in {
        "max_resource_types": 256,
        "page_size": 1000,
        "max_pages": 4096,
        "max_resources": 100000,
        "max_artifact_bytes": 10000000,
    }.items():
        limits["properties"][key]["maximum"] = maximum
    for key in ("max_seconds", "call_seconds"):
        limits["required"].append(key)
        limits["properties"][key] = {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": 3600,
        }
    schema["$defs"]["failure"]["properties"]["kind"]["enum"].append("deadline-exceeded")
    props["defaulted_fields"]["uniqueItems"] = True
    props["defaulted_fields"]["items"]["properties"]["json_pointer"][
        "pattern"
    ] = r"^/(?!apiVersion(?:/|$)|kind(?:/|$)|metadata(?:/|$)|data(?:/|$)|stringData(?:/|$)|status(?:/|$))(?:[^~]|~[01])*$"
    manifest = props["resources"]["items"]["properties"]["manifest"]
    manifest.update(
        {
            "required": ["apiVersion", "kind", "metadata"],
            "properties": {
                "apiVersion": {"type": "string", "minLength": 1},
                "kind": {"type": "string", "minLength": 1},
                "metadata": {
                    "type": "object",
                    "required": ["name", "uid", "resourceVersion"],
                    "properties": {
                        key: {"type": "string", "minLength": 1}
                        for key in ("name", "uid", "resourceVersion")
                    },
                },
            },
        }
    )
    fixture = json.loads(
        (ROOT / "tests/fixtures/discovery-v1/complete.json").read_text()
    )
    fixture["schema_version"] = 2
    fixture["provenance"] = {
        "source": "synthetic",
        "endpoint_id": "portable-fixture-v2",
        "cluster_uid": "",
        "namespace_uid": "",
    }
    fixture["limits"].update({"max_seconds": 30.0, "call_seconds": 5.0})
    fixture["resources"][0]["manifest"]["spec"]["revisionHistoryLimit"] = 10
    write(
        "docs/schemas/piceli-discovery-v2.manifest.json",
        {
            "contract": "piceli.discovery.v2",
            "schema": write("docs/schemas/piceli-discovery-v2.schema.json", schema),
            "fixture": write("tests/fixtures/discovery-v2/complete.json", fixture),
        },
    )


if __name__ == "__main__":
    main()
