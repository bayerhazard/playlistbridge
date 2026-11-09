"""Persistenter Speicher (SQLite) fuer Einstellungen, Tokens und Jobs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any


DB_PATH = os.environ.get("PB_DB", "/Data/playlistbridge.db")

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                provider      TEXT PRIMARY KEY,
                access_token  TEXT,
                refresh_token TEXT,
                expires_at    REAL DEFAULT 0,
                scope         TEXT,
                token_type    TEXT,
                account       TEXT
            );
            CREATE TABLE IF NOT EXISTS oauth_state (
                state      TEXT PRIMARY KEY,
                provider   TEXT NOT NULL,
                verifier   TEXT NOT NULL,
                redirect   TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                progress   INTEGER NOT NULL DEFAULT 0,
                total      INTEGER NOT NULL DEFAULT 0,
                message    TEXT NOT NULL DEFAULT '',
                result     TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        # Abgelaufene Login-Zustaende aufraeumen
        conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (time.time() - 3600,))
    os.chmod(DB_PATH, 0o600)


# --- Einstellungen ---------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_settings(keys: list[str]) -> dict[str, str]:
    return {k: get_setting(k) for k in keys}


# --- Tokens ----------------------------------------------------------------

def save_token(provider: str, data: dict[str, Any], account: str = "") -> None:
    expires_at = time.time() + float(data.get("expires_in") or 0)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO tokens(provider, access_token, refresh_token, expires_at, scope, token_type, account)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
              access_token=excluded.access_token,
              refresh_token=COALESCE(excluded.refresh_token, tokens.refresh_token),
              expires_at=excluded.expires_at,
              scope=excluded.scope,
              token_type=excluded.token_type,
              account=COALESCE(NULLIF(excluded.account, ''), tokens.account)
            """,
            (
                provider,
                data.get("access_token"),
                data.get("refresh_token"),
                expires_at,
                data.get("scope", ""),
                data.get("token_type", "Bearer"),
                account,
            ),
        )


def get_token(provider: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE provider=?", (provider,)).fetchone()
    return dict(row) if row else None


def delete_token(provider: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM tokens WHERE provider=?", (provider,))


# --- OAuth-Zustand (PKCE) --------------------------------------------------

def save_state(state: str, provider: str, verifier: str, redirect: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO oauth_state(state, provider, verifier, redirect, created_at) VALUES(?,?,?,?,?)",
            (state, provider, verifier, redirect, time.time()),
        )


def pop_state(state: str, provider: str) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_state WHERE state=? AND provider=?", (state, provider)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM oauth_state WHERE state=?", (state,))
    return dict(row) if row else None


# --- Jobs ------------------------------------------------------------------

def create_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO jobs(id, status, progress, total, message, created_at, updated_at) "
            "VALUES(?, 'queued', 0, 0, 'Wartet …', ?, ?)",
            (job_id, now, now),
        )
    return job_id


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    total: int | None = None,
    message: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if progress is not None:
        fields.append("progress=?")
        values.append(progress)
    if total is not None:
        fields.append("total=?")
        values.append(total)
    if message is not None:
        fields.append("message=?")
        values.append(message)
    if result is not None:
        fields.append("result=?")
        values.append(json.dumps(result, ensure_ascii=False))
    fields.append("updated_at=?")
    values.append(time.time())
    values.append(job_id)
    with _LOCK, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["result"] = json.loads(job["result"]) if job.get("result") else None
    return job


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, status, progress, total, message, created_at, updated_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
