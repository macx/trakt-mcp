"""Persistent state in a single SQLite file.

What is stored, and how:

* **MCP clients** registered via dynamic client registration, and which
  signed-in user approved which client on the consent page. Clients nobody
  approved expire.
* **Pending logins**: an MCP authorization request waiting for the user to
  return from Trakt. Keyed by a random ``state``; single use, short-lived.
* **Authorization codes, access tokens and refresh tokens** issued by this
  server. Only their SHA-256 hashes are stored, so a leaked database file does
  not grant access to the MCP endpoint.
* **Trakt tokens** per Trakt user. These must be usable, so they are stored as
  issued; protect the data directory (the container runs as an unprivileged
  user and the file is created with mode 0600).

All public methods are async and run the blocking SQLite calls in a worker
thread. A single connection guarded by a lock is plenty for a personal server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id   TEXT PRIMARY KEY,
    metadata    TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS client_approvals (
    client_id   TEXT NOT NULL REFERENCES clients (client_id) ON DELETE CASCADE,
    subject     TEXT NOT NULL,
    approved_at INTEGER NOT NULL,
    PRIMARY KEY (client_id, subject)
);
CREATE TABLE IF NOT EXISTS pending_logins (
    state       TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    stage       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('code', 'access', 'refresh', 'refresh_used')),
    client_id   TEXT NOT NULL,
    subject     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    family      TEXT NOT NULL,
    expires_at  INTEGER NOT NULL,
    used_at     INTEGER
);
CREATE INDEX IF NOT EXISTS grants_family ON grants (family);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    at INTEGER NOT NULL,
    ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trakt_tokens (
    subject       TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
"""


def hash_token(token: str) -> str:
    """Hash a high-entropy bearer secret for storage and lookup."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Grant:
    """A stored authorization code, access token or refresh token."""

    kind: str
    client_id: str
    subject: str
    payload: dict[str, Any]
    family: str
    expires_at: int
    used_at: int | None = None


@dataclass(frozen=True, slots=True)
class TraktTokens:
    access_token: str
    refresh_token: str
    expires_at: int


class Store:
    """Async facade over a small SQLite database."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """Create the database (mode 0600) and schema if needed."""
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch(mode=0o600, exist_ok=True)
            self._path.chmod(0o600)
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def now(self) -> int:
        return int(self._clock())

    async def _run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            if self._conn is None:
                raise RuntimeError("Store is not open")
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    # -- clients ------------------------------------------------------------

    async def count_clients(self, *, approved: bool | None = None) -> int:
        """Count clients; ``approved`` restricts to clients with/without any approval."""
        query = "SELECT COUNT(*) FROM clients"
        if approved is True:
            query += " WHERE client_id IN (SELECT client_id FROM client_approvals)"
        elif approved is False:
            query += " WHERE client_id NOT IN (SELECT client_id FROM client_approvals)"

        def op(c: sqlite3.Connection) -> int:
            return int(c.execute(query).fetchone()[0])

        return await self._run(op)

    async def save_client(self, client_id: str, metadata: dict[str, Any]) -> None:
        now = self.now()
        await self._run(
            lambda c: c.execute(
                "INSERT INTO clients (client_id, metadata, created_at) VALUES (?, ?, ?)",
                (client_id, json.dumps(metadata), now),
            )
        )

    async def approve_client(self, client_id: str, subject: str) -> None:
        """Remember that ``subject`` (a signed-in, allowlisted user) approved ``client_id``."""
        now = self.now()
        await self._run(
            lambda c: c.execute(
                "INSERT OR IGNORE INTO client_approvals (client_id, subject, approved_at)"
                " VALUES (?, ?, ?)",
                (client_id, subject, now),
            )
        )

    async def client_approved(self, client_id: str, subject: str) -> bool:
        def op(c: sqlite3.Connection) -> bool:
            row = c.execute(
                "SELECT 1 FROM client_approvals WHERE client_id = ? AND subject = ?",
                (client_id, subject),
            ).fetchone()
            return row is not None

        return await self._run(op)

    async def client_has_approval(self, client_id: str) -> bool:
        """Whether any user has approved this client (it may re-run sign-in)."""

        def op(c: sqlite3.Connection) -> bool:
            row = c.execute(
                "SELECT 1 FROM client_approvals WHERE client_id = ?", (client_id,)
            ).fetchone()
            return row is not None

        return await self._run(op)

    # -- settings -------------------------------------------------------------

    async def set_setting(self, key: str, value: str) -> None:
        await self._run(
            lambda c: c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )

    async def get_setting(self, key: str) -> str | None:
        def op(c: sqlite3.Connection) -> str | None:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None

        return await self._run(op)

    async def count_recent_failures(self, window: int, ip: str | None = None) -> int:
        """Failed passphrase attempts within the last ``window`` seconds (from ``ip``)."""
        since = self.now() - window

        def op(c: sqlite3.Connection) -> int:
            c.execute("DELETE FROM failures WHERE at <= ?", (since,))
            if ip is None:
                return int(c.execute("SELECT COUNT(*) FROM failures").fetchone()[0])
            row = c.execute("SELECT COUNT(*) FROM failures WHERE ip = ?", (ip,)).fetchone()
            return int(row[0])

        return await self._run(op)

    async def record_failure(self, ip: str = "") -> None:
        now = self.now()
        await self._run(
            lambda c: c.execute("INSERT INTO failures (at, ip) VALUES (?, ?)", (now, ip))
        )

    async def evict_unapproved_clients(self, keep: int) -> None:
        """Delete the oldest never-approved clients so that at most ``keep`` remain.

        Clients with a sign-in in progress are spared, so a flood of registrations
        cannot cancel the owner's first login with a new client.
        """
        now = self.now()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "DELETE FROM clients WHERE client_id IN (SELECT client_id FROM clients"
                " WHERE client_id NOT IN (SELECT client_id FROM client_approvals)"
                " AND client_id NOT IN (SELECT client_id FROM pending_logins"
                "  WHERE expires_at > ?)"
                " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (now, keep),
            )

        await self._run(op)

    async def client_created_at(self, client_id: str) -> int | None:
        def op(c: sqlite3.Connection) -> int | None:
            row = c.execute(
                "SELECT created_at FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            return int(row[0]) if row else None

        return await self._run(op)

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        def op(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(
                "SELECT metadata FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

        return await self._run(op)

    # -- pending logins -----------------------------------------------------

    async def save_pending_login(
        self,
        state: str,
        client_id: str,
        payload: dict[str, Any],
        ttl: int,
        *,
        stage: str = "trakt",
        max_per_client: int,
        max_total: int,
    ) -> None:
        """Store a pending login, evicting the oldest anonymous ones beyond the caps.

        Only ``stage="trakt"`` entries (created by anyone via /authorize) count
        towards and are evicted by the caps. ``stage="consent"`` entries exist
        only after an allowlisted sign-in, so a flood can never push them out.
        """
        now = self.now()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO pending_logins"
                " (state, client_id, stage, payload, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (state, client_id, stage, json.dumps(payload), now, now + ttl),
            )
            if stage != "trakt":
                return
            c.execute(
                "DELETE FROM pending_logins WHERE state IN (SELECT state FROM pending_logins"
                " WHERE stage = 'trakt' AND client_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (client_id, max_per_client),
            )
            c.execute(
                "DELETE FROM pending_logins WHERE state IN (SELECT state FROM pending_logins"
                " WHERE stage = 'trakt' ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (max_total,),
            )

        await self._run(op)

    async def get_pending_login(self, state: str) -> dict[str, Any] | None:
        now = self.now()

        def op(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(
                "SELECT payload FROM pending_logins WHERE state = ? AND expires_at > ?",
                (state, now),
            ).fetchone()
            return json.loads(row[0]) if row else None

        return await self._run(op)

    async def update_pending_login(self, state: str, payload: dict[str, Any]) -> None:
        await self._run(
            lambda c: c.execute(
                "UPDATE pending_logins SET payload = ? WHERE state = ?",
                (json.dumps(payload), state),
            )
        )

    async def pop_pending_login(self, state: str) -> dict[str, Any] | None:
        """Atomically fetch and delete a pending login; expired ones count as missing."""
        now = self.now()

        def op(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(
                "DELETE FROM pending_logins WHERE state = ? RETURNING payload, expires_at",
                (state,),
            ).fetchone()
            if row is None or row[1] <= now:
                return None
            return json.loads(row[0])  # type: ignore[no-any-return]

        return await self._run(op)

    # -- grants (codes, access & refresh tokens) ----------------------------

    async def save_grant(self, token: str, grant: Grant) -> None:
        await self._run(
            lambda c: c.execute(
                "INSERT INTO grants (token_hash, kind, client_id, subject, payload, family,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    hash_token(token),
                    grant.kind,
                    grant.client_id,
                    grant.subject,
                    json.dumps(grant.payload),
                    grant.family,
                    grant.expires_at,
                ),
            )
        )

    async def get_grant(self, token: str, kind: str) -> Grant | None:
        """Look up a non-expired grant of the given kind."""
        now = self.now()

        def op(c: sqlite3.Connection) -> Grant | None:
            row = c.execute(
                "SELECT kind, client_id, subject, payload, family, expires_at, used_at FROM grants"
                " WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (hash_token(token), kind, now),
            ).fetchone()
            return _row_to_grant(row) if row else None

        return await self._run(op)

    async def consume_grant(self, token: str, kind: str) -> Grant | None:
        """Atomically delete a grant and return it if it was still valid (single use)."""
        now = self.now()

        def op(c: sqlite3.Connection) -> Grant | None:
            row = c.execute(
                "DELETE FROM grants WHERE token_hash = ? AND kind = ?"
                " RETURNING kind, client_id, subject, payload, family, expires_at, used_at",
                (hash_token(token), kind),
            ).fetchone()
            if row is None or row[5] <= now:
                return None
            return _row_to_grant(row)

        return await self._run(op)

    async def retire_refresh_token(self, token: str) -> Grant | None:
        """Mark a valid refresh token as used and return it (rotation).

        Used tokens are kept until they expire so that a replay can be detected
        and the whole token family revoked (RFC 9700, section 4.14.2).
        """
        now = self.now()

        def op(c: sqlite3.Connection) -> Grant | None:
            row = c.execute(
                "UPDATE grants SET kind = 'refresh_used', used_at = ? WHERE token_hash = ?"
                " AND kind = 'refresh' AND expires_at > ?"
                " RETURNING kind, client_id, subject, payload, family, expires_at, used_at",
                (now, hash_token(token), now),
            ).fetchone()
            return _row_to_grant(row) if row else None

        return await self._run(op)

    async def family_of(self, token: str) -> str | None:
        """Return the family of any stored grant with this token, used or not."""

        def op(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT family FROM grants WHERE token_hash = ?", (hash_token(token),)
            ).fetchone()
            return str(row[0]) if row else None

        return await self._run(op)

    async def revoke_family(self, family: str) -> None:
        """Revoke every token descending from one authorization (RFC 9700, 4.14)."""
        await self._run(lambda c: c.execute("DELETE FROM grants WHERE family = ?", (family,)))

    async def revoke_subject(self, subject: str) -> None:
        await self._run(lambda c: c.execute("DELETE FROM grants WHERE subject = ?", (subject,)))

    async def purge_expired(self, unapproved_client_ttl: int) -> None:
        """Delete expired grants and logins, and clients that were never approved."""
        now = self.now()

        def op(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM grants WHERE expires_at <= ?", (now,))
            c.execute("DELETE FROM pending_logins WHERE expires_at <= ?", (now,))
            c.execute(
                "DELETE FROM clients WHERE created_at <= ? AND client_id NOT IN"
                " (SELECT client_id FROM client_approvals)",
                (now - unapproved_client_ttl,),
            )

        await self._run(op)

    # -- Trakt tokens -------------------------------------------------------

    async def save_trakt_tokens(self, subject: str, tokens: TraktTokens) -> None:
        now = self.now()
        await self._run(
            lambda c: c.execute(
                "INSERT INTO trakt_tokens (subject, access_token, refresh_token, expires_at,"
                " updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(subject) DO UPDATE SET"
                " access_token = excluded.access_token, refresh_token = excluded.refresh_token,"
                " expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (subject, tokens.access_token, tokens.refresh_token, tokens.expires_at, now),
            )
        )

    async def get_trakt_tokens(self, subject: str) -> TraktTokens | None:
        def op(c: sqlite3.Connection) -> TraktTokens | None:
            row = c.execute(
                "SELECT access_token, refresh_token, expires_at FROM trakt_tokens"
                " WHERE subject = ?",
                (subject,),
            ).fetchone()
            return TraktTokens(row[0], row[1], row[2]) if row else None

        return await self._run(op)

    async def delete_trakt_tokens(self, subject: str) -> None:
        await self._run(
            lambda c: c.execute("DELETE FROM trakt_tokens WHERE subject = ?", (subject,))
        )


def _row_to_grant(row: tuple[Any, ...]) -> Grant:
    return Grant(
        kind=row[0],
        client_id=row[1],
        subject=row[2],
        payload=json.loads(row[3]),
        family=row[4],
        expires_at=row[5],
        used_at=row[6],
    )
