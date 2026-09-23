"""The ``indexed`` driver: record hashes plus secondary indexes in Redis.

Per model, the write script maintains:

* ``all``: set of every primary key;
* ``s:<field>:<value>``: set of primary keys per value of a tag field;
* ``v:<field>``: set of the distinct values of a tag field;
* ``n:<field>``: set of primary keys whose field is ``None``;
* ``z:<field>``: sorted set of primary keys scored by a datetime field.

Queries turn the indexable part of a lookup tree into set operations on
temporary keys.
"""

from contextlib import contextmanager

from ..query.ast import MATCH_ALL, And, Condition, Not, Or
from ..query.planner import conjunction_range, is_range_condition, score_ranges
from .base import batched
from .pushdown import PushdownStore, Selection

TEMPORARY_KEY_TTL_MS = 60_000


class IndexedStore(PushdownStore):
    supports_ttl = False

    def index_payload(self):
        return {
            "prefix": self.keys.index_prefix,
            "tags": list(self.schema.tag_fields),
            "dates": list(self.schema.date_fields),
            "counts": [],
            "all": True,
        }

    def can_push(self, condition):
        schema = self.schema
        field = condition.field
        if condition.transform is None and condition.lookup == "isnull":
            return field in schema.tag_fields or field in schema.date_fields
        if field == schema.pk or field in schema.tag_fields:
            return condition.transform is None and condition.lookup in ("exact", "in")
        if field in schema.date_fields:
            return is_range_condition(condition)
        return False

    @contextmanager
    def selection(self, pushed, order_field=None):
        if pushed is None:
            key = (
                self.keys.all_members
                if order_field is None
                else self.keys.scores(order_field)
            )
            yield SortedSetSelection(self.client, key)
            return
        scored = self._scored_range(pushed, order_field)
        if scored is not None:
            yield scored
            return
        with self._materialized(pushed, order_field) as key:
            yield SortedSetSelection(self.client, key)

    def _scored_range(self, pushed, order_field):
        """Read a date index directly when ``pushed`` only restricts that field.

        The index holds every record with a value for the field, and a range
        condition cannot match a record without one, so the scores alone answer
        the query.
        """
        for field in [order_field] if order_field else self.schema.date_fields:
            interval = conjunction_range(pushed, field, self.can_push)
            if interval is not None:
                return SortedSetSelection(
                    self.client, self.keys.scores(field), interval
                )
        return None

    @contextmanager
    def _materialized(self, pushed, order_field=None):
        """Yield a temporary sorted set holding the primary keys of ``pushed``."""
        pipe = self.client.pipeline(transaction=True)
        builder = _SetBuilder(self, pipe)
        source = builder.build(pushed)
        result = builder.temporary()
        if order_field is None:
            pipe.zunionstore(result, [source])
        else:
            pipe.zinterstore(result, {source: 0, self.keys.scores(order_field): 1})
        for key in builder.temporary_keys:
            pipe.pexpire(key, TEMPORARY_KEY_TTL_MS)
        pipe.execute()
        try:
            yield result
        finally:
            self.client.unlink(*builder.temporary_keys)

    def get_many(self, pks):
        records, missing = [], []
        for batch in batched(pks, self.batch_size):
            pipe = self.client.pipeline(transaction=False)
            for pk in batch:
                pipe.hmget(self.keys.record(pk), self.field_names)
            for pk, values in zip(batch, pipe.execute(), strict=True):
                record = self.read(values)
                if record is None:
                    missing.append(pk)
                else:
                    records.append(record)
        if missing:
            self.repair(missing)
        return records

    def repair(self, pks):
        """Remove index entries of ``pks`` whose records no longer exist."""
        for batch in batched(pks, self.batch_size):
            self._run(
                {
                    "op": "repair",
                    "keys": [self.keys.record(pk) for pk in batch],
                    "pks": batch,
                    "fields": self.field_names,
                }
            )

    def rebuild(self):
        super().rebuild()
        pattern = f"{self.keys.index_prefix}*"
        for batch in batched(self.client.scan_iter(match=pattern), self.batch_size):
            self.client.unlink(*batch)
        for batch in batched(self.scan_pks(), self.batch_size):
            self._run(
                {
                    "op": "reindex",
                    "keys": [self.keys.record(pk) for pk in batch],
                    "pks": batch,
                    "fields": self.field_names,
                }
            )

    def count(self, where):
        pushed, complete = self.plan(where)
        if not complete:
            return super().count(where)
        keys = self._member_keys(pushed)
        if keys is None:
            return super().count(where)
        if len(keys) == 1:
            return self.client.zcard(keys[0])
        return self.client.zintercard(len(keys), keys)

    def _member_keys(self, node):
        """Index keys whose intersection holds exactly ``node``, or ``None``.

        Counting such a query needs no temporary key, which keeps the facet
        counts of a changelist cheap.
        """
        if node is None:
            return [self.keys.all_members]
        if isinstance(node, Condition):
            if node.lookup == "isnull" and node.value:
                return [self.keys.nulls(node.field)]
            if node.field in self.schema.tag_fields and node.lookup == "exact":
                return [self.keys.members(node.field, node.value)]
            return None
        if isinstance(node, And):
            keys = []
            for child in node.children:
                child_keys = self._member_keys(child)
                if child_keys is None:
                    return None
                keys += child_keys
            return keys
        return None

    def fetch(self, where, ordering, low=0, high=None):
        pushed, complete = self.plan(where)
        if complete and self._orders_by_tag(ordering) and (high is None or high > low):
            return self.get_many(self._tag_window(pushed, ordering, low, high))
        return super().fetch(where, ordering, low, high)

    def _tag_window(self, pushed, ordering, low, high):
        """Return the primary keys of a page ordered by a tag field.

        The primary keys of a value are already ordered inside its index, so
        each page only reads the groups it overlaps.
        """
        if pushed is None:
            return self._tag_page(None, ordering, low, high)
        with self._materialized(pushed) as source:
            return self._tag_page(source, ordering, low, high)

    def _tag_page(self, source, ordering, low, high):
        (field, descending), (_, pk_descending) = ordering
        return self.group_window(
            self._tag_groups(source, field, descending),
            low,
            high,
            lambda key, offset, count: self._group_members(
                source, key, offset, count, pk_descending
            ),
        )

    def _tag_groups(self, source, field, descending):
        """Return ``(index key, count)`` per value of ``field``, in order."""
        values = sorted(self.client.smembers(self.keys.values(field)))
        keys = [self.keys.members(field, value) for value in values]
        if descending:
            keys.reverse()
        # NULL sorts after every value ascending, and first descending.
        nulls = self.keys.nulls(field)
        keys = [nulls, *keys] if descending else [*keys, nulls]
        pipe = self.client.pipeline(transaction=False)
        for key in keys:
            if source is None:
                pipe.zcard(key)
            else:
                pipe.zintercard(2, [source, key])
        return [
            (key, count)
            for key, count in zip(keys, pipe.execute(), strict=True)
            if count
        ]

    def _group_members(self, source, key, offset, count, descending):
        stop = offset + count - 1
        if source is None:
            return self.client.zrange(key, offset, stop, desc=descending)
        destination = self.keys.temporary()
        pipe = self.client.pipeline(transaction=True)
        pipe.zinterstore(destination, {source: 0, key: 0})
        pipe.zrange(destination, offset, stop, desc=descending)
        pipe.unlink(destination)
        return pipe.execute()[1]

    def distinct_values(self, where, field):
        if where != MATCH_ALL or field not in self.schema.tag_fields:
            return super().distinct_values(where, field)
        pipe = self.client.pipeline(transaction=True)
        pipe.smembers(self.keys.values(field))
        pipe.zcard(self.keys.nulls(field))
        values, nulls = pipe.execute()
        return set(values) | ({None} if nulls else set())


class SortedSetSelection(Selection):
    """A sorted set of primary keys, optionally restricted to a score range.

    The range lets queries that only filter on the ordering field read the
    field's index directly instead of building a temporary key.
    """

    def __init__(self, client, key, interval=None):
        self.client = client
        self.key = key
        self.interval = interval

    @property
    def _bounds(self):
        if self.interval is None:
            return "-inf", "+inf"
        return (
            _bound(self.interval.low, self.interval.low_inclusive, "-inf"),
            _bound(self.interval.high, self.interval.high_inclusive, "+inf"),
        )

    def _clip(self, low, high, high_inclusive=True):
        minimum, maximum = self._bounds
        low = _bound(low, True, minimum)
        high = _bound(high, high_inclusive, maximum)
        return _tighter(low, minimum, lower=False), _tighter(high, maximum, lower=True)

    def pks(self):
        if self.interval is None:
            return self.client.zrange(self.key, 0, -1)
        minimum, maximum = self._bounds
        return self.client.zrangebyscore(self.key, minimum, maximum)

    def count(self):
        if self.interval is None:
            return self.client.zcard(self.key)
        return self.client.zcount(self.key, *self._bounds)

    def window(self, low, high, descending):
        num = None if high is None else high - low
        if self.interval is None:
            stop = -1 if high is None else high - 1
            members = self.client.zrange(
                self.key, low, stop, desc=descending, withscores=True
            )
        else:
            minimum, maximum = self._bounds
            command = (
                self.client.zrevrangebyscore
                if descending
                else self.client.zrangebyscore
            )
            arguments = (maximum, minimum) if descending else (minimum, maximum)
            members = command(
                self.key,
                *arguments,
                start=low,
                num=num if num is not None else self.count(),
                withscores=True,
            )
        return _integer_scores(members)

    def between(self, low, high):
        return _integer_scores(
            self.client.zrangebyscore(self.key, *self._clip(low, high), withscores=True)
        )

    def count_between(self, low, high, *, high_inclusive=True):
        return self.client.zcount(self.key, *self._clip(low, high, high_inclusive))

    def count_ranges(self, ranges):
        pipe = self.client.pipeline(transaction=False)
        for low, high in ranges:
            pipe.zcount(self.key, *self._clip(low, high, False))
        return pipe.execute()

    def extremes(self):
        first = self.window(0, 1, False)
        if not first:
            return None
        last = self.window(0, 1, True)
        return first[0][1], last[0][1]


def _integer_scores(members):
    return [(pk, int(score)) for pk, score in members]


class _SetBuilder:
    """Queue commands building a set for a pushable lookup tree."""

    def __init__(self, store, pipe):
        self.keys = store.keys
        self.schema = store.schema
        self.pipe = pipe
        self.temporary_keys = []

    def temporary(self):
        key = self.keys.temporary()
        self.temporary_keys.append(key)
        return key

    def build(self, node):
        if isinstance(node, Condition):
            return self._condition(node)
        if isinstance(node, And):
            return self._combine("zinterstore", [self.build(c) for c in node.children])
        if isinstance(node, Or):
            return self._combine("zunionstore", [self.build(c) for c in node.children])
        if isinstance(node, Not):
            return self._difference(self.build(node.child))
        raise TypeError(f"Cannot build a set for {node!r}")

    def _combine(self, command, sources):
        if len(sources) == 1:
            return sources[0]
        destination = self.temporary()
        if sources:
            getattr(self.pipe, command)(destination, sources)
        return destination

    def _difference(self, source):
        destination = self.temporary()
        self.pipe.zdiffstore(destination, [self.keys.all_members, source])
        return destination

    def _condition(self, condition):
        field = condition.field
        if condition.lookup == "isnull":
            nulls = self.keys.nulls(field)
            return nulls if condition.value else self._difference(nulls)
        values = (
            sorted(condition.value) if condition.lookup == "in" else [condition.value]
        )
        if field == self.schema.pk:
            members = self.temporary()
            self.pipe.sadd(members, *values)
            return self._combine("zinterstore", [members, self.keys.all_members])
        if field in self.schema.tag_fields:
            return self._combine(
                "zunionstore", [self.keys.members(field, v) for v in values]
            )
        sources = []
        for interval in score_ranges(condition):
            destination = self.temporary()
            self.pipe.zrangestore(
                destination,
                self.keys.scores(field),
                _bound(interval.low, interval.low_inclusive, "-inf"),
                _bound(interval.high, interval.high_inclusive, "+inf"),
                byscore=True,
            )
            sources.append(destination)
        return self._combine("zunionstore", sources)


def _bound(value, inclusive, unbounded):
    if value is None:
        return unbounded
    return str(value) if inclusive else f"({value}"


def _tighter(bound, other, *, lower):
    """Return the tighter of two ``ZRANGEBYSCORE`` bounds."""
    if other in ("-inf", "+inf"):
        return bound
    if bound in ("-inf", "+inf"):
        return other
    values = (float(bound.lstrip("(")), float(other.lstrip("(")))
    if values[0] == values[1]:
        return bound if bound.startswith("(") else other
    tighter = min if lower else max
    return bound if tighter(*values) == values[0] else other
