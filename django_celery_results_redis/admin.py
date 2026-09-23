"""Result Task Admin interface."""

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .conf import app_settings
from .models import GroupResult, TaskResult


class TaskResultAdmin(admin.ModelAdmin):
    """Admin-interface for results of tasks."""

    model = TaskResult
    date_hierarchy = "date_done"
    list_display = (
        "task_id",
        "periodic_task_name",
        "task_name",
        "date_done",
        "status",
        "worker",
    )
    list_filter = ("status", "date_done", "periodic_task_name", "task_name", "worker")
    readonly_fields = ("date_created", "date_started", "date_done", "result", "meta")
    search_fields = ("task_name", "task_id", "status", "task_args", "task_kwargs")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "task_id",
                    "task_name",
                    "periodic_task_name",
                    "status",
                    "worker",
                    "content_type",
                    "content_encoding",
                ),
                "classes": ("extrapretty", "wide"),
            },
        ),
        (
            _("Parameters"),
            {
                "fields": ("task_args", "task_kwargs"),
                "classes": ("extrapretty", "wide"),
            },
        ),
        (
            _("Result"),
            {
                "fields": (
                    "result",
                    "date_created",
                    "date_started",
                    "date_done",
                    "traceback",
                    "meta",
                ),
                "classes": ("extrapretty", "wide"),
            },
        ),
    )

    def get_readonly_fields(self, request, obj=None):
        if app_settings.allow_edits:
            return self.readonly_fields
        return list({field.name for field in self.opts.local_fields})


admin.site.register(TaskResult, TaskResultAdmin)


class GroupResultAdmin(admin.ModelAdmin):
    """Admin-interface for results of grouped tasks."""

    model = GroupResult
    date_hierarchy = "date_done"
    list_display = ("group_id", "date_done")
    list_filter = ("date_done",)
    readonly_fields = ("date_created", "date_done", "result")
    search_fields = ("group_id",)


admin.site.register(GroupResult, GroupResultAdmin)
