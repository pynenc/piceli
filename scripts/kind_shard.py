#!/usr/bin/env python3
"""Print the integration test files of one CI shard (stdlib only).

    python3 scripts/kind_shard.py SHARD SHARDS

The kind tests run serially on one cluster, so CI splits the files over
SHARDS runners, each with its own cluster. Files are assigned greedily,
longest first, by the seconds below (a local run's ``--durations``); a file
not listed counts {DEFAULT} s. Refresh the numbers from the shards'
``--durations`` output when one shard runs much longer than the others.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT = 15
SECONDS = {
    "test_gitops_flux_kind.py": 420,
    "test_dev_run_kind.py": 167,
    "test_deploy_pipeline_kind.py": 138,
    "test_restore_verify_kind.py": 127,
    "test_failed_build_output_kind.py": 86,
    "test_prerollout_kind.py": 83,
    "test_bundle_helm_kind.py": 60,
    "test_claim_growth_kind.py": 50,
    "test_restore_points_kind.py": 44,
    "test_shared_state_kind.py": 41,
    "test_skill_kind.py": 38,
    "test_reference_app_kind.py": 35,
    "test_registry_mirror_kind.py": 33,
    "test_access_status_kind.py": 31,
    "test_release_orphans_kind.py": 29,
    "test_retention_cluster_kind.py": 28,
    "test_custom_resources_kind.py": 28,
    "test_typed_kinds_kind.py": 25,
    "test_kill_kind.py": 24,
    "test_tailnet_policy_kind.py": 22,
    "test_ownership_hpa_kind.py": 22,
    "test_release_checks_kind.py": 21,
}


def shard(index: int, count: int, files: list[str]) -> list[str]:
    loads = [0] * count
    chosen: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(files, key=lambda f: (-SECONDS.get(f, DEFAULT), f)):
        target = loads.index(min(loads))
        loads[target] += SECONDS.get(name, DEFAULT)
        chosen[target].append(name)
    return sorted(chosen[index - 1])


if __name__ == "__main__":
    index, count = int(sys.argv[1]), int(sys.argv[2])
    root = Path(__file__).resolve().parent.parent / "tests" / "integration"
    names = sorted(path.name for path in root.glob("test_*.py"))
    print("\n".join(f"tests/integration/{name}" for name in shard(index, count, names)))
