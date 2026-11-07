"""Command-line interface for reading a SQLite metrics database.

Exit codes: 0 success, 1 runtime failure, 2 usage error or unusable database.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .dashboard import create_dashboard_app, sample_limit
from .export import to_csv, to_json
from .metrics import summarize
from .storage import SQLiteReader

DEFAULT_DB = "api_metrics.db"


def _window(value: str) -> int:
    """Parse a window such as ``900``, ``15m``, ``1h``, ``7d`` or ``all`` into seconds."""
    text = value.strip().lower()
    if text == "all":
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    multiplier = units.get(text[-1:])
    number = text[:-1] if multiplier else text
    try:
        seconds = int(number) * (multiplier or 1)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid window {value!r}; use e.g. 300, 15m, 1h, 7d or all"
        ) from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("window must be positive (or 'all')")
    return seconds


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api-performance-inspector",
        description="Inspect FastAPI performance metrics stored in a SQLite database.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_db(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument(
            "--db",
            default=DEFAULT_DB,
            type=Path,
            help=f"path to the metrics database (default: ./{DEFAULT_DB})",
        )
        cmd.add_argument(
            "--max-samples",
            type=_positive_int,
            default=100_000,
            help="maximum (newest) measurements to read (default: 100000)",
        )

    dashboard = commands.add_parser("dashboard", help="serve the local dashboard")
    add_db(dashboard)
    dashboard.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    dashboard.add_argument("--port", type=int, default=8765, help="port (default: 8765)")

    summary = commands.add_parser("summary", help="print a summary table")
    add_db(summary)
    summary.add_argument("--window", type=_window, default=3600, help="e.g. 15m, 1h, 7d, all")

    export = commands.add_parser("export", help="export measurements as JSON or CSV")
    add_db(export)
    export.add_argument("--format", choices=("json", "csv"), default="json")
    export.add_argument("--window", type=_window, default=0, help="e.g. 15m, 1h, 7d, all")
    export.add_argument("--output", "-o", type=Path, help="output file (default: stdout)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reader = SQLiteReader(args.db, max_samples=args.max_samples)
    except FileNotFoundError:
        print(
            f"error: metrics database not found: {args.db}\n"
            'Create one with PerformanceInspector(storage="sqlite", database_path=...) '
            "and send some requests, or pass --db PATH.",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "dashboard":
            return _dashboard(reader, args.host, args.port)
        if args.command == "summary":
            _summary(reader, args.window)
            return 0
        return _export(reader, args.format, args.window, args.output)
    except BrokenPipeError:
        # Output was piped into a command that exited early, e.g. `| head`.
        # Point stdout at devnull so the interpreter's final flush does not fail too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def _dashboard(reader: SQLiteReader, host: str, port: int) -> int:
    import uvicorn

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback:
        print(
            f"warning: binding to {host} exposes the unauthenticated dashboard to the network",
            file=sys.stderr,
        )
    # Fail clearly before announcing a URL that will not work.
    try:
        with socket.create_server((host, port)):
            pass
    except OSError as exc:
        print(
            f"error: cannot listen on {host}:{port} ({exc.strerror or exc}). "
            "Is another process using that port? Choose one with --port.",
            file=sys.stderr,
        )
        return 1
    print(f"Dashboard for {reader.path} at http://{host}:{port}/  (Ctrl+C to stop)", flush=True)
    uvicorn.run(create_dashboard_app(reader), host=host, port=port, log_level="warning")
    return 0


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def _summary(reader: SQLiteReader, window: int) -> None:
    now = time.time()
    window_seconds = float(window) if window else None
    measurements = reader.query(now - window if window else None)
    data = summarize(measurements, now=now, window_seconds=window_seconds)
    o = data["overview"]
    label = "all retained" if not window else f"last {window}s"
    print(f"Window: {label} ({data['window']['start']} to {data['window']['end']})")
    print(
        f"Requests: {o['requests']}  Errors: {o['errors']} ({o['error_rate']:.2%})  "
        f"Slow: {o['slow_requests']}  Avg throughput: {o['requests_per_second']} req/s"
    )
    print(
        f"Latency ms  p50 {_fmt(o['p50_ms'])}  p95 {_fmt(o['p95_ms'])}  "
        f"p99 {_fmt(o['p99_ms'])}  mean {_fmt(o['mean_ms'])}  max {_fmt(o['max_ms'])}"
    )
    if sample_limit(reader, measurements)["reached"]:
        print(
            f"Note: only the newest {reader.max_samples} measurements were read; "
            "raise --max-samples to include older requests in this window."
        )
    if not data["endpoints"]:
        print("No requests recorded in this window.")
        return
    print()
    header = (
        f"{'METHOD':<7} {'ROUTE':<40} {'COUNT':>7} {'P50':>8} {'P95':>8} {'P99':>8} {'ERR%':>6}"
    )
    print(header)
    for e in sorted(data["endpoints"], key=lambda e: -(e["p95_ms"] or 0)):
        print(
            f"{e['method']:<7} {e['route'][:40]:<40} {e['requests']:>7} {_fmt(e['p50_ms']):>8} "
            f"{_fmt(e['p95_ms']):>8} {_fmt(e['p99_ms']):>8} {e['error_rate'] * 100:>5.1f}%"
        )


def _export(reader: SQLiteReader, fmt: str, window: int, output: Path | None) -> int:
    now = time.time()
    measurements = reader.query(now - window if window else None)
    if fmt == "csv":
        text = to_csv(measurements)
    else:
        text = to_json(measurements, now=now, window_seconds=float(window) if window else None)
    if output is None:
        sys.stdout.write(text)
        return 0
    try:
        output.write_text(text, encoding="utf-8", newline="")
    except OSError as exc:
        print(f"error: cannot write {output}: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {len(measurements)} measurements to {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
