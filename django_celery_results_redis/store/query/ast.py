"""Lookup trees produced by the compiler and consumed by the drivers."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Condition:
    """``field[__transform]__lookup=value`` with a prepared ``value``."""

    field: str
    transform: str | None
    lookup: str
    value: Any


@dataclass(frozen=True)
class And:
    children: tuple = ()


@dataclass(frozen=True)
class Or:
    children: tuple = ()


@dataclass(frozen=True)
class Xor:
    children: tuple = ()


@dataclass(frozen=True)
class Not:
    child: Any


MATCH_ALL = And()
MATCH_NONE = Or()


def make_and(children):
    """Build a conjunction, flattening it and folding constant children."""
    flat = []
    for child in children:
        if child == MATCH_NONE:
            return MATCH_NONE
        flat.extend(child.children if isinstance(child, And) else (child,))
    return flat[0] if len(flat) == 1 else And(tuple(flat))


def make_or(children):
    """Build a disjunction, folding constant children."""
    kept = []
    for child in children:
        if child == MATCH_ALL:
            return MATCH_ALL
        if child != MATCH_NONE:
            kept.append(child)
    return kept[0] if len(kept) == 1 else Or(tuple(kept))


def make_not(child):
    if child == MATCH_ALL:
        return MATCH_NONE
    if child == MATCH_NONE:
        return MATCH_ALL
    return Not(child)


def conjoin(left, right):
    """Return ``left AND right``."""
    return make_and((left, right))


def primary_key_candidates(node, pk):
    """Return the primary keys ``node`` can match, or ``None`` if unrestricted."""
    if isinstance(node, Condition):
        if node.field != pk or node.transform is not None:
            return None
        if node.lookup == "exact":
            return {node.value}
        if node.lookup == "in":
            return set(node.value)
        return None
    if isinstance(node, And):
        candidates = None
        for child in node.children:
            child_candidates = primary_key_candidates(child, pk)
            if child_candidates is not None:
                candidates = (
                    child_candidates
                    if candidates is None
                    else candidates & child_candidates
                )
        return candidates
    if isinstance(node, Or):
        candidates = set()
        for child in node.children:
            child_candidates = primary_key_candidates(child, pk)
            if child_candidates is None:
                return None
            candidates |= child_candidates
        return candidates
    return None
