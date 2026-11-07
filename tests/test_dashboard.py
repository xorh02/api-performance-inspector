import csv
import io
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api_performance_inspector import PerformanceInspector
from api_performance_inspector.dashboard import create_dashboard_app
from api_performance_inspector.export import CSV_COLUMNS
from api_performance_inspector.storage import MemoryStore


@pytest.fixture
def mounted():
    app = FastAPI()
    inspector = PerformanceInspector()

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.post("/login")
    async def login(request: Request):
        await request.body()
        return {"ok": True}

    inspector.instrument(app, dashboard_path="/_inspector")
    return TestClient(app), inspector


def test_index_and_assets_served_with_security_headers(mounted):
    client, _ = mounted
    page = client.get("/_inspector/")
    assert page.status_code == 200
    assert "API Performance Inspector" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/_inspector/static/app.js").status_code == 200
    assert client.get("/_inspector/static/style.css").status_code == 200


def test_mount_without_trailing_slash_redirects(mounted):
    client, _ = mounted
    response = client.get("/_inspector", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"].endswith("/_inspector/")


def test_empty_state(mounted):
    client, _ = mounted
    data = client.get("/_inspector/api/summary").json()
    assert data["overview"]["requests"] == 0
    assert data["endpoints"] == []
    assert data["collector"]["recorded"] == 0
    assert "No requests recorded" in client.get("/_inspector/").text


def test_summary_reflects_real_requests_and_excludes_dashboard(mounted):
    client, _ = mounted
    for i in range(3):
        client.get(f"/items/{i}")
    client.get("/missing")
    client.get("/_inspector/")
    client.get("/_inspector/api/summary")
    data = client.get("/_inspector/api/summary?window=300").json()
    assert data["overview"]["requests"] == 4
    routes = {(e["route"], e["requests"]) for e in data["endpoints"]}
    assert routes == {("/items/{item_id}", 3), ("<unmatched>", 1)}
    assert data["endpoints"][0]["route"] == "/items/{item_id}"  # ordered by volume
    assert data["collector"]["storage"] == "memory"
    assert data["window"]["seconds"] == 300


def test_exports(mounted):
    client, _ = mounted
    client.get("/items/1")
    client.get("/items/2")

    response = client.get("/_inspector/api/export?format=csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert [r["route"] for r in rows] == ["/items/{item_id}"] * 2
    assert all(float(r["duration_ms"]) > 0 for r in rows)

    document = client.get("/_inspector/api/export?format=json&window=60").json()
    assert document["schema_version"] == 1
    assert document["summary"]["overview"]["requests"] == 2
    assert len(document["measurements"]) == 2
    assert set(document["measurements"][0]) == set(CSV_COLUMNS)


def test_sample_limit_flag():
    app = FastAPI()
    inspector = PerformanceInspector(max_samples=3)

    @app.get("/x")
    async def x():
        return {}

    inspector.instrument(app, dashboard_path="/_inspector")
    client = TestClient(app)
    client.get("/x")
    assert client.get("/_inspector/api/summary").json()["sample_limit"] == {
        "max_samples": 3,
        "reached": False,
    }
    for _ in range(5):
        client.get("/x")
    data = client.get("/_inspector/api/summary").json()
    assert data["sample_limit"]["reached"] is True
    assert data["overview"]["requests"] == 3


def test_empty_exports():
    client = TestClient(create_dashboard_app(MemoryStore(10)))
    assert client.get("/api/export?format=csv").text == ",".join(CSV_COLUMNS) + "\n"
    document = client.get("/api/export?format=json").json()
    assert document["measurements"] == []
    assert document["summary"]["overview"]["requests"] == 0


@pytest.mark.parametrize("query", ["window=-1", "window=abc", "format=xml", "window=99999999999"])
def test_invalid_parameters_rejected(query):
    client = TestClient(create_dashboard_app(MemoryStore(10)))
    assert client.get(f"/api/export?{query}").status_code == 422


def test_no_sensitive_data_exposed(mounted):
    client, _ = mounted
    client.post(
        "/login?password=hunter2",
        content=b'{"password": "hunter2"}',
        headers={"Authorization": "Bearer topsecret", "Cookie": "session=cookievalue"},
    )
    blob = json.dumps(client.get("/_inspector/api/summary").json())
    blob += client.get("/_inspector/api/export?format=json").text
    blob += client.get("/_inspector/api/export?format=csv").text
    assert "/login" in blob
    for secret in ("hunter2", "topsecret", "cookievalue", "password"):
        assert secret not in blob


def test_static_path_traversal_blocked():
    client = TestClient(create_dashboard_app(MemoryStore(10)))
    for path in ("/static/../dashboard.py", "/static/..%2fdashboard.py", "/static/%2e%2e/cli.py"):
        assert client.get(path).status_code == 404


def test_dashboard_not_mounted_by_default():
    app = FastAPI()
    PerformanceInspector().instrument(app)
    assert TestClient(app).get("/_inspector/").status_code == 404
