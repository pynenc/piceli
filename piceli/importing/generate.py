"""Generate a typed :class:`~piceli.app.App` module from declared manifests.

The generator is exact by construction:

1. every object is declared with the typed API where its shape allows it;
2. the typed declarations are rendered in-process and compared with the
   imported manifest, field by field;
3. every difference becomes an :meth:`~piceli.app.App.override` with one
   comment per field ("not typed: …");
4. the finished module is executed and rendered again and must reproduce every
   imported field (otherwise the import refuses with
   ``import-roundtrip-mismatch``).

Secret values never reach this module: :func:`~piceli.importing.clean.scrub`
replaces them with ``<private>`` before generation, and each Secret key
becomes an ``import`` generator input (``ctx.secret(...)``).
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from piceli.app.app import _named_items
from piceli.importing.clean import PRIVATE, is_default, strip_defaults
from piceli.importing.codegen import (
    Atom,
    Expr,
    call,
    dict_of,
    identifier,
    list_of,
    literal,
    names_in,
    one_tuple,
    statement,
)


class ImportFailure(ValueError):
    """The import cannot proceed; ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} [{code}]")
        self.code = code


_PICELI = frozenset(
    {
        "App",
        "ConfigKey",
        "ConfigVolume",
        "Container",
        "ContainerPort",
        "ExistingClaim",
        "FieldRef",
        "MemoryVolume",
        "Mount",
        "Resources",
        "SecretKey",
        "SecretVolume",
        "ServicePort",
    }
)
_PLAN = frozenset({"DeploymentComponent", "ResourceIntent"})
TYPED_KINDS = ("ConfigMap", "Secret", "Deployment", "Service", "NetworkPolicy")
_ORDER = {kind: index for index, kind in enumerate(TYPED_KINDS)}
_SECRET_INPUT = re.compile(r"[^a-z0-9_-]+")
_SKIPPED_SECRET_TYPES = {
    "kubernetes.io/service-account-token": "a service account token that Kubernetes manages",
    "helm.sh/release.v1": "a Helm release record",
}
_PORT_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_ENV_NAME = re.compile(r"[-._a-zA-Z][-._a-zA-Z0-9]*")
_QUANTITY = re.compile(
    r"[+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?"
)
_TIMING = {
    "initialDelaySeconds": "initial_delay_seconds",
    "periodSeconds": "period_seconds",
    "timeoutSeconds": "timeout_seconds",
    "failureThreshold": "failure_threshold",
    "successThreshold": "success_threshold",
}
_RESOURCES = {
    ("requests", "cpu"): "cpu",
    ("requests", "memory"): "memory",
    ("requests", "ephemeral-storage"): "ephemeral_storage",
    ("limits", "cpu"): "cpu_limit",
    ("limits", "memory"): "memory_limit",
    ("limits", "ephemeral-storage"): "ephemeral_storage_limit",
}


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class SecretInput:
    """One ``[secrets.<input>]`` import generator the module needs."""

    input: str
    secret: str
    key: str

    def toml(self) -> str:
        return (
            f"[secrets.{self.input}]\n"
            'type = "import"\n'
            f"secret = {{ name = {_toml(self.secret)}, key = {_toml(self.key)} }}"
        )


@dataclass(frozen=True)
class ImportedObject:
    """How one object was imported: ``typed``, ``raw`` or ``existing-claim``."""

    kind: str
    name: str
    mode: str
    untyped: tuple[str, ...] = ()
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "as": self.mode,
            "untyped_fields": list(self.untyped),
        }
        if self.reason:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True)
class Skipped:
    kind: str
    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class RunningImage:
    """An image a Deployment declares and the digest its pods run (if known)."""

    deployment: str
    container: str
    image: str
    running: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment": self.deployment,
            "container": self.container,
            "image": self.image,
            "running_digest_ref": self.running,
        }


@dataclass(frozen=True)
class ImportResult:
    """The generated module and what it contains."""

    module: str
    app: str
    namespace: str
    source: str
    objects: tuple[ImportedObject, ...]
    secrets: tuple[SecretInput, ...]
    skipped: tuple[Skipped, ...]
    images: tuple[RunningImage, ...]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.module.encode()).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "namespace": self.namespace,
            "source": self.source,
            "module_sha256": self.sha256,
            "objects": [item.to_dict() for item in self.objects],
            "secret_inputs": [
                {"input": item.input, "secret": item.secret, "key": item.key}
                for item in self.secrets
            ],
            "skipped": [item.to_dict() for item in self.skipped],
            "images": [item.to_dict() for item in self.images],
        }


def _toml(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------- diff


class _Same:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SAME"


SAME: Any = _Same()


@dataclass
class _Clear:
    """Replace a named list whose order the merge cannot reach: clear, then set."""

    value: list[Any]


def diff(
    rendered: Any,
    target: Any,
    kind: str,
    path: tuple[str, ...] = (),
    label: str = "",
    notes: list[str] | None = None,
) -> Any:
    """The override patch that turns ``rendered`` into ``target``, or ``SAME``.

    A field the typed model renders but ``target`` lacks is accepted when it
    holds the Kubernetes default; otherwise the patch removes it. ``notes``
    collects one readable path per patched field.
    """
    notes = notes if notes is not None else []
    if isinstance(rendered, dict) and isinstance(target, dict):
        patch: dict[str, Any] = {}
        for key, value in target.items():
            where = f"{label}.{key}" if label else key
            if key not in rendered:
                patch[key] = value
                notes.append(where)
                continue
            child = diff(rendered[key], value, kind, (*path, key), where, notes)
            if child is not SAME:
                patch[key] = child
        for key, value in rendered.items():
            if key not in target and not is_default(
                kind, (*path, key), value, rendered
            ):
                patch[key] = None
                notes.append(
                    f"{label}.{key} (removed)" if label else f"{key} (removed)"
                )
        return patch or SAME
    if isinstance(rendered, list) and isinstance(target, list):
        rendered_names = _named_items(rendered)
        target_names = _named_items(target)
        if rendered_names is not None and target_names is not None:
            if target_names[: len(rendered_names)] == rendered_names:
                items: list[Any] = []
                for index, name in enumerate(target_names):
                    where = f"{label}[{name}]"
                    if index < len(rendered):
                        child = diff(
                            rendered[index],
                            target[index],
                            kind,
                            (*path, "*"),
                            where,
                            notes,
                        )
                        if child is not SAME:
                            items.append({"name": name, **child})
                    else:
                        items.append(target[index])
                        notes.append(where)
                return items or SAME
            notes.append(f"{label} (order)")
            return _Clear(target)
        if len(rendered) == len(target) and all(
            diff(left, right, kind, (*path, "*"), label, []) is SAME
            for left, right in zip(rendered, target, strict=True)
        ):
            return SAME
        notes.append(label)
        return target
    if rendered == target:
        return SAME
    notes.append(label)
    return target


def _split_clears(patch: Any) -> tuple[Any, Any]:
    """``(clearing patch or None, main patch)`` from a patch with ``_Clear``."""
    found = False

    def clearing(value: Any) -> Any:
        nonlocal found
        if isinstance(value, _Clear):
            found = True
            return None
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if isinstance(item, _Clear):
                    found = True
                    result[key] = None
                elif isinstance(item, dict | list):
                    inner = clearing(item)
                    if inner:
                        result[key] = inner
            return result
        if isinstance(value, list):
            result_list = []
            for item in value:
                if isinstance(item, dict) and "name" in item:
                    inner = clearing({k: v for k, v in item.items() if k != "name"})
                    if inner:
                        result_list.append({"name": item["name"], **inner})
            return result_list
        return None

    def main(value: Any) -> Any:
        if isinstance(value, _Clear):
            return value.value
        if isinstance(value, dict):
            return {key: main(item) for key, item in value.items()}
        if isinstance(value, list):
            return [main(item) for item in value]
        return value

    first = clearing(patch)
    return (first if found else None), main(patch)


# ---------------------------------------------------------------- generator


@dataclass
class _Decl:
    kind: str
    name: str
    var: str | None
    expr: Expr | None
    typed: bool = True
    needs_var: bool = False
    reason: str | None = None
    notes: list[str] = field(default_factory=list)
    overrides: list[dict[str, Any]] = field(default_factory=list)


def _selector(manifest: Mapping[str, Any]) -> dict[str, str] | None:
    selector = manifest.get("spec", {}).get("selector")
    if not isinstance(selector, dict) or set(selector) != {"matchLabels"}:
        return None
    labels = selector["matchLabels"]
    return dict(labels) if isinstance(labels, dict) and labels else None


def _octal(mode: Any) -> Expr | None:
    if isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o777:
        return Atom(f"0o{mode:o}")
    return None


class _Generator:
    def __init__(
        self,
        manifests: Sequence[Mapping[str, Any]],
        *,
        app_name: str,
        namespace: str,
        source: str,
        origin: str,
        images: Mapping[tuple[str, str], str],
        skipped: Iterable[Skipped],
    ) -> None:
        self.namespace = namespace
        self.app_name = app_name
        self.source = source
        self.origin = origin
        self.running = dict(images)
        self.skipped = list(skipped)
        self.taken: set[str] = {"app", "ctx", "build"}
        self.secret_inputs: list[SecretInput] = []
        self.claims: dict[str, dict[str, Any]] = {}
        self.targets: dict[tuple[str, str], dict[str, Any]] = {}
        typed: list[dict[str, Any]] = []
        raw: list[dict[str, Any]] = []
        for manifest in manifests:
            kind = str(manifest.get("kind"))
            name = str(manifest["metadata"]["name"])
            if kind == "PersistentVolumeClaim":
                self.claims[name] = dict(manifest)
                continue
            if kind == "Secret" and manifest.get("type") in _SKIPPED_SECRET_TYPES:
                self.skipped.append(
                    Skipped(kind, name, _SKIPPED_SECRET_TYPES[str(manifest["type"])])
                )
                continue
            if kind == "Pod":
                continue
            target = strip_defaults(manifest)
            self.targets[(kind, name)] = target
            (typed if kind in _ORDER else raw).append(target)
        self.typed = sorted(
            typed, key=lambda item: (_ORDER[item["kind"]], item["metadata"]["name"])
        )
        self.raw = sorted(
            raw, key=lambda item: (item["kind"], item["metadata"]["name"])
        )
        self.vars: dict[tuple[str, str], str] = {}
        self.secret_keys: dict[str, set[str]] = {}
        self.config_keys: dict[str, set[str]] = {}
        self.selectors: dict[str, str] = {}
        self.decls: list[_Decl] = []
        self.images_seen: list[RunningImage] = []
        self.app_labels = self._app_labels()

    # ------------------------------------------------------------ naming

    def _allocate_names(self) -> None:
        suffix = {
            "Deployment": "deployment",
            "ConfigMap": "config",
            "Secret": "secret",
            "Service": "service",
            "NetworkPolicy": "policy",
        }
        order = ["Deployment", "ConfigMap", "Secret", "Service", "NetworkPolicy"]
        for kind in order:
            for item in self.typed:
                if item["kind"] == kind:
                    name = item["metadata"]["name"]
                    self.vars[(kind, name)] = identifier(name, self.taken, suffix[kind])

    def _app_labels(self) -> dict[str, str]:
        sets = [
            dict(item["metadata"].get("labels") or {})
            for item in self.typed
            if item["kind"] in _ORDER
        ]
        if not sets:
            return {"app.kubernetes.io/part-of": self.app_name}
        common = dict(sets[0])
        for labels in sets[1:]:
            common = {
                key: value for key, value in common.items() if labels.get(key) == value
            }
        return dict(sorted(common.items()))

    # ------------------------------------------------------------ objects

    def build(self) -> None:
        self._allocate_names()
        for item in self.typed:
            kind = item["kind"]
            name = item["metadata"]["name"]
            if kind == "ConfigMap":
                self.decls.append(self._config(item))
            elif kind == "Secret":
                self.decls.append(self._secret(item))
            elif kind == "Deployment":
                self.decls.append(self._deployment(item))
            elif kind == "Service":
                self.decls.append(self._service(item))
            else:
                self.decls.append(self._policy(item))
            self.decls[-1].name = name
        for item in self.raw:
            self.decls.append(self._raw(item, f"{item['kind']} is not typed yet"))

    def _config(self, item: dict[str, Any]) -> _Decl:
        name = item["metadata"]["name"]
        data = item.get("data") or {}
        if not all(isinstance(value, str) for value in data.values()):
            return self._raw(item, "non-string data")
        self.config_keys[name] = set(data)
        var = self.vars[("ConfigMap", name)]
        return _Decl(
            "ConfigMap", name, var, call("app.config", literal(name), literal(data))
        )

    def _secret(self, item: dict[str, Any]) -> _Decl:
        name = item["metadata"]["name"]
        keys = sorted(item.get("data") or {})
        var = self.vars[("Secret", name)]
        if not keys:
            self.skipped.append(Skipped("Secret", name, "no keys to import"))
            return _Decl("Secret", name, None, None, typed=False)
        taken = {entry.input for entry in self.secret_inputs}
        values = []
        for key in keys:
            base = _SECRET_INPUT.sub("-", f"{name}-{key}".lower()).strip("-_")
            base = ("s-" + base if not base[:1].isalpha() else base)[:60].rstrip("-_")
            chosen = base
            number = 2
            while chosen in taken:
                chosen = f"{base[:57]}-{number}"
                number += 1
            taken.add(chosen)
            self.secret_inputs.append(SecretInput(chosen, name, key))
            values.append((key, call("ctx.secret", literal(chosen))))
        self.secret_keys[name] = set(keys)
        data = dict_of(values)
        secret_type = item.get("type")
        return _Decl(
            "Secret",
            name,
            var,
            call(
                "app.secret",
                literal(name),
                data,
                type=literal(secret_type)
                if secret_type and secret_type != "Opaque"
                else None,
            ),
        )

    def _env(self, entries: Any) -> Expr | None:
        if not isinstance(entries, list):
            return None
        values: list[tuple[str, Expr]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            name = entry["name"]
            if name in seen or not _ENV_NAME.fullmatch(name):
                continue
            value = self._env_value(entry)
            if value is None:
                continue
            seen.add(name)
            values.append((name, value))
        return dict_of(values) if values else None

    def _env_value(self, entry: Mapping[str, Any]) -> Expr | None:
        keys = set(entry) - {"name"}
        if keys == {"value"} and isinstance(entry["value"], str):
            return literal(entry["value"])
        if keys != {"valueFrom"} or not isinstance(entry["valueFrom"], dict):
            return None
        source = entry["valueFrom"]
        if set(source) == {"secretKeyRef"}:
            ref = source["secretKeyRef"]
            if set(ref) - {"name", "key", "optional"} or not {"name", "key"} <= set(
                ref
            ):
                return None
            known = self.secret_keys.get(ref["name"], set())
            if ref["key"] in known and "optional" not in ref:
                return call(
                    f"{self.vars[('Secret', ref['name'])]}.key", literal(ref["key"])
                )
            return call(
                "SecretKey",
                secret=literal(ref["name"]),
                key=literal(ref["key"]),
                optional=literal(ref["optional"]) if "optional" in ref else None,
            )
        if set(source) == {"configMapKeyRef"}:
            ref = source["configMapKeyRef"]
            if set(ref) - {"name", "key", "optional"} or not {"name", "key"} <= set(
                ref
            ):
                return None
            known = self.config_keys.get(ref["name"], set())
            if ref["key"] in known and "optional" not in ref:
                return call(
                    f"{self.vars[('ConfigMap', ref['name'])]}.key", literal(ref["key"])
                )
            return call(
                "ConfigKey",
                config=literal(ref["name"]),
                key=literal(ref["key"]),
                optional=literal(ref["optional"]) if "optional" in ref else None,
            )
        if set(source) == {"fieldRef"} and set(source["fieldRef"]) == {"fieldPath"}:
            return call("FieldRef", field_path=literal(source["fieldRef"]["fieldPath"]))
        return None

    def _probe(self, probe: Any) -> Expr | None:
        if not isinstance(probe, dict):
            return None
        actions = [key for key in ("httpGet", "tcpSocket", "exec") if key in probe]
        if len(actions) != 1 or set(probe) - {actions[0], *_TIMING}:
            return None
        timing = {
            _TIMING[key]: literal(value)
            for key, value in probe.items()
            if key in _TIMING
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= (0 if key == "initialDelaySeconds" else 1)
        }
        if len(timing) != len(set(probe) & set(_TIMING)):
            return None
        action = probe[actions[0]]
        if actions[0] == "httpGet":
            if not isinstance(action, dict) or set(action) - {"path", "port", "scheme"}:
                return None
            path, port = action.get("path"), action.get("port")
            if not isinstance(path, str) or not path.startswith("/") or not _port(port):
                return None
            scheme = action.get("scheme")
            if scheme not in (None, "HTTP", "HTTPS"):
                return None
            return call(
                "app.probe.http",
                literal(path),
                literal(port),
                scheme=literal(scheme) if scheme else None,
                **timing,
            )
        if actions[0] == "tcpSocket":
            if not isinstance(action, dict) or set(action) != {"port"}:
                return None
            if not _port(action["port"]):
                return None
            return call("app.probe.tcp", literal(action["port"]), **timing)
        command = action.get("command") if isinstance(action, dict) else None
        if (
            set(action) != {"command"}
            or not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            return None
        return call("app.probe.exec", literal(command), **timing)

    def _resources(self, value: Any) -> Expr | None:
        if not isinstance(value, dict):
            return None
        kwargs: dict[str, Expr | None] = {}
        for (section, key), argument in _RESOURCES.items():
            quantity = (value.get(section) or {}).get(key)
            if isinstance(quantity, str) and _QUANTITY.fullmatch(quantity):
                kwargs[argument] = literal(quantity)
        if not kwargs:
            return None
        return call("Resources", **kwargs)

    def _ports(self, ports: Any) -> Expr | None:
        if not isinstance(ports, list) or not ports:
            return None
        items: list[Any] = []
        for port in ports:
            if not isinstance(port, dict) or set(port) - {
                "containerPort",
                "name",
                "protocol",
            }:
                return None
            number = port.get("containerPort")
            if not isinstance(number, int) or not 1 <= number <= 65535:
                return None
            name, protocol = port.get("name"), port.get("protocol")
            if name is not None and not _port_name(name):
                return None
            if protocol not in (None, "TCP", "UDP", "SCTP"):
                return None
            if name is None and protocol is None:
                items.append(literal(number))
            else:
                items.append(
                    call(
                        "ContainerPort",
                        port=literal(number),
                        name=literal(name) if name else None,
                        protocol=literal(protocol) if protocol else None,
                    )
                )
        return list_of(items)

    def _volume(
        self, volume: Mapping[str, Any], mount_path: str
    ) -> tuple[Expr, bool] | None:
        """``(volume expression, claim read-only)`` for a typed volume source."""
        name = volume.get("name")
        sources = [key for key in volume if key != "name"]
        if len(sources) != 1:
            return None
        source_key = sources[0]
        source = volume[source_key]
        if not isinstance(source, dict):
            return None
        explicit = lambda default: literal(name) if name != default else None  # noqa: E731
        if source_key == "configMap":
            if set(source) - {"name", "items", "defaultMode"} or "name" not in source:
                return None
            items = _items(source.get("items"))
            if items is False:
                return None
            config = source["name"]
            reference = (
                Atom(self.vars[("ConfigMap", config)])
                if ("ConfigMap", config) in self.vars and config in self.config_keys
                else literal(config)
            )
            return call(
                "ConfigVolume",
                reference,
                name=explicit(config.replace(".", "-")[:63]),
                items=literal(items) if items else None,
                default_mode=_octal(source.get("defaultMode"))
                if "defaultMode" in source
                else None,
            ), False
        if source_key == "secret":
            if (
                set(source) - {"secretName", "items", "defaultMode"}
                or "secretName" not in source
            ):
                return None
            items = _items(source.get("items"))
            if items is False:
                return None
            secret = source["secretName"]
            reference = (
                Atom(self.vars[("Secret", secret)])
                if secret in self.secret_keys
                else literal(secret)
            )
            return call(
                "SecretVolume",
                reference,
                name=explicit(secret.replace(".", "-")[:63]),
                items=literal(items) if items else None,
                default_mode=_octal(source.get("defaultMode"))
                if "defaultMode" in source
                else None,
            ), False
        if source_key == "emptyDir":
            if source.get("medium") != "Memory" or set(source) - {
                "medium",
                "sizeLimit",
            }:
                return None
            default = (
                re.sub(r"[^a-z0-9]+", "-", mount_path.lower()).strip("-")[:63] or "root"
            )
            return call(
                "MemoryVolume",
                name=explicit(default),
                size_limit=literal(str(source["sizeLimit"]))
                if "sizeLimit" in source
                else None,
            ), False
        if source_key == "persistentVolumeClaim":
            if set(source) - {"claimName", "readOnly"} or "claimName" not in source:
                return None
            claim = source["claimName"]
            read_only = source.get("readOnly") is True
            return call(
                "ExistingClaim",
                literal(claim),
                name=explicit(claim.replace(".", "-")[:63]),
                read_only=literal(True) if read_only else None,
            ), read_only
        return None

    def _mounts(
        self, container: Mapping[str, Any], pod_volumes: Mapping[str, Any]
    ) -> Expr | None:
        mounts = container.get("volumeMounts")
        if not isinstance(mounts, list):
            return None
        values: list[tuple[str, Expr]] = []
        for mount in mounts:
            if not isinstance(mount, dict) or set(mount) - {
                "name",
                "mountPath",
                "readOnly",
                "subPath",
            }:
                continue
            path = mount.get("mountPath")
            volume = pod_volumes.get(str(mount.get("name")))
            if not isinstance(path, str) or not path.startswith("/") or volume is None:
                continue
            typed = self._volume(volume, path)
            if typed is None:
                continue
            expr, claim_read_only = typed
            read_only = mount.get("readOnly") is True
            sub_path = mount.get("subPath")
            if claim_read_only and not read_only:
                continue  # a read-only claim is always mounted read-only
            if (read_only and not claim_read_only) or sub_path:
                expr = call(
                    "Mount",
                    expr,
                    read_only=literal(True)
                    if read_only and not claim_read_only
                    else None,
                    sub_path=literal(sub_path) if sub_path else None,
                )
            values.append((path, expr))
        return dict_of(values) if values else None

    def _container(
        self, container: Mapping[str, Any], pod_volumes: Mapping[str, Any]
    ) -> dict[str, Expr | None]:
        command, args = container.get("command"), container.get("args")
        working_dir = container.get("workingDir")
        policy = container.get("imagePullPolicy")
        return {
            "image": literal(container.get("image", "")),
            "command": literal(command) if _strings(command) else None,
            "args": literal(args) if _strings(args) else None,
            "working_dir": literal(working_dir)
            if isinstance(working_dir, str) and working_dir.startswith("/")
            else None,
            "env": self._env(container.get("env")),
            "ports": self._ports(container.get("ports")),
            "ready": self._probe(container.get("readinessProbe")),
            "live": self._probe(container.get("livenessProbe")),
            "startup": self._probe(container.get("startupProbe")),
            "resources": self._resources(container.get("resources")),
            "volumes": self._mounts(container, pod_volumes),
            "pull_policy": literal(policy)
            if policy in ("Always", "IfNotPresent", "Never")
            else None,
        }

    def _deployment(self, item: dict[str, Any]) -> _Decl:
        name = item["metadata"]["name"]
        spec = item.get("spec") or {}
        pod = (spec.get("template") or {}).get("spec") or {}
        containers = pod.get("containers")
        selector = _selector(item)
        if not isinstance(containers, list) or not containers or selector is None:
            return self._raw(item, "no containers or a non-matchLabels selector")
        if not all(
            isinstance(c, dict) and isinstance(c.get("image"), str) and c.get("name")
            for c in containers
        ):
            return self._raw(item, "a container without a name or image")
        volumes: dict[str, Any] = {
            str(volume.get("name")): volume
            for volume in pod.get("volumes") or []
            if isinstance(volume, dict)
        }
        main = containers[0]
        kwargs = self._container(main, volumes)
        sidecars = [self._container_call(c, volumes) for c in containers[1:]]
        init = [
            self._container_call(c, volumes)
            for c in pod.get("initContainers") or []
            if isinstance(c, dict) and c.get("name") and isinstance(c.get("image"), str)
        ]
        template_labels = ((spec.get("template") or {}).get("metadata") or {}).get(
            "labels"
        ) or {}
        extra_labels = {
            key: value
            for key, value in sorted(template_labels.items())
            if selector.get(key) != value and self.app_labels.get(key) != value
        }
        strategy = (spec.get("strategy") or {}).get("type")
        replicas = spec.get("replicas", 1)
        self.selectors[_canonical(selector)] = self.vars[("Deployment", name)]
        expr = call(
            "app.deployment",
            literal(name),
            container=literal(main["name"]) if main["name"] != name else None,
            **kwargs,
            sidecars=list_of(sidecars) if sidecars else None,
            init=list_of(init) if init else None,
            replicas=literal(replicas)
            if isinstance(replicas, int) and replicas != 1
            else None,
            share_process_namespace=literal(True)
            if pod.get("shareProcessNamespace") is True
            else None,
            strategy=literal(strategy)
            if strategy in ("RollingUpdate", "Recreate") and strategy != "RollingUpdate"
            else None,
            service_account=literal(pod["serviceAccountName"])
            if isinstance(pod.get("serviceAccountName"), str)
            and _port_name_like(pod["serviceAccountName"])
            else None,
            selector=literal(selector)
            if selector != {"app.kubernetes.io/name": name}
            else None,
            labels=literal(extra_labels) if extra_labels else None,
        )
        for container in containers:
            self._record_image(name, container)
        return _Decl("Deployment", name, self.vars[("Deployment", name)], expr)

    def _container_call(
        self, container: Mapping[str, Any], volumes: Mapping[str, Any]
    ) -> Expr:
        return call(
            "Container",
            name=literal(container["name"]),
            **self._container(container, volumes),
        )

    def _record_image(self, deployment: str, container: Mapping[str, Any]) -> None:
        self.images_seen.append(
            RunningImage(
                deployment,
                str(container["name"]),
                str(container["image"]),
                self.running.get((deployment, str(container["name"]))),
            )
        )

    def _service(self, item: dict[str, Any]) -> _Decl:
        name = item["metadata"]["name"]
        spec = item.get("spec") or {}
        selector = spec.get("selector")
        workload = (
            self.selectors.get(_canonical(selector))
            if isinstance(selector, dict)
            else None
        )
        if workload is None:
            return self._raw(item, "its selector matches no imported Deployment")
        service_type = spec.get("type")
        if service_type not in (None, "ClusterIP", "NodePort", "LoadBalancer"):
            return self._raw(item, f"type {service_type} is not typed yet")
        ports = spec.get("ports")
        if not isinstance(ports, list) or not ports:
            return self._raw(item, "no ports")
        typed_ports = []
        for port in ports:
            if not isinstance(port, dict) or not isinstance(port.get("port"), int):
                return self._raw(item, "a port without a number")
            target = port.get("targetPort")
            if target is not None and not _port(target):
                return self._raw(item, "an invalid target port")
            if port.get("name") is not None and not _port_name(port["name"]):
                return self._raw(item, "an invalid port name")
            typed_ports.append(port)
        deployment_name = next(
            key[1]
            for key, var in self.vars.items()
            if key[0] == "Deployment" and var == workload
        )
        kwargs: dict[str, Expr | None] = {}
        args: list[Expr] = [Atom(workload)]
        if (
            len(typed_ports) == 1
            and "name" not in typed_ports[0]
            and typed_ports[0].get("protocol", "TCP") == "TCP"
        ):
            port = typed_ports[0]
            args.append(literal(port["port"]))
            target = port.get("targetPort")
            kwargs["target_port"] = (
                literal(target)
                if target is not None and target != port["port"]
                else None
            )
        else:
            if any("name" not in port for port in typed_ports) and len(typed_ports) > 1:
                return self._raw(item, "several ports without names")
            kwargs["ports"] = list_of(
                [
                    call(
                        "ServicePort",
                        port=literal(port["port"]),
                        target_port=literal(port["targetPort"])
                        if port.get("targetPort") not in (None, port["port"])
                        else None,
                        name=literal(port["name"]) if "name" in port else None,
                        protocol=literal(port["protocol"])
                        if port.get("protocol") not in (None, "TCP")
                        else None,
                    )
                    for port in typed_ports
                ]
            )
        kwargs["name"] = literal(name) if name != deployment_name else None
        kwargs["type"] = (
            literal(service_type) if service_type not in (None, "ClusterIP") else None
        )
        return _Decl(
            "Service",
            name,
            self.vars[("Service", name)],
            call("app.service", *args, **kwargs),
        )

    def _policy(self, item: dict[str, Any]) -> _Decl:
        name = item["metadata"]["name"]
        spec = item.get("spec") or {}
        pod_selector = spec.get("podSelector")
        workload = None
        if isinstance(pod_selector, dict) and set(pod_selector) == {"matchLabels"}:
            workload = self.selectors.get(_canonical(pod_selector["matchLabels"]))
        if workload is None:
            return self._raw(item, "its pod selector matches no imported Deployment")
        deployment_name = next(
            key[1]
            for key, var in self.vars.items()
            if key[0] == "Deployment" and var == workload
        )
        rules = spec.get("ingress") or []
        allow: list[Expr] = []
        ports: list[int] = []
        if isinstance(rules, list) and len(rules) == 1:
            typed = self._ingress_rule(rules[0])
            if typed is not None:
                allow, ports = typed
        return _Decl(
            "NetworkPolicy",
            name,
            self.vars[("NetworkPolicy", name)],
            call(
                "app.network_policy",
                Atom(workload),
                allow_from=list_of(allow) if allow else None,
                ports=literal(ports) if ports else None,
                name=literal(name) if name != f"{deployment_name}-ingress" else None,
            ),
        )

    def _ingress_rule(self, rule: Any) -> tuple[list[Expr], list[int]] | None:
        """``(peer Deployments, TCP ports)`` for a rule the typed model can express."""
        if not isinstance(rule, dict) or set(rule) - {"from", "ports"}:
            return None
        peers: list[Expr] = []
        for peer in rule.get("from") or []:
            if not isinstance(peer, dict):
                return None
            selector = peer.get("podSelector")
            if (
                set(peer) != {"podSelector"}
                or not isinstance(selector, dict)
                or set(selector) != {"matchLabels"}
                or not isinstance(selector["matchLabels"], dict)
            ):
                return None
            var = self.selectors.get(_canonical(selector["matchLabels"]))
            if var is None:
                return None
            peers.append(Atom(var))
        ports: list[int] = []
        for port in rule.get("ports") or []:
            if not isinstance(port, dict) or set(port) != {"port"}:
                return None
            if not isinstance(port["port"], int) or isinstance(port["port"], bool):
                return None
            ports.append(port["port"])
        return peers, ports

    def _raw(self, item: Mapping[str, Any], reason: str) -> _Decl:
        kind = str(item["kind"])
        name = str(item["metadata"]["name"])
        component = f"{kind.lower()}-{name}"[:253]
        expr = call(
            "app.add",
            call(
                "DeploymentComponent",
                literal(component),
                one_tuple(call("ResourceIntent.from_manifest", literal(dict(item)))),
            ),
        )
        return _Decl(kind, name, None, expr, typed=False, reason=reason)

    # ----------------------------------------------------------- rendering

    def _app_statement(self, depth: int) -> list[str]:
        labels = self.app_labels
        default = {"app.kubernetes.io/part-of": self.app_name}
        return statement(
            call(
                "App",
                literal(self.app_name),
                labels=literal(labels) if labels != default else None,
            ),
            "app",
            depth,
        )

    def _imports(self, everything: bool = False) -> list[str]:
        """The import block: every name the declarations read (or all of them)."""
        used: set[str] = set(_PICELI | _PLAN) if everything else set()
        for decl in self.decls:
            if decl.expr is not None and not everything:
                used |= names_in(decl.expr)
        result = ["from __future__ import annotations", ""]
        for module, names in (
            ("piceli", sorted({"App"} | (used & _PICELI))),
            ("piceli.k8s.ops.plan", sorted({"DeploymentComposition"} | (used & _PLAN))),
            ("piceli.k8s.release_spec", ["ReleaseContext"]),
        ):
            line = f"from {module} import " + ", ".join(names)
            if len(line) <= 88:
                result.append(line)
            else:
                result += [
                    f"from {module} import (",
                    *[f"    {n}," for n in names],
                    ")",
                ]
        return result

    def _context(self) -> Any:
        from piceli.app.render import placeholder_inputs
        from piceli.k8s.release_spec import ReleaseContext

        return ReleaseContext(
            namespace=self.namespace,
            images=MappingProxyType({}),
            secrets=MappingProxyType(
                placeholder_inputs([item.input for item in self.secret_inputs])
            ),
        )

    def _rendered(self, app: Any) -> dict[tuple[str, str], dict[str, Any]]:
        composition = app.render(self.namespace)
        return {
            (resource.ref.kind, resource.ref.name): resource.manifest
            for component in composition.components
            for resource in component.resources
        }

    def _typed_pass(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Execute each declaration; one the typed model rejects becomes raw."""
        namespace: dict[str, Any] = {"ctx": self._context()}
        header = "\n".join([*self._imports(everything=True), *self._app_statement(0)])
        exec(compile(header, "<piceli-import>", "exec"), namespace)
        for index, decl in enumerate(self.decls):
            if decl.expr is None:
                continue
            source = "\n".join(statement(decl.expr, decl.var, 0))
            try:
                exec(compile(source, "<piceli-import>", "exec"), namespace)
            except Exception as error:  # the typed model rejected a value
                target = self.targets[(decl.kind, decl.name)]
                self.decls[index] = self._raw(
                    target, f"the typed model rejected it ({type(error).__name__})"
                )
                replacement = self.decls[index]
                assert replacement.expr is not None
                exec(
                    compile(
                        "\n".join(statement(replacement.expr, None, 0)),
                        "<piceli-import>",
                        "exec",
                    ),
                    namespace,
                )
        return self._rendered(namespace["app"])

    def _compute_overrides(self, rendered: Mapping[tuple[str, str], Any]) -> None:
        for decl in self.decls:
            if not decl.typed or decl.expr is None:
                continue
            key = (decl.kind, decl.name)
            notes: list[str] = []
            patch = diff(rendered[key], self.targets[key], decl.kind, notes=notes)
            if patch is SAME:
                continue
            clearing, main = _split_clears(patch)
            decl.overrides = [item for item in (clearing, main) if item]
            decl.notes = notes

    def module(self) -> str:
        body = self._app_statement(1)
        used: set[str] = set()
        for decl in self.decls:
            if decl.expr is not None:
                used |= names_in(decl.expr)
        for decl in self.decls:
            if decl.expr is None:
                continue
            body.append("")
            if not decl.typed:
                body.append(
                    f"    # {decl.kind} {decl.name}: kept as a raw manifest ({decl.reason})"
                )
            target = (
                decl.var if decl.var and (decl.var in used or decl.overrides) else None
            )
            body += statement(decl.expr, target, 1)
            for note in decl.notes:
                body.append(f"    # not typed: {note}")
            for patch in decl.overrides:
                assert decl.var is not None
                body += statement(
                    call("app.override", Atom(decl.var), literal(patch)), None, 1
                )
        body += ["", "    return app.composition(ctx)"]
        return "\n".join(
            [
                self._docstring(),
                "",
                *self._imports(),
                "",
                "",
                "def build(ctx: ReleaseContext) -> DeploymentComposition:",
                *body,
                "",
            ]
        )

    def _docstring(self) -> str:
        origin = re.sub(r"[^ -~]", "?", self.origin).replace("\\", "/")
        origin = origin.replace('"', "'")
        paragraphs: list[str | list[str]] = [
            f"Generated from {origin}. Review it, then release it with `piceli "
            'release plan --adopt-all-desired` (see "From kubectl scripts to '
            'Piceli" in the Piceli documentation). Fields the typed API does not '
            'cover yet are kept with `app.override(...)` under a "not typed" '
            "comment.",
        ]
        if self.secret_inputs:
            paragraphs.append(
                "Secret values were not copied. Declare these generators in "
                "release.toml; each imports the live value on the first release:"
            )
            for item in self.secret_inputs:
                paragraphs.append(item.toml().splitlines())
        mounted = sorted(
            {
                claim
                for target in self.targets.values()
                if target["kind"] == "Deployment"
                for claim in _claims(target)
            }
        )
        if mounted:
            paragraphs.append(
                "Existing claims (mounted, never managed): " + ", ".join(mounted) + "."
            )
        unmounted = sorted(set(self.claims) - set(mounted))
        if unmounted:
            paragraphs.append(
                "Claims that no imported Deployment mounts (left out): "
                + ", ".join(unmounted)
                + "."
            )
        pinned = [item for item in self.images_seen if item.running]
        if pinned:
            paragraphs.append(
                "Running images. To pin one by digest, add it to [images] and use "
                "ctx.image(name); that changes the pod template, so it rolls out:"
            )
            taken: set[str] = set()
            paragraphs.append(
                [
                    f"{_image_name(item, taken)} = {_toml(str(item.running))}"
                    for item in pinned
                ]
            )
        if self.skipped:
            paragraphs.append(
                "Skipped: "
                + "; ".join(
                    f"{item.kind} {item.name} ({item.reason})"
                    for item in sorted(self.skipped, key=lambda s: (s.kind, s.name))
                )
                + "."
            )
        result = [
            f'"""Typed app for namespace {self.namespace}, imported by '
            f"`piceli import {self.source}`."
        ]
        for paragraph in paragraphs:
            result.append("")
            if isinstance(paragraph, list):
                result += ["    " + line for line in paragraph]
            else:
                result += textwrap.wrap(
                    paragraph, width=79, break_long_words=False, break_on_hyphens=False
                )
        result.append('"""')
        return "\n".join(result)

    def run(self) -> ImportResult:
        self.build()
        try:
            self._compute_overrides(self._typed_pass())
        except ImportFailure:
            raise
        except Exception as error:
            raise ImportFailure(
                "import-roundtrip-mismatch",
                f"the typed declarations do not render ({type(error).__name__}: {error})",
            ) from None
        source = self.module()
        namespace: dict[str, Any] = {"__name__": "_piceli_import_check"}
        try:
            exec(compile(source, "<piceli-import>", "exec"), namespace)
            composition = namespace["build"](self._context())
        except Exception as error:
            raise ImportFailure(
                "import-roundtrip-mismatch",
                f"the generated module does not render ({type(error).__name__}: {error})",
            ) from None
        final = {
            (resource.ref.kind, resource.ref.name): resource.manifest
            for component in composition.components
            for resource in component.resources
        }
        objects: list[ImportedObject] = []
        for decl in self.decls:
            if decl.expr is None:
                continue
            key = (decl.kind, decl.name)
            leftover: list[str] = []
            if (
                key not in final
                or diff(final[key], self.targets[key], decl.kind, notes=leftover)
                is not SAME
            ):
                raise ImportFailure(
                    "import-roundtrip-mismatch",
                    f"{decl.kind}/{decl.name} does not render to the imported fields: "
                    f"{leftover or ['missing']}",
                )
            objects.append(
                ImportedObject(
                    decl.kind,
                    decl.name,
                    "typed" if decl.typed else "raw",
                    tuple(decl.notes),
                    decl.reason,
                )
            )
        for name in sorted(self.claims):
            objects.append(
                ImportedObject("PersistentVolumeClaim", name, "existing-claim")
            )
        return ImportResult(
            module=source,
            app=self.app_name,
            namespace=self.namespace,
            source=self.source,
            objects=tuple(objects),
            secrets=tuple(self.secret_inputs),
            skipped=tuple(sorted(self.skipped, key=lambda s: (s.kind, s.name))),
            images=tuple(self.images_seen),
        )


def generate(
    manifests: Sequence[Mapping[str, Any]],
    *,
    app_name: str,
    namespace: str,
    source: str,
    origin: str,
    running_images: Mapping[tuple[str, str], str] | None = None,
    skipped: Iterable[Skipped] = (),
) -> ImportResult:
    """Generate the typed module for scrubbed ``manifests`` (see :func:`~piceli.importing.clean.scrub`).

    :param app_name: The ``App`` name (a DNS label).
    :param namespace: The namespace the app renders into.
    :param source: ``live`` or ``yaml`` (named in the module docstring).
    :param origin: Where the manifests came from, in words.
    :param running_images: ``{(deployment, container): "repo@sha256:…"}``.
    :param skipped: Objects left out before generation, with a reason.
    :raises ImportFailure: ``import-roundtrip-mismatch`` when the module does
        not reproduce every imported field.
    """
    for manifest in manifests:
        if manifest.get("kind") == "Secret" and (
            "stringData" in manifest
            or any(value != PRIVATE for value in (manifest.get("data") or {}).values())
        ):
            raise ValueError("secret values must be scrubbed before generation")
    return _Generator(
        manifests,
        app_name=app_name,
        namespace=namespace,
        source=source,
        origin=origin,
        images=running_images or {},
        skipped=skipped,
    ).run()


def _claims(deployment: Mapping[str, Any]) -> list[str]:
    pod = deployment.get("spec", {}).get("template", {}).get("spec", {})
    return [
        str(volume["persistentVolumeClaim"]["claimName"])
        for volume in pod.get("volumes") or []
        if isinstance(volume, dict)
        and isinstance(volume.get("persistentVolumeClaim"), dict)
        and "claimName" in volume["persistentVolumeClaim"]
    ]


def _image_name(item: RunningImage, taken: set[str]) -> str:
    base = (
        item.deployment
        if item.container == item.deployment
        else f"{item.deployment}-{item.container}"
    )
    base = _SECRET_INPUT.sub("-", base.lower()).strip("-_")
    base = (base if base[:1].isalpha() else "i-" + base)[:60]
    name, number = base, 2
    while name in taken:
        name = f"{base[:57]}-{number}"
        number += 1
    taken.add(name)
    return name


def _canonical(labels: Any) -> str:
    import json

    return json.dumps(labels, sort_keys=True)


def _port(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1 <= value <= 65535
    return isinstance(value, str) and _port_name(value)


def _port_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 15
        and bool(_PORT_NAME.fullmatch(value))
        and bool(re.search(r"[a-z]", value))
        and "--" not in value
    )


def _port_name_like(value: str) -> bool:
    return (
        bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)) and len(value) <= 63
    )


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _items(items: Any) -> dict[str, str] | bool | None:
    if items is None:
        return None
    if not isinstance(items, list):
        return False
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"key", "path"}:
            return False
        result[item["key"]] = item["path"]
    return result
