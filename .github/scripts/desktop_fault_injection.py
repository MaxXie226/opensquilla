#!/usr/bin/env python3
"""Drive a packaged Desktop build through injected failure environments.

The release gate this replaces only proved the packaged process stayed alive for
eight seconds and that ``recovery inspect`` reported ``ready``.  That is why the
0.5.0 profile-consolidation regression shipped: nothing asserted the user could
actually reach the control surface, and nothing built a hostile profile first.

Each scenario constructs an Electron ``userData`` tree, launches the packaged
application against it, and classifies the outcome as ``entered`` (the gateway
answered, so the user got into the product) or ``blocked`` (startup stopped on
the primary-repair page).  Scenarios then assert the consolidation side effects
that the maintainer's requirements depend on: every recovery profile consumed,
primary configuration authoritative, the container archived rather than deleted.

Usage:
    desktop_fault_injection.py list
    desktop_fault_injection.py run --scenario NAME --app /path/OpenSquilla.app \\
        --workdir DIR [--port 18931] [--timeout 180] [--report out.json]
    desktop_fault_injection.py run --scenario NAME --workdir DIR --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A launch that neither answers nor records a terminal event inside this window
# is reported as ``timeout`` rather than silently passing.
DEFAULT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 1.0

_MINIMAL_SESSION_SCHEMA = """
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    label TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL
);
"""

# Terminal desktop.log events. Reaching either means the launch has decided.
_BLOCKED_EVENTS = frozenset({"desktop_open_failed"})
_CONSOLIDATION_EVENT = "desktop_profile_consolidation_completed"


def _seed_sessions_db(path: Path, *, session_key: str, session_id: str, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_MINIMAL_SESSION_SCHEMA)
        connection.execute(
            "INSERT INTO sessions(session_key, session_id, updated_at, label) VALUES (?, ?, 1, ?)",
            (session_key, session_id, label),
        )
        connection.execute(
            "INSERT INTO transcript_entries("
            "session_id, session_key, message_id, role, content, created_at"
            ") VALUES (?, ?, ?, 'user', ?, 1)",
            (session_id, session_key, f"message-{session_id}", label),
        )
        connection.commit()
    finally:
        connection.close()


def _session_labels(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT label FROM sessions").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


def _write_primary_context(user_data: Path) -> None:
    (user_data / "desktop-profile-context.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_profile_kind": "primary",
                "active_recovery_id": None,
                "attention_acknowledgement": None,
                "updated_at": "2026-07-13T00:00:00.000Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@dataclass
class Fixture:
    """The userData tree a scenario launches against."""

    user_data: Path
    recovery_ids: list[str] = field(default_factory=list)
    expected_session_labels: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def _primary(user_data: Path, *, config: str | None, session_label: str | None) -> Path:
    home = user_data / "opensquilla"
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / "config.toml").write_text(config, encoding="utf-8")
    if session_label is not None:
        _seed_sessions_db(
            home / "state" / "sessions.db",
            session_key="agent:main:main",
            session_id="primary-session",
            label=session_label,
        )
    _write_primary_context(user_data)
    return home


def _recovery(user_data: Path, recovery_id: str, *, config: str, session_label: str) -> Path:
    root = user_data / "recovery-profiles" / recovery_id
    home = root / "opensquilla"
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(config, encoding="utf-8")
    (home / ".env").write_text(f"OPENSQUILLA_SOURCE_MARKER={recovery_id}\n", encoding="utf-8")
    (root / "desktop-credential.json").write_text("{}\n", encoding="utf-8")
    (home / "workspace" / "MEMORY.md").write_text(f"memory {recovery_id}\n", encoding="utf-8")
    _seed_sessions_db(
        home / "state" / "sessions.db",
        # A distinct key per source avoids conflating this harness's coverage
        # with the separate same-key collision path.
        session_key=f"agent:{recovery_id[:8]}:main",
        session_id=f"session-{recovery_id}",
        label=session_label,
    )
    return root


# ── Scenario builders ──────────────────────────────────────────────────────


def _fresh_install(user_data: Path) -> Fixture:
    """The common case: no legacy container at all. Must reach the product."""

    _primary(user_data, config="", session_label=None)
    return Fixture(user_data, notes=["no recovery-profiles container exists"])


def _single_recovery(user_data: Path) -> Fixture:
    _primary(user_data, config="primary = true\n", session_label="primary chat")
    recovery_id = str(uuid.uuid4())
    _recovery(user_data, recovery_id, config="selected = 'recovery'\n", session_label="recovery A")
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
    )


def _multi_recovery(user_data: Path) -> Fixture:
    """R1: every recovery profile is consolidated, with no chooser."""

    _primary(user_data, config="primary = true\n", session_label="primary chat")
    ids = []
    for index in range(3):
        recovery_id = str(uuid.uuid4())
        ids.append(recovery_id)
        _recovery(
            user_data,
            recovery_id,
            config=f"selected = 'recovery-{index}'\n",
            session_label=f"recovery {index}",
        )
    return Fixture(
        user_data,
        recovery_ids=ids,
        expected_session_labels={"primary chat", "recovery 0", "recovery 1", "recovery 2"},
    )


def _empty_primary_config(user_data: Path) -> Fixture:
    """R2: an empty primary adopts configuration from the newest recovery."""

    _primary(user_data, config="", session_label=None)
    older = str(uuid.uuid4())
    _recovery(user_data, older, config="adopted = 'older'\n", session_label="older recovery")
    time.sleep(1.1)
    newer = str(uuid.uuid4())
    _recovery(user_data, newer, config="adopted = 'newer'\n", session_label="newer recovery")
    return Fixture(
        user_data,
        recovery_ids=[older, newer],
        expected_session_labels={"older recovery", "newer recovery"},
        notes=[f"newest recovery is {newer}"],
    )


def _corrupt_primary_config(user_data: Path) -> Fixture:
    """R2: a corrupt-but-present primary configuration stays authoritative."""

    _primary(user_data, config="this is = = not valid toml [\n", session_label="primary chat")
    recovery_id = str(uuid.uuid4())
    _recovery(user_data, recovery_id, config="adopted = 'recovery'\n", session_label="recovery A")
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
    )


def _stray_shell_metadata(user_data: Path) -> Fixture:
    """Inert shell/antivirus files must never strand startup."""

    _primary(user_data, config="primary = true\n", session_label="primary chat")
    recovery_id = str(uuid.uuid4())
    _recovery(user_data, recovery_id, config="selected = 'recovery'\n", session_label="recovery A")
    container = user_data / "recovery-profiles"
    (container / ".DS_Store").write_bytes(b"finder metadata")
    (container / ".localized").write_bytes(b"")
    (container / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    (container / "Thumbs.db").write_bytes(b"thumbs")
    (container / "sessions.db.avquarantine").write_bytes(b"quarantine sidecar")
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        expected_session_labels={"primary chat", "recovery A"},
        notes=["five inert stray files in the container"],
    )


def _stray_directory(user_data: Path) -> Fixture:
    """A profile-shaped directory is a deliberate fail-closed boundary."""

    _primary(user_data, config="primary = true\n", session_label="primary chat")
    recovery_id = str(uuid.uuid4())
    _recovery(user_data, recovery_id, config="selected = 'recovery'\n", session_label="recovery A")
    (user_data / "recovery-profiles" / f"{uuid.uuid4()} - Copy").mkdir()
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        notes=["a manual '- Copy' directory must block rather than be archived blindly"],
    )


def _only_stray_files(user_data: Path) -> Fixture:
    """A container holding no real profile is a noop, not a blocked startup."""

    _primary(user_data, config="primary = true\n", session_label="primary chat")
    container = user_data / "recovery-profiles"
    container.mkdir(parents=True)
    (container / "desktop.ini").write_bytes(b"[.ShellClassInfo]\n")
    (container / "Thumbs.db").write_bytes(b"thumbs")
    return Fixture(user_data, expected_session_labels={"primary chat"})


def _readonly_recovery_source(user_data: Path) -> Fixture:
    """A recovery profile the process cannot read must not lose primary access."""

    _primary(user_data, config="primary = true\n", session_label="primary chat")
    recovery_id = str(uuid.uuid4())
    root = _recovery(
        user_data, recovery_id, config="selected = 'recovery'\n", session_label="recovery A"
    )
    os.chmod(root / "opensquilla" / "state" / "sessions.db", 0o000)
    return Fixture(
        user_data,
        recovery_ids=[recovery_id],
        notes=["recovery sessions.db is mode 000; primary itself is healthy"],
    )


SCENARIOS: dict[str, dict[str, Any]] = {
    "fresh-install": {
        "build": _fresh_install,
        "expect": "entered",
        "why": "A new 0.5.x install has no legacy container; consolidation must be a fast noop.",
    },
    "single-recovery": {
        "build": _single_recovery,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "The baseline upgrade path: one legacy profile folded into primary.",
    },
    "multi-recovery": {
        "build": _multi_recovery,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "R1: several recovery profiles are all consolidated with no chooser.",
    },
    "empty-primary-config": {
        "build": _empty_primary_config,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "assert_config_adopted": True,
        "why": "R2: an empty primary adopts configuration from the newest recovery only.",
    },
    "corrupt-primary-config": {
        "build": _corrupt_primary_config,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "R2: corrupt-but-present primary configuration is never clobbered.",
    },
    "stray-shell-metadata": {
        "build": _stray_shell_metadata,
        "expect": "entered",
        "consume_all": True,
        "assert_sessions": True,
        "why": "Explorer and antivirus write into a folder the app created; that cannot brick it.",
    },
    "stray-directory": {
        "build": _stray_directory,
        "expect": "blocked",
        "why": "Deliberate boundary: an unknown directory blocks up front, not mid-archival.",
    },
    "only-stray-files": {
        "build": _only_stray_files,
        "expect": "entered",
        "assert_sessions": True,
        "why": "A container with no real profile must be a noop.",
    },
    "readonly-recovery-source": {
        "build": _readonly_recovery_source,
        "expect": "any",
        "why": (
            "An unreadable legacy source should not cost the user their healthy primary. "
            "Recorded rather than asserted until per-source fault isolation lands."
        ),
    },
}


# ── Launch and classification ──────────────────────────────────────────────


def _app_binary(app: Path) -> Path:
    if app.suffix == ".app":
        candidate = app / "Contents" / "MacOS" / "OpenSquilla"
        if candidate.is_file():
            return candidate
        raise SystemExit(f"no launchable binary inside {app}")
    if app.is_file():
        return app
    raise SystemExit(f"unrecognized application path: {app}")


def _read_events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    events = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _gateway_answered(port: int) -> bool:
    for path in ("/healthz", "/health"):
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
                f"http://127.0.0.1:{port}{path}", timeout=2
            ) as response:
                if 200 <= response.status < 500:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return False


def _launch(
    binary: Path,
    user_data: Path,
    *,
    port: int,
    isolated_home: Path,
) -> subprocess.Popen[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        # Keep the X11 handles the Linux virtual display needs; drop ambient
        # OpenSquilla configuration so the fixture is the only input.
        if key in {"DISPLAY", "XAUTHORITY"} or not key.startswith("OPENSQUILLA_")
    }
    environment.update(
        {
            "HOME": str(isolated_home),
            "OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE": "1",
            "OPENSQUILLA_DESKTOP_GATEWAY_PORT": str(port),
            "OPENSQUILLA_DESKTOP_SECRET_STORAGE": "plain",
            "OPENSQUILLA_USER_STATE_DIR": str(isolated_home / "user-state"),
        }
    )
    log_handle = (user_data.parent / "launch-stdio.log").open("ab")
    return subprocess.Popen(
        [
            str(binary),
            "--use-mock-keychain",
            f"--user-data-dir={user_data}",
        ],
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=20)


def _classify(
    process: subprocess.Popen[bytes],
    log_path: Path,
    *,
    port: int,
    timeout: float,
    kill_after: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``entered`` | ``blocked`` | ``timeout`` | ``killed`` plus log events."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if kill_after is not None and time.monotonic() >= kill_after:
            _terminate(process)
            return "killed", _read_events(log_path)
        if _gateway_answered(port):
            return "entered", _read_events(log_path)
        events = _read_events(log_path)
        if any(event.get("event") in _BLOCKED_EVENTS for event in events):
            return "blocked", events
        if process.poll() is not None and not _gateway_answered(port):
            # The process exited without ever answering.
            return "blocked", events
        time.sleep(POLL_INTERVAL_SECONDS)
    return "timeout", _read_events(log_path)


def _assert_side_effects(
    scenario: dict[str, Any],
    fixture: Fixture,
    events: list[dict[str, Any]],
) -> list[str]:
    """Return human-readable failures; empty means the scenario held."""

    failures: list[str] = []
    user_data = fixture.user_data
    primary_home = user_data / "opensquilla"
    container = user_data / "recovery-profiles"

    consolidation = [event for event in events if event.get("event") == _CONSOLIDATION_EVENT]

    if scenario.get("consume_all"):
        if not consolidation:
            failures.append(f"no {_CONSOLIDATION_EVENT} event was recorded")
        else:
            consumed = consolidation[-1].get("consumedRecoveryProfileCount")
            if consumed != len(fixture.recovery_ids):
                failures.append(
                    f"consolidation consumed {consumed} profiles, "
                    f"expected {len(fixture.recovery_ids)}"
                )
        if container.exists():
            failures.append(
                "recovery-profiles container is still in place; it must be archived so the "
                "on-disk world converges to a single primary"
            )
        backups = user_data / "backups" / "profile-consolidation"
        if not backups.is_dir() or not any(backups.iterdir()):
            failures.append("no consolidation backup was recorded; sources must be archived")

    if scenario.get("assert_sessions"):
        found = _session_labels(primary_home / "state" / "sessions.db")
        missing = fixture.expected_session_labels - found
        if missing:
            failures.append(f"sessions missing from the primary profile: {sorted(missing)}")

    if scenario.get("assert_config_adopted"):
        config = primary_home / "config.toml"
        text = config.read_text(encoding="utf-8") if config.is_file() else ""
        if "adopted = 'newer'" not in text:
            failures.append(
                "primary config.toml did not adopt the newest recovery configuration; "
                f"contents were {text!r}"
            )

    return failures


def _run_scenario(
    name: str,
    *,
    app: Path | None,
    workdir: Path,
    port: int,
    timeout: float,
    dry_run: bool,
) -> dict[str, Any]:
    scenario = SCENARIOS[name]
    # Consolidation refuses profile roots reached through a link, and on macOS
    # /tmp is a symlink to /private/tmp. Resolve so the fixture exercises the
    # product rather than tripping the path guard.
    workdir = workdir.resolve()
    root = workdir / name
    if root.exists():
        shutil.rmtree(root)
    user_data = root / "user-data"
    isolated_home = root / "home"
    user_data.mkdir(parents=True)
    isolated_home.mkdir(parents=True)

    fixture: Fixture = scenario["build"](user_data)
    result: dict[str, Any] = {
        "scenario": name,
        "why": scenario["why"],
        "expected": scenario["expect"],
        "recovery_profiles": len(fixture.recovery_ids),
        "notes": fixture.notes,
        "user_data": str(user_data),
    }

    if dry_run:
        result["verdict"] = "dry-run"
        result["ok"] = True
        return result

    assert app is not None
    binary = _app_binary(app)
    process = _launch(binary, user_data, port=port, isolated_home=isolated_home)
    log_path = user_data / "logs" / "desktop.log"
    try:
        verdict, events = _classify(process, log_path, port=port, timeout=timeout)
    finally:
        _terminate(process)

    result["verdict"] = verdict
    result["events"] = [event.get("event") for event in events]
    blocked = [
        event for event in events if event.get("event") == "desktop_profile_consolidation_completed"
    ]
    if blocked:
        result["consolidation"] = blocked[-1]

    expected = scenario["expect"]
    failures: list[str] = []
    if expected != "any" and verdict != expected:
        failures.append(f"expected verdict {expected!r} but observed {verdict!r}")
    if verdict == "entered":
        failures.extend(_assert_side_effects(scenario, fixture, events))
    result["failures"] = failures
    result["ok"] = not failures
    return result


def _touch_tree(root: Path) -> None:
    """Bump mtimes the way a user browsing the folder in Finder/Explorer would."""

    if not root.exists():
        return
    stamp = time.time() + 1
    for path in [root, *root.rglob("*")]:
        try:
            os.utime(path, (stamp, stamp))
        except OSError:
            continue


def _run_wedge_probe(
    *,
    app: Path,
    workdir: Path,
    port: int,
    kill_after: float,
    timeout: float,
) -> dict[str, Any]:
    """Interrupt a consolidation, disturb the sources, then relaunch.

    Resume refuses to continue when a recorded source snapshot no longer
    reproduces, so a crash followed by an ordinary metadata change is the
    documented path into a permanently blocked startup.  This probe reports what
    actually happens instead of assuming.
    """

    workdir = workdir.resolve()
    label = f"wedge-{kill_after:g}s"
    root = workdir / label
    if root.exists():
        shutil.rmtree(root)
    user_data = root / "user-data"
    isolated_home = root / "home"
    user_data.mkdir(parents=True)
    isolated_home.mkdir(parents=True)
    fixture = _multi_recovery(user_data)

    binary = _app_binary(app)
    log_path = user_data / "logs" / "desktop.log"

    first = _launch(binary, user_data, port=port, isolated_home=isolated_home)
    try:
        deadline = time.monotonic() + kill_after
        entered_before_kill = False
        while time.monotonic() < deadline:
            if _gateway_answered(port):
                entered_before_kill = True
                break
            time.sleep(0.2)
    finally:
        _terminate(first)

    journal = user_data / ".opensquilla-profile-consolidation.json"
    journal_present = journal.is_file()
    journal_phase = None
    if journal_present:
        try:
            journal_phase = json.loads(journal.read_text(encoding="utf-8")).get("phase")
        except (json.JSONDecodeError, OSError):
            journal_phase = "unreadable"

    _touch_tree(user_data / "recovery-profiles")
    _touch_tree(user_data / "backups")

    second = _launch(binary, user_data, port=port + 1, isolated_home=isolated_home)
    try:
        verdict, events = _classify(second, log_path, port=port + 1, timeout=timeout)
    finally:
        _terminate(second)

    return {
        "probe": label,
        "kill_after_seconds": kill_after,
        "gateway_answered_before_kill": entered_before_kill,
        "journal_present_after_kill": journal_present,
        "journal_phase_after_kill": journal_phase,
        "relaunch_verdict": verdict,
        "wedged": verdict != "entered",
        "recovery_profiles": len(fixture.recovery_ids),
        "events": [event.get("event") for event in events],
        "user_data": str(user_data),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="print the scenario catalogue as JSON")

    run = sub.add_parser("run", help="build a fault environment and launch the packaged app")
    run.add_argument("--scenario", action="append", default=None, help="repeatable; default all")
    run.add_argument("--app", type=Path, default=None, help="path to OpenSquilla.app")
    run.add_argument("--workdir", type=Path, required=True)
    run.add_argument("--port", type=int, default=18931)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--report", type=Path, default=None)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="build fixtures and validate the catalogue without launching",
    )

    wedge = sub.add_parser(
        "wedge",
        help="interrupt consolidation, disturb the sources, and report whether startup recovers",
    )
    wedge.add_argument("--app", type=Path, required=True)
    wedge.add_argument("--workdir", type=Path, required=True)
    wedge.add_argument("--port", type=int, default=18951)
    wedge.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    wedge.add_argument("--report", type=Path, default=None)
    wedge.add_argument(
        "--kill-after",
        type=float,
        action="append",
        default=None,
        help="seconds before SIGKILL; repeatable (default 1 2 4 8)",
    )

    args = parser.parse_args(argv)

    if args.command == "wedge":
        args.workdir.mkdir(parents=True, exist_ok=True)
        delays = args.kill_after or [1.0, 2.0, 4.0, 8.0]
        probes = []
        for index, delay in enumerate(delays):
            probes.append(
                _run_wedge_probe(
                    app=args.app,
                    workdir=args.workdir,
                    port=args.port + index * 2,
                    kill_after=delay,
                    timeout=args.timeout,
                )
            )
            latest = probes[-1]
            state = "WEDGED" if latest["wedged"] else "recovered"
            print(
                f"[{state}] kill after {delay:g}s -> relaunch {latest['relaunch_verdict']}"
                f" (journal phase {latest['journal_phase_after_kill']})",
                flush=True,
            )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(probes, indent=2) + "\n", encoding="utf-8")
        wedged = [probe for probe in probes if probe["wedged"]]
        print(f"\n{len(wedged)}/{len(probes)} interruptions left startup blocked", flush=True)
        # Reported, never gating: these paths are known-unfixed, and failing the
        # workflow on them would mask new regressions.
        return 0

    if args.command == "list":
        print(
            json.dumps(
                {
                    name: {"expect": spec["expect"], "why": spec["why"]}
                    for name, spec in SCENARIOS.items()
                },
                indent=2,
            )
        )
        return 0

    if not args.dry_run and args.app is None:
        parser.error("--app is required unless --dry-run is used")

    names = args.scenario or list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    args.workdir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, name in enumerate(names):
        # A distinct port per scenario keeps a lingering gateway from being
        # mistaken for the next scenario's success.
        results.append(
            _run_scenario(
                name,
                app=args.app,
                workdir=args.workdir,
                port=args.port + index,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
        )
        latest = results[-1]
        status = "ok" if latest["ok"] else "FAIL"
        print(f"[{status}] {name}: verdict={latest['verdict']}", flush=True)
        for failure in latest.get("failures", []):
            print(f"       - {failure}", flush=True)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    failed = [result for result in results if not result["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios ok", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
