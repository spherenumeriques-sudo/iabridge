"""Configuration du logging pour l'agent + l'UI."""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_file: Path, level: int = logging.INFO) -> logging.Logger:
    """Configure le logging racine : console + fichier."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    # Évite les doublons si setup_logging est appelé plusieurs fois
    if not any(getattr(h, "_iabridge", False) for h in root.handlers):
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh._iabridge = True  # type: ignore[attr-defined]
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        ch._iabridge = True  # type: ignore[attr-defined]
        root.addHandler(fh)
        root.addHandler(ch)
    return logging.getLogger("iabridge")
