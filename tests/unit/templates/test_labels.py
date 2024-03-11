import pytest

from piceli.k8s.templates.auxiliary import labels


# Cluster Labels Tests
@pytest.mark.parametrize(
    "key, value",
    [
        ("team", None),
        ("component", ""),
    ],
)
def test_cluster_labels_not_empty(key: str, value: str) -> None:
    """test cluster label not empty"""
    with pytest.raises(ValueError):
        labels.check_labels({key: value})


@pytest.mark.parametrize(
    "key, value",
    [
        ("state", "-"),
        ("team", "_"),
        ("team", "A"),
        ("team", "0"),
    ],
)
def test_cluster_labels_starts_lowercase(key: str, value: str) -> None:
    """test cluster label starts only by [a-z]"""
    with pytest.raises(ValueError):
        labels.check_labels({key: value})


@pytest.mark.parametrize(
    "key, value",
    [
        ("team", "a."),
        ("team", "a$"),
    ],
)
def test_cluster_labels_middle(key: str, value: str) -> None:
    """test cluster label can only contains [a-z][0-9][-_]"""
    with pytest.raises(ValueError):
        labels.check_labels({key: value})


def test_cluster_labels_len() -> None:
    """test cluster label len between 1 and 63"""
    with pytest.raises(ValueError):
        labels.check_labels({"team": "a" * 64})


@pytest.mark.parametrize(
    "key, value",
    [
        ("team", "this-works"),
        ("team", "this_too_"),
        ("team", "and-this_too--"),
    ],
)
def test_cluster_labels_valid(key: str, value: str) -> None:
    """test cluster label valid examples"""
    assert labels.check_labels({key: value}) == {key: value}


# Standard Labels Tests
@pytest.mark.parametrize(
    "data",
    [
        {"keyx": None},
        {"any-name": ""},
    ],
)
def test_labels_empty(data: dict) -> None:
    """test label value can be empty"""
    assert labels.check_labels(data) == data


@pytest.mark.parametrize(
    "key, value",
    [
        ("key", "-"),
        ("key", "_"),
        ("key", "."),
    ],
)
def test_labels_starts_invalid(key: str, value: str) -> None:
    """test label starts only by [a-z0-9A-Z] and invalid cases"""
    with pytest.raises(ValueError):
        labels.check_labels({key: value})


@pytest.mark.parametrize("key", ["a", "A", "0"])
def test_labels_starts_valid(key: str) -> None:
    """test label starts only by [a-z0-9A-Z] and valid cases"""
    assert labels.check_labels({key: "valid"}) == {key: "valid"}


@pytest.mark.parametrize("key", ["a.a", "a_a", "a-a"])
def test_labels_middle(key: str) -> None:
    """test label can contains [-_.] in the middle too"""
    assert labels.check_labels({key: "valid"}) == {key: "valid"}


def test_labels_len() -> None:
    """test label len between 0 and 63"""
    with pytest.raises(ValueError):
        labels.check_labels({"key": "a" * 64})
    assert labels.check_labels({"key": ""}) == {"key": ""}


@pytest.mark.parametrize(
    "key, value",
    [
        ("key", "this-works"),
        ("key", "this_too_"),
        ("tam", "and-this_too--"),
        ("key", "THIS_too_"),
        ("key", "0this.too_"),
    ],
)
def test_labels_valid(key: str, value: str) -> None:
    """test label valid examples"""
    assert labels.check_labels({key: value}) == {key: value}


# Keys Tests
@pytest.mark.parametrize(
    "data",
    [
        {"": ""},
        {None: ""},
    ],
)
def test_keys_empty(data: dict) -> None:
    """test keys cannot be empty"""
    with pytest.raises(ValueError):
        labels.check_labels(data)


@pytest.mark.parametrize(
    "data",
    [
        ({"-": "aaa"}),
        ({"_": "aaa"}),
        ({".": "aaa"}),
    ],
)
def test_key_starts_invalid(data: dict) -> None:
    """test key starts only by [a-z0-9A-Z] and invalid cases"""
    with pytest.raises(ValueError):
        labels.check_labels(data)


@pytest.mark.parametrize(
    "data",
    [
        ({"a": "aaa"}),
        ({"A": "aaa"}),
        ({"0": "aaa"}),
    ],
)
def test_key_starts_valid(data: dict) -> None:
    """test key starts only by [a-z0-9A-Z] and valid cases"""
    assert labels.check_labels(data) == data


@pytest.mark.parametrize(
    "data",
    [
        ({"a.a": "aaa"}),
        ({"a_a": "aaa"}),
        ({"a-a": "aaa"}),
    ],
)
def test_key_middle(data: dict) -> None:
    """test key can contains [-_.] in the middle too"""
    assert labels.check_labels(data) == data


@pytest.mark.parametrize(
    "key",
    [
        "aaa.",
        "aaa-",
        "aaa_",
    ],
)
def test_key_end(key: str) -> None:
    """test key cannot ends with [-_.]"""
    with pytest.raises(ValueError):
        labels.check_labels({key: "valid"})


def test_key_len() -> None:
    """test key len between 1 and 63"""
    with pytest.raises(ValueError):
        labels.check_labels({"a" * 64: "aaa"})
    with pytest.raises(ValueError):
        labels.check_labels({"": "aaa"})


@pytest.mark.parametrize(
    "data",
    [
        ({"Avalid.K.e.y": "aaa"}),
        ({"aNoth-asd": "aaa"}),
        ({"0V.-_validKe0": "aaa"}),
    ],
)
def test_key_valid(data: dict) -> None:
    """test key valid examples"""
    assert labels.check_labels(data) == data
