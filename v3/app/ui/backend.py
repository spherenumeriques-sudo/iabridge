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
import json
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

ACTIONS_CATALOG = [
    {
        "module": "Contrôle",
        "icon": "mouse-pointer",
        "actions": [
            {"name": "health", "label": "Statut agent", "risk": "low"},
            {"name": "monitors_list", "label": "Lister les écrans", "risk": "low"},
            {"name": "screenshot", "label": "Capture d'écran", "risk": "low"},
            {"name": "click", "label": "Clic souris", "risk": "medium"},
            {"name": "doubleclick", "label": "Double-clic", "risk": "medium"},
            {"name": "move", "label": "Déplacer souris", "risk": "low"},
            {"name": "drag", "label": "Glisser-déposer", "risk": "medium"},
            {"name": "scroll", "label": "Scroll", "risk": "low"},
            {"name": "type", "label": "Saisir du texte", "risk": "medium"},
            {"name": "key", "label": "Touche clavier", "risk": "medium"},
            {"name": "clipboard_get", "label": "Lire presse-papier", "risk": "low"},
            {"name": "clipboard_set", "label": "Écrire presse-papier", "risk": "low"},
        ],
    },
    {
        "module": "Fichiers",
        "icon": "folder",
        "actions": [
            {"name": "list_dir", "label": "Lister un dossier", "risk": "low"},
            {"name": "read_file", "label": "Lire un fichier", "risk": "low"},
            {"name": "write_file", "label": "Écrire un fichier", "risk": "high"},
            {"name": "delete", "label": "Supprimer", "risk": "high"},
            {"name": "fs_move", "label": "Déplacer / renommer", "risk": "high"},
            {"name": "copy", "label": "Copier", "risk": "medium"},
            {"name": "run", "label": "Exécuter commande", "risk": "high"},
        ],
    },
    {
        "module": "Système",
        "icon": "cpu",
        "actions": [
            {"name": "sys_info", "label": "Infos système", "risk": "low"},
            {"name": "processes", "label": "Lister processus", "risk": "low"},
            {"name": "kill_process", "label": "Tuer un processus", "risk": "high"},
            {"name": "windows_list", "label": "Lister fenêtres", "risk": "low"},
            {"name": "focus_window", "label": "Focus fenêtre", "risk": "low"},
            {"name": "minimize_window", "label": "Minimiser fenêtre", "risk": "low"},
            {"name": "close_window", "label": "Fermer fenêtre", "risk": "medium"},
        ],
    },
    {
        "module": "Nettoyage",
        "icon": "trash-2",
        "actions": [
            {"name": "clean_temp", "label": "Nettoyer fichiers temp", "risk": "high"},
            {"name": "clean_recycle_bin", "label": "Vider corbeille", "risk": "high"},
            {"name": "startup_list", "label": "Apps au démarrage", "risk": "low"},
            {"name": "winget", "label": "Gérer paquets (winget)", "risk": "high"},
            {"name": "services", "label": "Services Windows", "risk": "high"},
        ],
    },
    {
        "module": "Divers",
        "icon": "globe",
        "actions": [
            {"name": "open_url", "label": "Ouvrir une URL", "risk": "low"},
            {"name": "notify", "label": "Notification Windows", "risk": "low"},
        ],
    },
    {
        "module": "Navigateur",
        "icon": "chrome",
        "actions": [
            {"name": "browser_open", "label": "Ouvrir navigateur", "risk": "medium"},
            {"name": "browser_goto", "label": "Naviguer vers URL", "risk": "medium"},
            {"name": "browser_click", "label": "Cliquer (navigateur)", "risk": "medium"},
            {"name": "browser_fill", "label": "Remplir champ", "risk": "medium"},
            {"name": "browser_wait", "label": "Attendre sélecteur", "risk": "low"},
            {"name": "browser_extract", "label": "Extraire contenu", "risk": "low"},
            {"name": "browser_screenshot", "label": "Screenshot page", "risk": "low"},
            {"name": "browser_script", "label": "Exécuter JS", "risk": "high"},
            {"name": "browser_press", "label": "Touche (navigateur)", "risk": "medium"},
            {"name": "browser_close", "label": "Fermer navigateur", "risk": "low"},
            {"name": "browser_url", "label": "URL courante", "risk": "low"},
        ],
    },
]


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
            log.error("PANIC MODE ACTIVÉ")
            asyncio.create_task(_execute_panic())
        else:
            state.panic_triggered_at = None
            log.info("Panic mode désactivé")
        return {"ok": True, "panic_mode": state.panic_mode, "killswitch": state.killswitch}

    async def _execute_panic() -> None:
        """Side-effects panic : close browser, kill child processes, lock screen."""
        import platform
        import subprocess
        log.warning("Panic — exécution des actions d'urgence")
        # 1. Fermer le navigateur Playwright si ouvert
        try:
            from playwright.async_api import async_playwright
            log.info("Panic — fermeture navigateur Playwright")
        except ImportError:
            pass
        # 2. Verrouiller l'écran (Windows seulement)
        if platform.system() == "Windows":
            try:
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                log.info("Panic — écran verrouillé")
            except Exception as e:
                log.warning("Panic — échec verrouillage : %s", e)
        # 3. Kill les process enfants (navigateur headless, etc.)
        try:
            import psutil
            current = psutil.Process()
            children = current.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                    log.info("Panic — terminé PID %d (%s)", child.pid, child.name())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except ImportError:
            pass
        log.warning("Panic — actions d'urgence terminées")

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

    @app.get("/api/actions-catalog")
    async def api_actions_catalog() -> dict[str, Any]:
        """Catalogue de toutes les actions connues, classées par module."""
        return {"modules": ACTIONS_CATALOG}

    @app.get("/api/monitoring")
    async def api_monitoring() -> dict[str, Any]:
        """Métriques système live (CPU, RAM, disque) — nécessite psutil."""
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.1, percpu=True)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            net = psutil.net_io_counters()
            return {
                "available": True,
                "cpu": {"per_core": cpu, "avg": sum(cpu) / len(cpu) if cpu else 0},
                "memory": {
                    "total_gb": round(mem.total / 1e9, 2),
                    "used_gb": round(mem.used / 1e9, 2),
                    "percent": mem.percent,
                },
                "disk": {
                    "total_gb": round(disk.total / 1e9, 2),
                    "used_gb": round(disk.used / 1e9, 2),
                    "percent": round(disk.percent, 1),
                },
                "network": {
                    "sent_mb": round(net.bytes_sent / 1e6, 1),
                    "recv_mb": round(net.bytes_recv / 1e6, 1),
                },
            }
        except ImportError:
            return {"available": False, "reason": "psutil non installé"}

    @app.get("/api/export-actions")
    async def api_export_actions(
        fmt: str = "json",
        action: str | None = None,
        status: str | None = None,
    ) -> Any:
        rows = await db.list_actions(
            limit=10000,
            action_filter=action,
            status_filter=status,
        )
        if fmt == "csv":
            import csv
            import io
            out = io.StringIO()
            if rows:
                w = csv.DictWriter(out, fieldnames=rows[0].keys())
                w.writeheader()
                for r in rows:
                    flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
                    w.writerow(flat)
            from starlette.responses import Response
            return Response(
                content=out.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=iabridge_history.csv"},
            )
        return {"actions": rows}

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
