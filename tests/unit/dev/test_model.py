"""``Cluster(dev=DevBuilds(...))``: development builds declared on a cluster."""

from __future__ import annotations

import pytest

from piceli.infra import Cluster, Node
from piceli.infra.cluster import ClusterError

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64


def _cluster(**dev: object) -> Cluster:
    from piceli.dev.model import DevBuilds, DevProfile

    return Cluster(
        "my-cluster",
        api="https://10.0.0.1:6443",
        credentials="my-cluster",
        nodes=[
            Node("builder-1", arch="amd64", roles=["builder"]),
            Node("worker-1", arch="arm64", roles=["workloads"]),
        ],
        dev=DevBuilds(
            **{  # type: ignore[arg-type]
                "node": "builder-1",
                "image": IMAGE,
                "profiles": [
                    DevProfile("rust", tools=["cargo"], prefetch="cargo"),
                    DevProfile("node", tools=["node"], cpu="2", memory="2Gi"),
                ],
                **dev,
            }
        ),
    )


def test_profiles_inherit_the_run_size_and_image() -> None:
    cluster = _cluster(run_cpu="6", run_memory="12Gi")
    assert cluster.dev is not None
    rust = cluster.dev.profile("rust")
    assert (rust.image, rust.cpu, rust.memory) == (IMAGE, "6", "12Gi")
    node = cluster.dev.profile("node")
    assert (node.cpu, node.memory) == ("2", "2Gi")
    assert cluster.dev.profile(None).name == "rust"  # the first is the default


def test_the_declaration_is_in_the_cluster_description_only_when_set() -> None:
    described = _cluster().describe()
    assert described["dev"]["node"] == "builder-1"
    assert [p["name"] for p in described["dev"]["profiles"]] == ["rust", "node"]
    plain = Cluster("c", api="https://10.0.0.1:6443", credentials="c")
    assert "dev" not in plain.describe()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"node": "nowhere"}, "not a declared node"),
        ({"image": "example/builder:latest"}, "pinned by digest"),
        ({"slots": 0}, "slots"),
        ({"run_memory": "lots"}, "run_memory"),
        ({"cache_size": "1Mi"}, "cache_size"),
        ({"network": "open"}, "network"),
    ],
)
def test_bad_declarations_are_refused(change: dict[str, object], message: str) -> None:
    with pytest.raises(ClusterError) as raised:
        _cluster(**change)
    assert raised.value.code == "cluster-invalid" and message in str(raised.value)


def test_profile_names_are_unique_labels() -> None:
    from piceli.dev.model import DevBuilds, DevProfile

    with pytest.raises(ClusterError):
        DevBuilds(
            node="b", image=IMAGE, profiles=[DevProfile("rust"), DevProfile("rust")]
        )
    with pytest.raises(ClusterError):
        DevProfile("Not A Label")


def test_unknown_profile_is_refused() -> None:
    from piceli.dev.model import DevError

    cluster = _cluster()
    assert cluster.dev is not None
    with pytest.raises(DevError) as raised:
        cluster.dev.profile("python")
    assert raised.value.code == "dev-profile-unknown"
