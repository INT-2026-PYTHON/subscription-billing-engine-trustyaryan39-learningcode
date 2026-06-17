"""
Database connection helper.

✅ COMPLETE. Use this from repositories; do not call sqlite3 directly elsewhere.

Usage:
    db = Database("billing.db")
    db.init_schema()                 # one-time setup
    with db.transaction() as conn:   # for multi-statement atomic work
        conn.execute("INSERT ...")
        conn.execute("INSERT ...")
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, factory=ManagedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_schema(self) -> None:
        """Create all tables (idempotent — uses CREATE TABLE IF NOT EXISTS)."""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(sql)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager for atomic multi-statement work.

        Uses BEGIN IMMEDIATE so we acquire a write lock up front and avoid
        the silent-rollback foot-gun where two writers see overlapping reads.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
