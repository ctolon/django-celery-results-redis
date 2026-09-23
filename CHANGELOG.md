# Changelog

## 0.1.0 (unreleased)

* Initial release: Redis result backend with `raw`, `indexed` and `redis_om`
  drivers, django-celery-results 2.6.0 compatible models, managers, admin,
  views and URLs.
* Lookups: `regex`, `iregex` and every datetime transform Django defines,
  chainable as `date__year`, `time__hour` and so on.
* Optional RediSearch trigram index (`SEARCH`) for the admin search.
* Redis Sentinel support through `sentinel://` URLs.
* Storage format version recorded under `<prefix>:meta`.
* `celery_results_redis_import` copies existing django-celery-results rows,
  timestamps included.
