"""In-memory evaluation of lookup trees over decoded records.

This is the reference implementation of lookup semantics: comparisons with
``None`` are false, negation is a plain ``not`` (matching Django's ``exclude()``
on a single table), strings compare by code point, case-insensitive lookups
use ``str.casefold()`` and regular expressions use Python's ``re.search()``.
"""

import functools
import operator
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .ast import And, Condition, Not, Or, Xor


def matches(node, record):
    if isinstance(node, Condition):
        return _matches_condition(node, record)
    if isinstance(node, And):
        return all(matches(child, record) for child in node.children)
    if isinstance(node, Or):
        return any(matches(child, record) for child in node.children)
    if isinstance(node, Xor):
        return sum(matches(child, record) for child in node.children) % 2 == 1
    if isinstance(node, Not):
        return not matches(node.child, record)
    raise TypeError(f"Unknown lookup node {node!r}")


def local_datetime(value, tzinfo=None):
    """Return ``value`` in ``tzinfo`` (the current time zone by default)."""
    if settings.USE_TZ and timezone.is_aware(value):
        return timezone.localtime(value, tzinfo)
    return value


TRANSFORMS = {
    "year": lambda value: value.year,
    "iso_year": lambda value: value.isocalendar()[0],
    "quarter": lambda value: (value.month - 1) // 3 + 1,
    "month": lambda value: value.month,
    "week": lambda value: value.isocalendar()[1],
    "week_day": lambda value: value.isoweekday() % 7 + 1,
    "iso_week_day": lambda value: value.isoweekday(),
    "day": lambda value: value.day,
    "hour": lambda value: value.hour,
    "minute": lambda value: value.minute,
    "second": lambda value: value.second,
    "date": lambda value: value.date(),
    "time": lambda value: value.time(),
}


def apply_transform(value, transform):
    """Extract ``transform`` from a datetime in the current time zone.

    Definitions follow Django: ``week_day`` counts from Sunday (1) to Saturday
    (7), ``week``, ``iso_year`` and ``iso_week_day`` follow ISO-8601.
    """
    return TRANSFORMS[transform](local_datetime(value))


def _casefold(value):
    return value.casefold()


@functools.lru_cache(maxsize=256)
def _regex(pattern, flags):
    return re.compile(pattern, flags)


TEXT_OPERATORS = {
    "iexact": lambda a, b: _casefold(a) == _casefold(b),
    "contains": lambda a, b: b in a,
    "icontains": lambda a, b: _casefold(b) in _casefold(a),
    "startswith": lambda a, b: a.startswith(b),
    "istartswith": lambda a, b: _casefold(a).startswith(_casefold(b)),
    "endswith": lambda a, b: a.endswith(b),
    "iendswith": lambda a, b: _casefold(a).endswith(_casefold(b)),
    "regex": lambda a, b: _regex(b, 0).search(a) is not None,
    "iregex": lambda a, b: _regex(b, re.IGNORECASE).search(a) is not None,
}
OPERATORS = {
    **TEXT_OPERATORS,
    "exact": operator.eq,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": lambda a, b: a in b,
    "range": lambda a, b: b[0] <= a <= b[1],
}


def _matches_condition(condition, record):
    value = record[condition.field]
    if value is not None and condition.transform:
        value = apply_transform(value, condition.transform)
    if condition.lookup == "isnull":
        return (value is None) is condition.value
    if value is None:
        return False
    return OPERATORS[condition.lookup](value, condition.value)


def sort_key(value):
    """Sort key placing ``None`` after every value, like PostgreSQL."""
    return (True, 0) if value is None else (False, value)


def order_records(records, ordering):
    """Sort ``records`` in place by ``ordering``, a sequence of (field, descending)."""
    for field, descending in reversed(ordering):
        records.sort(key=lambda record: sort_key(record[field]), reverse=descending)
    return records


@dataclass(frozen=True)
class AggregateSpec:
    function: str
    field: str | None
    distinct: bool
    where: object
    default: object


def aggregate(records, specs):
    """Evaluate ``specs`` (alias -> AggregateSpec) over ``records``."""
    records = list(records)
    return {alias: _aggregate(spec, records) for alias, spec in specs.items()}


def _aggregate(spec, records):
    if spec.where is not None:
        records = [record for record in records if matches(spec.where, record)]
    if spec.field is None:
        values = [True] * len(records)
    else:
        values = [r[spec.field] for r in records if r[spec.field] is not None]
    if spec.distinct:
        values = list(set(values))
    if spec.function == "Count":
        return len(values)
    if not values:
        return spec.default
    if spec.function == "Min":
        return min(values)
    if spec.function == "Max":
        return max(values)
    if spec.function == "Sum":
        return sum(values)
    return sum(values) / len(values)


def truncate(value, kind, tzinfo=None):
    """Truncate a datetime like ``Trunc(kind)``; aware results use ``tzinfo``."""
    if settings.USE_TZ:
        tzinfo = tzinfo or timezone.get_current_timezone()
        value = timezone.localtime(value, tzinfo).replace(tzinfo=None)
    if kind == "year":
        value = value.replace(month=1, day=1)
    elif kind == "quarter":
        value = value.replace(month=(value.month - 1) // 3 * 3 + 1, day=1)
    elif kind == "month":
        value = value.replace(day=1)
    elif kind == "week":
        value = value - timedelta(days=value.weekday())
    if kind in ("year", "quarter", "month", "week", "day"):
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elif kind == "hour":
        value = value.replace(minute=0, second=0, microsecond=0)
    elif kind == "minute":
        value = value.replace(second=0, microsecond=0)
    elif kind == "second":
        value = value.replace(microsecond=0)
    if settings.USE_TZ:
        value = timezone.make_aware(value, tzinfo)
    return value


def truncate_to_date(value, kind):
    """Truncate a datetime like ``dates(kind)``: a date in the current time zone."""
    return truncate(value, kind).date()
