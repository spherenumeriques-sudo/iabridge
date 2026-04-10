# IABridge v3

Assistant IA qui pilote ton PC Windows depuis n'importe où, via un tunnel WebSocket inverse.

## Architecture

```
┌────────────┐  wss://labonneocaz.fr/iabridge/ws  ┌──────────────┐
│ PC Windows │ ────────────────────────────────▶  │ Gateway VPS  │
│   Agent    │ ◀────── commandes / réponses ───── │   (FastAPI)  │
└────────────┘                                    └──────┬───────┘
                                                         │ HTTP local
                                                         │ (127.0.0.1:9998)
                                                   ┌─────┴─────┐
                                                   │   Claude  │
                                                   │  (CLI)    │
                                                   └───────────┘
```

L'agent Windows se connecte **sortant** au gateway VPS (pas besoin de port
forwarding, pas besoin de VPN). Le gateway relaie les commandes de Claude à
l'agent et renvoie les réponses.

## Structure

- `gateway/` — Gateway FastAPI sur le VPS (WebSocket + API HTTP locale)
- `agent/` — Agent Python Windows (connexion WS, handlers, dashboard web)
- `cli/` — CLI `iabridge` pour piloter l'agent depuis le terminal VPS

## Installation côté VPS (déjà fait)

- Venv Python dans `gateway/venv/` avec FastAPI, uvicorn, websockets
- Service systemd : `iabridge-gateway.service`
- Config nginx : proxy `/iabridge/` → `127.0.0.1:9998` avec support WebSocket
- Token stocké dans `~/.config/iabridge/token`
- CLI symlinké dans `~/bin/iabridge`

## Installation côté Windows

```powershell
iwr https://labonneocaz.fr/downloads/install.ps1 -UseBasicParsing | iex
```

Ce script installe les dépendances Python (`websockets mss pyautogui pyperclip
Pillow psutil pywin32 playwright`), télécharge `agent.py`, écrit la config avec
le token pré-rempli, et lance l'agent.

Pour Playwright, il faut également :

```powershell
python -m playwright install chromium
```

## Modules disponibles

### Module 1 — Contrôle de base
`screenshot`, `click`, `doubleclick`, `move`, `drag`, `scroll`, `type`, `key`,
`clipboard_get`, `clipboard_set`

### Module 2 — Fichiers
`list_dir`, `read_file`, `write_file`, `delete`, `fs_move`, `copy`, `run`

### Module 3 — Système
`sys_info` (CPU, RAM, disques, batterie), `processes`, `kill_process`,
`windows_list`, `focus_window`, `minimize_window`, `close_window`

### Module 4 — Nettoyage & optimisation
`clean_temp`, `clean_recycle_bin`, `startup_list`, `winget` (list/upgrade/install/
uninstall), `services` (list/start/stop/restart)

### Module 5 — Automation navigateur (Playwright)
`browser_open`, `browser_goto`, `browser_click`, `browser_fill`, `browser_wait`,
`browser_extract`, `browser_screenshot`, `browser_script`, `browser_press`,
`browser_close`, `browser_url`

### Divers
`open_url`, `notify`, `health`

## Dashboard local

Une fois l'agent lancé, le dashboard est accessible à :

```
http://localhost:9999/
```

Il affiche l'état de connexion au gateway, le compteur d'actions, l'état du
navigateur Playwright, la gestion des permissions (`trust.json`), et un log
des actions récentes.

## Sécurité

- Toutes les actions sensibles sont régies par `~/AppData/Roaming/IABridge/trust.json`
- Trois niveaux : `allow` (exécuté sans prompt), `ask` (MessageBox Windows), `deny` (refusé)
- Par défaut : `ask` sur `delete`, `run`, `kill_process`, `clean_*`, `winget`, `services`
- Le token d'auth est stocké en clair dans `agent.json` (prévu DPAPI en v3.1)

## Fichiers de config (côté Windows)

- `%APPDATA%\IABridge\agent.json` — URL du gateway + token + nom de l'agent
- `%APPDATA%\IABridge\trust.json` — Permissions par action
- `%APPDATA%\IABridge\agent.log` — Logs

## Exemples d'utilisation depuis le VPS

```bash
# Statut
iabridge health

# Screenshot
iabridge screenshot --save /tmp/screen.jpg

# Info système
iabridge raw '{"action": "sys_info"}'

# Top 10 processus par CPU
iabridge raw '{"action": "processes", "sort": "cpu", "limit": 10}'

# Nettoyage temp
iabridge raw '{"action": "clean_temp"}'

# Ouvrir un site dans Playwright
iabridge raw '{"action": "browser_goto", "url": "https://labonneocaz.fr"}'

# Extraire tous les titres de la page
iabridge raw '{"action": "browser_extract", "selector": "h1, h2, h3"}'
```
