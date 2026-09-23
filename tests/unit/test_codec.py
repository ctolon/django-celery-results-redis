from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from django.test import override_settings

from django_celery_results_redis.models import ChordCounter, TaskResult
from django_celery_results_redis.store import codec
from django_celery_results_redis.store.schema import Schema

UTC = timezone.utc


@pytest.mark.parametrize(
    "value",
    [
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(2026, 9, 22, 15, 30, 1, 123456, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 3, 29, 3, 30, tzinfo=ZoneInfo("Europe/Berlin")),
    ],
)
def test_aware_datetimes_round_trip(value):
    micros = codec.datetime_to_micros(value)
    assert isinstance(micros, int)
    assert codec.micros_to_datetime(micros) == value


def test_epoch_is_zero():
    assert codec.datetime_to_micros(datetime(1970, 1, 1, tzinfo=UTC)) == 0


@override_settings(USE_TZ=False, TIME_ZONE="Europe/Istanbul")
def test_naive_datetimes_use_the_default_time_zone():
    value = datetime(2026, 9, 22, 12, 0, 0, 1)
    micros = codec.datetime_to_micros(value)
    assert micros == codec.datetime_to_micros(
        datetime(2026, 9, 22, 9, 0, 0, 1, tzinfo=UTC)
    )
    decoded = codec.micros_to_datetime(micros)
    assert decoded == value
    assert decoded.tzinfo is None


@override_settings(USE_TZ=False, TIME_ZONE="Europe/Berlin")
@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 3, 29, 1, 59),
        datetime(2026, 3, 29, 3, 0),
        datetime(2026, 10, 25, 4),
    ],
)
def test_naive_round_trip_across_dst(value):
    assert codec.micros_to_datetime(codec.datetime_to_micros(value)) == value


def test_encode_and_decode_by_kind():
    schema = Schema.for_model(ChordCounter)
    count = schema.fields["count"]
    assert codec.encode(count, 3) == "3"
    assert codec.decode(count, "3") == 3
    sub_tasks = schema.fields["sub_tasks"]
    assert codec.encode(sub_tasks, "[]") == "[]"
    assert codec.decode(sub_tasks, None) is None


def test_decode_record_fills_missing_fields():
    schema = Schema.for_model(TaskResult)
    record = codec.decode_record(schema, {"task_id": "a", "worker": "", "junk": "x"})
    assert record["task_id"] == "a"
    assert record["worker"] == ""
    assert record["task_name"] is None
    assert record["status"] == "PENDING"
    assert record["content_type"] == ""
    assert record["date_started"] is None
    assert "junk" not in record
    assert set(record) == set(schema.fields)


def test_schema_of_task_result():
    schema = Schema.for_model(TaskResult)
    assert schema.kind == "task"
    assert schema.pk == "task_id"
    assert schema.tag_fields == ("status", "task_name", "periodic_task_name", "worker")
    assert schema.date_fields == ("date_created", "date_started", "date_done")
    assert "date_started" in schema.nullable_fields
    assert "status" not in schema.nullable_fields
