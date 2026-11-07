"""The local dashboard: a small FastAPI app serving static assets and a JSON API.

Endpoints are plain ``def`` functions so FastAPI runs them in a worker thread and
SQLite reads never block the event loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .export import to_csv, to_json
from .metrics import summarize
from .storage import MeasurementSource

STATIC_DIR = Path(__file__).parent / "static"
MAX_WINDOW_SECONDS = 366 * 24 * 3600

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# 0 selects all retained measurements.
WindowParam = Annotated[int, Query(ge=0, le=MAX_WINDOW_SECONDS)]


def sample_limit(source: MeasurementSource, measurements: list[Any]) -> dict[str, Any]:
    """Whether the result hit the read cap, i.e. older requests in the window are missing."""
    return {
        "max_samples": source.max_samples,
        "reached": len(measurements) >= source.max_samples,
    }


def create_dashboard_app(
    source: MeasurementSource,
    *,
    diagnostics: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="API Performance Inspector", docs_url=None, redoc_url=None, openapi_url=None
    )

    def window_data(window: int) -> tuple[list[Any], float, float | None]:
        now = time.time()
        window_seconds = float(window) if window else None
        since = now - window if window else None
        return source.query(since), now, window_seconds

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=_SECURITY_HEADERS)

    @app.get("/api/summary")
    def summary(window: WindowParam = 3600) -> JSONResponse:
        measurements, now, window_seconds = window_data(window)
        payload = summarize(measurements, now=now, window_seconds=window_seconds)
        payload["sample_limit"] = sample_limit(source, measurements)
        payload["collector"] = diagnostics() if diagnostics else None
        return JSONResponse(payload, headers=_SECURITY_HEADERS)

    @app.get("/api/export")
    def export(
        format: Annotated[Literal["json", "csv"], Query()] = "json",
        window: WindowParam = 0,
    ) -> Response:
        measurements, now, window_seconds = window_data(window)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        if format == "csv":
            body, media_type = to_csv(measurements), "text/csv; charset=utf-8"
        else:
            body = to_json(measurements, now=now, window_seconds=window_seconds)
            media_type = "application/json"
        headers = {
            **_SECURITY_HEADERS,
            "Content-Disposition": f'attachment; filename="api-metrics-{stamp}.{format}"',
        }
        return Response(body, media_type=media_type, headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
