from django.db import migrations, models

from django_celery_results_redis.conf import app_settings


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ChordCounter",
            fields=[
                (
                    "group_id",
                    models.CharField(
                        help_text="Celery ID for the Chord header group",
                        max_length=app_settings.task_id_max_length,
                        primary_key=True,
                        serialize=False,
                        verbose_name="Group ID",
                    ),
                ),
                (
                    "sub_tasks",
                    models.TextField(
                        help_text="JSON serialized list of task result tuples. use .group_result() to decode"
                    ),
                ),
                (
                    "count",
                    models.PositiveIntegerField(
                        help_text="Starts at len(chord header) and decrements after each task is finished"
                    ),
                ),
            ],
            options={
                "managed": False,
                "base_manager_name": "objects",
            },
        ),
        migrations.CreateModel(
            name="GroupResult",
            fields=[
                (
                    "group_id",
                    models.CharField(
                        help_text="Celery ID for the Group that was run",
                        max_length=app_settings.task_id_max_length,
                        primary_key=True,
                        serialize=False,
                        verbose_name="Group ID",
                    ),
                ),
                (
                    "date_created",
                    models.DateTimeField(
                        auto_now_add=True,
                        help_text="Datetime field when the group result was created in UTC",
                        verbose_name="Created DateTime",
                    ),
                ),
                (
                    "date_done",
                    models.DateTimeField(
                        auto_now=True,
                        help_text="Datetime field when the group was completed in UTC",
                        verbose_name="Completed DateTime",
                    ),
                ),
                (
                    "content_type",
                    models.CharField(
                        help_text="Content type of the result data",
                        max_length=128,
                        verbose_name="Result Content Type",
                    ),
                ),
                (
                    "content_encoding",
                    models.CharField(
                        help_text="The encoding used to save the task result data",
                        max_length=64,
                        verbose_name="Result Encoding",
                    ),
                ),
                (
                    "result",
                    models.TextField(
                        default=None,
                        editable=False,
                        help_text="The data returned by the task.  Use content_encoding and content_type fields to read.",
                        null=True,
                        verbose_name="Result Data",
                    ),
                ),
            ],
            options={
                "verbose_name": "group result",
                "verbose_name_plural": "group results",
                "ordering": ["-date_done"],
                "managed": False,
                "base_manager_name": "objects",
            },
        ),
        migrations.CreateModel(
            name="TaskResult",
            fields=[
                (
                    "task_id",
                    models.CharField(
                        help_text="Celery ID for the Task that was run",
                        max_length=app_settings.task_id_max_length,
                        primary_key=True,
                        serialize=False,
                        verbose_name="Task ID",
                    ),
                ),
                (
                    "periodic_task_name",
                    models.CharField(
                        help_text="Name of the Periodic Task which was run",
                        max_length=255,
                        null=True,
                        verbose_name="Periodic Task Name",
                    ),
                ),
                (
                    "task_name",
                    models.CharField(
                        help_text="Name of the Task which was run",
                        max_length=app_settings.task_id_max_length,
                        null=True,
                        verbose_name="Task Name",
                    ),
                ),
                (
                    "task_args",
                    models.TextField(
                        help_text="JSON representation of the positional arguments used with the task",
                        null=True,
                        verbose_name="Task Positional Arguments",
                    ),
                ),
                (
                    "task_kwargs",
                    models.TextField(
                        help_text="JSON representation of the named arguments used with the task",
                        null=True,
                        verbose_name="Task Named Arguments",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        default="PENDING",
                        help_text="Current state of the task being run",
                        max_length=50,
                        verbose_name="Task State",
                    ),
                ),
                (
                    "worker",
                    models.CharField(
                        default=None,
                        help_text="Worker that executes the task",
                        max_length=100,
                        null=True,
                        verbose_name="Worker",
                    ),
                ),
                (
                    "content_type",
                    models.CharField(
                        help_text="Content type of the result data",
                        max_length=128,
                        verbose_name="Result Content Type",
                    ),
                ),
                (
                    "content_encoding",
                    models.CharField(
                        help_text="The encoding used to save the task result data",
                        max_length=64,
                        verbose_name="Result Encoding",
                    ),
                ),
                (
                    "result",
                    models.TextField(
                        default=None,
                        editable=False,
                        help_text="The data returned by the task.  Use content_encoding and content_type fields to read.",
                        null=True,
                        verbose_name="Result Data",
                    ),
                ),
                (
                    "date_created",
                    models.DateTimeField(
                        auto_now_add=True,
                        help_text="Datetime field when the task result was created in UTC",
                        verbose_name="Created DateTime",
                    ),
                ),
                (
                    "date_started",
                    models.DateTimeField(
                        default=None,
                        help_text="Datetime field when the task was started in UTC",
                        null=True,
                        verbose_name="Started DateTime",
                    ),
                ),
                (
                    "date_done",
                    models.DateTimeField(
                        auto_now=True,
                        help_text="Datetime field when the task was completed in UTC",
                        verbose_name="Completed DateTime",
                    ),
                ),
                (
                    "traceback",
                    models.TextField(
                        blank=True,
                        help_text="Text of the traceback if the task generated one",
                        null=True,
                        verbose_name="Traceback",
                    ),
                ),
                (
                    "meta",
                    models.TextField(
                        default=None,
                        editable=False,
                        help_text="JSON meta information about the task, such as information on child tasks",
                        null=True,
                        verbose_name="Task Meta Information",
                    ),
                ),
            ],
            options={
                "verbose_name": "task result",
                "verbose_name_plural": "task results",
                "ordering": ["-date_done"],
                "managed": False,
                "base_manager_name": "objects",
            },
        ),
    ]
