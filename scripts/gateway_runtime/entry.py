"""Stable executable entrypoint for the public Gateway Runtime."""

from __future__ import annotations

import sqlite3
import ssl
import sys
from collections.abc import Sequence

_RUNTIME_CA_PROBE_ARG = "--_runtime-ca-probe"
_RUNTIME_CAPABILITY_PROBE_ARG = "--_runtime-capability-probe"
_LEGACY_DESKTOP_CA_PROBE_ARG = "--_desktop-ca-probe"
_RUNTIME_CA_PROBE_OK = "opensquilla-runtime-ca-store-ok"
_RUNTIME_CAPABILITY_PROBE_OK = "opensquilla-runtime-capabilities-ok"
_LEGACY_DESKTOP_CA_PROBE_OK = "opensquilla-desktop-ca-store-ok"
_SANDBOX_FILESYSTEM_WORKER_ARG = "--_sandbox-filesystem-worker"


def _run_ca_probe(*, legacy_desktop_output: bool) -> int:
    try:
        context = ssl.create_default_context()
        ca_certificate_count = len(context.get_ca_certs(binary_form=True))
    except Exception:
        ca_certificate_count = 0

    if ca_certificate_count <= 0:
        print(
            "OpenSquilla Gateway Runtime TLS trust probe found no trusted CA certificates.",
            file=sys.stderr,
        )
        return 1

    marker = _LEGACY_DESKTOP_CA_PROBE_OK if legacy_desktop_output else _RUNTIME_CA_PROBE_OK
    print(f"{marker} x509_ca={ca_certificate_count}")
    return 0


def _run_capability_probe() -> int:
    import sqlite_vec

    from opensquilla.persistence.migrator import resolve_migrations_dir
    from opensquilla.provider.registry import list_provider_specs

    migrations = tuple(resolve_migrations_dir().glob("*.py"))
    if not migrations:
        print("OpenSquilla Gateway Runtime contains no database migrations.", file=sys.stderr)
        return 1
    if not list_provider_specs():
        print("OpenSquilla Gateway Runtime contains no Provider registry.", file=sys.stderr)
        return 1

    with sqlite3.connect(":memory:") as connection:
        connection.enable_load_extension(True)
        try:
            connection.load_extension(sqlite_vec.loadable_path())
        finally:
            connection.enable_load_extension(False)
        connection.execute("CREATE VIRTUAL TABLE smoke_fts USING fts5(content)")
        connection.execute("INSERT INTO smoke_fts(content) VALUES ('gateway runtime')")
        result = connection.execute(
            "SELECT content FROM smoke_fts WHERE smoke_fts MATCH 'gateway'"
        ).fetchone()
        if result != ("gateway runtime",):
            print("OpenSquilla Gateway Runtime SQLite FTS5 probe failed.", file=sys.stderr)
            return 1
        vec_version = connection.execute("SELECT vec_version()").fetchone()
        if not vec_version or not isinstance(vec_version[0], str):
            print("OpenSquilla Gateway Runtime sqlite-vec probe failed.", file=sys.stderr)
            return 1

    print(
        f"{_RUNTIME_CAPABILITY_PROBE_OK} "
        f"migrations={len(migrations)} sqlite_vec={vec_version[0]}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        from opensquilla import __version__

        print(__version__)
        return 0

    if args == [_RUNTIME_CA_PROBE_ARG]:
        return _run_ca_probe(legacy_desktop_output=False)
    if args == [_RUNTIME_CAPABILITY_PROBE_ARG]:
        return _run_capability_probe()
    if args == [_LEGACY_DESKTOP_CA_PROBE_ARG]:
        return _run_ca_probe(legacy_desktop_output=True)

    if args == [_SANDBOX_FILESYSTEM_WORKER_ARG]:
        from opensquilla.sandbox.filesystem_worker import main as filesystem_worker_main

        filesystem_worker_main(["-"])
        return 0

    if len(args) == 2 and args[0] == "--elevated-helper":
        from opensquilla.sandbox.backend.windows_default_setup import (
            elevated_setup_helper_main,
        )

        return elevated_setup_helper_main(args)

    from opensquilla.cli.main import app

    if argv is not None:
        sys.argv = [sys.argv[0], *args]
    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
