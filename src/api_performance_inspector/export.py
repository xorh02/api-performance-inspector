"""JSON and CSV export of retained measurements.

CSV columns: timestamp_utc (ISO 8601), timestamp_unix (seconds), method, route,
status, duration_ms, error (0/1), slow (0/1). One row per request.
JSON: ``{"schema_version", "summary": <summarize() output>, "measurements": [...]}``
with the same per-request fields as the CSV.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .metrics import SCHEMA_VERSION, Measurement, summarize

CSV_COLUMNS = (
    "timestamp_utc",
    "timestamp_unix",
    "method",
    "route",
    "status",
    "duration_ms",
    "error",
    "slow",
)


def _row(m: Measurement) -> dict[str, Any]:
    return {
        "timestamp_utc": datetime.fromtimestamp(m.timestamp, UTC).isoformat(
            timespec="microseconds"
        ),
        "timestamp_unix": round(m.timestamp, 6),
        "method": m.method,
        "route": m.route,
        "status": m.status,
        "duration_ms": round(m.duration_ms, 3),
        "error": int(m.error),
        "slow": int(m.slow),
    }


def to_csv(measurements: Sequence[Measurement]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_row(m) for m in measurements)
    return buffer.getvalue()


def to_json(
    measurements: Sequence[Measurement], *, now: float, window_seconds: float | None
) -> str:
    document = {
        "schema_version": SCHEMA_VERSION,
        "summary": summarize(measurements, now=now, window_seconds=window_seconds),
        "measurements": [_row(m) for m in measurements],
    }
    return json.dumps(document, indent=2)
