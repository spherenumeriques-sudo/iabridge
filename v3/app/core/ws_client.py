"""Client WebSocket vers le gateway VPS.

Tâche asyncio permanente : maintient la connexion ouverte, handshake,
relaie les commandes reçues au dispatcher, renvoie la réponse, et
reconnecte avec backoff exponentiel en cas de coupure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import platform
from typing import TYPE_CHECKING

import websockets

from .state import AppState

if TYPE_CHECKING:
    from actions.dispatcher import Dispatcher


log = logging.getLogger("iabridge.ws_client")


class WsClient:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        agent_name: str,
        state: AppState,
        dispatcher: "Dispatcher",
    ) -> None:
        self.gateway_url = gateway_url
        self.token = token
        self.agent_name = agent_name
        self.state = state
        self.dispatcher = dispatcher
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run_forever(), name="ws_client")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run_forever(self) -> None:
        info = {
            "name": self.agent_name,
            "platform": platform.system(),
            "release": platform.release(),
            "version": "3.1-app",
        }
        backoff = 1.0
        while not self._stop.is_set():
            try:
                log.info("Connexion à %s", self.gateway_url)
                async with websockets.connect(
                    self.gateway_url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=32 * 1024 * 1024,
                ) as ws:
                    await ws.send(json.dumps({"auth": self.token, "info": info}))
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    resp = json.loads(resp_raw)
                    if not resp.get("ok"):
                        log.error("Auth refusée : %s", resp)
                        await asyncio.sleep(30)
                        continue
                    log.info("Connecté au gateway %s", resp.get("gateway", "?"))
                    backoff = 1.0
                    self.state.mark_connected(self.gateway_url)

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("JSON invalide reçu : %s", raw[:200])
                            continue
                        response = await self.dispatcher.dispatch(msg)
                        await ws.send(json.dumps(response, default=str))
            except (websockets.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                log.warning("Déconnecté : %s (reconnect dans %.1fs)", e, backoff)
                self.state.mark_disconnected(str(e))
            except Exception as e:
                log.exception("Erreur inattendue : %s", e)
                self.state.mark_disconnected(str(e))
            finally:
                self.state.mark_disconnected(self.state.last_disconnect_reason or "")
            self.state.reconnect_attempts += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                # si stop() → on sort
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30)
