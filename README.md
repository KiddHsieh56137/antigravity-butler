# Antigravity Homelab Butler

**Language / 語言:** [English](README.md) · [繁體中文](README.zh-TW.md)

**One web UI to run your home lab** — Debian host, Windows PCs, Home Assistant, and a Gemini butler you can type or talk to.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![UI](https://img.shields.io/badge/UI-zh--TW%20%7C%20en-00e676)](README.zh-TW.md)
[![Stack](https://img.shields.io/badge/stack-FastAPI%20%2B%20Gemini-00d2ff)](#features)

<p align="center">
  <img src="docs/screenshots/dashboard.jpg" alt="Homelab Butler dashboard" width="920" />
</p>

<p align="center">
  <img src="docs/screenshots/nodes-chat.jpg" alt="Node management with AI chat" width="450" />
  &nbsp;
  <img src="docs/screenshots/mobile.jpg" alt="Mobile console" width="220" />
</p>

> **Security:** There is **no login by default**. Keep it on a trusted LAN, firewall the port, or put auth in front of it.

## Why this exists

Most homelab stacks are a pile of bookmarks (HA, Portainer, Mesh, SSH…).  
Butler is a **single glass cockpit**: health, containers, LAN probes, files, terminal, cameras — plus an AI that can actually call tools.

## Features

- Web dashboard (`/`), mobile console (`/m`), full chat (`/chat`), voice (`/voice`)
- Home Assistant tools, Zigbee/MQTT awareness, Debian APT helpers
- Windows **Butler Node** probes (`:8789`) for remote files / terminal / VNC links
- Optional MeshCentral / WebRTC remote desktop entry points
- UI: **zh-TW / en** · AI reply language: follow UI or fixed
- Settings page for Gemini key, HA URL/token, languages

## Architecture

```mermaid
flowchart LR
  browser[Browser_UI]
  butler[Butler_API_8788]
  gemini[Gemini]
  ha[Home_Assistant]
  nodes[Windows_Nodes_8789]
  browser --> butler
  butler --> gemini
  butler --> ha
  butler --> nodes
```

## Quick start (Debian / Raspberry Pi)

```bash
cd antigravity-butler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp butler_config.example.json butler_config.json
# edit butler_config.json — set gemini_api_key, ha_token, etc.

cp butler_memory.example.json butler_memory.json
cp skills/homelab-architecture.example.md skills/homelab-architecture.md
# optional: cp skills/persona.example.md skills/persona.md

uvicorn butler_api:app --host 0.0.0.0 --port 8788
```

Open `http://<host>:8788/`.

### Environment overrides

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Overrides `gemini_api_key` in config |
| `HA_URL` | Overrides Home Assistant URL |
| `HA_TOKEN` | Overrides HA long-lived token |
| `BUTLER_NODE_TOKEN` | Token expected by Windows probes |

## Windows probe

1. Place `Homelab-ButlerNode-Setup.zip` under `static/downloads/` on the host (not in git).
2. On a LAN PC open `http://<host>:8788/downloads/butler-node`.
3. Run `Install-All.cmd` as Administrator → **Scan LAN** in Node Management.

Source: `butler_node.py` (token must match `node_token` / `BUTLER_NODE_TOKEN`).

## Config files (do not commit secrets)

| File | In git? |
|------|---------|
| `butler_config.example.json` | yes |
| `butler_config.json` | **no** |
| `butler_memory.example.json` | yes |
| `butler_memory.json` | **no** |
| `skills/*.example.md` | yes |
| `skills/homelab-architecture.md` / `persona.md` | **no** |

## UI / AI languages

- Sidebar **中 \| EN** → `assistant.ui_locale`
- Settings → AI reply language: `follow_ui` / `zh-TW` / `en`
- Changing reply language rebuilds the Gemini session

## systemd (example)

```ini
[Unit]
Description=Homelab Butler
After=network.target

[Service]
User=past
WorkingDirectory=/home/past/antigravity-butler
Environment=GEMINI_API_KEY=
ExecStart=/home/past/antigravity-butler/.venv/bin/uvicorn butler_api:app --host 0.0.0.0 --port 8788
Restart=always

[Install]
WantedBy=multi-user.target
```

## Screenshots

| Dashboard | Nodes + AI | Mobile |
|-----------|------------|--------|
| ![](docs/screenshots/dashboard.jpg) | ![](docs/screenshots/nodes-chat.jpg) | ![](docs/screenshots/mobile.jpg) |

More: [`docs/screenshots/`](docs/screenshots/)

## License

MIT — see [LICENSE](LICENSE).
