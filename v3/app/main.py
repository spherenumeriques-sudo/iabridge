"""IABridge Desktop — point d'entrée unique.

Lance dans un seul process asyncio :
  - Le client WebSocket vers le gateway VPS
  - Le backend FastAPI local (127.0.0.1:9999) qui sert dashboard + API
  - (Windows) Une fenêtre native pywebview qui charge le dashboard

Sans pywebview (ex: VPS Linux headless), on tourne en mode headless :
seuls le WS client + le backend HTTP démarrent, et on peut accéder
au dashboard via http://127.0.0.1:9999/ depuis un navigateur normal.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

# Rend les imports relatifs style `from core import ...` possibles
# que l'app soit lancée via `python main.py` ou packagée en .exe
APP_DIR = Path(__file__).parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import uvicorn

from core import (
    AppState,
    DB_FILE,
    LOG_FILE,
    load_config,
    setup_logging,
)
from core.ws_client import WsClient
from actions import Dispatcher
from storage import Database
from ui import create_app


log = setup_logging(LOG_FILE)


UI_HOST = "127.0.0.1"
UI_PORT = 9999


async def amain() -> int:
    cfg = load_config()
    log.info("IABridge Desktop v3.1 démarre")
    log.info("Gateway : %s", cfg.get("gateway_url"))
    log.info("Agent   : %s", cfg.get("agent_name"))
    log.info("Config  : %s", DB_FILE.parent)

    # ── Stockage ──────────────────────────────────────────────────────────
    db = Database(DB_FILE)
    await db.connect()
    log.info("DB connectée : %s", DB_FILE)

    # ── État + dispatcher ────────────────────────────────────────────────
    state = AppState()
    # Cherche l'ancien agent.py pour réutiliser ses handlers
    legacy_path = APP_DIR.parent / "agent" / "agent.py"
    dispatcher = Dispatcher(state=state, db=db, legacy_agent_path=legacy_path)
    if dispatcher.is_legacy_available():
        log.info("Handlers legacy chargés depuis %s", legacy_path)
    else:
        log.warning("Mode stub : les commandes ne seront pas exécutées (legacy indisponible)")

    # ── WebSocket client ─────────────────────────────────────────────────
    ws_client: WsClient | None = None
    token = cfg.get("token", "").strip()
    if token:
        ws_client = WsClient(
            gateway_url=cfg["gateway_url"],
            token=token,
            agent_name=cfg.get("agent_name", "agent"),
            state=state,
            dispatcher=dispatcher,
        )
        await ws_client.start()
    else:
        log.warning("Aucun token configuré — WS client non démarré. Configure via le dashboard.")

    # ── Backend FastAPI ──────────────────────────────────────────────────
    api_app = create_app(state=state, db=db)
    uv_config = uvicorn.Config(
        api_app,
        host=UI_HOST,
        port=UI_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uv_config)
    # On lance uvicorn comme coroutine dans l'event loop principal
    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    log.info("Backend UI : http://%s:%d/", UI_HOST, UI_PORT)

    # ── Signal handlers (arrêt propre) ───────────────────────────────────
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows : les signal handlers asyncio sont limités
            pass

    try:
        await stop_event.wait()
    finally:
        log.info("Arrêt demandé, cleanup…")
        if ws_client is not None:
            await ws_client.stop()
        server.should_exit = True
        await asyncio.wait([server_task], timeout=3)
        await db.close()
        log.info("Bye")
    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
