"""Splitting lookup trees between Redis and the in-memory evaluator."""

from dataclasses import dataclass
from datetime import MAXYEAR, MINYEAR, date, datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from ..codec import datetime_to_micros
from .ast import MATCH_ALL, And, Condition, Not, Or, Xor, conjoin, make_and

RANGE_LOOKUPS = frozenset({"exact", "gt", "gte", "lt", "lte", "range", "in"})
RANGE_TRANSFORMS = frozenset({None, "date", "year"})


def split(node, can_push):
    """Split ``node`` into ``(pushed, residual)``.

    A record matches ``node`` exactly when it matches both parts. ``pushed``
    only contains conditions accepted by ``can_push``; it is ``None`` when
    nothing can be pushed. ``residual`` is ``MATCH_ALL`` when everything was
    pushed.
    """
    if isinstance(node, Condition):
        return (node, MATCH_ALL) if can_push(node) else (None, node)
    if isinstance(node, And):
        pushed, residual = [], MATCH_ALL
        for child in node.children:
            child_pushed, child_residual = split(child, can_push)
            if child_pushed is not None:
                pushed.append(child_pushed)
            residual = conjoin(residual, child_residual)
        return (make_and(pushed) if pushed else None), residual
    if isinstance(node, Or):
        parts = [split(child, can_push) for child in node.children]
        if all(p is not None and r == MATCH_ALL for p, r in parts):
            return Or(tuple(p for p, _ in parts)), MATCH_ALL
        return None, node
    if isinstance(node, Not):
        pushed, residual = split(node.child, can_push)
        if pushed is not None and residual == MATCH_ALL:
            return Not(pushed), MATCH_ALL
        return None, node
    if isinstance(node, Xor):
        return None, node
    raise TypeError(f"Unknown lookup node {node!r}")


def is_range_condition(condition):
    """Whether a datetime ``condition`` can be answered with ``score_ranges``."""
    if condition.lookup not in RANGE_LOOKUPS:
        return False
    if condition.transform not in RANGE_TRANSFORMS:
        return False
    if condition.lookup in ("in", "range"):
        values = condition.value
    else:
        values = (condition.value,)
    if condition.transform == "year":
        return all(MINYEAR < year < MAXYEAR for year in values)
    if condition.transform == "date":
        return all(value < date.max for value in values)
    return True


@dataclass(frozen=True)
class ScoreRange:
    """An interval of epoch microseconds; ``None`` bounds are unbounded."""

    low: int | None
    high: int | None
    low_inclusive: bool = True
    high_inclusive: bool = True


def score_ranges(condition):
    """Return the intervals matched by a datetime ``condition``.

    ``condition`` must satisfy ``is_range_condition``. ``__date`` and
    ``__year`` values are converted to boundaries in the current time zone,
    which is how the evaluator applies those transforms.
    """
    if condition.lookup == "in":
        return [
            interval
            for value in sorted(condition.value)
            for interval in score_ranges(
                Condition(condition.field, condition.transform, "exact", value)
            )
        ]
    start, end = _bounds_function(condition.transform)
    lookup, value = condition.lookup, condition.value
    exact_bounds = condition.transform is None
    if lookup == "exact":
        return [ScoreRange(start(value), end(value), high_inclusive=exact_bounds)]
    if lookup == "range":
        low, high = value
        return [ScoreRange(start(low), end(high), high_inclusive=exact_bounds)]
    if lookup == "gt":
        return [ScoreRange(end(value), None, low_inclusive=not exact_bounds)]
    if lookup == "gte":
        return [ScoreRange(start(value), None)]
    if lookup == "lt":
        return [ScoreRange(None, start(value), high_inclusive=False)]
    return [ScoreRange(None, end(value), high_inclusive=exact_bounds)]


def conjunction_range(node, field, can_push):
    """Return the single interval ``node`` restricts ``field`` to, or ``None``.

    Only conjunctions of range conditions on ``field`` qualify, which is the
    shape the admin's date filters and date hierarchy produce.
    """
    children = node.children if isinstance(node, And) else (node,)
    interval = ScoreRange(None, None)
    for child in children:
        if (
            not isinstance(child, Condition)
            or child.field != field
            or not is_range_condition(child)
            or not can_push(child)
        ):
            return None
        ranges = score_ranges(child)
        if len(ranges) != 1:
            return None
        interval = _intersect(interval, ranges[0])
        if interval is None:
            return None
    return interval


def _intersect(left, right):
    low, low_inclusive = _wider(
        (left.low, left.low_inclusive), (right.low, right.low_inclusive), lower=False
    )
    high, high_inclusive = _wider(
        (left.high, left.high_inclusive), (right.high, right.high_inclusive), lower=True
    )
    if low is not None and high is not None and low > high:
        return None
    return ScoreRange(low, high, low_inclusive, high_inclusive)


def _wider(left, right, *, lower):
    """Return the tighter of two bounds."""
    if left[0] is None:
        return right
    if right[0] is None:
        return left
    if left[0] == right[0]:
        return left[0], left[1] and right[1]
    tighter = min if lower else max
    return left if tighter(left[0], right[0]) == left[0] else right


def _bounds_function(transform):
    """Return ``(start, end)`` mapping a lookup value to micros.

    Without a transform both return the value itself (``end`` is then an
    inclusive bound); with a transform ``end`` is the exclusive start of the
    next period.
    """
    if transform is None:
        return datetime_to_micros, datetime_to_micros
    if transform == "date":
        return _start_of_day, lambda value: _start_of_day(value + timedelta(days=1))
    return (
        lambda year: _start_of_day(date(year, 1, 1)),
        lambda year: _start_of_day(date(year + 1, 1, 1)),
    )


def _start_of_day(value):
    moment = datetime.combine(value, time.min)
    if settings.USE_TZ:
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return datetime_to_micros(moment)
