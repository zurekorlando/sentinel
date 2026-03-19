"""
Agent Registry — persistent store for Sentinel remote agents.

Follows the same thread-local SQLite WAL pattern as ``sentinel/mcd/catalog.py``.
Each agent slot is created by the server (which generates the api_key); the
agent then calls /api/agents/register on startup and /api/agents/{id}/heartbeat
every 30 seconds to stay online.

Schema
------
agents  — one row per registered agent; api_key and outbound_token are the
          same UUID-based secret.  api_key is used by the agent to authenticate
          inbound calls; outbound_token is used by the server to call the agent.

The api_key is NEVER included in list/get response dicts — only returned once
at creation time.  ``_safe_row()`` enforces this by replacing the field with
``"[hidden]"``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, List, Optional

log = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL DEFAULT '',
    hostname       TEXT NOT NULL DEFAULT '',
    os             TEXT NOT NULL DEFAULT '',
    os_version     TEXT NOT NULL DEFAULT '',
    ip_last_seen   TEXT NOT NULL DEFAULT '',
    port           INTEGER NOT NULL DEFAULT 8765,
    api_key        TEXT NOT NULL,
    outbound_token TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'never_connected',
    last_seen      TEXT,
    version        TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_agents_status ON agents (status);
CREATE INDEX IF NOT EXISTS idx_agents_ip     ON agents (ip_last_seen);

CREATE TRIGGER IF NOT EXISTS agents_updated_at
AFTER UPDATE ON agents
BEGIN
    UPDATE agents SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
    WHERE agent_id = NEW.agent_id;
END;
"""


class AgentRegistry:
    """
    Thread-safe SQLite registry for Sentinel remote agents.

    A thread-local connection is opened on first use and reused for the
    lifetime of the thread, following the same pattern as ``CatalogManager``.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        # Initialise schema on a dedicated connection.
        conn = self._open_connection()
        conn.executescript(_DDL)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    #  Connection management                                               #
    # ------------------------------------------------------------------ #

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA foreign_keys=ON;"
            "PRAGMA synchronous=NORMAL;"
        )
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._open_connection()
        return self._local.conn

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for a single database transaction."""
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        """Close the current thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict."""
        return dict(row)

    def _safe_row(self, row: sqlite3.Row) -> dict:
        """Convert a row to dict, replacing api_key with '[hidden]'."""
        d = self._row_to_dict(row)
        d["api_key"] = "[hidden]"
        return d

    # ------------------------------------------------------------------ #
    #  CRUD                                                                #
    # ------------------------------------------------------------------ #

    def create_agent(self, display_name: str, description: str = "") -> dict:
        """
        Create a new agent slot.

        Generates a UUID for agent_id and a UUID v4 api_key (same value used as
        outbound_token so the server can call the agent).  Returns the full row
        dict INCLUDING the raw api_key — callers must expose this only once.
        """
        agent_id = str(uuid.uuid4())
        api_key = str(uuid.uuid4())
        with self._tx() as cur:
            cur.execute(
                """
                INSERT INTO agents
                    (agent_id, display_name, description, api_key, outbound_token,
                     status)
                VALUES (?, ?, ?, ?, ?, 'never_connected')
                """,
                (agent_id, display_name, description, api_key, api_key),
            )
        row = self._conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        # Return full row including raw api_key — shown ONCE by the router.
        return self._row_to_dict(row)

    def list_agents(self, status_filter: str | None = None) -> List[dict]:
        """Return all agents, optionally filtered by status.  api_key hidden."""
        if status_filter:
            rows = self._conn.execute(
                "SELECT * FROM agents WHERE status = ? ORDER BY created_at",
                (status_filter,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY created_at"
            ).fetchall()
        return [self._safe_row(r) for r in rows]

    def get_agent(self, agent_id: str) -> Optional[dict]:
        """Return one agent by agent_id, or None.  api_key hidden."""
        row = self._conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        return self._safe_row(row)

    def get_agent_by_key(self, api_key: str) -> Optional[dict]:
        """
        Find an agent by api_key.

        Used during agent registration and heartbeat validation.
        Returns full row dict INCLUDING api_key for internal use.
        """
        row = self._conn.execute(
            "SELECT * FROM agents WHERE api_key = ?", (api_key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update_agent(self, agent_id: str, **fields) -> Optional[dict]:
        """
        Update arbitrary fields on an agent row.

        Only whitelisted fields are accepted to prevent SQL injection.
        Returns the updated row (api_key hidden), or None if not found.
        """
        _allowed = {
            "display_name", "description", "hostname", "os", "os_version",
            "ip_last_seen", "port", "status", "last_seen", "version",
            "outbound_token",
        }
        safe_fields = {k: v for k, v in fields.items() if k in _allowed}
        if not safe_fields:
            return self.get_agent(agent_id)

        set_clause = ", ".join(f"{k} = ?" for k in safe_fields)
        values = list(safe_fields.values()) + [agent_id]
        with self._tx() as cur:
            cur.execute(
                f"UPDATE agents SET {set_clause} WHERE agent_id = ?",
                values,
            )
        return self.get_agent(agent_id)

    def register_agent(
        self,
        agent_id: str,
        api_key: str,
        hostname: str,
        os: str,
        os_version: str,
        ip: str,
        port: int,
        version: str,
    ) -> Optional[dict]:
        """
        Called by the agent on startup.

        Validates api_key against the stored key for agent_id.  On success,
        updates all agent fields and sets status='online'.
        Returns updated row (api_key hidden) on success, None on key mismatch.
        """
        row = self._conn.execute(
            "SELECT api_key FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is None or row["api_key"] != api_key:
            return None

        with self._tx() as cur:
            cur.execute(
                """
                UPDATE agents
                   SET hostname     = ?,
                       os           = ?,
                       os_version   = ?,
                       ip_last_seen = ?,
                       port         = ?,
                       version      = ?,
                       status       = 'online',
                       last_seen    = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                 WHERE agent_id = ?
                """,
                (hostname, os, os_version, ip, port, version, agent_id),
            )
        return self.get_agent(agent_id)

    def heartbeat(
        self, api_key: str, ip: str, port: int, version: str
    ) -> Optional[dict]:
        """
        Called by the agent every 30 seconds.

        Finds the agent by api_key, updates last_seen + ip + status='online'.
        Returns updated row (api_key hidden), or None if key not found.
        """
        row = self._conn.execute(
            "SELECT agent_id FROM agents WHERE api_key = ?", (api_key,)
        ).fetchone()
        if row is None:
            return None

        agent_id = row["agent_id"]
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE agents
                   SET ip_last_seen = ?,
                       port         = ?,
                       version      = ?,
                       status       = 'online',
                       last_seen    = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                 WHERE agent_id = ?
                """,
                (ip, port, version, agent_id),
            )
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent slot.  Returns True if a row was deleted."""
        with self._tx() as cur:
            cur.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
            return cur.rowcount > 0

    def mark_stale(self, offline_after_s: int = 90) -> int:
        """
        Set status='offline' for agents that haven't sent a heartbeat recently.

        Targets agents where status='online' AND last_seen is older than
        *offline_after_s* seconds.  Returns the number of rows updated.
        """
        with self._tx() as cur:
            cur.execute(
                """
                UPDATE agents
                   SET status = 'offline'
                 WHERE status = 'online'
                   AND last_seen < strftime(
                           '%Y-%m-%dT%H:%M:%fZ',
                           'now',
                           ? || ' seconds'
                       )
                """,
                (f"-{offline_after_s}",),
            )
            return cur.rowcount


# Module-level default path — overridden by SENTINEL_AGENTS_DB env var.
DEFAULT_DB_PATH = Path(os.getenv("SENTINEL_AGENTS_DB", "agents.db"))
