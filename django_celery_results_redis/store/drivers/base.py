"""Storage of one model's records in Redis.

``Store`` implements every operation with plain hashes: queries scan all
records of the model and evaluate lookups in memory. Drivers subclass it and
override the query methods they can answer with Redis structures.
"""

import json
from importlib import resources
from itertools import islice

from django.db import IntegrityError

from .. import codec
from ..keys import KeySpace
from ..query import evaluator
from ..query.ast import MATCH_ALL, primary_key_candidates
from ..schema import Schema
from ..search import SearchIndex

LUA_SOURCE = (
    resources.files("django_celery_results_redis.store")
    .joinpath("lua/store.lua")
    .read_text()
)

STATUS_CREATED = 1
STATUS_UPDATED = 0
STATUS_EXISTS = -1
STATUS_MISSING = -2


def batched(iterable, size):
    iterator = iter(iterable)
    while batch := list(islice(iterator, size)):
        yield batch


class Store:
    supports_ttl = True

    def __init__(
        self,
        model,
        client,
        *,
        prefix,
        scan_count,
        batch_size,
        ttl=None,
        search=None,
    ):
        self.model = model
        self.schema = Schema.for_model(model)
        self.keys = KeySpace(prefix, self.schema.kind)
        self.client = client
        self.scan_count = scan_count
        self.batch_size = batch_size
        self.ttl = ttl
        self.field_names = list(self.schema.fields)
        self.search = (
            SearchIndex(self) if search and self.schema.search_fields else None
        )
        self._script = client.register_script(LUA_SOURCE)

    # Hooks for drivers.

    def index_payload(self):
        """Index description passed to the Lua script, or ``None``."""
        return None

    def prepare(self):
        """Create server-side structures the driver needs (e.g. search indexes)."""
        if self.search is not None:
            self.search.ensure_index()

    def rebuild(self):
        """Recreate the driver's indexes from the stored records."""
        if self.search is not None:
            self.search.ensure_index()
        if not self.maintains_index_fields:
            return
        for batch in self.scan_batches():
            pipe = self.client.pipeline(transaction=False)
            for record in self.get_many(batch):
                pk = record[self.schema.pk]
                indexed = self.index_values(pk, record)
                write = {name: v for name, v in indexed.items() if v is not None}
                clear = [name for name, v in indexed.items() if v is None]
                if write:
                    pipe.hset(self.keys.record(pk), mapping=write)
                if clear:
                    pipe.hdel(self.keys.record(pk), *clear)
            pipe.execute()

    # Encoding.

    def decode(self, mapping):
        return codec.decode_record(self.schema, mapping)

    def encode_values(self, values):
        to_set, to_clear = {}, []
        for name, value in values.items():
            spec = self.schema.fields[name]
            if value is None:
                to_clear.append(name)
            else:
                to_set[name] = codec.encode(spec, value)
        return to_set, to_clear

    def _run(self, payload, client=None):
        index = self.index_payload()
        if index is not None:
            payload["index"] = index
        return self._script(args=[json.dumps(payload)], client=client or self.client)

    @property
    def maintains_index_fields(self):
        """Whether records carry index fields besides their own."""
        return self.search is not None

    def index_values(self, pk, values):
        """Index fields to write for ``values``, including the primary key."""
        if self.search is None:
            return {}
        return self.search.index_values({self.schema.pk: pk, **values})

    def renamed_index_values(self, new_pk):
        """Index fields a rename has to rewrite for the new primary key."""
        return {
            name: value
            for name, value in self.index_values(new_pk, {}).items()
            if value is not None
        }

    # Records.

    def read(self, values):
        """Build a record from the values of ``field_names``, or ``None``."""
        mapping = {
            name: value
            for name, value in zip(self.field_names, values, strict=True)
            if value is not None
        }
        return self.decode(mapping) if mapping else None

    def get(self, pk):
        values = self.client.hmget(self.keys.record(pk), self.field_names)
        return self.read(values)

    def get_many(self, pks):
        """Return the records of ``pks`` that exist, in the order given."""
        records = []
        for batch in batched(pks, self.batch_size):
            pipe = self.client.pipeline(transaction=False)
            for pk in batch:
                pipe.hmget(self.keys.record(pk), self.field_names)
            records.extend(
                record for record in map(self.read, pipe.execute()) if record
            )
        return records

    def write_payload(self, pk, values, *, mode, on_create=None):
        to_set, to_clear = self.encode_values(values)
        if self.maintains_index_fields:
            for name, indexed in self.index_values(pk, values).items():
                if indexed is None:
                    to_clear.append(name)
                else:
                    to_set[name] = indexed
        payload = {
            "op": "write",
            "key": self.keys.record(pk),
            "pk": pk,
            "mode": mode,
            "set": {self.schema.pk: pk, **to_set},
            "clear": to_clear,
            "on_create": self.encode_values(on_create or {})[0],
            "nullable": list(self.schema.nullable_fields),
            "null_marker": codec.NULL_MARKER_FIELD,
            "fields": self.field_names,
        }
        if self.ttl and self.supports_ttl:
            payload["ttl"] = self.ttl
        return payload

    def write(self, pk, values, *, mode="upsert", on_create=None):
        """Store ``values`` (``None`` clears a field) for the record ``pk``.

        ``mode`` is ``"upsert"``, ``"insert"`` (fails if the record exists) or
        ``"update"`` (does nothing if it does not). ``on_create`` values are
        only written when the record is created. Returns ``(created, record)``,
        or ``None`` when an update found no record.
        """
        response = self._run(
            self.write_payload(pk, values, mode=mode, on_create=on_create)
        )
        return self._write_result(pk, response)

    def _write_result(self, pk, response):
        status = response[0]
        if status == STATUS_EXISTS:
            raise IntegrityError(
                f"{self.model._meta.label} with {self.schema.pk} {pk!r} already exists."
            )
        if status == STATUS_MISSING:
            return None
        return status == STATUS_CREATED, self.read(response[1])

    def write_many(self, items):
        """Write ``(pk, values)`` pairs, one round trip per batch.

        Values are stored as given: no automatic timestamps are applied, which
        is what importing existing results needs.
        """
        written = 0
        for batch in batched(items, self.batch_size):
            pipe = self.client.pipeline(transaction=False)
            for pk, values in batch:
                self._run(self.write_payload(pk, values, mode="upsert"), client=pipe)
            written += sum(1 for response in pipe.execute() if response[0] >= 0)
        return written

    def delete_pks(self, pks):
        deleted = 0
        for batch in batched(pks, self.batch_size):
            deleted += self._run(
                {
                    "op": "delete",
                    "keys": [self.keys.record(pk) for pk in batch],
                    "pks": batch,
                    "fields": self.field_names,
                }
            )
        return deleted

    def rename(self, pk, new_pk):
        """Move record ``pk`` to ``new_pk``; returns ``False`` if ``pk`` is missing."""
        status = self._run(
            {
                "op": "rename",
                "key": self.keys.record(pk),
                "new_key": self.keys.record(new_pk),
                "pk": pk,
                "new_pk": new_pk,
                "pk_field": self.schema.pk,
                "fields": self.field_names,
                "set": self.renamed_index_values(new_pk),
            }
        )
        if status == STATUS_EXISTS:
            raise IntegrityError(
                f"{self.model._meta.label} with {self.schema.pk} {new_pk!r} "
                "already exists."
            )
        return status != STATUS_MISSING

    def decrement_counter(self, pk):
        """Decrement a chord counter.

        Returns ``None`` if the counter does not exist, otherwise
        ``(remaining, sub_tasks)``. ``sub_tasks`` is only returned, and the
        counter deleted, once ``remaining`` reaches zero.
        """
        response = self._run({"op": "chord_part_return", "key": self.keys.record(pk)})
        if response is None:
            return None
        remaining, *sub_tasks = response
        return remaining, (sub_tasks[0] if sub_tasks else None)

    def scan_pks(self):
        seen = set()
        for key in self.client.scan_iter(
            match=self.keys.record_pattern, count=self.scan_count, _type="HASH"
        ):
            if key not in seen:
                seen.add(key)
                yield self.keys.pk_from_record(key)

    # Queries. ``where`` is a lookup tree from ``query.compiler``; ``ordering``
    # is a sequence of (field, descending) pairs.

    def iter_records(self, where=MATCH_ALL):
        pks = primary_key_candidates(where, self.schema.pk)
        if pks is None and self.search is not None:
            pks = self.search.candidates(where)
        if pks is None:
            records = self.candidate_records(where)
        else:
            records = self.get_many(sorted(pks))
        return (record for record in records if evaluator.matches(where, record))

    def candidate_records(self, where):
        """Yield a superset of the records matching ``where``."""
        for batch in self.scan_batches():
            yield from self.get_many(batch)

    def scan_batches(self):
        return batched(self.scan_pks(), self.batch_size)

    def fetch(self, where, ordering, low=0, high=None):
        records = evaluator.order_records(list(self.iter_records(where)), ordering)
        return records[low:high]

    def count(self, where):
        return sum(1 for _ in self.iter_records(where))

    def count_many(self, wheres):
        return [self.count(where) for where in wheres]

    def exists(self, where):
        return next(self.iter_records(where), None) is not None

    def aggregate(self, where, specs):
        return evaluator.aggregate(self.iter_records(where), specs)

    def distinct_values(self, where, field):
        return {record[field] for record in self.iter_records(where)}

    def datetimes(self, where, field, kind, tzinfo=None):
        return sorted(
            {
                evaluator.truncate(record[field], kind, tzinfo)
                for record in self.iter_records(where)
                if record[field] is not None
            }
        )

    def matching_pks(self, where):
        return [record[self.schema.pk] for record in self.iter_records(where)]

    def delete(self, where):
        return self.delete_pks(self.matching_pks(where))

    def update(self, where, values):
        updated = 0
        for batch in batched(self.matching_pks(where), self.batch_size):
            pipe = self.client.pipeline(transaction=False)
            for pk in batch:
                self._run(self.write_payload(pk, values, mode="update"), client=pipe)
            updated += sum(1 for response in pipe.execute() if response[0] >= 0)
        return updated
