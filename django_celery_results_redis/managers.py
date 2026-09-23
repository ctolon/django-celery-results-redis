"""Model managers."""

from functools import wraps
from itertools import count

from celery.utils.time import maybe_timedelta
from django.db import IntegrityError
from django.db.models import signals
from django.db.models.functions import Now
from django.utils import timezone
from redis.exceptions import ConnectionError, TimeoutError

from .store import get_store
from .store.queryset import RedisManager, resolve_value
from .utils import now

RETRYABLE_ERRORS = (ConnectionError, TimeoutError)


def transaction_retry(max_retries=1):
    """Retry the decorated function on Redis connection errors.

    Keyword Arguments:
        max_retries (int): Maximum number of retries. Default one retry.
            Callers can override it with ``exception_retry_count``.
    """

    def _outer(fun):
        @wraps(fun)
        def _inner(*args, **kwargs):
            _max_retries = kwargs.pop("exception_retry_count", max_retries)
            for retries in count(0):
                try:
                    return fun(*args, **kwargs)
                except RETRYABLE_ERRORS:
                    if retries >= _max_retries:
                        raise

        return _inner

    return _outer


class ResultManager(RedisManager):
    """Generic manager for celery results."""

    def store(self, pk, fields):
        """Create or update one record and return the instance.

        Saving an instance costs a read and a write. When nothing listens for
        the model's save signals, the record is written in a single round trip
        instead, which is the path Celery workers take for every state change.
        """
        model = self.model
        if signals.pre_save.has_listeners(model) or signals.post_save.has_listeners(
            model
        ):
            instance, created = self.get_or_create(pk=pk, defaults=fields)
            if not created:
                for name, value in fields.items():
                    setattr(instance, name, value)
                instance.save()
            return instance
        moment = timezone.now()
        values, on_create = {}, {}
        for name, value in fields.items():
            field = model._meta.get_field(name)
            value = moment if isinstance(value, Now) else resolve_value(field, value)
            if value is None and not field.null:
                raise IntegrityError(
                    f"NOT NULL constraint failed: {model._meta.label}.{name}"
                )
            values[name] = value
        for field in model._meta.local_concrete_fields:
            if getattr(field, "auto_now", False):
                values[field.name] = moment
            elif getattr(field, "auto_now_add", False):
                on_create[field.name] = moment
        _, record = get_store(model).write(pk, values, on_create=on_create)
        return model.from_db(self.db, list(record), list(record.values()))

    def get_all_expired(self, expires):
        """Get all expired results."""
        return self.filter(date_done__lt=now() - maybe_timedelta(expires))

    def delete_expired(self, expires):
        """Delete all expired results."""
        self.get_all_expired(expires).delete()


class TaskResultManager(ResultManager):
    """Manager for :class:`~.models.TaskResult` models."""

    def get_task(self, task_id):
        """Get result for task by ``task_id``, or an unsaved instance."""
        try:
            return self.get(task_id=task_id)
        except self.model.DoesNotExist:
            return self.model(task_id=task_id)

    @transaction_retry(max_retries=2)
    def store_result(
        self,
        content_type,
        content_encoding,
        task_id,
        result,
        status,
        traceback=None,
        meta=None,
        periodic_task_name=None,
        task_name=None,
        task_args=None,
        task_kwargs=None,
        worker=None,
        using=None,
        **kwargs,
    ):
        """Store the result and status of a task.

        Arguments:
            content_type (str): Mime-type of result and meta content.
            content_encoding (str): Type of encoding (e.g. binary/utf-8).
            task_id (str): Id of task.
            periodic_task_name (str): Celery Periodic task name.
            task_name (str): Celery task name.
            task_args (str): Task arguments.
            task_kwargs (str): Task kwargs.
            result (str): The serialized return value of the task,
                or an exception instance raised by the task.
            status (str): Task status.  See :mod:`celery.states` for a list of
                possible status values.
            worker (str): Worker that executes the task.
            using (str): Accepted for compatibility; ignored.
            traceback (str): The traceback string taken at the point of
                exception (only passed if the task failed).
            meta (str): Serialized result meta data (this contains e.g.
                children).

        Keyword Arguments:
            date_started (datetime): When the task started.
            exception_retry_count (int): How many times to retry on Redis
                connection errors.  The default is to retry twice.
        """
        fields = {
            "status": status,
            "result": result,
            "traceback": traceback,
            "meta": meta,
            "content_encoding": content_encoding,
            "content_type": content_type,
            "periodic_task_name": periodic_task_name,
            "task_name": task_name,
            "task_args": task_args,
            "task_kwargs": task_kwargs,
            "worker": worker,
        }
        if "date_started" in kwargs:
            fields["date_started"] = kwargs["date_started"]

        return self.store(task_id, fields)


class GroupResultManager(ResultManager):
    """Manager for :class:`~.models.GroupResult` models."""

    def get_group(self, group_id):
        """Get result for group by ``group_id``, or an unsaved instance."""
        try:
            return self.get(group_id=group_id)
        except self.model.DoesNotExist:
            return self.model(group_id=group_id)

    @transaction_retry(max_retries=2)
    def store_group_result(
        self, content_type, content_encoding, group_id, result, using=None
    ):
        fields = {
            "result": result,
            "content_encoding": content_encoding,
            "content_type": content_type,
        }
        return self.store(group_id, fields)


class ChordCounterManager(RedisManager):
    """Manager for :class:`~.models.ChordCounter` models."""

    def decrement(self, group_id):
        """Atomically decrement the counter of ``group_id``.

        Returns ``None`` if there is no counter. Otherwise returns an unsaved
        instance holding the remaining count; once the count reaches zero the
        counter is deleted and the instance carries its ``sub_tasks``.
        """
        result = get_store(self.model).decrement_counter(group_id)
        if result is None:
            return None
        remaining, sub_tasks = result
        return self.model(group_id=group_id, count=remaining, sub_tasks=sub_tasks)
