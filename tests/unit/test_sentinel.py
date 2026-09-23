import pytest
import redis

from django_celery_results_redis import store

FACTORY_CLIENT = object()


def factory():
    return FACTORY_CLIENT


class FakeSentinel:
    def __init__(self, sentinels, **kwargs):
        self.sentinels = sentinels
        self.kwargs = kwargs
        self.master_name = None

    def master_for(self, name, **kwargs):
        self.master_name = name
        return self


@pytest.fixture
def sentinels(monkeypatch):
    created = []

    def create(sentinels, **kwargs):
        created.append(FakeSentinel(sentinels, **kwargs))
        return created[-1]

    monkeypatch.setattr(redis.sentinel, "Sentinel", create)
    return created


def test_single_sentinel(redis_settings, sentinels):
    redis_settings("raw", URL="sentinel://localhost:26379/0")

    client = store.create_client()

    assert sentinels[0].sentinels == [("localhost", 26379)]
    assert sentinels[0].kwargs["db"] == 0
    assert sentinels[0].kwargs["decode_responses"] is True
    assert sentinels[0].kwargs["sentinel_kwargs"] == {}
    assert client is sentinels[0]
    assert client.master_name == "mymaster"


def test_several_sentinels(redis_settings, sentinels):
    redis_settings("raw", URL="sentinel://a:26379;sentinel://b:26380/2")

    store.create_client()

    assert sentinels[0].sentinels == [("a", 26379), ("b", 26380)]
    assert sentinels[0].kwargs["db"] == 2


def test_default_port(redis_settings, sentinels):
    redis_settings("raw", URL="sentinel://a;sentinel://b")

    store.create_client()

    assert sentinels[0].sentinels == [("a", 26379), ("b", 26379)]
    assert "db" not in sentinels[0].kwargs


def test_credentials(redis_settings, sentinels):
    redis_settings("raw", URL="sentinel://user:secret@a:26379/1")

    store.create_client()

    assert sentinels[0].kwargs["username"] == "user"
    assert sentinels[0].kwargs["password"] == "secret"
    assert sentinels[0].kwargs["db"] == 1


def test_options_of_every_sentinel_are_kept(redis_settings, sentinels):
    redis_settings("raw", URL="sentinel://user:secret@a:26379;sentinel://b:26379/3")

    store.create_client()

    assert sentinels[0].kwargs["username"] == "user"
    assert sentinels[0].kwargs["password"] == "secret"
    assert sentinels[0].kwargs["db"] == 3


def test_tls(redis_settings, sentinels):
    redis_settings("raw", URL="rediss+sentinel://a:26379/0")

    store.create_client()

    assert sentinels[0].kwargs["ssl"] is True


def test_master_name_and_sentinel_kwargs(redis_settings, sentinels):
    redis_settings(
        "raw",
        URL="sentinel://a:26379/0",
        SENTINEL_MASTER_NAME="records",
        SENTINEL_KWARGS={"password": "sentinel-secret"},
        CLIENT_KWARGS={"socket_timeout": 3},
    )

    client = store.create_client()

    assert client.master_name == "records"
    assert sentinels[0].kwargs["sentinel_kwargs"] == {"password": "sentinel-secret"}
    assert sentinels[0].kwargs["socket_timeout"] == 3


def test_plain_url_does_not_use_sentinel(redis_settings, sentinels, monkeypatch):
    urls = []

    def from_url(cls, url, **kwargs):
        urls.append(url)

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(from_url))
    redis_settings("raw", URL="redis://localhost:6379/0")

    store.create_client()

    assert urls == ["redis://localhost:6379/0"]
    assert sentinels == []


def test_client_factory_wins(redis_settings, sentinels):
    redis_settings(
        "raw",
        URL="sentinel://a:26379/0",
        CLIENT_FACTORY="tests.unit.test_sentinel.factory",
    )

    assert store.create_client() is FACTORY_CLIENT
    assert sentinels == []
