"""Application configuration."""

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

__all__ = ["CeleryResultsRedisConfig"]


class CeleryResultsRedisConfig(AppConfig):
    """Default configuration for the django_celery_results_redis app."""

    name = "django_celery_results_redis"
    label = "django_celery_results_redis"
    verbose_name = _("Celery Results (Redis)")
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import checks  # noqa: F401
