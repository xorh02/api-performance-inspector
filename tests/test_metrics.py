import random
import statistics

import pytest

from api_performance_inspector.metrics import (
    MAX_BUCKETS,
    Measurement,
    choose_bucket_seconds,
    latency_stats,
    percentile,
    summarize,
)

NOW = 1_700_000_000.0


def m(ts=NOW - 10, method="GET", route="/a", status=200, ms=10.0, slow=False):
    return Measurement(ts, method, route, status, ms, status >= 500, slow)


class TestPercentile:
    def test_empty_returns_none(self):
        assert percentile([], 0.5) is None

    def test_single_sample(self):
        assert percentile([7.0], 0.0) == percentile([7.0], 0.99) == 7.0

    def test_identical_values(self):
        assert percentile([3.0] * 10, 0.95) == 3.0

    def test_linear_interpolation(self):
        # rank = (4 - 1) * 0.5 = 1.5 -> halfway between 20 and 30
        assert percentile([10.0, 20.0, 30.0, 40.0], 0.5) == 25.0
        assert percentile([10.0, 20.0, 30.0, 40.0], 0.0) == 10.0
        assert percentile([10.0, 20.0, 30.0, 40.0], 1.0) == 40.0

    @pytest.mark.parametrize("n", [2, 3, 7, 100, 10_001])
    def test_matches_statistics_inclusive_method(self, n):
        rng = random.Random(n)
        values = sorted(rng.expovariate(1 / 50) for _ in range(n))
        cuts = statistics.quantiles(values, n=100, method="inclusive")
        for q, expected in ((0.50, cuts[49]), (0.95, cuts[94]), (0.99, cuts[98])):
            assert percentile(values, q) == pytest.approx(expected)

    def test_outlier_moves_tail_not_median(self):
        values = sorted([10.0] * 99 + [10_000.0])
        assert percentile(values, 0.5) == 10.0
        assert percentile(values, 1.0) == 10_000.0
        assert percentile(values, 0.99) == pytest.approx(10.0 + 0.01 * 9_990.0)

    @pytest.mark.parametrize("q", [-0.1, 1.1])
    def test_rejects_out_of_range_q(self, q):
        with pytest.raises(ValueError):
            percentile([1.0], q)


def test_latency_stats():
    stats = latency_stats([4.0, 1.0, 3.0, 2.0])
    assert stats == {
        "mean_ms": 2.5,
        "min_ms": 1.0,
        "max_ms": 4.0,
        "p50_ms": 2.5,
        "p95_ms": 3.85,
        "p99_ms": 3.97,
    }
    assert set(latency_stats([]).values()) == {None}


def test_bucket_size_bounds_bucket_count():
    for span in (1, 59, 300, 3600, 86400, 7 * 86400):
        size = choose_bucket_seconds(span)
        assert span / size <= MAX_BUCKETS
    assert choose_bucket_seconds(60) == 1
    assert choose_bucket_seconds(3600) == 60


class TestSummarize:
    def test_empty(self):
        s = summarize([], now=NOW, window_seconds=300)
        assert s["overview"]["requests"] == 0
        assert s["overview"]["error_rate"] == 0.0
        assert s["overview"]["p95_ms"] is None
        assert s["endpoints"] == []
        assert all(p["requests"] == 0 for p in s["timeseries"]["points"])

    def test_counts_rates_and_breakdowns(self):
        data = [
            m(route="/a", ms=10),
            m(route="/a", ms=20),
            m(route="/a", ms=30, status=500),
            m(route="/b", method="POST", ms=600, slow=True),
            m(route="/b", method="POST", ms=5, status=404),
        ]
        s = summarize(data, now=NOW, window_seconds=100)
        o = s["overview"]
        assert o["requests"] == 5
        assert o["errors"] == 1
        assert o["error_rate"] == 0.2
        assert o["slow_requests"] == 1
        assert o["mean_ms"] == pytest.approx(133.0)
        assert o["min_ms"] == 5 and o["max_ms"] == 600
        assert o["requests_per_second"] == 0.05
        assert s["status_classes"] == {"2xx": 3, "4xx": 1, "5xx": 1}
        assert s["methods"] == {"GET": 3, "POST": 2}

        a, b = s["endpoints"]
        assert (a["method"], a["route"], a["requests"], a["errors"]) == ("GET", "/a", 3, 1)
        assert a["error_rate"] == pytest.approx(1 / 3, abs=1e-6)
        assert a["p50_ms"] == 20
        assert (b["route"], b["slow_requests"], b["status_classes"]) == (
            "/b",
            1,
            {"2xx": 1, "4xx": 1},
        )

    def test_window_excludes_old_and_future_samples(self):
        data = [m(ts=NOW - 400), m(ts=NOW - 100), m(ts=NOW - 1), m(ts=NOW + 5)]
        assert summarize(data, now=NOW, window_seconds=300)["overview"]["requests"] == 2

    def test_all_retained_window_starts_at_oldest_sample(self):
        data = [m(ts=NOW - 1000), m(ts=NOW - 10)]
        s = summarize(data, now=NOW, window_seconds=None)
        assert s["overview"]["requests"] == 2
        assert s["overview"]["requests_per_second"] == 0.002
        assert s["window"]["seconds"] is None

    def test_timeseries_buckets(self):
        data = [m(ts=NOW - 30, ms=1), m(ts=NOW - 30, ms=3), m(ts=NOW - 5, status=503)]
        ts = summarize(data, now=NOW, window_seconds=60)["timeseries"]
        assert ts["bucket_seconds"] == 1
        assert sum(p["requests"] for p in ts["points"]) == 3
        assert sum(p["errors"] for p in ts["points"]) == 1
        busy = next(p for p in ts["points"] if p["requests"] == 2)
        assert busy["p50_ms"] == 2.0
        assert busy["requests_per_second"] == 2.0
        empty = [p for p in ts["points"] if p["requests"] == 0]
        assert empty and all(p["p50_ms"] is None for p in empty)
        starts = [p["start_unix"] for p in ts["points"]]
        assert starts == sorted(starts) and len(starts) <= MAX_BUCKETS + 1

    def test_large_sample_set(self):
        rng = random.Random(1)
        data = [m(ts=NOW - rng.uniform(0, 3600), ms=rng.uniform(1, 100)) for _ in range(50_000)]
        s = summarize(data, now=NOW, window_seconds=3600)
        assert s["overview"]["requests"] == 50_000
        assert 45 < s["overview"]["p50_ms"] < 55
        assert 94 < s["overview"]["p95_ms"] < 96
