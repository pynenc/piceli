from piceli.k8s.ops.compare import path


def test_path_equality() -> None:
    p1 = path.Path.from_list(["metadata", "name"])
    p2 = path.Path.from_list(["spec", "containers:nginx", "image"])
    assert p1 == path.Path.from_list(["metadata", "name"])
    assert p2 == path.Path.from_list(["spec", "containers:nginx", "image"])
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


def test_wildcards() -> None:
    p0 = path.Path.from_string("spec,containers,name:some-name,field0")
    p1 = path.Path.from_string("*,spec,containers,*,field0")
    p2 = path.Path.from_string("cronjob,stuff,spec,containers,name:other-name,field0")
    assert p0 == p1 and hash(p0) != hash(p1)
    assert p2 == p1 and hash(p2) != hash(p1)
    assert p0 != p2
    assert p0 in [p1]  # uses comparison
    assert p2 in [p1]
    assert p0 not in {p1}  # uses the hashes


def test_match_wildcard() -> None:
    p0 = path.Path.from_string(
        "spec,template,spec,containers,name:example-container,terminationMessagePath"
    )
    wp = path.Path.from_string("spec,template,spec,containers,*,terminationMessagePath")
    assert p0 in [wp]
    assert p0 not in {wp}
    assert path.path_matches_any_with_wildcard(p0, {wp})
    assert path.path_matches_any_with_wildcard(p0, [wp])


def test_path_containment() -> None:
    other_path = path.Path.from_string("metadata,other")
    smaller_path = path.Path.from_string("metadata,name")
    larger_path = path.Path.from_string("metadata,name,managedFields,other")
    assert smaller_path in larger_path
    assert other_path not in larger_path

    wildcard_path = path.Path.from_string("metadata,*")
    assert wildcard_path in larger_path
    assert larger_path in wildcard_path
    assert smaller_path in wildcard_path
    assert other_path in wildcard_path
