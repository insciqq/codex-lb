"""Starting the in-process metrics server must not reconfigure process logging.

The server runs as a subprocess because log configuration is applied at
process start and cannot be observed through an in-process ASGI client.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ok(url: str, proc: subprocess.Popen[bytes], output_path: Path, deadline: float) -> None:
    while True:
        assert proc.poll() is None, f"server exited early: {output_path.read_text()}"
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{url} did not answer"
        time.sleep(0.25)


def _run_server(tmp_path: Path, extra_env: dict[str, str]) -> str:
    """Start the server, request /health after the metrics server is up, stop it; return its output."""

    port = _free_port()
    env = {
        **os.environ,
        "CODEX_LB_DATA_DIR": str(tmp_path / "data"),
        "CODEX_LB_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'codex-lb.db'}",
        "CODEX_LB_METRICS_ENABLED": "false",
        **extra_env,
    }
    # A file, not a pipe: nobody drains a pipe while the server runs.
    output_path = tmp_path / "server-output.txt"
    with output_path.open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 90
        _wait_ok(f"http://127.0.0.1:{port}/health", proc, output_path, deadline)
        if env["CODEX_LB_METRICS_ENABLED"] == "true":
            # Probe only after the metrics server has started, so any logging
            # reset it performs has already happened.
            _wait_ok(f"http://127.0.0.1:{env['CODEX_LB_METRICS_PORT']}/metrics", proc, output_path, deadline)
        httpx.get(f"http://127.0.0.1:{port}/health?probe=access-line", timeout=5)
    finally:
        proc.terminate()
        proc.wait(timeout=60)
    return output_path.read_text()


def test_metrics_server_does_not_silence_access_logs(tmp_path: Path) -> None:
    pytest.importorskip("prometheus_client")  # optional ``metrics`` extra
    metrics_env = {"CODEX_LB_METRICS_ENABLED": "true", "CODEX_LB_METRICS_PORT": str(_free_port())}

    output = _run_server(tmp_path, metrics_env)

    assert "GET /health?probe=access-line" in output


def test_json_access_line_has_client_request_and_status(tmp_path: Path) -> None:
    output = _run_server(tmp_path, {"CODEX_LB_LOG_FORMAT": "json"})

    entries = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    access = next(e for e in entries if e.get("type") == "access" and "probe=" in str(e["request"]))
    assert access["client"]
    assert access["request"] == "GET /health?probe=access-line HTTP/1.1"
    assert access["status"] == 200
