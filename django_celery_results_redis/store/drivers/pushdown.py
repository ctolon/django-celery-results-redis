"""Query execution shared by the drivers that can filter inside Redis.

A driver describes which conditions it can evaluate (``can_push``) and how to
select the matching records (``selection``). This module turns those into
counts, pages, aggregates and date buckets, and falls back to the in-memory
evaluator for the conditions the driver cannot evaluate.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..codec import datetime_to_micros, micros_to_datetime
from ..query import evaluator
from ..query.ast import MATCH_ALL, MATCH_NONE, Condition, conjoin
from ..query.planner import split
from .base import Store, batched

DATETIME_BUCKET_KINDS = ("year", "month", "day")
NOT_PUSHED = object()


class Selection:
    """The records matching a pushed lookup tree, as seen by a driver.

    With an ``order_field``, members are scored by that datetime field and
    records where it is ``None`` are excluded. Scores are epoch microseconds.
    """

    def pks(self):
        """Return every selected primary key, or ``None`` if there are too many."""
        raise NotImplementedError

    def count(self):
        raise NotImplementedError

    def window(self, low, high, descending):
        """Return ``(pk, score)`` pairs ranked ``low`` to ``high`` by score."""
        raise NotImplementedError

    def between(self, low, high):
        """Return ``(pk, score)`` pairs with ``low <= score <= high``."""
        raise NotImplementedError

    def count_between(self, low, high, *, high_inclusive=True):
        """Count members with ``low <= score <= high``; ``None`` is unbounded."""
        raise NotImplementedError

    def count_ranges(self, ranges):
        """Count members in each ``[low, high)`` range."""
        return [
            self.count_between(low, high, high_inclusive=False) for low, high in ranges
        ]

    def extremes(self):
        """Return the lowest and highest score, or ``None`` if empty."""
        raise NotImplementedError


class PushdownStore(Store):
    def can_push(self, condition):
        raise NotImplementedError

    def selection(self, pushed, order_field=None):
        """Return a context manager yielding a ``Selection`` for ``pushed``.

        ``pushed`` is a tree accepted by ``can_push``, or ``None`` for every
        record.
        """
        raise NotImplementedError

    def split(self, where):
        return split(where, self.can_push)

    def plan(self, where):
        """Return ``(pushed, complete)``; ``complete`` if nothing is left over."""
        pushed, residual = self.split(where)
        return pushed, residual == MATCH_ALL

    def _orders_by_tag(self, ordering):
        (field, _), *rest = ordering
        return field in self.schema.tag_fields and [name for name, _ in rest] == [
            self.schema.pk
        ]

    def group_window(self, groups, low, high, page):
        """Return the primary keys of a window over ordered groups.

        ``groups`` are ``(group, count)`` pairs in the requested order and
        ``page(group, offset, count)`` reads part of one group, so only the
        groups the window overlaps are read.
        """
        pks, position = [], 0
        for group, count in groups:
            if position + count <= low:
                position += count
                continue
            offset = max(0, low - position)
            take = count - offset
            if high is not None:
                take = min(take, high - max(low, position))
            pks += page(group, offset, take)
            position += count
            if high is not None and position >= high:
                break
        return pks

    def _orders_by_score(self, ordering):
        (field, _), *rest = ordering
        return (
            field in self.schema.date_fields
            and not self.schema.fields[field].null
            and [name for name, _ in rest] == [self.schema.pk]
        )

    def candidate_records(self, where):
        pushed, _ = self.split(where)
        with self.selection(pushed) as selection:
            pks = selection.pks()
        if pks is None:
            yield from super().candidate_records(where)
            return
        for batch in batched(pks, self.batch_size):
            yield from self.get_many(batch)

    def matching_pks(self, where):
        if where == MATCH_NONE:
            return []
        pushed, complete = self.plan(where)
        if not complete:
            return super().matching_pks(where)
        with self.selection(pushed) as selection:
            pks = selection.pks()
        return super().matching_pks(where) if pks is None else pks

    def fetch(self, where, ordering, low=0, high=None):
        if where == MATCH_NONE or (high is not None and high <= low):
            return []
        pushed, complete = self.plan(where)
        if not complete or not self._orders_by_score(ordering):
            return super().fetch(where, ordering, low, high)
        (field, descending), (_, pk_descending) = ordering
        with self.selection(pushed, order_field=field) as selection:
            members = selection.window(low, high, descending)
            if members:
                members = self._order_ties(
                    selection, members, low, descending, pk_descending
                )
        return self.get_many([pk for pk, _ in members])

    def _order_ties(self, selection, members, low, descending, pk_descending):
        """Order members with equal scores by primary key.

        Fetch every member scored between the first and last score of the
        window, sort them by (score, primary key) and cut the window again.
        """
        first, last = members[0][1], members[-1][1]
        if descending:
            span = selection.between(last, first)
            before = selection.count_between(first + 1, None)
        else:
            span = selection.between(first, last)
            before = selection.count_between(None, first - 1)
        span.sort(key=lambda member: member[0], reverse=pk_descending)
        span.sort(key=lambda member: member[1], reverse=descending)
        offset = low - before
        return span[offset : offset + len(members)]

    def count(self, where):
        if where == MATCH_NONE:
            return 0
        pushed, complete = self.plan(where)
        if not complete:
            return super().count(where)
        with self.selection(pushed) as selection:
            return selection.count()

    def exists(self, where):
        if not self.plan(where)[1]:
            return super().exists(where)
        return self.count(where) > 0

    def aggregate(self, where, specs):
        results, remaining = {}, {}
        for alias, spec in specs.items():
            value = self._aggregate_in_redis(where, spec)
            if value is NOT_PUSHED:
                remaining[alias] = spec
            else:
                results[alias] = value
        if remaining:
            results.update(super().aggregate(where, remaining))
        return results

    def _aggregate_in_redis(self, where, spec):
        condition = where if spec.where is None else conjoin(where, spec.where)
        if spec.field is not None and self.schema.fields[spec.field].null:
            condition = conjoin(condition, Condition(spec.field, None, "isnull", False))
        if spec.function == "Count" and (
            not spec.distinct or spec.field in (None, self.schema.pk)
        ):
            if condition == MATCH_NONE or self.plan(condition)[1]:
                return self.count(condition)
            return NOT_PUSHED
        if spec.function in ("Min", "Max") and spec.field in self.schema.date_fields:
            if condition == MATCH_NONE:
                return spec.default
            pushed, complete = self.plan(condition)
            if not complete:
                return NOT_PUSHED
            with self.selection(pushed, order_field=spec.field) as selection:
                extremes = selection.extremes()
            if extremes is None:
                return spec.default
            score = extremes[0] if spec.function == "Min" else extremes[1]
            return micros_to_datetime(score)
        return NOT_PUSHED

    def datetimes(self, where, field, kind, tzinfo=None):
        if where == MATCH_NONE:
            return []
        pushed, complete = self.plan(where)
        if (
            not complete
            or field not in self.schema.date_fields
            or kind not in DATETIME_BUCKET_KINDS
        ):
            return super().datetimes(where, field, kind, tzinfo)
        with self.selection(pushed, order_field=field) as selection:
            extremes = selection.extremes()
            if extremes is None:
                return []
            first, last = (micros_to_datetime(score) for score in extremes)
            periods = list(periods_between(first, last, kind, tzinfo))
            counts = selection.count_ranges(
                [
                    (datetime_to_micros(start), datetime_to_micros(end))
                    for start, end in periods
                ]
            )
        return [
            start for (start, _), count in zip(periods, counts, strict=True) if count
        ]


def periods_between(first, last, kind, tzinfo=None):
    """Yield ``(start, end)`` of each ``kind`` period from ``first`` to ``last``."""
    start = evaluator.truncate(first, kind, tzinfo)
    while start <= last:
        end = next_period(start, kind)
        yield start, end
        start = end


def next_period(start, kind):
    naive = start.replace(tzinfo=None)
    if kind == "day":
        naive += timedelta(days=1)
    elif kind == "month":
        years, month = divmod(naive.month, 12)
        naive = naive.replace(year=naive.year + years, month=month + 1)
    else:
        naive = naive.replace(year=naive.year + 1)
    if settings.USE_TZ:
        return timezone.make_aware(naive, start.tzinfo)
    return naive
