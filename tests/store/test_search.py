import pytest
from django.core.management import call_command
from django.db.models import Q
from django.test import override_settings

from django_celery_results_redis.models import TaskResult
from django_celery_results_redis.store import get_store
from django_celery_results_redis.store.drivers.base import Store
from django_celery_results_redis.store.query.compiler import compile_filter
from django_celery_results_redis.store.search import OVERFLOW_TOKEN, gram_field

from ..conftest import REDIS_URL

MAX_TEXT = 64
LONG_VALUE = "x" * MAX_TEXT + "needle"

TASKS = [
    ("plain", "app.tasks.SendMail", '["user@example.com"]', "{}"),
    ("upper", "APP.TASKS.SENDMAIL", "[]", "{}"),
    ("sharp", "job.straße.run", '["STRASSE"]', "{}"),
    ("dotted", "job.İstanbul.run", '["istanbul"]', "{}"),
    ("comma", "a,b,c", '["x,y"]', "{}"),
    ("pipe", "a|b", '["x|y"]', "{}"),
    ("spaced", "  spaced  value  ", "[]", "{}"),
    ("empty", "", "[]", "{}"),
    ("short", "ab", "[]", "{}"),
    ("unicode", "日本語タスク", '["東京"]', "{}"),
    ("null", None, None, None),
    ("long", "app.tasks.Long", LONG_VALUE, "{}"),
]

QUERIES = [
    {"task_name__icontains": "sendmail"},
    {"task_name__icontains": "SendMail"},
    {"task_name__contains": "SendMail"},
    {"task_name__iexact": "app.tasks.sendmail"},
    {"task_name__exact": "app.tasks.SendMail"},
    {"task_name__startswith": "app."},
    {"task_name__istartswith": "APP."},
    {"task_name__endswith": "Mail"},
    {"task_name__iendswith": "MAIL"},
    {"task_name__icontains": "straße"},
    {"task_name__icontains": "STRASSE"},
    {"task_name__icontains": "ss"},
    {"task_name__icontains": "İstanbul"},
    {"task_name__icontains": "istanbul"},
    {"task_args__icontains": "ISTANBUL"},
    {"task_name__icontains": "a,b"},
    {"task_args__icontains": "x,y"},
    {"task_name__icontains": "a|b"},
    {"task_args__icontains": "x|y"},
    {"task_name__icontains": "  spaced  "},
    {"task_name__exact": ""},
    {"task_name__icontains": ""},
    {"task_name__icontains": "ab"},
    {"task_name__icontains": "日本"},
    {"task_args__icontains": "東京"},
    {"task_args__icontains": "needle"},
    {"task_args__icontains": "xxxxx"},
    {"task_id__icontains": "plain"},
    {"task_id__iexact": "UNICODE"},
    {"status__iexact": "success"},
    {"task_name__isnull": True},
    {"task_name__regex": "^app"},
]


def load():
    for task_id, name, args, kwargs in TASKS:
        TaskResult.objects.create(
            task_id=task_id,
            status="SUCCESS",
            task_name=name,
            task_args=args,
            task_kwargs=kwargs,
        )


def pks(*args, **kwargs):
    matches = TaskResult.objects.filter(*args, **kwargs)
    return sorted(matches.values_list("pk", flat=True))


def where(**kwargs):
    return compile_filter(TaskResult, (), kwargs)


@pytest.fixture
def searching(store_driver, redis_settings, has_search):
    """Re-apply the driver's settings with the search index enabled."""
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings(store_driver, SEARCH="redisearch", SEARCH_MAX_TEXT=MAX_TEXT)
    load()
    return get_store(TaskResult)


@pytest.fixture
def unindexed(store_driver, redis_settings, has_search):
    """A second key prefix holding the same records without a search index."""
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    prefix = redis_settings(store_driver, SEARCH_MAX_TEXT=MAX_TEXT)
    load()
    return prefix


@pytest.mark.parametrize("lookup", QUERIES, ids=lambda lookup: str(lookup))
def test_results_match_the_unindexed_store(store_driver, unindexed, searching, lookup):
    with override_settings(
        DJANGO_CELERY_RESULTS_REDIS={
            "DRIVER": store_driver,
            "URL": REDIS_URL,
            "KEY_PREFIX": unindexed,
            "SEARCH_MAX_TEXT": MAX_TEXT,
        }
    ):
        expected = pks(**lookup)
    assert pks(**lookup) == expected


@pytest.mark.parametrize("lookup", QUERIES, ids=lambda lookup: str(lookup))
def test_excluding_matches_the_rest(searching, lookup):
    everything = pks()
    matching = pks(**lookup)
    assert pks(~Q(**lookup)) == sorted(set(everything) - set(matching))


def test_admin_style_multi_term_search(searching):
    terms = ("app", "user@example")
    query = Q()
    for term in terms:
        query &= Q(task_name__icontains=term) | Q(task_args__icontains=term)
    assert pks(query) == ["plain"]


def test_candidates_narrow_the_query(searching):
    candidates = searching.search.candidates(where(task_name__icontains="sendmail"))
    assert candidates == {"plain", "upper"}


def test_candidates_of_an_unsupported_lookup(searching):
    assert searching.search.candidates(where(task_name__regex="app")) is None
    assert searching.search.candidates(where(task_name__icontains="ab")) is None


def test_query_reads_no_other_record(searching, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the store read every record")

    monkeypatch.setattr(type(searching), "candidate_records", fail, raising=False)
    monkeypatch.setattr(Store, "scan_pks", fail)
    assert pks(task_name__icontains="sendmail") == ["plain", "upper"]
    assert pks(task_id__icontains="plai") == ["plain"]


def test_too_many_candidates_falls_back_to_reading_records(
    store_driver, redis_settings, has_search
):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    redis_settings(store_driver, SEARCH="redisearch", SEARCH_MAX_CANDIDATES=1)
    load()
    store = get_store(TaskResult)
    assert store.search.candidates(where(task_name__icontains="app")) is None
    assert pks(task_name__icontains="app") == ["long", "plain", "upper"]


def test_index_fields_are_not_part_of_the_record(searching, redis_client):
    stored = redis_client.hgetall(searching.keys.record("plain"))
    assert gram_field("task_name") in stored
    assert gram_field("task_id") in stored
    record = searching.get("plain")
    assert set(record) == set(searching.field_names)
    assert record["task_name"] == "app.tasks.SendMail"
    assert TaskResult.objects.get(pk="plain").task_name == "app.tasks.SendMail"


def test_long_values_are_marked_as_overflowing(searching, redis_client):
    assert (
        redis_client.hget(searching.keys.record("long"), gram_field("task_args"))
        == OVERFLOW_TOKEN
    )
    assert pks(task_args__icontains="needle") == ["long"]
    assert pks(task_args__icontains="nothing") == []


def test_partial_update_rewrites_the_index(searching, redis_client):
    TaskResult.objects.filter(pk="plain").update(task_name="app.tasks.Renamed")
    assert pks(task_name__icontains="sendmail") == ["upper"]
    assert pks(task_name__icontains="renamed") == ["plain"]
    assert redis_client.hget(searching.keys.record("plain"), gram_field("task_args"))


def test_clearing_a_field_clears_its_index(searching, redis_client):
    TaskResult.objects.filter(pk="plain").update(task_name=None)
    assert pks(task_name__icontains="sendmail") == ["upper"]
    assert not redis_client.hexists(
        searching.keys.record("plain"), gram_field("task_name")
    )


def test_delete_removes_the_document(searching):
    TaskResult.objects.filter(pk="plain").delete()
    assert searching.search.candidates(where(task_name__icontains="sendmail")) == {
        "upper"
    }
    assert pks(task_name__icontains="sendmail") == ["upper"]


def test_rename_reindexes_the_primary_key(searching):
    task = TaskResult.objects.get(pk="plain")
    task.task_id = "renamed-plain"
    task.save()
    assert pks(task_id__icontains="renamed") == ["renamed-plain"]
    assert pks(task_id__icontains="plain") == ["renamed-plain"]
    assert pks(task_name__icontains="sendmail") == ["renamed-plain", "upper"]


def test_rebuild_indexes_records_written_without_the_index(
    store_driver, redis_settings, has_search, redis_client
):
    if not has_search:
        pytest.skip("the Redis server has no RediSearch")
    prefix = redis_settings(store_driver)
    load()
    searched = {
        "DRIVER": store_driver,
        "URL": REDIS_URL,
        "KEY_PREFIX": prefix,
        "SEARCH": "redisearch",
        "SEARCH_MAX_TEXT": MAX_TEXT,
    }
    with override_settings(DJANGO_CELERY_RESULTS_REDIS=searched):
        assert pks(task_name__icontains="sendmail") == []

        call_command("celery_results_redis_rebuild_index")

        assert pks(task_name__icontains="sendmail") == ["plain", "upper"]
        assert pks(task_id__icontains="uni") == ["unicode"]
        assert pks(task_args__icontains="needle") == ["long"]
        assert (
            redis_client.hget(
                get_store(TaskResult).keys.record("long"), gram_field("task_args")
            )
            == OVERFLOW_TOKEN
        )
