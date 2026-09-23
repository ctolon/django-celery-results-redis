"""Render the changelists of both packages from identical data and compare them."""

from datetime import datetime, timezone

import pytest
import time_machine

from .conftest import (
    GROUPS_URL,
    TASKS,
    TASKS_URL,
    UPSTREAM_GROUPS_URL,
    UPSTREAM_TASKS_URL,
    filter_choices,
    hierarchy,
    load_groups,
    row_ids,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2024, 2, 15, 18, 0, tzinfo=timezone.utc)
# Ordering column -> TASKS index of a nullable field. NULL ordering is database
# dependent (docs/admin-parity.md), so only the order of non-null rows is compared.
NULLABLE_COLUMNS = {"2": 4, "3": 2, "6": 3}

PARAMS = [
    "",
    "o=1",
    "o=-1",
    "o=2&all=",
    "o=-2&all=",
    "o=3&all=",
    "o=-3&all=",
    "o=4",
    "o=-4",
    "o=5",
    "o=-5",
    "o=6&all=",
    "o=-6&all=",
    "o=5.-4",
    "o=-5.4.1",
    "status__exact=SUCCESS",
    "status__exact=MISSING",
    "worker__isnull=True",
    "periodic_task_name__isnull=True",
    "task_name__exact=app.add&worker__exact=w1",
    "task_name__isnull=True",
    "date_done__gte=2024-01-01+00%3A00%3A00%2B00%3A00"
    "&date_done__lt=2024-02-01+00%3A00%3A00%2B00%3A00",
    "q=foo",
    "q=FOO",
    "q=app.add",
    "q=%22foo+bar%22",
    "q=needle",
    "q=add+t0",
    "q=SUCCESS+mul",
    "date_done__year=2024",
    "date_done__year=2024&date_done__month=2",
    "date_done__year=2024&date_done__month=2&date_done__day=15",
    "date_done__year=2023&date_done__month=12&date_done__day=31",
    "_facets=",
    "_facets=&status__exact=SUCCESS",
    "_facets=&q=app",
    "all=",
    "p=2",
    "p=3&o=4",
    "all=&o=-4",
]


def comparable_ids(params, response):
    ids = row_ids(response)
    ordering = dict(p.split("=", 1) for p in params.split("&") if p).get("o", "")
    column = NULLABLE_COLUMNS.get(ordering.lstrip("-"))
    if column is None:
        return ids
    values = {task[0]: task[column] for task in TASKS}
    return [task_id for task_id in ids if values[task_id] is not None], set(ids)


def page_state(response):
    cl = response.context["cl"]
    return {
        "result_count": cl.result_count,
        "full_result_count": cl.full_result_count,
        "multi_page": cl.multi_page,
        "can_show_all": cl.can_show_all,
        "show_all": cl.show_all,
        "num_pages": cl.paginator.num_pages,
        "page_num": cl.page_num,
    }


@pytest.mark.parametrize("params", PARAMS)
def test_task_changelist_matches_upstream(
    params, tasks, upstream_tasks, admin_client, per_page
):
    per_page(3)
    with time_machine.travel(NOW, tick=False):
        ours = admin_client.get(f"{TASKS_URL}?{params}")
        theirs = admin_client.get(f"{UPSTREAM_TASKS_URL}?{params}")
        assert ours.status_code == theirs.status_code == 200
        assert comparable_ids(params, ours) == comparable_ids(params, theirs)
        assert page_state(ours) == page_state(theirs)
        assert filter_choices(ours) == filter_choices(theirs)
        assert hierarchy(ours) == hierarchy(theirs)


@pytest.mark.parametrize("time_zone", ["America/New_York", "Asia/Tokyo"])
@pytest.mark.parametrize(
    "params",
    [
        "",
        "date_done__year=2023",
        "date_done__year=2024&date_done__month=1",
        "date_done__year=2023&date_done__month=12&date_done__day=31",
        "date_done__year=2024&date_done__month=1&date_done__day=1",
        "_facets=",
    ],
)
def test_task_changelist_in_local_time_matches_upstream(
    params, time_zone, tasks, upstream_tasks, admin_client, settings
):
    settings.TIME_ZONE = time_zone
    with time_machine.travel(NOW, tick=False):
        ours = admin_client.get(f"{TASKS_URL}?{params}")
        theirs = admin_client.get(f"{UPSTREAM_TASKS_URL}?{params}")
        assert row_ids(ours) == row_ids(theirs)
        assert filter_choices(ours) == filter_choices(theirs)
        assert hierarchy(ours) == hierarchy(theirs)


@pytest.mark.parametrize("params", ["p=99", "status=", "nonexistent=1"])
def test_task_changelist_errors_match_upstream(
    params, tasks, upstream_tasks, admin_client, per_page
):
    per_page(3)
    ours = admin_client.get(f"{TASKS_URL}?{params}")
    theirs = admin_client.get(f"{UPSTREAM_TASKS_URL}?{params}")
    assert ours.status_code == theirs.status_code
    assert (
        ours.headers.get("Location", "").rsplit("?", 1)[-1]
        == (theirs.headers.get("Location", "").rsplit("?", 1)[-1])
    )


@pytest.mark.parametrize(
    "params", ["", "o=1", "o=-2", "q=g0", "date_done__year=2024", "_facets="]
)
def test_group_changelist_matches_upstream(params, store_driver, admin_client):
    from django_celery_results import models as upstream_models

    from django_celery_results_redis import models

    load_groups(models.GroupResult)
    load_groups(upstream_models.GroupResult)
    with time_machine.travel(NOW, tick=False):
        ours = admin_client.get(f"{GROUPS_URL}?{params}")
        theirs = admin_client.get(f"{UPSTREAM_GROUPS_URL}?{params}")
        assert ours.status_code == theirs.status_code == 200
        assert row_ids(ours, "group_id") == row_ids(theirs, "group_id")
        assert page_state(ours) == page_state(theirs)
        assert filter_choices(ours) == filter_choices(theirs)
        assert hierarchy(ours) == hierarchy(theirs)
