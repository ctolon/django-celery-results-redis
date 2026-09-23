"""The ``redis_om`` driver: a RediSearch index declared with redis-om.

Each Django model gets a redis-om ``HashModel`` describing the index over the
record hashes: datetimes are ``NUMERIC`` fields holding epoch microseconds, the
null marker is a ``TAG`` list of the fields that are ``None``, and every tag
field is indexed through an encoded copy of its value.

The copy exists because RediSearch splits tag values on a separator character
and trims them, so some values cannot be matched exactly. Records therefore
carry ``t_<field>``, the value in URL-safe base64, which contains no character
RediSearch treats specially. Every value is then matched exactly, and the
distinct values of a field can be read back from the index.

redis-om provides the index schema and the query strings; records are written by
the shared Lua script, because ``HashModel.save()`` never removes fields that
became ``None``.
"""

import base64
import functools
import hashlib
import operator
import time
import types
from contextlib import contextmanager
from typing import Optional

from redis.commands.search.query import Query
from redis.exceptions import ResponseError
from redis_om import Field, HashModel

from ..codec import NULL_MARKER_FIELD
from ..query.ast import MATCH_ALL, MATCH_NONE, And, Condition, Not, Or, conjoin
from ..query.planner import is_range_condition, score_ranges
from .base import batched
from .pushdown import PushdownStore, Selection

DIALECT = 2
INDEX_POLL_INTERVAL = 0.05
#: Fallback for the server's FT.SEARCH result limit. Larger result sets are
#: enumerated by scanning the keyspace instead, which stays consistent.
DEFAULT_MAX_SEARCH_RESULTS = 1_000_000
#: A tag value with more matches than this is ordered by reading the records
#: instead of sorting its primary keys here.
MAX_GROUP_PKS = 100_000
#: Matches nothing: the null marker only ever holds field names.
IMPOSSIBLE_QUERY = f"@{NULL_MARKER_FIELD}:{{__none__}}"


def tag_field(field):
    return f"t_{field}"


def encode_tag(value):
    """Encode a tag value so that RediSearch stores and matches it verbatim.

    The leading character keeps the encoding of an empty string non-empty,
    which RediSearch cannot index or query.
    """
    encoded = base64.urlsafe_b64encode(str(value).encode()).decode()
    return "v" + encoded.rstrip("=")


def document_model(store):
    """Return a ``HashModel`` describing the records of ``store``."""
    schema, keys = store.schema, store.keys
    annotations = {schema.pk: str}
    attrs = {schema.pk: Field(primary_key=True)}
    for name in schema.tag_fields:
        encoded = tag_field(name)
        annotations[encoded] = Optional[str]  # noqa: UP045 (redis-om reads Optional)
        attrs[encoded] = Field(
            default=None, index=True, case_sensitive=True, separator=","
        )
    for name in schema.date_fields:
        annotations[name] = Optional[int]  # noqa: UP045
        attrs[name] = Field(default=None, index=True, sortable=True)
    annotations[NULL_MARKER_FIELD] = Optional[str]  # noqa: UP045
    attrs[NULL_MARKER_FIELD] = Field(
        default=None, index=True, case_sensitive=True, separator=","
    )
    meta = type(
        "Meta",
        (),
        {
            "global_key_prefix": keys.prefix,
            "model_key_prefix": keys.kind,
            "index_name": keys.search_index,
            "database": store.client,
        },
    )
    namespace = {
        "__module__": __name__,
        "__annotations__": annotations,
        "Meta": meta,
        **attrs,
    }
    return types.new_class(
        f"{schema.model.__name__}Document",
        (HashModel,),
        {"index": True},
        lambda body: body.update(namespace),
    )


class RedisOMStore(PushdownStore):
    def prepare(self):
        super().prepare()
        self.document = document_model(self)
        self.ft = self.client.ft(self.keys.search_index)
        self.max_search_results = self._max_search_results()
        self.ensure_index()

    def _max_search_results(self):
        try:
            config = self.client.config_get("search-max-search-results")
        except ResponseError:
            return DEFAULT_MAX_SEARCH_RESULTS
        value = int(next(iter(config.values()), DEFAULT_MAX_SEARCH_RESULTS))
        return DEFAULT_MAX_SEARCH_RESULTS if value < 0 else value

    def ensure_index(self):
        """Create the search index, or recreate it if its schema changed."""
        schema = self.document.redisearch_schema()
        digest = hashlib.sha256(schema.encode()).hexdigest()
        digest_key = f"{self.keys.search_index}:schema"
        if self.client.get(digest_key) == digest and self._index_exists():
            return
        if self._index_exists():
            self.ft.dropindex(delete_documents=False)
        try:
            self.client.execute_command(
                "FT.CREATE", self.keys.search_index, *schema.split()
            )
        except ResponseError as exc:
            if "already exists" not in str(exc).lower():
                raise
        self.client.set(digest_key, digest)

    def _index_exists(self):
        try:
            self.ft.info()
        except ResponseError:
            return False
        return True

    def rebuild(self):
        super().rebuild()
        self._rebuild_counts()
        if self._index_exists():
            self.ft.dropindex(delete_documents=False)
        self.client.delete(f"{self.keys.search_index}:schema")
        self.ensure_index()
        while int(self.ft.info().get("indexing", 0)):
            time.sleep(INDEX_POLL_INTERVAL)

    def _rebuild_counts(self):
        pattern = f"{self.keys.index_prefix}*"
        for batch in batched(self.client.scan_iter(match=pattern), self.batch_size):
            self.client.unlink(*batch)
        for batch in self.scan_batches():
            self._run(
                {
                    "op": "reindex",
                    "keys": [self.keys.record(pk) for pk in batch],
                    "pks": batch,
                    "fields": self.field_names,
                }
            )

    @property
    def maintains_index_fields(self):
        return True

    def index_payload(self):
        """Count the distinct values of each tag field.

        The counters answer a list filter's choices. RediSearch cannot: its
        aggregations are not consistent with writes that just happened.
        """
        return {
            "prefix": self.keys.index_prefix,
            "tags": [],
            "dates": [],
            "counts": list(self.schema.tag_fields),
            "all": False,
        }

    def index_values(self, pk, values):
        indexed = super().index_values(pk, values)
        for field in self.schema.tag_fields:
            if field in values:
                value = values[field]
                indexed[tag_field(field)] = None if value is None else encode_tag(value)
        return indexed

    def can_push(self, condition):
        schema = self.schema
        field = condition.field
        if condition.transform is not None:
            return field in schema.date_fields and is_range_condition(condition)
        if condition.lookup == "isnull":
            return field in schema.tag_fields or field in schema.date_fields
        if field in schema.tag_fields:
            return condition.lookup in ("exact", "in")
        if field in schema.date_fields:
            return is_range_condition(condition)
        return False

    def query_string(self, pushed):
        if pushed is None:
            return "*"
        if pushed == MATCH_NONE:
            return IMPOSSIBLE_QUERY
        return self.document.find(self._expression(pushed)).query

    def _expression(self, node):
        if isinstance(node, Condition):
            return self._condition(node)
        if isinstance(node, And):
            return functools.reduce(operator.and_, map(self._expression, node.children))
        if isinstance(node, Or):
            return functools.reduce(operator.or_, map(self._expression, node.children))
        if isinstance(node, Not):
            return ~self._expression(node.child)
        raise TypeError(f"Cannot build a search query for {node!r}")

    def _condition(self, condition):
        if condition.lookup == "isnull":
            is_null = getattr(self.document, NULL_MARKER_FIELD) == condition.field
            return is_null if condition.value else ~is_null
        if condition.field in self.schema.tag_fields:
            field = getattr(self.document, tag_field(condition.field))
            if condition.lookup == "in":
                return field << sorted(map(encode_tag, condition.value))
            return field == encode_tag(condition.value)
        field = getattr(self.document, condition.field)
        intervals = []
        for interval in score_ranges(condition):
            bounds = []
            if interval.low is not None:
                bounds.append(
                    field >= interval.low
                    if interval.low_inclusive
                    else field > interval.low
                )
            if interval.high is not None:
                bounds.append(
                    field <= interval.high
                    if interval.high_inclusive
                    else field < interval.high
                )
            intervals.append(functools.reduce(operator.and_, bounds))
        return functools.reduce(operator.or_, intervals)

    @contextmanager
    def selection(self, pushed, order_field=None):
        yield SearchSelection(self, self.query_string(pushed), order_field)

    def fetch(self, where, ordering, low=0, high=None):
        pushed, complete = self.plan(where)
        if complete and self._orders_by_tag(ordering) and (high is None or high > low):
            pks = self._tag_window(pushed, ordering, low, high)
            if pks is not None:
                return self.get_many(pks)
        return super().fetch(where, ordering, low, high)

    def _tag_window(self, pushed, ordering, low, high):
        """Return the primary keys of a page ordered by a tag field.

        Each value of the field is one search; only the values the window
        overlaps are read, and their primary keys are ordered here.
        """
        (field, descending), (_, pk_descending) = ordering
        values = self.distinct_values(MATCH_ALL, field)
        ordered = sorted(
            (value for value in values if value is not None), reverse=descending
        )
        if None in values:
            ordered = [None, *ordered] if descending else [*ordered, None]
        conditions = [
            Condition(field, None, "isnull", True)
            if value is None
            else Condition(field, None, "exact", value)
            for value in ordered
        ]
        counts = [self.count(conjoin(pushed or MATCH_ALL, c)) for c in conditions]
        if any(count > MAX_GROUP_PKS for count in counts):
            return None
        groups = [(c, n) for c, n in zip(conditions, counts, strict=True) if n]
        return self.group_window(
            groups,
            low,
            high,
            lambda condition, offset, count: self._group_page(
                pushed, condition, offset, count, pk_descending
            ),
        )

    def _group_page(self, pushed, condition, offset, count, descending):
        with self.selection(conjoin(pushed or MATCH_ALL, condition)) as selection:
            pks = sorted(selection.pks(), reverse=descending)
        return pks[offset : offset + count]

    def distinct_values(self, where, field):
        """Read the distinct values of a tag field from the value counters."""
        if where != MATCH_ALL or field not in self.schema.tag_fields:
            return super().distinct_values(where, field)
        return {
            None if member == "n" else member[1:]
            for member in self.client.hkeys(self.keys.counts(field))
        }


class SearchSelection(Selection):
    """Selection answered by ``FT.SEARCH``.

    Searches only ever ask for document ids. RediSearch does not return a
    consistent snapshot for results that need documents to be loaded, and the
    scores are cheaper to read from the records themselves.
    """

    def __init__(self, store, query, order_field):
        self.store = store
        self.order_field = order_field
        if order_field is not None:
            query = _and(query, _numeric(order_field, None, None))
        self.query = query

    def _total(self, query):
        return self.store.ft.search(Query(query).dialect(DIALECT).paging(0, 0)).total

    def _ids(self, query, offset, num, *, descending=None):
        if not num:
            return []
        request = Query(query).dialect(DIALECT).no_content().paging(offset, num)
        if descending is not None:
            request.sort_by(self.order_field, asc=not descending)
        result = self.store.ft.search(request)
        return [self.store.keys.pk_from_record(document.id) for document in result.docs]

    def _members(self, pks):
        """Pair primary keys with their score, read from the records."""
        pipe = self.store.client.pipeline(transaction=False)
        for pk in pks:
            pipe.hget(self.store.keys.record(pk), self.order_field)
        return [
            (pk, int(score))
            for pk, score in zip(pks, pipe.execute(), strict=True)
            if score is not None
        ]

    def pks(self):
        total = self._total(self.query)
        if total > self.store.max_search_results:
            return None
        return self._ids(self.query, 0, total)

    def count(self):
        return self._total(self.query)

    def window(self, low, high, descending):
        num = (high - low) if high is not None else max(self.count() - low, 0)
        return self._members(self._ids(self.query, low, num, descending=descending))

    def between(self, low, high):
        query = _and(self.query, _numeric(self.order_field, low, high))
        return self._members(self._ids(query, 0, self._total(query)))

    def count_between(self, low, high, *, high_inclusive=True):
        query = _and(self.query, _numeric(self.order_field, low, high, high_inclusive))
        return self._total(query)

    def extremes(self):
        first = self._members(self._ids(self.query, 0, 1, descending=False))
        if not first:
            return None
        last = self._members(self._ids(self.query, 0, 1, descending=True))
        return first[0][1], last[0][1]


def _numeric(field, low, high, high_inclusive=True):
    low = "-inf" if low is None else str(low)
    if high is None:
        high = "+inf"
    elif not high_inclusive:
        high = f"({high}"
    return f"@{field}:[{low} {high}]"


def _and(query, clause):
    return clause if query == "*" else f"({query}) {clause}"
