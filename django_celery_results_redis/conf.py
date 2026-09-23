"""Access to the ``DJANGO_CELERY_RESULTS_REDIS`` setting."""

from datetime import timedelta

from django.conf import settings

SETTING_NAME = "DJANGO_CELERY_RESULTS_REDIS"

DEFAULTS = {
    "DRIVER": "indexed",
    "URL": "redis://localhost:6379/0",
    "CLIENT_KWARGS": {},
    "CLIENT_FACTORY": None,
    "KEY_PREFIX": "dcrr",
    "ALLOW_EDITS": None,
    "TASK_ID_MAX_LENGTH": None,
    "SCAN_COUNT": 1000,
    "BATCH_SIZE": 500,
    "RESULT_TTL": None,
    "ID_FIRST_URLS": False,
    "SEARCH": None,
    "SEARCH_MAX_TEXT": 4096,
    "SEARCH_MAX_CANDIDATES": 50_000,
    "SENTINEL_MASTER_NAME": "mymaster",
    "SENTINEL_KWARGS": {},
}


class AppSettings:
    """Read the setting on every access so ``override_settings`` is honoured."""

    def _user_settings(self):
        return getattr(settings, SETTING_NAME, None) or {}

    def __getattr__(self, name):
        if name not in DEFAULTS:
            raise AttributeError(f"Invalid {SETTING_NAME} key: {name!r}")
        return self._user_settings().get(name, DEFAULTS[name])

    @property
    def allow_edits(self):
        """``ALLOW_EDITS``, falling back to the django-celery-results setting."""
        value = self.ALLOW_EDITS
        if value is not None:
            return value
        try:
            return settings.DJANGO_CELERY_RESULTS["ALLOW_EDITS"]
        except (AttributeError, KeyError):
            return False

    @property
    def task_id_max_length(self):
        """``TASK_ID_MAX_LENGTH``, falling back to the django-celery-results setting."""
        value = self.TASK_ID_MAX_LENGTH
        if value is not None:
            return value
        return getattr(settings, "DJANGO_CELERY_RESULTS_TASK_ID_MAX_LENGTH", 255)

    @property
    def result_ttl(self):
        """``RESULT_TTL`` in whole seconds, or ``None``."""
        value = self.RESULT_TTL
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        return value


app_settings = AppSettings()
