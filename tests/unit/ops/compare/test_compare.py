from piceli.k8s.ops.compare import object_comparer


def test_find_differences_no_changes() -> None:
    desired_spec = {"key1": "value1", "key2": "value2"}
    existing_spec = {"key1": "value1", "key2": "value2"}
    assert (
        object_comparer.find_differences(desired_spec, existing_spec).considered == []
    )


def test_find_differences_with_changes() -> None:
    desired_spec = {"key1": "new_value", "key2": "value2"}
    existing_spec = {"key1": "value1", "key2": "value2"}
    assert object_comparer.find_differences(desired_spec, existing_spec).considered == [
        object_comparer.PathComparison(
            object_comparer.Path.from_string("key1"), "value1", "new_value"
        )
    ]


def test_find_differences_with_nested_changes() -> None:
    desired_spec = {"key1": {"nested_key": "new_value"}}
    existing_spec = {"key1": {"nested_key": "old_value"}}
    assert object_comparer.find_differences(desired_spec, existing_spec).considered == [
        object_comparer.PathComparison(
            object_comparer.Path.from_list(["key1", "nested_key"]),
            "old_value",
            "new_value",
        )
    ]


def test_determine_update_action_equals() -> None:
    desired = {"key": "value"}
    existing = {"key": "value"}
    assert (
        object_comparer._determine_update_action(
            "anyKind", desired, existing
        ).update_action
        == object_comparer.UpdateAction.EQUALS
    )


def test_determine_update_action_needs_patch() -> None:
    desired = {"key": "new_value"}
    existing = {"key": "old_value"}
    assert (
        object_comparer._determine_update_action(
            "anyKind", desired, existing
        ).update_action
        == object_comparer.UpdateAction.NEEDS_PATCH
    )


def test_determine_update_action_needs_replacement() -> None:
    # Test with an immutable field
    assert (
        object_comparer.Path.from_string("spec,selector")
        in object_comparer.IMMUTABLE_FIELDS
    )
    # The desired spec has a different value for the immutable field
    desired = {"spec": {"selector": "new_selector"}}
    existing = {"spec": {"selector": "old_selector"}}
    # The update action should be NEEDS_REPLACEMENT for the immutable field
    assert (
        object_comparer._determine_update_action(
            "anyKind", desired, existing
        ).update_action
        == object_comparer.UpdateAction.NEEDS_REPLACEMENT
    )


def test_find_differences_with_ignored_changes() -> None:
    desired_spec = {
        "metadata": {
            "creationTimestamp": None,
            "resourceVersion": "123456",
            "uid": "new-uid",
            "labels": {"kubernetes.io/metadata.name": "new-name"},
            "managedFields": [],
        },
        "key": "value",
    }
    existing_spec = {
        "metadata": {
            "creationTimestamp": "2020-01-01T00:00:00Z",
            "resourceVersion": "654321",
            "uid": "old-uid",
            "labels": {"kubernetes.io/metadata.name": "old-name"},
            "managedFields": ["someFields"],
        },
        "key": "value",
    }
    differences = object_comparer.find_differences(desired_spec, existing_spec)
    # No considered differences since only 'key' matches and it's unchanged.
    assert differences.considered == []
    # Five ignored differences due to metadata fields.
    assert len(differences.ignored) == 5


def test_requires_replacement_for_subpath() -> None:
    # Immutable field as part of the path
    path_immutable = object_comparer.Path.from_list(
        ["spec", "template", "metadata", "labels"]
    )
    diff_immutable = object_comparer.PathComparison(
        desired=None, existing=None, path=path_immutable
    )
    assert object_comparer.requires_replacement("AnyKind", diff_immutable) is True

    # A mutable path
    path_immutable = object_comparer.Path.from_list(["spec", "containers", "image"])
    diff_immutable = object_comparer.PathComparison(
        desired=None, existing=None, path=path_immutable
    )
    assert object_comparer.requires_replacement("AnyKind", diff_immutable) is False

    # Subpath of an immutable field but not starting with it should not require replacement
    path_not_starting_with_immutable = object_comparer.Path.from_list(
        ["metadata", "spec", "selector"]
    )
    diff_not_starting_with_immutable = object_comparer.PathComparison(
        desired=None, existing=None, path=path_not_starting_with_immutable
    )
    assert (
        object_comparer.requires_replacement(
            "AnyKind", diff_not_starting_with_immutable
        )
        is False
    )

    # Check an exact immutable field
    path_exact_immutable = object_comparer.Path.from_list(["spec", "selector"])
    diff_exact_immutable = object_comparer.PathComparison(
        desired=None, existing=None, path=path_exact_immutable
    )
    assert object_comparer.requires_replacement("AnyKind", diff_exact_immutable) is True


def test_required_replacement_for_pvc_resource() -> None:
    """PVC spec is immutable except for storage requests"""
    path = object_comparer.Path.from_list(["spec", "resources", "requests", "storage"])
    diff = object_comparer.PathComparison(desired=None, existing=None, path=path)
    assert object_comparer.requires_replacement("PersistentVolumeClaim", diff) is False

    path = object_comparer.Path.from_list(["spec", "resources", "requests", "whatever"])
    diff = object_comparer.PathComparison(desired=None, existing=None, path=path)
    assert object_comparer.requires_replacement("PersistentVolumeClaim", diff) is False

    # If new desired value for PersistenVolumeClaim spec field, then it requires replacement
    path = object_comparer.Path.from_list(["spec", "other"])
    diff = object_comparer.PathComparison(desired="NewValue", existing=None, path=path)
    assert object_comparer.requires_replacement("PersistentVolumeClaim", diff) is True

    # But a None value will be ignored, so it does not require replacement
    path = object_comparer.Path.from_list(["spec", "other"])
    diff = object_comparer.PathComparison(desired=None, existing=None, path=path)
    assert object_comparer.requires_replacement("PersistentVolumeClaim", diff) is False


def test_find_differences_with_ignored_paths() -> None:
    desired_spec = {"metadata": {"labels": None}, "spec": None}
    existing_spec = {
        "metadata": {"labels": {"kubernetes.io/metadata.name": "example-namespace"}},
        "spec": {"finalizers": ["kubernetes"]},
    }
    differences = object_comparer.find_differences(desired_spec, existing_spec)
    # Check that differences that should be ignored are indeed ignored
    assert (
        differences.considered == []
    ), "There should be no considered differences for ignored paths"
    assert set(differences.ignored) == {
        object_comparer.PathComparison(
            object_comparer.Path.from_list(
                ["metadata", "labels", "kubernetes.io/metadata.name"]
            ),
            "example-namespace",
            None,
        ),
        object_comparer.PathComparison(
            object_comparer.Path.from_string("spec,finalizers"), ["kubernetes"], None
        ),
    }, "Ignored differences did not match expected"


def test_find_differences_with_none_and_string() -> None:
    # Setup: one spec has a None value and the other has a string for the same key
    desired_spec = {"key": None}
    existing_spec = {"key": "existing_value"}
    # Execute: Find differences between the two specs
    differences = object_comparer.find_differences(desired_spec, existing_spec)
    # Assert: Check that the difference is correctly identified and considered
    expected = object_comparer.PathComparison(
        object_comparer.Path.from_string("key"), "existing_value", None
    )
    assert differences.considered == [
        expected
    ], "Should detect and consider a difference where one value is None and the other is a string"
    # Also assert that there are no ignored differences in this case
    assert (
        differences.ignored == []
    ), "There should be no ignored differences for this case"
