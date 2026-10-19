"""Where each deployed image was built (0.22.0).

Every image the GitOps controller records, built by its own Job or
delivered by a build host (``piceli builder serve``), keeps a
**provenance record**: the machine that built it (the build host's name, or
the cluster node the Job ran on), the builder image, the source commits,
the toolchains when the build reported them, and when it started and
ended. The controller keeps the record with the component's image (its
cache of built images, the environment's status and every run in the
history), so a machine found faulty later can be traced to the images it
made (``piceli gitops suspects --host NAME``) and those rebuilt elsewhere
(``piceli gitops rebuild --host NAME``).

The same record, as an in-toto Statement v1 with a SLSA provenance v1
predicate whose subject is the image's digest, is pushed next to the image
as an OCI 1.1 referrer (``artifactType: application/vnd.in-toto+json``)
when the registry takes it.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

STATEMENT = "https://in-toto.io/Statement/v1"
SLSA_PROVENANCE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://piceli.dev/gitops-build/v1"
#: How a record names the kind of builder.
BUILDERS = ("host", "cluster-job")
_KEYS = (
    "builder",
    "host",
    "node",
    "builder_image",
    "commits",
    "toolchains",
    "platforms",
    "started_at",
    "finished_at",
)


def record(
    *,
    builder: str,
    host: str,
    builder_image: str | None,
    commits: Mapping[str, str],
    started_at: str | None,
    finished_at: str | None,
    node: str | None = None,
    toolchains: Mapping[str, Any] | None = None,
    platforms: Iterable[str] = (),
) -> dict[str, Any]:
    """A provenance record (see the module doc); absent values are left out."""
    found: dict[str, Any] = {
        "builder": builder,
        "host": str(host)[:64],
        "node": None if node is None else str(node)[:64],
        "builder_image": None if builder_image is None else str(builder_image)[:300],
        "commits": {str(k): str(v)[:64] for k, v in sorted(dict(commits).items())},
        "toolchains": {str(k): str(v)[:200] for k, v in dict(toolchains).items()}
        if toolchains
        else None,
        "platforms": sorted(str(item) for item in platforms) or None,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    return {key: value for key, value in found.items() if value is not None}


def from_receipt(receipt: Mapping[str, Any], host: str) -> dict[str, Any]:
    """The record of a build host's ``built`` answer (its receipt)."""
    toolchains = receipt.get("toolchains")
    return record(
        builder="host",
        host=str(receipt.get("host") or host),
        builder_image=receipt.get("image"),
        commits=dict(receipt.get("commits") or {}),
        started_at=receipt.get("started_at"),
        finished_at=receipt.get("finished_at"),
        toolchains=toolchains if isinstance(toolchains, Mapping) else None,
        platforms=list(receipt.get("platforms") or ()),
    )


def clean(value: Any) -> dict[str, Any] | None:
    """A record read back from state or status, with only the known keys."""
    if not isinstance(value, Mapping) or not value.get("host"):
        return None
    return {key: value[key] for key in _KEYS if value.get(key) is not None}


def statement(repository: str, digest: str, provenance: Mapping[str, Any]) -> bytes:
    """``provenance`` as an in-toto Statement v1 (SLSA provenance v1) about
    ``repository@digest``: canonical JSON bytes, the same for the same record."""
    commits = dict(provenance.get("commits") or {})
    dependencies: list[dict[str, Any]] = [
        {"name": f"source:{name}", "digest": {"gitCommit": commit}}
        for name, commit in sorted(commits.items())
    ]
    image = str(provenance.get("builder_image") or "")
    if "@sha256:" in image:
        name, _, hexa = image.partition("@sha256:")
        dependencies.append({"uri": f"oci://{name}", "digest": {"sha256": hexa}})
    builder_id = (
        f"https://piceli.dev/builder/{provenance.get('builder', 'host')}/"
        f"{provenance.get('host', 'unknown')}"
    )
    body = {
        "_type": STATEMENT,
        "subject": [{"name": repository, "digest": {"sha256": digest.split(":")[-1]}}],
        "predicateType": SLSA_PROVENANCE,
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "commits": commits,
                    "platforms": list(provenance.get("platforms") or ()),
                },
                "internalParameters": {
                    key: provenance[key]
                    for key in ("node", "toolchains")
                    if provenance.get(key)
                },
                "resolvedDependencies": dependencies,
            },
            "runDetails": {
                "builder": {"id": builder_id},
                "metadata": {
                    key: value
                    for key, value in (
                        ("startedOn", provenance.get("started_at")),
                        ("finishedOn", provenance.get("finished_at")),
                    )
                    if value
                },
            },
        },
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo else found.replace(tzinfo=UTC)


def parse_since(value: str | None) -> datetime | None:
    """``--since``: an ISO time (``2026-10-01``, ``2026-10-01T12:00:00Z``).

    :raises ValueError: not an ISO time.
    """
    if value is None:
        return None
    found = _when(value)
    if found is None:
        raise ValueError(f"{value!r} is not an ISO time")
    return found


def built_on(provenance: Any, host: str, since: datetime | None = None) -> bool:
    """Whether ``provenance`` names ``host`` (the build host or the node) and
    the build ended at or after ``since``."""
    found = clean(provenance)
    if found is None or host not in {found.get("host"), found.get("node")}:
        return False
    if since is None:
        return True
    ended = _when(found.get("finished_at")) or _when(found.get("started_at"))
    return ended is None or ended >= since


def suspects(
    status: Mapping[str, Any] | None, host: str, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Every deployed image the controller's status says ``host`` built
    (since ``since``), one entry per environment, cluster and component,
    sorted."""
    found: list[dict[str, Any]] = []
    envs = (status or {}).get("envs") or {}
    for env, record_ in sorted(envs.items()):
        places: list[tuple[str | None, Mapping[str, Any]]] = [(None, record_)]
        places += sorted((record_.get("clusters") or {}).items())
        seen: set[tuple[str, str]] = set()
        for cluster, place in places:
            for name, entry in sorted((place.get("components") or {}).items()):
                if not isinstance(entry, Mapping):
                    continue
                provenance = entry.get("provenance")
                if not built_on(provenance, host, since):
                    continue
                key = (str(name), str(entry.get("digest")))
                if cluster is not None and key in seen:
                    continue
                seen.add(key)
                found.append(
                    {
                        "env": env,
                        **({"cluster": cluster} if cluster else {}),
                        "component": name,
                        "image": entry.get("image"),
                        "digest": entry.get("digest"),
                        "source_digest": entry.get("source_digest"),
                        "provenance": clean(provenance),
                    }
                )
    return found
