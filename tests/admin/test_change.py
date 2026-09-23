import pytest
from django.contrib.admin.models import CHANGE, DELETION, LogEntry
from django.contrib.auth.models import Permission, User
from django.urls import reverse
from django_celery_results import admin as upstream_admin
from django_celery_results import models as upstream_models

from django_celery_results_redis.models import TaskResult

from .conftest import TASKS_URL

pytestmark = pytest.mark.django_db

EDITABLE = {
    "task_id": "t00",
    "task_name": "app.renamed",
    "periodic_task_name": "weekly",
    "status": "FAILURE",
    "worker": "w9",
    "content_type": "application/json",
    "content_encoding": "utf-8",
    "task_args": "[1, 2]",
    "task_kwargs": "{}",
    "traceback": "",
}


def url(view, *args, app="django_celery_results_redis"):
    return reverse(f"admin:{app}_taskresult_{view}", args=args)


@pytest.fixture
def allow_edits(settings, monkeypatch):
    settings.DJANGO_CELERY_RESULTS = {"ALLOW_EDITS": True}
    monkeypatch.setattr(upstream_admin, "ALLOW_EDITS", True)


def test_change_view_is_read_only_by_default(tasks, admin_client):
    response = admin_client.get(url("change", "t00"))
    assert response.status_code == 200
    assert 'name="status"' not in response.text
    assert "app.add" in response.text
    response = admin_client.post(url("change", "t00"), {**EDITABLE, "_save": "Save"})
    assert response.status_code == 302
    assert TaskResult.objects.get(pk="t00").status == "SUCCESS"


def test_change_view_of_missing_object_redirects(tasks, admin_client):
    response = admin_client.get(url("change", "missing"))
    assert response.status_code == 302
    assert response.url == reverse("admin:index")


@pytest.mark.usefixtures("allow_edits")
def test_editing_persists(tasks, admin_client):
    before = TaskResult.objects.get(pk="t00")
    response = admin_client.get(url("change", "t00"))
    assert 'name="status"' in response.text
    assert 'name="result"' not in response.text
    response = admin_client.post(url("change", "t00"), {**EDITABLE, "_save": "Save"})
    assert response.status_code == 302
    after = TaskResult.objects.get(pk="t00")
    assert (after.status, after.task_name, after.worker) == (
        "FAILURE",
        "app.renamed",
        "w9",
    )
    assert after.periodic_task_name == "weekly"
    assert after.date_created == before.date_created
    assert after.date_done > before.date_done
    entry = LogEntry.objects.get(action_flag=CHANGE)
    assert entry.object_id == "t00"
    assert TaskResult.objects.filter(status="FAILURE", worker="w9").count() == 1


@pytest.mark.usefixtures("allow_edits")
def test_editing_the_task_id_renames_the_record(tasks, admin_client):
    data = {**EDITABLE, "task_id": "renamed", "_save": "Save"}
    response = admin_client.post(url("change", "t00"), data)
    assert response.status_code == 302
    assert not TaskResult.objects.filter(pk="t00").exists()
    assert TaskResult.objects.get(pk="renamed").task_name == "app.renamed"
    assert TaskResult.objects.filter(task_name="app.renamed").count() == 1
    assert LogEntry.objects.get(action_flag=CHANGE).object_id == "renamed"


@pytest.mark.usefixtures("allow_edits")
def test_renaming_to_an_existing_task_id_is_rejected(tasks, admin_client):
    data = {**EDITABLE, "task_id": "t01", "_save": "Save"}
    response = admin_client.post(url("change", "t00"), data)
    assert response.status_code == 200
    assert "Task result with this Task ID already exists." in response.text
    assert TaskResult.objects.get(pk="t01").status == "FAILURE"
    assert TaskResult.objects.get(pk="t00").status == "SUCCESS"


@pytest.mark.usefixtures("allow_edits")
@pytest.mark.parametrize(
    "app", ["django_celery_results_redis", "django_celery_results"]
)
def test_add_view(store_driver, admin_client, app):
    data = {**EDITABLE, "task_id": "new", "_save": "Save"}
    response = admin_client.post(url("add", app=app), data)
    assert response.status_code == 302
    model = (
        TaskResult
        if app == "django_celery_results_redis"
        else (upstream_models.TaskResult)
    )
    created = model.objects.get(task_id="new")
    assert (created.status, created.task_name) == ("FAILURE", "app.renamed")
    assert created.date_created is not None


def test_delete_view(tasks, admin_client):
    response = admin_client.get(url("delete", "t00"))
    assert response.status_code == 200
    assert "Are you sure" in response.text
    assert "&lt;Task: t00 (SUCCESS)&gt;" in response.text
    response = admin_client.post(url("delete", "t00"), {"post": "yes"})
    assert response.status_code == 302
    assert not TaskResult.objects.filter(pk="t00").exists()
    assert TaskResult.objects.count() == 7
    assert LogEntry.objects.get(action_flag=DELETION).object_id == "t00"


def test_bulk_delete_action(tasks, admin_client):
    data = {"action": "delete_selected", "_selected_action": ["t00", "t01"]}
    response = admin_client.post(TASKS_URL, data)
    assert response.status_code == 200
    assert "Are you sure" in response.text
    assert "Task results: 2" in response.text
    response = admin_client.post(TASKS_URL, {**data, "post": "yes"})
    assert response.status_code == 302
    assert set(TaskResult.objects.values_list("pk", flat=True)) == {
        "t02",
        "t03",
        "t04",
        "t05",
        "t06",
        "t07",
    }
    assert LogEntry.objects.filter(action_flag=DELETION).count() == 2


def test_bulk_delete_across_filtered_results(tasks, admin_client):
    data = {
        "action": "delete_selected",
        "_selected_action": ["t00"],
        "select_across": "1",
        "index": "0",
        "post": "yes",
    }
    response = admin_client.post(f"{TASKS_URL}?status__exact=SUCCESS", data)
    assert response.status_code == 302
    assert not TaskResult.objects.filter(status="SUCCESS").exists()
    assert TaskResult.objects.count() == 4


@pytest.mark.usefixtures("allow_edits")
def test_history_view(tasks, admin_client):
    admin_client.post(url("change", "t00"), {**EDITABLE, "_save": "Save"})
    response = admin_client.get(url("history", "t00"))
    assert response.status_code == 200
    assert "Changed" in response.text


def test_staff_without_permissions_is_denied(tasks, client):
    user = User.objects.create_user("staff", password="pw", is_staff=True)
    client.force_login(user)
    assert client.get(TASKS_URL).status_code == 403
    assert client.get(url("change", "t00")).status_code == 403
    user.user_permissions.add(
        Permission.objects.get(
            codename="view_taskresult",
            content_type__app_label="django_celery_results_redis",
        )
    )
    assert client.get(TASKS_URL).status_code == 200
    assert client.post(url("delete", "t00"), {"post": "yes"}).status_code == 403
    assert TaskResult.objects.filter(pk="t00").exists()


def test_anonymous_user_is_redirected_to_login(tasks, client):
    response = client.get(TASKS_URL)
    assert response.status_code == 302
    assert reverse("admin:login") in response.url
