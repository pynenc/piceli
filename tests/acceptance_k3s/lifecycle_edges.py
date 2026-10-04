"""Stages 25-27 of the k3s lifecycle acceptance: two edge clusters (0.15).

Mixed into :class:`lifecycle.Lifecycle` (its home cluster, commands,
``status``, the candidate's controller after stage 1). Two more disposable
k3d clusters, one node each (``<name>-ea``, ``<name>-eb``), are the edge;
both are deleted with the home cluster, whatever the outcome.

25. Add the edge clusters as the note says: ``piceli login``, ``piceli
    cluster init`` (each installs its own registry), ``piceli secrets
    cluster`` (the controller reaches them at ``host.k3d.internal``), the
    composition's ``edge`` environment on both with ``Rollout(order=["edge-a",
    "edge-*"])``, ``gitops enable``. Its revision deploys to ``edge-a`` first,
    then ``edge-b``; each cluster's pods run images its own registry holds
    (copied by digest through its API server); no credential is printed.
26. ``edge-b`` unreachable mid-rollout (its API load balancer stopped): a
    push deploys to main and to ``edge-a``; ``edge-b`` is ``unreachable``
    (``cluster-unreachable``, its last contact) in ``gitops status``; when
    it answers again it converges on the same revision.
27. ``edge-b`` removed from the environment: the controller deletes the
    app's objects there; ``edge-a`` and main keep running.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from lifecycle_support import Result, StageFailed, check, log, wait_for

#: The edge clusters: (k3d name suffix, cluster name, attribute in infra.py).
EDGES = (("ea", "edge-a", "edge_a"), ("eb", "edge-b", "edge_b"))
EDGE_NAMESPACE = "lc-edge"
EDGE_INFRA = """

# --- 0.15: two edge clusters; a canary first (written by the acceptance) ----
from lifecycle_site import EDGE_A_API, EDGE_A_NODE, EDGE_B_API, EDGE_B_NODE  # noqa: E402

from piceli import Registry  # noqa: E402
from piceli.envs import Placement, Rollout  # noqa: E402

edge_a = Cluster(
    "edge-a",
    api=EDGE_A_API,
    credentials="edge-a",
    nodes=[Node(EDGE_A_NODE, arch=ARCH, roles=["workloads", "registry"])],
    registry=Registry.in_cluster(on=EDGE_A_NODE, storage="1Gi", repository="lifecycle"),
)
edge_b = Cluster(
    "edge-b",
    api=EDGE_B_API,
    credentials="edge-b",
    nodes=[Node(EDGE_B_NODE, arch=ARCH, roles=["workloads", "registry"])],
    registry=Registry.in_cluster(on=EDGE_B_NODE, storage="1Gi", repository="lifecycle"),
)
EDGE_PLACEMENTS = [Placement(edge_a), Placement(edge_b)]
environments.append(
    Environment(
        "edge",
        namespace="lc-edge",
        pipeline=pipeline,
        stack=Stack("edge", workloads=["web", "cache"]),
        follow={infra: "main", web: "main", store: "main"},
        auto_approve=True,
        clusters=EDGE_PLACEMENTS,
        rollout=Rollout(order=["edge-a", "edge-*"]),
    )
)
"""


class EdgeStages:
    """Stages 25-27 (see the module docstring)."""

    args: Any
    proc: Any
    cluster: Any
    repos: Any
    values: dict[str, str]
    piceli: str
    scratch: Path
    log_since: str

    # ------------------------------------------------------------ edge clusters
    def _edge_name(self, suffix: str) -> str:
        return f"{self.args.name}-{suffix}"

    def _edge_kubeconfig(self, suffix: str) -> Path:
        return self.scratch / f"edge-{suffix}.kubeconfig"

    def edge_kubectl(self, suffix: str, *args: str, check_exit: Any = (0,)) -> Result:
        return self.proc.run(
            [
                self.cluster.kubectl_bin, "--kubeconfig", str(self._edge_kubeconfig(suffix)),
                "--context", f"k3d-{self._edge_name(suffix)}", *args,
            ],
            check_exit=check_exit, timeout=300, quiet=True,
        )  # fmt: skip

    def edge_get(self, suffix: str, *args: str) -> Any:
        found = self.edge_kubectl(suffix, "get", *args, "-o", "json", check_exit=None)
        return json.loads(found.stdout) if found.code == 0 else None

    def _create_edge(self, suffix: str) -> tuple[str, int]:
        """One single-node k3d cluster; ``(laptop API URL, port)``."""
        from lifecycle import free_port

        name = self._edge_name(suffix)
        port = free_port()
        self.proc.run([*self.cluster.k3d, "cluster", "delete", name],
                      check_exit=None, quiet=True, timeout=600)  # fmt: skip
        self.proc.run(
            [
                *self.cluster.k3d, "cluster", "create", name, "--agents", "0",
                "--api-port", f"0.0.0.0:{port}",
                "--kubeconfig-update-default=false",
                "--kubeconfig-switch-context=false",
                "--k3s-arg", "--disable=traefik@server:0",
                "--k3s-arg", "--disable=metrics-server@server:0",
                # The controller reaches it through the host, by this name.
                "--k3s-arg", "--tls-san=host.k3d.internal@server:0",
                "--k3s-arg", "--kubelet-arg=eviction-hard=imagefs.available<1%,nodefs.available<1%@all",
                "--wait", "--timeout", "600s",
            ],
            timeout=900,
        )  # fmt: skip
        text = self.proc.run([*self.cluster.k3d, "kubeconfig", "get", name],
                             quiet=True).stdout  # fmt: skip
        text = re.sub(r"https://(0\.0\.0\.0|localhost):", "https://127.0.0.1:", text)
        path = self._edge_kubeconfig(suffix)
        path.write_text(text)
        path.chmod(0o600)
        return f"https://127.0.0.1:{port}", port

    def edge_cleanup(self) -> None:
        for suffix, _, _ in EDGES:
            self.proc.run(
                [*self.cluster.k3d, "cluster", "delete", self._edge_name(suffix)],
                timeout=600, check_exit=None, quiet=True,
            )  # fmt: skip
            self._edge_kubeconfig(suffix).unlink(missing_ok=True)

    def _lb(self, suffix: str) -> str:
        return f"k3d-{self._edge_name(suffix)}-serverlb"

    # ------------------------------------------------------------ status
    def edge_clusters(self) -> dict[str, Any]:
        status = self.status()  # type: ignore[attr-defined]
        return dict(
            ((status.get("envs") or {}).get("edge") or {}).get("clusters") or {}
        )

    def wait_edge(self, done: Any, *, timeout: float, what: str) -> dict[str, Any]:
        last: dict[str, Any] = {}

        def probe() -> dict[str, Any] | None:
            nonlocal last
            last = self.edge_clusters()
            return last if done(last) else None

        try:
            return dict(wait_for(f"edge: {what}", probe, timeout=timeout, interval=5))
        except StageFailed as error:
            brief = {
                name: {k: entry.get(k) for k in ("state", "reason", "last_contact")}
                for name, entry in last.items()
            }
            raise StageFailed(
                f"{error}; clusters: {json.dumps(brief)[:1500]}"
            ) from None

    @staticmethod
    def _at(entry: dict[str, Any], sources: dict[str, str]) -> bool:
        revision = entry.get("deployed_revision") or {}
        return entry.get("state") == "deployed" and all(
            revision.get(k) == v for k, v in sources.items()
        )

    def _edge_pods_ready(self, suffix: str) -> list[str]:
        body = self.edge_get(suffix, "pods", "-n", EDGE_NAMESPACE) or {"items": []}
        ready = []
        for pod in body["items"]:
            statuses = (pod.get("status") or {}).get("containerStatuses") or []
            if statuses and all(item.get("ready") for item in statuses):
                ready.append(str(pod["metadata"]["name"]))
        return ready

    # ------------------------------------------------------------ stages
    def stage_25_edge_add(self) -> None:
        site = self.repos.read("infra", "lifecycle_site.py")
        extra: list[str] = []
        ports: dict[str, int] = {}
        for suffix, name, _attr in EDGES:
            api, port = self._create_edge(suffix)
            ports[suffix] = port
            node = f"k3d-{self._edge_name(suffix)}-server-0"
            key = name.replace("-", "_").upper()
            extra += [f"{key}_API = {api!r}", f"{key}_NODE = {node!r}"]
        (self.repos.work("infra") / "lifecycle_site.py").write_text(
            site.rstrip("\n") + "\n" + "\n".join(extra) + "\n"
        )
        infra_py = self.repos.read("infra", "infra.py")
        (self.repos.work("infra") / "infra.py").write_text(infra_py + EDGE_INFRA)
        outputs: list[Result] = []
        for suffix, name, attr in EDGES:
            self.values.update(
                edge=name,
                edge_attr=attr,
                edge_kubeconfig=str(self._edge_kubeconfig(suffix)),
                edge_context=f"k3d-{self._edge_name(suffix)}",
                edge_server=f"https://host.k3d.internal:{ports[suffix]}",
            )
            done = self.run_group("edge-add")  # type: ignore[attr-defined]
            outputs += list(done.values())
            secret = json.loads(done["edge-secrets"].stdout)
            check(
                secret["secret"]["name"] == f"piceli-cluster-{name}",
                f"secret {secret}",
            )
        kubeconfigs = [
            self._edge_kubeconfig(suffix).read_text() for suffix, _, _ in EDGES
        ]
        for result in outputs:
            text = result.stdout + result.stderr
            for document in kubeconfigs:
                for value in re.findall(
                    r"(?:client-key-data|client-certificate-data|token): (\S+)",
                    document,
                ):
                    check(value[:24] not in text, "a credential was printed")
        # The controller's new config (the Role reading both Secrets), then
        # the composition with the edge environment.
        self.run_group("upgrade")  # type: ignore[attr-defined]
        self.log_since = self.mark()  # type: ignore[attr-defined]
        sha = self.repos.commit(
            "infra",
            "deploy to two edge clusters, a canary first",
            {
                "lifecycle_site.py": (
                    self.repos.work("infra") / "lifecycle_site.py"
                ).read_text(),
                "infra.py": (self.repos.work("infra") / "infra.py").read_text(),
            },
        )
        clusters = self.wait_edge(
            lambda found: all(
                self._at(found.get(n) or {}, {"infra": sha}) for _, n, _ in EDGES
            ),
            timeout=1500,
            what="edge-a and edge-b deployed",
        )
        log(
            "edge clusters: "
            + json.dumps({k: v.get("state") for k, v in clusters.items()})
        )
        check(
            clusters["edge-a"].get("wave") == 0,
            f"edge-a wave {clusters['edge-a'].get('wave')}",
        )
        check(
            clusters["edge-b"].get("wave") == 1,
            f"edge-b wave {clusters['edge-b'].get('wave')}",
        )
        lines = self.controller_log(self.log_since).splitlines()  # type: ignore[attr-defined]
        first_a = next(
            (i for i, line in enumerate(lines) if "edge@edge-a: deployed" in line), None
        )
        first_b = next(
            (i for i, line in enumerate(lines) if "edge@edge-b: deployed" in line), None
        )
        check(first_a is not None and first_b is not None, "no deploy line per cluster")
        check(first_a < first_b, "edge-b deployed before the canary edge-a")  # type: ignore[operator]
        for suffix, name, _ in EDGES:
            ready = self._edge_pods_ready(suffix)
            check(
                any(pod.startswith("web-") for pod in ready)
                and any(pod.startswith("cache-") for pod in ready),
                f"{name}: pods ready {ready}",
            )
            checks = clusters[name].get("checks") or {}
            check(checks.get("state") == "passed", f"{name} checks {checks}")
        status = self.status()  # type: ignore[attr-defined]
        check(
            status.get("clusters", {}).get("edge-b", {}).get("reachable") is True,
            f"clusters {status.get('clusters')}",
        )
        self.values["edge_sha"] = sha

    def stage_26_edge_unreachable(self) -> None:
        before = self.edge_clusters()
        contact = (before.get("edge-b") or {}).get("last_contact")
        check(contact, "edge-b has no last contact")
        self.proc.run(["docker", "stop", self._lb("eb")], timeout=120)
        stopped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            readme = self.repos.work("web") / "README.md"
            text = (readme.read_text() if readme.exists() else "") + "\nEdge rollout.\n"
            sha = self.repos.commit("web", "web: edge rollout", {"README.md": text})
            self.wait_env("main", self.deployed("main", {"web": sha}),  # type: ignore[attr-defined]
                          timeout=1200, what="web change")  # fmt: skip
            clusters = self.wait_edge(
                lambda found: (
                    self._at(found.get("edge-a") or {}, {"web": sha})
                    and (found.get("edge-b") or {}).get("state") == "unreachable"
                ),
                timeout=900,
                what="edge-a deployed, edge-b unreachable",
            )
            b = clusters["edge-b"]
            check(
                b.get("reason") == "cluster-unreachable",
                f"edge-b reason {b.get('reason')}",
            )
            check(
                bool(b.get("last_contact")) and b["last_contact"] <= stopped_at,
                f"edge-b last contact {b.get('last_contact')} after {stopped_at}",
            )
            human = self.run_step("gitops-status")  # type: ignore[attr-defined]
            check(
                "edge-b: unreachable" in human.stderr,
                "gitops status prints no unreachable edge-b",
            )
        finally:
            self.proc.run(["docker", "start", self._lb("eb")], timeout=120)
        clusters = self.wait_edge(
            lambda found: self._at(found.get("edge-b") or {}, {"web": sha}),
            timeout=900,
            what="edge-b converges",
        )
        check(
            (clusters["edge-b"].get("last_contact") or "") > (contact or ""),
            "edge-b last contact did not move",
        )

    def stage_27_edge_remove(self) -> None:
        infra_py = self.repos.read("infra", "infra.py").replace(
            "EDGE_PLACEMENTS = [Placement(edge_a), Placement(edge_b)]",
            "EDGE_PLACEMENTS = [Placement(edge_a)]",
        )
        self.repos.commit(
            "infra", "edge-b leaves the edge environment", {"infra.py": infra_py}
        )

        def removed(_: dict[str, Any]) -> bool:
            status = self.status()  # type: ignore[attr-defined]
            clusters = ((status.get("envs") or {}).get("edge") or {}).get(
                "clusters"
            ) or {}
            return "edge-b" not in clusters and any(
                item.get("cluster") == "edge-b" for item in status.get("removals") or []
            )

        wait_for("edge-b removed", lambda: removed({}) or None, timeout=600, interval=5)
        deployments = self.edge_get("eb", "deployments", "-n", EDGE_NAMESPACE) or {
            "items": []
        }
        check(
            not deployments["items"],
            f"edge-b keeps {[d['metadata']['name'] for d in deployments['items']]}",
        )
        check(
            any(pod.startswith("web-") for pod in self._edge_pods_ready("ea")),
            "edge-a lost its web pod",
        )
        # The same infra push re-plans main (pending for a poll or two).
        main = self.wait_env(  # type: ignore[attr-defined]
            "main",
            lambda r: r.get("state") == "deployed",
            timeout=600,
            what="main deployed",
        )
        check(main.get("state") == "deployed", f"main {main.get('state')}")
