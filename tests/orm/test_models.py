from datetime import datetime, timezone

import pytest
from django.core import serializers
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from django.db.models.functions import Now
from django.db.models.signals import post_save
from django.utils import timezone as django_timezone

from django_celery_results_redis.models import GroupResult, TaskResult
from django_celery_results_redis.store import get_store

UTC = timezone.utc


@pytest.fixture
def saved(store_driver):
    received = []

    def receiver(instance, created, update_fields, **kwargs):
        received.append((instance.pk, created, update_fields))

    post_save.connect(receiver, sender=TaskResult)
    yield received
    post_save.disconnect(receiver, sender=TaskResult)


def test_post_save_created_flag(saved):
    obj = TaskResult(task_id="a", status="PENDING")
    obj.save()
    obj.status = "SUCCESS"
    obj.save()
    assert saved == [("a", True, None), ("a", False, None)]
    assert TaskResult.objects.get(pk="a").status == "SUCCESS"


def test_saving_an_unsaved_instance_with_an_existing_pk_updates(saved):
    TaskResult.objects.create(task_id="a", status="PENDING")
    TaskResult(task_id="a", status="SUCCESS").save()
    assert saved[-1] == ("a", False, None)
    assert TaskResult.objects.get(pk="a").status == "SUCCESS"


def test_date_created_is_set_once_and_date_done_on_every_save(store_driver):
    obj = TaskResult.objects.create(task_id="a")
    created, done = obj.date_created, obj.date_done
    assert created is not None
    stored = TaskResult.objects.get(pk="a")
    assert stored.date_created == created
    assert stored.date_done == done
    stored.save()
    assert stored.date_created == created
    assert stored.date_done > done
    TaskResult(task_id="a").save()
    assert TaskResult.objects.get(pk="a").date_created == created


def test_update_fields(store_driver):
    obj = TaskResult.objects.create(task_id="a", status="A", worker="w")
    done = obj.date_done
    obj.status, obj.worker = "B", "changed"
    obj.save(update_fields=["status"])
    stored = TaskResult.objects.get(pk="a")
    assert (stored.status, stored.worker) == ("B", "w")
    assert stored.date_done == done
    obj.save(update_fields=["worker", "date_done"])
    stored = TaskResult.objects.get(pk="a")
    assert stored.worker == "changed"
    assert stored.date_done > done
    with pytest.raises(DatabaseError, match="update_fields"):
        TaskResult(task_id="missing").save(update_fields=["status"])


def test_force_insert_and_force_update(store_driver):
    TaskResult.objects.create(task_id="a")
    with pytest.raises(IntegrityError):
        TaskResult(task_id="a").save(force_insert=True)
    with pytest.raises(DatabaseError, match="Forced update"):
        TaskResult(task_id="missing").save(force_update=True)
    assert not TaskResult.objects.filter(pk="missing").exists()


def test_primary_key_is_required(store_driver):
    with pytest.raises(ValueError, match="without a task_id"):
        TaskResult(task_id=None).save()


def test_not_null_fields_reject_none(store_driver):
    with pytest.raises(IntegrityError, match="status"):
        TaskResult(task_id="a", status=None).save()
    assert not TaskResult.objects.filter(pk="a").exists()


def test_now_expression_is_resolved(store_driver):
    before = django_timezone.now()
    obj = TaskResult.objects.create(task_id="a", date_started=Now())
    assert obj.date_started >= before
    assert TaskResult.objects.get(pk="a").date_started == obj.date_started


def test_string_values_are_prepared(store_driver):
    TaskResult.objects.create(task_id="a", date_started="2026-01-02T03:04:05Z")
    assert TaskResult.objects.get(pk="a").date_started == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )


def test_raw_save_keeps_the_given_values(store_driver):
    TaskResult.objects.create(task_id="a", status="SUCCESS", worker="w")
    original = TaskResult.objects.get(pk="a")
    data = serializers.serialize("python", [original])
    TaskResult.objects.all().delete()
    for deserialized in serializers.deserialize("python", data):
        deserialized.save()
    restored = TaskResult.objects.get(pk="a")
    assert restored.as_dict() == original.as_dict()
    assert restored.date_created == original.date_created


def test_changing_the_primary_key_renames_the_record(store_driver):
    obj = TaskResult.objects.create(task_id="a", status="SUCCESS")
    obj = TaskResult.objects.get(pk="a")
    obj.task_id = "b"
    obj.save()
    assert not TaskResult.objects.filter(pk="a").exists()
    assert TaskResult.objects.get(pk="b").status == "SUCCESS"
    assert TaskResult.objects.filter(status="SUCCESS").count() == 1
    obj.task_id = "c"
    obj.status = "FAILURE"
    obj.save()
    assert list(TaskResult.objects.values_list("pk", "status")) == [("c", "FAILURE")]


def test_changing_the_primary_key_to_an_existing_one_fails(store_driver):
    TaskResult.objects.create(task_id="a")
    TaskResult.objects.create(task_id="b")
    obj = TaskResult.objects.get(pk="a")
    obj.task_id = "b"
    with pytest.raises(ValidationError) as error:
        obj.validate_unique()
    assert "task_id" in error.value.message_dict
    obj.validate_unique(exclude=["task_id"])
    with pytest.raises(IntegrityError):
        obj.save()
    assert TaskResult.objects.filter(pk="a").exists()


def test_validate_unique_passes_for_unchanged_and_new_objects(store_driver):
    TaskResult.objects.create(task_id="a")
    TaskResult.objects.get(pk="a").validate_unique()
    with pytest.raises(ValidationError):
        TaskResult(task_id="a").validate_unique()
    TaskResult(task_id="new").validate_unique()


def test_delete(store_driver):
    obj = TaskResult.objects.create(task_id="a")
    assert obj.delete() == (1, {"django_celery_results_redis.TaskResult": 1})
    assert obj.delete() == (0, {})
    assert not TaskResult.objects.filter(pk="a").exists()
    with pytest.raises(ValueError, match="can't be deleted"):
        TaskResult(task_id=None).delete()


def test_refresh_from_db(store_driver):
    obj = TaskResult.objects.create(task_id="a", status="A")
    TaskResult.objects.filter(pk="a").update(status="B")
    obj.refresh_from_db()
    assert obj.status == "B"
    obj.delete()
    with pytest.raises(TaskResult.DoesNotExist):
        obj.refresh_from_db()


def test_loaded_instances_remember_their_stored_key(store_driver):
    TaskResult.objects.create(task_id="a")
    loaded = TaskResult.objects.get(pk="a")
    assert loaded._stored_pk == "a"
    assert not loaded._state.adding
    assert TaskResult(task_id="x")._state.adding


def test_as_dict_and_str(store_driver):
    obj = TaskResult.objects.create(task_id="a", status="SUCCESS", result='"1"')
    assert str(obj) == "<Task: a (SUCCESS)>"
    assert obj.as_dict()["result"] == '"1"'
    group = GroupResult.objects.create(group_id="g", result="[]")
    assert str(group) == "<Group: g>"
    assert GroupResult.objects.get(pk="g").as_dict() == {
        "group_id": "g",
        "result": "[]",
        "date_done": group.date_done,
    }


def test_workers_never_touch_the_sql_database(store_driver):
    obj = TaskResult.objects.create(task_id="a")
    obj.save()
    TaskResult.objects.filter(status="PENDING").count()
    obj.delete()
    assert get_store(TaskResult).get("a") is None
