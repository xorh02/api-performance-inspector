from __future__ import annotations

import atexit
import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .config import ConfigError, InspectorConfig, StorageMode
from .metrics import Measurement, summarize
from .middleware import InspectorMiddleware
from .storage import MemoryStore, SQLiteStore, Store

logger = logging.getLogger("api_performance_inspector")

OTHER_ROUTE = "<other>"
_KNOWN_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


class PerformanceInspector:
    """Collects request timings from a FastAPI application.

    Create one inspector per application and call :meth:`instrument` before the
    application starts serving requests.
    """

    def __init__(
        self,
        *,
        storage: StorageMode = "memory",
        database_path: str | Path = "api_metrics.db",
        slow_request_threshold_ms: float = 500.0,
        max_samples: int = 100_000,
        retention_seconds: float = 7 * 24 * 3600,
        exclude_paths: Iterable[str] = (),
        max_routes: int = 500,
        queue_size: int = 10_000,
    ) -> None:
        if isinstance(exclude_paths, str):
            raise ConfigError("exclude_paths must be a list of paths, not a single string")
        self.config = InspectorConfig(
            storage=storage,
            database_path=Path(database_path),
            slow_request_threshold_ms=slow_request_threshold_ms,
            max_samples=max_samples,
            retention_seconds=retention_seconds,
            exclude_paths=tuple(exclude_paths),
            max_routes=max_routes,
            queue_size=queue_size,
        )
        self._store: Store
        if self.config.storage == "sqlite":
            self._store = SQLiteStore(
                self.config.database_path,
                max_samples=self.config.max_samples,
                retention_seconds=self.config.retention_seconds,
                queue_size=self.config.queue_size,
            )
        else:
            self._store = MemoryStore(self.config.max_samples)
        self._excluded: list[str] = [p.rstrip("/") or "/" for p in self.config.exclude_paths]
        self._routes: set[str] = set()
        self._lock = threading.Lock()
        self._recorded = 0
        self._routes_overflowed = 0
        self._internal_errors = 0
        self._last_internal_error: str | None = None
        atexit.register(self.close)

    def instrument(self, app: FastAPI, *, dashboard_path: str | None = None) -> None:
        """Add the timing middleware to ``app``.

        ``dashboard_path`` (for example ``"/_inspector"``) additionally mounts the
        dashboard inside the application. It is served to anyone who can reach the
        application, so only enable it where that is acceptable.
        """
        if getattr(app.state, "api_performance_inspector", None) is not None:
            raise ConfigError("this application is already instrumented")
        if dashboard_path is not None:
            if not dashboard_path.startswith("/") or dashboard_path.rstrip("/") == "":
                raise ConfigError(
                    "dashboard_path must start with '/' and not be the root, "
                    f"got {dashboard_path!r}"
                )
            from .dashboard import create_dashboard_app

            dashboard_path = dashboard_path.rstrip("/")
            app.mount(
                dashboard_path,
                create_dashboard_app(self._store, diagnostics=self.diagnostics),
            )
            self._excluded.append(dashboard_path)
        app.add_middleware(InspectorMiddleware, inspector=self)
        app.state.api_performance_inspector = self

    def summary(self, window_seconds: float | None = None) -> dict[str, Any]:
        """Aggregate statistics for the last ``window_seconds`` (``None``: all retained)."""
        now = time.time()
        since = None if window_seconds is None else now - window_seconds
        return summarize(self._store.query(since), now=now, window_seconds=window_seconds)

    def diagnostics(self) -> dict[str, Any]:
        """Collector health: recorded/dropped counts and the last storage error."""
        with self._lock:
            info = {
                "recorded": self._recorded,
                "routes_tracked": len(self._routes),
                "routes_overflowed": self._routes_overflowed,
                "internal_errors": self._internal_errors,
                "last_internal_error": self._last_internal_error,
            }
        info.update(self._store.stats())
        return info

    def start(self) -> None:
        self._store.start()

    def close(self, timeout: float = 5.0) -> None:
        """Flush pending measurements (SQLite) and stop the background writer."""
        self._store.close(timeout)

    def _is_excluded(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") or p == "/" for p in self._excluded)

    def _record(
        self,
        *,
        started_at: float,
        method: str,
        route: str,
        status: int,
        duration_ms: float,
        failed: bool,
    ) -> None:
        # Instrumentation must never break a request, so all failures are contained.
        try:
            with self._lock:
                if route not in self._routes:
                    if len(self._routes) >= self.config.max_routes:
                        self._routes_overflowed += 1
                        route = OTHER_ROUTE
                    else:
                        self._routes.add(route)
                self._recorded += 1
            self._store.add(
                Measurement(
                    timestamp=started_at,
                    # Arbitrary client-supplied methods would be unbounded labels.
                    method=method if method in _KNOWN_METHODS else "OTHER",
                    route=route,
                    status=status,
                    duration_ms=duration_ms,
                    error=failed or status >= 500,
                    slow=duration_ms > self.config.slow_request_threshold_ms,
                )
            )
        except Exception as exc:
            with self._lock:
                self._internal_errors += 1
                self._last_internal_error = f"{type(exc).__name__}: {exc}"
            logger.exception("failed to record measurement")
