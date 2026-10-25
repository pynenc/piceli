"""A client bundle as a Helm chart: one command installs it, one removes it.

``piceli bundle TARGET --helm`` turns the same bundle (the same render, after
the same safety gate for foreign clusters) into a Helm 3 chart instead of a
kustomize base and ``prepare.sh``:

- **Settings.** Each key of the app's ConfigMaps is a top-level value named
  in camelCase (``cluster-id`` becomes ``clusterId``), or only the
  keys a :class:`HelmChart` names. A required setting fails the install with
  a message naming it (Helm's ``required``).
- **Secrets.** Each Secret the app declares is rendered by the chart, never
  carried in it. Values Piceli can generate (``Random``, ``Static``,
  ``Template``) are generated at install like ``prepare.sh`` does and kept
  across ``helm upgrade`` (read back with ``lookup``); any other source (an
  import) is a value the client passes with ``--set-file NAME=FILE``, kept
  across upgrades when not passed again.
- **Images** stay pinned by digest; ``image.registry`` replaces the
  registry host of every image for a mirror; ``image.pullSecrets`` names
  pull Secrets.
- **Storage.** ``storageClass`` sets the class of every claim;
  ``deleteDataOnUninstall`` makes ``helm uninstall`` delete the claims of
  StatefulSets (their ``persistentVolumeClaimRetentionPolicy``); by default
  they stay.
- **Toggles.** A :class:`HelmToggle` is a boolean value that patches named
  objects when true (``relayOnly: true`` narrowing an egress rule).
- **Optional objects** (0.31.0). A :class:`HelmOptional` is a boolean value
  (``intake.expose``) that renders named objects only when true.
- **Values in objects** (0.31.0). A :class:`HelmValue` is a value (a list,
  a string, a number) written at JSON pointers of named objects
  (``intake.allowNamespaces`` as a NetworkPolicy's namespace list).
- **An existing Secret** (0.31.0). ``existingSecret=NAME``: the chart makes
  no Secret and every workload reads its secret keys from the client's
  Secret ``NAME`` (it holds every key the README lists). Nothing is
  generated, so ``helm template`` and Argo CD render the same every time.
- **Storage size** (0.31.0). ``storage.size`` sets the size of every claim;
  ``HelmChart(storage_env={"DISK_LIMIT": 90})`` sets an environment variable
  the workloads declare to that percentage of it, in bytes.

Cluster-scoped objects (ClusterRoles and their bindings) are part of the
release, so ``helm uninstall`` removes them. The chart is deterministic: the
same bundle gives the same files, archive bytes and OCI digest.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.bundle.build import (
    BUNDLE_ANNOTATION,
    PART_OF,
    Bundle,
    BundleError,
    BundleObject,
)

#: Top-level values every chart may have; a setting, input or toggle cannot
#: take these names.
RESERVED = frozenset(
    {"global", "image", "storageClass", "deleteDataOnUninstall", "storage",
     "existingSecret"}
)  # fmt: skip
#: 0.31.0: a Kubernetes quantity ``storage.size`` takes (whole numbers).
SIZE = re.compile(r"^([0-9]+(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?)?$")
VALUE_NAME = re.compile(r"^[a-z][A-Za-z0-9]{0,62}$")
SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"
SECRETS_TEMPLATE = "templates/piceli-secrets.yaml"
CONFIG_DIGEST = "piceli.io/configmaps-digest"
SECRET_DIGEST = "piceli.io/secrets-digest"
NAMESPACE_ANNOTATION = "piceli.io/namespace"
_SCALAR = re.compile(r"'?__piceli_helm_(\d+)__'?")
_KEY = re.compile(r"^(?P<indent> *)'?~~piceli_helm_(?P<site>\d+)'?: null$", re.M)
_TOKEN = re.compile(r"\{\{|\}\}|\{(?:secret:)?([a-z][a-z0-9_.-]{0,190})\}")
_POD_PATHS = {
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}


def value_name(key: str) -> str:
    """The camelCase value name of a ConfigMap key or generator name."""
    parts = [part for part in re.split(r"[-_.]+", key) if part]
    if not parts:
        return ""
    return (
        parts[0][:1].lower()
        + parts[0][1:]
        + "".join(part[:1].upper() + part[1:] for part in parts[1:])
    )


def _check_name(name: str, what: str) -> str:
    if not VALUE_NAME.fullmatch(name) or name in RESERVED:
        raise BundleError(
            "bundle-invalid",
            f"{what} {name!r} cannot be a chart value: use camelCase letters and "
            f"digits, not one of {sorted(RESERVED)}",
        )
    return name


# ------------------------------------------------------------ declarations


def _object_key(item: Any) -> str:
    if isinstance(item, str):
        kind, sep, name = item.partition("/")
        if not sep or not kind or not name:
            raise ValueError(f"name objects as Kind/name, got {item!r}")
        return item
    from piceli.app.app import kind_of

    name = getattr(item, "name", None)
    if not isinstance(name, str):
        raise ValueError(f"{type(item).__name__} is not a declared object")
    return f"{kind_of(item)}/{name}"


@dataclass(frozen=True)
class HelmToggle:
    """A boolean chart value that patches named objects when it is true.

    ``patches`` pairs a declared object (or ``"Kind/name"``) with a patch of
    its rendered manifest, as a list of pairs or a mapping keyed by
    ``"Kind/name"``: a mapping patch merges like :meth:`App.override
    <piceli.app.App.override>`; a list is a JSON Patch (RFC 6902: ``add``,
    ``remove``, ``replace`` and ``test``). Both renders pass the safety gate.

    Example::

        HelmToggle("UDP only to the relay", patches=[(policy, [
            {"op": "replace", "path": "/spec/egress/1/ports", "value": [...]},
        ])])
    """

    description: str
    patches: Sequence[tuple[Any, Any]] | Mapping[str, Any]
    default: bool = False

    def resolved(self) -> dict[str, Any]:
        """``"Kind/name"`` → patch (plain JSON values)."""
        result: dict[str, Any] = {}
        pairs = (
            list(self.patches.items())
            if isinstance(self.patches, Mapping)
            else list(self.patches)
        )
        for item, patch in pairs:
            key = _object_key(item)
            if not isinstance(patch, Mapping | list | tuple):
                raise ValueError(f"the patch of {key} must be a mapping or a list")
            result[key] = json.loads(json.dumps(patch))
        if not result:
            raise ValueError("a toggle patches at least one object")
        return result


def _check_path(name: str, what: str) -> tuple[str, ...]:
    """The parts of a dotted value name (``intake.expose``), each camelCase;
    the first is not a reserved value."""
    parts = tuple(name.split("."))
    if (
        not 1 <= len(parts) <= 3
        or not all(VALUE_NAME.fullmatch(part) for part in parts)
        or parts[0] in RESERVED
    ):
        raise BundleError(
            "bundle-invalid",
            f"{what} {name!r} cannot be a chart value: up to three camelCase "
            f"names joined by dots, not starting with one of {sorted(RESERVED)}",
        )
    return parts


@dataclass(frozen=True)
class HelmOptional:
    """A boolean chart value that renders named objects only when true
    (0.31.0). The objects are part of the app, so the safety gate checks
    them; with the value false (``default``) they are not installed.

    Example::

        HelmOptional("Expose the intake to other namespaces",
                     objects=[intake_service, intake_policy])
    """

    description: str
    objects: Sequence[Any]
    default: bool = False

    def keys(self) -> list[str]:
        """The objects as ``"Kind/name"``."""
        keys = [_object_key(item) for item in self.objects]
        if not keys:
            raise ValueError("an optional value names at least one object")
        return keys


@dataclass(frozen=True)
class HelmValue:
    """A chart value written into named objects (0.31.0).

    ``targets`` pairs a declared object (or ``"Kind/name"``) with a JSON
    pointer of its rendered manifest; the value replaces what is there.
    ``default`` (when ``None``: the rendered value at the first target) and
    its JSON type (``array``, ``string``, ``integer``, ``number``,
    ``boolean``, ``object``) go to ``values.yaml`` and the schema.

    Example::

        HelmValue("Namespaces whose pods may send", targets=[(
            policy, "/spec/ingress/0/from/0/namespaceSelector/matchExpressions/0/values",
        )])
    """

    description: str
    targets: Sequence[tuple[Any, str]]
    default: Any = None

    def resolved(self) -> list[tuple[str, list[str]]]:
        """``("Kind/name", pointer parts)`` of each target."""
        result = [(_object_key(item), _pointer(path)) for item, path in self.targets]
        if not result or any(not parts for _key, parts in result):
            raise ValueError("a value names at least one object and a non-empty path")
        return result


@dataclass(frozen=True)
class HelmChart:
    """How ``piceli bundle --helm`` names the values of a client chart.

    Point ``piceli bundle`` at this object instead of the Pipeline or App it
    wraps (``piceli bundle app.py:chart --helm --out DIR``).

    :param target: The :class:`~piceli.pipeline.Pipeline` or
        :class:`~piceli.app.App` to bundle.
    :param settings: Value name → ConfigMap key (``config.key("org")``);
        default: every key of every ConfigMap, named in camelCase.
    :param required: Values the client must set (settings or toggles are
        checked at install with Helm's ``required``). Inputs (imported
        secrets) are always required on the first install.
    :param toggles: Value name → :class:`HelmToggle`.
    :param descriptions: Value name → one line for ``values.yaml`` and the
        README.
    :param optional: 0.31.0: dotted value name (``intake.expose``) →
        :class:`HelmOptional`.
    :param values: 0.31.0: dotted value name (``intake.allowNamespaces``)
        → :class:`HelmValue`.
    :param storage_env: 0.31.0: environment variable → percent: when the
        client sets ``storage.size``, each container that declares the
        variable gets that percentage of the size, in bytes
        (``{"DISK_LIMIT": 90}``); otherwise it keeps its declared value.
    """

    target: Any
    settings: Mapping[str, Any] | None = None
    required: Sequence[str] = ()
    toggles: Mapping[str, HelmToggle] = field(default_factory=dict)
    descriptions: Mapping[str, str] = field(default_factory=dict)
    optional: Mapping[str, HelmOptional] = field(default_factory=dict)
    values: Mapping[str, HelmValue] = field(default_factory=dict)
    storage_env: Mapping[str, int] = field(default_factory=dict)

    def setting_keys(self) -> dict[str, tuple[str, str]] | None:
        """Value name → ``(ConfigMap name, key)``, or ``None`` for the default."""
        if self.settings is None:
            return None
        result = {}
        for name, ref in self.settings.items():
            config, key = getattr(ref, "config", None), getattr(ref, "key", None)
            if not isinstance(config, str) or not isinstance(key, str):
                raise ValueError(
                    f"setting {name!r} must name a ConfigMap key (config.key(...))"
                )
            result[name] = (config, key)
        return result


# --------------------------------------------------------------- patching


def _pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise ValueError(f"a JSON pointer starts with '/', got {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _walk(document: Any, parts: Sequence[str]) -> Any:
    for part in parts:
        document = document[_index(document, part)]
    return document


def _index(container: Any, part: str) -> Any:
    if not isinstance(container, list):
        return part
    if not part.isdigit():
        raise IndexError(part)
    return int(part)


class _NotApplied(ValueError):
    """A JSON Patch operation that does not apply."""


def _apply(result: Any, op: Any, path: str, parts: list[str], value: Any) -> None:
    if op == "test":
        if _walk(result, parts) != value:
            raise _NotApplied(f"test failed at {path}")
        return
    if not parts:
        raise _NotApplied("a JSON Patch cannot replace the whole object")
    parent, last = _walk(result, parts[:-1]), parts[-1]
    if op == "add" and isinstance(parent, list):
        parent.insert(len(parent) if last == "-" else _index(parent, last), value)
    elif op == "add":
        parent[last] = value
    elif op == "remove":
        del parent[_index(parent, last)]
    elif op == "replace":
        key = _index(parent, last)
        if isinstance(parent, dict) and key not in parent:
            raise KeyError(key)
        parent[key] = value
    else:
        raise _NotApplied(f"unsupported JSON Patch op {op!r}")


def json_patch(document: Any, operations: Sequence[Mapping[str, Any]]) -> Any:
    """``document`` with RFC 6902 ``add``/``remove``/``replace``/``test`` applied.

    :raises ValueError: an operation does not apply (a failed ``test``, a
        missing path, an unsupported ``op``).
    """
    result = copy.deepcopy(document)
    for operation in operations:
        op, path = operation.get("op"), operation.get("path")
        if not isinstance(path, str):
            raise ValueError("every JSON Patch operation needs a path")
        value = copy.deepcopy(operation.get("value"))
        try:
            _apply(result, op, path, _pointer(path), value)
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError(f"{op} {path} does not apply") from error
    return result


def apply_toggle(
    components: Sequence[Mapping[str, Any]], name: str, toggle: HelmToggle
) -> list[dict[str, Any]]:
    """The render with ``toggle``'s patches applied (the input is unchanged).

    :raises BundleError: ``bundle-invalid`` when a patch names no rendered
        object or does not apply.
    """
    from piceli.app.app import merge_override

    try:
        patches = toggle.resolved()
    except ValueError as error:
        raise BundleError("bundle-invalid", f"toggle {name}: {error}") from None
    result = copy.deepcopy([dict(item) for item in components])
    found: set[str] = set()
    for component in result:
        for resource in component["resources"]:
            manifest = resource["manifest"]
            key = (
                f"{manifest.get('kind')}/{(manifest.get('metadata') or {}).get('name')}"
            )
            patch = patches.get(key)
            if patch is None:
                continue
            found.add(key)
            try:
                if isinstance(patch, list):
                    resource["manifest"] = json_patch(manifest, patch)
                else:
                    resource["manifest"] = merge_override(manifest, patch)
            except ValueError as error:
                raise BundleError(
                    "bundle-invalid", f"toggle {name}: {key}: {error}"
                ) from None
    missing = sorted(set(patches) - found)
    if missing:
        raise BundleError(
            "bundle-invalid", f"toggle {name} patches {missing}, which the render lacks"
        )
    return result


# ------------------------------------------------------------- templating


@dataclass(frozen=True)
class _Site:
    """One place the chart fills: ``expression`` (a scalar) or an optional key."""

    expression: str
    key: str = ""  # an optional key: rendered only when ``expression`` is set
    body: str = "{{ toJson . }}"


def _literal(text: str) -> str:
    """A Go template string literal (JSON quoting is a subset of Go's)."""
    return json.dumps(text, ensure_ascii=False)


def _dig(value: Any, path: Sequence[str]) -> Any:
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _image_path(image: str) -> str:
    """``image`` without its registry host, as a mirror serves it."""
    first, sep, rest = image.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return rest
    return image if sep else f"library/{image}"


class _Templater:
    """Turns bundle manifests into template text, one site at a time."""

    def __init__(
        self,
        name: str,
        namespace: str,
        settings: Mapping[tuple[str, str], str],
        required: set[str],
        storage_class: str | None,
        *,
        secret_names: frozenset[str] = frozenset(),
        values_at: Mapping[str, Sequence[tuple[list[str], str]]] | None = None,
        storage_env: Mapping[str, int] | None = None,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.settings = settings
        self.required = required
        self.storage_class = storage_class
        #: 0.31.0: the app's Secrets ``existingSecret`` replaces (empty: no
        #: ``existingSecret`` value).
        self.secret_names = secret_names
        #: 0.31.0: ``"Kind/name"`` → ``(pointer parts, value path)``.
        self.values_at = dict(values_at or {})
        self.storage_env = dict(storage_env or {})
        self.placed: set[tuple[str, int]] = set()  # (value path, target number)
        self.env_set: set[str] = set()
        self.sites: list[_Site] = []
        self.claims = False  # a StatefulSet with claim templates

    def scalar(self, expression: str) -> str:
        self.sites.append(_Site(expression))
        return f"__piceli_helm_{len(self.sites) - 1}__"

    def optional(
        self, mapping: dict[str, Any], key: str, expression: str, body: str = ""
    ) -> None:
        mapping.pop(key, None)
        self.sites.append(_Site(expression, key, body or "{{ toJson . }}"))
        mapping[f"~~piceli_helm_{len(self.sites) - 1}"] = None

    def setting(self, value: str) -> str:
        if value in self.required:
            message = f"{value} is required: --set {value}=..."
            return self.scalar(
                f"{{{{ required {_literal(message)} $.Values.{value} | toString | toJson }}}}"
            )
        return self.scalar(
            f'{{{{ $.Values.{value} | default "" | toString | toJson }}}}'
        )

    def manifest(self, item: BundleObject) -> dict[str, Any]:
        manifest = copy.deepcopy(dict(item.manifest))
        kind = str(manifest.get("kind"))
        metadata = manifest.setdefault("metadata", {})
        labels = metadata.setdefault("labels", {})
        labels["helm.sh/chart"] = self.scalar(
            '{{ printf "%s-%s" $.Chart.Name ($.Chart.Version | replace "+" "_") | toJson }}'
        )
        labels["app.kubernetes.io/managed-by"] = self.scalar(
            "{{ $.Release.Service | toJson }}"
        )
        annotations = metadata.get("annotations") or {}
        if annotations.get(NAMESPACE_ANNOTATION) == self.namespace:
            annotations[NAMESPACE_ANNOTATION] = self.namespace_site()
        if item.cluster:
            metadata["name"] = self.cluster_name(str(metadata.get("name")))
        if kind in {"ClusterRoleBinding", "RoleBinding"}:
            role = manifest.get("roleRef") or {}
            if role.get("kind") == "ClusterRole" and isinstance(role.get("name"), str):
                role["name"] = self.cluster_name(role["name"])
            for subject in manifest.get("subjects") or ():
                if isinstance(subject, dict) and subject.get("namespace") == (
                    self.namespace
                ):
                    subject["namespace"] = self.namespace_site()
        if kind == "ConfigMap":
            data = manifest.get("data") or {}
            config = str(metadata.get("name"))
            for key in sorted(data):
                value = self.settings.get((config, key))
                if value is not None:
                    data[key] = self.setting(value)
        if kind == "PersistentVolumeClaim":
            self.storage(manifest.setdefault("spec", {}))
        pod_path = _POD_PATHS.get(kind)
        if pod_path is not None:
            self.workload(manifest, pod_path)
        self.place_values(f"{kind}/{metadata.get('name')}", manifest)
        return self.escape(manifest)

    def place_values(self, key: str, manifest: dict[str, Any]) -> None:
        """Each :class:`HelmValue` aimed at ``key``, at its pointer."""
        for number, (parts, value) in enumerate(self.values_at.get(key, ())):
            try:
                parent = _walk(manifest, parts[:-1])
                last = parts[-1]
                if isinstance(parent, list):
                    index = int(last)
                    if not 0 <= index < len(parent):
                        raise IndexError(last)
                    parent[index] = self.scalar(f"{{{{ toJson $.Values.{value} }}}}")
                elif isinstance(parent, dict) and last in parent:
                    parent[last] = self.scalar(f"{{{{ toJson $.Values.{value} }}}}")
                else:
                    raise KeyError(last)
            except (KeyError, IndexError, TypeError, ValueError):
                raise BundleError(
                    "bundle-invalid",
                    f"value {value}: {key} has nothing at /{'/'.join(parts)}",
                ) from None
            self.placed.add((value, number))

    def namespace_site(self) -> str:
        return self.scalar("{{ $.Release.Namespace | toJson }}")

    def cluster_name(self, name: str) -> str:
        prefix = f"{self.namespace}:"
        if not name.startswith(prefix):
            return name
        rest = _literal(name[len(prefix) :])
        return self.scalar(
            f'{{{{ printf "%s:%s" $.Release.Namespace {rest} | toJson }}}}'
        )

    def storage(self, spec: dict[str, Any]) -> None:
        fallback = spec.get("storageClassName") or self.storage_class
        expression = "$.Values.storageClass"
        if fallback:
            expression = f"({expression} | default {_literal(fallback)})"
        self.optional(spec, "storageClassName", expression)
        requests = (spec.get("resources") or {}).get("requests")
        if isinstance(requests, dict) and isinstance(requests.get("storage"), str):
            size = _literal(requests["storage"])
            requests["storage"] = self.scalar(
                f"{{{{ $.Values.storage.size | default {size} | toJson }}}}"
            )

    def workload(self, manifest: dict[str, Any], pod_path: Sequence[str]) -> None:
        spec = manifest.get("spec") or {}
        templates = spec.get("volumeClaimTemplates") or ()
        for template in templates:
            if isinstance(template, dict):
                self.storage(template.setdefault("spec", {}))
        if manifest.get("kind") == "StatefulSet" and templates:
            self.claims = True
            policy = spec.setdefault("persistentVolumeClaimRetentionPolicy", {})
            kept = _literal(str(policy.get("whenDeleted") or "Retain"))
            policy["whenDeleted"] = self.scalar(
                f'{{{{ if $.Values.deleteDataOnUninstall }}}}"Delete"{{{{ else }}}}{kept}{{{{ end }}}}'
            )
            policy.setdefault("whenScaled", "Retain")
        pod = _dig(manifest, pod_path)
        if not isinstance(pod, dict):
            return
        annotations = _dig(manifest, (*pod_path[:-1], "metadata", "annotations"))
        if isinstance(annotations, dict):
            for key, helper in ((CONFIG_DIGEST, "config"), (SECRET_DIGEST, "secrets")):
                if isinstance(annotations.get(key), str):
                    digest = (
                        f'{{{{ printf "sha256:%s" (sha256sum (cat {_literal(annotations[key])} '
                        f'(include "{self.name}.{helper}" $))) | toJson }}}}'
                    )
                    if helper == "secrets" and self.secret_names:
                        # 0.31.0: an existing Secret: its name, never its values.
                        digest = (
                            "{{ if $.Values.existingSecret }}"
                            '{{ printf "sha256:%s" (sha256sum (printf "existing:%s" '
                            "$.Values.existingSecret)) | toJson }}{{ else }}"
                            + digest
                            + "{{ end }}"
                        )
                    annotations[key] = self.scalar(digest)
        if self.secret_names:
            self.existing_secret(pod)
        for group in ("initContainers", "containers"):
            for container in pod.get(group) or ():
                if isinstance(container, dict):
                    self.disk_env(container)
                image = container.get("image") if isinstance(container, dict) else None
                if isinstance(image, str):
                    container["image"] = self.scalar(
                        f'{{{{ include "{self.name}.image" (list {_literal(image)} '
                        f"{_literal(_image_path(image))} $.Values.image.registry) | toJson }}}}"
                    )
        if "imagePullSecrets" not in pod:
            self.optional(
                pod,
                "imagePullSecrets",
                "$.Values.image.pullSecrets",
                "{{ $names := list }}{{ range . }}"
                '{{ $names = append $names (dict "name" .) }}{{ end }}'
                "{{ toJson $names }}",
            )

    def _secret(self, name: Any) -> Any:
        if not isinstance(name, str) or name not in self.secret_names:
            return name
        return self.scalar(
            f"{{{{ $.Values.existingSecret | default {_literal(name)} | toJson }}}}"
        )

    def existing_secret(self, pod: dict[str, Any]) -> None:
        """Every reference to an app Secret reads ``existingSecret`` when set."""
        for group in ("initContainers", "containers"):
            for container in pod.get(group) or ():
                if not isinstance(container, dict):
                    continue
                for entry in container.get("env") or ():
                    ref = _dig(entry, ("valueFrom", "secretKeyRef"))
                    if isinstance(ref, dict):
                        ref["name"] = self._secret(ref.get("name"))
                for entry in container.get("envFrom") or ():
                    ref = entry.get("secretRef") if isinstance(entry, dict) else None
                    if isinstance(ref, dict):
                        ref["name"] = self._secret(ref.get("name"))
        for volume in pod.get("volumes") or ():
            if not isinstance(volume, dict):
                continue
            secret = volume.get("secret")
            if isinstance(secret, dict):
                secret["secretName"] = self._secret(secret.get("secretName"))
            for source in _dig(volume, ("projected", "sources")) or ():
                ref = source.get("secret") if isinstance(source, dict) else None
                if isinstance(ref, dict):
                    ref["name"] = self._secret(ref.get("name"))

    def disk_env(self, container: dict[str, Any]) -> None:
        """``storage_env``: the declared variable, or a share of ``storage.size``."""
        for entry in container.get("env") or ():
            if not isinstance(entry, dict):
                continue
            percent = self.storage_env.get(str(entry.get("name")))
            if percent is None or not isinstance(entry.get("value"), str):
                continue
            declared = _literal(entry["value"])
            entry["value"] = self.scalar(
                "{{ if $.Values.storage.size }}"
                f'{{{{ include "{self.name}.percentOf" (list $.Values.storage.size '
                f"{int(percent)}) | toJson }}}}"
                f"{{{{ else }}}}{{{{ toJson {declared} }}}}{{{{ end }}}}"
            )
            self.env_set.add(str(entry["name"]))

    def escape(self, value: Any) -> Any:
        if isinstance(value, dict):
            for key in list(value):
                if isinstance(key, str) and "{{" in key:
                    raise BundleError(
                        "bundle-invalid",
                        "a mapping key holds '{{'; Helm cannot escape it",
                    )
                value[key] = self.escape(value[key])
            return value
        if isinstance(value, list):
            return [self.escape(item) for item in value]
        if isinstance(value, str) and "{{" in value and not _SCALAR.fullmatch(value):
            return self.scalar("{{ " + _literal(value) + " | toJson }}")
        return value

    def text(self, manifest: Mapping[str, Any]) -> str:
        import yaml

        body = yaml.safe_dump(dict(manifest), sort_keys=True, width=100000)
        body = _KEY.sub(self._key_text, body)
        return _SCALAR.sub(
            lambda match: self.sites[int(match.group(1))].expression, body
        )

    def _key_text(self, match: re.Match[str]) -> str:
        indent = match.group("indent")
        site = self.sites[int(match.group("site"))]
        return (
            f"{indent}{{{{- with {site.expression} }}}}\n"
            f"{indent}{site.key}: {site.body}\n"
            f"{indent}{{{{- end }}}}"
        )


# ------------------------------------------------------------ the secrets


def _secret_helper(name: str, bundle: Bundle, inputs: Mapping[str, str]) -> str:
    """``<name>.secrets``: every generator output as JSON, kept across upgrades."""
    from piceli.bundle.prepare import required_generators
    from piceli.k8s.release_secret_spec import (
        ImportSecretSpec,
        RandomSecretSpec,
        StaticSecretSpec,
        TemplateSecretSpec,
        TlsCaSpec,
        TlsSelfSignedSpec,
    )

    generators = bundle.generators
    order = required_generators(bundle.secrets, generators)
    where: dict[str, tuple[int, str]] = {}
    for number, plan in enumerate(bundle.secrets):
        for key, output in plan.keys:
            where.setdefault(output, (number, key))
    lines = [
        f'{{{{- define "{name}.secrets" -}}}}',
        "{{- $out := dict -}}",
        '{{- $v := "" -}}',
    ]
    for number, plan in enumerate(bundle.secrets):
        lines.append(
            f'{{{{- $s{number} := (lookup "v1" "Secret" .Release.Namespace '
            f"{_literal(plan.name)}) | default dict -}}}}"
        )
        lines.append(
            f'{{{{- $d{number} := (get $s{number} "data") | default dict -}}}}'
        )

    def kept(output: str) -> list[str]:
        if output not in where:
            return []
        number, key = where[output]
        return [
            f"{{{{- if and (not $v) (hasKey $d{number} {_literal(key)}) -}}}}"
            f"{{{{- $v = get $d{number} {_literal(key)} | b64dec -}}}}{{{{- end -}}}}"
        ]

    for generator in order:
        spec = generators[generator]
        out = _literal(generator)
        if isinstance(spec, TlsCaSpec | TlsSelfSignedSpec):
            raise BundleError(
                "bundle-secret-unsupported",
                f"generator {generator!r} makes TLS keys, which a Helm chart "
                "does not generate; use the kustomize bundle (prepare.sh) or "
                "an imported certificate",
            )
        lines.append('{{- $v = "" -}}')
        if isinstance(spec, RandomSecretSpec):
            lines += kept(generator)
            lines.append(
                f'{{{{- if not $v -}}}}{{{{- $v = randBytes {spec.bytes} | replace "+" "-" '
                '| replace "/" "_" | trimAll "=" -}}{{- end -}}'
            )
        elif isinstance(spec, StaticSecretSpec):
            lines.append(f"{{{{- $v = {_literal(spec.value)} -}}}}")
        elif isinstance(spec, TemplateSecretSpec):
            parts = []
            position = 0
            for match in _TOKEN.finditer(spec.template):
                if match.start() > position:
                    parts.append(_literal(spec.template[position : match.start()]))
                token = match.group(0)
                if token in {"{{", "}}"}:
                    parts.append(_literal(token[0]))
                else:
                    parts.append(f"(get $out {_literal(match.group(1))})")
                position = match.end()
            if position < len(spec.template):
                parts.append(_literal(spec.template[position:]))
            lines.append(f"{{{{- $v = print {' '.join(parts) or _literal('')} -}}}}")
        else:
            value = inputs[generator]
            lines.append(f'{{{{- $v = $.Values.{value} | default "" | toString -}}}}')
            if isinstance(spec, ImportSecretSpec) and spec.trim_newline:
                lines.append('{{- $v = regexReplaceAll "\\\\s+$" $v "" -}}')
            lines += kept(generator)
            message = (
                f"{value} is required on the first install: --set-file {value}=FILE"
            )
            lines.append(
                f"{{{{- if not $v -}}}}{{{{- fail {_literal(message)} -}}}}{{{{- end -}}}}"
            )
        lines.append(f"{{{{- $_ := set $out {out} $v -}}}}")
    lines += ["{{- toJson $out -}}", "{{- end -}}", ""]
    return "\n".join(lines)


def _secrets_template(name: str, bundle: Bundle) -> str:
    import yaml

    documents = [f'{{{{- $values := include "{name}.secrets" $ | fromJson }}}}']
    for plan in bundle.secrets:
        header = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": plan.name,
                "labels": {PART_OF: bundle.name},
                "annotations": {BUNDLE_ANNOTATION: f"{bundle.name}@{bundle.version}"},
            },
            "type": plan.type,
        }
        text = yaml.safe_dump(header, sort_keys=True)
        text = text.replace(
            "  labels:\n",
            "  labels:\n"
            "    app.kubernetes.io/managed-by: {{ $.Release.Service | toJson }}\n"
            '    helm.sh/chart: {{ printf "%s-%s" $.Chart.Name ($.Chart.Version '
            '| replace "+" "_") | toJson }}\n',
            1,
        )
        data = "".join(
            f"  {json.dumps(key)}: {{{{ get $values {_literal(output)} | b64enc | toJson }}}}\n"
            for key, output in plan.keys
        )
        documents.append("---\n" + text + "data:\n" + data)
    return "\n".join(documents)


# ------------------------------------------------------------------ chart


@dataclass(frozen=True)
class HelmBundle:
    """A client chart built from a bundle (see :func:`build_helm`)."""

    name: str
    version: str
    app_version: str
    files: dict[str, bytes] = field(repr=False)
    settings: dict[str, tuple[str, str]]
    inputs: dict[str, str]  # generator → value name
    toggles: tuple[str, ...]
    required: tuple[str, ...]
    cluster_objects: tuple[str, ...]
    claims: bool
    #: 0.31.0: the optional-object values, the values written into objects,
    #: and the keys an ``existingSecret`` holds (empty: not offered).
    optional: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    existing_secret_keys: tuple[str, ...] = ()

    def metadata(self) -> dict[str, Any]:
        return {
            "apiVersion": "v2",
            "name": self.name,
            "version": self.version,
            "appVersion": self.app_version,
            "type": "application",
            "description": f"{self.name}, packaged by piceli bundle for a client's cluster",
        }

    @property
    def archive_name(self) -> str:
        return f"{self.name}-{self.version}.tgz"

    @property
    def tag(self) -> str:
        return self.version.replace("+", "_")

    def archive(self) -> bytes:
        """The chart archive ``helm package`` would write; byte-identical per chart."""
        from piceli.gitops import ManifestFile, gzip_bytes, tar_bytes

        return gzip_bytes(
            tar_bytes(
                [
                    ManifestFile(f"{self.name}/{path}", body)
                    for path, body in sorted(self.files.items())
                ]
            )
        )

    def oci(self) -> Any:
        """The OCI layout of ``helm push`` (Helm config and one chart layer)."""
        from piceli.chart import helm_oci

        return helm_oci(self.metadata(), self.archive(), self.archive_name)

    def public(self) -> dict[str, Any]:
        return {
            "chart": self.name,
            "version": self.version,
            "app_version": self.app_version,
            "settings": sorted(self.settings),
            "inputs": sorted(self.inputs.values()),
            "toggles": list(self.toggles),
            "required_values": list(self.required),
            "cluster_objects": list(self.cluster_objects),
            "optional": list(self.optional),
            "values": list(self.values),
            "existing_secret_keys": list(self.existing_secret_keys),
        }


def _settings(
    bundle: Bundle, declared: Mapping[str, tuple[str, str]] | None
) -> dict[str, tuple[str, str]]:
    keys = {
        (item.name, key)
        for item in bundle.objects
        if item.kind == "ConfigMap"
        for key in (item.manifest.get("data") or {})
    }
    result: dict[str, tuple[str, str]] = {}
    if declared is None:
        for config, key in sorted(keys):
            name = _check_name(value_name(key), f"ConfigMap {config} key {key!r} as")
            if name in result:
                raise BundleError(
                    "bundle-invalid",
                    f"two ConfigMap keys become the value {name!r}; name the "
                    "settings with HelmChart(settings=...)",
                )
            result[name] = (config, key)
        return result
    for name, ref in declared.items():
        _check_name(name, "setting")
        if ref not in keys:
            raise BundleError(
                "bundle-invalid",
                f"setting {name!r} names ConfigMap {ref[0]} key {ref[1]!r}, which "
                "the bundle lacks",
            )
        result[name] = ref
    return result


def build_helm(
    bundle: Bundle,
    *,
    variants: Mapping[str, Bundle] | None = None,
    declaration: HelmChart | None = None,
    app_version: str | None = None,
) -> HelmBundle:
    """The chart of ``bundle``; ``variants`` are the bundles of each toggle.

    :raises BundleError: ``bundle-invalid`` (names, toggles, a build image),
        ``bundle-secret-unsupported`` (a TLS generator).
    """
    import yaml

    from piceli.bundle.prepare import client_inputs
    from piceli.chart import _APP_VERSION

    variants = dict(variants or {})
    declared = declaration or HelmChart(bundle.name)
    app_version = bundle.version if app_version is None else app_version
    if not _APP_VERSION.fullmatch(app_version):
        raise BundleError(
            "bundle-invalid", "--app-version must be short printable text"
        )
    if any(item.source == "receipt" for item in bundle.images):
        raise BundleError(
            "bundle-invalid",
            "a Helm chart names its images by digest in a registry; push the "
            "build images and declare them repository@sha256:... (or use the "
            "kustomize bundle with --receipt)",
        )
    try:
        settings = _settings(bundle, declared.setting_keys())
    except ValueError as error:
        raise BundleError("bundle-invalid", str(error)) from None
    inputs = {
        item["generator"]: _check_name(
            value_name(item["generator"]), f"secret input {item['generator']!r} as"
        )
        for item in client_inputs(bundle.secrets, bundle.generators)
    }
    toggles = {
        _check_name(name, "toggle"): toggle for name, toggle in declared.toggles.items()
    }
    try:
        optional = {
            name: (_check_path(name, "optional value"), item, item.keys())
            for name, item in declared.optional.items()
        }
        placed_values = {
            name: (_check_path(name, "value"), item, item.resolved())
            for name, item in declared.values.items()
        }
    except ValueError as error:
        raise BundleError("bundle-invalid", str(error)) from None
    storage_env = {}
    for env, percent in declared.storage_env.items():
        if (
            not isinstance(percent, int)
            or isinstance(percent, bool)
            or not (1 <= percent <= 100)
        ):
            raise BundleError(
                "bundle-invalid", f"storage_env {env}: a percent from 1 to 100"
            )
        storage_env[str(env)] = percent
    names = [*settings, *inputs.values(), *toggles]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise BundleError("bundle-invalid", f"two values are named {duplicated}")
    paths = [parts for parts, _i, _k in (*optional.values(), *placed_values.values())]
    for parts in paths:
        clash = [
            other for other in paths
            if other != parts and other[: len(parts)] == parts
        ] or ([parts] if parts[0] in names else [])  # fmt: skip
        if clash or paths.count(parts) > 1:
            raise BundleError(
                "bundle-invalid", f"the value {'.'.join(parts)} is named twice"
            )
    # 0.31.0: an existing Secret holds every key of the app's Secrets; two
    # Secrets with one key name could not share it, so it is not offered.
    secret_keys = [key for plan in bundle.secrets for key, _output in plan.keys]
    offer_existing = bool(secret_keys) and len(set(secret_keys)) == len(secret_keys)
    unknown = sorted(set(declared.required) - set(settings) - set(toggles))
    if unknown:
        raise BundleError(
            "bundle-invalid",
            f"required names {unknown}, which are not settings or toggles",
        )
    if set(variants) != set(toggles):
        raise BundleError("bundle-invalid", "every toggle needs its rendered variant")
    required = {name for name in declared.required if name in settings}
    reverse = {ref: name for name, ref in settings.items()}
    values_at: dict[str, list[tuple[list[str], str]]] = {}
    for name, (_parts, _item, targets) in placed_values.items():
        for key, pointer in targets:
            values_at.setdefault(key, []).append((pointer, name))
    templater = _Templater(
        bundle.name, bundle.namespace, reverse, required, bundle.storage_class,
        secret_names=frozenset(plan.name for plan in bundle.secrets)
        if offer_existing
        else frozenset(),
        values_at=values_at, storage_env=storage_env,
    )  # fmt: skip
    gated: dict[str, str] = {}
    by_key = {f"{item.kind}/{item.name}": item for item in bundle.objects}
    for name, (_parts, _item, keys) in optional.items():
        for key in keys:
            found = by_key.get(key)
            if found is None:
                raise BundleError(
                    "bundle-invalid",
                    f"optional {name} names {key}, which the render lacks",
                )
            if found.file in gated:
                raise BundleError(
                    "bundle-invalid", f"{key} is optional under two values"
                )
            gated[found.file] = name

    base = {item.file: item for item in bundle.objects}
    order = [item.file for item in bundle.objects]
    changed: dict[str, list[str]] = {}
    for toggle, variant in variants.items():
        other = {item.file: item for item in variant.objects}
        for file in other:
            if file not in base and file not in order:
                order.append(file)
        for file in set(base) | set(other):
            mine, theirs = base.get(file), other.get(file)
            if (mine and mine.manifest) != (theirs and theirs.manifest):
                changed.setdefault(file, []).append(toggle)
    conflicts = sorted(
        file for file, owners in changed.items() if len(owners) > 1 or file in gated
    )
    if conflicts:
        raise BundleError(
            "bundle-invalid", f"two toggles change the same object(s): {conflicts}"
        )

    files: dict[str, bytes] = {}
    cluster_objects: list[str] = []
    for file in order:
        owners = changed.get(file)
        mine = base.get(file)
        if mine is not None and mine.cluster:
            cluster_objects.append(f"{mine.kind}/{mine.name}")
        header = f"# {file} (piceli bundle {bundle.name} {bundle.version})\n"
        if not owners:
            assert mine is not None
            body = templater.text(templater.manifest(mine))
            if file in gated:
                body = f"{{{{- if $.Values.{gated[file]} }}}}\n{body}{{{{- end }}}}\n"
            files[f"templates/{file}"] = (header + body).encode()
            continue
        toggle = owners[0]
        theirs = {item.file: item for item in variants[toggle].objects}.get(file)
        on = templater.text(templater.manifest(theirs)) if theirs else ""
        off = templater.text(templater.manifest(mine)) if mine else ""
        if on and off:
            body = f"{{{{- if $.Values.{toggle} }}}}\n{on}{{{{- else }}}}\n{off}{{{{- end }}}}\n"
        elif on:
            body = f"{{{{- if $.Values.{toggle} }}}}\n{on}{{{{- end }}}}\n"
        else:
            body = f"{{{{- if not $.Values.{toggle} }}}}\n{off}{{{{- end }}}}\n"
        files[f"templates/{file}"] = (header + body).encode()
    missing = sorted(
        name
        for name, (_parts, _item, targets) in placed_values.items()
        for number in range(len(targets))
        if (name, number) not in templater.placed
    )
    if missing:
        raise BundleError(
            "bundle-invalid",
            f"value(s) {sorted(set(missing))} name objects the render lacks",
        )
    unset = sorted(set(storage_env) - templater.env_set)
    if unset:
        raise BundleError(
            "bundle-invalid",
            f"storage_env names {unset}, which no container declares with a value",
        )
    if bundle.secrets:
        body = _secrets_template(bundle.name, bundle)
        if offer_existing:
            body = (
                f"{{{{- if not $.Values.existingSecret }}}}\n{body}\n{{{{- end }}}}\n"
            )
        files[SECRETS_TEMPLATE] = (
            f"# The Secrets of {bundle.name}: generated once, kept across upgrades\n"
            + body
        ).encode()

    descriptions = dict(declared.descriptions)
    values: dict[str, Any] = {}
    schema: dict[str, Any] = {}
    comments: list[str] = []
    for name, (config, key) in sorted(settings.items()):
        default = (
            ""
            if name in required
            else str(
                next(
                    item.manifest["data"][key]
                    for item in bundle.objects
                    if item.kind == "ConfigMap" and item.name == config
                )
            )
        )
        values[name] = default
        text = descriptions.get(name) or f"ConfigMap {config}, key {key}"
        schema[name] = {
            "type": ["string", "number", "integer", "boolean"],
            "description": text,
        }
        comments.append(
            f"# {name}: {text}" + (" (required)" if name in required else "")
        )
    for generator, name in sorted(inputs.items(), key=lambda item: item[1]):
        values[name] = ""
        text = descriptions.get(name) or f"secret input {generator}"
        schema[name] = {"type": "string", "description": text}
        comments.append(
            f"# {name}: {text} (required on the first install: --set-file {name}=FILE)"
        )
    for name, toggle in sorted(toggles.items()):
        values[name] = toggle.default
        text = descriptions.get(name) or toggle.description
        schema[name] = {"type": "boolean", "description": text}
        comments.append(
            f"# {name}: {text}" + (" (required)" if name in required else "")
        )
    tree_values: dict[str, Any] = {}
    tree_schema: dict[str, Any] = {}

    def put(parts: tuple[str, ...], value: Any, node: dict[str, Any]) -> None:
        here_v, here_s = tree_values, tree_schema
        for part in parts[:-1]:
            here_v = here_v.setdefault(part, {})
            group = here_s.setdefault(
                part, {"type": "object", "additionalProperties": False,
                       "properties": {}},
            )  # fmt: skip
            here_s = group["properties"]
        here_v[parts[-1]] = value
        here_s[parts[-1]] = node

    for name, (parts, item, _keys) in sorted(optional.items()):
        text = descriptions.get(name) or item.description
        put(parts, item.default, {"type": "boolean", "description": text})
        comments.append(f"# {name}: {text} (true installs its objects)")
    for name, (parts, item, targets) in sorted(placed_values.items()):
        default = item.default
        if default is None:
            key, pointer = targets[0]
            default = _walk(copy.deepcopy(dict(by_key[key].manifest)), pointer)
        default = json.loads(json.dumps(default))
        node: dict[str, Any] = {
            "description": descriptions.get(name) or item.description
        }
        kind = {list: "array", str: "string", bool: "boolean", int: "integer",
                float: "number", dict: "object"}.get(type(default))  # fmt: skip
        if kind is not None:
            node["type"] = kind
        put(parts, default, node)
        comments.append(f"# {name}: {node['description']}")
    for top in sorted(tree_values):
        values[top] = tree_values[top]
        schema[top] = tree_schema[top]
    if offer_existing:
        values["existingSecret"] = ""
        schema["existingSecret"] = {
            "type": "string",
            "description": "A Secret you created holding every secret key "
            f"({', '.join(sorted(secret_keys))}); the chart then makes no Secret",
        }
        comments.append(
            "# existingSecret: a Secret you created with the keys "
            f"{', '.join(sorted(secret_keys))}; the chart then makes and reads no other"
        )
    values["storage"] = {"size": ""}
    schema["storage"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "size": {
                "type": "string",
                "pattern": SIZE.pattern,
                "description": "Size of every claim (10Gi); empty keeps each "
                "claim's declared size",
            }
        },
    }
    comments.append(
        "# storage.size: size of every claim (10Gi); empty keeps the declared sizes"
    )
    values["image"] = {"registry": "", "pullSecrets": []}
    schema["image"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "registry": {
                "type": "string",
                "description": "Registry host (and path) that replaces the registry "
                "of every image, for a mirror; the digests stay",
            },
            "pullSecrets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of existing image pull Secrets",
            },
        },
    }
    comments.append(
        "# image.registry: a mirror's host (and path) for every image; digests stay"
    )
    comments.append("# image.pullSecrets: names of existing image pull Secrets")
    values["storageClass"] = bundle.storage_class or ""
    schema["storageClass"] = {
        "type": "string",
        "description": "Storage class of every claim; empty for the cluster's default",
    }
    comments.append(
        "# storageClass: storage class of every claim (empty: the cluster's default)"
    )
    if templater.claims:
        values["deleteDataOnUninstall"] = False
        schema["deleteDataOnUninstall"] = {
            "type": "boolean",
            "description": "helm uninstall also deletes the StatefulSets' claims "
            "(their data); by default they stay",
        }
        comments.append(
            "# deleteDataOnUninstall: helm uninstall also deletes the data claims "
            "(default: they stay)"
        )
    schema["global"] = {"type": "object"}
    files["values.yaml"] = (
        f"# Values of {bundle.name} {bundle.version} (written by piceli bundle --helm).\n"
        + "\n".join(comments)
        + "\n"
        + yaml.safe_dump(values, sort_keys=True, width=1000)
    ).encode()
    files["values.schema.json"] = (
        json.dumps(
            {
                "$schema": SCHEMA_DRAFT,
                "title": "Values",
                "type": "object",
                "additionalProperties": False,
                "properties": schema,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    files["templates/_helpers.tpl"] = _helpers(bundle.name, inputs).encode()
    if bundle.secrets:
        files["templates/_helpers.tpl"] += _secret_helper(
            bundle.name, bundle, inputs
        ).encode()
    chart = HelmBundle(
        name=bundle.name,
        version=bundle.version,
        app_version=app_version,
        files=files,
        settings=settings,
        inputs=inputs,
        toggles=tuple(sorted(toggles)),
        required=tuple(sorted(required)),
        cluster_objects=tuple(cluster_objects),
        claims=templater.claims,
        optional=tuple(sorted(optional)),
        values=tuple(sorted(placed_values)),
        existing_secret_keys=tuple(sorted(secret_keys)) if offer_existing else (),
    )
    files["Chart.yaml"] = yaml.safe_dump(chart.metadata(), sort_keys=True).encode()
    files["templates/NOTES.txt"] = _notes(chart).encode()
    files["README.md"] = _readme(
        chart, descriptions, toggles, declared.optional, declared.values
    ).encode()
    return chart


def _helpers(name: str, inputs: Mapping[str, str]) -> str:
    omitted = " ".join(_literal(value) for value in sorted(inputs.values()))
    config = f"(omit .Values {omitted})" if omitted else ".Values"
    return (
        f'{{{{- define "{name}.image" -}}}}\n'
        '{{- $registry := index . 2 | default "" -}}\n'
        "{{- if $registry -}}\n"
        '{{- printf "%s/%s" (trimSuffix "/" $registry) (index . 1) -}}\n'
        "{{- else -}}\n"
        "{{- index . 0 -}}\n"
        "{{- end -}}\n"
        "{{- end -}}\n"
        f'{{{{- define "{name}.config" -}}}}\n'
        f"{{{{- toJson {config} -}}}}\n"
        "{{- end -}}\n"
        # 0.31.0: (list QUANTITY PERCENT) -> that share of the quantity, in bytes.
        f'{{{{- define "{name}.percentOf" -}}}}\n'
        "{{- $q := index . 0 | toString -}}\n"
        '{{- $n := regexFind "^[0-9]+" $q | atoi -}}\n'
        '{{- $u := regexFind "[A-Za-z]+$" $q -}}\n'
        '{{- $m := dict "" 1 "k" 1000 "M" 1000000 "G" 1000000000 "T" 1000000000000 '
        '"P" 1000000000000000 "Ki" 1024 "Mi" 1048576 "Gi" 1073741824 '
        '"Ti" 1099511627776 "Pi" 1125899906842624 -}}\n'
        "{{- div (mul $n (get $m $u) (index . 1)) 100 -}}\n"
        "{{- end -}}\n"
    )


def _install_command(chart: HelmBundle, source: str = "<chart>") -> str:
    parts = [f"helm install {chart.name} {source} --version {chart.version}"]
    parts.append(f"-n {chart.name} --create-namespace")
    parts += [f"--set {name}=..." for name in chart.required]
    parts += [f"--set-file {name}=FILE" for name in sorted(chart.inputs.values())]
    return " ".join(parts)


def _notes(chart: HelmBundle) -> str:
    lines = [
        f"{chart.name} {chart.version} is installed in namespace {{{{ .Release.Namespace }}}}.",
        "",
        "Status:   kubectl -n {{ .Release.Namespace }} get pods",
        "Remove:   helm uninstall {{ .Release.Name }} -n {{ .Release.Namespace }}",
    ]
    if chart.claims:
        lines += [
            "{{- if .Values.deleteDataOnUninstall }}",
            "Uninstall also deletes the data claims of the StatefulSets.",
            "{{- else }}",
            "The data claims of the StatefulSets stay after uninstall; delete the",
            "namespace to remove them: kubectl delete namespace {{ .Release.Namespace }}",
            "{{- end }}",
        ]
    return "\n".join(lines) + "\n"


def _readme(
    chart: HelmBundle,
    descriptions: Mapping[str, str],
    toggles: Mapping[str, HelmToggle],
    optional: Mapping[str, HelmOptional] | None = None,
    placed: Mapping[str, HelmValue] | None = None,
) -> str:
    lines = [
        f"# {chart.name}",
        "",
        "Written by `piceli bundle --helm`. Install:",
        "",
        "```sh",
        _install_command(chart),
        "```",
        "",
        "Remove:",
        "",
        "```sh",
        f"helm uninstall {chart.name} -n {chart.name}",
        "```",
        "",
        "Uninstall removes every object of the chart, the cluster-scoped ones "
        "included"
        + (f" ({', '.join(chart.cluster_objects)})" if chart.cluster_objects else "")
        + ", and the Secrets it generated.",
    ]
    if chart.claims:
        lines += [
            "",
            "The claims of StatefulSets (the data) stay after uninstall unless "
            "`deleteDataOnUninstall=true` (Kubernetes 1.27 or later); delete the "
            "namespace to remove them.",
        ]
    lines += ["", "## Values", ""]
    for name in sorted(chart.settings):
        config, key = chart.settings[name]
        text = descriptions.get(name) or f"ConfigMap {config}, key {key}"
        flag = " (required)" if name in chart.required else ""
        lines.append(f"- `{name}`{flag}: {text}")
    for name in sorted(chart.inputs.values()):
        text = descriptions.get(name) or "a secret input"
        lines.append(
            f"- `{name}` (required on the first install, `--set-file {name}=FILE`): "
            f"{text}; kept across upgrades when not passed again"
        )
    for name in sorted(toggles):
        text = descriptions.get(name) or toggles[name].description
        lines.append(f"- `{name}` (true or false): {text}")
    for name, item in sorted((optional or {}).items()):
        text = descriptions.get(name) or item.description
        objects = ", ".join(item.keys())
        lines.append(f"- `{name}` (true or false): {text}; true installs {objects}")
    for name, value in sorted((placed or {}).items()):
        lines.append(f"- `{name}`: {descriptions.get(name) or value.description}")
    if chart.existing_secret_keys:
        keys = ", ".join(f"`{key}`" for key in chart.existing_secret_keys)
        lines.append(
            "- `existingSecret`: the name of a Secret you created in the release "
            f"namespace with the keys {keys}; the chart then makes no Secret and "
            "generates nothing"
        )
    lines += [
        "- `storage.size`: the size of every claim (for example `20Gi`); empty "
        "keeps each claim's declared size. Set it at install: Kubernetes does "
        "not resize a StatefulSet's claims on upgrade",
        "- `image.registry`: a mirror's host (and path) for every image; the digests stay",
        "- `image.pullSecrets`: names of existing image pull Secrets",
        "- `storageClass`: the storage class of every claim (empty: the cluster's default)",
    ]
    if chart.claims:
        lines.append(
            "- `deleteDataOnUninstall`: uninstall also deletes the data claims"
        )
    lines += [
        "",
        "Generated secrets are made once at install and kept across `helm upgrade` "
        "(read back with `lookup`); they are never printed.",
    ]
    if chart.existing_secret_keys:
        lines += [
            "",
            "Argo CD and `helm template` render without a cluster, so `lookup` "
            "finds nothing and generated secrets would change on every render. "
            "There, create the Secret yourself (random values for the generated "
            "keys, for example `openssl rand -base64 32`) and set "
            "`existingSecret`: nothing is generated and every render is the same. "
            "The same Secret is the one to copy a key from when other namespaces "
            "need it.",
        ]
    lines.append("")
    return "\n".join(lines)


def write_helm(directory: Any, chart: HelmBundle) -> list[str]:
    """Write the chart into ``directory`` (absent or empty); the file list.

    :raises BundleError: ``bundle-out-refused``.
    """
    import shutil

    if directory.is_symlink() or (
        directory.exists() and (not directory.is_dir() or any(directory.iterdir()))
    ):
        raise BundleError(
            "bundle-out-refused", f"{directory} must be absent or an empty directory"
        )
    created = not directory.exists()
    try:
        for path, body in sorted(chart.files.items()):
            target = directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    except OSError as error:
        if created:
            shutil.rmtree(directory, ignore_errors=True)
        raise BundleError(
            "bundle-out-refused", f"cannot write the chart: {type(error).__name__}"
        ) from None
    return sorted(chart.files)
