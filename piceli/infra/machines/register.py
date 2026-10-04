"""Register a server's k3s as a cluster Piceli deploys to.

After the OS is installed (by the owner's hook), ``piceli infra register
REF SERVER`` takes the server's k3s kubeconfig, either

- from the owner (``--kubeconfig FILE``), or
- over SSH: ``ssh-keyscan`` reads the host keys, the preview shows their
  fingerprints and the approval digest covers them; approved, they are
  pinned in the state directory's ``known_hosts`` and ``ssh`` (with
  ``StrictHostKeyChecking=yes`` against that file only) reads
  ``Ssh(kubeconfig_path=)``, retried until k3s has written it;

rewrites its server to ``Cluster(api=)`` (the address the owner declared,
for example a private-network name; k3s must list it in its TLS SANs),
waits until the API answers with a Ready node, and stores it ``0600``
under ``$PICELI_CREDENTIALS_DIR/kubeconfigs/`` (never in Git, never
printed). Then :func:`register_cluster` makes it the cluster's credentials.

:func:`register_cluster` calls the multi-cluster
registration (``piceli.infra.multicluster.register_cluster``: local profile
plus the home controller's Secret) when it exists and the infrastructure
names its ``home`` cluster; otherwise it stores the local profile only.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.infra.machines.model import InfraError, Infrastructure, Server
from piceli.infra.machines.state import read_record, write_record

if TYPE_CHECKING:
    from piceli.infra import Cluster

__all__ = [
    "RegisterPreview",
    "fingerprint",
    "preview_register",
    "register_cluster",
    "run_register",
]

REGISTRATIONS = "registrations.json"
KNOWN_HOSTS = "known_hosts"
_KUBECONFIG_LIMIT = 1024 * 1024


def fingerprint(key: str) -> str:
    """OpenSSH's ``SHA256:`` fingerprint of a base64 public key blob."""
    blob = base64.b64decode(key.encode(), validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
    return "SHA256:" + digest.rstrip("=")


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise InfraError("infra-ssh-missing", f"{name} was not found on PATH")
    return found


def scan_host_keys(address: str, port: int) -> list[tuple[str, str]]:
    """``(type, base64 key)`` of every host key ``address:port`` offers."""
    try:
        done = subprocess.run(
            [_tool("ssh-keyscan"), "-T", "10", "-p", str(port), "--", address],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        done = None
    keys: set[tuple[str, str]] = set()
    for line in (done.stdout if done else "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            try:
                fingerprint(parts[2])
            except ValueError:
                continue
            keys.add((parts[1], parts[2]))
    if not keys:
        raise InfraError(
            "infra-ssh-unreachable",
            "no SSH host key was read from the server (is it up and is SSH open to you?)",
        )
    return sorted(keys)


def _host(address: str, port: int) -> str:
    return address if port == 22 else f"[{address}]:{port}"


@dataclass(frozen=True)
class RegisterPreview:
    infra: str
    server: str
    cluster: str
    api: str
    profile: str
    source: str
    address: str | None
    host_keys: tuple[tuple[str, str], ...]
    kubeconfig_ca: str | None
    profile_exists: bool
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "infra": self.infra,
            "server": self.server,
            "cluster": self.cluster,
            "api": self.api,
            "profile": self.profile,
            "source": self.source,
            "address": self.address,
            "host_keys": [
                {"type": kind, "fingerprint": fingerprint(key)}
                for kind, key in self.host_keys
            ],
            "kubeconfig_ca": self.kubeconfig_ca,
            "profile_exists": self.profile_exists,
            "digest": self.digest,
        }


def _read_yaml(text: str) -> dict[str, Any]:
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        document = None
    if not isinstance(document, dict):
        raise InfraError(
            "infra-kubeconfig-invalid", "the kubeconfig is not a YAML mapping"
        )
    return document


def _entries(
    document: Mapping[str, Any], context: str | None
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    from piceli.k8s.ops.exec_credentials import ProviderFactoryError
    from piceli.k8s.ops.provider_factory import _named

    names = [
        str(item["name"])
        for item in document.get("contexts") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    if context is None:
        if len(names) != 1:
            raise InfraError(
                "infra-kubeconfig-invalid",
                f"the kubeconfig defines several contexts (or none): pass --context; available: {sorted(names)}",
            )
        context = names[0]
    try:
        entry = _named(document, "contexts", context)
        cluster = _named(document, "clusters", str(entry.get("cluster")))
        user = _named(document, "users", str(entry.get("user")))
    except ProviderFactoryError as error:
        raise InfraError("infra-kubeconfig-invalid", str(error)) from None
    return context, dict(cluster), dict(user)


def _ca_digest(cluster: Mapping[str, Any]) -> str | None:
    data = cluster.get("certificate-authority-data")
    if isinstance(data, str):
        return "sha256:" + hashlib.sha256(data.encode()).hexdigest()
    return None


def rewrite_kubeconfig(
    text: str, context: str | None, cluster: Cluster, base: Path | None = None
) -> dict[str, Any]:
    """One context named after the cluster, its server set to ``Cluster(api=)``."""
    document = _read_yaml(text)
    _, entry, user = _entries(document, context)
    for section, keys in (
        (entry, ("certificate-authority",)),
        (user, ("client-certificate", "client-key")),
    ):
        for key in keys:
            value = section.get(key)
            if isinstance(value, str) and base is not None and not os.path.isabs(value):
                section[key] = str((base / value).absolute())
    entry["server"] = cluster.api
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": cluster.name, "cluster": entry}],
        "users": [{"name": cluster.name, "user": user}],
        "contexts": [
            {
                "name": cluster.name,
                "context": {"cluster": cluster.name, "user": cluster.name},
            }
        ],
        "preferences": {},
    }


def _target(server: Server) -> Cluster:
    if server.cluster is None:
        raise InfraError(
            "infra-no-cluster",
            f"server {server.name} declares no cluster (Server(cluster=Cluster(...)))",
        )
    return server.cluster


def preview_register(
    infra: Infrastructure,
    directory: Path,
    name: str,
    *,
    kubeconfig: Path | None = None,
    context: str | None = None,
) -> RegisterPreview:
    """What ``register`` would do, and its approval digest (reads, never writes)."""
    from piceli.infra.machines.install import server_values
    from piceli.profiles import ProfileError, load_profile

    server = infra.server(name)
    if server is None:
        raise InfraError(
            "infra-server-unknown", f"no server named {name!r} is declared"
        )
    cluster = _target(server)
    values = server_values(infra, directory, server)
    try:
        load_profile(cluster.credentials)
        exists = True
    except ProfileError:
        exists = False
    address: str | None = None
    keys: tuple[tuple[str, str], ...] = ()
    ca: str | None = None
    if kubeconfig is not None:
        source = "file"
        text = _read_file(kubeconfig)
        _, entry, _ = _entries(_read_yaml(text), context)
        ca = _ca_digest(entry)
    else:
        source = "ssh"
        address = values.get("address")
        if not address:
            raise InfraError(
                "infra-ssh-unreachable", f"server {name} has no address to reach"
            )
        keys = tuple(scan_host_keys(address, server.ssh.port))
    body = json.dumps(
        {
            "infra": infra.name,
            "server": name,
            "cluster": cluster.name,
            "api": cluster.api,
            "profile": cluster.credentials,
            "source": source,
            "address": address,
            "port": server.ssh.port,
            "user": server.ssh.user,
            "host_keys": keys,
            "ca": ca,
            "context": context,
            "profile_exists": exists,
        },
        sort_keys=True,
    )
    return RegisterPreview(
        infra.name,
        name,
        cluster.name,
        cluster.api,
        cluster.credentials,
        source,
        address,
        keys,
        ca,
        exists,
        "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
    )


def _read_file(path: Path) -> str:
    try:
        if path.stat().st_size > _KUBECONFIG_LIMIT:
            raise InfraError("infra-kubeconfig-invalid", "the kubeconfig exceeds 1 MB")
        return path.read_text()
    except OSError:
        raise InfraError(
            "infra-kubeconfig-invalid", "the kubeconfig file is unreadable"
        ) from None


def fetch_over_ssh(
    server: Server, address: str, known_hosts: Path, deadline: float
) -> str:
    """The k3s kubeconfig read over SSH (retried until ``deadline``)."""
    ssh = server.ssh
    remote = ["cat", "--", ssh.kubeconfig_path]
    if ssh.user != "root":
        remote = ["sudo", "-n", *remote]
    argv = [
        _tool("ssh"),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-p", str(ssh.port),
        "-l", ssh.user,
        "--", address, *remote,
    ]  # fmt: skip
    while True:
        try:
            done = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=120
            )
            if (
                done.returncode == 0
                and done.stdout
                and len(done.stdout) <= _KUBECONFIG_LIMIT
            ):
                return done.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            pass
        if time.monotonic() >= deadline:
            raise InfraError(
                "infra-k3s-not-ready",
                "the k3s kubeconfig could not be read over SSH before the deadline",
            )
        time.sleep(5)


def wait_ready(
    path: Path, context: str, deadline: float, transport: str = "https"
) -> dict[str, int]:
    """Wait until the API answers and lists a Ready node."""
    from piceli.gitops.install import Api
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    while True:
        try:
            client = api_client_from_kubeconfig(path, context, transport=transport)  # type: ignore[arg-type]
            api = Api(client, field_manager="piceli-infra")
            try:
                body = api.call("/api/v1/nodes", "GET")
            finally:
                api.close()
            nodes = [
                item
                for item in (body or {}).get("items") or ()
                if isinstance(item, dict)
            ]
            ready = sum(
                1
                for node in nodes
                if any(
                    isinstance(c, dict)
                    and c.get("type") == "Ready"
                    and c.get("status") == "True"
                    for c in ((node.get("status") or {}).get("conditions") or ())
                )
            )
            if ready:
                return {"nodes": len(nodes), "ready": ready}
        except Exception:  # not up yet: retried until the deadline
            pass
        if time.monotonic() >= deadline:
            raise InfraError(
                "infra-k3s-not-ready",
                "the cluster's API did not report a Ready node in time",
            )
        time.sleep(3)


def kubeconfig_path(cluster: Cluster) -> Path:
    from piceli.infra.machines.credentials import credentials_dir

    return credentials_dir() / "kubeconfigs" / f"{cluster.name}.yaml"


def register_cluster(
    cluster: Cluster,
    kubeconfig: Path,
    context: str,
    *,
    home: Cluster | None = None,
    transport: str = "https",
) -> dict[str, Any]:
    """Make ``kubeconfig``/``context`` the credentials of ``cluster``.

    With a ``home`` cluster (``Infrastructure(home=)``, the cluster whose
    controller deploys onto this one), the multi-cluster registration stores
    the local profile and the controller's Secret. Without one it stores the
    local profile ``Cluster(credentials=)`` the way ``piceli login`` does, so
    ``piceli cluster init|status`` and deploys from this machine reach the
    cluster.
    """
    from piceli.infra import multicluster
    from piceli.profiles import save_profile

    if home is not None:
        result = multicluster.register_cluster(
            cluster,
            kubeconfig=kubeconfig,
            context=context,
            home=home,
            server=None,
            transport=transport,
        )
        return {"cluster": cluster.name, "context": context, **dict(result)}
    profile = save_profile(cluster.credentials, kubeconfig, context)
    return {
        "cluster": cluster.name,
        "state": "profile",
        "profile": profile.name,
        "context": profile.context,
        "secret": None,
        "home": home.name if home is not None else None,
    }


def run_register(
    infra: Infrastructure,
    directory: Path,
    preview: RegisterPreview,
    *,
    kubeconfig: Path | None = None,
    context: str | None = None,
    wait: float = 600,
    transport: str = "https",
    say: Callable[[str], None] = lambda _: None,
) -> dict[str, Any]:
    """Fetch (or read), rewrite, store, wait for k3s, then register."""
    import yaml

    server = infra.server(preview.server)
    assert server is not None
    cluster = _target(server)
    deadline = time.monotonic() + max(0.0, wait)
    if preview.source == "file":
        assert kubeconfig is not None
        text = _read_file(kubeconfig)
        base: Path | None = kubeconfig.absolute().parent
    else:
        assert preview.address is not None
        known = directory / KNOWN_HOSTS
        lines = sorted(
            f"{_host(preview.address, server.ssh.port)} {kind} {key}"
            for kind, key in preview.host_keys
        )
        known.write_text("\n".join(lines) + "\n")
        known.chmod(0o600)
        say(
            f"pinned {len(lines)} host key(s) of {preview.address}; reading the k3s kubeconfig over SSH"
        )
        text = fetch_over_ssh(server, preview.address, known, deadline)
        base = None
    document = rewrite_kubeconfig(
        text, context if preview.source == "file" else None, cluster, base
    )
    del text
    path = kubeconfig_path(cluster)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    staged = path.with_name(f".{path.name}.partial")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(yaml.safe_dump(document, sort_keys=True))
    os.replace(staged, path)
    say(f"waiting for the k3s API at {cluster.api}")
    nodes = wait_ready(path, cluster.name, deadline, transport)
    registered = register_cluster(
        cluster, path, cluster.name, home=infra.home, transport=transport
    )
    outcome = {
        **registered,
        "api": cluster.api,
        "kubeconfig": str(path),
        "source": preview.source,
        "host_keys": preview.to_dict()["host_keys"],
        "nodes": nodes,
        "digest": preview.digest,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    record = read_record(directory, REGISTRATIONS)
    record[preview.server] = outcome
    write_record(directory, REGISTRATIONS, record)
    return outcome
