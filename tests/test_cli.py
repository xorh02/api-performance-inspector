import csv
import io
import json

import pytest

from api_performance_inspector import cli
from api_performance_inspector.metrics import Measurement
from api_performance_inspector.storage import SQLiteStore


@pytest.fixture
def db(tmp_path):
    import time

    path = tmp_path / "metrics.db"
    store = SQLiteStore(path, max_samples=1000, retention_seconds=86400, queue_size=1000)
    now = time.time()
    store.add(Measurement(now - 5, "GET", "/users/{user_id}", 200, 12.0, False, False))
    store.add(Measurement(now - 4, "GET", "/users/{user_id}", 500, 30.0, True, False))
    store.add(Measurement(now - 3, "POST", "/orders", 201, 700.0, False, True))
    store.close()
    return path


def test_summary(db, capsys):
    assert cli.main(["summary", "--db", str(db), "--window", "15m"]) == 0
    out = capsys.readouterr().out
    assert "Requests: 3" in out
    assert "/users/{user_id}" in out and "/orders" in out
    assert out.index("/orders") < out.index("/users/{user_id}")  # slowest first


def test_export_csv_to_stdout(db, capsys):
    assert cli.main(["export", "--db", str(db), "--format", "csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 3 and rows[2]["slow"] == "1"


def test_export_json_to_file(db, tmp_path):
    out = tmp_path / "out.json"
    assert cli.main(["export", "--db", str(db), "--output", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["summary"]["overview"]["errors"] == 1
    assert len(document["measurements"]) == 3


def test_export_to_unwritable_path(db, tmp_path, capsys):
    target = tmp_path / "no-such-dir" / "out.csv"
    assert cli.main(["export", "--db", str(db), "--format", "csv", "-o", str(target)]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_missing_database(tmp_path, capsys):
    assert cli.main(["summary", "--db", str(tmp_path / "nope.db")]) == 2
    assert "metrics database not found" in capsys.readouterr().err


def test_invalid_database(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database" * 100)
    assert cli.main(["summary", "--db", str(bad)]) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("window", ["abc", "0", "-5m", "1y"])
def test_invalid_window(db, window):
    with pytest.raises(SystemExit) as exc:
        cli.main(["summary", "--db", str(db), "--window", window])
    assert exc.value.code == 2


def test_directory_database(tmp_path, capsys):
    assert cli.main(["summary", "--db", str(tmp_path)]) == 2
    assert "is a directory" in capsys.readouterr().err


def test_max_samples_limits_reads(db, capsys):
    assert cli.main(["summary", "--db", str(db), "--max-samples", "2"]) == 0
    out = capsys.readouterr().out
    assert "Requests: 2" in out
    assert "only the newest 2 measurements" in out
    with pytest.raises(SystemExit):
        cli.main(["summary", "--db", str(db), "--max-samples", "0"])


def test_dashboard_port_in_use(db, monkeypatch, capsys):
    import socket

    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("should not start"))
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        assert cli.main(["dashboard", "--db", str(db), "--port", str(port)]) == 1
    captured = capsys.readouterr()
    assert "cannot listen on 127.0.0.1" in captured.err
    assert "http://" not in captured.out


def test_closed_pipe_exits_quietly(tmp_path):
    import subprocess
    import sys
    import time

    path = tmp_path / "big.db"
    store = SQLiteStore(path, max_samples=50_000, retention_seconds=86400, queue_size=50_000)
    now = time.time()
    for i in range(20_000):
        store.add(Measurement(now - i / 1000, "GET", "/a", 200, 1.0, False, False))
    store.close()
    proc = subprocess.Popen(
        [sys.executable, "-m", "api_performance_inspector.cli", "export", "--db", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdout.readline()
    proc.stdout.close()  # like `| head -1`
    stderr = proc.stderr.read().decode()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 1
    assert "Traceback" not in stderr, stderr


def test_window_parsing():
    assert cli._window("all") == 0
    assert cli._window("90") == 90
    assert cli._window("15m") == 900
    assert cli._window("2h") == 7200
    assert cli._window("7d") == 604800


def test_dashboard_binds_localhost_by_default(db, monkeypatch, capsys):
    calls = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: calls.update(kw))
    monkeypatch.setattr("socket.create_server", lambda addr: __import__("contextlib").nullcontext())
    assert cli.main(["dashboard", "--db", str(db)]) == 0
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8765
    captured = capsys.readouterr()
    assert "http://127.0.0.1:8765/" in captured.out
    assert "warning" not in captured.err


def test_dashboard_warns_on_public_bind(db, monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.setattr("socket.create_server", lambda addr: __import__("contextlib").nullcontext())
    cli.main(["dashboard", "--db", str(db), "--host", "0.0.0.0"])
    assert "exposes the unauthenticated dashboard" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "api-performance-inspector" in capsys.readouterr().out
