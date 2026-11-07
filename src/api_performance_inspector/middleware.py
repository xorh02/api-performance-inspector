"""Pure ASGI middleware that times HTTP requests.

Measurement boundary: from when the middleware receives the request until the
final ``http.response.body`` message (``more_body`` false) has been passed to the
server, so streamed responses are timed to completion. Request and response
bodies pass through untouched; only the status code is read.
"""

from __future__ import annotations

import time
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from .inspector import PerformanceInspector

UNMATCHED_ROUTE = "<unmatched>"
UNRESOLVED_ROUTE = "<unresolved>"


class InspectorMiddleware:
    def __init__(self, app: ASGIApp, inspector: PerformanceInspector) -> None:
        self.app = app
        self.inspector = inspector

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, self._lifespan_send(send))
            return
        # WebSockets and excluded paths are passed through without measurement.
        if scope["type"] != "http" or self.inspector._is_excluded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        started_at = time.time()
        start = perf_counter_ns()
        root_path = scope.get("root_path", "")
        status: int | None = None
        end: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status, end
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                end = perf_counter_ns()

        failed = False
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            failed = True
            raise
        finally:
            finished = end if end is not None else perf_counter_ns()
            self.inspector._record(
                started_at=started_at,
                method=scope.get("method", ""),
                route=_route_template(scope, root_path, status),
                # An exception before the response started becomes a 500 upstream.
                status=status if status is not None else 500,
                duration_ms=(finished - start) / 1_000_000,
                failed=failed,
            )

    def _lifespan_send(self, send: Send) -> Send:
        async def wrapper(message: Message) -> None:
            if message["type"] == "lifespan.startup.complete":
                self.inspector.start()
            elif message["type"] == "lifespan.shutdown.complete":
                await anyio.to_thread.run_sync(self.inspector.close)
            await send(message)

        return wrapper


def _route_template(scope: Scope, root_path: str, status: int | None) -> str:
    """Return the matched route template, e.g. ``/users/{user_id}``.

    FastAPI stores the matched ``APIRoute`` in ``scope["route"]``. Mounted apps
    extend ``root_path``, so the mount prefix is recovered from the difference.
    """
    route: Any = scope.get("route")
    mount_prefix: str = scope.get("root_path", "")[len(root_path) :]
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str):
        return mount_prefix + path_format
    if mount_prefix:
        # Mounted non-FastAPI app (for example StaticFiles): group by mount point.
        return mount_prefix + "/{path}"
    return UNMATCHED_ROUTE if status in (None, 404) else UNRESOLVED_ROUTE
