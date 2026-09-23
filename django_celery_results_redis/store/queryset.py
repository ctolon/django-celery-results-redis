"""A ``QuerySet`` whose rows live in Redis.

``RedisQuerySet`` subclasses Django's ``QuerySet`` so that code checking
``isinstance(obj, QuerySet)`` (the admin does) keeps working. Its ``query`` is a
``RedisQuery`` holding a lookup tree instead of SQL. Methods that only use the
public query API (``filter``, ``exclude``, ``order_by``, ``get``, slicing, ...)
are inherited; methods that would build SQL are overridden or raise
``NotSupportedError``.
"""

import copy
from collections import namedtuple

from django.core.exceptions import FieldError
from django.db import IntegrityError, NotSupportedError, models
from django.db.models import signals
from django.db.models.constants import LOOKUP_SEP
from django.db.models.expressions import F, OrderBy, Star
from django.db.models.functions import Now
from django.db.models.query import BaseIterable
from django.db.models.utils import resolve_callables
from django.utils import timezone

from . import get_store
from .query import evaluator
from .query.ast import MATCH_ALL, MATCH_NONE, conjoin
from .query.compiler import compile_q, resolve_field

AGGREGATES = frozenset({"Count", "Min", "Max", "Sum", "Avg"})
TRUNC_KINDS = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")
DATE_KINDS = ("year", "month", "week", "day")

UNSUPPORTED_METHODS = (
    "alias",
    "annotate",
    "bulk_create",
    "bulk_update",
    "difference",
    "explain",
    "extra",
    "intersection",
    "prefetch_related",
    "raw",
    "resolve_expression",
    "select_for_update",
    "union",
)


def parse_ordering(model, items):
    """Return ``items`` (``order_by()`` arguments) as (field name, descending) pairs."""
    ordering = []
    for item in items:
        if isinstance(item, str):
            if item == "?":
                raise NotSupportedError("Random ordering is not supported.")
            descending = item.startswith("-")
            name = item.removeprefix("-")
        elif isinstance(item, OrderBy) and isinstance(item.expression, F):
            if item.nulls_first or item.nulls_last:
                raise NotSupportedError("nulls_first/nulls_last are not supported.")
            descending = item.descending
            name = item.expression.name
        elif isinstance(item, F):
            descending = False
            name = item.name
        else:
            raise NotSupportedError(f"Ordering by {item!r} is not supported.")
        if LOOKUP_SEP in name:
            raise FieldError(f"Cannot resolve keyword '{name}' into field.")
        ordering.append((resolve_field(model, name).name, descending))
    return tuple(ordering)


def resolve_value(field, value):
    """Prepare a value assigned to ``field`` for storage."""
    if isinstance(value, Now):
        return timezone.now()
    if hasattr(value, "resolve_expression"):
        raise NotSupportedError(f"Expressions are not supported: {value!r}")
    return field.get_prep_value(value)


class RedisQuery:
    """The query state of a ``RedisQuerySet``.

    Exposes the attributes of ``django.db.models.sql.Query`` that Django's
    ``QuerySet`` and admin read, with a lookup tree as ``where``.
    """

    combinator = None
    extra_order_by = ()
    group_by = None
    select_for_update = False
    values_select = ()
    annotations = {}
    filter_is_sticky = False

    def __init__(self, model):
        self.model = model
        self.where = MATCH_ALL
        self.empty = False
        self.order_by = ()
        self.default_ordering = True
        self.standard_ordering = True
        self.select_related = False
        self.distinct = False
        self.distinct_fields = ()
        self.low_mark = 0
        self.high_mark = None

    def chain(self, klass=None):
        return copy.copy(self)

    clone = chain

    def get_meta(self):
        return self.model._meta

    def add_q(self, q_object):
        self.where = conjoin(self.where, compile_q(self.model, q_object))
        if self.where == MATCH_NONE:
            self.set_empty()

    def set_empty(self):
        self.empty = True

    def is_empty(self):
        return self.empty

    def add_ordering(self, *ordering):
        parse_ordering(self.model, ordering)
        if ordering:
            self.order_by += ordering
        else:
            self.default_ordering = False

    def clear_ordering(self, force=False, clear_default=True):
        if not force and (self.is_sliced or self.distinct_fields):
            return
        self.order_by = ()
        if clear_default:
            self.default_ordering = False

    def add_distinct_fields(self, *field_names):
        if field_names:
            raise NotSupportedError("DISTINCT ON fields is not supported.")
        self.distinct = True

    def set_limits(self, low=None, high=None):
        if high is not None:
            if self.high_mark is not None:
                self.high_mark = min(self.high_mark, self.low_mark + high)
            else:
                self.high_mark = self.low_mark + high
        if low is not None:
            if self.high_mark is not None:
                self.low_mark = min(self.high_mark, self.low_mark + low)
            else:
                self.low_mark += low
        if self.low_mark == self.high_mark:
            self.set_empty()

    def clear_limits(self):
        self.low_mark, self.high_mark = 0, None

    @property
    def is_sliced(self):
        return self.low_mark != 0 or self.high_mark is not None

    def can_filter(self):
        return not self.is_sliced

    def get_ordering(self):
        """The effective ordering, made total by a final primary key key."""
        items = self.order_by
        if not items and self.default_ordering:
            items = self.model._meta.ordering
        ordering = parse_ordering(self.model, items)
        if not self.standard_ordering:
            ordering = tuple((name, not desc) for name, desc in ordering)
        pk = self.model._meta.pk.name
        if pk not in (name for name, _ in ordering):
            ordering += ((pk, ordering[-1][1] if ordering else False),)
        return ordering

    def __str__(self):
        return (
            f"RedisQuery(model={self.model._meta.label}, where={self.where!r}, "
            f"ordering={self.get_ordering()!r}, "
            f"limits=({self.low_mark}, {self.high_mark}))"
        )


class RedisIterable(BaseIterable):
    def __iter__(self):
        return self.queryset._iterate()


class RedisQuerySet(models.QuerySet):
    def __init__(self, model=None, query=None, using=None, hints=None):
        super().__init__(
            model=model, query=query or RedisQuery(model), using=using, hints=hints
        )
        self._iterable_class = RedisIterable
        self._values_kind = None
        self._loaded_fields = None

    @property
    def store(self):
        return get_store(self.model)

    def _clone(self):
        clone = super()._clone()
        clone._values_kind = self._values_kind
        clone._loaded_fields = self._loaded_fields
        return clone

    # Reading.

    def _records(self):
        query = self.query
        if query.is_empty():
            return []
        if query.distinct and self._fields is not None:
            return self.store.fetch(query.where, query.get_ordering())
        return self.store.fetch(
            query.where, query.get_ordering(), query.low_mark, query.high_mark
        )

    def _iterate(self):
        if self._fields is None:
            return self._instances(self._records())
        if self._is_distinct_single_value():
            rows = self._distinct_single_value()
        else:
            rows = self._project(self._records())
            if self.query.distinct:
                rows = list(dict.fromkeys(rows))
        if self.query.distinct:
            rows = rows[self.query.low_mark : self.query.high_mark]
        return iter(rows)

    def _instances(self, records):
        fields = self.model._meta.concrete_fields
        names = self._loaded_fields
        db = self.db
        for record in records:
            if names is None:
                values = [record[f.name] for f in fields]
                yield self.model.from_db(db, [f.attname for f in fields], values)
            else:
                loaded = [f for f in fields if f.name in names]
                yield self.model.from_db(
                    db, [f.attname for f in loaded], [record[f.name] for f in loaded]
                )

    def _project(self, records):
        names = [self._field_name(name) for name in self._fields]
        if self._values_kind == "dict":
            return [
                {
                    out: record[name]
                    for out, name in zip(self._fields, names, strict=True)
                }
                for record in records
            ]
        if self._values_kind == "flat":
            return [record[names[0]] for record in records]
        rows = [tuple(record[name] for name in names) for record in records]
        if self._values_kind == "named":
            row = namedtuple("Row", self._fields)
            return [row(*values) for values in rows]
        return rows

    def _field_name(self, name):
        return resolve_field(self.model, name).name

    def _is_distinct_single_value(self):
        if not (self.query.distinct and self._values_kind == "flat"):
            return False
        field = self._field_name(self._fields[0])
        return all(name == field for name, _ in self.query.get_ordering()[:1])

    def _distinct_single_value(self):
        field = self._field_name(self._fields[0])
        values = list(self.store.distinct_values(self.query.where, field))
        (_, descending), *_ = self.query.get_ordering()
        return sorted(values, key=evaluator.sort_key, reverse=descending)

    def iterator(self, chunk_size=None):
        return self._iterate()

    def count(self):
        if self._result_cache is not None:
            return len(self._result_cache)
        query = self.query
        if query.is_empty():
            return 0
        if query.distinct and self._fields is not None:
            return len(list(self._iterate()))
        total = self.store.count(query.where)
        high = total if query.high_mark is None else min(total, query.high_mark)
        return max(0, high - query.low_mark)

    def exists(self):
        if self._result_cache is not None:
            return bool(self._result_cache)
        if self.query.is_empty():
            return False
        if self.query.is_sliced:
            return self.count() > 0
        return self.store.exists(self.query.where)

    def in_bulk(self, id_list=None, *, field_name="pk"):
        if self.query.is_sliced:
            raise TypeError("Cannot use 'limit' or 'offset' with in_bulk().")
        field = resolve_field(self.model, field_name)
        if not field.unique:
            raise ValueError(
                f"in_bulk()'s field_name must be a unique field but {field_name!r} "
                "isn't."
            )
        queryset = self
        if id_list is not None:
            if not id_list:
                return {}
            queryset = self.filter(**{f"{field_name}__in": id_list})
        return {getattr(obj, field_name): obj for obj in queryset.order_by()}

    def aggregate(self, *args, **kwargs):
        for arg in args:
            try:
                kwargs[arg.default_alias] = arg
            except (AttributeError, TypeError):
                raise TypeError("Complex aggregates require an alias") from None
        specs = {alias: self._aggregate_spec(expr) for alias, expr in kwargs.items()}
        if self.query.is_empty():
            return evaluator.aggregate([], specs)
        if self.query.is_sliced:
            return evaluator.aggregate(self._records(), specs)
        return self.store.aggregate(self.query.where, specs)

    def _aggregate_spec(self, expression):
        if not isinstance(expression, models.Aggregate) or (
            expression.name not in AGGREGATES
        ):
            raise NotSupportedError(f"Aggregate {expression!r} is not supported.")
        source = expression.get_source_expressions()[0]
        if isinstance(source, Star):
            field = None
        elif isinstance(source, F):
            field = resolve_field(self.model, source.name).name
        else:
            raise NotSupportedError(f"Aggregating {source!r} is not supported.")
        condition = getattr(expression.filter, "condition", expression.filter)
        return evaluator.AggregateSpec(
            function=expression.name,
            field=field,
            distinct=expression.distinct,
            where=None if condition is None else compile_q(self.model, condition),
            default=expression.default,
        )

    def datetimes(self, field_name, kind, order="ASC", tzinfo=None):
        if kind not in TRUNC_KINDS:
            raise ValueError(f"'kind' must be one of {', '.join(TRUNC_KINDS)}.")
        return self._truncated(field_name, kind, order, tzinfo)

    def dates(self, field_name, kind, order="ASC"):
        if kind not in DATE_KINDS:
            raise ValueError(f"'kind' must be one of {', '.join(DATE_KINDS)}.")
        values = self._truncated(field_name, kind, order)
        return list(dict.fromkeys(value.date() for value in values))

    def _truncated(self, field_name, kind, order, tzinfo=None):
        if order not in ("ASC", "DESC"):
            raise ValueError("'order' must be either 'ASC' or 'DESC'.")
        field = resolve_field(self.model, field_name)
        if not isinstance(field, models.DateTimeField):
            raise FieldError(f"'{field_name}' is not a DateTimeField.")
        if self.query.is_empty():
            values = []
        elif self.query.is_sliced:
            values = sorted(
                {
                    evaluator.truncate(record[field.name], kind, tzinfo)
                    for record in self._records()
                    if record[field.name] is not None
                }
            )
        else:
            values = self.store.datetimes(self.query.where, field.name, kind, tzinfo)
        return values[::-1] if order == "DESC" else values

    # Shape.

    def values(self, *fields, **expressions):
        if expressions:
            raise NotSupportedError("values() expressions are not supported.")
        return self._values("dict", fields)

    def values_list(self, *fields, flat=False, named=False):
        if flat and named:
            raise TypeError("'flat' and 'named' can't be used together.")
        if flat and len(fields) > 1:
            raise TypeError(
                "'flat' is not valid when values_list is called with more than one "
                "field."
            )
        kind = "flat" if flat else "named" if named else "tuple"
        return self._values(kind, fields)

    def _values(self, kind, fields):
        fields = fields or tuple(f.attname for f in self.model._meta.concrete_fields)
        for name in fields:
            self._field_name(name)
        clone = self._chain()
        clone._fields = tuple(fields)
        clone._values_kind = kind
        return clone

    def only(self, *fields):
        if self._fields is not None:
            raise TypeError("Cannot call only() after .values() or .values_list()")
        if fields == (None,):
            raise TypeError("Cannot pass None as an argument to only().")
        names = {self._field_name(name) for name in fields}
        names.add(self.model._meta.pk.name)
        clone = self._chain()
        clone._loaded_fields = frozenset(names)
        return clone

    def defer(self, *fields):
        if self._fields is not None:
            raise TypeError("Cannot call defer() after .values() or .values_list()")
        clone = self._chain()
        if fields == (None,):
            clone._loaded_fields = None
            return clone
        deferred = {self._field_name(name) for name in fields}
        loaded = self._loaded_fields or {
            f.name for f in self.model._meta.concrete_fields
        }
        clone._loaded_fields = frozenset(
            name
            for name in loaded
            if name not in deferred or name == self.model._meta.pk.name
        )
        return clone

    def select_related(self, *fields):
        if self._fields is not None:
            raise TypeError(
                "Cannot call select_related() after .values() or .values_list()"
            )
        if fields and fields != (None,):
            raise FieldError(
                "Invalid field name(s) given in select_related: "
                f"{', '.join(map(repr, fields))}. Choices are: (none)"
            )
        return self._chain()

    # Writing.

    def get_or_create(self, defaults=None, **kwargs):
        self._for_write = True
        try:
            return self.get(**kwargs), False
        except self.model.DoesNotExist:
            params = self._extract_model_params(defaults, **kwargs)
            params = dict(resolve_callables(params))
            try:
                return self.create(**params), True
            except IntegrityError:
                try:
                    return self.get(**kwargs), False
                except self.model.DoesNotExist:
                    pass
                raise

    get_or_create.alters_data = True

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        update_defaults = defaults or {}
        if create_defaults is None:
            create_defaults = update_defaults
        self._for_write = True
        obj, created = self.get_or_create(create_defaults, **kwargs)
        if created:
            return obj, created
        for name, value in resolve_callables(update_defaults):
            setattr(obj, name, value)
        update_fields = set(update_defaults)
        if update_fields:
            for field in self.model._meta.local_concrete_fields:
                if not (
                    field.primary_key
                    or field.__class__.pre_save is models.Field.pre_save
                ):
                    update_fields.add(field.name)
            obj.save(using=self.db, update_fields=update_fields)
        else:
            obj.save(using=self.db)
        return obj, False

    update_or_create.alters_data = True

    def update(self, **kwargs):
        if self.query.is_sliced:
            raise TypeError("Cannot update a query once a slice has been taken.")
        values = {}
        for name, value in kwargs.items():
            field = resolve_field(self.model, name)
            if field.primary_key:
                raise NotSupportedError("Updating the primary key is not supported.")
            value = resolve_value(field, value)
            if value is None and not field.null:
                raise IntegrityError(
                    f"NOT NULL constraint failed: {self.model._meta.label}.{field.name}"
                )
            values[field.name] = value
        self._for_write = True
        if self.query.is_empty():
            return 0
        updated = self.store.update(self.query.where, values)
        self._result_cache = None
        return updated

    update.alters_data = True

    def delete(self):
        if self.query.is_sliced:
            raise TypeError("Cannot use 'limit' or 'offset' with delete().")
        if self.query.distinct_fields:
            raise TypeError("Cannot call delete() after .distinct(*fields).")
        if self._fields is not None:
            raise TypeError("Cannot call delete() after .values() or .values_list()")
        if self.query.is_empty():
            return 0, {}
        model = self.model
        if signals.pre_delete.has_listeners(model) or signals.post_delete.has_listeners(
            model
        ):
            deleted = delete_instances(model, list(self._chain()), self.db)
        else:
            deleted = self.store.delete(self.query.where)
        self._result_cache = None
        return deleted, ({model._meta.label: deleted} if deleted else {})

    delete.alters_data = True
    delete.queryset_only = True

    def _raw_delete(self, using):
        return self.store.delete(self.query.where)

    _raw_delete.alters_data = True


def delete_instances(model, instances, using):
    """Delete ``instances`` of ``model`` sending ``pre_delete``/``post_delete``."""
    for instance in instances:
        signals.pre_delete.send(
            sender=model, instance=instance, using=using, origin=instance
        )
    deleted = get_store(model).delete_pks([instance.pk for instance in instances])
    for instance in instances:
        signals.post_delete.send(
            sender=model, instance=instance, using=using, origin=instance
        )
    return deleted


def _unsupported(name):
    def method(self, *args, **kwargs):
        raise NotSupportedError(
            f"{type(self).__name__}.{name}() is not supported by the Redis result "
            "store."
        )

    method.__name__ = name
    return method


for _name in UNSUPPORTED_METHODS:
    setattr(RedisQuerySet, _name, _unsupported(_name))


class RedisManager(models.manager.BaseManager.from_queryset(RedisQuerySet)):
    pass
