"""Plan, approve and apply (or destroy) an :class:`~piceli.infra.Infrastructure`.

One command holds the state directory's lock from start to end
(:func:`workspace`): it renders ``main.tf.json`` and the lock file, runs
``tofu init -lockfile=readonly`` (pinned providers, verified hashes), then
``tofu plan -json -out=FILE`` and ``tofu show -json FILE``.

The plan hash is ``sha256`` of the rendered configuration (with its lock
file), the operation (``apply`` or ``destroy``) and the plan JSON without
its timestamp. Approving runs a fresh plan and applies that plan file only
when its hash is the approved one (``infra-plan-changed`` otherwise), so
what runs is exactly what was reviewed, also after drift at the provider.

Ownership: Piceli only changes or deletes a resource it created. Before an
apply, the addresses the plan creates are added to the ledger
(``inventory.json`` ``created``); a plan that would update, replace or
delete a resource that is not in the ledger, or whose labels are not
``piceli.io/managed-by=piceli`` and ``piceli.io/infra=<name>``, is refused
(``infra-foreign-resource``) before anything runs.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from piceli.infra.machines.model import (
    INFRA_LABEL,
    MANAGED_BY,
    InfraError,
    Infrastructure,
)
from piceli.infra.machines.provider import Prices, Provider
from piceli.infra.machines.render import Rendered, render
from piceli.infra.machines.state import (
    encryption_env,
    locked,
    read_record,
    state_dir,
    write_record,
)
from piceli.infra.machines.tofu import Tofu, TofuResult, locate

__all__ = ["InfraPlan", "Workspace", "apply", "plan", "workspace"]

INVENTORY = "inventory.json"
BACKEND = "backend.json"
CONFIG = "main.tf.json"
LOCK = ".terraform.lock.hcl"
INVENTORY_SCHEMA = "piceli.infra.inventory.v1"
PLAN_SCHEMA = "piceli.infra.plan.v1"
#: Plan JSON keys that change between two identical plans.
VOLATILE = ("timestamp",)
#: Plan JSON lists whose order changes between two identical plans.
UNORDERED = ("relevant_attributes",)


@dataclass
class Workspace:
    """A locked state directory with a located ``tofu`` and its environment."""

    infra: Infrastructure
    directory: Path
    tofu: Tofu
    rendered: Rendered
    env: dict[str, str]
    tokens: dict[str, str] = field(default_factory=dict)

    def run(self, *args: str, timeout: float = 1800) -> TofuResult:
        return self.tofu.run(
            list(args), cwd=self.directory, env=self.env, timeout=timeout
        )

    def ledger(self) -> set[str]:
        return set(read_record(self.directory, INVENTORY).get("created") or ())


def _check(result: TofuResult, code: str, what: str) -> TofuResult:
    if result.code != 0:
        raise InfraError(code, f"tofu {what} failed", result.diagnostics())
    return result


@contextmanager
def workspace(infra: Infrastructure, *, tofu: str | None = None) -> Iterator[Workspace]:
    """Lock the state directory, render the configuration and ``tofu init``."""
    from piceli.infra.machines.credentials import load_credential

    directory = state_dir(infra.name, infra.state_dir)
    with locked(directory):
        binary = locate(tofu)
        assert infra.state_key is not None and infra.backend is not None
        passphrase = load_credential(infra.state_key, "state-key")
        env = encryption_env(passphrase)
        tokens: dict[str, str] = {}
        for provider in infra.providers():
            variable = provider.token_env()
            if variable is not None and provider.credentials is not None:
                tokens[provider.name] = env[variable] = load_credential(
                    provider.credentials, "provider-token"
                )
        env.update(infra.backend.env())
        binary.secrets = [passphrase, *tokens.values(), *infra.backend.secrets()]
        backend = infra.backend.describe()
        recorded = read_record(directory, BACKEND)
        if recorded and recorded != backend:
            raise InfraError(
                "infra-backend-changed",
                "the state backend changed since the last command; move the state "
                "first (tofu init -migrate-state) or restore the declaration",
            )
        stray = sorted(
            item.name
            for pattern in (
                "*.tf",
                "*.tf.json",
                "*.tofu",
                "*.tofu.json",
                "*.tfvars",
                "*.tfvars.json",
            )
            for item in directory.glob(pattern)
            if item.name != CONFIG
        )
        if stray:
            # OpenTofu would load them with Piceli's configuration.
            raise InfraError(
                "infra-state-unexpected",
                "the state directory holds configuration Piceli did not write: "
                + ", ".join(stray),
            )
        rendered = render(infra)
        _write(directory / CONFIG, rendered.config_text)
        _write(directory / LOCK, rendered.lock)
        ws = Workspace(infra, directory, binary, rendered, env, tokens)
        _check(
            ws.run("init", "-input=false", "-no-color", "-lockfile=readonly"),
            "infra-tofu-failed",
            "init",
        )
        if not recorded:
            write_record(directory, BACKEND, backend)
        yield ws


def _write(path: Path, text: str) -> None:
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    path.write_text(text)
    path.chmod(0o600)


@dataclass(frozen=True)
class Change:
    address: str
    actions: tuple[str, ...]
    provider: str | None
    kind: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "actions": list(self.actions),
            "provider": self.provider,
            "kind": self.kind,
            "name": self.name,
        }

    @property
    def action(self) -> str:
        if self.actions in (("delete", "create"), ("create", "delete")):
            return "replace"
        return self.actions[0] if len(self.actions) == 1 else "+".join(self.actions)


@dataclass(frozen=True)
class InfraPlan:
    """A reviewed plan: its hash, its changes and the monthly estimate."""

    infra: str
    operation: str
    plan_hash: str
    config_digest: str
    changes: tuple[Change, ...]
    foreign: tuple[str, ...]
    estimate: dict[str, Any] | None
    tofu: str
    file: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "infra": self.infra,
            "operation": self.operation,
            "plan_hash": self.plan_hash,
            "config_digest": self.config_digest,
            "changes": [change.to_dict() for change in self.changes],
            "summary": _summary(self.changes),
            "foreign": list(self.foreign),
            "estimate": self.estimate,
            "tofu": self.tofu,
        }


def _summary(changes: tuple[Change, ...]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "replace": 0, "delete": 0}
    for change in changes:
        counts[change.action] = counts.get(change.action, 0) + 1
    return counts


def plan_hash(operation: str, rendered: Rendered, document: Mapping[str, Any]) -> str:
    """The approval hash: configuration, operation and plan JSON (no timestamp)."""
    stable = {k: v for k, v in document.items() if k not in VOLATILE}
    # OpenTofu lists these in map order, which changes from run to run.
    for key in UNORDERED:
        if isinstance(stable.get(key), list):
            stable[key] = sorted(
                stable[key], key=lambda item: json.dumps(item, sort_keys=True)
            )
    body = json.dumps(
        {"operation": operation, "config": rendered.digest, "plan": stable},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _provider_of(ws: Workspace, address: str) -> Provider | None:
    owner = ws.rendered.resources.get(address)
    if owner is not None:
        return next((p for p in ws.infra.providers() if p.name == owner[0]), None)
    recorded = {
        item.get("address"): item.get("provider")
        for item in read_record(ws.directory, INVENTORY).get("resources") or ()
        if isinstance(item, dict)
    }
    name = recorded.get(address)
    return next((p for p in ws.infra.providers() if p.name == name), None)


def owned(
    ws: Workspace, address: str, before: Mapping[str, Any] | None, ledger: set[str]
) -> bool:
    """Whether Piceli created ``address`` and it carries this infrastructure's labels."""
    if address not in ledger or not isinstance(before, Mapping):
        return False
    provider = _provider_of(ws, address)
    if provider is None:
        return False
    found = provider.labels_of(before)
    return found.get(MANAGED_BY) == "piceli" and found.get(INFRA_LABEL) == ws.infra.name


def changes_of(
    ws: Workspace, document: Mapping[str, Any]
) -> tuple[tuple[Change, ...], tuple[str, ...]]:
    """The plan's changes (no-ops left out) and the foreign resources it touches."""
    ledger = ws.ledger()
    changes: list[Change] = []
    foreign: list[str] = []
    for item in document.get("resource_changes") or ():
        if not isinstance(item, dict) or item.get("mode") != "managed":
            continue
        change = item.get("change") or {}
        actions = tuple(change.get("actions") or ())
        if not actions or actions == ("no-op",) or actions == ("read",):
            continue
        address = str(item.get("address"))
        owner = ws.rendered.resources.get(address)
        provider = _provider_of(ws, address)
        changes.append(
            Change(
                address,
                actions,
                provider.name if provider else None,
                owner[1] if owner else "unknown",
                owner[2] if owner else address,
            )
        )
        if actions != ("create",) and not owned(
            ws, address, change.get("before"), ledger
        ):
            foreign.append(address)
    changes.sort(key=lambda c: c.address)
    return tuple(changes), tuple(sorted(foreign))


def estimate(ws: Workspace) -> dict[str, Any] | None:
    """Monthly net prices of the declared servers and primary IPs, where known."""
    cache: dict[str, Prices | None] = {}
    items: list[dict[str, Any]] = []
    total = 0.0
    currency: str | None = None
    complete = True
    for server in ws.infra.servers:
        provider = server.provider
        if provider.name not in cache:
            cache[provider.name] = provider.prices(ws.tokens.get(provider.name))
        prices = cache[provider.name]
        assert server.location is not None
        value = prices.server(server.type, server.location) if prices else None
        lines = [("server", server.name, value)]
        for ip in server.primary_ips():
            lines.append(
                (
                    "ip",
                    ip.name,
                    prices.primary_ip(ip.kind, server.location) if prices else None,
                )
            )
        for kind, name, price in lines:
            items.append(
                {
                    "kind": kind,
                    "name": name,
                    "server": server.name,
                    "monthly_net": price,
                }
            )
            if price is None:
                complete = False
            else:
                total += price
                currency = currency or (prices.currency if prices else None)
    if not items or currency is None:
        return None
    return {
        "currency": currency,
        "monthly_net": round(total, 4),
        "complete": complete,
        "items": items,
    }


def plan(
    ws: Workspace,
    *,
    destroy: bool = False,
    keep: Path | None = None,
    prices: bool = True,
) -> InfraPlan:
    """Run ``tofu plan`` and return the reviewed plan (its file under ``keep``)."""
    operation = "destroy" if destroy else "apply"
    from piceli import tempfiles

    with tempfiles.temporary_directory(
        "infra-plan", dir=ws.directory, hidden=True
    ) as work:
        file = work / "plan.tfplan"
        args = ["plan", "-input=false", "-json", "-out=" + str(file)]
        if destroy:
            args.append("-destroy")
        _check(ws.run(*args), "infra-tofu-failed", "plan")
        shown = _check(ws.run("show", "-json", str(file)), "infra-tofu-failed", "show")
        try:
            document = json.loads(shown.stdout)
        except ValueError:
            raise InfraError(
                "infra-tofu-failed", "tofu show -json did not print a plan"
            ) from None
        changes, foreign = changes_of(ws, document)
        result = InfraPlan(
            ws.infra.name,
            operation,
            plan_hash(operation, ws.rendered, document),
            ws.rendered.digest,
            changes,
            foreign,
            estimate(ws) if prices and not destroy else None,
            ws.tofu.version_text,
        )
        if keep is not None:
            target = keep / "plan.tfplan"
            file.replace(target)
            return replace(result, file=target)
        return result


def apply(
    ws: Workspace,
    approved: str,
    *,
    destroy: bool = False,
    say: Callable[[str], None] = lambda _: None,
) -> tuple[InfraPlan, dict[str, Any]]:
    """Plan again; apply that plan only if its hash is ``approved``."""
    from piceli import tempfiles

    with tempfiles.temporary_directory(
        "infra-apply", dir=ws.directory, hidden=True
    ) as work:
        fresh = plan(ws, destroy=destroy, keep=work)
        if fresh.plan_hash != approved:
            raise InfraError(
                "infra-plan-changed",
                "the plan changed since it was approved (or the hash is wrong); "
                "review the new plan",
            )
        if fresh.foreign:
            raise InfraError(
                "infra-foreign-resource",
                "the plan would change or delete resources Piceli did not create: "
                + ", ".join(fresh.foreign),
            )
        record = read_record(ws.directory, INVENTORY)
        ledger = set(record.get("created") or ())
        creates = {c.address for c in fresh.changes if "create" in c.actions}
        record["created"] = sorted(ledger | creates)
        write_record(ws.directory, INVENTORY, record)
        assert fresh.file is not None
        say(f"applying {len(fresh.changes)} change(s) with tofu {ws.tofu.version_text}")
        result = ws.run("apply", "-input=false", "-json", str(fresh.file), timeout=3600)
        inventory = refresh_inventory(ws, prices=fresh.estimate)
        if result.code != 0:
            raise InfraError(
                "infra-apply-failed", "tofu apply failed", result.diagnostics()
            )
        return fresh, inventory


def refresh_inventory(
    ws: Workspace, *, prices: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Rewrite ``inventory.json`` from the state (``tofu show -json``)."""
    shown = _check(ws.run("show", "-json"), "infra-tofu-failed", "show")
    try:
        document = json.loads(shown.stdout) if shown.stdout.strip() else {}
    except ValueError:
        document = {}
    values = document.get("values") or {}
    resources: list[dict[str, Any]] = []
    addresses: set[str] = set()
    for item in (values.get("root_module") or {}).get("resources") or ():
        if not isinstance(item, dict) or item.get("mode") != "managed":
            continue
        address = str(item.get("address"))
        addresses.add(address)
        owner = ws.rendered.resources.get(address)
        provider = _provider_of(ws, address)
        resources.append(
            {
                "address": address,
                "type": item.get("type"),
                "provider": provider.name if provider else None,
                "kind": owner[1] if owner else "unknown",
                "name": owner[2] if owner else address,
                "labels": dict(provider.labels_of(item.get("values") or {}))
                if provider
                else {},
            }
        )
    servers: dict[str, Any] = {}
    for key, output in (values.get("outputs") or {}).items():
        if key.startswith("server-") and isinstance(output, dict):
            value = output.get("value")
            if isinstance(value, dict):
                servers[key.removeprefix("server-")] = {
                    k: value.get(k)
                    for k in ("id", "ipv4", "ipv6", "status")
                    if k in value
                }
    previous = read_record(ws.directory, INVENTORY)
    created = sorted(a for a in previous.get("created") or () if a in addresses)
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "infra": ws.infra.name,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tofu": ws.tofu.version_text,
        "config_digest": ws.rendered.digest,
        "created": created,
        "resources": sorted(resources, key=lambda r: r["address"]),
        "servers": servers,
        "estimate": prices if prices is not None else previous.get("estimate"),
    }
    if not servers and not resources:
        inventory["estimate"] = None
    write_record(ws.directory, INVENTORY, inventory)
    _forget_gone(ws.directory, set(servers))
    return inventory


def _forget_gone(directory: Path, servers: set[str]) -> None:
    """Drop install and registration records of servers that no longer exist."""
    from piceli.infra.machines.install import INSTALLS
    from piceli.infra.machines.register import REGISTRATIONS

    for name in (INSTALLS, REGISTRATIONS):
        record = read_record(directory, name)
        kept = {key: value for key, value in record.items() if key in servers}
        if kept != record:
            write_record(directory, name, kept)
