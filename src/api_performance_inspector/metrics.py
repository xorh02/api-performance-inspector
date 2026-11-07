"""Measurement record and aggregation.

All latencies are milliseconds, all timestamps are Unix epoch seconds (UTC).
Statistics are computed over the measurements passed in, which is always a
bounded, retained window rather than lifetime data.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

SCHEMA_VERSION = 1

# Candidate time-series bucket sizes in seconds; the smallest one giving at most
# MAX_BUCKETS buckets for the selected window is used.
_BUCKET_SIZES = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 86400)
MAX_BUCKETS = 60


class Measurement(NamedTuple):
    """One completed HTTP request. A plain tuple keeps per-sample memory small."""

    timestamp: float  # request start, Unix seconds
    method: str
    route: str  # normalised route template, never the raw URL
    status: int
    duration_ms: float
    error: bool  # 5xx status or unhandled exception
    slow: bool  # duration exceeded the threshold configured when recorded


def percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Percentile by linear interpolation between closest ranks.

    This is Hyndman & Fan definition 7, the default of NumPy's ``percentile`` and
    of ``statistics.quantiles(method="inclusive")``: the value at fractional rank
    ``(n - 1) * q`` of the sorted samples. Returns ``None`` for no samples.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be between 0 and 1, got {q}")
    n = len(sorted_values)
    if n == 0:
        return None
    rank = (n - 1) * q
    lower = math.floor(rank)
    upper = min(lower + 1, n - 1)
    return sorted_values[lower] + (rank - lower) * (sorted_values[upper] - sorted_values[lower])


def latency_stats(durations: Iterable[float]) -> dict[str, float | None]:
    values = sorted(durations)
    if not values:
        return dict.fromkeys(("mean_ms", "min_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"))
    return {
        "mean_ms": _round(math.fsum(values) / len(values)),
        "min_ms": _round(values[0]),
        "max_ms": _round(values[-1]),
        "p50_ms": _round(percentile(values, 0.50)),
        "p95_ms": _round(percentile(values, 0.95)),
        "p99_ms": _round(percentile(values, 0.99)),
    }


def status_class(status: int) -> str:
    return f"{status // 100}xx"


def choose_bucket_seconds(span_seconds: float) -> int:
    for size in _BUCKET_SIZES:
        if span_seconds / size <= MAX_BUCKETS:
            return size
    return _BUCKET_SIZES[-1]


def summarize(
    measurements: Sequence[Measurement],
    *,
    now: float,
    window_seconds: float | None,
) -> dict[str, Any]:
    """Aggregate measurements that fall inside the window ending at ``now``.

    ``window_seconds=None`` means all retained measurements; the window then
    starts at the oldest retained sample. Requests per second is the request
    count divided by the window length.
    """
    if window_seconds is not None:
        start = now - window_seconds
        samples = [m for m in measurements if start <= m.timestamp <= now]
    else:
        samples = [m for m in measurements if m.timestamp <= now]
        start = min((m.timestamp for m in samples), default=now)
    span = max(now - start, 1.0)

    total = len(samples)
    errors = sum(1 for m in samples if m.error)
    overview: dict[str, Any] = {
        "requests": total,
        "errors": errors,
        "error_rate": _rate(errors, total),
        "slow_requests": sum(1 for m in samples if m.slow),
        "requests_per_second": _round(total / span),
        **latency_stats(m.duration_ms for m in samples),
    }

    status_classes: dict[str, int] = defaultdict(int)
    methods: dict[str, int] = defaultdict(int)
    by_endpoint: dict[tuple[str, str], list[Measurement]] = defaultdict(list)
    for m in samples:
        status_classes[status_class(m.status)] += 1
        methods[m.method] += 1
        by_endpoint[(m.method, m.route)].append(m)

    endpoints: list[dict[str, Any]] = []
    for (method, route), group in by_endpoint.items():
        group_errors = sum(1 for m in group if m.error)
        group_classes: dict[str, int] = defaultdict(int)
        for m in group:
            group_classes[status_class(m.status)] += 1
        endpoints.append(
            {
                "method": method,
                "route": route,
                "requests": len(group),
                "errors": group_errors,
                "error_rate": _rate(group_errors, len(group)),
                "slow_requests": sum(1 for m in group if m.slow),
                **latency_stats(m.duration_ms for m in group),
                "status_classes": dict(sorted(group_classes.items())),
            }
        )
    endpoints.sort(key=lambda e: (-e["requests"], e["route"], e["method"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "window": {
            "seconds": window_seconds,
            "start": _iso(start),
            "end": _iso(now),
        },
        "units": {"latency": "milliseconds", "throughput": "requests per second"},
        "overview": overview,
        "status_classes": dict(sorted(status_classes.items())),
        "methods": dict(sorted(methods.items())),
        "endpoints": endpoints,
        "timeseries": _timeseries(samples, start=start, now=now, span=span),
    }


def _timeseries(
    samples: Sequence[Measurement], *, start: float, now: float, span: float
) -> dict[str, Any]:
    size = choose_bucket_seconds(span)
    first = math.floor(start / size) * size
    last = math.floor(now / size) * size
    buckets: dict[int, list[Measurement]] = defaultdict(list)
    for m in samples:
        buckets[math.floor(m.timestamp / size) * size].append(m)

    points = []
    for bucket_start in range(int(first), int(last) + 1, size):
        group = buckets.get(bucket_start, [])
        durations = sorted(m.duration_ms for m in group)
        points.append(
            {
                "start_unix": bucket_start,
                "requests": len(group),
                "requests_per_second": _round(len(group) / size),
                "errors": sum(1 for m in group if m.error),
                "p50_ms": _round(percentile(durations, 0.50)),
                "p95_ms": _round(percentile(durations, 0.95)),
            }
        )
    return {"bucket_seconds": size, "points": points}


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 6) if whole else 0.0


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds")
