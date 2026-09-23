from django_celery_results_redis.store.keys import KeySpace


def test_record_keys():
    keys = KeySpace("p", "task")
    assert keys.record("a:b") == "p:task:a:b"
    assert keys.pk_from_record("p:task:a:b") == "a:b"
    assert keys.record_pattern == "p:task:*"


def test_index_keys():
    keys = KeySpace("p", "task")
    assert keys.all_members == "p:idx:task:all"
    assert keys.members("status", "OK") == "p:idx:task:s:status:OK"
    assert keys.values("status") == "p:idx:task:v:status"
    assert keys.nulls("worker") == "p:idx:task:n:worker"
    assert keys.scores("date_done") == "p:idx:task:z:date_done"
    assert keys.search_index == "p:ft:task"


def test_temporary_keys_are_unique_and_prefixed():
    keys = KeySpace("p", "task")
    first, second = keys.temporary(), keys.temporary()
    assert first != second
    assert first.startswith("p:tmp:")
    assert not first.startswith(keys.record_pattern[:-1])
