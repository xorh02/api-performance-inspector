"""Measurement storage backends.

``add`` is called on the event loop for every request, so it must never block
or raise. Reads (``query``) may block and are called from worker threads.
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from .config import ConfigError
from .metrics import Measurement

logger = logging.getLogger("api_performance_inspector")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    timestamp REAL NOT NULL,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    status INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    error INTEGER NOT NULL,
    slow INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_timestamp ON measurements (timestamp);
"""


class MeasurementSource(Protocol):
    # Upper bound on what query() returns; reaching it means older data was cut off.
    max_samples: int

    def query(self, since: float | None = None) -> list[Measurement]: ...


class Store(MeasurementSource, Protocol):
    def add(self, measurement: Measurement) -> None: ...
    def stats(self) -> dict[str, Any]: ...
    def start(self) -> None: ...
    def close(self, timeout: float = 5.0) -> None: ...


class MemoryStore:
    """Keeps the most recent ``max_samples`` measurements; older ones are evicted."""

    def __init__(self, max_samples: int) -> None:
        self.max_samples = max_samples
        self._samples: deque[Measurement] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def add(self, measurement: Measurement) -> None:
        with self._lock:
            self._samples.append(measurement)

    def query(self, since: float | None = None) -> list[Measurement]:
        with self._lock:
            samples = list(self._samples)
        if since is None:
            return samples
        return [m for m in samples if m.timestamp >= since]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            retained = len(self._samples)
        return {"storage": "memory", "retained_samples": retained}

    def start(self) -> None:
        pass

    def close(self, timeout: float = 5.0) -> None:
        pass


class SQLiteReader:
    """Read-only access to an existing metrics database (used by the CLI)."""

    def __init__(self, path: str | Path, max_samples: int = 100_000) -> None:
        self.path = Path(path).expanduser()
        self.max_samples = max_samples
        if self.path.is_dir():
            raise ValueError(f"{self.path} is a directory, expected a metrics database file")
        if not self.path.is_file():
            raise FileNotFoundError(f"metrics database not found: {self.path}")
        try:
            conn = self._connect()
            try:
                found = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'measurements'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"{self.path} is not a readable SQLite database: {exc}") from exc
        if not found:
            raise ValueError(f"{self.path} is not an API Performance Inspector database")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def query(self, since: float | None = None) -> list[Measurement]:
        # The newest max_samples rows at most, so reads stay memory-bounded even
        # if retention cleanup has not yet run.
        sql = (
            "SELECT timestamp, method, route, status, duration_ms, error, slow FROM measurements"
            " WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?"
        )
        conn = self._connect()
        try:
            rows = conn.execute(sql, (since if since is not None else 0.0, self.max_samples))
            result = [
                Measurement(ts, method, route, status, duration, bool(error), bool(slow))
                for ts, method, route, status, duration, error, slow in rows
            ]
        finally:
            conn.close()
        result.reverse()
        return result


class SQLiteStore(SQLiteReader):
    """Persists measurements through a bounded queue and one background writer thread.

    When the queue is full, new measurements are dropped and counted rather than
    blocking the request. ``close`` flushes pending rows within a timeout.
    """

    FLUSH_INTERVAL = 0.5
    BATCH_SIZE = 1000
    CLEANUP_INTERVAL = 60.0

    def __init__(
        self,
        path: str | Path,
        *,
        max_samples: int,
        retention_seconds: float,
        queue_size: int,
        clock: Any = time.time,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_samples = max_samples
        self.retention_seconds = retention_seconds
        self._clock = clock
        if self.path.is_dir():
            raise ConfigError(f"database_path {self.path} is a directory, expected a file path")
        if not self.path.parent.is_dir():
            raise ConfigError(
                f"database_path directory {self.path.parent} does not exist; create it first"
            )
        try:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise ConfigError(f"cannot open SQLite database {self.path}: {exc}") from exc

        self._queue: queue.Queue[Measurement] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counter_lock = threading.Lock()
        self._dropped = 0
        self._failed_writes = 0
        self._written = 0
        self._last_error: str | None = None
        self.start()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="api-performance-inspector-writer", daemon=True
        )
        self._thread.start()

    def add(self, measurement: Measurement) -> None:
        try:
            self._queue.put_nowait(measurement)
        except queue.Full:
            with self._counter_lock:
                self._dropped += 1

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            logger.warning(
                "SQLite writer did not finish within %.1fs; %d measurements not flushed",
                timeout,
                self._queue.qsize(),
            )

    def stats(self) -> dict[str, Any]:
        with self._counter_lock:
            return {
                "storage": "sqlite",
                "database_path": str(self.path),
                "written": self._written,
                "dropped": self._dropped,
                "failed_writes": self._failed_writes,
                "queue_depth": self._queue.qsize(),
                "last_error": self._last_error,
            }

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        last_cleanup = 0.0
        while True:
            batch = self._drain()
            try:
                if conn is None:
                    conn = self._connect()
                if batch:
                    self._write(conn, batch)
                if time.monotonic() - last_cleanup >= self.CLEANUP_INTERVAL:
                    self.cleanup(conn)
                    last_cleanup = time.monotonic()
            except Exception as exc:
                # Never let a database problem kill the writer; count and keep going.
                with self._counter_lock:
                    self._failed_writes += len(batch)
                    self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("failed to write %d measurements: %s", len(batch), exc)
                if conn is not None:
                    conn.close()
                    conn = None
            if self._stop.is_set() and self._queue.empty():
                break
        if conn is not None:
            conn.close()

    def _drain(self) -> list[Measurement]:
        try:
            batch = [self._queue.get(timeout=self.FLUSH_INTERVAL)]
        except queue.Empty:
            return []
        while len(batch) < self.BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, conn: sqlite3.Connection, batch: list[Measurement]) -> None:
        with conn:
            conn.executemany(
                "INSERT INTO measurements"
                " (timestamp, method, route, status, duration_ms, error, slow)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
        with self._counter_lock:
            self._written += len(batch)

    def cleanup(self, conn: sqlite3.Connection | None = None) -> None:
        """Delete rows older than the retention period or beyond ``max_samples``."""
        own = conn is None
        conn = conn or self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM measurements WHERE timestamp < ?",
                    (self._clock() - self.retention_seconds,),
                )
                conn.execute(
                    "DELETE FROM measurements WHERE id <= ("
                    "SELECT id FROM measurements ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (self.max_samples,),
                )
        finally:
            if own:
                conn.close()
