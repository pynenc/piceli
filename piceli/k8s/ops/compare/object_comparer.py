from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, NamedTuple

from kubernetes.utils.quantity import parse_quantity

from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.compare.path import DictKey, ListElemId, Path


class UpdateAction(Enum):
    EQUALS = auto()
    NEEDS_PATCH = auto()
    NEEDS_REPLACEMENT = auto()


class PathComparison(NamedTuple):
    path: Path
    existing: Any
    desired: Any

    def __hash__(self) -> int:
        return hash(self.path)


@dataclass
class Differences:
    considered: list[PathComparison] = field(default_factory=list)
    ignored: list[PathComparison] = field(default_factory=list)
    defaults: list[PathComparison] = field(default_factory=list)

    def extend(self, other: "Differences") -> None:
        self.considered.extend(other.considered)
        self.ignored.extend(other.ignored)
        self.defaults.extend(other.defaults)


@dataclass
class CompareResult:
    desired_spec: dict
    existing_spec: dict
    update_action: UpdateAction
    differences: Differences

    def patch_document(self) -> dict:
        """Build the patch document from the considered differences."""
        patch: dict = {}
        for diff in self.differences.considered:
            if not self._is_path_in_desired(diff.path):
                # if the path is not in the desired document,
                # it should not be considred in the patch document.
                continue
            previous, previous_path_elem = None, None
            current = patch
            for path_elem in diff.path[:-1]:
                if isinstance(path_elem, ListElemId):
                    if not isinstance(previous_path_elem, DictKey) or not previous:
                        raise ValueError(
                            "A list cannot be first element or go after another list"
                        )
                    if not isinstance(current, list):
                        previous[previous_path_elem.key] = (current := [])
                    for elem in current:
                        if path_elem.id_value == elem.get(path_elem.id_field):
                            current = elem
                            break
                    else:
                        current.append({path_elem.id_field: path_elem.id_value})
                        current = current[-1]
                    previous, previous_path_elem = None, None
                elif isinstance(path_elem, DictKey):
                    previous_path_elem, previous = path_elem, current
                    current = current.setdefault(path_elem.key, {})
                else:
                    raise ValueError(f"Unknown path element {path_elem}")
            if isinstance(diff.path[-1], ListElemId):
                if not isinstance(previous_path_elem, DictKey) or not previous:
                    raise ValueError(
                        "A list cannot be first element or go after another list"
                    )
                if not isinstance(current, list):
                    previous[previous_path_elem.key] = []
                previous[previous_path_elem.key].append(diff.desired)
            elif isinstance(diff.path[-1], DictKey):
                current[diff.path[-1].key] = diff.desired
            else:
                raise ValueError(f"Unknown path element {diff.path[-1]}")
        return patch

    def _is_path_in_desired(self, path: Path) -> bool:
        """Check if the given path exists in the desired document."""
        current = self.desired_spec
        for key in path:
            if isinstance(key, ListElemId) and isinstance(current, list):
                for elem in current:
                    if key.id_value == elem.get(key.id_field):
                        current = elem
            else:
                if key not in current:
                    return False
                current = current[key]
        return True

    @property
    def no_action_needed(self) -> bool:
        return self.update_action == UpdateAction.EQUALS

    @property
    def needs_patch(self) -> bool:
        return self.update_action == UpdateAction.NEEDS_PATCH

    @property
    def needs_replacement(self) -> bool:
        return self.update_action == UpdateAction.NEEDS_REPLACEMENT


# paths to ignore in exising and desired specs
# would always be overwritten by k8s
IGNORED_PATHS = {
    Path.from_list(["metadata", "creationTimestamp"]),
    Path.from_list(["metadata", "finalizers"]),
    Path.from_list(["metadata", "labels", "kubernetes.io/metadata.name"]),
    Path.from_list(["metadata", "managedFields"]),
    Path.from_list(["metadata", "resourceVersion"]),
    Path.from_list(["metadata", "uid"]),
    Path.from_list(["metadata", "namespace"]),  # handle on function lev]el
    Path.from_list(["metadata", "annotations"]),
    Path.from_list(["metadata", "generation"]),
    Path.from_list(["spec", "finalizers"]),
    Path.from_list(["status"]),
}

# paths to ignore if only exsits in existing_spec
# because they are default values and desired do not explicitly set them
# k8s will define them if not set in desired spec
DEFAULTED_PATHS = {
    Path.from_string("spec,storageClassName"),
    Path.from_string("spec,volumeMode"),
}


def is_path_ignored(path_comparison: PathComparison) -> bool:
    """Check if the path should be completely ignored."""
    return any(ignored_path in path_comparison.path for ignored_path in IGNORED_PATHS)


def is_path_defaulted(path_comparison: PathComparison) -> bool:
    """Check if the path should be considered a default, ignored only if missing in desired."""
    return (path_comparison.path in DEFAULTED_PATHS) and (
        path_comparison.desired is None and path_comparison.existing is not None
    )


RESOURCE_KEYS = {"memory", "cpu", "ephemeral-storage", "storage"}


def are_values_equal(path_comparison: PathComparison) -> bool:
    """Determine if two values are different, considering special cases."""
    if path_comparison.existing == path_comparison.desired:
        return True
    if path_comparison.path[-1] in RESOURCE_KEYS:
        if path_comparison.existing and path_comparison.desired:
            return parse_quantity(path_comparison.existing) == parse_quantity(
                path_comparison.desired
            )
    return False


# PATH THAT CONTAINS A LIST OF DICT WITH AN UNIQUE ID
# dict with the path and the key of the unique id in each of the list elements
PATH_WITH_ID_LIST = {
    Path.from_string("spec,template,spec,containers"): "name",
}


def compare_values(path_comparison: PathComparison) -> Differences:
    """Compare two values and create a Difference based on their comparison."""
    if are_values_equal(path_comparison):
        return Differences()
    if is_path_ignored(path_comparison):
        return Differences(ignored=[path_comparison])
    if is_path_defaulted(path_comparison):
        return Differences(defaults=[path_comparison])
    if (
        path_comparison.desired is None or isinstance(path_comparison.desired, dict)
    ) and (
        path_comparison.existing is None or isinstance(path_comparison.existing, dict)
    ):
        return find_differences(
            desired_spec=path_comparison.desired or {},
            existing_spec=path_comparison.existing or {},
            prefix=path_comparison.path,
        )
    if path_comparison.path in PATH_WITH_ID_LIST:
        return find_id_list_differences(
            desired_spec=path_comparison.desired or [],
            existing_spec=path_comparison.existing or [],
            prefix=path_comparison.path,
        )
    return Differences(considered=[path_comparison])


def find_id_list_differences(
    desired_spec: list, existing_spec: list, prefix: Path
) -> Differences:
    differences = Differences()
    id_field = PATH_WITH_ID_LIST[prefix]
    desired_elems = {elem[id_field]: elem for elem in desired_spec}
    existing_elems = {elem[id_field]: elem for elem in existing_spec}
    for id_value in set(desired_elems).union(existing_elems):
        path_comparison = PathComparison(
            path=prefix.add(ListElemId(id_field, id_value)),
            existing=existing_elems.get(id_value),
            desired=desired_elems.get(id_value),
        )
        differences.extend(compare_values(path_comparison))
    return differences


def find_differences(
    desired_spec: dict | None, existing_spec: dict | None, prefix: Path | None = None
) -> Differences:
    differences = Differences()
    prefix = prefix or Path([])
    _desired_spec, _existing_spec = desired_spec or {}, existing_spec or {}
    for key in set(_desired_spec).union(_existing_spec):
        path_comparison = PathComparison(
            path=prefix.add(DictKey(key)),
            existing=_existing_spec.get(key),
            desired=_desired_spec.get(key),
        )
        differences.extend(compare_values(path_comparison))
    return differences


def determine_update_action(desired: K8sObject, existing: K8sObject) -> CompareResult:
    kind = desired.kind
    return _determine_update_action(kind, desired.spec, existing.spec)


def filter_spec(spec: dict) -> dict:
    return {
        key: value for key, value in spec.items() if key not in ["status", "events"]
    }


def _determine_update_action(kind: str, desired: dict, existing: dict) -> CompareResult:
    filtered_desired_spec = filter_spec(desired)
    filtered_existing_spec = filter_spec(existing)
    differences = find_differences(filtered_desired_spec, filtered_existing_spec)
    considered = differences.considered
    if any(diff for diff in considered if requires_replacement(kind, diff)):
        return CompareResult(
            desired, existing, UpdateAction.NEEDS_REPLACEMENT, differences
        )
    elif considered:
        return CompareResult(desired, existing, UpdateAction.NEEDS_PATCH, differences)
    else:
        return CompareResult(desired, existing, UpdateAction.EQUALS, differences)


IMMUTABLE_FIELDS = {
    Path.from_string("spec,selector"),
    Path.from_string("spec,template"),
    Path.from_string("spec,completions"),
}


def requires_replacement(kind: str, diff: PathComparison) -> bool:
    # Check if any of the immutable field paths is a prefix of the current path
    if any(diff.path[: len(field)] == field for field in IMMUTABLE_FIELDS):
        return True
    # "PersistentVolumeClaim"
    # "spec is immutable after creation except resources.requests for bound claims"
    if kind == "PersistentVolumeClaim" and diff.path[0] == "spec":
        if diff.desired is None:
            return False
        if diff.path[1] != "resources":
            return True
    return False
