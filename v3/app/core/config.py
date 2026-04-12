"""Chargement config + trust.json pour IABridge app.

Compatible avec l'ancien agent.py : lit les mêmes fichiers au même endroit
(%APPDATA%\\IABridge\\agent.json sur Windows). Pas de migration nécessaire.
"""
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


def get_config_dir() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "IABridge"
    return Path.home() / ".config" / "iabridge"


CONFIG_DIR = get_config_dir()
CONFIG_FILE = CONFIG_DIR / "agent.json"
TRUST_FILE = CONFIG_DIR / "trust.json"
LOG_FILE = CONFIG_DIR / "agent.log"
DB_FILE = CONFIG_DIR / "iabridge.db"


# Actions sensibles par défaut — "allow" | "deny" | "ask"
DEFAULT_TRUST: dict[str, str] = {
    "delete": "ask",
    "fs_move": "ask",
    "run": "ask",
    "kill_process": "ask",
    "clean_temp": "ask",
    "clean_recycle_bin": "ask",
    "winget": "ask",
    "services": "ask",
    "write_file": "allow",
}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_config_dir()
    defaults = {
        "gateway_url": os.environ.get("IABRIDGE_GATEWAY", "wss://labonneocaz.fr/iabridge/ws"),
        "token": os.environ.get("IABRIDGE_TOKEN", ""),
        "agent_name": platform.node() or "agent",
    }
    if CONFIG_FILE.exists():
        try:
            # utf-8-sig : tolère le BOM que PowerShell ajoute parfois
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except json.JSONDecodeError as e:
            print(f"[warn] config JSON invalide : {e}", file=sys.stderr)
    return defaults


def save_config(cfg: dict[str, Any]) -> None:
    ensure_config_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def load_trust() -> dict[str, str]:
    trust = dict(DEFAULT_TRUST)
    if TRUST_FILE.exists():
        try:
            data = json.loads(TRUST_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                trust.update({k: str(v) for k, v in data.items() if v in ("allow", "deny", "ask")})
        except json.JSONDecodeError:
            pass
    return trust


def save_trust(trust: dict[str, str]) -> None:
    ensure_config_dir()
    TRUST_FILE.write_text(json.dumps(trust, indent=2, ensure_ascii=False), encoding="utf-8")
