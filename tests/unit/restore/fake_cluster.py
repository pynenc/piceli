"""An in-memory stand-in for :class:`piceli.restore.cluster.RestoreCluster`.

Claims are local directories; the helper's commands run in a local shell
with the claim mount replaced by the claim's directory, so the archive and
digest commands are the real ones.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from piceli.restore.cluster import MOUNT
from piceli.restore.model import RestorePointError


class FakeCluster:
    def __init__(
        self, root: Path, workloads: list[dict[str, Any]], namespace: str = "shop"
    ):
        self.root = root
        self.namespace = namespace
        self.live = {f"{w['kind']}/{w['metadata']['name']}": w for w in workloads}
        self.calls: list[str] = []
        self.fail: dict[str, int] = {}
        self.claim_names: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self._pod_claim: dict[str, str] = {}

    def claim_dir(self, claim: str) -> Path:
        path = self.root / "claims" / claim
        path.mkdir(parents=True, exist_ok=True)
        return path

    # reads
    def workload(self, kind: str, name: str) -> dict[str, Any] | None:
        return self.live.get(f"{kind}/{name}")

    def workloads(self) -> list[dict[str, Any]]:
        return list(self.live.values())

    def claims(self) -> list[dict[str, Any]]:
        return [
            {"metadata": {"name": name}, "status": {"phase": "Bound"}}
            for name in self.claim_names
        ]

    # writes
    def scale(self, kind: str, name: str, replicas: int) -> None:
        self.calls.append(f"scale {kind}/{name} {replicas}")
        self.live[f"{kind}/{name}"]["spec"]["replicas"] = replicas

    def run_hook(self, writer: Mapping[str, Any], hook: Mapping[str, Any]) -> None:
        self.calls.append(f"hook {writer['workload']} {hook['type']}")

    def wait_stopped(
        self,
        writers: Sequence[Mapping[str, Any]],
        claims: Sequence[str],
        seconds: float,
    ) -> None:
        self.calls.append("wait " + ",".join(sorted(claims)))

    @contextmanager
    def helper(
        self, name: str, manifest: Mapping[str, Any], seconds: float
    ) -> Iterator[str]:
        volume = manifest["spec"]["template"]["spec"]["volumes"][0]
        self.jobs[name] = dict(manifest)
        self.calls.append(
            f"job {name} ro={volume['persistentVolumeClaim']['readOnly']}"
        )
        pod = f"{name}-pod"
        self._pod_claim[pod] = volume["persistentVolumeClaim"]["claimName"]
        try:
            yield pod
        finally:
            self.calls.append(f"delete {name}")

    def exec(
        self,
        pod: str,
        command: list[str],
        *,
        container: str | None = None,
        timeout: float,
        stdout: Callable[[bytes], None] | None = None,
        stdin: Iterator[bytes] | None = None,
        code: str = "restore-point-copy-failed",
    ) -> int:
        script = command[-1]
        for marker, exit_code in self.fail.items():
            if marker in script:
                return exit_code
        directory = self.claim_dir(self._pod_claim[pod])
        data = b"".join(stdin) if stdin is not None else None
        result = subprocess.run(
            [command[0], command[1], script.replace(MOUNT, str(directory))],
            input=data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if stdout is not None:
            stdout(result.stdout)
        if (
            result.returncode not in (0,)
            and "tar -x" not in script
            and "tar -c" not in script
        ):
            raise RestorePointError(code, "fake exec failed", failed=True)
        return result.returncode
