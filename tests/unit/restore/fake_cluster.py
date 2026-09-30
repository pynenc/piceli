"""An in-memory stand-in for :class:`piceli.restore.cluster.RestoreCluster`.

Claims are local directories; the helper's commands run in a local shell
with the claim mount replaced by the claim's directory, so the archive and
digest commands are the real ones.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from piceli.pipeline.prerollout_cluster import Outcome
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
        self.scratch: dict[str, dict[str, Any]] = {}
        self.undeletable: set[str] = set()
        self.storage_class: str | None = "standard"
        self.affinity: dict[str, Any] | None = None
        #: Live claims by name (claim growth tests), with their spec/status.
        self.pvcs: dict[str, dict[str, Any]] = {}
        self.poll_seconds = 0.0
        #: How many polls a claim takes to report its new capacity.
        self.resize_polls = 1

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

    # scratch claims (restore --to-new-claim)
    def claim(self, name: str) -> dict[str, Any] | None:
        if name in self.pvcs:
            live = self.pvcs[name]
            pending = live.get("_polls", 0)
            if pending:
                live["_polls"] = pending - 1
                if pending == 1:
                    live.setdefault("status", {})["capacity"] = {
                        "storage": live["spec"]["resources"]["requests"]["storage"]
                    }
            return {key: value for key, value in live.items() if key != "_polls"}
        if name in self.scratch:
            return self.scratch[name]
        if name not in self.claim_names:
            return None
        return {
            "metadata": {"name": name},
            "spec": {
                "storageClassName": self.storage_class,
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "1Gi"}},
                "volumeName": f"pv-{name}",
            },
            "status": {"phase": "Bound"},
        }

    def volume_affinity(self, volume: str) -> dict[str, Any] | None:
        self.calls.append(f"read {volume}")
        return self.affinity

    def create_claim(self, manifest: Mapping[str, Any]) -> None:
        name = manifest["metadata"]["name"]
        self.calls.append(f"create claim {name}")
        if self.pvcs:
            self.pvcs[name] = {**dict(manifest), "status": {"phase": "Bound"}}
            return
        self.scratch[name] = dict(manifest)

    # claim growth
    def expandable(self, storage_class: str | None) -> bool:
        return storage_class == "expandable"

    def expand_claim(self, name: str, size: str) -> None:
        self.calls.append(f"expand {name} {size}")
        live = self.pvcs[name]
        live["spec"]["resources"]["requests"]["storage"] = size
        live["_polls"] = self.resize_polls

    def annotate_claim(
        self,
        name: str,
        *,
        labels: Mapping[str, str],
        annotations: Mapping[str, str],
    ) -> None:
        self.calls.append(f"annotate {name}")
        metadata = self.pvcs[name].setdefault("metadata", {})
        metadata.setdefault("labels", {}).update(labels)
        metadata.setdefault("annotations", {}).update(annotations)

    def delete_claim(self, name: str, seconds: float) -> bool:
        self.calls.append(f"delete claim {name}")
        if name in self.undeletable:
            return False
        self.scratch.pop(name, None)
        path = self.root / "claims" / name
        if path.exists():
            shutil.rmtree(path)
        return True


class FakeJobs:
    """Runs a verify Job's command locally, its claim mounts replaced by the
    claims' directories (``sh -c`` scripts see the restored files)."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster
        self.jobs: list[dict[str, Any]] = []
        self.raise_on_run: BaseException | None = None

    def run_job(self, job: Mapping[str, Any], *, redact: Any = ()) -> Outcome:
        self.jobs.append(dict(job))
        if self.raise_on_run is not None:
            raise self.raise_on_run
        spec = job["spec"]["template"]["spec"]
        (container,) = spec["containers"]
        claims = {
            volume["name"]: volume["persistentVolumeClaim"]["claimName"]
            for volume in spec.get("volumes") or ()
            if "persistentVolumeClaim" in volume
        }
        command = list(container["command"])
        for mount in container.get("volumeMounts") or ():
            if mount["name"] in claims:
                assert mount["readOnly"] is True
                local = str(self.cluster.claim_dir(claims[mount["name"]]))
                command = [part.replace(mount["mountPath"], local) for part in command]
        result = subprocess.run(command, capture_output=True, check=False, timeout=30)
        return Outcome(
            state="passed" if result.returncode == 0 else "failed",
            exit_code=result.returncode,
            log_tail=result.stdout.decode(errors="replace"),
            cleaned=True,
        )
