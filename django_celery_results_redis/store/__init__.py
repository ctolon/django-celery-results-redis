"""Redis clients and per-model stores built from the app settings."""

import threading
from urllib.parse import urlsplit

import redis
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

from ..conf import SETTING_NAME, app_settings

DRIVERS = {
    "raw": "django_celery_results_redis.store.drivers.raw.RawStore",
    "indexed": "django_celery_results_redis.store.drivers.indexed.IndexedStore",
    "redis_om": "django_celery_results_redis.store.drivers.redis_om.RedisOMStore",
}

#: Layout of the stored records. Written to ``<prefix>:meta`` on first use; a
#: different version means the data was written by another release and the
#: package refuses to read it.
FORMAT_VERSION = 1
SENTINEL_SCHEMES = ("sentinel", "rediss+sentinel")
SENTINEL_URI_SEPARATOR = ";"

_lock = threading.RLock()
_client = None
_stores = {}
_checked_prefixes = set()


def driver_class():
    driver = app_settings.DRIVER
    return import_string(DRIVERS.get(driver, driver))


def create_client():
    if app_settings.CLIENT_FACTORY:
        return import_string(app_settings.CLIENT_FACTORY)()
    kwargs = {**app_settings.CLIENT_KWARGS, "decode_responses": True}
    url = app_settings.URL
    if urlsplit(url.split(SENTINEL_URI_SEPARATOR)[0]).scheme in SENTINEL_SCHEMES:
        return create_sentinel_client(url, kwargs)
    return redis.Redis.from_url(url, **kwargs)


def create_sentinel_client(url, kwargs):
    """Return the current master of a ``sentinel://`` URL.

    The URL lists the sentinels separated by ``;``, like Celery's sentinel
    result backend, for example
    ``sentinel://host-a:26379;sentinel://host-b:26379/0``.
    """
    sentinels, connection_kwargs = [], {}
    for part in url.split(SENTINEL_URI_SEPARATOR):
        parsed = urlsplit(part)
        sentinels.append((parsed.hostname, parsed.port or 26379))
        database = parsed.path.strip("/")
        connection_kwargs.update(
            {
                key: value
                for key, value in (
                    ("username", parsed.username),
                    ("password", parsed.password),
                    ("db", int(database) if database else None),
                    ("ssl", parsed.scheme == "rediss+sentinel" or None),
                )
                if value is not None
            }
        )
    sentinel = redis.sentinel.Sentinel(
        sentinels,
        sentinel_kwargs=app_settings.SENTINEL_KWARGS,
        **connection_kwargs,
        **kwargs,
    )
    return sentinel.master_for(app_settings.SENTINEL_MASTER_NAME)


def get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = create_client()
    return _client


def check_format_version(client, prefix):
    """Claim the key prefix for this format version, or refuse to use it."""
    key = f"{prefix}:meta"
    stored = client.hget(key, "format")
    if stored is None:
        client.hsetnx(key, "format", FORMAT_VERSION)
        return
    if int(stored) != FORMAT_VERSION:
        raise ImproperlyConfigured(
            f"The results under the key prefix {prefix!r} use storage format "
            f"{stored}, but this version of django-celery-results-redis writes "
            f"format {FORMAT_VERSION}. Use a matching release, a different "
            f"{SETTING_NAME}['KEY_PREFIX'], or delete the old results."
        )


def get_store(model):
    """Return the store of ``model`` for the configured driver."""
    store = _stores.get(model)
    if store is None:
        with _lock:
            store = _stores.get(model)
            if store is None:
                client = get_client()
                prefix = app_settings.KEY_PREFIX
                if prefix not in _checked_prefixes:
                    check_format_version(client, prefix)
                    _checked_prefixes.add(prefix)
                cls = driver_class() if model.redis_indexed_fields else _raw_store()
                store = cls(
                    model,
                    client,
                    prefix=prefix,
                    scan_count=app_settings.SCAN_COUNT,
                    batch_size=app_settings.BATCH_SIZE,
                    ttl=app_settings.result_ttl,
                    search=app_settings.SEARCH,
                )
                store.prepare()
                _stores[model] = store
    return store


def _raw_store():
    from .drivers.raw import RawStore

    return RawStore


def reset():
    """Drop the cached client and stores; the next access rebuilds them."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
        _client = None
        _stores.clear()
        _checked_prefixes.clear()


@receiver(setting_changed)
def _reset_on_setting_changed(*, setting, **kwargs):
    if setting == SETTING_NAME:
        reset()
