from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_gateway_entry_runs_hidden_filesystem_worker_without_entering_cli(
    tmp_path: Path,
) -> None:
    gateway_entry = (
        Path(__file__).resolve().parents[2]
        / "desktop"
        / "electron"
        / "scripts"
        / "gateway-entry.py"
    )
    target = tmp_path / "worker-probe.txt"
    target.write_text("worker-ok\n", encoding="utf-8")
    payload = {
        "kind": "read_file",
        "path": str(target),
        "displayPath": str(target),
    }

    completed = subprocess.run(
        [sys.executable, str(gateway_entry), "--_sandbox-filesystem-worker"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"message": "1\tworker-ok\n"}
    assert completed.stderr == ""
