"""Stage 25 of the k3s lifecycle acceptance: machines provisioned with OpenTofu.

Mixed into :class:`lifecycle.Lifecycle` (it uses its cluster, process runner
and commands). From 0.15.0; runs alone with ``--stages 25`` (a light setup:
the candidate's CLI and the k3d cluster only) or last in the full run.

25. ``machines.py`` declares one server at the fake provider (OpenTofu's
    built-in ``terraform_data``: a real ``tofu plan``/``apply`` and a real,
    encrypted state, nothing created anywhere), a fixed IP, a firewall, a
    DNS record, an install hook (a no-op script) and the cluster the server
    becomes (the k3d cluster's API). The commands of the ``infra`` group, as
    the note gives them: plan, approve, status, install (preview, approve),
    register with the k3d kubeconfig (preview, approve), ``cluster status``
    through the new profile. Then: a re-plan is unchanged; the state holds
    no address in the clear; nothing secret was printed; the destroy plan
    lists exactly what apply created; after it the k3d cluster still runs.

OpenTofu: ``$PICELI_TOFU``, ``tofu`` on PATH, or ``nix shell
nixpkgs#opentofu``. Everything lives in the run's scratch directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from lifecycle_support import StageFailed, check, log

MACHINES = '''\
"""Machines of the lifecycle acceptance (written by the harness)."""

from pathlib import Path

from piceli.infra import Cluster, DnsRecord, Hook, Infrastructure, PrimaryIp, Rule, Server
from piceli.testing.infra import FakeProvider

HERE = Path(__file__).parent
fake = FakeProvider(addresses={{"edge-1": "127.0.0.1"}}, monthly={{"t1": 3.79}})
# The cluster the server's k3s becomes: here the disposable k3d cluster.
edge = Cluster("lc-edge", api={api!r}, credentials="lc-edge")
edge_1 = Server(
    "edge-1",
    provider=fake,
    type="t1",
    image="any",
    ipv4=PrimaryIp("edge-1-v4"),
    firewall=[Rule.tcp(443, name="https"), Rule.udp(3478, name="turn")],
    install=Hook(["sh", str(HERE / "install-os.sh"), "{{name}}", "{{ipv4}}"]),
    cluster=edge,
)
infra = Infrastructure(
    "lc-edge",
    servers=[edge_1],
    records=[DnsRecord("example.com", "edge", "A", server=edge_1)],
    state_key="machines-state",
    state_dir={state!r},
)
'''

INSTALL = """\
#!/bin/sh
# The install hook of the acceptance: records that it ran, installs nothing.
echo "install-os $1 $2" > "$(dirname "$0")/installed"
"""

CREATED = [
    "terraform_data.firewall-edge-1",
    "terraform_data.ip-edge-1-v4",
    "terraform_data.record-example-com-edge-a",
    "terraform_data.server-edge-1",
]


def find_tofu() -> str:
    """A real OpenTofu: ``$PICELI_TOFU``, PATH, else one from nixpkgs."""
    import os

    found = os.environ.get("PICELI_TOFU") or shutil.which("tofu")
    if found:
        return found
    if shutil.which("nix"):
        done = subprocess.run(
            ["nix", "shell", "nixpkgs#opentofu", "-c", "sh", "-c", "command -v tofu"],
            capture_output=True,
            text=True,
            timeout=900,
        )
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    raise StageFailed("OpenTofu not found (PICELI_TOFU, tofu on PATH, or nix)")


class InfraStages:
    """Stage 25 (see the module docstring); mixed into ``Lifecycle``."""

    def stage_25_infra(self) -> None:
        scratch: Path = self.scratch  # type: ignore[attr-defined]
        work = scratch / "machines"
        work.mkdir(exist_ok=True)
        state = scratch / "machines-state"
        api = self.cluster.api_server()  # type: ignore[attr-defined]
        (work / "machines.py").write_text(MACHINES.format(api=api, state=str(state)))
        (work / "install-os.sh").write_text(INSTALL)
        env = self.proc.env  # type: ignore[attr-defined]
        env["PICELI_TOFU"] = find_tofu()
        env["PICELI_CREDENTIALS_DIR"] = str(scratch / "credentials")
        values = {
            **self.values,  # type: ignore[attr-defined]
            "piceli": self.piceli,  # type: ignore[attr-defined]
            "kubeconfig": str(self.cluster.kubeconfig),  # type: ignore[attr-defined]
            "context": self.cluster.context,  # type: ignore[attr-defined]
        }
        commands = self.commands  # type: ignore[attr-defined]
        done = commands.run_group("infra", self.proc, values, {}, cwd=work)  # type: ignore[attr-defined]
        outputs = "\n".join(r.stdout + r.stderr for r in done.values())

        plan = done["infra-plan"].json() or {}
        check(
            [c["address"] for c in plan.get("changes") or []] == CREATED,
            f"the plan creates {[c.get('address') for c in plan.get('changes') or []]}",
        )
        check(
            (plan.get("estimate") or {}).get("monthly_net") == 3.79,
            f"monthly estimate {plan.get('estimate')}",
        )
        applied = done["infra-apply"].json() or {}
        check(applied.get("state") == "applied", f"apply: {applied.get('state')}")
        check(
            (applied.get("servers") or {}).get("edge-1", {}).get("ipv4") == "127.0.0.1",
            f"servers after apply: {applied.get('servers')}",
        )
        check(
            (work / "installed").read_text().strip() == "install-os edge-1 127.0.0.1",
            "the install hook did not run with the server's values",
        )
        registered = done["infra-register-approve"].json() or {}
        check(
            registered.get("state") == "registered"
            and (registered.get("outcome") or {}).get("profile") == "lc-edge",
            f"register: {registered.get('state')} {registered.get('reason')}",
        )
        cluster = done["infra-cluster-status"].json() or {}
        check(
            cluster.get("cluster") == "lc-edge",
            f"cluster status through the new profile: {sorted(cluster)[:8]}",
        )
        status = done["infra-status"].json() or {}
        server = (status.get("servers") or [{}])[0]
        check(
            status.get("state") == "applied"
            and server.get("install", {}).get("state") == "installed"
            and (server.get("cluster") or {}).get("registered") is True,
            f"infra status: {status.get('state')} {server}",
        )

        # Idempotent: a second plan is unchanged.
        again = commands.run_step("infra-plan", self.proc, values, {}, cwd=work)  # type: ignore[attr-defined]
        check(
            (again.json() or {}).get("state") == "unchanged",
            f"re-plan: {(again.json() or {}).get('state')}",
        )
        # Encrypted at rest; no secret printed.
        text = (state / "terraform.tfstate").read_text()
        check(
            "encrypted_data" in text and "127.0.0.1" not in text, "state not encrypted"
        )
        key = json.loads((scratch / "credentials" / "machines-state.json").read_text())[
            "value"
        ]
        check(key not in outputs, "the state passphrase was printed")
        kubeconfig = Path(str(self.cluster.kubeconfig)).read_text()  # type: ignore[attr-defined]
        for line in kubeconfig.splitlines():
            if "client-key-data" in line:
                secret = line.split(":", 1)[1].strip()
                check(secret not in outputs, "the client key was printed")

        # Destroy: exactly what apply created; the cluster is untouched.
        gone = commands.run_group("infra-destroy", self.proc, values, {}, cwd=work)  # type: ignore[attr-defined]
        planned = gone["infra-destroy-plan"].json() or {}
        check(
            [c["address"] for c in planned.get("changes") or []] == CREATED
            and all(c["actions"] == ["delete"] for c in planned.get("changes") or []),
            f"destroy plans {planned.get('changes')}",
        )
        check(planned.get("foreign") == [], f"foreign: {planned.get('foreign')}")
        after = gone["infra-status"].json() or {}
        check(
            after.get("state") == "not-applied", f"after destroy: {after.get('state')}"
        )
        destroyed = gone["infra-destroy-approve"].json() or {}
        check(
            destroyed.get("profiles_left") == ["lc-edge"],
            f"profiles left: {destroyed.get('profiles_left')}",
        )
        nodes: Any = self.cluster.get("nodes")  # type: ignore[attr-defined]
        check(bool(nodes and nodes.get("items")), "the k3d cluster no longer answers")
        log(
            "stage 25: planned, applied, installed, registered and destroyed "
            f"{len(CREATED)} fake resources; k3d cluster untouched"
        )
