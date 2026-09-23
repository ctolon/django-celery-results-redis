from datetime import datetime, timedelta

import pytest
from django.db import IntegrityError
from django.db.models import Count, Max, Min, Q
from django.test import override_settings
from django.utils import timezone as django_timezone

from django_celery_results_redis.models import ChordCounter, GroupResult, TaskResult
from django_celery_results_redis.store import get_store
from django_celery_results_redis.store.codec import (
    NULL_MARKER_FIELD,
    datetime_to_micros,
)
from django_celery_results_redis.store.query.ast import MATCH_ALL, Condition
from django_celery_results_redis.store.query.compiler import compile_filter

from .conftest import moment


def where(**kwargs):
    return compile_filter(TaskResult, (), kwargs)


def put(store, pk, **values):
    values.setdefault("date_done", moment())
    values.setdefault("date_created", moment())
    return store.write(pk, values)


def test_upsert_creates_then_updates(task_store):
    created, record = put(task_store, "a", status="PENDING")
    assert created is True
    assert record["status"] == "PENDING"
    created, record = put(task_store, "a", status="SUCCESS")
    assert created is False
    assert record["status"] == "SUCCESS"
    assert task_store.get("a")["status"] == "SUCCESS"


def test_insert_fails_if_the_record_exists(task_store):
    task_store.write("a", {"status": "A"}, mode="insert")
    with pytest.raises(IntegrityError):
        task_store.write("a", {"status": "B"}, mode="insert")
    assert task_store.get("a")["status"] == "A"


def test_update_of_a_missing_record_writes_nothing(task_store, redis_client):
    assert task_store.write("a", {"status": "A"}, mode="update") is None
    assert task_store.get("a") is None
    assert not redis_client.exists(task_store.keys.record("a"))


def test_on_create_values_are_only_written_on_create(task_store):
    first = moment(1)
    _, record = task_store.write("a", {}, on_create={"date_created": first})
    assert record["date_created"] == first
    _, record = task_store.write("a", {}, on_create={"date_created": moment(2)})
    assert record["date_created"] == first


def test_none_clears_and_empty_string_is_kept(task_store, redis_client):
    put(task_store, "a", worker="w", task_name="")
    put(task_store, "a", worker=None)
    stored = redis_client.hgetall(task_store.keys.record("a"))
    assert "worker" not in stored
    assert stored["task_name"] == ""
    record = task_store.get("a")
    assert record["worker"] is None
    assert record["task_name"] == ""
    assert task_store.count(where(task_name="")) == 1
    assert task_store.count(where(task_name__isnull=True)) == 0
    assert task_store.count(where(worker__isnull=True)) == 1


def test_null_marker_lists_null_fields(task_store, redis_client):
    put(task_store, "a", worker="w", task_name="t", periodic_task_name="p")
    marker = redis_client.hget(task_store.keys.record("a"), NULL_MARKER_FIELD)
    assert set(marker.split(",")) == {
        "task_args",
        "task_kwargs",
        "result",
        "date_started",
        "traceback",
        "meta",
    }
    put(task_store, "a", worker=None)
    marker = redis_client.hget(task_store.keys.record("a"), NULL_MARKER_FIELD)
    assert "worker" in marker.split(",")


def test_primary_key_is_stored_in_the_hash(task_store, redis_client):
    put(task_store, "a:b c")
    assert redis_client.hget(task_store.keys.record("a:b c"), "task_id") == "a:b c"
    assert task_store.get("a:b c")["task_id"] == "a:b c"


def test_rename(task_store, redis_client):
    put(task_store, "a", status="A", worker="w")
    put(task_store, "b", status="B")
    assert task_store.rename("a", "c") is True
    assert task_store.get("a") is None
    assert task_store.get("c")["task_id"] == "c"
    assert task_store.matching_pks(where(status="A")) == ["c"]
    assert task_store.matching_pks(where(worker="w")) == ["c"]
    with pytest.raises(IntegrityError):
        task_store.rename("c", "b")
    assert task_store.rename("missing", "d") is False
    assert sorted(task_store.matching_pks(MATCH_ALL)) == ["b", "c"]


def test_delete_pks_counts_existing_records(task_store):
    for pk in "abc":
        put(task_store, pk, status="A")
    assert task_store.delete_pks(["a", "b", "missing"]) == 2
    assert task_store.matching_pks(MATCH_ALL) == ["c"]
    assert task_store.count(where(status="A")) == 1


def test_get_many_keeps_order_and_skips_missing(task_store):
    for pk in "abc":
        put(task_store, pk)
    records = task_store.get_many(["c", "missing", "a"])
    assert [r["task_id"] for r in records] == ["c", "a"]


def test_scan_pks_is_deduplicated(task_store):
    for index in range(50):
        put(task_store, f"t{index}")
    pks = list(task_store.scan_pks())
    assert sorted(pks) == sorted(f"t{i}" for i in range(50))


def test_decrement_counter(store_driver):
    ChordCounter.objects.create(group_id="g", sub_tasks="[1]", count=2)
    counters = get_store(ChordCounter)
    assert counters.decrement_counter("g") == (1, None)
    assert counters.decrement_counter("g") == (0, "[1]")
    assert counters.decrement_counter("g") is None
    assert not ChordCounter.objects.filter(pk="g").exists()


def test_group_results_use_their_own_namespace(store_driver):
    TaskResult.objects.create(task_id="x")
    GroupResult.objects.create(group_id="x")
    assert TaskResult.objects.count() == 1
    assert GroupResult.objects.count() == 1
    TaskResult.objects.all().delete()
    assert GroupResult.objects.filter(pk="x").exists()


@pytest.fixture
def ties(task_store):
    for index, pk in enumerate(["a", "B", "c", "d", "e", "f", "g"]):
        minutes = 0 if index < 2 else 1 if index < 5 else 2
        put(task_store, pk, date_done=moment(minutes))
    return task_store


@pytest.mark.parametrize("date_descending", [True, False])
@pytest.mark.parametrize("pk_descending", [True, False])
@pytest.mark.parametrize(("low", "high"), [(0, None), (0, 3), (1, 4), (2, 6), (5, 7)])
def test_score_ordered_pages_break_ties_by_primary_key(
    ties, date_descending, pk_descending, low, high
):
    ordering = (("date_done", date_descending), ("task_id", pk_descending))
    everything = ties.fetch(MATCH_ALL, (("task_id", False),))
    expected = sorted(everything, key=lambda r: r["task_id"], reverse=pk_descending)
    expected.sort(key=lambda r: r["date_done"], reverse=date_descending)
    expected = [r["task_id"] for r in expected][low:high]
    page = ties.fetch(MATCH_ALL, ordering, low, high)
    assert [r["task_id"] for r in page] == expected
    filtered = ties.fetch(where(date_done__gte=moment()), ordering, low, high)
    assert [r["task_id"] for r in filtered] == expected


def test_fast_paths_agree_with_the_evaluator(task_store):
    for index in range(30):
        put(
            task_store,
            f"t{index:02}",
            status="SUCCESS" if index % 3 else "FAILURE",
            worker=None if index % 4 == 0 else f"w{index % 2}",
            date_done=moment(minutes=index * 60 * 17),
            date_started=None if index % 5 == 0 else moment(index),
        )
    queryset = TaskResult.objects.all()
    records = task_store.fetch(MATCH_ALL, (("task_id", False),))
    assert queryset.count() == 30
    assert queryset.filter(status="SUCCESS").count() == 20
    assert queryset.exclude(worker="w1").count() == sum(
        r["worker"] != "w1" for r in records
    )
    assert queryset.filter(worker__isnull=True).count() == 8
    assert queryset.aggregate(
        first=Min("date_done"),
        last=Max("date_done"),
        started=Count("date_started"),
        failures=Count("pk", filter=Q(status="FAILURE")),
    ) == {
        "first": moment(),
        "last": moment(29 * 60 * 17),
        "started": 24,
        "failures": 10,
    }
    expected_days = sorted(
        {r["date_done"].replace(hour=0, minute=0, second=0) for r in records}
    )
    assert queryset.datetimes("date_done", "day") == expected_days
    assert queryset.datetimes("date_done", "month") == sorted(
        {d.replace(day=1) for d in expected_days}
    )
    assert queryset.datetimes("date_done", "year") == [
        moment().replace(month=1, day=1, hour=0)
    ]
    assert task_store.distinct_values(MATCH_ALL, "worker") == {None, "w0", "w1"}
    assert task_store.distinct_values(where(status="FAILURE"), "status") == {"FAILURE"}
    assert queryset.filter(date_done__year=2026).count() == 30
    assert queryset.filter(date_done__date=moment().date()).count() == 1


def test_result_ttl_sets_an_expiry(redis_settings, redis_client, store_driver):
    if store_driver == "indexed":
        pytest.skip("the indexed driver does not support RESULT_TTL")
    redis_settings(store_driver, RESULT_TTL=timedelta(minutes=5))
    store = get_store(TaskResult)
    put(store, "a")
    assert 0 < redis_client.ttl(store.keys.record("a")) <= 300


@pytest.mark.parametrize("value", ["a|b", " lead", "trail ", "", "tab\there"])
def test_values_redis_search_cannot_match_are_still_found(task_store, value):
    put(task_store, "hit", worker=value, task_name=value)
    put(task_store, "miss", worker="other", task_name="other")
    for lookup in ({"worker": value}, {"worker__in": [value, "zzz"]}):
        assert task_store.matching_pks(where(**lookup)) == ["hit"]
        assert task_store.count(where(**lookup)) == 1
    assert task_store.matching_pks(where(date_done__gte=moment(), worker=value)) == [
        "hit"
    ]


@pytest.fixture
def indexed_store(redis_settings):
    redis_settings("indexed")
    return get_store(TaskResult)


def members(client, key):
    return set(client.zrange(key, 0, -1))


def index_state(store, client):
    keys = store.keys
    state = {"all": members(client, keys.all_members)}
    for field in store.schema.tag_fields:
        state[f"v:{field}"] = client.smembers(keys.values(field))
        state[f"n:{field}"] = members(client, keys.nulls(field))
        for value in state[f"v:{field}"]:
            state[f"s:{field}:{value}"] = members(client, keys.members(field, value))
    for field in store.schema.date_fields:
        state[f"n:{field}"] = members(client, keys.nulls(field))
        state[f"z:{field}"] = dict(
            client.zrange(keys.scores(field), 0, -1, withscores=True)
        )
    return state


def test_indexed_keys_follow_writes(indexed_store, redis_client):
    keys = indexed_store.keys
    put(indexed_store, "a", status="PENDING", worker="w1")
    put(indexed_store, "b", status="PENDING")
    assert members(redis_client, keys.all_members) == {"a", "b"}
    assert members(redis_client, keys.members("status", "PENDING")) == {"a", "b"}
    assert redis_client.smembers(keys.values("worker")) == {"w1"}
    assert members(redis_client, keys.nulls("worker")) == {"b"}
    assert members(redis_client, keys.nulls("date_started")) == {"a", "b"}
    assert redis_client.zscore(keys.scores("date_done"), "a") == datetime_to_micros(
        moment()
    )

    put(indexed_store, "a", status="SUCCESS", worker=None, date_started=moment(1))
    assert members(redis_client, keys.members("status", "PENDING")) == {"b"}
    assert redis_client.smembers(keys.values("status")) == {"PENDING", "SUCCESS"}
    assert redis_client.smembers(keys.values("worker")) == set()
    assert not redis_client.exists(keys.members("worker", "w1"))
    assert members(redis_client, keys.nulls("worker")) == {"a", "b"}
    assert members(redis_client, keys.nulls("date_started")) == {"b"}
    assert redis_client.zscore(keys.scores("date_started"), "a") is not None

    indexed_store.delete_pks(["a", "b"])
    assert not redis_client.keys(f"{keys.index_prefix}*")


def test_indexed_repairs_entries_of_vanished_records(indexed_store, redis_client):
    put(indexed_store, "a", status="A", worker="w")
    put(indexed_store, "b", status="A")
    redis_client.delete(indexed_store.keys.record("a"))
    assert indexed_store.count(where(status="A")) == 2
    assert [
        r["task_id"]
        for r in indexed_store.fetch(
            where(status="A"), (("date_done", True), ("task_id", True))
        )
    ] == ["b"]
    assert indexed_store.count(where(status="A")) == 1
    assert indexed_store.distinct_values(MATCH_ALL, "worker") == {None}
    assert "a" not in members(redis_client, indexed_store.keys.all_members)


def test_indexed_rebuild_recreates_every_index(indexed_store, redis_client):
    put(indexed_store, "a", status="A", worker="w", date_started=moment(1))
    put(indexed_store, "b", status="B")
    before = index_state(indexed_store, redis_client)
    for key in redis_client.scan_iter(match=f"{indexed_store.keys.index_prefix}*"):
        redis_client.delete(key)
    redis_client.sadd(indexed_store.keys.members("status", "stale"), "zzz")
    indexed_store.rebuild()
    assert index_state(indexed_store, redis_client) == before


TAG_ROWS = [
    ("a", "B", "w1"),
    ("b", "A", None),
    ("c", "B", "w2"),
    ("d", "A", "w1"),
    ("e", "C", None),
    ("f", "B", None),
    ("g", "A", "w2"),
    ("h", "C", "w1"),
    ("i", "B", "w1"),
]


@pytest.fixture
def tag_stores(redis_settings):
    """The same rows in an indexed and in a raw store."""
    stores = []
    for driver in ("indexed", "raw"):
        redis_settings(driver)
        store = get_store(TaskResult)
        for index, (pk, status, worker) in enumerate(TAG_ROWS):
            put(store, pk, status=status, worker=worker, date_done=moment(index))
        stores.append(store)
    return stores


@pytest.fixture
def temporary_keys(monkeypatch):
    """Record the temporary keys a store builds while querying."""

    def track(store):
        created = []
        original = store.keys.temporary

        def temporary():
            key = original()
            created.append(key)
            return key

        monkeypatch.setattr(store.keys, "temporary", temporary)
        return created

    return track


@pytest.mark.parametrize("field", ["status", "worker"])
@pytest.mark.parametrize("descending", [True, False])
@pytest.mark.parametrize("pk_descending", [True, False])
@pytest.mark.parametrize(
    ("low", "high"), [(0, None), (0, 2), (2, 5), (3, 4), (4, 9), (8, 9), (9, None)]
)
def test_tag_ordered_pages(tag_stores, field, descending, pk_descending, low, high):
    indexed, raw = tag_stores
    ordering = ((field, descending), ("task_id", pk_descending))
    expected = [r["task_id"] for r in raw.fetch(MATCH_ALL, ordering, low, high)]
    assert [r["task_id"] for r in indexed.fetch(MATCH_ALL, ordering, low, high)] == (
        expected
    )


@pytest.mark.parametrize(
    "lookup",
    [
        {"status": "B"},
        {"worker__isnull": False},
        {"worker": "w1"},
        {"status__in": ["A", "C"]},
    ],
)
@pytest.mark.parametrize(("low", "high"), [(0, None), (1, 3)])
def test_tag_ordered_pages_with_a_filter(tag_stores, lookup, low, high):
    indexed, raw = tag_stores
    ordering = (("worker", False), ("task_id", True))
    expected = [r["task_id"] for r in raw.fetch(where(**lookup), ordering, low, high)]
    assert [
        r["task_id"] for r in indexed.fetch(where(**lookup), ordering, low, high)
    ] == expected


def test_tag_ordering_places_nulls_last_ascending(tag_stores):
    indexed, _ = tag_stores
    ordering = (("worker", False), ("task_id", False))
    pks = [r["task_id"] for r in indexed.fetch(MATCH_ALL, ordering)]
    assert pks == ["a", "d", "h", "i", "c", "g", "b", "e", "f"]
    descending = (("worker", True), ("task_id", False))
    pks = [r["task_id"] for r in indexed.fetch(MATCH_ALL, descending)]
    assert pks == ["b", "e", "f", "c", "g", "a", "d", "h", "i"]


def test_tag_ordered_pages_do_not_read_every_record(tag_stores, monkeypatch):
    indexed, raw = tag_stores

    def fail(where):
        raise AssertionError("the records were scanned")

    monkeypatch.setattr(indexed, "candidate_records", fail)
    ordering = (("status", False), ("task_id", True))
    assert [r["task_id"] for r in indexed.fetch(MATCH_ALL, ordering, 0, 3)] == [
        r["task_id"] for r in raw.fetch(MATCH_ALL, ordering, 0, 3)
    ]


@pytest.mark.parametrize(
    ("lookup", "counted_by_keys", "materializes"),
    [
        ({"status": "B"}, True, False),
        ({"status": "B", "worker": "w1"}, True, False),
        ({"worker__isnull": True}, True, False),
        ({"status": "B", "worker__isnull": True}, True, False),
        ({}, True, False),
        ({"date_done__gte": moment(3)}, False, False),
        ({"status__in": ["A", "B"]}, False, True),
        ({"task_id": "a"}, False, True),
    ],
)
def test_counts_use_the_index_keys(
    tag_stores, temporary_keys, lookup, counted_by_keys, materializes
):
    indexed, raw = tag_stores
    condition = where(**lookup) if lookup else MATCH_ALL
    pushed, _ = indexed.plan(condition)
    assert (indexed._member_keys(pushed) is not None) is counted_by_keys
    created = temporary_keys(indexed)
    assert indexed.count(condition) == raw.count(condition)
    assert bool(created) is materializes


def test_negated_and_disjunct_counts_match_the_raw_driver(tag_stores):
    indexed, raw = tag_stores
    for condition in (
        compile_filter(TaskResult, (~Q(status="B"),), {}),
        compile_filter(TaskResult, (Q(status="A") | Q(worker="w1"),), {}),
        where(task_args__icontains="x"),
    ):
        assert indexed.count(condition) == raw.count(condition)


DATE_LOOKUPS = [
    {"date_done__gte": moment(3)},
    {"date_done__gt": moment(3)},
    {"date_done__lt": moment(5)},
    {"date_done__lte": moment(5)},
    {"date_done__gte": moment(2), "date_done__lt": moment(6)},
    {"date_done__gt": moment(2), "date_done__lte": moment(6)},
    {"date_done__range": (moment(1), moment(4))},
    {"date_done__year": 2026},
    {"date_done__date": moment().date()},
]


@pytest.mark.parametrize("lookup", DATE_LOOKUPS, ids=str)
def test_date_ranges_read_the_date_index(tag_stores, temporary_keys, lookup):
    indexed, raw = tag_stores
    condition = where(**lookup)
    ordering = (("date_done", True), ("task_id", True))
    created = temporary_keys(indexed)
    page = [r["task_id"] for r in indexed.fetch(condition, ordering, 0, 4)]
    assert page == [r["task_id"] for r in raw.fetch(condition, ordering, 0, 4)]
    assert indexed.count(condition) == raw.count(condition)
    assert sorted(indexed.matching_pks(condition)) == sorted(
        raw.matching_pks(condition)
    )
    assert indexed.datetimes(condition, "date_done", "day") == raw.datetimes(
        condition, "date_done", "day"
    )
    assert created == []


def test_date_ranges_of_another_field_still_materialize(tag_stores, temporary_keys):
    indexed, raw = tag_stores
    condition = where(date_created__gte=moment(3))
    ordering = (("date_done", True), ("task_id", True))
    created = temporary_keys(indexed)
    assert [r["task_id"] for r in indexed.fetch(condition, ordering)] == [
        r["task_id"] for r in raw.fetch(condition, ordering)
    ]
    assert created


def test_date_ranges_without_time_zone_support(redis_settings):
    stores = []
    for driver in ("indexed", "raw"):
        redis_settings(driver)
        stores.append(get_store(TaskResult))
    indexed, raw = stores
    with override_settings(USE_TZ=False, TIME_ZONE="America/New_York"):
        naive = datetime(2026, 1, 1, 0, 30)
        for index in range(6):
            for store in stores:
                put(
                    store,
                    f"t{index}",
                    date_done=naive + timedelta(hours=index * 7),
                    date_created=naive,
                )
        for lookup in (
            {"date_done__gte": naive + timedelta(hours=7)},
            {"date_done__lt": naive + timedelta(hours=21)},
            {"date_done__date": naive.date()},
            {"date_done__year": 2026},
        ):
            condition = where(**lookup)
            assert indexed.count(condition) == raw.count(condition)
            assert sorted(indexed.matching_pks(condition)) == sorted(
                raw.matching_pks(condition)
            )
            assert indexed.datetimes(condition, "date_done", "day") == raw.datetimes(
                condition, "date_done", "day"
            )


@pytest.mark.parametrize("time_zone", ["UTC", "Asia/Tokyo", "America/New_York"])
def test_date_ranges_in_another_time_zone(tag_stores, time_zone):
    indexed, raw = tag_stores
    with django_timezone.override(time_zone):
        for lookup in DATE_LOOKUPS:
            condition = where(**lookup)
            assert indexed.count(condition) == raw.count(condition)
            assert sorted(indexed.matching_pks(condition)) == sorted(
                raw.matching_pks(condition)
            )


def test_redis_om_recreates_an_outdated_index(redis_settings, has_search, redis_client):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings("redis_om")
    store = get_store(TaskResult)
    put(store, "a", status="A")
    digest_key = f"{store.keys.search_index}:schema"
    redis_client.set(digest_key, "outdated")
    store.ensure_index()
    assert redis_client.get(digest_key) != "outdated"
    store.rebuild()
    assert store.count(where(status="A")) == 1


def test_redis_om_pushes_down_supported_conditions(redis_settings, has_search):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings("redis_om")
    store = get_store(TaskResult)
    assert store.can_push(Condition("status", None, "exact", "A"))
    assert store.can_push(Condition("worker", None, "isnull", True))
    assert store.can_push(Condition("status", None, "exact", "a|b , "))
    assert store.can_push(Condition("status", None, "exact", ""))
    assert not store.can_push(Condition("status", None, "icontains", "a"))
    assert not store.can_push(Condition("task_id", None, "exact", "a"))


@pytest.mark.parametrize(
    "value", ["A", "a|b", " padded ", "", "comma,separated", "ünïcödé", "TAB\there"]
)
def test_redis_om_matches_any_tag_value(redis_settings, has_search, value):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings("redis_om")
    store = get_store(TaskResult)
    put(store, "a", status=value)
    put(store, "b", status="other")
    assert store.count(where(status=value)) == 1
    assert [r["task_id"] for r in store.iter_records(where(status=value))] == ["a"]
    assert store.distinct_values(MATCH_ALL, "status") == {value, "other"}


def test_naive_datetimes_without_time_zone_support(store_driver):
    with override_settings(USE_TZ=False, TIME_ZONE="Europe/Istanbul"):
        for index, day in enumerate([30, 31, 31]):
            TaskResult.objects.create(task_id=f"t{index}")
            TaskResult.objects.filter(pk=f"t{index}").update(
                date_done=datetime(2025, 12, day, 23, 30)
            )
        TaskResult.objects.filter(pk="t2").update(date_done=datetime(2026, 1, 1, 0, 30))
        queryset = TaskResult.objects.all()
        assert queryset.get(pk="t0").date_done == datetime(2025, 12, 30, 23, 30)
        assert (
            queryset.filter(date_done__date=datetime(2025, 12, 31).date()).count() == 1
        )
        assert queryset.filter(date_done__year=2025).count() == 2
        assert queryset.filter(date_done__gte=datetime(2025, 12, 31)).count() == 2
        assert queryset.datetimes("date_done", "year") == [
            datetime(2025, 1, 1),
            datetime(2026, 1, 1),
        ]
        assert queryset.datetimes("date_done", "day") == [
            datetime(2025, 12, 30),
            datetime(2025, 12, 31),
            datetime(2026, 1, 1),
        ]
        assert queryset.aggregate(Max("date_done")) == {
            "date_done__max": datetime(2026, 1, 1, 0, 30)
        }
        assert [obj.pk for obj in queryset[:2]] == ["t2", "t1"]
