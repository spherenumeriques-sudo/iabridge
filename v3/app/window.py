"""Fenêtre native pywebview pour IABridge Desktop.

pywebview lance une fenêtre native (WebView2 sur Windows, WebKit sur
macOS, GTK/Qt sur Linux) qui charge l'UI servie en local par le backend
FastAPI sur 127.0.0.1:9999.

Contrainte architecturale : `webview.start()` doit tourner sur le
*main thread* (exigence Windows). L'event loop asyncio (WS client +
uvicorn + db) est donc déplacé dans un thread dédié, et le main thread
reste libre pour la fenêtre native.

Usage attendu (voir main.py) :

    if has_webview():
        run_with_window(amain_coro)
    else:
        asyncio.run(amain_coro())   # fallback headless

Dépendance : `pip install pywebview` côté Windows. Sans pywebview,
`has_webview()` retourne False et l'app tourne en mode headless —
le dashboard reste accessible via http://127.0.0.1:9999/ dans un
navigateur classique, pratique pour dev sur Linux.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Coroutine


log = logging.getLogger("iabridge.window")


def has_webview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except ImportError:
        return False


def run_with_window(
    amain_factory: Callable[[], Coroutine[Any, Any, int]],
    *,
    title: str = "IABridge",
    url: str = "http://127.0.0.1:9999/",
    width: int = 1440,
    height: int = 900,
    min_width: int = 1100,
    min_height: int = 680,
) -> int:
    """Lance la fenêtre native + l'event loop asyncio en parallèle.

    `amain_factory` est une callable qui renvoie la coroutine principale
    de l'app (ex: `amain` dans main.py). Elle est exécutée dans un thread
    séparé avec son propre event loop asyncio.

    La fonction bloque jusqu'à la fermeture de la fenêtre native puis
    demande l'arrêt du backend avant de retourner.
    """
    import webview  # type: ignore

    loop: asyncio.AbstractEventLoop | None = None
    ready = threading.Event()
    result_box: dict[str, int] = {"rc": 0}
    stop_event_box: dict[str, asyncio.Event | None] = {"ev": None}

    async def _amain_wrapper() -> int:
        stop_event_box["ev"] = asyncio.Event()
        # amain_factory() retourne la coroutine ; on la lance en tâche
        # pour pouvoir lui demander stop() depuis l'extérieur
        main_task = asyncio.create_task(amain_factory(), name="iabridge.amain")
        ready.set()
        try:
            return await main_task
        except asyncio.CancelledError:
            return 0

    def _run_asyncio() -> None:
        nonlocal loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result_box["rc"] = loop.run_until_complete(_amain_wrapper())
        except Exception as e:
            log.exception("Erreur dans le thread asyncio : %s", e)
            result_box["rc"] = 1
        finally:
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_run_asyncio, name="iabridge.asyncio", daemon=True)
    t.start()

    # Attendre que l'event loop soit prêt avant de créer la fenêtre
    # (sinon webview pourrait charger l'URL avant que le backend
    # uvicorn n'ait fini de binder sur 127.0.0.1:9999)
    ready.wait(timeout=5)

    # Attendre aussi que le backend HTTP réponde avant d'ouvrir la fenêtre
    _wait_backend_ready(url, timeout=10)

    window = webview.create_window(
        title=title,
        url=url,
        width=width,
        height=height,
        min_size=(min_width, min_height),
        background_color="#050810",
        text_select=True,
        easy_drag=False,
    )

    def _on_closed() -> None:
        log.info("Fenêtre fermée, arrêt de l'event loop")
        if loop is not None and loop.is_running():
            # Signale l'arrêt au main asyncio — le stop_event est défini
            # dans amain, pas ici, donc on cancel toutes les tâches
            for task in asyncio.all_tasks(loop):
                loop.call_soon_threadsafe(task.cancel)

    window.events.closed += _on_closed

    # webview.start() est bloquant jusqu'à fermeture de la fenêtre.
    try:
        webview.start(gui="edgechromium" if _is_windows() else None)
    except Exception as e:
        log.warning("webview.start() erreur (%s), fallback navigateur", e)
        # pywebview inutilisable (pythonnet manquant sur Python 3.14+)
        # → on ouvre le dashboard dans le navigateur par défaut
        import webbrowser
        webbrowser.open(url)
        log.info("Dashboard ouvert dans le navigateur : %s", url)
        # Attendre que le thread asyncio se termine
        t.join()

    # Fenêtre fermée → attendre que le thread asyncio se termine
    t.join(timeout=5)
    return result_box["rc"]


def _wait_backend_ready(url: str, timeout: float = 10.0) -> None:
    """Attend que le backend FastAPI réponde avant d'ouvrir la fenêtre."""
    import time
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.1)


def _is_windows() -> bool:
    import platform
    return platform.system() == "Windows"
