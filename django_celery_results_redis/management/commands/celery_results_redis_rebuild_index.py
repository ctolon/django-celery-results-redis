from django.core.management.base import BaseCommand

from ...conf import app_settings
from ...models import GroupResult, TaskResult
from ...store import get_store


class Command(BaseCommand):
    help = (
        "Rebuild the indexes of the configured storage driver from the stored "
        "task and group results."
    )

    def handle(self, *args, **options):
        for model in (TaskResult, GroupResult):
            get_store(model).rebuild()
            self.stdout.write(
                f"Rebuilt the {app_settings.DRIVER} indexes of {model._meta.label}."
            )
