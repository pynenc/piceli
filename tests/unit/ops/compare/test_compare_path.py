from piceli.k8s.ops.compare import path


def test_path_equality() -> None:
    p1 = path.Path.from_list(["metadata", "name"])
    p2 = path.Path.from_list(["spec", "containers:nginx", "image"])
    assert p1 == path.Path.from_list(["metadata", "name"])
    assert p2 != path.Path.from_list(["spec", "containers:nginx", "ports"])


def test_in() -> None:
    path_0 = path.Path.from_list(["metadata", "managedFields", "other"])
    same_path = path.Path.from_string("metadata,managedFields,other")
    assert path_0 == same_path
    assert same_path in path_0
    assert path_0 in same_path
    assert path_0[:2] in same_path
    assert path_0[1:2] not in same_path
    assert path_0 not in same_path[:2]
