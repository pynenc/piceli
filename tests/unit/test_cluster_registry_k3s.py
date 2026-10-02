"""The in-cluster registry on k3s nodes: the agent that merges ``registries.yaml``.

The agent script runs here with ``sh`` and ``awk`` against a temporary
directory standing in for the node's ``/etc/rancher/k3s`` and k3s's containerd
directory: it adds its entry without touching anything else, keeps the file's
mode, refuses a file it cannot edit, reports whether k3s needs a restart, and
on stop leaves the file exactly as it found it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from piceli import Registry
from piceli.artifacts import cluster_registry as cr
from piceli.pipeline import PipelineError

HOST = "piceli-registry.piceli-system.svc:5000"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("awk") is None,
    reason="needs a POSIX sh and awk",
)


class Node:
    def __init__(self, root: Path) -> None:
        self.etc = root / "etc-rancher-k3s"
        self.containerd = root / "k3s-containerd"
        self.state = root / "state"
        self.containerd.mkdir(parents=True)

    @property
    def registries(self) -> Path:
        return self.etc / "registries.yaml"

    @property
    def hosts_toml(self) -> Path:
        return self.containerd / "certs.d" / HOST / "hosts.toml"

    def script(self, registry: Any) -> str:
        return (
            cr.k3s_agent_script(registry)
            .replace(cr.K3S_ETC_MOUNT, str(self.etc))
            .replace(cr.K3S_CONTAINERD_MOUNT, str(self.containerd))
            .replace(cr.AGENT_STATE_DIR, str(self.state))
        )


@contextmanager
def agent(
    node: Node, address: str = "10.43.0.9", stop: int = signal.SIGTERM
) -> Iterator[list[str]]:
    """Run the agent until its report line; stop it (``stop``) on exit."""
    process = subprocess.Popen(
        ["sh", "-c", node.script(Registry.in_cluster(on="node-a"))],
        env={"PATH": os.environ["PATH"], "PICELI_REGISTRY_SERVICE_HOST": address},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    lines: list[str] = []
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            if line.startswith(cr.REPORT_PREFIX):
                break
        yield lines
    finally:
        os.killpg(process.pid, stop)
        process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()


def _report(lines: list[str]) -> dict[str, str] | None:
    return cr.parse_report("\n".join(lines))


def test_a_new_file_is_created_and_removed_again(tmp_path: Path) -> None:
    node = Node(tmp_path)
    (node.containerd / "config.toml").write_text(
        '[plugins."io.containerd.grpc.v1.cri".registry]\n'
        '  config_path = "/var/lib/rancher/k3s/agent/etc/containerd/certs.d"\n'
    )
    with agent(node) as lines:
        assert _report(lines) == {
            "runtime": "k3s",
            "file": "written",
            "restart": "not-needed",  # containerd reads certs.d: in effect now
        }
        text = node.registries.read_text()
        assert text.startswith(cr.CREATED_MARK + "\n")
        assert yaml.safe_load(text) == {
            "mirrors": {HOST: {"endpoint": ["http://10.43.0.9:5000"]}}
        }
        assert node.registries.stat().st_mode & 0o777 == 0o600
        assert node.hosts_toml.read_text() == cr.hosts_toml("10.43.0.9", 5000)
        assert (node.state / "ready").exists()
    assert not node.registries.exists()
    assert not node.hosts_toml.parent.exists()


def test_existing_entries_are_kept_and_restored_exactly(tmp_path: Path) -> None:
    node = Node(tmp_path)
    node.etc.mkdir()
    original = (
        "# site registries\n"
        "mirrors:\n"
        "    docker.io:\n"
        "        endpoint:\n"
        '            - "https://mirror.example.com"\n'
        "configs:\n"
        "    mirror.example.com:\n"
        "        auth:\n"
        "            username: robot\n"
        "            password: not-a-real-secret\n"
    )
    node.registries.write_text(original)
    node.registries.chmod(0o640)
    with agent(node) as lines:
        # No config_path in k3s's containerd config: an old k3s, restart needed.
        assert _report(lines) == {
            "runtime": "k3s",
            "file": "written",
            "restart": "needed",
        }
        merged = yaml.safe_load(node.registries.read_text())
        assert merged["mirrors"] == {
            "docker.io": {"endpoint": ["https://mirror.example.com"]},
            HOST: {"endpoint": ["http://10.43.0.9:5000"]},
        }
        assert merged["configs"]["mirror.example.com"]["auth"]["username"] == "robot"
        assert node.registries.stat().st_mode & 0o777 == 0o640
        # Nothing of the file reaches the log (it may hold registry passwords).
        assert not any("not-a-real-secret" in line for line in lines)
    assert node.registries.read_text() == original
    assert node.registries.stat().st_mode & 0o777 == 0o640


def test_a_rerun_is_unchanged_and_a_file_without_mirrors_gets_the_key(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)
    node.etc.mkdir()
    node.registries.write_text("configs:\n  other.example:\n    tls: {}\n")
    with agent(node):
        first = node.registries.read_text()
    # Stopped: only the empty key it added stays (valid, means no mirrors).
    assert yaml.safe_load(node.registries.read_text()) == {
        "configs": {"other.example": {"tls": {}}},
        "mirrors": None,
    }
    # Killed without cleanup (a node crash): the next agent finds its entry.
    with agent(node, stop=signal.SIGKILL):
        assert node.registries.read_text() == first
    with agent(node) as lines:
        assert _report(lines) == {
            "runtime": "k3s",
            "file": "unchanged",
            "restart": "needed",
        }
        assert node.registries.read_text() == first


@pytest.mark.parametrize(
    "content",
    [
        "mirrors: {}\n",
        '{"mirrors": {}}\n',
        "'mirrors':\n  a: {endpoint: [x]}\n",
        "mirrors:\n  a: {}\nmirrors:\n  b: {}\n",
    ],
)
def test_a_file_it_cannot_edit_is_left_alone(tmp_path: Path, content: str) -> None:
    node = Node(tmp_path)
    node.etc.mkdir()
    node.registries.write_text(content)
    with agent(node) as lines:
        assert _report(lines) == {
            "runtime": "k3s",
            "file": "unmergeable",
            "restart": "not-needed",
        }
        assert node.registries.read_text() == content
        assert not node.hosts_toml.exists()
        assert not (node.state / "ready").exists()
    assert node.registries.read_text() == content


def test_render_k3s_mode_and_auto_mode() -> None:
    k3s = cr.render(Registry.in_cluster(on="node-a", node_mirror="k3s"))
    agents = [o for o in k3s if o["kind"] == "DaemonSet"]
    assert [a["metadata"]["name"] for a in agents] == ["piceli-registry-mirror-k3s"]
    pod = agents[0]["spec"]["template"]["spec"]
    assert "affinity" not in pod  # every node
    assert {v["name"]: v.get("hostPath", {}).get("path") for v in pod["volumes"]} == {
        "k3s-etc": "/etc/rancher/k3s",
        "k3s-containerd": "/var/lib/rancher/k3s/agent/etc/containerd",
        "state": None,
    }
    assert pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    selector = agents[0]["spec"]["selector"]["matchLabels"]
    assert selector["app.kubernetes.io/component"] == "mirror-k3s"

    auto = cr.render(Registry.in_cluster(on="node-a"))
    by_name = {o["metadata"]["name"]: o for o in auto if o["kind"] == "DaemonSet"}
    assert set(by_name) == {"piceli-registry-mirror", "piceli-registry-mirror-k3s"}

    def terms(name: str) -> Any:
        spec = by_name[name]["spec"]["template"]["spec"]
        return spec["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]

    k3s_terms, containerd_terms = (
        terms("piceli-registry-mirror-k3s"),
        terms("piceli-registry-mirror"),
    )
    assert k3s_terms[0]["matchExpressions"] == [
        {"key": "piceli.io/runtime", "operator": "In", "values": ["k3s"]}
    ]
    assert containerd_terms[1]["matchExpressions"][1] == {
        "key": "node.kubernetes.io/instance-type",
        "operator": "NotIn",
        "values": ["k3s"],
    }
    containerd = cr.render(Registry.in_cluster(on="node-a", node_mirror="containerd"))
    only = [o for o in containerd if o["kind"] == "DaemonSet"]
    assert [o["metadata"]["name"] for o in only] == ["piceli-registry-mirror"]
    assert "affinity" not in only[0]["spec"]["template"]["spec"]


def test_node_mirror_is_validated_and_described() -> None:
    with pytest.raises(PipelineError):
        Registry.in_cluster(on="node-a", node_mirror="docker")
    assert "node_mirror" not in Registry.in_cluster(on="node-a").describe()
    assert (
        Registry.in_cluster(on="node-a", node_mirror="k3s").describe()["node_mirror"]
        == "k3s"
    )


def test_status_carries_each_agent_report() -> None:
    registry = Registry.in_cluster(on="node-a")

    def pod(name: str, node: str, component: str) -> dict[str, Any]:
        return {
            "metadata": {
                "name": name,
                "labels": {"app.kubernetes.io/component": component},
            },
            "spec": {"nodeName": node},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }

    body = cr.summarize(
        registry,
        deployment={"status": {"readyReplicas": 1}},
        service=None,
        claim=None,
        nodes=[{"metadata": {"name": "node-a"}}, {"metadata": {"name": "node-b"}}],
        pods=[
            pod("reg", "node-a", "registry"),
            pod("m-a", "node-a", "mirror-k3s"),
            pod("m-b", "node-b", "mirror"),
        ],
        reports={"node-a": {"runtime": "k3s", "file": "written", "restart": "needed"}},
    )
    assert body["state"] == "ready"
    assert body["mirrors"] == [
        {
            "node": "node-a",
            "mirror": "ready",
            "runtime": "k3s",
            "file": "written",
            "restart": "needed",
        },
        {"node": "node-b", "mirror": "ready"},
    ]


def test_parse_report_takes_the_last_well_formed_line() -> None:
    log = (
        "starting\n"
        'piceli-mirror: {"runtime":"k3s","file":"written","restart":"needed"}\n'
        'piceli-mirror: {"runtime":"k3s","file":"unchanged","restart":"not-needed"}\n'
    )
    assert cr.parse_report(log) == {
        "runtime": "k3s",
        "file": "unchanged",
        "restart": "not-needed",
    }
    assert cr.parse_report("piceli-mirror: {not json") is None
    assert cr.parse_report('piceli-mirror: {"runtime":"K3S; rm"}') is None
    assert cr.parse_report("") is None
    assert json.loads(json.dumps(cr.parse_report(log)))
