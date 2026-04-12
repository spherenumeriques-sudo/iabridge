"""État global partagé de l'app : état connexion WS, stats live, panic mode.

Objet unique AppState utilisé par le WS client, le backend FastAPI et l'UI.
Thread-safe via un Lock asyncio pour les écritures non atomiques.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    # Connexion WebSocket vers le gateway
    connected: bool = False
    connected_since: float | None = None
    gateway_url: str = ""
    last_disconnect: float | None = None
    last_disconnect_reason: str = ""
    reconnect_attempts: int = 0

    # Stats session courante
    actions_count_session: int = 0
    actions_ok_session: int = 0
    actions_error_session: int = 0
    actions_denied_session: int = 0
    session_started_at: float = field(default_factory=time.time)

    # Dernière action (pour l'onglet Vue d'ensemble)
    last_action: dict[str, Any] | None = None

    # Mode d'urgence
    killswitch: bool = False              # bloque toutes les commandes
    panic_mode: bool = False              # killswitch + side-effects
    panic_triggered_at: float | None = None

    # Lock pour mutations multi-champ
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def mark_connected(self, gateway_url: str) -> None:
        self.connected = True
        self.connected_since = time.time()
        self.gateway_url = gateway_url
        self.reconnect_attempts = 0

    def mark_disconnected(self, reason: str = "") -> None:
        self.connected = False
        self.connected_since = None
        self.last_disconnect = time.time()
        self.last_disconnect_reason = reason

    def record_action(self, action: str, status: str, duration_ms: int) -> None:
        self.actions_count_session += 1
        if status == "ok":
            self.actions_ok_session += 1
        elif status == "denied":
            self.actions_denied_session += 1
        else:
            self.actions_error_session += 1
        self.last_action = {
            "action": action,
            "status": status,
            "duration_ms": duration_ms,
            "ts": time.time(),
        }

    def uptime(self) -> float | None:
        if self.connected_since is None:
            return None
        return time.time() - self.connected_since

    def session_uptime(self) -> float:
        return time.time() - self.session_started_at

    def snapshot(self) -> dict[str, Any]:
        """Dump sérialisable pour l'API /status."""
        return {
            "connected": self.connected,
            "connected_since": self.connected_since,
            "uptime": self.uptime(),
            "gateway_url": self.gateway_url,
            "last_disconnect": self.last_disconnect,
            "last_disconnect_reason": self.last_disconnect_reason,
            "reconnect_attempts": self.reconnect_attempts,
            "session": {
                "started_at": self.session_started_at,
                "uptime": self.session_uptime(),
                "actions_count": self.actions_count_session,
                "ok": self.actions_ok_session,
                "error": self.actions_error_session,
                "denied": self.actions_denied_session,
            },
            "last_action": self.last_action,
            "killswitch": self.killswitch,
            "panic_mode": self.panic_mode,
            "panic_triggered_at": self.panic_triggered_at,
        }
