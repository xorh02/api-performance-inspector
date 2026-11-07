import sqlite3
import threading
import time

import pytest

from api_performance_inspector import ConfigError
from api_performance_inspector.metrics import Measurement
from api_performance_inspector.storage import MemoryStore, SQLiteReader, SQLiteStore

# Recent base time, so retention cleanup in the writer keeps these rows.
T0 = time.time() - 60


def m(ts=0.0, route="/a", ms=1.0):
    """``ts`` is an offset in seconds from T0."""
    return Measurement(T0 + ts, "GET", route, 200, ms, False, False)


def offsets(rows):
    return [round(x.timestamp - T0, 3) for x in rows]


def sqlite_store(path, **options):
    defaults = {"max_samples": 1000, "retention_seconds": 3600, "queue_size": 1000}
    return SQLiteStore(path, **{**defaults, **options})


class TestMemoryStore:
    def test_bounded_retention_keeps_newest(self):
        store = MemoryStore(max_samples=3)
        for i in range(5):
            store.add(m(ts=float(i)))
        assert offsets(store.query()) == [2.0, 3.0, 4.0]
        assert store.stats()["retained_samples"] == 3

    def test_query_since(self):
        store = MemoryStore(max_samples=10)
        for i in range(5):
            store.add(m(ts=float(i)))
        assert offsets(store.query(since=T0 + 3)) == [3.0, 4.0]

    def test_concurrent_adds(self):
        store = MemoryStore(max_samples=100_000)
        threads = [
            threading.Thread(target=lambda: [store.add(m()) for _ in range(1000)]) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(store.query()) == 8000


class TestSQLiteStore:
    def test_persists_across_instances(self, tmp_path):
        db = tmp_path / "metrics.db"
        store = sqlite_store(db)
        store.add(m(ts=1.0, route="/x", ms=12.5))
        store.close()
        [row] = SQLiteReader(db).query()
        assert row == m(ts=1.0, route="/x", ms=12.5)
        reopened = sqlite_store(db)
        reopened.add(m(ts=2.0))
        reopened.close()
        assert len(SQLiteReader(db).query()) == 2

    def test_close_flushes_pending_measurements(self, tmp_path):
        store = sqlite_store(tmp_path / "m.db", queue_size=10_000)
        for i in range(5000):
            store.add(m(ts=float(i)))
        store.close()
        assert store.stats()["written"] == 5000
        assert len(store.query()) == 1000  # reads are capped at max_samples
        assert offsets(store.query())[-1] == 4999.0

    def test_concurrent_writers(self, tmp_path):
        store = sqlite_store(tmp_path / "m.db", max_samples=100_000, queue_size=100_000)
        threads = [
            threading.Thread(target=lambda: [store.add(m()) for _ in range(500)]) for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        store.close()
        assert len(store.query()) == 4000
        assert store.stats()["dropped"] == 0

    def test_queue_overflow_drops_and_counts(self, tmp_path):
        store = sqlite_store(tmp_path / "m.db", queue_size=5)
        store.close()  # stop the writer so the queue cannot drain
        for i in range(8):
            store.add(m(ts=float(i)))
        assert store.stats()["dropped"] == 3
        store.start()
        store.close()
        assert offsets(store.query()) == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_retention_cleanup(self, tmp_path):
        store = sqlite_store(
            tmp_path / "m.db", retention_seconds=100, max_samples=3, clock=lambda: T0 + 1000
        )
        for ts in (0.0, 950.0, 960.0, 970.0, 980.0):
            store.add(m(ts=ts))
        store.close()
        store.cleanup()
        assert offsets(store.query()) == [960.0, 970.0, 980.0]

    def test_database_failure_is_counted_not_raised(self, tmp_path, caplog):
        db = tmp_path / "m.db"
        store = sqlite_store(db)
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE measurements")
        conn.close()
        store.add(m())
        store.close()
        stats = store.stats()
        assert stats["failed_writes"] == 1
        assert "no such table" in stats["last_error"]
        assert "failed to write" in caplog.text

    def test_missing_directory_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="does not exist"):
            sqlite_store(tmp_path / "missing" / "m.db")

    def test_directory_path_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="is a directory"):
            sqlite_store(tmp_path)

    def test_corrupt_file_rejected(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"this is not sqlite" * 100)
        with pytest.raises(ConfigError, match="cannot open"):
            sqlite_store(bad)


class TestSQLiteReader:
    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SQLiteReader(tmp_path / "nope.db")

    def test_not_a_database(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"garbage" * 200)
        with pytest.raises(ValueError, match="not a readable SQLite database"):
            SQLiteReader(bad)

    def test_foreign_database(self, tmp_path):
        other = tmp_path / "other.db"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE users (id INTEGER)")
        conn.close()
        with pytest.raises(ValueError, match="not an API Performance Inspector database"):
            SQLiteReader(other)

    def test_query_since(self, tmp_path):
        store = sqlite_store(tmp_path / "m.db")
        for ts in (1.0, 2.0, 3.0):
            store.add(m(ts=ts))
        store.close()
        assert offsets(SQLiteReader(tmp_path / "m.db").query(since=T0 + 2)) == [2.0, 3.0]
