"""Cluster builds: the Job spec, the cache claim, the receipt, and ``env push``.

The fake API serves Jobs and pod logs; the build itself (``job_run``) runs a
real host build into the in-process registry, as the Job would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli import App, Build, Pipeline, Registry, Target
from piceli.artifacts.cluster_build import (
    BuildCluster,
    ClusterBuildConfig,
    cache_claim_name,
    decode_receipt,
    encode_receipt,
    job_run,
    plan_build_job,
    run_build_job,
    slug,
)
from piceli.k8s.cli.env_push import (
    apply_push,
    configmap_name,
    images_from_digests,
    images_from_receipt,
    plan_push,
)
from piceli.pipeline.errors import PipelineError
from piceli.testing import TARGET, manifest
from tests.unit.host_build_support import (
    FACTS_4K,
    FACTS_16K,
    FACTS_AMD64,
    publish_base,
    write_project,
)
from tests.unit.test_registry_delivery import FakeRegistry

COMMIT = "a" * 40
IMAGE = "registry.example/builder@sha256:" + "b" * 64
SECRET_VALUE = "hunter2-token"


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


def pipeline_for(tmp_path: Path, facts: Any = FACTS_16K, port: int = 5000) -> Pipeline:
    spec = tmp_path / "host-build.toml"
    if not spec.exists():
        spec.write_text("")
    target = Target.kubeconfig(
        "kubeconfig",
        context="fake",
        namespace=TARGET.namespace,
        nodes={"w": "worker-1"},
    )
    images = Build.spec(spec, builder="host", node_facts=facts.to_dict())
    return Pipeline(
        App("shop"),
        target,
        build=images,
        deliver=Registry(f"oci://127.0.0.1:{port}/shop"),
        state_dir=tmp_path / "state",
    )


def config(**extra: Any) -> ClusterBuildConfig:
    return ClusterBuildConfig(image=IMAGE, repo="https://git.example/shop.git", **extra)


def plan(tmp_path: Path, **kwargs: Any) -> Any:
    facts = kwargs.pop("facts", FACTS_16K)
    return plan_build_job(
        pipeline_for(tmp_path, facts),
        kwargs.pop("commit", COMMIT),
        cache_key=kwargs.pop("cache_key", "wp/fix-1"),
        platforms=kwargs.pop("platforms", ("linux/arm64", "linux/amd64")),
        config=config(repo_root=tmp_path, **kwargs),
    )


def test_the_job_runs_on_the_builder_node_and_carries_no_secret(tmp_path: Path) -> None:
    built = plan(tmp_path)
    pod = built.job["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {
        "piceli.io/builder": "true",
        "kubernetes.io/arch": "amd64",
    }
    assert pod["hostNetwork"] is True  # the registry is on the node's loopback
    container = pod["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["PICELI_BUILD_COMMIT"]["value"] == COMMIT
    assert env["GIT_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "piceli-build-git",
        "key": "password",
    }
    assert "value" not in env["GIT_PASSWORD"]
    assert SECRET_VALUE not in json.dumps(built.job)
    assert "GIT_ASKPASS" in container["command"][2]
    assert "$GIT_PASSWORD" not in " ".join(container["command"][3:])
    assert built.job["spec"]["activeDeadlineSeconds"] == 3600
    assert built.job["spec"]["template"]["spec"]["volumes"][0][
        "persistentVolumeClaim"
    ] == {"claimName": built.claim["metadata"]["name"]}
    facts = json.loads(env["PICELI_BUILD_FACTS"]["value"])
    assert facts["linux/arm64"]["page_size"] == 16384
    assert facts["linux/amd64"]["page_size"] == 4096
    assert set(built.preview()) >= {"plan_hash", "job", "cache_claim", "node_facts"}


def test_the_cache_is_keyed_per_branch_and_page_size(tmp_path: Path) -> None:
    sixteen = plan(tmp_path)
    four = plan(tmp_path, facts=FACTS_4K)
    other_branch = plan(tmp_path, cache_key="main")
    names = {item.claim["metadata"]["name"] for item in (sixteen, four, other_branch)}
    assert len(names) == 3
    assert "16k" in sixteen.claim["metadata"]["name"]
    assert len({sixteen.plan_hash, four.plan_hash, other_branch.plan_hash}) == 3
    assert plan(tmp_path).plan_hash == sixteen.plan_hash  # deterministic
    assert plan(tmp_path, commit="c" * 40).plan_hash != sixteen.plan_hash
    assert (
        cache_claim_name("c", "wp/x", [4096, 16384]) == f"c-{slug('wp/x', 32)}-4k-16k"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commit": "main"},
        {"platforms": ("linux/s390x",)},
        {"cache_key": ""},
    ],
)
def test_bad_inputs_are_refused(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(PipelineError) as raised:
        plan(tmp_path, **kwargs)
    assert raised.value.code == "cluster-build-invalid"


def test_credentials_in_the_repo_url_and_unpinned_images_are_refused() -> None:
    with pytest.raises(PipelineError):
        ClusterBuildConfig(image=IMAGE, repo="https://user:pw@git.example/x.git")
    with pytest.raises(PipelineError):
        ClusterBuildConfig(image="builder:latest", repo="https://git.example/x.git")


def cluster_for(local_api: Any) -> BuildCluster:
    _, provider = local_api
    return BuildCluster(
        provider.client, TARGET.namespace, poll_seconds=0, request_seconds=5
    )


def test_run_build_job_returns_the_receipt_and_removes_the_job(
    tmp_path: Path, local_api: Any
) -> None:
    api, _ = local_api
    api.put(manifest("Secret", "piceli-build-git"))
    api.objects[("Secret", "piceli-build-git")]["data"] = {
        "username": "eA==",
        "password": "eA==",
    }
    receipt = {
        "state": "succeeded",
        "outputs": {"images": {"web": {"digest": "sha256:" + "d" * 64}}},
    }
    api.job_result(
        "piceli-build-",
        container="build",
        logs=f"cloning\nbuilding\n\n{encode_receipt(receipt)}\n",
    )
    pipeline = pipeline_for(tmp_path)
    built = plan_build_job(
        pipeline, COMMIT, cache_key="wp/x", config=config(repo_root=tmp_path)
    )
    with pytest.raises(PipelineError) as stale:
        run_build_job(
            pipeline,
            COMMIT,
            cache_key="wp/x",
            config=config(repo_root=tmp_path),
            approve="sha256:" + "0" * 64,
            cluster=cluster_for(local_api),
        )
    assert stale.value.code == "cluster-build-invalid"
    result = run_build_job(
        pipeline,
        COMMIT,
        cache_key="wp/x",
        config=config(repo_root=tmp_path),
        approve=built.plan_hash,
        cluster=cluster_for(local_api),
    )
    assert result["outputs"]["images"]["web"]["digest"] == "sha256:" + "d" * 64
    assert result["job"]["commit"] == COMMIT
    assert result["job"]["cache_claim"] == built.claim["metadata"]["name"]
    assert [k for k in api.objects if k[0] in {"Job", "Pod"}] == []  # cleaned
    assert ("PersistentVolumeClaim", built.claim["metadata"]["name"]) in api.objects


def test_a_failed_job_or_missing_secret_is_a_registered_failure(
    tmp_path: Path, local_api: Any
) -> None:
    api, _ = local_api
    pipeline = pipeline_for(tmp_path)
    args: dict[str, Any] = {"cache_key": "b", "config": config(repo_root=tmp_path)}
    with pytest.raises(PipelineError) as missing:
        run_build_job(pipeline, COMMIT, cluster=cluster_for(local_api), **args)
    assert missing.value.code == "cluster-build-invalid"
    api.put(manifest("Secret", "piceli-build-git"))
    api.objects[("Secret", "piceli-build-git")]["data"] = {
        "username": "eA==",
        "password": "eA==",
    }
    api.job_result(
        "piceli-build-",
        container="build",
        exit_code=1,
        logs=f"boom password={SECRET_VALUE}\n",
    )
    with pytest.raises(PipelineError) as failed:
        run_build_job(pipeline, COMMIT, cluster=cluster_for(local_api), **args)
    assert failed.value.code == "cluster-build-failed" and failed.value.failed
    assert SECRET_VALUE not in json.dumps(failed.value.details)


def test_the_job_builds_both_platforms_and_pushes_by_digest(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    base = publish_base(registry, architectures=("arm64", "amd64"))
    source = tmp_path / "src-repo"
    write_project(source, registry.port, base)
    receipt = job_run(
        source=source,
        specs=["host-build.toml"],
        cache=tmp_path / "cache",
        out=tmp_path / "out",
        registry_url=f"oci://127.0.0.1:{registry.port}/shop",
        node_registry=f"127.0.0.1:{registry.port}",
        facts={"linux/arm64": FACTS_16K, "linux/amd64": FACTS_AMD64},
        commit=COMMIT,
        timeout_seconds=300,
    )
    assert receipt["platforms"] == ["linux/arm64", "linux/amd64"]
    web = receipt["outputs"]["images"]["web"]
    assert web["delivered"]["pull_ref"].endswith("@" + web["digest"])
    assert set(receipt["platform_receipts"]) == {"linux/amd64"}
    assert receipt["node_facts"]["page_size"] == 16384
    assert set(receipt["delivered"]) == {"linux/arm64", "linux/amd64"}
    # Separate target directories per architecture and page size (B28).
    targets = sorted(p.name for p in (tmp_path / "cache" / "target").iterdir())
    assert targets == ["amd64-4096", "arm64-16384"]
    assert decode_receipt(encode_receipt(receipt).split(" ", 1)[1]) == json.loads(
        json.dumps(receipt)
    )


def test_env_push_records_a_digest_in_the_branch_namespace(local_api: Any) -> None:
    api, provider = local_api
    receipt = {
        "commit": COMMIT,
        "outputs": {
            "images": {
                "web": {
                    "digest": "sha256:" + "e" * 64,
                    "platform": "linux/arm64",
                    "delivered": {
                        "pull_ref": "127.0.0.1:5000/shop/web@sha256:" + "e" * 64
                    },
                }
            }
        },
    }
    images = images_from_receipt(receipt)
    assert images["web"]["pull_ref"].startswith("127.0.0.1:5000/shop/web@")
    assert images_from_digests(["api=sha256:" + "f" * 64]) == {
        "api": {"digest": "sha256:" + "f" * 64}
    }
    with pytest.raises(ValueError):
        images_from_digests(["api=latest"])
    planned = plan_push("wp/Fix 1", TARGET.namespace, images, COMMIT)
    assert planned["configmap"]["metadata"]["name"] == configmap_name("wp/Fix 1")
    cluster = BuildCluster(provider.client, TARGET.namespace, request_seconds=5)
    apply_push(cluster, "wp/Fix 1", planned)
    stored = api.objects[("ConfigMap", configmap_name("wp/Fix 1"))]
    assert json.loads(stored["data"]["images"]) == images
    assert stored["data"]["commit"] == COMMIT and "pushed_at" in stored["data"]
