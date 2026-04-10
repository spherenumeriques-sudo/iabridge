#!/usr/bin/env python3
"""
IABridge v3 — Agent Windows

Se connecte au gateway VPS via WebSocket sécurisé (wss://) et maintient
la connexion ouverte en permanence, avec reconnexion automatique.

Architecture :
    Agent (ici) ──wss──▶ Gateway VPS ◀── Claude / CLI

Lancement :
    python agent.py
    (ou double-clic sur l'exe quand packagé)

Configuration :
    La première fois, demande l'URL du gateway et le token, sauve dans
    %APPDATA%\\IABridge\\config.json (Windows) ou ~/.config/iabridge/agent.json (Linux).
"""
import asyncio
import base64
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# Dépendances externes
try:
    import websockets
except ImportError:
    print("Dépendance manquante : pip install websockets", file=sys.stderr)
    sys.exit(1)

# Modules optionnels — l'agent marche en mode dégradé s'ils manquent
_HAS_MSS = False
_HAS_PYAUTOGUI = False
_HAS_PYPERCLIP = False
_HAS_PIL = False
_HAS_PSUTIL = False
_HAS_WIN32 = False
try:
    import mss  # type: ignore
    _HAS_MSS = True
except ImportError:
    pass
try:
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.02
    _HAS_PYAUTOGUI = True
except ImportError:
    pass
try:
    import pyperclip  # type: ignore
    _HAS_PYPERCLIP = True
except ImportError:
    pass
try:
    from PIL import Image  # type: ignore
    _HAS_PIL = True
except ImportError:
    pass
try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except ImportError:
    pass
try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    import win32process  # type: ignore
    _HAS_WIN32 = True
except ImportError:
    pass

_HAS_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright, Page, Browser, BrowserContext  # type: ignore
    _HAS_PLAYWRIGHT = True
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────

def get_config_dir() -> Path:
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "IABridge"
    return Path.home() / ".config" / "iabridge"


CONFIG_DIR = get_config_dir()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "agent.json"
LOG_FILE = CONFIG_DIR / "agent.log"
TRUST_FILE = CONFIG_DIR / "trust.json"

# Actions sensibles par défaut : demander confirmation à l'utilisateur
# Valeurs possibles : "allow" | "deny" | "ask"
DEFAULT_TRUST: dict[str, str] = {
    "delete": "ask",
    "fs_move": "ask",
    "run": "ask",
    "kill_process": "ask",
    "clean_temp": "ask",
    "clean_recycle_bin": "ask",
    "winget": "ask",
    "services": "ask",
    "write_file": "allow",  # utilisé par Claude pour mettre à jour l'agent lui-même
}


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
    TRUST_FILE.write_text(json.dumps(trust, indent=2), encoding="utf-8")


def load_config() -> dict[str, Any]:
    defaults = {
        "gateway_url": os.environ.get("IABRIDGE_GATEWAY", "wss://labonneocaz.fr/iabridge/ws"),
        "token": os.environ.get("IABRIDGE_TOKEN", ""),
        "agent_name": platform.node() or "agent",
    }
    if CONFIG_FILE.exists():
        try:
            # utf-8-sig tolère le BOM UTF-8 que PowerShell ajoute quand il écrit un fichier
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except json.JSONDecodeError as e:
            print(f"[warn] config JSON invalide : {e}", file=sys.stderr)
    return defaults


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except Exception:
        pass


def interactive_setup() -> dict[str, Any]:
    """Configuration interactive si lancé manuellement sans config existante."""
    print("=" * 60)
    print("IABridge v3 — Première configuration")
    print("=" * 60)
    cfg = load_config()
    gw = input(f"URL du gateway [{cfg['gateway_url']}] : ").strip() or cfg["gateway_url"]
    token = input("Token d'authentification : ").strip()
    name = input(f"Nom de cette machine [{cfg['agent_name']}] : ").strip() or cfg["agent_name"]
    cfg = {"gateway_url": gw, "token": token, "agent_name": name}
    save_config(cfg)
    print(f"Configuration sauvegardée dans {CONFIG_FILE}")
    return cfg


# ── Logging ───────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("iabridge.agent")


# ── Utilitaires écran (DPI scaling) ──────────────────────────────────────

def get_scale_factor() -> tuple[float, float]:
    if not (_HAS_PYAUTOGUI and _HAS_MSS):
        return 1.0, 1.0
    logical_w, logical_h = pyautogui.size()
    with mss.mss() as sct:
        try:
            m = sct.monitors[1]
            phys_w, phys_h = m["width"], m["height"]
        except IndexError:
            return 1.0, 1.0
    return phys_w / logical_w, phys_h / logical_h


SCALE_X, SCALE_Y = get_scale_factor()


def phys_to_logical(x: int, y: int) -> tuple[int, int]:
    return int(x / SCALE_X), int(y / SCALE_Y)


# ── Handlers des actions ──────────────────────────────────────────────────
#
# Chaque handler reçoit les paramètres de la commande (dict) et doit retourner
# un dict de résultat. Exception → le runner renvoie {"error": str(e)}.

def require(*modules: bool, name: str) -> None:
    if not all(modules):
        raise RuntimeError(f"Module {name} non disponible (dépendance manquante)")


def handle_health(params: dict[str, Any]) -> dict[str, Any]:
    info: dict[str, Any] = {
        "status": "ok",
        "agent_version": "3.0",
        "platform": platform.system(),
        "release": platform.release(),
        "hostname": platform.node(),
        "python": sys.version.split()[0],
        "modules": {
            "mss": _HAS_MSS,
            "pyautogui": _HAS_PYAUTOGUI,
            "pyperclip": _HAS_PYPERCLIP,
            "pillow": _HAS_PIL,
            "psutil": _HAS_PSUTIL,
            "win32": _HAS_WIN32,
            "playwright": _HAS_PLAYWRIGHT,
        },
    }
    if _HAS_MSS:
        try:
            with mss.mss() as sct:
                m = sct.monitors[1]
                info["screen"] = f"{m['width']}x{m['height']}"
        except Exception:
            pass
    if _HAS_PYAUTOGUI:
        info["logical_size"] = "{}x{}".format(*pyautogui.size())
        info["scale"] = {"x": SCALE_X, "y": SCALE_Y}
    return info


def handle_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_MSS, _HAS_PIL, name="screenshot")
    fmt = str(params.get("format", "jpeg")).lower()
    if fmt not in ("png", "jpeg"):
        raise ValueError("format must be png or jpeg")
    quality = max(1, min(95, int(params.get("quality", 80))))
    region = params.get("region")  # {"x":..., "y":..., "w":..., "h":...}

    with mss.mss() as sct:
        if region:
            target = {
                "left": int(region["x"]),
                "top": int(region["y"]),
                "width": int(region["w"]),
                "height": int(region["h"]),
            }
        else:
            target = sct.monitors[1]
        sct_img = sct.grab(target)
        w, h = sct_img.width, sct_img.height
        img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")

    buffer = io.BytesIO()
    if fmt == "jpeg":
        img.save(buffer, format="JPEG", quality=quality)
    else:
        img.save(buffer, format="PNG", optimize=False)
    buffer.seek(0)
    b64 = base64.b64encode(buffer.read()).decode("ascii")
    return {"image": b64, "width": w, "height": h, "format": fmt, "bytes": len(b64)}


def handle_click(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, name="click")
    x = int(params["x"])
    y = int(params["y"])
    button = params.get("button", "left")
    clicks = int(params.get("clicks", 1))
    if button not in ("left", "right", "middle"):
        raise ValueError("button must be left, right or middle")
    lx, ly = phys_to_logical(x, y)
    pyautogui.click(x=lx, y=ly, clicks=clicks, button=button, interval=0.05)
    return {"status": "ok", "logical_x": lx, "logical_y": ly, "button": button}


def handle_doubleclick(params: dict[str, Any]) -> dict[str, Any]:
    return handle_click({**params, "clicks": 2})


def handle_move(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, name="move")
    x = int(params["x"])
    y = int(params["y"])
    duration = float(params.get("duration", 0.2))
    lx, ly = phys_to_logical(x, y)
    pyautogui.moveTo(lx, ly, duration=duration)
    return {"status": "ok", "logical_x": lx, "logical_y": ly}


def handle_drag(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, name="drag")
    x1 = int(params["x1"]); y1 = int(params["y1"])
    x2 = int(params["x2"]); y2 = int(params["y2"])
    duration = float(params.get("duration", 0.5))
    button = params.get("button", "left")
    lx1, ly1 = phys_to_logical(x1, y1)
    lx2, ly2 = phys_to_logical(x2, y2)
    pyautogui.moveTo(lx1, ly1, duration=0.2)
    pyautogui.dragTo(lx2, ly2, duration=duration, button=button)
    return {"status": "ok"}


def handle_scroll(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, name="scroll")
    clicks = int(params.get("clicks", -3))
    x = params.get("x")
    y = params.get("y")
    if x is not None and y is not None:
        lx, ly = phys_to_logical(int(x), int(y))
        pyautogui.scroll(clicks, x=lx, y=ly)
    else:
        pyautogui.scroll(clicks)
    return {"status": "ok", "clicks": clicks}


def handle_type(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, _HAS_PYPERCLIP, name="type")
    text = str(params["text"])
    old_clip = pyperclip.paste()
    try:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)
    finally:
        try:
            pyperclip.copy(old_clip)
        except Exception:
            pass
    return {"status": "ok", "length": len(text)}


def handle_key(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYAUTOGUI, name="key")
    key = params["key"]
    if isinstance(key, list):
        parts = [str(k).strip() for k in key]
    elif isinstance(key, str):
        parts = [k.strip() for k in key.split("+")] if "+" in key else [key.strip()]
    else:
        raise ValueError("key must be string or list")
    if len(parts) > 1:
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])
    return {"status": "ok", "key": parts}


def handle_clipboard_get(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYPERCLIP, name="clipboard")
    return {"status": "ok", "text": pyperclip.paste()}


def handle_clipboard_set(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PYPERCLIP, name="clipboard")
    pyperclip.copy(str(params["text"]))
    return {"status": "ok"}


# ── Module 2 — Système de fichiers ───────────────────────────────────────

def _resolve(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path))).resolve()


def handle_list_dir(params: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(str(params["path"]))
    if not p.exists():
        raise FileNotFoundError(str(p))
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    entries = []
    for child in sorted(p.iterdir()):
        try:
            st = child.stat()
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
                "size": st.st_size if not child.is_dir() else None,
                "mtime": int(st.st_mtime),
            })
        except (PermissionError, OSError):
            entries.append({"name": child.name, "path": str(child), "error": "access"})
    return {"status": "ok", "path": str(p), "entries": entries, "count": len(entries)}


def handle_read_file(params: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(str(params["path"]))
    binary = bool(params.get("binary", False))
    max_size = int(params.get("max_size", 10 * 1024 * 1024))  # 10 MB par défaut
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    size = p.stat().st_size
    if size > max_size:
        raise ValueError(f"File too large: {size} > {max_size}")
    data = p.read_bytes()
    if binary:
        return {"status": "ok", "path": str(p), "size": size, "content_b64": base64.b64encode(data).decode("ascii")}
    try:
        text = data.decode("utf-8")
        return {"status": "ok", "path": str(p), "size": size, "content": text}
    except UnicodeDecodeError:
        return {"status": "ok", "path": str(p), "size": size, "content_b64": base64.b64encode(data).decode("ascii"), "note": "binary"}


def handle_write_file(params: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(str(params["path"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    if "content_b64" in params:
        data = base64.b64decode(params["content_b64"])
        p.write_bytes(data)
        size = len(data)
    elif "content" in params:
        text = str(params["content"])
        p.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
    else:
        raise ValueError("Provide content (text) or content_b64 (binary)")
    return {"status": "ok", "path": str(p), "size": size}


def handle_delete(params: dict[str, Any]) -> dict[str, Any]:
    p = _resolve(str(params["path"]))
    recursive = bool(params.get("recursive", False))
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.is_dir():
        if recursive:
            shutil.rmtree(p)
        else:
            p.rmdir()
    else:
        p.unlink()
    return {"status": "ok", "path": str(p)}


def handle_move(params: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(str(params["src"]))
    dst = _resolve(str(params["dst"]))
    if not src.exists():
        raise FileNotFoundError(str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"status": "ok", "src": str(src), "dst": str(dst)}


def handle_copy(params: dict[str, Any]) -> dict[str, Any]:
    src = _resolve(str(params["src"]))
    dst = _resolve(str(params["dst"]))
    if not src.exists():
        raise FileNotFoundError(str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(str(src), str(dst))
    else:
        shutil.copy2(str(src), str(dst))
    return {"status": "ok", "src": str(src), "dst": str(dst)}


def handle_run(params: dict[str, Any]) -> dict[str, Any]:
    """Exécuter une commande shell. Désactivé par défaut via env var."""
    if os.environ.get("IABRIDGE_ALLOW_RUN", "1") != "1":
        raise PermissionError("/run désactivé : set IABRIDGE_ALLOW_RUN=1")
    command = str(params["command"])
    timeout = int(params.get("timeout", 30))
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "status": "ok",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


# ── Module 3 — Info et contrôle système ─────────────────────────────────

def handle_sys_info(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PSUTIL, name="sys_info")
    cpu = {
        "count_physical": psutil.cpu_count(logical=False),
        "count_logical": psutil.cpu_count(logical=True),
        "percent": psutil.cpu_percent(interval=0.3),
        "freq_mhz": psutil.cpu_freq().current if psutil.cpu_freq() else None,
    }
    mem = psutil.virtual_memory()
    memory = {
        "total": mem.total,
        "available": mem.available,
        "used": mem.used,
        "percent": mem.percent,
    }
    swap_info = psutil.swap_memory()
    swap = {"total": swap_info.total, "used": swap_info.used, "percent": swap_info.percent}
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        except PermissionError:
            pass
    battery_info = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery_info = {
                "percent": b.percent,
                "plugged": b.power_plugged,
                "secs_left": b.secsleft if b.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except Exception:
        pass
    return {
        "status": "ok",
        "cpu": cpu,
        "memory": memory,
        "swap": swap,
        "disks": disks,
        "battery": battery_info,
        "boot_time": int(psutil.boot_time()),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


def handle_processes(params: dict[str, Any]) -> dict[str, Any]:
    """Liste les processus triés par CPU% ou RAM%."""
    require(_HAS_PSUTIL, name="processes")
    sort_by = params.get("sort", "cpu")  # cpu | memory | name
    limit = int(params.get("limit", 30))
    # Première passe pour initialiser les compteurs cpu_percent
    procs = []
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            p.cpu_percent(interval=None)
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.3)
    entries = []
    for p in procs:
        try:
            with p.oneshot():
                entries.append({
                    "pid": p.pid,
                    "name": p.info.get("name"),
                    "user": p.info.get("username"),
                    "cpu_percent": round(p.cpu_percent(interval=None), 1),
                    "memory_mb": round(p.memory_info().rss / 1024 / 1024, 1),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if sort_by == "cpu":
        entries.sort(key=lambda e: e["cpu_percent"], reverse=True)
    elif sort_by == "memory":
        entries.sort(key=lambda e: e["memory_mb"], reverse=True)
    else:
        entries.sort(key=lambda e: (e["name"] or "").lower())
    return {"status": "ok", "count": len(entries), "processes": entries[:limit]}


def handle_kill_process(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_PSUTIL, name="kill_process")
    force = bool(params.get("force", False))
    killed = []
    errors = []
    if "pid" in params:
        targets = [int(params["pid"])]
        for pid in targets:
            try:
                p = psutil.Process(pid)
                if force:
                    p.kill()
                else:
                    p.terminate()
                killed.append({"pid": pid, "name": p.name()})
            except Exception as e:
                errors.append({"pid": pid, "error": str(e)})
    elif "name" in params:
        name = str(params["name"]).lower()
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == name:
                    if force:
                        p.kill()
                    else:
                        p.terminate()
                    killed.append({"pid": p.pid, "name": p.info.get("name")})
            except Exception as e:
                errors.append({"pid": p.pid, "error": str(e)})
    else:
        raise ValueError("Provide 'pid' or 'name'")
    return {"status": "ok", "killed": killed, "errors": errors}


def handle_windows_list(params: dict[str, Any]) -> dict[str, Any]:
    """Liste les fenêtres visibles avec leur titre et process."""
    require(_HAS_WIN32, name="windows_list")
    windows = []

    def callback(hwnd: int, _: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = None
            if _HAS_PSUTIL:
                try:
                    proc_name = psutil.Process(pid).name()
                except Exception:
                    pass
            rect = win32gui.GetWindowRect(hwnd)
            windows.append({
                "hwnd": hwnd,
                "title": title,
                "pid": pid,
                "process": proc_name,
                "x": rect[0], "y": rect[1],
                "w": rect[2] - rect[0], "h": rect[3] - rect[1],
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return {"status": "ok", "count": len(windows), "windows": windows}


def _find_hwnd(params: dict[str, Any]) -> int:
    if "hwnd" in params:
        return int(params["hwnd"])
    if "title" in params:
        require(_HAS_WIN32, name="window_by_title")
        needle = str(params["title"]).lower()
        found = []
        def cb(hwnd: int, _: Any) -> bool:
            t = win32gui.GetWindowText(hwnd)
            if needle in t.lower():
                found.append(hwnd)
            return True
        win32gui.EnumWindows(cb, None)
        if not found:
            raise ValueError(f"No window matching '{params['title']}'")
        return found[0]
    raise ValueError("Provide 'hwnd' or 'title'")


def handle_focus_window(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_WIN32, name="focus_window")
    hwnd = _find_hwnd(params)
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    return {"status": "ok", "hwnd": hwnd}


def handle_minimize_window(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_WIN32, name="minimize_window")
    hwnd = _find_hwnd(params)
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    return {"status": "ok", "hwnd": hwnd}


def handle_close_window(params: dict[str, Any]) -> dict[str, Any]:
    require(_HAS_WIN32, name="close_window")
    hwnd = _find_hwnd(params)
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return {"status": "ok", "hwnd": hwnd}


# ── Module 4 — Nettoyage et optimisation PC ──────────────────────────────

def handle_clean_temp(params: dict[str, Any]) -> dict[str, Any]:
    """Vide les dossiers temp utilisateur et système (ce qui est supprimable)."""
    dry_run = bool(params.get("dry_run", False))
    targets = [
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        r"C:\Windows\Temp",
    ]
    seen = set()
    deleted_count = 0
    deleted_bytes = 0
    failed = 0
    for t in targets:
        if not t or t in seen:
            continue
        seen.add(t)
        p = Path(t)
        if not p.exists():
            continue
        for child in p.iterdir():
            try:
                if child.is_file() or child.is_symlink():
                    size = child.stat().st_size
                    if not dry_run:
                        child.unlink()
                    deleted_count += 1
                    deleted_bytes += size
                elif child.is_dir():
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                    if not dry_run:
                        shutil.rmtree(child, ignore_errors=True)
                    deleted_count += 1
                    deleted_bytes += size
            except (PermissionError, OSError):
                failed += 1
    return {
        "status": "ok",
        "dry_run": dry_run,
        "deleted_entries": deleted_count,
        "deleted_bytes": deleted_bytes,
        "deleted_mb": round(deleted_bytes / 1024 / 1024, 1),
        "failed": failed,
        "targets": list(seen),
    }


def handle_clean_recycle_bin(params: dict[str, Any]) -> dict[str, Any]:
    """Vide la corbeille Windows via la commande PowerShell Clear-RecycleBin."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
        capture_output=True, text=True, timeout=60,
    )
    return {"status": "ok", "stdout": result.stdout, "stderr": result.stderr}


def handle_startup_list(params: dict[str, Any]) -> dict[str, Any]:
    """Liste les programmes qui se lancent au démarrage."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_StartupCommand | Select-Object Name,Command,Location,User | ConvertTo-Json -Depth 2"],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        data = []
    if isinstance(data, dict):
        data = [data]
    return {"status": "ok", "count": len(data), "entries": data}


def handle_winget(params: dict[str, Any]) -> dict[str, Any]:
    """Wrapper winget. op = list | upgrade-list | upgrade-all | install | uninstall"""
    op = params.get("op", "list")
    args = params.get("args", "")
    accept = "--accept-source-agreements --accept-package-agreements"
    if op == "list":
        cmd = f"winget list --disable-interactivity"
    elif op == "upgrade-list":
        cmd = f"winget upgrade --disable-interactivity {accept}"
    elif op == "upgrade-all":
        cmd = f"winget upgrade --all --silent --disable-interactivity {accept}"
    elif op == "install":
        if not args:
            raise ValueError("'args' required (package id)")
        cmd = f"winget install {args} --silent --disable-interactivity {accept}"
    elif op == "uninstall":
        if not args:
            raise ValueError("'args' required (package id)")
        cmd = f"winget uninstall {args} --silent --disable-interactivity {accept}"
    else:
        raise ValueError(f"Unknown op: {op}")
    timeout = int(params.get("timeout", 300))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return {
        "status": "ok",
        "op": op,
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def handle_services(params: dict[str, Any]) -> dict[str, Any]:
    """Lister / démarrer / arrêter un service Windows. op = list | start | stop | restart"""
    op = params.get("op", "list")
    name = params.get("name")
    if op == "list":
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Service | Where-Object {$_.Status -eq 'Running'} | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Depth 2"],
            capture_output=True, text=True, timeout=30,
        )
        try:
            data = json.loads(result.stdout) if result.stdout.strip() else []
        except json.JSONDecodeError:
            data = []
        return {"status": "ok", "count": len(data) if isinstance(data, list) else 1, "services": data}
    if not name:
        raise ValueError("'name' required")
    ps_op = {"start": "Start-Service", "stop": "Stop-Service", "restart": "Restart-Service"}.get(op)
    if not ps_op:
        raise ValueError(f"Unknown op: {op}")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"{ps_op} -Name '{name}'"],
        capture_output=True, text=True, timeout=60,
    )
    return {"status": "ok", "op": op, "name": name, "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def handle_open_url(params: dict[str, Any]) -> dict[str, Any]:
    """Ouvre une URL dans le navigateur par défaut."""
    url = str(params["url"])
    os.startfile(url)  # type: ignore
    return {"status": "ok", "url": url}


def handle_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Affiche une notification toast Windows."""
    title = str(params.get("title", "IABridge"))
    message = str(params["message"])
    # Toast via PowerShell + BurntToast indisponible → fallback MessageBox
    if _HAS_WIN32:
        try:
            import win32api  # type: ignore
            win32api.MessageBox(0, message, title, 0x40)  # MB_ICONINFORMATION
            return {"status": "ok"}
        except Exception:
            pass
    # Fallback : PowerShell toast
    ps = f'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show("{message}","{title}")
'''
    subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
    return {"status": "ok"}


# ── Module 5 — Automation navigateur (Playwright) ────────────────────────
#
# État global partagé : une seule instance Playwright+browser+context+page.
# Permet de garder les cookies, sessions et l'état du navigateur entre les
# commandes — indispensable pour rester connecté à Facebook/LinkedIn/etc.
# Le browser se lance au premier appel à browser_*, et reste vivant jusqu'à
# browser_close ou arrêt de l'agent.

_browser_lock: asyncio.Lock | None = None
_browser_state: dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
}


def _get_browser_lock() -> asyncio.Lock:
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


async def _ensure_browser(headless: bool = False) -> "Page":
    if not _HAS_PLAYWRIGHT:
        raise RuntimeError("playwright non installé")
    async with _get_browser_lock():
        if _browser_state["browser"] is None:
            pw = await async_playwright().start()
            _browser_state["playwright"] = pw
            browser = await pw.chromium.launch(headless=headless)
            _browser_state["browser"] = browser
            # User agent réaliste pour éviter les détections basiques
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                locale="fr-FR",
            )
            _browser_state["context"] = context
            page = await context.new_page()
            _browser_state["page"] = page
            log.info("Navigateur lancé (headless=%s)", headless)
    return _browser_state["page"]


async def _close_browser() -> None:
    async with _get_browser_lock():
        if _browser_state["browser"] is not None:
            try:
                await _browser_state["context"].close()
                await _browser_state["browser"].close()
                await _browser_state["playwright"].stop()
            except Exception as e:
                log.warning("Fermeture navigateur : %s", e)
            _browser_state.update({"playwright": None, "browser": None, "context": None, "page": None})


async def handle_browser_open(params: dict[str, Any]) -> dict[str, Any]:
    headless = bool(params.get("headless", False))
    page = await _ensure_browser(headless)
    return {"status": "ok", "url": page.url, "title": await page.title()}


async def handle_browser_goto(params: dict[str, Any]) -> dict[str, Any]:
    page = await _ensure_browser(bool(params.get("headless", False)))
    url = str(params["url"])
    timeout = int(params.get("timeout", 30)) * 1000
    wait_until = params.get("wait_until", "domcontentloaded")
    await page.goto(url, timeout=timeout, wait_until=wait_until)
    return {"status": "ok", "url": page.url, "title": await page.title()}


async def handle_browser_click(params: dict[str, Any]) -> dict[str, Any]:
    page = await _ensure_browser()
    selector = str(params["selector"])
    timeout = int(params.get("timeout", 10)) * 1000
    await page.click(selector, timeout=timeout)
    return {"status": "ok", "selector": selector}


async def handle_browser_fill(params: dict[str, Any]) -> dict[str, Any]:
    page = await _ensure_browser()
    selector = str(params["selector"])
    text = str(params["text"])
    timeout = int(params.get("timeout", 10)) * 1000
    await page.fill(selector, text, timeout=timeout)
    return {"status": "ok", "selector": selector, "length": len(text)}


async def handle_browser_wait(params: dict[str, Any]) -> dict[str, Any]:
    page = await _ensure_browser()
    selector = params.get("selector")
    timeout = int(params.get("timeout", 30)) * 1000
    state = params.get("state", "visible")  # visible | hidden | attached | detached
    if selector:
        await page.wait_for_selector(selector, timeout=timeout, state=state)
        return {"status": "ok", "selector": selector, "state": state}
    await page.wait_for_load_state(params.get("load_state", "networkidle"), timeout=timeout)
    return {"status": "ok"}


async def handle_browser_extract(params: dict[str, Any]) -> dict[str, Any]:
    """Extrait du texte ou HTML depuis un sélecteur CSS, ou toute la page."""
    page = await _ensure_browser()
    selector = params.get("selector")
    mode = params.get("mode", "text")  # text | html | attribute
    attr = params.get("attribute")
    if selector:
        if mode == "text":
            values = await page.eval_on_selector_all(selector, "els => els.map(e => e.innerText)")
        elif mode == "html":
            values = await page.eval_on_selector_all(selector, "els => els.map(e => e.outerHTML)")
        elif mode == "attribute":
            if not attr:
                raise ValueError("'attribute' required for mode=attribute")
            values = await page.eval_on_selector_all(selector, f"els => els.map(e => e.getAttribute('{attr}'))")
        else:
            raise ValueError(f"Unknown mode: {mode}")
        return {"status": "ok", "count": len(values), "values": values}
    # Pas de selector → contenu de la page entière
    if mode == "text":
        content = await page.inner_text("body")
    else:
        content = await page.content()
    return {"status": "ok", "content": content[:200000]}


async def handle_browser_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    page = await _ensure_browser()
    full_page = bool(params.get("full_page", False))
    quality = int(params.get("quality", 80))
    selector = params.get("selector")
    if selector:
        element = await page.wait_for_selector(selector, timeout=5000)
        data = await element.screenshot(type="jpeg", quality=quality)
    else:
        data = await page.screenshot(full_page=full_page, type="jpeg", quality=quality)
    return {
        "status": "ok",
        "image": base64.b64encode(data).decode("ascii"),
        "format": "jpeg",
        "bytes": len(data),
        "url": page.url,
    }


async def handle_browser_script(params: dict[str, Any]) -> dict[str, Any]:
    """Exécute un script JavaScript dans la page et retourne le résultat."""
    page = await _ensure_browser()
    script = str(params["script"])
    result = await page.evaluate(script)
    return {"status": "ok", "result": result}


async def handle_browser_press(params: dict[str, Any]) -> dict[str, Any]:
    """Simuler une touche clavier dans le navigateur (ex: Enter)."""
    page = await _ensure_browser()
    key = str(params["key"])
    selector = params.get("selector")
    if selector:
        await page.press(selector, key)
    else:
        await page.keyboard.press(key)
    return {"status": "ok", "key": key}


async def handle_browser_close(params: dict[str, Any]) -> dict[str, Any]:
    await _close_browser()
    return {"status": "ok"}


async def handle_browser_url(params: dict[str, Any]) -> dict[str, Any]:
    """Renvoie l'URL et le titre courants."""
    if _browser_state["page"] is None:
        return {"status": "ok", "open": False}
    page = _browser_state["page"]
    return {"status": "ok", "open": True, "url": page.url, "title": await page.title()}


# ── Dispatcher ────────────────────────────────────────────────────────────

HANDLERS: dict[str, Any] = {
    # Module 1 — Contrôle de base
    "health": handle_health,
    "screenshot": handle_screenshot,
    "click": handle_click,
    "doubleclick": handle_doubleclick,
    "move": handle_move,
    "drag": handle_drag,
    "scroll": handle_scroll,
    "type": handle_type,
    "key": handle_key,
    "clipboard_get": handle_clipboard_get,
    "clipboard_set": handle_clipboard_set,
    # Module 2 — Fichiers
    "list_dir": handle_list_dir,
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "delete": handle_delete,
    "fs_move": handle_move,
    "copy": handle_copy,
    "run": handle_run,
    # Module 3 — Système
    "sys_info": handle_sys_info,
    "processes": handle_processes,
    "kill_process": handle_kill_process,
    "windows_list": handle_windows_list,
    "focus_window": handle_focus_window,
    "minimize_window": handle_minimize_window,
    "close_window": handle_close_window,
    # Module 4 — Nettoyage & optimisation
    "clean_temp": handle_clean_temp,
    "clean_recycle_bin": handle_clean_recycle_bin,
    "startup_list": handle_startup_list,
    "winget": handle_winget,
    "services": handle_services,
    # Divers
    "open_url": handle_open_url,
    "notify": handle_notify,
    # Module 5 — Automation navigateur Playwright (async)
    "browser_open": handle_browser_open,
    "browser_goto": handle_browser_goto,
    "browser_click": handle_browser_click,
    "browser_fill": handle_browser_fill,
    "browser_wait": handle_browser_wait,
    "browser_extract": handle_browser_extract,
    "browser_screenshot": handle_browser_screenshot,
    "browser_script": handle_browser_script,
    "browser_press": handle_browser_press,
    "browser_close": handle_browser_close,
    "browser_url": handle_browser_url,
}


def _ask_user(action: str, details: str) -> bool:
    """Affiche une MessageBox Windows demandant la confirmation utilisateur.
    Returns True si l'utilisateur clique Oui, False sinon.
    Timeout après 30 secondes → refus par défaut.
    """
    message = f"Claude veut exécuter l'action suivante :\n\n  {action}\n\nDétails : {details}\n\nAutoriser ?"
    title = "IABridge — Confirmation requise"
    if _HAS_WIN32:
        try:
            import win32api  # type: ignore
            # MB_YESNO | MB_ICONQUESTION | MB_SYSTEMMODAL | MB_SETFOREGROUND
            flags = 0x4 | 0x20 | 0x1000 | 0x10000
            rc = win32api.MessageBox(0, message, title, flags)
            return rc == 6  # IDYES
        except Exception as e:
            log.warning("MessageBox failed: %s", e)
            return False
    return False  # par défaut refus si pas de win32


async def process_command(raw: dict[str, Any]) -> dict[str, Any]:
    cmd_id = raw.get("id", "")
    action = raw.get("action", "")
    handler = HANDLERS.get(action)
    if handler is None:
        return {"id": cmd_id, "error": f"unknown action: {action}", "actions": sorted(HANDLERS.keys())}

    # Consultation du fichier de confiance
    trust = load_trust()
    policy = trust.get(action, "allow")
    if policy == "deny":
        return {"id": cmd_id, "action": action, "error": "action denied by trust.json"}
    if policy == "ask":
        details_summary = ", ".join(f"{k}={str(v)[:80]}" for k, v in raw.items() if k not in ("id", "action"))
        allowed = await asyncio.to_thread(_ask_user, action, details_summary or "(pas de paramètres)")
        if not allowed:
            return {"id": cmd_id, "action": action, "error": "user denied the action"}

    start = time.time()
    try:
        # Les handlers browser_* sont async, les autres sync
        if asyncio.iscoroutinefunction(handler):
            result = await handler(raw)
        else:
            result = await asyncio.to_thread(handler, raw)
        duration_ms = (time.time() - start) * 1000
        log.info("action=%s ok (%.0fms)", action, duration_ms)
        _record_action(action, True, duration_ms)
        return {"id": cmd_id, "action": action, **result}
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        log.error("action=%s failed: %s", action, e)
        _record_action(action, False, duration_ms)
        return {
            "id": cmd_id,
            "action": action,
            "error": str(e),
            "error_type": type(e).__name__,
            "trace": traceback.format_exc() if os.environ.get("IABRIDGE_DEBUG") else None,
        }


# ── Boucle principale WebSocket ──────────────────────────────────────────

async def run_agent(cfg: dict[str, Any]) -> None:
    url = cfg["gateway_url"]
    token = cfg["token"]
    info = {
        "name": cfg.get("agent_name", platform.node()),
        "platform": platform.system(),
        "release": platform.release(),
        "version": "3.0",
    }
    backoff = 1.0
    while True:
        try:
            log.info("Connexion à %s", url)
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=32 * 1024 * 1024) as ws:
                # Handshake
                await ws.send(json.dumps({"auth": token, "info": info}))
                resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                resp = json.loads(resp_raw)
                if not resp.get("ok"):
                    log.error("Auth refusée : %s", resp)
                    await asyncio.sleep(30)
                    continue
                log.info("Connecté au gateway %s", resp.get("gateway", "?"))
                backoff = 1.0
                _dashboard_state["connected"] = True
                _dashboard_state["connected_since"] = time.time()

                async for raw in ws:
                    # Check si le dashboard a demandé la fermeture du navigateur
                    if _dashboard_state.pop("_stop_browser_requested", False):
                        try:
                            await _close_browser()
                        except Exception:
                            pass
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("JSON invalide : %s", raw[:200])
                        continue
                    response = await process_command(msg)
                    await ws.send(json.dumps(response))
        except (websockets.ConnectionClosed, asyncio.TimeoutError, OSError) as e:
            log.warning("Déconnecté : %s (reconnect dans %.1fs)", e, backoff)
        except Exception as e:
            log.exception("Erreur inattendue : %s", e)
        _dashboard_state["connected"] = False
        _dashboard_state["connected_since"] = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)  # exponential backoff capé à 30s


# ── Dashboard local (127.0.0.1:9999) ─────────────────────────────────────
#
# Petit serveur HTTP qui tourne en parallèle du WebSocket, sert une page
# HTML/JS et expose des endpoints JSON pour gérer l'agent depuis un
# navigateur local. Accessible uniquement en loopback.

DASHBOARD_PORT = 9999
_dashboard_state: dict[str, Any] = {
    "connected": False,
    "connected_since": None,
    "last_action": None,
    "action_count": 0,
    "recent_actions": [],  # liste des 50 dernières
}


def _record_action(action: str, ok: bool, duration_ms: float) -> None:
    _dashboard_state["action_count"] += 1
    _dashboard_state["last_action"] = {
        "action": action,
        "ok": ok,
        "duration_ms": round(duration_ms, 1),
        "at": time.time(),
    }
    _dashboard_state["recent_actions"].insert(0, _dashboard_state["last_action"])
    _dashboard_state["recent_actions"] = _dashboard_state["recent_actions"][:50]


DASHBOARD_HTML = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"/>
<title>IABridge Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  :root { --bg:#0b1220; --card:#131c2e; --text:#e6edf3; --muted:#8b98b0; --ok:#22c55e; --bad:#ef4444; --accent:#3b82f6; --border:#1f2a40; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,sans-serif; }
  header { padding:20px 28px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:16px; }
  header h1 { margin:0; font-size:20px; font-weight:800; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--bad); box-shadow:0 0 8px currentColor; }
  .dot.on { background:var(--ok); }
  .container { padding:24px 28px; display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:20px; max-width:1400px; margin:0 auto; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px; padding:20px; }
  .card h2 { margin:0 0 14px; font-size:13px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); font-weight:700; }
  .metric { font-size:28px; font-weight:800; }
  .sub { color:var(--muted); font-size:12px; margin-top:4px; }
  .row { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid var(--border); font-size:13px; }
  .row:last-child { border:none; }
  .row .name { font-family:SFMono-Regular,Consolas,monospace; font-size:12px; }
  select { background:#0b1220; color:var(--text); border:1px solid var(--border); border-radius:8px; padding:4px 8px; font-size:12px; }
  button { background:var(--accent); color:white; border:0; padding:8px 14px; border-radius:10px; font-weight:600; cursor:pointer; font-size:13px; }
  button:hover { filter:brightness(1.1); }
  button.danger { background:var(--bad); }
  .log { font-family:SFMono-Regular,Consolas,monospace; font-size:11px; max-height:340px; overflow-y:auto; background:#0b1220; border:1px solid var(--border); border-radius:10px; padding:12px; }
  .log .entry { padding:2px 0; color:var(--muted); }
  .log .entry.err { color:var(--bad); }
  .log .entry.ok { color:var(--ok); }
  .log .entry .t { color:#475569; margin-right:6px; }
  .actions { display:flex; flex-direction:column; gap:6px; }
  .grid-wide { grid-column:1/-1; }
</style>
</head>
<body>
<header>
  <span class="dot" id="statusDot"></span>
  <h1>IABridge Dashboard</h1>
  <span id="version" style="color:var(--muted); font-size:12px;">v3.0</span>
  <div style="flex:1"></div>
  <span id="heartbeat" style="color:var(--muted); font-size:12px;">—</span>
</header>
<div class="container">
  <div class="card">
    <h2>Connexion</h2>
    <div class="metric" id="connStatus">—</div>
    <div class="sub" id="connSince">—</div>
  </div>
  <div class="card">
    <h2>Actions totales</h2>
    <div class="metric" id="actionCount">0</div>
    <div class="sub" id="lastAction">—</div>
  </div>
  <div class="card">
    <h2>Navigateur (Playwright)</h2>
    <div class="metric" id="browserStatus">—</div>
    <div class="sub" id="browserUrl">—</div>
  </div>
  <div class="card">
    <h2>Trust — Niveau de confiance</h2>
    <div id="trustList"></div>
  </div>
  <div class="card">
    <h2>Actions rapides</h2>
    <div class="actions">
      <button onclick="fetch('/api/stop_browser', {method:'POST'}).then(refresh)">Fermer le navigateur</button>
      <button class="danger" onclick="if(confirm('Arrêter l\\'agent ?')) fetch('/api/quit', {method:'POST'})">Arrêter l'agent</button>
    </div>
  </div>
  <div class="card grid-wide">
    <h2>Actions récentes</h2>
    <div class="log" id="recentLog"></div>
  </div>
</div>
<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('statusDot').className = 'dot' + (d.connected ? ' on' : '');
    document.getElementById('connStatus').textContent = d.connected ? 'Connecté' : 'Déconnecté';
    document.getElementById('connSince').textContent = d.connected_since ? ('Depuis ' + new Date(d.connected_since*1000).toLocaleTimeString()) : '';
    document.getElementById('actionCount').textContent = d.action_count;
    document.getElementById('lastAction').textContent = d.last_action ? (d.last_action.action + ' · ' + d.last_action.duration_ms + 'ms') : '';
    document.getElementById('browserStatus').textContent = d.browser_open ? 'Ouvert' : 'Fermé';
    document.getElementById('browserUrl').textContent = d.browser_url || '';
    document.getElementById('heartbeat').textContent = new Date().toLocaleTimeString();
    // Actions récentes
    const logEl = document.getElementById('recentLog');
    logEl.innerHTML = d.recent_actions.map(a => {
      const t = new Date(a.at*1000).toLocaleTimeString();
      const cls = a.ok ? 'ok' : 'err';
      return `<div class="entry ${cls}"><span class="t">${t}</span>${a.action} · ${a.duration_ms}ms ${a.ok?'':'· error'}</div>`;
    }).join('');
    // Trust
    const tr = await fetch('/api/trust').then(r => r.json());
    const trustEl = document.getElementById('trustList');
    trustEl.innerHTML = Object.entries(tr).map(([action, policy]) => `
      <div class="row">
        <span class="name">${action}</span>
        <select onchange="setTrust('${action}', this.value)">
          <option value="allow" ${policy==='allow'?'selected':''}>Allow</option>
          <option value="ask" ${policy==='ask'?'selected':''}>Ask</option>
          <option value="deny" ${policy==='deny'?'selected':''}>Deny</option>
        </select>
      </div>
    `).join('');
  } catch (e) { console.error(e); }
}
async function setTrust(action, policy) {
  await fetch('/api/trust', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({[action]: policy}),
  });
  refresh();
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


def start_dashboard() -> None:
    """Démarre un serveur HTTP local sur 127.0.0.1:9999 dans un thread dédié."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # silence

        def _send_json(self, code: int, data: Any) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/" or self.path == "/index.html":
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                browser_open = _browser_state["browser"] is not None
                browser_url = _browser_state["page"].url if browser_open and _browser_state["page"] else None
                self._send_json(200, {
                    **_dashboard_state,
                    "browser_open": browser_open,
                    "browser_url": browser_url,
                })
                return
            if self.path == "/api/trust":
                self._send_json(200, load_trust())
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            if self.path == "/api/trust":
                trust = load_trust()
                for k, v in payload.items():
                    if v in ("allow", "deny", "ask"):
                        trust[k] = v
                save_trust(trust)
                self._send_json(200, trust)
                return
            if self.path == "/api/stop_browser":
                # Le dashboard tourne dans un thread à part, on ne peut pas
                # awaiter la coroutine directement. On pose un flag que la
                # boucle principale lira.
                _dashboard_state["_stop_browser_requested"] = True
                self._send_json(200, {"status": "queued"})
                return
            if self.path == "/api/quit":
                self._send_json(200, {"status": "quitting"})
                os._exit(0)
            self._send_json(404, {"error": "not found"})

    import threading
    srv = ThreadingHTTPServer(("127.0.0.1", DASHBOARD_PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, name="dashboard", daemon=True)
    t.start()
    log.info("Dashboard local : http://127.0.0.1:%d/", DASHBOARD_PORT)


def main() -> None:
    cfg = load_config()
    if not cfg.get("token"):
        if sys.stdin.isatty():
            cfg = interactive_setup()
        else:
            log.error("Pas de token configuré. Lance 'python agent.py' manuellement pour configurer.")
            sys.exit(1)

    log.info("IABridge Agent v3.0 démarre")
    log.info("Config : %s", CONFIG_FILE)
    log.info("Gateway : %s", cfg["gateway_url"])
    log.info("Agent : %s", cfg["agent_name"])
    start_dashboard()
    try:
        asyncio.run(run_agent(cfg))
    except KeyboardInterrupt:
        log.info("Arrêt demandé")


if __name__ == "__main__":
    main()
