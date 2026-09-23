"""System checks for the ``DJANGO_CELERY_RESULTS_REDIS`` setting and server."""

import contextlib
import importlib.util
import re
from urllib.parse import urlsplit

from django.core.checks import Error, Tags, Warning, register
from django.utils.module_loading import import_string

from .conf import SETTING_NAME, app_settings
from .store import DRIVERS, get_client
from .store.drivers.base import Store

ID = "django_celery_results_redis"
KEY_PREFIX_RE = re.compile(r"^[^\s:*?\[\]]+$")
MIN_REDIS_VERSION = (7, 0)


@register()
def check_settings(app_configs, **kwargs):
    errors = []
    driver = app_settings.DRIVER
    driver_class = None
    if driver == "redis_om" and importlib.util.find_spec("redis_om") is None:
        errors.append(
            Error(
                "The redis_om driver requires the redis-om package.",
                hint="pip install 'django-celery-results-redis[redis-om]'",
                id=f"{ID}.E002",
            )
        )
    else:
        with contextlib.suppress(ImportError):
            driver_class = import_string(DRIVERS.get(driver, driver))
        if not (isinstance(driver_class, type) and issubclass(driver_class, Store)):
            driver_class = None
            errors.append(
                Error(
                    f"{SETTING_NAME}['DRIVER'] {driver!r} is not a known driver or "
                    "the dotted path of a Store subclass.",
                    hint=f"Use one of: {', '.join(DRIVERS)}.",
                    id=f"{ID}.E001",
                )
            )
    search = app_settings.SEARCH
    if search not in (None, "redisearch"):
        errors.append(
            Error(
                f"{SETTING_NAME}['SEARCH'] must be None or 'redisearch'.",
                id=f"{ID}.E007",
            )
        )
    if (
        (driver == "redis_om" or search)
        and not app_settings.CLIENT_FACTORY
        and _url_database(app_settings.URL) != 0
    ):
        errors.append(
            Error(
                "RediSearch requires Redis database 0.",
                hint="RediSearch only indexes keys in database 0.",
                id=f"{ID}.E003",
            )
        )
    prefix = app_settings.KEY_PREFIX
    if not isinstance(prefix, str) or not KEY_PREFIX_RE.match(prefix):
        errors.append(
            Error(
                f"{SETTING_NAME}['KEY_PREFIX'] must be a non-empty string without "
                "whitespace, ':', '*', '?', '[' or ']'.",
                id=f"{ID}.E004",
            )
        )
    if not isinstance(app_settings.allow_edits, bool):
        errors.append(
            Error(f"{SETTING_NAME}['ALLOW_EDITS'] must be a boolean.", id=f"{ID}.E005")
        )
    errors.extend(_check_result_ttl(driver_class))
    return errors


def _check_result_ttl(driver_class):
    ttl = app_settings.result_ttl
    if ttl is None:
        return []
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        return [
            Error(
                f"{SETTING_NAME}['RESULT_TTL'] must be a positive number of seconds "
                "or a timedelta.",
                id=f"{ID}.E006",
            )
        ]
    if driver_class is not None and not driver_class.supports_ttl:
        return [
            Error(
                f"{SETTING_NAME}['RESULT_TTL'] is not supported by the "
                f"{app_settings.DRIVER} driver.",
                hint="Use Celery's result_expires with the backend_cleanup task.",
                id=f"{ID}.E006",
            )
        ]
    return []


def _url_database(url):
    path = urlsplit(url).path.strip("/")
    return int(path) if path.isdigit() else 0


@register(Tags.database)
def check_server(app_configs, databases=None, **kwargs):
    if not databases:
        return []
    try:
        client = get_client()
        info = client.info("server")
        memory = client.info("memory")
    except Exception as exc:
        return [Error(f"Cannot connect to Redis: {exc}", id=f"{ID}.E101")]
    errors = []
    if app_settings.DRIVER == "redis_om" or app_settings.SEARCH:
        modules = {module["name"].lower() for module in client.module_list()}
        if not modules & {"search", "ft"}:
            feature = (
                "The redis_om driver"
                if app_settings.DRIVER == "redis_om"
                else "The search index"
            )
            errors.append(
                Error(
                    f"{feature} requires RediSearch.",
                    hint="Use Redis 8 or Redis Stack.",
                    id=f"{ID}.E102",
                )
            )
    version = tuple(int(part) for part in info["redis_version"].split(".")[:2])
    if version < MIN_REDIS_VERSION:
        errors.append(
            Error(
                f"Redis {info['redis_version']} is not supported; "
                "Redis 7.0 or later is required.",
                id=f"{ID}.E103",
            )
        )
    if str(memory.get("maxmemory_policy", "")).startswith("allkeys-"):
        errors.append(
            Warning(
                f"maxmemory-policy {memory['maxmemory_policy']} may evict results "
                "and leave secondary indexes out of sync.",
                hint="Use noeviction or a volatile-* policy.",
                id=f"{ID}.W101",
            )
        )
    return errors
