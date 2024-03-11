from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Iterator, Union, overload


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
            if ListElemId._id_separator in part:
                key, value = part.split(ListElemId._id_separator)
                elements.append(ListElemId(key, value))
            else:
                elements.append(DictKey(part))
        return cls(elements)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Path):
            return False
        return self.elements == other.elements

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
            # traverse item and if elems match self return True
            if len(item) > len(self):
                return False
            for i, elem in enumerate(item.elements):
                if elem != self.elements[i]:
                    return False
            return True
        if isinstance(item, PathElem):
            return item in self.elements
        if isinstance(item, str):
            return item in str(self)
        return False
