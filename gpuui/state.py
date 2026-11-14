"""Bindings usable with Blender RNA or independent editor documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass
class Binding(Generic[T]):
    get: Callable[[object], T]
    set: Callable[[object, T], None]

    @classmethod
    def attribute(cls, owner, name: str):
        if not hasattr(owner, name):
            raise AttributeError(name)
        return cls(lambda _context: getattr(owner, name), lambda _context, value: setattr(owner, name, value))


@dataclass
class State(Generic[T]):
    value: T

    def binding(self) -> Binding[T]:
        return Binding.attribute(self, "value")
