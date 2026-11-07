"""Demo FastAPI application instrumented with API Performance Inspector.

Endpoint behaviour is deterministic so the demo is reproducible: /reports/{id}
is slow for ids divisible by 4, and every 5th /flaky request fails.

Run:      python examples/basic_app.py
Traffic:  python examples/generate_traffic.py
Inspect:  api-performance-inspector dashboard     (reads ./api_metrics.db)
"""

import asyncio
import itertools

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from api_performance_inspector import PerformanceInspector

app = FastAPI(title="Demo API")

inspector = PerformanceInspector(
    storage="sqlite",
    database_path="api_metrics.db",
    slow_request_threshold_ms=250,
)
inspector.instrument(app)

USERS = {i: {"id": i, "name": f"user-{i}"} for i in range(1, 51)}
_flaky_calls = itertools.count(1)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/users/{user_id}")
async def get_user(user_id: int) -> dict[str, object]:
    if user_id not in USERS:
        raise HTTPException(status_code=404, detail="user not found")
    return USERS[user_id]


@app.post("/orders")
async def create_order(order: dict[str, object]) -> dict[str, object]:
    await asyncio.sleep(0.03)  # simulated database write
    return {"accepted": True, "items": len(order)}


@app.get("/reports/{report_id}")
async def build_report(report_id: int) -> dict[str, int]:
    await asyncio.sleep(0.4 if report_id % 4 == 0 else 0.06)
    return {"report": report_id}


@app.get("/flaky")
async def flaky() -> dict[str, bool]:
    if next(_flaky_calls) % 5 == 0:
        raise RuntimeError("simulated failure")
    return {"ok": True}


@app.get("/export")
async def stream_export() -> StreamingResponse:
    async def rows():  # type: ignore[no-untyped-def]
        for i in range(5):
            await asyncio.sleep(0.01)
            yield f"{i},value-{i}\n"

    return StreamingResponse(rows(), media_type="text/csv")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
