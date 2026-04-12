"""Backend FastAPI local (127.0.0.1:9999) pour l'UI IABridge.

Sert :
  - Le dashboard statique (HTML/CSS/JS) depuis ui/static/
  - Une API REST JSON pour l'UI :
      GET  /api/status          → snapshot de AppState
      GET  /api/actions         → historique paginé + filtres
      GET  /api/stats           → agrégats pour les graphiques
      GET  /api/trust           → trust.json courant
      POST /api/trust           → modifier une permission
      POST /api/killswitch      → activer / désactiver le killswitch
      POST /api/panic           → déclencher le panic mode
      POST /api/clear-history   → purger l'historique
      GET  /api/settings        → clé/valeur depuis SQLite
      POST /api/settings        → mise à jour setting

Tout est binded sur 127.0.0.1 — aucune exposition réseau.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.state import AppState
from core.config import load_trust, save_trust, DEFAULT_TRUST
from storage import Database


log = logging.getLogger("iabridge.backend")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(state: AppState, db: Database) -> FastAPI:
    app = FastAPI(title="IABridge Desktop", version="3.1")

    # ── API ───────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        return state.snapshot()

    @app.get("/api/actions")
    async def api_actions(
        limit: int = 100,
        offset: int = 0,
        action: str | None = None,
        status: str | None = None,
        search: str | None = None,
        since: float | None = None,
    ) -> dict[str, Any]:
        rows = await db.list_actions(
            limit=limit,
            offset=offset,
            action_filter=action,
            status_filter=status,
            search=search,
            since=since,
        )
        total = await db.count_actions(
            action_filter=action, status_filter=status, since=since
        )
        return {"total": total, "count": len(rows), "offset": offset, "actions": rows}

    @app.get("/api/stats")
    async def api_stats(days: int = 7) -> dict[str, Any]:
        return await db.stats_daily(days=days)

    @app.get("/api/trust")
    async def api_trust_get() -> dict[str, Any]:
        trust = load_trust()
        return {"defaults": DEFAULT_TRUST, "current": trust}

    @app.post("/api/trust")
    async def api_trust_set(req: Request) -> dict[str, Any]:
        body = await req.json()
        action = body.get("action")
        mode = body.get("mode")
        if not action or mode not in ("allow", "ask", "deny"):
            raise HTTPException(status_code=400, detail="action et mode (allow|ask|deny) requis")
        trust = load_trust()
        trust[action] = mode
        save_trust(trust)
        return {"ok": True, "trust": trust}

    @app.post("/api/killswitch")
    async def api_killswitch(req: Request) -> dict[str, Any]:
        body = await req.json()
        enable = bool(body.get("enable", True))
        state.killswitch = enable
        if not enable:
            state.panic_mode = False
            state.panic_triggered_at = None
        log.warning("Killswitch %s", "activé" if enable else "désactivé")
        return {"ok": True, "killswitch": state.killswitch, "panic_mode": state.panic_mode}

    @app.post("/api/panic")
    async def api_panic(req: Request) -> dict[str, Any]:
        body = await req.json()
        enable = bool(body.get("enable", True))
        state.panic_mode = enable
        state.killswitch = enable
        if enable:
            state.panic_triggered_at = time.time()
            log.error("🚨 PANIC MODE ACTIVÉ 🚨")
            # Les side-effects (kill browser, lock screen…) sont déclenchés
            # par le dispatcher en Session 3.
        else:
            state.panic_triggered_at = None
            log.info("Panic mode désactivé")
        return {"ok": True, "panic_mode": state.panic_mode, "killswitch": state.killswitch}

    @app.post("/api/clear-history")
    async def api_clear_history(req: Request) -> dict[str, Any]:
        body = await req.json() if req.headers.get("content-length") else {}
        older = body.get("older_than_days") if isinstance(body, dict) else None
        deleted = await db.clear_actions(older_than_days=older)
        return {"ok": True, "deleted": deleted}

    @app.get("/api/settings")
    async def api_settings_get() -> dict[str, Any]:
        return await db.all_settings()

    @app.post("/api/settings")
    async def api_settings_set(req: Request) -> dict[str, Any]:
        body = await req.json()
        if not isinstance(body, dict) or "key" not in body:
            raise HTTPException(status_code=400, detail="body must have 'key' field")
        await db.set_setting(body["key"], body.get("value"))
        return {"ok": True}

    # ── Static files ──────────────────────────────────────────────────────
    # Le dashboard HTML est à la racine /

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/favicon.ico")
        async def favicon() -> FileResponse:
            fav = STATIC_DIR / "favicon.ico"
            if fav.exists():
                return FileResponse(fav)
            raise HTTPException(status_code=404)

    return app
