"""What ``cluster init`` installs for development builds, and a run's Job."""

from __future__ import annotations

import json
from typing import Any

from piceli.dev import jobs
from piceli.dev.model import DevBuilds, DevProfile
from piceli.infra import Cluster, Controller, Node

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
CONTROLLER = "ghcr.io/example/controller@sha256:" + "b" * 64


def _cluster(network: str = "fetch") -> Cluster:
    return Cluster(
        "my-cluster",
        api="https://10.0.0.1:6443",
        credentials="my-cluster",
        nodes=[Node("builder-1", arch="amd64", roles=["builder", "controller"])],
        storage_class="local-path",
        controller=Controller(on="builder-1", image=CONTROLLER),
        dev=DevBuilds(
            node="builder-1",
            image=IMAGE,
            profiles=[DevProfile("rust", tools=["cargo"], prefetch="cargo")],
            cache_size="300Gi",
            network=network,  # type: ignore[arg-type]
        ),
    )


def _by_kind(objects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"{o['kind']}/{o['metadata']['name']}": o for o in objects}


def test_init_installs_a_namespace_cache_policy_config_and_scheduler_rights() -> None:
    found = _by_kind(jobs.install_objects(_cluster()))
    namespace = found["Namespace/piceli-dev"]
    assert (
        namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"]
        == "restricted"
    )
    claim = found["PersistentVolumeClaim/piceli-dev-cache"]
    assert claim["metadata"]["namespace"] == "piceli-dev"
    assert claim["spec"]["resources"]["requests"]["storage"] == "300Gi"
    assert claim["spec"]["storageClassName"] == "local-path"
    config = json.loads(found["ConfigMap/piceli-dev-config"]["data"]["config.json"])
    assert config["node"] == "builder-1" and config["profiles"][0]["name"] == "rust"
    role = found["Role/piceli-dev-scheduler"]
    resources = {r for rule in role["rules"] for r in rule["resources"]}
    assert resources == {"jobs", "pods", "pods/log", "configmaps", "secrets"}
    secret_rules = [r for r in role["rules"] if r["resources"] == ["secrets"]]
    assert {tuple(r["verbs"]) for r in secret_rules} == {
        ("get", "update", "patch"),
        ("create",),
    }
    assert [r.get("resourceNames") for r in secret_rules if "get" in r["verbs"]] == [
        ["piceli-dev-git"]
    ]
    (config_rule,) = [r for r in role["rules"] if r["resources"] == ["configmaps"]]
    assert config_rule == {
        "apiGroups": [""],
        "resources": ["configmaps"],
        "resourceNames": ["piceli-dev-config"],
        "verbs": ["get"],
    }
    assert all(
        "create" not in rule["verbs"] or rule["resources"] == ["secrets"]
        for rule in role["rules"]
    )
    binding = found["RoleBinding/piceli-dev-scheduler"]
    assert binding["subjects"][0]["namespace"] == "piceli-system"


def test_runs_reach_dns_and_public_web_only() -> None:
    policy = _by_kind(jobs.install_objects(_cluster()))["NetworkPolicy/piceli-dev-runs"]
    spec = policy["spec"]
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in spec or spec["ingress"] == []
    dns, web = spec["egress"]
    assert {p["port"] for p in dns["ports"]} == {53}
    (block,) = web["to"]
    assert block["ipBlock"]["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"} <= set(
        block["ipBlock"]["except"]
    )
    assert {p["port"] for p in web["ports"]} == {80, 443}
    offline = _by_kind(jobs.install_objects(_cluster("none")))[
        "NetworkPolicy/piceli-dev-runs"
    ]
    assert len(offline["spec"]["egress"]) == 1  # DNS only


def test_a_run_job_is_isolated() -> None:
    dev = _cluster().dev
    assert dev is not None
    job = jobs.run_job(
        dev,
        dev.profile("rust"),
        run="20261008t071900-ab12",
        spec={"command": ["cargo", "test"], "archive_sha256": "0" * 64},
        requester="agent-7",
        priority="agent",
    )
    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == "piceli-dev"
    assert (
        job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] > 3600
    )
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "builder-1"}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = pod["containers"]
    assert container["image"] == IMAGE
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["requests"]["cpu"] == "4"
    assert container["resources"]["limits"]["memory"] == "8Gi"
    assert "cpu" not in container["resources"]["limits"]
    # No Secret reaches the container that runs the repository's code: no
    # env reference, and it mounts only its scratch and the cache.
    text = json.dumps(container)
    assert "secretKeyRef" not in text and "envFrom" not in text
    assert {m["name"] for m in container["volumeMounts"]} == {"work", "cache"}
    assert "initContainers" not in pod  # no sources: nothing to fetch
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["work"]["emptyDir"]["sizeLimit"] == "20Gi"
    assert volumes["cache"]["persistentVolumeClaim"]["claimName"] == "piceli-dev-cache"
    env = {item["name"]: item["value"] for item in container["env"]}
    spec = json.loads(env["PICELI_DEV_SPEC"])
    assert spec["command"] == ["cargo", "test"] and spec["prefetch"] == "cargo"
    assert spec["tools"] == ["cargo"] and spec["timeout_seconds"] == 3600
    assert spec["isolation_check"] is True  # no code before the policy holds
    assert container["command"][:3] == ["python3", "-I", "-c"]
    # The wrapper travels with the Job, compressed (0.20).
    import base64
    import re
    import zlib

    blob = re.search(r"b64decode\('([^']+)'\)", container["command"][3])
    assert blob is not None
    assert b"def main(" in zlib.decompress(base64.b64decode(blob.group(1)))
    labels = job["metadata"]["labels"]
    assert labels["piceli.io/dev-run"] == "20261008t071900-ab12"
    assert labels["piceli.io/dev-requester"] == "agent-7"
    assert labels["piceli.io/dev-priority"] == "agent"
    assert job["spec"]["template"]["metadata"]["labels"]["piceli.io/dev-run"]


def test_cluster_init_includes_development_builds() -> None:
    from piceli.infra.cluster_init import render_objects

    names = {
        f"{o['kind']}/{o['metadata']['name']}" for o in render_objects(_cluster(), None)
    }
    assert {"Namespace/piceli-dev", "NetworkPolicy/piceli-dev-runs"} <= names
    plain = Cluster("c", api="https://10.0.0.1:6443", credentials="c")
    assert not any(
        "piceli-dev" in name
        for name in (
            f"{o['kind']}/{o['metadata']['name']}" for o in render_objects(plain, None)
        )
    )


def test_sources_get_a_fetch_init_container_that_alone_mounts_the_git_secret() -> None:
    dev = _cluster().dev
    assert dev is not None
    job = jobs.run_job(
        dev,
        dev.profile("rust"),
        run="r1",
        spec={
            "command": ["cargo", "test"],
            "sources": [
                {
                    "name": "shop",
                    "key": "shop-1",
                    "url": None,
                    "head": "a" * 40,
                    "candidates": [],
                }
            ],
        },
        requester="agent-7",
        priority="agent",
    )
    pod = job["spec"]["template"]["spec"]
    (init,) = pod["initContainers"]
    (run,) = pod["containers"]
    env = {item["name"]: item["value"] for item in init["env"]}
    assert env["PICELI_DEV_ROLE"] == "fetch"
    assert {m["name"] for m in init["volumeMounts"]} == {"work", "cache", "git"}
    assert {m["name"] for m in run["volumeMounts"]} == {"work", "cache"}
    git = next(v for v in pod["volumes"] if v["name"] == "git")
    assert git["secret"] == {
        "secretName": "piceli-dev-git",
        "optional": True,
        "defaultMode": 0o440,
    }
    assert init["securityContext"] == run["securityContext"]
