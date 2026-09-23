from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from django_celery_results_redis.models import GroupResult, TaskResult

from .conftest import (
    GROUPS_URL,
    TASKS,
    TASKS_URL,
    column_index,
    filter_choices,
    hierarchy,
    load_groups,
    load_tasks,
    row_ids,
)

pytestmark = pytest.mark.django_db

ALL_IDS = [task[0] for task in TASKS]
BY_DATE_DESC = ["t07", "t06", "t05", "t04", "t03", "t02", "t01", "t00"]


def get(client, params=""):
    return client.get(f"{TASKS_URL}?{params}")


def test_default_ordering_is_newest_first(tasks, admin_client):
    response = get(admin_client)
    assert response.status_code == 200
    assert row_ids(response) == BY_DATE_DESC
    assert "8 task results" in response.text


def test_pagination(tasks, admin_client, per_page):
    per_page(3)
    pages = [row_ids(get(admin_client, f"p={page}")) for page in (1, 2, 3)]
    assert pages == [BY_DATE_DESC[0:3], BY_DATE_DESC[3:6], BY_DATE_DESC[6:]]
    assert get(admin_client, "p=4").url.endswith("?e=1")
    assert row_ids(get(admin_client, "all=")) == BY_DATE_DESC


def test_ordering_by_date_breaks_ties_by_primary_key(
    store_driver, admin_client, per_page
):
    rows = [
        (f"tie{i}", "SUCCESS", "n", "w", None, "[]", "{}", TASKS[0][7])
        for i in range(7)
    ]
    load_tasks(TaskResult, rows)
    per_page(3)
    index = column_index(get(admin_client), "date_done")
    expected = [f"tie{i}" for i in reversed(range(7))]
    for params in (f"o={index}", f"o=-{index}"):
        pages = [row_ids(get(admin_client, f"{params}&p={page}")) for page in (1, 2, 3)]
        assert [task_id for page in pages for task_id in page] == expected


@pytest.mark.parametrize(
    ("column", "values"),
    [
        ("task_id", {t[0]: t[0] for t in TASKS}),
        ("status", {t[0]: t[1] for t in TASKS}),
        ("task_name", {t[0]: t[2] for t in TASKS}),
        ("worker", {t[0]: t[3] for t in TASKS}),
        ("periodic_task_name", {t[0]: t[4] for t in TASKS}),
        ("date_done", {t[0]: t[7] for t in TASKS}),
    ],
)
def test_ordering_by_each_column(tasks, admin_client, column, values):
    index = column_index(get(admin_client), column)
    for descending in (False, True):
        response = get(admin_client, f"o={'-' if descending else ''}{index}")
        by_pk = sorted(ALL_IDS, reverse=True)
        present = sorted(
            (pk for pk in by_pk if values[pk] is not None),
            key=values.get,
            reverse=descending,
        )
        missing = [pk for pk in by_pk if values[pk] is None]
        expected = missing + present if descending else present + missing
        assert row_ids(response) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ("status__exact=SUCCESS", ["t07", "t05", "t02", "t00"]),
        ("status__exact=RETRY", ["t06"]),
        ("task_name__exact=app.mul", ["t04", "t02"]),
        ("task_name__isnull=True", ["t03"]),
        ("worker__exact=w1", ["t05", "t03", "t00"]),
        ("worker__isnull=True", ["t06", "t02"]),
        ("periodic_task_name__exact=hourly", ["t06", "t03"]),
        ("periodic_task_name__isnull=True", ["t07", "t05", "t02", "t01"]),
        ("status__exact=SUCCESS&worker__exact=w1", ["t05", "t00"]),
        ("worker__isnull=False&status__exact=SUCCESS", ["t07", "t05", "t00"]),
        ("date_done__gte=2024-02-15+12%3A00%3A00%2B00%3A00", BY_DATE_DESC[:5]),
        ("date_done__lt=2024-01-01+00%3A00%3A00%2B00%3A00", ["t00"]),
        ("status__in=RETRY,PENDING", ["t06", "t03"]),
        ("task_id__startswith=t0", BY_DATE_DESC),
        ("q=foo", ["t05", "t01"]),
        ("q=%22foo+bar%22", ["t05", "t01"]),
        ("q=FOO+BAR+t05", ["t05"]),
        ("q=needle", ["t03"]),
        ("q=add", ["t07", "t01", "t00"]),
        ("q=success+mul", ["t02"]),
        ("q=nothing", []),
        ("date_done__year=2024", ["t06", "t05", "t04", "t03", "t02", "t01"]),
        ("date_done__year=2024&date_done__month=2", ["t05", "t04", "t03"]),
        ("date_done__year=2024&date_done__month=1&date_done__day=1", ["t02", "t01"]),
    ],
)  # fmt: skip
def test_filters_and_search(tasks, admin_client, params, expected):
    response = get(admin_client, params)
    assert response.status_code == 200
    assert row_ids(response) == expected
    assert response.context["cl"].result_count == len(expected)
    assert response.context["cl"].full_result_count == len(TASKS)


def test_list_filter_choices(tasks, admin_client):
    choices = filter_choices(get(admin_client, "worker__isnull=True"))
    assert [display for display, _, _ in choices["Worker"]] == [
        "All",
        "w1",
        "w2",
        "w3",
        "-",
    ]
    assert [selected for _, selected, _ in choices["Worker"]] == [
        False,
        False,
        False,
        False,
        True,
    ]
    assert [display for display, _, _ in choices["Task State"]] == [
        "All",
        "FAILURE",
        "PENDING",
        "RETRY",
        "STARTED",
        "SUCCESS",
    ]
    assert [display for display, _, _ in choices["Task Name"]] == [
        "All",
        "app.add",
        "app.div",
        "app.mul",
        "-",
    ]


def test_facet_counts(tasks, admin_client):
    choices = filter_choices(get(admin_client, "_facets=&worker=w1"))
    assert [display for display, _, _ in choices["Task State"]] == [
        "All",
        "FAILURE (0)",
        "PENDING (1)",
        "RETRY (0)",
        "STARTED (0)",
        "SUCCESS (2)",
    ]
    assert [display for display, _, _ in choices["Worker"]] == [
        "All",
        "w1 (3)",
        "w2 (2)",
        "w3 (1)",
        "- (2)",
    ]


def test_date_filter_links(store_driver, admin_client):
    now = datetime(2024, 3, 20, 15, 0, tzinfo=timezone.utc)
    rows = [
        (f"d{days:02}", "S", "n", "w", None, "[]", "{}", now - timedelta(days=days))
        for days in (0, 1, 5, 10, 40, 400)
    ]
    load_tasks(TaskResult, rows)
    with time_machine.travel(now, tick=False):
        choices = filter_choices(get(admin_client))["Completed DateTime"]
        counts = {
            display: len(row_ids(get(admin_client, query.lstrip("?"))))
            for display, _, query in choices
        }
    assert counts == {
        "Any date": 6,
        "Today": 1,
        "Past 7 days": 3,
        "This month": 4,
        "This year": 5,
    }


def test_date_hierarchy_drill_down(tasks, admin_client):
    top = hierarchy(get(admin_client))
    assert [choice["title"] for choice in top["choices"]] == ["2023", "2024", "2025"]
    year = hierarchy(get(admin_client, "date_done__year=2024"))
    assert [choice["title"] for choice in year["choices"]] == [
        "January 2024",
        "February 2024",
        "March 2024",
    ]
    month = hierarchy(get(admin_client, "date_done__year=2024&date_done__month=1"))
    assert [choice["title"] for choice in month["choices"]] == ["January 1"]


def test_date_hierarchy_in_local_time(tasks, admin_client, settings):
    settings.TIME_ZONE = "America/New_York"
    top = hierarchy(get(admin_client, "date_done__year=2023"))
    assert [choice["title"] for choice in top["choices"]] == ["December 2023"]
    day = get(
        admin_client, "date_done__year=2023&date_done__month=12&date_done__day=31"
    )
    assert row_ids(day) == ["t02", "t01", "t00"]


def test_date_hierarchy_without_time_zone_support(settings, store_driver, admin_client):
    settings.USE_TZ = False
    load_tasks(TaskResult, use_tz=False)
    day = get(admin_client, "date_done__year=2024&date_done__month=2&date_done__day=15")
    assert row_ids(day) == ["t05", "t04", "t03"]
    assert row_ids(get(admin_client)) == BY_DATE_DESC
    assert [c["title"] for c in hierarchy(get(admin_client))["choices"]] == [
        "2023",
        "2024",
        "2025",
    ]


@pytest.mark.parametrize(
    "params",
    [
        "nonexistent=1",
        "date_done__gte=not-a-date",
        "date_done__year=abc",
        "task_name__regex=[",
        "task_name__trigram_similar=app",
    ],
)
def test_invalid_parameters_redirect_with_error(tasks, admin_client, params):
    response = get(admin_client, params)
    assert response.status_code == 302
    assert response.url.endswith("?e=1")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ("task_name__regex=^app[.]add$", ["t07", "t01", "t00"]),
        ("task_name__iregex=APP%5C.MUL", ["t04", "t02"]),
        ("date_done__week_day=2", ["t02", "t01"]),
        ("date_done__iso_week_day=7", ["t07", "t06", "t00"]),
        ("date_done__quarter=1", ["t06", "t05", "t04", "t03", "t02", "t01"]),
        ("date_done__hour__gte=12", ["t05", "t04", "t03", "t00"]),
        ("date_done__date__year=2025", ["t07"]),
    ],
)
def test_extended_lookups(tasks, admin_client, params, expected):
    assert row_ids(get(admin_client, params)) == expected


def test_search_result_count_text(tasks, admin_client):
    response = get(admin_client, "q=app.add")
    assert "3 results" in response.text
    assert "8 total" in response.text


def test_group_changelist(store_driver, admin_client):
    load_groups(GroupResult)
    response = admin_client.get(GROUPS_URL)
    assert row_ids(response, "group_id") == ["g02", "g01", "g00"]
    response = admin_client.get(f"{GROUPS_URL}?q=g01")
    assert row_ids(response, "group_id") == ["g01"]
    response = admin_client.get(f"{GROUPS_URL}?date_done__year=2024")
    assert row_ids(response, "group_id") == ["g01", "g00"]
