import os
import base64
import subprocess
import io
import time
import traceback
import logging
import random
from functools import wraps

from flask import Flask, request, jsonify
import mss
import pyautogui
import pyperclip
from PIL import Image

logging.basicConfig(
    filename="agent_v2.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

_DEFAULT_TOKEN = "change-me-secret-token"
AUTH_TOKEN = os.environ.get("IABRIDGE_TOKEN", _DEFAULT_TOKEN)
if AUTH_TOKEN == _DEFAULT_TOKEN:
    logger.warning("IABRIDGE_TOKEN not set — using insecure default token.")

PORT = 9999
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

def get_scale_factor():
    logical_w, logical_h = pyautogui.size()
    with mss.mss() as sct:
        try:
            m = sct.monitors[1]
            phys_w, phys_h = m["width"], m["height"]
        except IndexError:
            return 1.0, 1.0
    return phys_w / logical_w, phys_h / logical_h

SCALE_X, SCALE_Y = get_scale_factor()

def to_logical(x, y):
    return int(x / SCALE_X), int(y / SCALE_Y)

def human_move(x, y, duration=None, jitter=3):
    lx, ly = to_logical(x, y)
    lx += random.randint(-jitter, jitter)
    ly += random.randint(-jitter, jitter)
    if duration is None:
        cx, cy = pyautogui.position()
        dist = ((lx - cx) ** 2 + (ly - cy) ** 2) ** 0.5
        duration = max(0.15, min(0.8, dist / 1500))
    pyautogui.moveTo(lx, ly, duration=duration, tween=pyautogui.easeInOutQuad)
    return lx, ly

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth_header[7:]
        if token != AUTH_TOKEN:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

def get_primary_monitor():
    with mss.mss() as sct:
        try:
            return sct.monitors[1]
        except IndexError:
            raise RuntimeError("No primary monitor found")

def _capture_region(region):
    with mss.mss() as sct:
        try:
            sct_img = sct.grab(region)
        except IndexError:
            raise RuntimeError("No primary monitor found")
        w, h = sct_img.width, sct_img.height
        img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
    return img, w, h

def _encode_image(img, fmt, quality):
    buffer = io.BytesIO()
    if fmt == "jpeg":
        img.save(buffer, format="JPEG", quality=max(1, min(95, quality)))
    else:
        img.save(buffer, format="PNG", optimize=False)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")

@app.route("/health", methods=["GET"])
@require_auth
def health():
    try:
        mon = get_primary_monitor()
        phys_w, phys_h = mon["width"], mon["height"]
        log_w, log_h = pyautogui.size()
        return jsonify({"status": "ok", "screen": f"{phys_w}x{phys_h}", "logical": f"{log_w}x{log_h}", "scale": {"x": SCALE_X, "y": SCALE_Y}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/refresh-scale", methods=["GET"])
@require_auth
def refresh_scale():
    global SCALE_X, SCALE_Y
    try:
        SCALE_X, SCALE_Y = get_scale_factor()
        logger.info("Scale refreshed: SCALE_X=%.4f SCALE_Y=%.4f", SCALE_X, SCALE_Y)
        return jsonify({"status": "ok", "scale": {"x": SCALE_X, "y": SCALE_Y}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/screenshot", methods=["GET"])
@require_auth
def screenshot_get():
    try:
        fmt = request.args.get("format", "png").lower()
        if fmt not in ("png", "jpeg"):
            return jsonify({"error": "format must be png or jpeg"}), 400
        quality = max(1, min(95, int(request.args.get("quality", 85))))
        with mss.mss() as sct:
            sct_img = sct.grab(sct.monitors[1])
            w, h = sct_img.width, sct_img.height
            img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
        b64 = _encode_image(img, fmt, quality)
        return jsonify({"image": b64, "width": w, "height": h, "format": fmt})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/screenshot", methods=["POST"])
@require_auth
def screenshot_post():
    try:
        fmt = request.args.get("format", "png").lower()
        quality = max(1, min(95, int(request.args.get("quality", 85))))
        data = request.get_json(force=True, silent=True) or {}
        if data and all(k in data for k in ("x", "y", "w", "h")):
            region = {"left": int(data["x"]), "top": int(data["y"]), "width": int(data["w"]), "height": int(data["h"])}
            img, w, h = _capture_region(region)
        else:
            with mss.mss() as sct:
                sct_img = sct.grab(sct.monitors[1])
                w, h = sct_img.width, sct_img.height
                img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
        b64 = _encode_image(img, fmt, quality)
        return jsonify({"image": b64, "width": w, "height": h, "format": fmt})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

@app.route("/click", methods=["POST"])
@require_auth
def click():
    try:
        data = request.get_json(force=True)
        x, y = int(data["x"]), int(data["y"])
        button = data.get("button", "left")
        if button not in ("left", "right", "middle"):
            return jsonify({"error": "Invalid button"}), 400
        duration = data.get("duration", None)
        jitter = data.get("jitter", 3)
        lx, ly = human_move(x, y, duration=duration, jitter=jitter)
        time.sleep(random.uniform(0.05, 0.12))
        pyautogui.click(button=button)
        logger.info("click button=%s phys=(%d,%d) logical=(%d,%d)", button, x, y, lx, ly)
        return jsonify({"status": "ok", "x": x, "y": y, "logical_x": lx, "logical_y": ly, "button": button})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/move", methods=["POST"])
@require_auth
def move():
    try:
        data = request.get_json(force=True)
        x, y = int(data["x"]), int(data["y"])
        lx, ly = human_move(x, y, duration=data.get("duration"), jitter=data.get("jitter", 0))
        logger.info("move phys=(%d,%d) logical=(%d,%d)", x, y, lx, ly)
        return jsonify({"status": "ok", "logical_x": lx, "logical_y": ly})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scroll", methods=["POST"])
@require_auth
def scroll():
    try:
        data = request.get_json(force=True)
        x, y = int(data["x"]), int(data["y"])
        clicks = int(data.get("clicks", -3))
        lx, ly = human_move(x, y, jitter=0)
        pyautogui.scroll(clicks, x=lx, y=ly)
        logger.info("scroll phys=(%d,%d) logical=(%d,%d) clicks=%d", x, y, lx, ly, clicks)
        return jsonify({"status": "ok", "clicks": clicks})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/drag", methods=["POST"])
@require_auth
def drag():
    try:
        data = request.get_json(force=True)
        x1, y1 = int(data["x1"]), int(data["y1"])
        x2, y2 = int(data["x2"]), int(data["y2"])
        duration = data.get("duration", 0.5)
        lx1, ly1 = human_move(x1, y1, duration=0.2, jitter=0)
        lx2, ly2 = to_logical(x2, y2)
        pyautogui.dragTo(lx2, ly2, duration=duration, tween=pyautogui.easeInOutQuad, button='left')
        logger.info("drag phys=(%d,%d)->(%d,%d) logical=(%d,%d)->(%d,%d)", x1, y1, x2, y2, lx1, ly1, lx2, ly2)
        return jsonify({"status": "ok"})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/type", methods=["POST"])
@require_auth
def type_text():
    try:
        data = request.get_json(force=True)
        text = data["text"]
        old_clip = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        pyperclip.copy(old_clip)
        logger.info("type length=%d", len(text))
        return jsonify({"status": "ok", "length": len(text)})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/key", methods=["POST"])
@require_auth
def press_key():
    try:
        data = request.get_json(force=True)
        key = data["key"]
        if isinstance(key, list):
            parts = [str(k).strip() for k in key]
        elif isinstance(key, str):
            parts = [k.strip() for k in key.split("+")] if "+" in key else [key.strip()]
        else:
            return jsonify({"error": "key must be string or list"}), 400
        if len(parts) > 1:
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(parts[0])
        logger.info("key parts=%s", parts)
        return jsonify({"status": "ok", "key": parts})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/run", methods=["POST"])
@require_auth
def run_command():
    if os.environ.get("IABRIDGE_ALLOW_RUN", "0") != "1":
        return jsonify({"error": "Endpoint /run disabled. Set IABRIDGE_ALLOW_RUN=1."}), 403
    try:
        data = request.get_json(force=True)
        command = data["command"]
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        return jsonify({"status": "ok", "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out"}), 408
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    logger.info("IABridge v2 starting on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
