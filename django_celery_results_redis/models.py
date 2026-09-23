"""Models stored in Redis.

The models are unmanaged: Django never creates tables for them. Saving and
deleting go to the configured Redis store, and ``objects`` returns a
``RedisQuerySet``, so the admin, forms and signals work as with database models.
"""

import json

from celery import states
from celery.result import GroupResult as CeleryGroupResult
from celery.result import result_from_tuple
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, models, router
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import managers
from .conf import app_settings
from .store import get_store
from .store.queryset import delete_instances, resolve_value

ALL_STATES = sorted(states.ALL_STATES)
TASK_STATE_CHOICES = sorted(zip(ALL_STATES, ALL_STATES, strict=True))


class RedisModel(models.Model):
    """Base class of models whose records are stored in Redis.

    ``redis_kind`` names the key namespace of the records. The indexing drivers
    index ``redis_indexed_fields``, and the optional search index covers
    ``redis_search_fields``.
    """

    redis_kind = None
    redis_indexed_fields = ()
    redis_search_fields = ()

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db, field_names, values, **kwargs):
        instance = super().from_db(db, field_names, values, **kwargs)
        instance._stored_pk = instance.pk
        return instance

    def _save_table(
        self,
        raw=False,
        cls=None,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        meta = self._meta
        pk = self.pk
        if pk is None:
            raise ValueError(
                f"{meta.object_name} cannot be saved without a {meta.pk.name}."
            )
        store = get_store(cls or type(self))
        stored_pk = getattr(self, "_stored_pk", None)
        if not force_insert and stored_pk is not None and stored_pk != pk:
            store.rename(stored_pk, pk)

        fields = [f for f in meta.local_concrete_fields if not f.primary_key]
        if update_fields:
            fields = [
                f
                for f in fields
                if f.name in update_fields or f.attname in update_fields
            ]
        values, on_create, refreshed = {}, {}, []
        for field in fields:
            value = getattr(self, field.attname)
            if not raw and getattr(field, "auto_now_add", False) and value is None:
                on_create[field.name] = timezone.now()
                refreshed.append(field)
                continue
            if not raw:
                value = field.pre_save(self, add=False)
            if hasattr(value, "resolve_expression"):
                refreshed.append(field)
            value = resolve_value(field, value)
            if value is None and not field.null:
                raise IntegrityError(
                    f"NOT NULL constraint failed: {meta.label}.{field.name}"
                )
            values[field.name] = value

        if force_insert:
            mode = "insert"
        elif force_update or update_fields:
            mode = "update"
        else:
            mode = "upsert"
        result = store.write(pk, values, mode=mode, on_create=on_create)
        if result is None:
            if force_update:
                raise DatabaseError("Forced update did not affect any rows.")
            raise DatabaseError("Save with update_fields did not affect any rows.")
        created, record = result
        for field in refreshed:
            setattr(self, field.attname, record[field.name])
        self._stored_pk = pk
        return not created

    def delete(self, using=None, keep_parents=False):
        if self.pk is None:
            raise ValueError(
                f"{self._meta.object_name} object can't be deleted because its "
                f"{self._meta.pk.attname} attribute is set to None."
            )
        using = using or router.db_for_write(type(self), instance=self)
        deleted = delete_instances(type(self), [self], using)
        return deleted, ({self._meta.label: deleted} if deleted else {})

    delete.alters_data = True

    def validate_unique(self, exclude=None):
        super().validate_unique(exclude)
        stored_pk = getattr(self, "_stored_pk", None)
        pk_field = self._meta.pk
        if (
            stored_pk is not None
            and self.pk != stored_pk
            and pk_field.name not in (exclude or ())
            and type(self)._default_manager.filter(pk=self.pk).exists()
        ):
            raise ValidationError(
                {pk_field.name: self.unique_error_message(type(self), (pk_field.name,))}
            )


class TaskResult(RedisModel):
    """Task result/status."""

    task_id = models.CharField(
        primary_key=True,
        max_length=app_settings.task_id_max_length,
        verbose_name=_("Task ID"),
        help_text=_("Celery ID for the Task that was run"),
    )
    periodic_task_name = models.CharField(
        null=True,
        max_length=255,
        verbose_name=_("Periodic Task Name"),
        help_text=_("Name of the Periodic Task which was run"),
    )
    task_name = models.CharField(
        null=True,
        max_length=app_settings.task_id_max_length,
        verbose_name=_("Task Name"),
        help_text=_("Name of the Task which was run"),
    )
    task_args = models.TextField(
        null=True,
        verbose_name=_("Task Positional Arguments"),
        help_text=_(
            "JSON representation of the positional arguments used with the task"
        ),
    )
    task_kwargs = models.TextField(
        null=True,
        verbose_name=_("Task Named Arguments"),
        help_text=_("JSON representation of the named arguments used with the task"),
    )
    status = models.CharField(
        max_length=50,
        default=states.PENDING,
        verbose_name=_("Task State"),
        help_text=_("Current state of the task being run"),
    )
    worker = models.CharField(
        max_length=100,
        default=None,
        null=True,
        verbose_name=_("Worker"),
        help_text=_("Worker that executes the task"),
    )
    content_type = models.CharField(
        max_length=128,
        verbose_name=_("Result Content Type"),
        help_text=_("Content type of the result data"),
    )
    content_encoding = models.CharField(
        max_length=64,
        verbose_name=_("Result Encoding"),
        help_text=_("The encoding used to save the task result data"),
    )
    result = models.TextField(
        null=True,
        default=None,
        editable=False,
        verbose_name=_("Result Data"),
        help_text=_(
            "The data returned by the task.  "
            "Use content_encoding and content_type fields to read."
        ),
    )
    date_created = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created DateTime"),
        help_text=_("Datetime field when the task result was created in UTC"),
    )
    date_started = models.DateTimeField(
        null=True,
        default=None,
        verbose_name=_("Started DateTime"),
        help_text=_("Datetime field when the task was started in UTC"),
    )
    date_done = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Completed DateTime"),
        help_text=_("Datetime field when the task was completed in UTC"),
    )
    traceback = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Traceback"),
        help_text=_("Text of the traceback if the task generated one"),
    )
    meta = models.TextField(
        null=True,
        default=None,
        editable=False,
        verbose_name=_("Task Meta Information"),
        help_text=_(
            "JSON meta information about the task, such as information on child tasks"
        ),
    )

    objects = managers.TaskResultManager()

    redis_kind = "task"
    redis_search_fields = (
        "task_id",
        "task_name",
        "status",
        "task_args",
        "task_kwargs",
    )
    redis_indexed_fields = (
        "status",
        "task_name",
        "periodic_task_name",
        "worker",
        "date_created",
        "date_started",
        "date_done",
    )

    class Meta:
        managed = False
        base_manager_name = "objects"
        ordering = ["-date_done"]
        verbose_name = _("task result")
        verbose_name_plural = _("task results")

    def __str__(self):
        return f"<Task: {self.task_id} ({self.status})>"

    def as_dict(self):
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_args": self.task_args,
            "task_kwargs": self.task_kwargs,
            "status": self.status,
            "result": self.result,
            "date_done": self.date_done,
            "traceback": self.traceback,
            "meta": self.meta,
            "worker": self.worker,
        }


class ChordCounter(RedisModel):
    """Chord synchronisation."""

    group_id = models.CharField(
        primary_key=True,
        max_length=app_settings.task_id_max_length,
        verbose_name=_("Group ID"),
        help_text=_("Celery ID for the Chord header group"),
    )
    sub_tasks = models.TextField(
        help_text=_(
            "JSON serialized list of task result tuples. use .group_result() to decode"
        )
    )
    count = models.PositiveIntegerField(
        help_text=_(
            "Starts at len(chord header) and decrements after each task is finished"
        )
    )

    objects = managers.ChordCounterManager()

    redis_kind = "chord"

    class Meta:
        managed = False
        base_manager_name = "objects"

    def group_result(self, app=None):
        """Return the :class:`celery.result.GroupResult` of self.

        Arguments:
            app (celery.app.base.Celery): app instance to create the
                :class:`celery.result.GroupResult` with.

        """
        return CeleryGroupResult(
            self.group_id,
            [result_from_tuple(r, app=app) for r in json.loads(self.sub_tasks)],
            app=app,
        )


class GroupResult(RedisModel):
    """Task Group result/status."""

    group_id = models.CharField(
        primary_key=True,
        max_length=app_settings.task_id_max_length,
        verbose_name=_("Group ID"),
        help_text=_("Celery ID for the Group that was run"),
    )
    date_created = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created DateTime"),
        help_text=_("Datetime field when the group result was created in UTC"),
    )
    date_done = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Completed DateTime"),
        help_text=_("Datetime field when the group was completed in UTC"),
    )
    content_type = models.CharField(
        max_length=128,
        verbose_name=_("Result Content Type"),
        help_text=_("Content type of the result data"),
    )
    content_encoding = models.CharField(
        max_length=64,
        verbose_name=_("Result Encoding"),
        help_text=_("The encoding used to save the task result data"),
    )
    result = models.TextField(
        null=True,
        default=None,
        editable=False,
        verbose_name=_("Result Data"),
        help_text=_(
            "The data returned by the task.  "
            "Use content_encoding and content_type fields to read."
        ),
    )

    objects = managers.GroupResultManager()

    redis_kind = "group"
    redis_search_fields = ("group_id",)
    redis_indexed_fields = ("date_created", "date_done")

    class Meta:
        managed = False
        base_manager_name = "objects"
        ordering = ["-date_done"]
        verbose_name = _("group result")
        verbose_name_plural = _("group results")

    def __str__(self):
        return f"<Group: {self.group_id}>"

    def as_dict(self):
        return {
            "group_id": self.group_id,
            "result": self.result,
            "date_done": self.date_done,
        }
