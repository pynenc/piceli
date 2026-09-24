"""Example: Git commit and direct OCI digest delivery paths without public registry mandate."""

import json
from pathlib import Path

from piceli.k8s.automation import promote_release
from piceli.k8s.operator_state import StandingPolicy
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import ReleaseCatalog, ReleaseRecord, ReleaseSource


def run_pipeline(state_dir: Path) -> None:
    catalog_path = state_dir / "releases.json"
    catalog = ReleaseCatalog(catalog_path)

    archive = DeploymentSessionArchive.from_json(
        json.dumps(
            {
                "bundle": {},
                "composition": [],
                "private_inputs": [],
                "revision": {},
                "schema_version": 1,
                "session_id": "a" * 32,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    # 1. Git-based release
    git_source = ReleaseSource(
        kind="git",
        identity="1" * 40,
        artifact_digest="sha256:" + "b" * 64,
    )
    git_record = ReleaseRecord(
        name="feature-branch",
        source=git_source,
        namespace="my-app-preview",
        archive=archive,
    )
    catalog.add(git_record, select=True)
    print(
        f"[OK] Added Git release '{git_record.name}' with digest {git_source.artifact_digest}"
    )

    # 2. Direct OCI digest release (no git dependency, local containerd / registry.local:5000)
    oci_source = ReleaseSource(
        kind="oci",
        identity="sha256:" + "c" * 64,
        artifact_digest="sha256:" + "c" * 64,
    )
    oci_record = ReleaseRecord(
        name="direct-oci",
        source=oci_source,
        namespace="my-app-preview",
        archive=archive,
    )
    catalog.add(oci_record, select=False)
    print(
        f"[OK] Added direct OCI release '{oci_record.name}' with digest {oci_source.artifact_digest}"
    )

    # 3. Promote existing built digest to production tag without rebuild
    policy = StandingPolicy(
        name="prod-promotion",
        allowed_namespaces=("my-app-preview",),
        allowed_operations=("promote",),
    )
    promoted = promote_release(
        catalog, "feature-branch", "production-v1", policy=policy, select=True
    )
    print(
        f"[OK] Promoted '{promoted.name}' to production retaining immutable digest: {promoted.source.artifact_digest}"
    )
    assert catalog.selected().name == "production-v1"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run_pipeline(Path(tmp))
