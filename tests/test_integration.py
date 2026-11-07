"""End-to-end: a real FastAPI app with SQLite persistence, then the dashboard and CLI."""

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api_performance_inspector import PerformanceInspector, cli
from api_performance_inspector.dashboard import create_dashboard_app
from api_performance_inspector.storage import SQLiteReader

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def build_app(db_path):
    app = FastAPI()
    inspector = PerformanceInspector(storage="sqlite", database_path=db_path)

    @app.get("/orders/{order_id}")
    async def order(order_id: int):
        if order_id > 100:
            raise HTTPException(404)
        return {"id": order_id}

    @app.get("/crash")
    async def crash():
        raise ValueError("crash")

    inspector.instrument(app)
    return app


def test_sqlite_persistence_dashboard_and_export(tmp_path, capsys):
    db = tmp_path / "api_metrics.db"

    # First application run; leaving the TestClient context runs lifespan shutdown,
    # which flushes the background writer.
    with TestClient(build_app(db), raise_server_exceptions=False) as client:
        for i in range(10):
            assert client.get(f"/orders/{i}").status_code == 200
        assert client.get("/orders/500").status_code == 404
        assert client.get("/crash").status_code == 500
    assert len(SQLiteReader(db).query()) == 12

    # Second run against the same database: data survives the restart.
    with TestClient(build_app(db)) as client:
        client.get("/orders/1")
    assert len(SQLiteReader(db).query()) == 13

    dashboard = TestClient(create_dashboard_app(SQLiteReader(db)))
    data = dashboard.get("/api/summary").json()
    assert data["overview"]["requests"] == 13
    assert data["overview"]["errors"] == 1
    by_route = {e["route"]: e for e in data["endpoints"]}
    assert by_route["/orders/{order_id}"]["requests"] == 12
    assert by_route["/orders/{order_id}"]["status_classes"] == {"2xx": 11, "4xx": 1}
    assert by_route["/crash"]["error_rate"] == 1.0
    assert data["collector"] is None

    assert cli.main(["export", "--db", str(db), "--format", "json"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert len(exported["measurements"]) == 13


def test_external_consumer_example():
    """examples/inventory_service is a separate app; its check script must pass."""
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "inventory_service" / "check.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_example_app_over_real_http(tmp_path):
    """Run the shipped example under uvicorn and send real HTTP requests to it."""
    port = _free_port()
    code = (
        f"import sys, uvicorn; sys.path.insert(0, {str(EXAMPLES)!r}); "
        f"uvicorn.run('basic_app:app', host='127.0.0.1', port={port}, log_level='warning')"
    )
    server = subprocess.Popen([sys.executable, "-c", code], cwd=tmp_path)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1).read()
                break
            except (urllib.error.URLError, ConnectionError):
                assert time.monotonic() < deadline, "example app did not start"
                time.sleep(0.1)
        for user_id in (1, 2, 999):
            try:
                urllib.request.urlopen(f"{base}/users/{user_id}", timeout=5).read()
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
        urllib.request.urlopen(f"{base}/export", timeout=5).read()

        # The background writer flushes about every 0.5 s; wait for it.
        # Read while the server runs: after a hard kill (terminate() on Windows) the
        # OS can briefly hold SQLite's file locks.
        db = tmp_path / "api_metrics.db"
        deadline = time.monotonic() + 10
        while len(rows := SQLiteReader(db).query()) < 5:
            assert time.monotonic() < deadline, "measurements were not persisted"
            time.sleep(0.1)
    finally:
        server.terminate()
        server.wait(10)

    routes = [m.route for m in rows]
    assert routes.count("/users/{user_id}") == 3
    assert "/export" in routes and "/health" in routes
