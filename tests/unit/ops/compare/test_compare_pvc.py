from piceli.k8s.ops.compare import object_comparer

desired_spec = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "metadata": {
        "name": "example-persistentvolumeclaim",
        "namespace": "example-namespace",
    },
    "spec": {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "0.1Gi"}},
    },
}
existing_spec = {
    "apiVersion": "v1",
    "kind": "PersistentVolumeClaim",
    "metadata": {
        "creationTimestamp": "2024-03-01T10:19:10+00:00",
        "finalizers": ["kubernetes.io/pvc-protection"],
        "managedFields": [
            {
                "apiVersion": "v1",
                "fieldsType": "FieldsV1",
                "fieldsV1": "...",
                "manager": "OpenAPI-Generator",
                "operation": "Update",
                "time": "2024-03-01T10:19:10+00:00",
            }
        ],
        "name": "example-persistentvolumeclaim",
        "namespace": "example-namespace",
        "resourceVersion": "255202869",
        "uid": "d8eba4cf-c0b5-476f-beef-c691dd87c80a",
    },
    "spec": {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "107374182400m"}},
        "storageClassName": "standard-rwo",
        "volumeMode": "Filesystem",
    },
    "status": {"phase": "Pending"},
}


def test_find_differences() -> None:
    differences = object_comparer.find_differences(desired_spec, existing_spec)
    assert differences.considered == []
    assert set(differences.defaults) == {
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("spec,volumeMode"),
            existing="Filesystem",
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("spec,storageClassName"),
            existing="standard-rwo",
            desired=None,
        ),
    }
    assert set(differences.ignored) == {
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("metadata,finalizers"),
            existing=["kubernetes.io/pvc-protection"],
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("metadata,managedFields"),
            existing=[
                {
                    "apiVersion": "v1",
                    "fieldsType": "FieldsV1",
                    "fieldsV1": "...",
                    "manager": "OpenAPI-Generator",
                    "operation": "Update",
                    "time": "2024-03-01T10:19:10+00:00",
                }
            ],
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("metadata,uid"),
            existing="d8eba4cf-c0b5-476f-beef-c691dd87c80a",
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("metadata,resourceVersion"),
            existing="255202869",
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("metadata,creationTimestamp"),
            existing="2024-03-01T10:19:10+00:00",
            desired=None,
        ),
        object_comparer.PathComparison(
            path=object_comparer.Path.from_string("status"),
            existing={"phase": "Pending"},
            desired=None,
        ),
    }
