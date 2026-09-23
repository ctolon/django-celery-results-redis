from datetime import datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ...models import GroupResult, TaskResult
from ...store import get_store

BATCH_SIZE = 500


def parse_moment(value):
    """Parse a ``--since`` value into a datetime in the current time zone."""
    moment = parse_datetime(value)
    if moment is None:
        day = parse_date(value)
        if day is None:
            raise CommandError(f"Cannot parse {value!r} as a date or datetime.")
        moment = datetime.combine(day, time.min)
    if settings.USE_TZ and timezone.is_naive(moment):
        return timezone.make_aware(moment)
    return moment


class Command(BaseCommand):
    help = (
        "Copy task and group results from the django-celery-results tables into "
        "Redis. Existing records with the same id are overwritten."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            help="Only copy results completed on or after this date (YYYY-MM-DD).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=BATCH_SIZE,
            help=f"Rows read per batch (default {BATCH_SIZE}).",
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to read from (default 'default').",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the rows that would be copied without writing them.",
        )

    def handle(self, *args, **options):
        try:
            from django_celery_results import models as source
        except ImportError as exc:
            raise CommandError(
                "django-celery-results is not installed, so there is nothing to "
                "import from."
            ) from exc

        since = options["since"] and parse_moment(options["since"])
        for source_model, model in (
            (source.TaskResult, TaskResult),
            (source.GroupResult, GroupResult),
        ):
            copied = self.copy(source_model, model, since, options)
            self.stdout.write(f"{copied} {model._meta.verbose_name_plural} imported.")

    def copy(self, source_model, model, since, options):
        """Copy every row of ``source_model``, timestamps included."""
        pk_name = model._meta.pk.name
        fields = [
            field.name for field in model._meta.concrete_fields if field.name != pk_name
        ]
        rows = source_model.objects.using(options["database"]).order_by("pk")
        if since:
            rows = rows.filter(date_done__gte=since)
        rows = rows.values(pk_name, *fields).iterator(chunk_size=options["batch_size"])
        if options["dry_run"]:
            return sum(1 for _ in rows)
        store = get_store(model)
        store.batch_size = options["batch_size"]
        return store.write_many((row.pop(pk_name), row) for row in rows)
