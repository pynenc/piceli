#!/usr/bin/env python3
"""Print the integration test files of one CI shard (stdlib only).

    python3 scripts/kind_shard.py SHARD SHARDS

The kind tests run serially on one cluster, so CI splits the files over
SHARDS runners, each with its own cluster. Files are assigned greedily,
longest first, by the seconds below (summed from the shards' ``--durations``
output); a file not listed counts {DEFAULT} s. Refresh the numbers when one
shard runs much longer than the others. The Flux test alone takes about five
minutes, so with five shards it has a shard to itself and sets the floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT = 5
# Seconds per file (setup, call and teardown of its tests) from the CI shards
# of 2026-11-23 on Kubernetes 1.37 (pull request #137).
SECONDS = {
    "test_gitops_flux_kind.py": 312,
    "test_restore_verify_kind.py": 135,
    "test_deploy_pipeline_kind.py": 110,
    "test_dev_run_kind.py": 93,
    "test_bundle_helm_kind.py": 91,
    "test_prerollout_kind.py": 85,
    "test_failed_build_output_kind.py": 60,
    "test_skill_kind.py": 53,
    "test_restore_points_kind.py": 50,
    "test_claim_growth_kind.py": 48,
    "test_shared_state_kind.py": 44,
    "test_registry_mirror_kind.py": 37,
    "test_release_orphans_kind.py": 30,
    "test_retention_cluster_kind.py": 27,
    "test_typed_kinds_kind.py": 23,
    "test_tailnet_policy_kind.py": 23,
    "test_two_images_kind.py": 22,
    "test_release_checks_kind.py": 22,
    "test_reference_app_kind.py": 20,
    "test_access_status_kind.py": 15,
    "test_release_diff_kind.py": 14,
    "test_network_policy_kind.py": 14,
    "test_import_kind.py": 13,
    "test_chart_helm_kind.py": 10,
    "test_first_install_checks_kind.py": 9,
    "test_release_adopt_kind.py": 9,
    "test_isolated_checks_kind.py": 7,
    "test_exec_auth_kind.py": 7,
    "test_custom_resources_kind.py": 7,
    "test_typed_rbac_kind.py": 6,
    "test_diagnosis_kind.py": 6,
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
