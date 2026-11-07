"""Send a reproducible mix of requests to the demo app (standard library only).

Usage: python examples/generate_traffic.py [--requests 1000] [--seed 0]

Also prints client-observed latency, which includes the HTTP round trip and is
therefore higher than the server-side latency the inspector records.
"""

import argparse
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def build_requests(base_url: str, count: int, seed: int) -> list[urllib.request.Request]:
    rng = random.Random(seed)
    requests = []
    for _ in range(count):
        choice = rng.random()
        if choice < 0.35:
            req = urllib.request.Request(f"{base_url}/users/{rng.randint(1, 60)}")
        elif choice < 0.5:
            body = json.dumps({"sku": "A-1", "qty": rng.randint(1, 5)}).encode()
            req = urllib.request.Request(
                f"{base_url}/orders", data=body, headers={"Content-Type": "application/json"}
            )
        elif choice < 0.65:
            req = urllib.request.Request(f"{base_url}/reports/{rng.randint(1, 12)}")
        elif choice < 0.8:
            req = urllib.request.Request(f"{base_url}/flaky")
        elif choice < 0.9:
            req = urllib.request.Request(f"{base_url}/export?format=csv")
        elif choice < 0.97:
            req = urllib.request.Request(f"{base_url}/health")
        else:
            req = urllib.request.Request(f"{base_url}/does-not-exist")
        requests.append(req)
    return requests


def send(req: urllib.request.Request) -> tuple[int, float]:
    """Return (status, client-observed milliseconds); status 0 means no connection."""
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError):
        status = 0
    return status, (time.perf_counter() - start) * 1000


def main() -> None:
    parser = argparse.ArgumentParser(description="Send demo traffic to examples/basic_app.py")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0, help="seed for the request mix")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    with ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(send, build_requests(args.base_url, args.requests, args.seed)))

    counts: dict[int, int] = {}
    for status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    failed = counts.pop(0, 0)
    if failed == len(results):
        raise SystemExit(f"Cannot reach {args.base_url}. Is the demo app running?")
    print(f"Sent {len(results)} requests; status codes: {dict(sorted(counts.items()))}")
    if failed:
        print(f"{failed} requests failed to connect")
    latencies = [ms for status, ms in results if status]
    if len(latencies) >= 2:
        cuts = statistics.quantiles(latencies, n=100, method="inclusive")
        print(
            f"Client-observed latency ms: p50 {cuts[49]:.1f}  p95 {cuts[94]:.1f}  "
            f"p99 {cuts[98]:.1f}"
        )


if __name__ == "__main__":
    main()
