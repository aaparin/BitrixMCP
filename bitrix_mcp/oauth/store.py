from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class StoredToken:
    member_id: str
    bitrix_user_id: int
    email: str
    access_token: str
    refresh_token: str
    expires_at: int
    client_endpoint: str
    scope: str
    status: str  # active | revoked


@dataclass(frozen=True)
class AuthState:
    state: str
    email: str
    created_at: int
    expires_at: int


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
    """SQLite + Fernet token store. Sync API; call via asyncio.to_thread from async code."""

    def __init__(self, path: str | Path, encryption_key: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = encryption_key.encode('utf-8') if isinstance(encryption_key, str) else encryption_key
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError('BITRIX_TOKEN_ENCRYPTION_KEY must be a valid Fernet key.') from exc
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
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    member_id TEXT NOT NULL,
                    bitrix_user_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    access_token BLOB NOT NULL,
                    refresh_token BLOB NOT NULL,
                    expires_at INTEGER NOT NULL,
                    client_endpoint TEXT NOT NULL,
                    scope TEXT,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (member_id, bitrix_user_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth_active_email
                    ON oauth_tokens (member_id, email) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS auth_state (
                    state TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

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
                """
            )

    def _encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode('utf-8'))

    def _decrypt(self, value: bytes) -> str:
        try:
            return self._fernet.decrypt(value).decode('utf-8')
        except InvalidToken as exc:
            raise RuntimeError('Failed to decrypt token; check BITRIX_TOKEN_ENCRYPTION_KEY.') from exc

    @staticmethod
    def normalize_email(email: str) -> str:
        return ' '.join(email.strip().lower().split())

    def save_token(
        self,
        *,
        member_id: str,
        bitrix_user_id: int,
        email: str,
        access_token: str,
        refresh_token: str,
        expires_at: int,
        client_endpoint: str,
        scope: str = '',
        status: str = 'active',
    ) -> StoredToken:
        email_n = self.normalize_email(email)
        now = int(time.time())
        with self._db() as conn:
            if status == 'active':
                conn.execute(
                    """
                    UPDATE oauth_tokens
                    SET status = 'revoked', updated_at = ?
                    WHERE member_id = ? AND email = ? AND status = 'active'
                      AND bitrix_user_id != ?
                    """,
                    (now, member_id, email_n, bitrix_user_id),
                )
            conn.execute(
                """
                INSERT INTO oauth_tokens (
                    member_id, bitrix_user_id, email, access_token, refresh_token,
                    expires_at, client_endpoint, scope, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(member_id, bitrix_user_id) DO UPDATE SET
                    email = excluded.email,
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    client_endpoint = excluded.client_endpoint,
                    scope = excluded.scope,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    member_id,
                    bitrix_user_id,
                    email_n,
                    self._encrypt(access_token),
                    self._encrypt(refresh_token),
                    int(expires_at),
                    client_endpoint.rstrip('/') + '/',
                    scope,
                    status,
                    now,
                    now,
                ),
            )
        return StoredToken(
            member_id=member_id,
            bitrix_user_id=bitrix_user_id,
            email=email_n,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=int(expires_at),
            client_endpoint=client_endpoint.rstrip('/') + '/',
            scope=scope,
            status=status,
        )

    def get_by_email(self, member_id: str, email: str) -> StoredToken | None:
        email_n = self.normalize_email(email)
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE member_id = ? AND email = ? AND status = 'active'
                """,
                (member_id, email_n),
            ).fetchone()
        return self._row_to_token(row) if row else None

    def get_by_user_id(self, member_id: str, bitrix_user_id: int) -> StoredToken | None:
        with self._db() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE member_id = ? AND bitrix_user_id = ? AND status = 'active'
                """,
                (member_id, bitrix_user_id),
            ).fetchone()
        return self._row_to_token(row) if row else None

    def mark_revoked(self, member_id: str, bitrix_user_id: int) -> None:
        now = int(time.time())
        with self._db() as conn:
            conn.execute(
                """
                UPDATE oauth_tokens
                SET status = 'revoked', updated_at = ?
                WHERE member_id = ? AND bitrix_user_id = ?
                """,
                (now, member_id, bitrix_user_id),
            )

    def revoke_by_email(self, member_id: str, email: str) -> bool:
        email_n = self.normalize_email(email)
        now = int(time.time())
        with self._db() as conn:
            cur = conn.execute(
                """
                UPDATE oauth_tokens
                SET status = 'revoked', updated_at = ?
                WHERE member_id = ? AND email = ? AND status = 'active'
                """,
                (now, member_id, email_n),
            )
            return cur.rowcount > 0

    def _row_to_token(self, row: sqlite3.Row) -> StoredToken:
        return StoredToken(
            member_id=row['member_id'],
            bitrix_user_id=int(row['bitrix_user_id']),
            email=row['email'],
            access_token=self._decrypt(row['access_token']),
            refresh_token=self._decrypt(row['refresh_token']),
            expires_at=int(row['expires_at']),
            client_endpoint=row['client_endpoint'],
            scope=row['scope'] or '',
            status=row['status'],
        )

    def create_auth_state(self, state: str, email: str, *, ttl_seconds: int) -> AuthState:
        email_n = self.normalize_email(email)
        now = int(time.time())
        expires_at = now + int(ttl_seconds)
        with self._db() as conn:
            conn.execute('DELETE FROM auth_state WHERE expires_at < ?', (now,))
            conn.execute(
                """
                INSERT INTO auth_state (state, email, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (state, email_n, now, expires_at),
            )
        return AuthState(state=state, email=email_n, created_at=now, expires_at=expires_at)

    def consume_auth_state(self, state: str) -> AuthState | None:
        """Atomically fetch and delete a state row. Returns None if missing/expired."""
        now = int(time.time())
        with self._db() as conn:
            row = conn.execute(
                'SELECT * FROM auth_state WHERE state = ?',
                (state,),
            ).fetchone()
            if row is None:
                return None
            conn.execute('DELETE FROM auth_state WHERE state = ?', (state,))
            if int(row['expires_at']) < now:
                return None
            return AuthState(
                state=row['state'],
                email=row['email'],
                created_at=int(row['created_at']),
                expires_at=int(row['expires_at']),
            )

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
