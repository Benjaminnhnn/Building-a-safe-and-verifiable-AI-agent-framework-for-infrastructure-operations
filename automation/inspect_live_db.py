#!/usr/bin/env python3
"""Read-only, privacy-preserving inspection of a local SQLite evidence DB."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def inspect(database: Path) -> dict[str, int]:
    if not database.is_file():
        raise FileNotFoundError(f"database file not found: {database}")
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        counts: dict[str, int] = {}
        for table in ("evidence", "checkpoints", "incidents"):
            if table in tables:
                # Identifiers are selected only from the fixed allowlist above.
                counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        if not counts:
            raise ValueError("database has no recognized evidence tables")
        return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="local SQLite evidence database; no remote access is attempted")
    args = parser.parse_args()
    try:
        counts = inspect(args.database)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"inspection failed: {exc}", file=sys.stderr)
        return 2
    print(f"database: {args.database.resolve()}")
    for table, count in counts.items():
        print(f"{table}: {count}")
    print("mode: read-only; row contents and secrets were not emitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
