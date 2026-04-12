"""Persistance SQLite pour IABridge : historique des actions + settings.

Schéma :
  actions  — log de chaque commande exécutée (params + résultat + durée)
  settings — clé/valeur pour préférences utilisateur (thème, auto-start, etc.)

La DB vit dans le config dir de l'agent (même dossier que agent.json).
Utilisée depuis l'event loop asyncio via aiosqlite pour ne pas bloquer.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,              -- unix timestamp
    action      TEXT    NOT NULL,              -- nom de l'action (ex: "screenshot")
    params      TEXT    NOT NULL,              -- JSON des paramètres
    status      TEXT    NOT NULL,              -- "ok" | "error" | "denied"
    duration_ms INTEGER NOT NULL,              -- durée d'exécution
    result      TEXT,                          -- JSON du résultat (nullable)
    error       TEXT                           -- message d'erreur (nullable)
);

CREATE INDEX IF NOT EXISTS idx_actions_ts     ON actions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_actions_action ON actions(action);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                        -- JSON sérialisé
);
"""


class Database:
    """Wrapper asyncio autour de SQLite pour IABridge."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database non connectée — appeler connect() d'abord")
        return self._db

    # ── Actions ───────────────────────────────────────────────────────────

    async def log_action(
        self,
        action: str,
        params: dict[str, Any],
        status: str,
        duration_ms: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        """Enregistre une action exécutée. Retourne l'id inséré."""
        cursor = await self.db.execute(
            "INSERT INTO actions (ts, action, params, status, duration_ms, result, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                action,
                json.dumps(params, ensure_ascii=False, default=str),
                status,
                duration_ms,
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                error,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid or 0

    async def list_actions(
        self,
        limit: int = 100,
        offset: int = 0,
        action_filter: str | None = None,
        status_filter: str | None = None,
        search: str | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Liste paginée + filtrable de l'historique."""
        where: list[str] = []
        args: list[Any] = []
        if action_filter:
            where.append("action = ?")
            args.append(action_filter)
        if status_filter:
            where.append("status = ?")
            args.append(status_filter)
        if search:
            where.append("(params LIKE ? OR result LIKE ? OR error LIKE ?)")
            pat = f"%{search}%"
            args.extend([pat, pat, pat])
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        sql = "SELECT * FROM actions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        cursor = await self.db.execute(sql, args)
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def count_actions(
        self,
        action_filter: str | None = None,
        status_filter: str | None = None,
        since: float | None = None,
    ) -> int:
        where: list[str] = []
        args: list[Any] = []
        if action_filter:
            where.append("action = ?")
            args.append(action_filter)
        if status_filter:
            where.append("status = ?")
            args.append(status_filter)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        sql = "SELECT COUNT(*) FROM actions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        cursor = await self.db.execute(sql, args)
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def stats_daily(self, days: int = 7) -> dict[str, Any]:
        """Stats agrégées pour le dashboard : total, par statut, top actions."""
        since = time.time() - days * 86400
        total = await self.count_actions(since=since)
        # Par statut
        cursor = await self.db.execute(
            "SELECT status, COUNT(*) FROM actions WHERE ts >= ? GROUP BY status",
            (since,),
        )
        by_status = {row[0]: row[1] for row in await cursor.fetchall()}
        # Top 5 actions
        cursor = await self.db.execute(
            "SELECT action, COUNT(*) c FROM actions WHERE ts >= ? GROUP BY action ORDER BY c DESC LIMIT 5",
            (since,),
        )
        top = [{"action": row[0], "count": row[1]} for row in await cursor.fetchall()]
        return {"total": total, "by_status": by_status, "top_actions": top, "days": days}

    async def clear_actions(self, older_than_days: int | None = None) -> int:
        """Purge l'historique. Sans argument, tout supprime."""
        if older_than_days is None:
            cursor = await self.db.execute("DELETE FROM actions")
        else:
            cutoff = time.time() - older_than_days * 86400
            cursor = await self.db.execute("DELETE FROM actions WHERE ts < ?", (cutoff,))
        await self.db.commit()
        return cursor.rowcount or 0

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        d = dict(row)
        # dé-sérialisation JSON des champs stockés
        for k in ("params", "result"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    # ── Settings ──────────────────────────────────────────────────────────

    async def get_setting(self, key: str, default: Any = None) -> Any:
        cursor = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )
        await self.db.commit()

    async def all_settings(self) -> dict[str, Any]:
        cursor = await self.db.execute("SELECT key, value FROM settings")
        out: dict[str, Any] = {}
        for row in await cursor.fetchall():
            try:
                out[row[0]] = json.loads(row[1])
            except json.JSONDecodeError:
                out[row[0]] = row[1]
        return out
