from dataclasses import dataclass, field

from piceli.k8s.k8s_objects.base import K8sObject, K8sObjectIdentifier
from piceli.k8s.ops.compare import object_comparer


@dataclass
class ModifiedObjects:
    before: K8sObject
    after: K8sObject
    compare_result: object_comparer.CompareResult


@dataclass
class ObjectDifferences:
    added: list[K8sObject] = field(default_factory=list)
    removed: list[K8sObject] = field(default_factory=list)
    modified: dict[K8sObjectIdentifier, ModifiedObjects] = field(default_factory=dict)


def compare_object_sets(
    before: dict[str | None, list[K8sObject]], after: dict[str | None, list[K8sObject]]
) -> dict[str | None, ObjectDifferences]:
    namespace_diffs = {}
    for namespace in set(before.keys()) | set(after.keys()):
        before_set = {obj.unnamespaced_id: obj for obj in before.get(namespace, [])}
        after_set = {obj.unnamespaced_id: obj for obj in after.get(namespace, [])}

        diffs = ObjectDifferences()
        diffs.added = [obj for id, obj in after_set.items() if id not in before_set]
        diffs.removed = [obj for id, obj in before_set.items() if id not in after_set]

        for id in set(before_set.keys()) & set(after_set.keys()):
            _after, _before = after_set[id], before_set[id]
            compare_result = object_comparer.determine_update_action(_after, _before)
            if compare_result.update_action != object_comparer.UpdateAction.EQUALS:
                diffs.modified[id] = ModifiedObjects(_before, _after, compare_result)

        namespace_diffs[namespace] = diffs
    return namespace_diffs
