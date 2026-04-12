"""System tray Windows pour IABridge — icône dans la barre des tâches.

Fonctionnalités :
  - Icône tray avec le logo IA dégradé
  - Menu clic droit : Ouvrir / Statut / Killswitch / Quitter
  - Notifications toast sur événements critiques
  - Minimiser la fenêtre pywebview → l'app reste en tray

Dépendance : `pip install pystray pillow` côté Windows.
Sans pystray, `has_tray()` retourne False et le tray n'est pas créé.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable


log = logging.getLogger("iabridge.tray")


def has_tray() -> bool:
    try:
        import pystray  # noqa: F401
        return True
    except ImportError:
        return False


def _create_icon_image() -> Any:
    """Génère l'icône tray en mémoire (64x64 dégradé violet/cyan avec IA)."""
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Fond arrondi avec dégradé simple (2 bandes)
    for y in range(size):
        ratio = y / size
        r = int(0 + 122 * ratio)
        g = int(212 - 120 * ratio)
        b = int(255 - (255 - 255) * ratio)
        draw.line([(8, y), (size - 8, y)], fill=(r, g, b, 240))

    # Coins arrondis via masque
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=255)
    img.putalpha(mask)

    # Texte IA
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except (IOError, OSError):
        font = ImageFont.load_default()
    draw.text((size // 2, size // 2), "IA", fill=(255, 255, 255, 255), anchor="mm", font=font)

    # Point vert connecté
    draw.ellipse([size - 18, 2, size - 6, 14], fill=(0, 255, 136, 255))

    return img


class TrayIcon:
    """Wrapper pystray pour IABridge."""

    def __init__(
        self,
        on_open: Callable[[], None] | None = None,
        on_killswitch: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.on_open = on_open
        self.on_killswitch = on_killswitch
        self.on_quit = on_quit
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not has_tray():
            log.warning("pystray non disponible, tray non créé")
            return
        import pystray
        from pystray import MenuItem as MI

        icon_image = _create_icon_image()
        menu = pystray.Menu(
            MI("Ouvrir IABridge", self._do_open, default=True),
            pystray.Menu.SEPARATOR,
            MI("Killswitch ON/OFF", self._do_killswitch),
            pystray.Menu.SEPARATOR,
            MI("Quitter", self._do_quit),
        )
        self._icon = pystray.Icon("iabridge", icon_image, "IABridge", menu)
        self._thread = threading.Thread(target=self._icon.run, name="tray", daemon=True)
        self._thread.start()
        log.info("Tray icon démarré")

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception as e:
                log.warning("Notification tray impossible : %s", e)

    def _do_open(self, icon: Any = None, item: Any = None) -> None:
        if self.on_open:
            self.on_open()

    def _do_killswitch(self, icon: Any = None, item: Any = None) -> None:
        if self.on_killswitch:
            self.on_killswitch()

    def _do_quit(self, icon: Any = None, item: Any = None) -> None:
        if self.on_quit:
            self.on_quit()
