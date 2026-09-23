"""Conversion between Python values and the text stored in record hashes.

Datetimes are stored as integer microseconds since the Unix epoch in UTC.
``None`` is never stored: a missing hash field means ``None``.
"""

from datetime import datetime, timedelta
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.utils import timezone

from .schema import DATETIME, INTEGER

EPOCH = datetime(1970, 1, 1, tzinfo=datetime_timezone.utc)
NULL_MARKER_FIELD = "null_fields"


def datetime_to_micros(value):
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def micros_to_datetime(micros):
    value = EPOCH + timedelta(microseconds=micros)
    if settings.USE_TZ:
        return value
    return timezone.make_naive(value, timezone.get_default_timezone())


def encode(spec, value):
    """Return the stored text for ``value``; ``value`` must not be ``None``."""
    if spec.kind == DATETIME:
        return str(datetime_to_micros(value))
    if spec.kind == INTEGER:
        return str(int(value))
    return str(value)


def decode(spec, raw):
    if raw is None:
        return None
    if spec.kind == DATETIME:
        return micros_to_datetime(int(raw))
    if spec.kind == INTEGER:
        return int(raw)
    return raw


def decode_record(schema, mapping):
    """Build a record dict with every field of ``schema`` from a stored hash."""
    record = {}
    for name, spec in schema.fields.items():
        raw = mapping.get(name)
        if raw is None and not spec.null:
            record[name] = spec.field.get_default()
        else:
            record[name] = decode(spec, raw)
    return record
