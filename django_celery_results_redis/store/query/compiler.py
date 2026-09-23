"""Translation of ``Q`` objects and keyword lookups into lookup trees.

Values are prepared here, when ``filter()`` is called, so invalid values raise
the same ``ValidationError``/``ValueError`` as Django's SQL compiler does and
the admin can turn them into its ``?e=1`` redirect.
"""

import re

from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db import NotSupportedError, models
from django.db.models.constants import LOOKUP_SEP

from .ast import MATCH_ALL, MATCH_NONE, Condition, Xor, make_and, make_not, make_or

TEXT_LOOKUPS = frozenset(
    {
        "iexact",
        "contains",
        "icontains",
        "startswith",
        "istartswith",
        "endswith",
        "iendswith",
        "regex",
        "iregex",
    }
)
COMPARISON_LOOKUPS = frozenset({"exact", "gt", "gte", "lt", "lte", "in", "range"})
LOOKUPS = TEXT_LOOKUPS | COMPARISON_LOOKUPS | {"isnull"}
DATE_PART_TRANSFORMS = frozenset(
    {"year", "iso_year", "quarter", "month", "week", "week_day", "iso_week_day", "day"}
)
TIME_PART_TRANSFORMS = frozenset({"hour", "minute", "second"})
DATETIME_TRANSFORMS = DATE_PART_TRANSFORMS | TIME_PART_TRANSFORMS | {"date", "time"}
CHAINED_TRANSFORMS = {"date": DATE_PART_TRANSFORMS, "time": TIME_PART_TRANSFORMS}

CONNECTORS = {
    models.Q.AND: make_and,
    models.Q.OR: make_or,
    models.Q.XOR: lambda children: Xor(tuple(children)),
}

_date_field = models.DateField()
_time_field = models.TimeField()
_integer_field = models.IntegerField()


def compile_filter(model, args, kwargs):
    return compile_q(model, models.Q(*args, **kwargs))


def compile_q(model, q):
    if not q.children:
        return MATCH_ALL
    children = []
    for child in q.children:
        if isinstance(child, models.Q):
            children.append(compile_q(model, child))
        elif isinstance(child, tuple):
            children.append(compile_lookup(model, *child))
        else:
            raise NotSupportedError(
                f"Filtering on {child!r} is not supported by the Redis result store."
            )
    node = CONNECTORS[q.connector](children)
    return make_not(node) if q.negated else node


def resolve_field(model, name):
    if name == "pk":
        return model._meta.pk
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        choices = ", ".join(sorted(f.name for f in model._meta.concrete_fields))
        raise FieldError(
            f"Cannot resolve keyword '{name}' into field. Choices are: {choices}"
        ) from None
    if not field.concrete:
        raise FieldError(f"Field '{name}' cannot be queried.")
    return field


def compile_lookup(model, key, value):
    name, *parts = key.split(LOOKUP_SEP)
    field = resolve_field(model, name)
    transform = None
    if isinstance(field, models.DateTimeField) and parts:
        transform = _pop_transform(parts)
    lookup = parts.pop(0) if parts else "exact"
    if parts or lookup not in LOOKUPS:
        raise FieldError(
            f"Unsupported lookup '{LOOKUP_SEP.join([lookup, *parts])}' for "
            f"{type(field).__name__} or join on the field not permitted."
        )
    output_field = _output_field(field, transform)
    if lookup in TEXT_LOOKUPS and not isinstance(
        output_field, (models.CharField, models.TextField)
    ):
        raise FieldError(
            f"Unsupported lookup '{lookup}' for {type(output_field).__name__}."
        )
    return _condition(field, transform, lookup, output_field, value)


def _pop_transform(parts):
    """Consume datetime transforms from ``parts`` and return the effective one.

    ``date__<part>`` and ``time__<part>`` extract the same value as ``<part>``
    on the datetime, so chains collapse into a single transform.
    """
    if parts[0] not in DATETIME_TRANSFORMS:
        return None
    transform = parts.pop(0)
    if parts and parts[0] in CHAINED_TRANSFORMS.get(transform, ()):
        transform = parts.pop(0)
    return transform


def _output_field(field, transform):
    if transform == "date":
        return _date_field
    if transform == "time":
        return _time_field
    if transform is not None:
        return _integer_field
    return field


def _condition(field, transform, lookup, output_field, value):
    if hasattr(value, "resolve_expression") and not isinstance(value, models.QuerySet):
        raise NotSupportedError("Expressions are not supported as lookup values.")
    if lookup == "isnull":
        if not isinstance(value, bool):
            raise ValueError(
                "The QuerySet value for an isnull lookup must be True or False."
            )
        return Condition(field.name, transform, lookup, value)
    if value is None:
        if lookup in ("exact", "iexact"):
            return Condition(field.name, transform, "isnull", True)
        raise ValueError("Cannot use None as a query value")
    if lookup == "in":
        values = _iterable_values(value)
        prepared = {output_field.get_prep_value(v) for v in values if v is not None}
        if not prepared:
            return MATCH_NONE
        return Condition(field.name, transform, lookup, frozenset(prepared))
    if lookup in ("regex", "iregex"):
        pattern = output_field.get_prep_value(value)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from None
        return Condition(field.name, transform, lookup, pattern)
    if lookup == "range":
        low, high = value
        return Condition(
            field.name,
            transform,
            lookup,
            (output_field.get_prep_value(low), output_field.get_prep_value(high)),
        )
    return Condition(field.name, transform, lookup, output_field.get_prep_value(value))


def _iterable_values(value):
    if isinstance(value, models.QuerySet):
        fields = getattr(value, "_fields", None) or ("pk",)
        return list(value.values_list(fields[0], flat=True))
    if isinstance(value, (str, bytes)):
        raise TypeError("The 'in' lookup requires an iterable of values.")
    return list(value)
