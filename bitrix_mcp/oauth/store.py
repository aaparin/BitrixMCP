from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class AuditEntry:
    ts: int
    email: str
    bitrix_user_id: int | None
    identity_kind: str
    method: str
    access: str
    ownership_result: str
    dry_run: bool
    outcome: str
    error: str = ''


class TokenStore:
    """SQLite store for the pinned portal and the write audit log.

    OAuth tokens themselves live in the OAuth proxy storage. Sync API; call via
    asyncio.to_thread from async code.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    bitrix_user_id INTEGER,
                    identity_kind TEXT NOT NULL,
                    method TEXT NOT NULL,
                    access TEXT NOT NULL,
                    ownership_result TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS portal_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    member_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def get_pinned_member_id(self) -> str | None:
        with self._db() as conn:
            row = conn.execute('SELECT member_id FROM portal_settings WHERE id = 1').fetchone()
        return str(row['member_id']) if row else None

    def pin_or_check_member_id(self, member_id: str) -> bool:
        """Pin portal on first successful auth. Later calls must match the pin."""
        member_id = (member_id or '').strip()
        if not member_id:
            return False
        now = int(time.time())
        with self._db() as conn:
            row = conn.execute('SELECT member_id FROM portal_settings WHERE id = 1').fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO portal_settings (id, member_id, updated_at) VALUES (1, ?, ?)',
                    (member_id, now),
                )
                return True
            return str(row['member_id']) == member_id

    @staticmethod
    def normalize_email(email: str) -> str:
        return ' '.join(email.strip().lower().split())

    def write_audit(self, entry: AuditEntry) -> None:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    ts, email, bitrix_user_id, identity_kind, method, access,
                    ownership_result, dry_run, outcome, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.ts,
                    entry.email,
                    entry.bitrix_user_id,
                    entry.identity_kind,
                    entry.method,
                    entry.access,
                    entry.ownership_result,
                    1 if entry.dry_run else 0,
                    entry.outcome,
                    entry.error,
                ),
            )
