"""URLs defined for celery.

* ``task/done/<task_id>/``
    URL to :func:`~.views.is_task_successful`.
* ``task/status/<task_id>/``
    URL to :func:`~.views.task_status`.
* ``group/done/<group_id>/``
    URL to :func:`~.views.is_group_successful`.
* ``group/status/<group_id>/``
    URL to :func:`~.views.group_status`.
"""

import warnings

from django.urls import path, register_converter

from . import views
from .conf import app_settings


class TaskPatternConverter:
    """Custom path converter for task & group id's.

    They are slightly different from the built `uuid`
    """

    regex = r"[\w\d\-\.]+"

    def to_python(self, value):
        """Convert url to python value."""
        return str(value)

    def to_url(self, value):
        """Convert python value into url, just a string."""
        return value


register_converter(TaskPatternConverter, "task_pattern")

urlpatterns = [
    path(
        "task/done/<task_pattern:task_id>/",
        views.is_task_successful,
        name="celery-is_task_successful",
    ),
    path(
        "task/status/<task_pattern:task_id>/",
        views.task_status,
        name="celery-task_status",
    ),
    path(
        "group/done/<task_pattern:group_id>/",
        views.is_group_successful,
        name="celery-is_group_successful",
    ),
    path(
        "group/status/<task_pattern:group_id>/",
        views.group_status,
        name="celery-group_status",
    ),
]

if app_settings.ID_FIRST_URLS:
    warnings.warn(
        "ID first urls are deprecated, use noun first urls instead.",
        DeprecationWarning,
        stacklevel=1,
    )

    urlpatterns += [
        path(
            "<task_pattern:task_id>/done/",
            views.is_task_successful,
            name="celery-is_task_successful",
        ),
        path(
            "<task_pattern:task_id>/status/",
            views.task_status,
            name="celery-task_status",
        ),
        path(
            "<task_pattern:group_id>/group/done/",
            views.is_group_successful,
            name="celery-is_group_successful",
        ),
        path(
            "<task_pattern:group_id>/group/status/",
            views.group_status,
            name="celery-group_status",
        ),
    ]
