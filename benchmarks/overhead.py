"""Measure the per-request overhead of API Performance Inspector.

The same FastAPI app is driven directly through the ASGI interface (no network,
no HTTP client) in three configurations: uninstrumented, in-memory storage and
SQLite storage. This isolates the middleware/collector cost from network noise,
so the absolute numbers are lower than real request latencies and the overhead
figure is the added time per request inside the process.

Usage: python benchmarks/overhead.py [--requests 20000] [--concurrency 50] [--repeats 5]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI

from api_performance_inspector import PerformanceInspector


def build_app(mode: str, tmp: Path) -> tuple[FastAPI, PerformanceInspector | None]:
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    inspector = None
    if mode == "memory":
        inspector = PerformanceInspector(storage="memory")
    elif mode == "sqlite":
        inspector = PerformanceInspector(
            storage="sqlite", database_path=tmp / "bench.db", queue_size=100_000
        )
    if inspector:
        inspector.instrument(app)
    return app, inspector


async def call(app: FastAPI, item_id: int) -> None:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/items/{item_id}",
        "raw_path": f"/items/{item_id}".encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"bench")],
        "client": ("127.0.0.1", 1),
        "server": ("bench", 80),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        pass

    await app(scope, receive, send)


async def run_load(app: FastAPI, requests: int, concurrency: int) -> float:
    """Return wall-clock seconds to complete ``requests`` with ``concurrency`` workers."""
    per_worker = requests // concurrency

    async def worker(offset: int) -> None:
        for i in range(per_worker):
            await call(app, offset + i)

    start = time.perf_counter()
    await asyncio.gather(*(worker(w * per_worker) for w in range(concurrency)))
    return time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=2_000)
    args = parser.parse_args()
    total = (args.requests // args.concurrency) * args.concurrency

    print(f"Python {sys.version.split()[0]} ({platform.python_implementation()})")
    print(f"OS: {platform.platform()}")
    print(f"CPU: {platform.processor() or platform.machine()} ({os.cpu_count()} logical cores)")
    print(
        f"Workload: {total} requests x {args.repeats} repeats, concurrency {args.concurrency}, "
        f"warm-up {args.warmup} requests; in-process ASGI calls\n"
    )

    results: dict[str, list[float]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        apps = {mode: build_app(mode, Path(tmp)) for mode in ("baseline", "memory", "sqlite")}
        for app, _ in apps.values():
            asyncio.run(run_load(app, args.warmup, args.concurrency))
        # Interleave configurations so drift (thermal, background load) affects all equally.
        for _ in range(args.repeats):
            for mode, (app, _) in apps.items():
                seconds = asyncio.run(run_load(app, total, args.concurrency))
                results.setdefault(mode, []).append(seconds / total * 1e6)
        for _, inspector in apps.values():
            if inspector:
                inspector.close()

    base = statistics.median(results["baseline"])
    # us/req is wall time divided by requests completed (inverse throughput).
    print(f"{'config':<10} {'median us/req':>14} {'min':>8} {'max':>8} ", end="")
    print(f"{'req/s':>10} {'overhead':>18}")
    for mode, samples in results.items():
        med = statistics.median(samples)
        overhead = "" if mode == "baseline" else f"+{med - base:.1f} us ({(med / base - 1):+.1%})"
        print(
            f"{mode:<10} {med:>14.1f} {min(samples):>8.1f} {max(samples):>8.1f} "
            f"{1e6 / med:>10,.0f} {overhead:>18}"
        )


if __name__ == "__main__":
    main()
