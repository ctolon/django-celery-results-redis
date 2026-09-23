import pytest


@pytest.fixture(autouse=True)
def test_cases_shortcuts(request, app):
    if request.instance:

        @app.task
        def add(x, y):
            return x + y

        request.instance.app = app
        request.instance.add = add
    yield
    if request.instance:
        request.instance.app = None
