"""What exists of an :class:`~piceli.infra.Infrastructure`: servers, addresses, clusters, cost.

Read from the records the last commands left in the state directory
(``inventory.json``, ``installs.json``, ``registrations.json``): no
OpenTofu run, no credential, no network, no lock. The UI's Machines page
shows the same document (``piceli.infra.status.v1``).

Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from piceli.infra.machines.model import Firewall, Infrastructure

__all__ = ["STATUS_SCHEMA", "summarize"]

STATUS_SCHEMA = "piceli.infra.status.v1"


def summarize(infra: Infrastructure, directory: Path | None = None) -> dict[str, Any]:
    """The status document of ``infra`` (``state``: not-applied, applied or partial)."""
    from piceli.infra.machines.install import INSTALLS
    from piceli.infra.machines.plan import INVENTORY
    from piceli.infra.machines.register import REGISTRATIONS
    from piceli.infra.machines.state import read_record, state_dir

    if directory is None:
        directory = state_dir(infra.name, infra.state_dir, create=False)
    inventory = read_record(directory, INVENTORY) if directory.is_dir() else {}
    installs = read_record(directory, INSTALLS) if directory.is_dir() else {}
    registrations = read_record(directory, REGISTRATIONS) if directory.is_dir() else {}
    live = inventory.get("servers") or {}
    estimate = inventory.get("estimate") or None
    costs = {
        item.get("server"): item
        for item in (estimate or {}).get("items") or ()
        if isinstance(item, dict) and item.get("kind") == "server"
    }
    servers: list[dict[str, Any]] = []
    for server in infra.servers:
        found = live.get(server.name) if isinstance(live, dict) else None
        found = found if isinstance(found, dict) else None
        registration = registrations.get(server.name)
        cluster = None
        if server.cluster is not None:
            cluster = {
                "name": server.cluster.name,
                "api": server.cluster.api,
                "profile": server.cluster.credentials,
                "registered": isinstance(registration, dict),
                "registered_at": registration.get("at")
                if isinstance(registration, dict)
                else None,
                "nodes": registration.get("nodes")
                if isinstance(registration, dict)
                else None,
            }
        install = installs.get(server.name)
        cost = costs.get(server.name)
        servers.append(
            {
                "name": server.name,
                "provider": server.provider.name,
                "type": server.type,
                "location": server.location,
                "image": server.image,
                "state": "created" if found else "not-created",
                "id": found.get("id") if found else None,
                "status": found.get("status") if found else None,
                "ipv4": found.get("ipv4") if found else None,
                "ipv6": found.get("ipv6") if found else None,
                "fixed_ipv4": server.ipv4 is not True and server.ipv4 is not False,
                "firewall": server.firewall.name
                if isinstance(server.firewall, Firewall)
                else None,
                "host_keys": (
                    (registration.get("host_keys") or [])
                    if isinstance(registration, dict)
                    else []
                ),
                "install": (
                    {"declared": server.install is not None, **install}
                    if isinstance(install, dict)
                    else {"declared": server.install is not None, "state": None}
                ),
                "cluster": cluster,
                "monthly_net": cost.get("monthly_net")
                if isinstance(cost, dict)
                else None,
            }
        )
    created = sum(1 for item in servers if item["state"] == "created")
    if not inventory.get("resources"):
        state = "not-applied"
    elif created == len(servers) and inventory.get("config_digest"):
        state = "applied"
    else:
        state = "partial"
    declared = {record.key: record.describe() for record in infra.records}
    resources = [
        item for item in inventory.get("resources") or () if isinstance(item, dict)
    ]
    records = []
    for key, body in declared.items():
        present = any(
            item.get("kind") == "record" and item.get("name") == key
            for item in resources
        )
        records.append(
            {**body, "key": key, "state": "created" if present else "not-created"}
        )
    return {
        "schema": STATUS_SCHEMA,
        "name": infra.name,
        "state": state,
        "state_dir": str(directory),
        "updated_at": inventory.get("updated_at"),
        "tofu": inventory.get("tofu"),
        "backend": infra.backend.describe() if infra.backend is not None else None,
        "servers": servers,
        "records": records,
        "resources": len(resources),
        "estimate": (
            {k: estimate.get(k) for k in ("currency", "monthly_net", "complete")}
            if isinstance(estimate, dict)
            else None
        ),
    }
