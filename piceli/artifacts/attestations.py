"""SBOM and provenance documents for host-built images.

For every image a host build writes, next to its OCI archive:

* ``<name>.spdx.json``: an SPDX 2.3 document. Its packages are the image
  (by manifest digest), its base image (by digest), every file the build put
  in the image (path and sha256) and every package pinned by a ``Cargo.lock``
  among the staged context files (name, version, registry checksum, package
  URL). The lockfiles pin a superset of what one binary links. The document
  depends only on the build's inputs and outputs: the same build gives the
  same bytes.
* ``<name>.provenance.json``: an in-toto Statement v1 with a SLSA provenance
  v1 predicate. The subject is the image manifest digest; the build
  definition records the spec and plan hashes, the substituted commands and
  ``env``, the node facts, the pinned tools, the sources (commit, dirty,
  diff digest), the contexts and the base image.

Neither holds a path of this machine, a secret, or process output. Importing
this module runs nothing.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from piceli.artifacts.plan import canonical, digest

if TYPE_CHECKING:
    from piceli.artifacts.host_build import HostBuildPlan

SPDX_VERSION = "SPDX-2.3"
STATEMENT = "https://in-toto.io/Statement/v1"
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://piceli.dev/host-build/v1"
MAX_LOCK_BYTES = 8 * 1024 * 1024


def _time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spdx_id(kind: str, value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ".-" else "-" for char in value)
    return f"SPDXRef-{kind}-{safe}"[:200]


def cargo_packages(plan: HostBuildPlan) -> list[dict[str, str]]:
    """Every ``[[package]]`` of the staged ``Cargo.lock`` files, sorted, once each."""
    found: dict[tuple[str, str, str], dict[str, str]] = {}
    for name, manifest in plan.contexts.items():
        root = plan.context_roots[name]
        for item in manifest.files:
            if PurePosixPath(item.path).name != "Cargo.lock":
                continue
            if item.size > MAX_LOCK_BYTES:
                continue
            path = root.joinpath(*PurePosixPath(item.path).parts)
            try:
                document = tomllib.loads(path.read_text())
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                continue
            for package in document.get("package") or []:
                if not isinstance(package, dict):
                    continue
                entry = {
                    key: str(package[key])
                    for key in ("name", "version", "source", "checksum")
                    if isinstance(package.get(key), str)
                }
                if "name" in entry and "version" in entry:
                    key = (entry["name"], entry["version"], entry.get("source", ""))
                    found[key] = entry
    return [found[key] for key in sorted(found)]


def sbom(
    name: str,
    repository: str,
    image: Mapping[str, Any],
    crates: Sequence[Mapping[str, str]],
    epoch: int,
) -> dict[str, Any]:
    """The SPDX 2.3 document of one image (see the module docstring)."""
    manifest = str(image["digest"])
    root = _spdx_id("Image", name)
    packages: list[dict[str, Any]] = [
        {
            "SPDXID": root,
            "name": repository,
            "versionInfo": manifest,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "checksums": [{"algorithm": "SHA256", "checksumValue": manifest[7:]}],
            "primaryPackagePurpose": "CONTAINER",
        }
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root,
        }
    ]
    base = image.get("base")
    if isinstance(base, Mapping):
        base_id = _spdx_id("Base", str(base["reference"]))
        packages.append(
            {
                "SPDXID": base_id,
                "name": str(base["reference"]).split("@", 1)[0],
                "versionInfo": str(base["manifest"]),
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "checksums": [
                    {
                        "algorithm": "SHA256",
                        "checksumValue": str(base["manifest"])[7:],
                    }
                ],
                "primaryPackagePurpose": "CONTAINER",
            }
        )
        relationships.append(
            {
                "spdxElementId": root,
                "relationshipType": "DESCENDANT_OF",
                "relatedSpdxElement": base_id,
            }
        )
    files = []
    for item in image.get("files") or []:
        file_id = _spdx_id("File", item["path"])
        files.append(
            {
                "SPDXID": file_id,
                "fileName": item["path"],
                "checksums": [
                    {"algorithm": "SHA256", "checksumValue": item["sha256"][7:]}
                ],
            }
        )
        relationships.append(
            {
                "spdxElementId": root,
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": file_id,
            }
        )
    for crate in crates:
        crate_id = _spdx_id("Crate", f"{crate['name']}-{crate['version']}")
        package: dict[str, Any] = {
            "SPDXID": crate_id,
            "name": crate["name"],
            "versionInfo": crate["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
        }
        source = crate.get("source", "")
        if source.startswith("registry+"):
            package["externalRefs"] = [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": f"pkg:cargo/{crate['name']}@{crate['version']}",
                }
            ]
        if "checksum" in crate:
            package["checksums"] = [
                {"algorithm": "SHA256", "checksumValue": crate["checksum"]}
            ]
        packages.append(package)
        relationships.append(
            {
                "spdxElementId": root,
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": crate_id,
            }
        )
    unique: dict[str, dict[str, Any]] = {item["SPDXID"]: item for item in packages}
    return {
        "spdxVersion": SPDX_VERSION,
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{repository}@{manifest}",
        "documentNamespace": f"https://piceli.dev/spdx/{manifest[7:]}",
        "creationInfo": {
            "created": _time(epoch),
            "creators": ["Tool: piceli-host-build"],
        },
        "packages": list(unique.values()),
        "files": files,
        "relationships": relationships,
    }


def provenance(
    repository: str,
    image: Mapping[str, Any],
    plan: HostBuildPlan,
    sources: Sequence[Mapping[str, Any]],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """The in-toto / SLSA provenance statement of one image."""
    spec = plan.spec
    dependencies: list[dict[str, Any]] = []
    for source in sources:
        entry: dict[str, Any] = {"name": f"source:{source.get('name')}"}
        if isinstance(source.get("commit"), str):
            entry["digest"] = {"gitCommit": source["commit"]}
        entry["annotations"] = {
            "dirty": bool(source.get("dirty")),
            "diff_sha256": source.get("diff_sha256"),
        }
        dependencies.append(entry)
    for name, manifest in plan.contexts.items():
        dependencies.append(
            {"name": f"context:{name}", "digest": {"sha256": manifest.sha256[7:]}}
        )
    base = image.get("base")
    if isinstance(base, Mapping):
        dependencies.append(
            {
                "uri": f"oci://{base['reference']}",
                "digest": {"sha256": str(base["manifest"])[7:]},
            }
        )
    return {
        "_type": STATEMENT,
        "subject": [
            {"name": repository, "digest": {"sha256": str(image["digest"])[7:]}}
        ],
        "predicateType": SLSA_PROVENANCE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "spec_sha256": spec.spec_sha256,
                    "plan_hash": plan.plan_hash,
                    "platform": plan.platform,
                    "commands": [list(item) for item in plan.commands],
                    "env": dict(plan.env),
                    "source_date_epoch": spec.source_date_epoch,
                },
                "internalParameters": {
                    "node_facts": plan.facts.to_dict(),
                    "tools": plan.builder()["tools"],
                },
                "resolvedDependencies": dependencies,
            },
            "runDetails": {
                "builder": {"id": BUILD_TYPE},
                "metadata": {
                    "invocationId": plan.plan_hash,
                    "startedOn": started_at,
                    "finishedOn": finished_at,
                },
            },
        },
    }


def write(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """Write ``document`` canonically; returns ``{"path": name, "sha256": …}``."""
    body = canonical(dict(document))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return {"path": path.name, "sha256": digest(body)}
