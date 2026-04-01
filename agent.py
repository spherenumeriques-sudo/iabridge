import os
import base64
import subprocess
import io
import traceback
import logging
from functools import wraps

from flask import Flask, request, jsonify
import mss
import pyautogui
import pyperclip
from PIL import Image

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename="agent_v2.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & config
# ---------------------------------------------------------------------------
app = Flask(__name__)

_DEFAULT_TOKEN = "change-me-secret-token"
AUTH_TOKEN = os.environ.get("IABRIDGE_TOKEN", _DEFAULT_TOKEN)
if AUTH_TOKEN == _DEFAULT_TOKEN:
    logger.warning(
        "IABRIDGE_TOKEN not set — using insecure default token. "
        "Set the IABRIDGE_TOKEN environment variable in production."
    )

PORT = 9999

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            logger.warning("Unauthorized request (no Bearer token)")
            return jsonify({"error": "Unauthorized"}), 401
        token = auth_header[7:]
        if token != AUTH_TOKEN:
            logger.warning("Forbidden request (wrong token)")
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_primary_monitor():
    with mss.mss() as sct:
        try:
            return sct.monitors[1]
        except IndexError:
            raise RuntimeError("No primary monitor found (sct.monitors has no index 1)")


def _capture_region(region: dict) -> tuple:
    with mss.mss() as sct:
        try:
            sct_img = sct.grab(region)
        except IndexError:
            raise RuntimeError("No primary monitor found")
        w = sct_img.width
        h = sct_img.height
        img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
    return img, w, h


def _encode_image(img: Image.Image, fmt: str, quality: int) -> str:
    buffer = io.BytesIO()
    if fmt == "jpeg":
        img.save(buffer, format="JPEG", quality=max(1, min(95, quality)))
    else:
        img.save(buffer, format="PNG", optimize=False)
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
@require_auth
def health():
    try:
        mon = get_primary_monitor()
        w = mon["width"]
        h = mon["height"]
        logger.info("Health check OK — screen %sx%s", w, h)
        return jsonify({"status": "ok", "screen": f"{w}x{h}"})
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/screenshot", methods=["GET"])
@require_auth
def screenshot_get():
    try:
        fmt = request.args.get("format", "png").lower()
        if fmt not in ("png", "jpeg"):
            return jsonify({"error": "format must be 'png' or 'jpeg'"}), 400
        quality = max(1, min(95, int(request.args.get("quality", 85))))
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            sct_img = sct.grab(monitor)
            w, h = sct_img.width, sct_img.height
            img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
        b64 = _encode_image(img, fmt, quality)
        logger.info("Screenshot GET — %sx%s fmt=%s", w, h, fmt)
        return jsonify({"image": b64, "width": w, "height": h, "format": fmt})
    except Exception as e:
        logger.error("Screenshot GET failed: %s", traceback.format_exc())
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/screenshot", methods=["POST"])
@require_auth
def screenshot_post():
    try:
        fmt = request.args.get("format", "png").lower()
        quality = max(1, min(95, int(request.args.get("quality", 85))))
        data = request.get_json(force=True, silent=True) or {}
        if data and all(k in data for k in ("x", "y", "w", "h")):
            region = {
                "left":   int(data["x"]),
                "top":    int(data["y"]),
                "width":  int(data["w"]),
                "height": int(data["h"]),
            }
            img, w, h = _capture_region(region)
        else:
            with mss.mss() as sct:
                sct_img = sct.grab(sct.monitors[1])
                w, h = sct_img.width, sct_img.height
                img = Image.frombytes("RGB", (w, h), sct_img.bgra, "raw", "BGRX")
        b64 = _encode_image(img, fmt, quality)
        logger.info("Screenshot POST — %sx%s fmt=%s", w, h, fmt)
        return jsonify({"image": b64, "width": w, "height": h, "format": fmt})
    except Exception as e:
        logger.error("Screenshot POST failed: %s", traceback.format_exc())
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/click", methods=["POST"])
@require_auth
def click():
    try:
        data = request.get_json(force=True)
        x, y = int(data["x"]), int(data["y"])
        button = data.get("button", "left")
        if button not in ("left", "right", "middle"):
            return jsonify({"error": "Invalid button. Use left, right, or middle."}), 400
        pyautogui.click(x=x, y=y, button=button)
        logger.info("Click %s at (%s, %s)", button, x, y)
        return jsonify({"status": "ok", "x": x, "y": y, "button": button})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        logger.error("Click failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/type", methods=["POST"])
@require_auth
def type_text():
    """Type text via clipboard (always) for reliable special char support on AZERTY."""
    try:
        data = request.get_json(force=True)
        text = data["text"]
        old_clip = pyperclip.paste()
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        import time; time.sleep(0.1)
        pyperclip.copy(old_clip)
        logger.info("Type %d chars via clipboard", len(text))
        return jsonify({"status": "ok", "length": len(text)})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        logger.error("Type failed: %s", e)
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
            return jsonify({"error": "'key' must be a string or list"}), 400
        if len(parts) > 1:
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(parts[0])
        logger.info("Key: %s", parts)
        return jsonify({"status": "ok", "key": parts})
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        logger.error("Key failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/run", methods=["POST"])
@require_auth
def run_command():
    if os.environ.get("IABRIDGE_ALLOW_RUN", "0") != "1":
        logger.warning("/run called but IABRIDGE_ALLOW_RUN is not set")
        return jsonify({
            "error": "Endpoint /run is disabled. Set IABRIDGE_ALLOW_RUN=1 to enable."
        }), 403
    try:
        data = request.get_json(force=True)
        command = data["command"]
        logger.info("Run: %s", command)
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        logger.info("Run done (rc=%s)", result.returncode)
        return jsonify({
            "status": "ok",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out after 30s"}), 408
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400
    except Exception as e:
        logger.error("Run failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("IABridge v2 starting on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
