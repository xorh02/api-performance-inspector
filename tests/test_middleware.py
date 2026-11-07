import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from api_performance_inspector import ConfigError, PerformanceInspector
from api_performance_inspector import middleware as middleware_module


def make_app(**inspector_options):
    app = FastAPI()
    inspector = PerformanceInspector(**inspector_options)

    @app.get("/users/{user_id}")
    async def get_user(user_id: int):
        if user_id == 0:
            raise HTTPException(404, "missing")
        return {"id": user_id}

    @app.get("/bad")
    async def bad():
        raise HTTPException(400, "bad request")

    @app.get("/unavailable")
    async def unavailable():
        raise HTTPException(503, "down")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.post("/echo")
    async def echo(request: Request):
        return {"body": (await request.body()).decode()}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text(await websocket.receive_text())
        await websocket.close()

    inspector.instrument(app)
    return app, inspector


def recorded(inspector):
    return inspector._store.query()


def test_successful_request_is_measured_once():
    app, inspector = make_app()
    response = TestClient(app).get("/users/42")
    assert response.json() == {"id": 42}
    [m] = recorded(inspector)
    assert (m.method, m.route, m.status, m.error, m.slow) == (
        "GET",
        "/users/{user_id}",
        200,
        False,
        False,
    )
    assert m.duration_ms > 0


def test_dynamic_parameters_share_one_route():
    app, inspector = make_app()
    client = TestClient(app)
    for user_id in (1, 2, 3):
        client.get(f"/users/{user_id}")
    assert {m.route for m in recorded(inspector)} == {"/users/{user_id}"}


@pytest.mark.parametrize(
    ("path", "status", "route", "error"),
    [
        ("/bad", 400, "/bad", False),
        ("/users/0", 404, "/users/{user_id}", False),
        ("/users/not-an-int", 422, "/users/{user_id}", False),
        ("/unavailable", 503, "/unavailable", True),
        ("/nope/123", 404, "<unmatched>", False),
    ],
)
def test_status_codes_and_errors(path, status, route, error):
    app, inspector = make_app()
    assert TestClient(app).get(path).status_code == status
    [m] = recorded(inspector)
    assert (m.status, m.route, m.error) == (status, route, error)


def test_method_not_allowed_keeps_route():
    app, inspector = make_app()
    assert TestClient(app).delete("/health").status_code == 405
    assert recorded(inspector)[0].route == "/health"


def test_unhandled_exception_is_recorded_and_reraised():
    app, inspector = make_app()
    with pytest.raises(RuntimeError, match="boom"):
        TestClient(app).get("/boom")
    [m] = recorded(inspector)
    assert (m.route, m.status, m.error) == ("/boom", 500, True)

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert len(recorded(inspector)) == 2


def test_query_string_is_not_stored():
    app, inspector = make_app()
    TestClient(app).get("/users/7?token=supersecret")
    assert "supersecret" not in repr(recorded(inspector))


def test_request_and_response_bodies_untouched():
    app, inspector = make_app()
    payload = "x" * 100_000
    response = TestClient(app).post("/echo", content=payload)
    assert response.json() == {"body": payload}
    assert int(response.headers["content-length"]) == len(response.content)
    assert len(recorded(inspector)) == 1


def test_excluded_paths_are_not_measured():
    app, inspector = make_app(exclude_paths=["/health"])
    client = TestClient(app)
    client.get("/health")
    client.get("/users/1")
    assert [m.route for m in recorded(inspector)] == ["/users/{user_id}"]


def test_websocket_is_not_measured():
    app, inspector = make_app()
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"
    assert recorded(inspector) == []


def test_unknown_methods_are_grouped():
    app, inspector = make_app()
    TestClient(app).request("FOOBAR", "/health")
    assert recorded(inspector)[0].method == "OTHER"


def test_concurrent_requests_each_measured_once():
    app, inspector = make_app()

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.gather(*(client.get(f"/users/{i}") for i in range(1, 101)))

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    assert len(recorded(inspector)) == 100


def test_streaming_response_timed_to_final_chunk(monkeypatch):
    clock = {"ns": 0}
    monkeypatch.setattr(middleware_module, "perf_counter_ns", lambda: clock["ns"])
    app = FastAPI()
    inspector = PerformanceInspector()

    @app.get("/stream")
    async def stream():
        async def body():
            yield b"first,"
            clock["ns"] += 250_000_000  # 250 ms pass while streaming
            yield b"last"

        return StreamingResponse(body())

    inspector.instrument(app)
    assert TestClient(app).get("/stream").content == b"first,last"
    [m] = recorded(inspector)
    assert m.duration_ms == 250.0
    assert m.slow is False  # default threshold is 500 ms


def test_slow_threshold(monkeypatch):
    ticks = iter([0, 600_000_000])
    monkeypatch.setattr(middleware_module, "perf_counter_ns", lambda: next(ticks))
    app, inspector = make_app(slow_request_threshold_ms=500)
    TestClient(app).get("/health")
    [m] = recorded(inspector)
    assert m.duration_ms == 600.0 and m.slow is True


def test_mounted_apps():
    app = FastAPI()
    inspector = PerformanceInspector()
    sub = FastAPI()

    @sub.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    app.mount("/v2", sub)
    app.mount("/files", StaticFiles(directory=Path(__file__).parent))
    inspector.instrument(app)
    client = TestClient(app)
    assert client.get("/v2/items/5").status_code == 200
    assert client.get("/files/test_middleware.py").status_code == 200
    assert [m.route for m in recorded(inspector)] == ["/v2/items/{item_id}", "/files/{path}"]


def test_route_cardinality_is_capped():
    app, inspector = make_app(max_routes=2)
    client = TestClient(app)
    for path in ("/health", "/bad", "/users/1", "/unavailable"):
        client.get(path)
    assert [m.route for m in recorded(inspector)] == ["/health", "/bad", "<other>", "<other>"]
    assert inspector.diagnostics()["routes_overflowed"] == 2


def test_storage_failure_does_not_break_requests():
    app, inspector = make_app()

    def broken_add(_):
        raise OSError("disk on fire")

    inspector._store.add = broken_add
    assert TestClient(app).get("/health").status_code == 200
    diagnostics = inspector.diagnostics()
    assert diagnostics["internal_errors"] == 1
    assert "disk on fire" in diagnostics["last_internal_error"]


def test_double_instrumentation_rejected():
    app, inspector = make_app()
    with pytest.raises(ConfigError, match="already instrumented"):
        inspector.instrument(app)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"storage": "redis"}, "storage"),
        ({"slow_request_threshold_ms": 0}, "slow_request_threshold_ms"),
        ({"slow_request_threshold_ms": float("nan")}, "slow_request_threshold_ms"),
        ({"max_samples": 0}, "max_samples"),
        ({"max_samples": 1.5}, "max_samples"),
        ({"retention_seconds": -1}, "retention_seconds"),
        ({"queue_size": True}, "queue_size"),
        ({"max_routes": 0}, "max_routes"),
        ({"exclude_paths": ["health"]}, "exclude_paths"),
        ({"exclude_paths": "/health"}, "exclude_paths"),
    ],
)
def test_invalid_configuration(options, message):
    with pytest.raises(ConfigError, match=message):
        PerformanceInspector(**options)


@pytest.mark.parametrize("path", ["inspector", "/", ""])
def test_invalid_dashboard_path(path):
    with pytest.raises(ConfigError, match="dashboard_path"):
        PerformanceInspector().instrument(FastAPI(), dashboard_path=path)
