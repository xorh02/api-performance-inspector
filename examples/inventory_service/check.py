"""End-to-end check of API Performance Inspector from a separate application.

1. Imports the package as installed (``--require-installed`` rejects a source checkout).
2. Serves app.py with uvicorn on a free local port and sends real HTTP requests.
3. Shuts the server down gracefully, which flushes metrics to SQLite.
4. Reads the results back with the installed ``api-performance-inspector`` CLI.

Usage: python check.py [--require-installed] [--keep]
Exits non-zero if any expectation fails.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def request(base: str, method: str, path: str, body: dict[str, object] | None = None) -> int:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def find_cli() -> str:
    name = "api-performance-inspector"
    found = shutil.which(name) or shutil.which(name, path=sysconfig.get_path("scripts"))
    if not found:
        sys.exit(f"FAIL: the {name} command is not installed in this environment")
    return found


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--require-installed", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the metrics database")
    args = parser.parse_args()

    import api_performance_inspector

    location = Path(api_performance_inspector.__file__).resolve()
    print(f"api_performance_inspector {api_performance_inspector.__version__} from {location}")
    if args.require_installed:
        check("site-packages" in location.parts, "package is imported from an installation")

    workdir = Path(tempfile.mkdtemp(prefix="inventory-check-"))
    os.chdir(workdir)  # app.py writes inventory_metrics.db to the working directory
    sys.path.insert(0, str(HERE))
    import uvicorn
    from app import app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            sys.exit("FAIL: server did not start")
        time.sleep(0.05)

    base = f"http://127.0.0.1:{port}"
    statuses = []
    statuses += [request(base, "GET", "/items/A-1?token=do-not-store") for _ in range(20)]
    statuses += [request(base, "GET", f"/items/Z-{i}") for i in range(5)]
    statuses += [request(base, "GET", "/search?q=bolt") for _ in range(5)]
    statuses += [request(base, "PUT", "/items/B-7", {"sku": "B-7"}) for _ in range(3)]
    statuses += [request(base, "GET", "/healthz") for _ in range(10)]
    print(f"sent {len(statuses)} requests")

    server.should_exit = True  # graceful shutdown runs the lifespan, which flushes metrics
    thread.join(15)
    check(not thread.is_alive(), "server shut down gracefully")

    cli = find_cli()
    db = workdir / "inventory_metrics.db"
    summary = subprocess.run(
        [cli, "summary", "--db", str(db)], capture_output=True, text=True, check=True
    )
    print("\n$ api-performance-inspector summary --db inventory_metrics.db")
    print(summary.stdout)
    exported = subprocess.run(
        [cli, "export", "--db", str(db), "--format", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    document = json.loads(exported)
    endpoints = {(e["method"], e["route"]): e for e in document["summary"]["endpoints"]}

    check(statuses.count(200) == 35 and statuses.count(404) == 5, "responses were unaffected")
    check(statuses.count(422) == 3, "validation errors were returned unchanged")
    check(len(document["measurements"]) == 33, "33 requests recorded (/healthz excluded)")
    check(endpoints[("GET", "/items/{sku}")]["requests"] == 25, "path parameters grouped")
    check(
        endpoints[("GET", "/items/{sku}")]["status_classes"] == {"2xx": 20, "4xx": 5},
        "404s recorded under their route",
    )
    check(endpoints[("PUT", "/items/{sku}")]["status_classes"] == {"4xx": 3}, "422s recorded")
    check(endpoints[("GET", "/search")]["slow_requests"] == 5, "slow /search requests flagged")
    check(endpoints[("GET", "/items/{sku}")]["slow_requests"] == 0, "fast requests not flagged")
    check("do-not-store" not in exported, "query string values were not stored")

    os.chdir(HERE)
    if args.keep:
        print(f"metrics database kept at {db}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    print("all checks passed")


if __name__ == "__main__":
    main()
