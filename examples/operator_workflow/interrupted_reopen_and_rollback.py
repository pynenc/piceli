"""Example: Interrupted session reopen, safe resume, and health-aware rollback."""

import json
from pathlib import Path

from piceli.k8s.automation import health_aware_rollback
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import ReleaseCatalog, ReleaseRecord, ReleaseSource


def run_reopen_and_rollback(state_dir: Path) -> None:
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
                "session_id": "e" * 32,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    # Add v1 stable
    r1 = ReleaseRecord(
        name="v1-stable",
        source=ReleaseSource("git", "1" * 40, artifact_digest="sha256:" + "1" * 64),
        namespace="my-app-preview",
        archive=archive,
    )
    catalog.add(r1, select=True)

    # Add v2 buggy
    r2 = ReleaseRecord(
        name="v2-buggy",
        source=ReleaseSource("git", "2" * 40, artifact_digest="sha256:" + "2" * 64),
        namespace="my-app-preview",
        archive=archive,
    )
    catalog.add(r2, select=True)
    print(f"[OK] Current active release is: {catalog.selected().name}")

    # Reopen v1 without rotating inputs
    reopened = catalog.get("v1-stable")
    assert reopened.archive.session_id == "e" * 32
    print(f"[OK] Reopened '{reopened.name}' preserving exact canonical session {reopened.archive.session_id}")

    # Execute health-aware rollback
    class DummyWorkflow:
        def __init__(self, cat):
            self.catalog = cat
            self.namespace = "my-app-preview"
        def reopen(self, name):
            class DummySession:
                def apply(self, executor):
                    return {"state": "ready"}
            return DummySession()

    res = health_aware_rollback(
        DummyWorkflow(catalog),
        "v1-stable",
        None,
        check_health_fn=lambda ns, name: True,
    )
    print(f"[OK] Health-aware rollback succeeded: {res['rollback_target']} is now active")
    assert catalog.selected().name == "v1-stable"


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        run_reopen_and_rollback(Path(tmp))
