"""JSON access lines of the real server carry client, request and status.

The server runs as a subprocess because log configuration is applied at
process start. It binds port 0 and the test reads the bound port from the
server's own startup line, so parallel runs cannot collide on a port.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

_BOUND_PORT = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


def _wait_for_port(proc: subprocess.Popen[bytes], output_path: Path, deadline: float) -> int:
    while True:
        assert proc.poll() is None, f"server exited early: {output_path.read_text()}"
        match = _BOUND_PORT.search(output_path.read_text())
        if match is not None:
            return int(match.group(1))
        assert time.monotonic() < deadline, "server did not report its port"
        time.sleep(0.25)


def test_json_access_line_has_client_request_and_status(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "CODEX_LB_DATA_DIR": str(tmp_path / "data"),
        "CODEX_LB_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'codex-lb.db'}",
        "CODEX_LB_METRICS_ENABLED": "false",
        "CODEX_LB_LOG_FORMAT": "json",
    }
    # A file, not a pipe: nobody drains a pipe while the server runs.
    output_path = tmp_path / "server-output.txt"
    with output_path.open("w") as output:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.cli", "--host", "127.0.0.1", "--port", "0"],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 90
        port = _wait_for_port(proc, output_path, deadline)
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "server did not become healthy"
            time.sleep(0.25)
        httpx.get(f"http://127.0.0.1:{port}/health?probe=access-line", timeout=5)
    finally:
        proc.terminate()
        proc.wait(timeout=60)

    entries = [json.loads(line) for line in output_path.read_text().splitlines() if line.startswith("{")]
    access = next(e for e in entries if e.get("type") == "access" and "probe=" in str(e["request"]))
    assert access["client"]
    assert access["request"] == "GET /health?probe=access-line HTTP/1.1"
    assert access["status"] == 200
