from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import ClassVar, Iterable, Iterator, Union, overload


class PathElem(ABC):
    @property
    @abstractmethod
    def id(self) -> str:
        pass

    @abstractmethod
    def __eq__(self, other: object) -> bool:
        pass

    @abstractmethod
    def __hash__(self) -> int:
        pass


@dataclass(frozen=True)
class DictKey(PathElem):
    key: str

    @property
    def id(self) -> str:
        return self.key

    def __eq__(self, other: object) -> bool:
        if isinstance(other, DictKey):
            return self.key == other.key
        elif isinstance(other, str):
            return self.key == other
        return False

    def __hash__(self) -> int:
        return hash(self.key)

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class ListElemId(PathElem):
    id_field: str
    id_value: str
    _id_separator: ClassVar[str] = ":"

    @property
    def id(self) -> str:
        return f"{self.id_field}{self._id_separator}{self.id_value}"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ListElemId):
            return self.id_field == other.id_field and self.id_value == other.id_value
        elif isinstance(other, str):
            return f"{self.id_field}{self._id_separator}{self.id_value}" == other
        return False

    def __hash__(self) -> int:
        return hash((self.id_field, self.id_value))


@dataclass(frozen=True)
class Wildcard(PathElem):
    _wildcard: ClassVar[str] = "*"

    @property
    def id(self) -> str:
        return self._wildcard

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PathElem):
            return True
        return False

    def __hash__(self) -> int:
        return hash(self._wildcard)


@dataclass(frozen=True)
class Path:
    elements: list[PathElem]
    _elem_separator: ClassVar[str] = ","

    def __str__(self) -> str:
        return self._elem_separator.join(elem.id for elem in self.elements)

    def add(self, element: PathElem) -> "Path":
        return Path(self.elements + [element])

    def __add__(self, other: "Path") -> "Path":
        if not isinstance(other, Path):
            raise ValueError(
                f"Can only concatenate Path (not {type(other).__name__}) to Path"
            )
        return Path(self.elements + other.elements)

    @classmethod
    def from_string(cls, path: str) -> "Path":
        return cls.from_list(path.split(cls._elem_separator))

    @classmethod
    def from_list(cls, path: list[str]) -> "Path":
        elements: list[PathElem] = []
        for part in path:
            if Wildcard._wildcard == part:
                elements.append(Wildcard())
            elif ListElemId._id_separator in part:
                key, value = part.split(ListElemId._id_separator)
                elements.append(ListElemId(key, value))
            else:
                elements.append(DictKey(part))
        return cls(elements)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Path):
            return False
        return wildcard_match_paths(self, other)

    def __hash__(self) -> int:
        return hash(tuple(self.elements))

    def __iter__(self) -> Iterator[PathElem]:
        return iter(self.elements)

    @overload
    def __getitem__(self, index: slice) -> "Path":
        ...

    @overload
    def __getitem__(self, index: int) -> PathElem:
        ...

    def __getitem__(self, index: int | slice) -> Union[PathElem, "Path"]:
        if isinstance(index, slice):
            return Path(elements=self.elements[index])
        else:
            return self.elements[index]

    def __len__(self) -> int:
        return len(self.elements)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Path):
            return wildcard_contains(item.elements, self.elements)
        if isinstance(item, PathElem):
            return item in self.elements
        if isinstance(item, str):
            return item in str(self)
        return False


def wildcard_contains(seq1: list[PathElem], seq2: list[PathElem]) -> bool:
    @lru_cache(maxsize=None)
    def match_helper(index1: int, index2: int) -> bool:
        if index1 == len(seq1):
            return True
        if index2 == len(seq2):
            return False
        if index1 < len(seq1) and isinstance(seq1[index1], Wildcard):
            return any(
                match_helper(index1 + 1, j) for j in range(index2, len(seq2) + 1)
            )
        if index2 < len(seq2) and isinstance(seq2[index2], Wildcard):
            return any(
                match_helper(i, index2 + 1) for i in range(index1, len(seq1) + 1)
            )
        return seq1[index1] == seq2[index2] and match_helper(index1 + 1, index2 + 1)

    # Check if the first elements are wildcards or if they match directly without wildcards
    # If not, then do not allow flexible start positions for matching
    allow_flexible_start = isinstance(seq1[0], Wildcard) or isinstance(
        seq2[0], Wildcard
    )
    if not allow_flexible_start:
        return match_helper(0, 0)  # Start matching from the beginning of both sequences

    return any(match_helper(0, start) for start in range(len(seq2) + 1))


def match_sequences(seq1: list[PathElem], seq2: list[PathElem]) -> bool:
    @lru_cache(maxsize=None)
    def match_helper(index1: int, index2: int) -> bool:
        # End of both sequences reached, successful match
        if index1 == len(seq1) and index2 == len(seq2):
            return True
        # End of one sequence but not the other, unsuccessful match
        if index1 == len(seq1) or index2 == len(seq2):
            return index1 == len(seq1) and index2 == len(seq2)
        # Wildcard in seq1
        if index1 < len(seq1) and isinstance(seq1[index1], Wildcard):
            # Skip wildcard, match rest of seq1 with any subsequence of seq2
            return any(
                match_helper(index1 + 1, j) for j in range(index2, len(seq2) + 1)
            )
        # Wildcard in seq2
        if index2 < len(seq2) and isinstance(seq2[index2], Wildcard):
            # Skip wildcard, match rest of seq2 with any subsequence of seq1
            return any(
                match_helper(i, index2 + 1) for i in range(index1, len(seq1) + 1)
            )

        # Direct match for the current elements and the rest
        return seq1[index1] == seq2[index2] and match_helper(index1 + 1, index2 + 1)

    # Start matching from the beginning of both sequences
    return match_helper(0, 0)


def wildcard_match_paths(path0: Path, path1: Path) -> bool:
    """Check if the patches match using wildcard"""
    return match_sequences(path0.elements, path1.elements)


def wildcard_contains_path(path_to_check: Path, path_container: Path) -> bool:
    """Check if path_to_check is contained in patch_container considering wildcard"""
    return wildcard_contains(path_to_check.elements, path_container.elements)


def path_matches_any_with_wildcard(path: Path, paths: Iterable[Path]) -> bool:
    """Check if the target_path matches any path in the paths using wildcard matching."""
    return any(wildcard_match_paths(path, _path) for _path in paths)
