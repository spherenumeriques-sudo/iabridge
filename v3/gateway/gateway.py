#!/usr/bin/env python3
"""
IABridge v3 — Gateway VPS

Reçoit une connexion WebSocket persistante depuis l'agent Windows.
Expose une API REST locale (localhost:9998) pour que Claude envoie
des commandes, qui sont relayées via le WebSocket à l'agent.

Architecture :
    Agent Windows ──wss──▶ Gateway ◀──http localhost──── Claude
                          (ce fichier)
"""
import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# Config depuis variables d'environnement avec défauts raisonnables
CONFIG_DIR = Path(os.environ.get("IABRIDGE_CONFIG_DIR", "/home/ubuntu/.config/iabridge"))
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = CONFIG_DIR / "token"

HTTP_HOST = os.environ.get("IABRIDGE_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("IABRIDGE_HTTP_PORT", "9998"))
WS_TIMEOUT = float(os.environ.get("IABRIDGE_WS_TIMEOUT", "30"))  # timeout commande

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("iabridge.gateway")


def load_or_create_token() -> str:
    """Charge le token depuis disk ou en génère un nouveau."""
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    log.info("Nouveau token généré dans %s", TOKEN_FILE)
    return token


AUTH_TOKEN = load_or_create_token()
log.info("Token chargé (longueur %d)", len(AUTH_TOKEN))

app = FastAPI(title="IABridge Gateway", version="3.0")


class AgentConnection:
    """Encapsule une connexion WebSocket unique vers l'agent Windows.

    Pour la v3 on supporte 1 seul agent à la fois. Si un deuxième se
    connecte, on ferme l'ancien. Multi-agent = v3.1.
    """

    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.info: dict[str, Any] = {}
        self.connected_at: float | None = None

    @property
    def is_connected(self) -> bool:
        return self.ws is not None

    async def attach(self, ws: WebSocket, info: dict[str, Any]) -> None:
        async with self.lock:
            if self.ws is not None:
                log.warning("Nouvel agent remplace l'ancien")
                try:
                    await self.ws.close(code=1000, reason="replaced by new agent")
                except Exception:
                    pass
            self.ws = ws
            self.info = info
            self.connected_at = time.time()
            log.info("Agent connecté : %s", info)

    async def detach(self) -> None:
        async with self.lock:
            self.ws = None
            self.info = {}
            self.connected_at = None
            # réveille tous les waiters en erreur
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("agent disconnected"))
            self.pending.clear()
            log.info("Agent déconnecté")

    async def send_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Envoie une commande à l'agent et attend la réponse."""
        if self.ws is None:
            raise RuntimeError("no agent connected")
        # Id unique pour matcher la réponse
        cmd_id = secrets.token_urlsafe(8)
        command = {**command, "id": cmd_id}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cmd_id] = fut
        try:
            await self.ws.send_text(json.dumps(command))
            result = await asyncio.wait_for(fut, timeout=WS_TIMEOUT)
            return result
        finally:
            self.pending.pop(cmd_id, None)

    def handle_response(self, message: dict[str, Any]) -> None:
        """Route une réponse reçue de l'agent vers le bon waiter."""
        cmd_id = message.get("id")
        if cmd_id is None:
            log.warning("Réponse sans id : %s", message)
            return
        fut = self.pending.get(cmd_id)
        if fut is None:
            log.warning("Réponse pour cmd_id inconnue : %s", cmd_id)
            return
        if not fut.done():
            fut.set_result(message)


agent = AgentConnection()


@app.get("/health")
async def health() -> dict[str, Any]:
    """Healthcheck du gateway + statut agent."""
    return {
        "gateway": "ok",
        "agent_connected": agent.is_connected,
        "agent_info": agent.info,
        "uptime_agent": (time.time() - agent.connected_at) if agent.connected_at else None,
    }


@app.post("/cmd")
async def command(request: Request) -> JSONResponse:
    """Envoie une commande à l'agent Windows connecté.

    Le body est un JSON libre, relayé tel quel à l'agent (minus le champ id).
    Exemple : {"action": "screenshot", "format": "jpeg", "quality": 80}
    """
    # Auth : bearer token sur l'API locale aussi, même si on écoute sur 127.0.0.1
    # (sécurité en profondeur)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not agent.is_connected:
        raise HTTPException(status_code=503, detail="No agent connected")

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    if not isinstance(body, dict) or "action" not in body:
        raise HTTPException(status_code=400, detail="Body must have 'action' field")

    try:
        result = await agent.send_command(body)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Agent timeout") from None
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return JSONResponse(result)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Point d'entrée WebSocket pour l'agent Windows.

    L'agent se connecte, envoie un premier message {"auth": "<token>", "info": {...}},
    puis la connexion reste ouverte pour recevoir des commandes et renvoyer des réponses.
    """
    await ws.accept()
    try:
        # Premier message = auth
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            hello = json.loads(raw)
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.close(code=1008, reason="auth timeout or invalid json")
            return

        if hello.get("auth") != AUTH_TOKEN:
            log.warning("Auth refusée depuis %s", ws.client)
            await ws.close(code=1008, reason="invalid token")
            return

        await agent.attach(ws, hello.get("info", {}))
        await ws.send_text(json.dumps({"ok": True, "gateway": "v3.0"}))

        # Boucle de réception — l'agent envoie uniquement des réponses aux commandes
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("JSON invalide reçu de l'agent : %s", raw[:200])
                continue
            agent.handle_response(message)
    except WebSocketDisconnect:
        log.info("Agent s'est déconnecté (normal)")
    except Exception as e:
        log.exception("Erreur WebSocket : %s", e)
    finally:
        await agent.detach()


if __name__ == "__main__":
    import uvicorn

    log.info("IABridge Gateway v3.0 démarre sur %s:%d", HTTP_HOST, HTTP_PORT)
    log.info("Token file : %s", TOKEN_FILE)
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")
