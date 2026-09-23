from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("celery/", include("django_celery_results_redis.urls")),
]
