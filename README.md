# IABridge

**IABridge** is a lightweight Python agent that exposes a REST API to remotely control a Windows machine — no VNC, no RDP, no TeamViewer needed.

Control your Windows PC from any device on the same network (or via Tailscale/VPN): take screenshots, click, type, press keys, and run commands — all via simple HTTP calls.

---

## Features

- 📸 **Screenshot** — full screen or custom region, PNG or JPEG
- 🖱️ **Click** — left, right, middle button at any coordinates
- ⌨️ **Type** — full Unicode support via clipboard (works on any keyboard layout)
- 🔑 **Hotkeys** — send key combos like `ctrl+c`, `alt+F4`, `super+r`
- 💻 **Run commands** — execute shell commands (opt-in, disabled by default)
- 🔒 **Bearer token auth** — all endpoints protected
- 📝 **File logging** — all actions logged to `agent.log`

---

## Installation

### Requirements

- Python 3.8+
- Windows 10/11

### Install dependencies

```bash
pip install flask mss pyautogui pyperclip pillow
```

### Download the agent

```powershell
iwr https://raw.githubusercontent.com/spherenumeriques-sudo/iabridge/main/agent.py -OutFile agent.py
```

---

## Configuration

Set environment variables before launching:

| Variable | Default | Description |
|---|---|---|
| `IABRIDGE_TOKEN` | *(required)* | Bearer token for authentication |
| `IABRIDGE_ALLOW_RUN` | `0` | Set to `1` to enable `/run` endpoint |

**Example (PowerShell):**

```powershell
$env:IABRIDGE_TOKEN = "my-secret-token"
pythonw agent.py
```

> ⚠️ If `IABRIDGE_TOKEN` is not set, the agent will start but log a warning. Always set a strong token in production.

---

## Usage

### Start the agent

```powershell
pythonw agent.py
```

The agent runs silently on port `9999`. Logs are written to `agent.log` in the same directory.

### Auto-start on Windows boot (Task Scheduler)

```powershell
schtasks /create /tn "IABridge" /tr "pythonw C:\path\to\agent.py" /sc onlogon /rl highest /f
```

---

## API Reference

All endpoints require the header:
```
Authorization: Bearer <your-token>
```

---

### `GET /health`

Check if the agent is running.

**Response:**
```json
{"status": "ok", "screen": "1920x1080"}
```

---

### `GET /screenshot`

Capture the full screen.

**Query params:**
| Param | Default | Description |
|---|---|---|
| `format` | `png` | `png` or `jpeg` |
| `quality` | `85` | JPEG quality (1–95) |

**Response:**
```json
{
  "image": "<base64>",
  "width": 1920,
  "height": 1080,
  "format": "jpeg"
}
```

---

### `POST /screenshot`

Capture a region of the screen.

**Body (optional):**
```json
{"x": 100, "y": 200, "w": 800, "h": 600}
```

If body is omitted or incomplete, captures the full screen.

Same query params as GET `/screenshot`.

---

### `POST /click`

Click at a position.

**Body:**
```json
{"x": 960, "y": 540, "button": "left"}
```

`button` can be `left` (default), `right`, or `middle`.

---

### `POST /type`

Type text into the focused window. Uses clipboard internally — works with all languages and special characters.

**Body:**
```json
{"text": "Hello, World! こんにちは"}
```

---

### `POST /key`

Press a key or hotkey combo.

**Body (string with `+` separator):**
```json
{"key": "ctrl+c"}
```

**Body (JSON list):**
```json
{"key": ["ctrl", "alt", "delete"]}
```

**Body (single key):**
```json
{"key": "Return"}
```

Common keys: `Return`, `Tab`, `Escape`, `space`, `BackSpace`, `Delete`, `F1`–`F12`, `super` (Win key), `alt`, `ctrl`, `shift`.

---

### `POST /run`

Execute a shell command. **Requires `IABRIDGE_ALLOW_RUN=1`.**

**Body:**
```json
{"command": "echo hello"}
```

**Response:**
```json
{
  "status": "ok",
  "returncode": 0,
  "stdout": "hello\n",
  "stderr": ""
}
```

> ⚠️ This endpoint executes arbitrary commands on the host machine. Only enable it in trusted environments.

---

## Example: Python client

```python
import requests, base64, json

BASE = "http://192.168.1.100:9999"
HEADERS = {"Authorization": "Bearer my-secret-token"}

# Health check
r = requests.get(f"{BASE}/health", headers=HEADERS)
print(r.json())

# Take a screenshot and save it
r = requests.get(f"{BASE}/screenshot?format=jpeg&quality=80", headers=HEADERS)
data = r.json()
with open("screenshot.jpg", "wb") as f:
    f.write(base64.b64decode(data["image"]))

# Click somewhere
requests.post(f"{BASE}/click", headers=HEADERS, json={"x": 960, "y": 540})

# Type text
requests.post(f"{BASE}/type", headers=HEADERS, json={"text": "Hello!"})

# Press Enter
requests.post(f"{BASE}/key", headers=HEADERS, json={"key": "Return"})
```

---

## Security

- Always use a strong, random token
- Run behind a VPN (e.g. Tailscale) — do not expose port 9999 to the public internet
- `/run` is disabled by default — only enable if you know what you're doing
- Logs all actions to `agent.log` for audit

---

## License

MIT — see [LICENSE](LICENSE)
