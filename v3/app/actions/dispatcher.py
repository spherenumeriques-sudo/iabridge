"""Dispatcher des actions IABridge.

Stratégie Session 1 : réutilise les 43 handlers de l'ancien
`v3/agent/agent.py` via import dynamique au lieu de les réécrire.
Le refactor complet en modules thématiques (system/files/browser/…)
arrivera en Session 2.

Sur une plateforme où l'ancien agent ne peut pas s'importer (ex: VPS
Linux headless sans pyautogui), le dispatcher tombe en mode stub :
toutes les commandes retournent `{"error": "not available on this platform"}`
mais le reste de l'app (UI, DB, backend) continue de fonctionner
normalement — utile pour dev et tests.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from core.state import AppState
from core.config import load_trust
from storage import Database


log = logging.getLogger("iabridge.dispatcher")


class Dispatcher:
    """Route les commandes entrantes vers les handlers et log le résultat."""

    def __init__(self, state: AppState, db: Database, legacy_agent_path: Path | None = None) -> None:
        self.state = state
        self.db = db
        self._legacy: Any | None = None
        self._legacy_available = False
        if legacy_agent_path is not None:
            self._load_legacy(legacy_agent_path)

    def _load_legacy(self, path: Path) -> None:
        """Import dynamique de l'ancien agent.py comme module isolé."""
        if not path.exists():
            log.warning("Legacy agent introuvable : %s", path)
            return
        try:
            spec = importlib.util.spec_from_file_location("iabridge_legacy", path)
            if spec is None or spec.loader is None:
                return
            mod = importlib.util.module_from_spec(spec)
            sys.modules["iabridge_legacy"] = mod
            spec.loader.exec_module(mod)
            self._legacy = mod
            self._legacy_available = True
            log.info("Legacy agent chargé depuis %s", path)
        except Exception as e:
            log.warning("Import legacy agent impossible (%s) — mode stub activé", e)
            self._legacy = None
            self._legacy_available = False

    def is_legacy_available(self) -> bool:
        return self._legacy_available

    async def dispatch(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Entrée principale : reçoit une commande, applique trust, exécute, log."""
        action = str(raw.get("action", "")).strip()
        cmd_id = raw.get("id")
        params = {k: v for k, v in raw.items() if k not in ("action", "id")}
        t0 = time.time()

        # Killswitch / Panic mode : bloque tout
        if self.state.killswitch or self.state.panic_mode:
            reason = "panic_mode" if self.state.panic_mode else "killswitch"
            response = {"id": cmd_id, "error": f"blocked by {reason}", "blocked": True}
            await self._log(action, params, "denied", t0, error=reason)
            return response

        # Vérif trust.json
        trust = load_trust()
        mode = trust.get(action, "allow")
        if mode == "deny":
            response = {"id": cmd_id, "error": "action denied by trust policy", "blocked": True}
            await self._log(action, params, "denied", t0, error="trust=deny")
            return response
        # "ask" : en attendant l'UI de confirmation → traité comme allow pour l'instant
        # TODO Session 2 : implémenter la boucle d'approbation via l'UI

        # Exécution
        if not self._legacy_available:
            response = {
                "id": cmd_id,
                "error": "legacy agent not loaded (dispatcher stub mode)",
                "stub": True,
            }
            await self._log(action, params, "error", t0, error="stub")
            return response

        try:
            result = await self._legacy.process_command(raw)  # type: ignore[attr-defined]
            status = "error" if isinstance(result, dict) and "error" in result else "ok"
            await self._log(action, params, status, t0, result=result)
            return result if isinstance(result, dict) else {"id": cmd_id, "result": result}
        except Exception as e:
            log.exception("Erreur pendant dispatch %s", action)
            err = {"id": cmd_id, "error": f"{type(e).__name__}: {e}"}
            await self._log(action, params, "error", t0, error=str(e))
            return err

    async def _log(
        self,
        action: str,
        params: dict[str, Any],
        status: str,
        t0: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        duration_ms = int((time.time() - t0) * 1000)
        self.state.record_action(action, status, duration_ms)
        try:
            await self.db.log_action(action, params, status, duration_ms, result, error)
        except Exception as e:
            log.warning("Impossible d'écrire l'action en DB : %s", e)
