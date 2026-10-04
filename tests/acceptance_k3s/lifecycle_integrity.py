"""Stage 29 of the k3s lifecycle acceptance: a corrupted controller image (0.15.1).

29. One file of the controller's unpacked image is altered on its node (what
    a failing disk or RAM did on a real cluster: a ``.pyc`` with different
    bytes). The controller pod is restarted: its self-check refuses to start
    with "controller image files corrupted on this node; …", it
    crash-loops, and ``piceli cluster status``, ``piceli gitops status`` and
    the in-cluster UI report the controller ``down`` with that message, the
    status document marked as written before it stopped. Then the original
    bytes are written back (always, also when the stage fails) and the
    controller runs again.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from lifecycle_support import UiError, check, log, wait_for

#: A file a failing node altered on a real cluster (a ``.pyc`` shipped in the image).
TARGET = "kubernetes/client/api/__pycache__/networking_v1beta1_api.cpython-313.pyc"
SNAPSHOTS = "/var/lib/rancher/k3s/agent/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots"
MANIFEST = "/usr/local/share/piceli/files.sha256"
MESSAGE = "controller image files corrupted on this node"
BACKUP = "/var/tmp/piceli-lifecycle-corrupt"


class IntegrityStages:
    """Stage 29; mixed into ``Lifecycle``."""

    def _k(self, *args: str, **kwargs: Any) -> Any:
        return self.kubectl(*args, **kwargs)  # type: ignore[attr-defined]

    def _controller_pod(self) -> dict[str, Any]:
        found = self._k(
            "-n", "piceli-system", "get", "pods", "-l", "app.kubernetes.io/name=piceli-gitops",
            "-o", "json", quiet=True,
        )  # fmt: skip
        items = (json.loads(found.stdout) or {}).get("items") or []
        items.sort(key=lambda item: item["metadata"].get("creationTimestamp") or "")
        check(items, "no controller pod")
        return dict(items[-1])

    def _node_sh(self, node: str, script: str, *, check_exit: Any = (0,)) -> Any:
        return self.proc.run(  # type: ignore[attr-defined]
            ["docker", "exec", node, "sh", "-c", script],
            timeout=300,
            quiet=True,
            check_exit=check_exit,
        )

    def _cluster_live(self) -> dict[str, Any]:
        body = self.run_step("cluster-status").json()  # type: ignore[attr-defined]
        return dict(((body or {}).get("controller") or {}).get("live") or {})

    def stage_29_corrupt_controller_image(self) -> None:
        pod = self._controller_pod()
        node = pod["spec"]["nodeName"]
        name = pod["metadata"]["name"]
        manifest = self._k(
            "-n", "piceli-system", "exec", name, "--", "cat", MANIFEST, quiet=True,
        ).stdout  # fmt: skip
        line = next(
            (item for item in manifest.splitlines() if item.endswith("/" + TARGET)),
            None,
        )
        check(line is not None, f"{TARGET} is not in the image's manifest")
        assert line is not None
        digest = line.split("  ", 1)[0]
        # Every unpacked copy of that file with the image's bytes, on this node.
        copies = [
            path
            for path in self._node_sh(
                node,
                f"find {SNAPSHOTS} -path {shlex.quote('*/' + TARGET)} -type f "
                f"-exec sha256sum {{}} + | awk '$1 == \"{digest}\" {{print $2}}'",
            ).stdout.split()
            if path
        ]
        check(copies, f"no unpacked copy of {TARGET} on {node}")
        log(f"altering {len(copies)} unpacked copy(ies) of {TARGET} on {node}")
        script = [f"mkdir -p {BACKUP}"]
        for index, path in enumerate(copies):
            script.append(f"cp -p {shlex.quote(path)} {BACKUP}/{index}")
            # Same length, different bytes: like a flipped block on disk.
            script.append(
                f"printf 'piceli-corrupt' | dd of={shlex.quote(path)} bs=1 seek=64 conv=notrunc status=none"
            )
        try:
            self._node_sh(node, " && ".join(script))
            self._k(
                "-n", "piceli-system", "delete", "pod", name, "--wait=false", quiet=True
            )  # type: ignore[attr-defined]
            live = wait_for(
                "cluster status: the controller down with the corruption message",
                lambda: (
                    (found := self._cluster_live()).get("state") == "down"
                    and MESSAGE in (found.get("message") or "")
                    and found
                ),
                timeout=300,
                interval=5,
            )
            log(
                f"cluster status: {live.get('message')} ({(live.get('pod') or {}).get('restarts')} restarts)"
            )
            status = self.status()  # type: ignore[attr-defined]
            gitops_live = status.get("controller_live") or {}
            check(
                gitops_live.get("state") == "down", f"gitops status live {gitops_live}"
            )
            check(
                gitops_live.get("status_is_stale") is True,
                "gitops status does not mark its data stale",
            )
            with self.ui_session() as (client, _url):  # type: ignore[attr-defined]
                composition = client.get("/api/v1/composition")
                check(not isinstance(composition, UiError), "the UI does not answer")
                controller = composition.get("controller") or {}
                check(controller.get("state") == "down", f"UI controller {controller}")
                check(
                    MESSAGE in (controller.get("message") or ""),
                    f"UI message {controller.get('message')}",
                )
                check(
                    controller.get("status_is_stale") is True,
                    "the UI does not mark the status stale",
                )
        finally:
            restore = [
                f"cp -p {BACKUP}/{index} {shlex.quote(path)}"
                for index, path in enumerate(copies)
            ]
            self._node_sh(
                node, " && ".join([*restore, f"rm -rf {BACKUP}"]), check_exit=None
            )
            for item in [self._controller_pod()]:
                self._k(
                    "-n", "piceli-system", "delete", "pod", item["metadata"]["name"], "--wait=false",
                    quiet=True, check_exit=None,
                )  # fmt: skip
        wait_for(
            "the controller running again after the files are restored",
            lambda: self._cluster_live().get("state") == "running",
            timeout=300,
            interval=5,
        )
        log(
            "stage 29: corrupted image reported down with its message; restored and running"
        )
