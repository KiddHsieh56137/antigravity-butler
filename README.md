# Antigravity Homelab Butler

LAN control plane + Gemini assistant for a home lab: dashboard, file manager, web terminal, node probes, voice/chat UI.

> **Security warning:** By default there is **no login**. Bind to a trusted LAN, firewall the host, or put a reverse proxy with auth in front before exposing beyond your network.

## Features

- Web dashboard (`/`), mobile (`/m`), full chat (`/chat`), voice (`/voice`)
- Home Assistant tools, Zigbee/MQTT awareness, APT helpers (Debian)
- Windows **Butler Node** probes (`:8789`) for remote files/terminal
- Optional MeshCentral / VNC / WebRTC remote desktop links
- UI language: **zh-TW / en**; AI reply language: follow UI or fixed
- Settings page for Gemini API key, HA URL/token, languages

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

1. On the Homelab host, place `Homelab-ButlerNode-Setup.zip` under `static/downloads/` (not committed; build from your installer scripts).
2. LAN PCs download from `http://<host>:8788/downloads/butler-node`.
3. Run `Install-All.cmd` as Administrator, then **Scan LAN** in Node Management.

Probe source: `butler_node.py` (token must match `node_token` / `BUTLER_NODE_TOKEN`).

## Config files (do not commit secrets)

| File | In git? |
|------|---------|
| `butler_config.example.json` | yes |
| `butler_config.json` | **no** (gitignore) |
| `butler_memory.example.json` | yes |
| `butler_memory.json` | **no** |
| `skills/homelab-architecture.example.md` | yes |
| `skills/homelab-architecture.md` | **no** (your private map) |
| `skills/persona.example.md` | yes |
| `skills/persona.md` | **no** |

## UI / AI languages

- Sidebar **中 | EN** toggles UI locale (`assistant.ui_locale`).
- **Settings → AI reply language**: `follow_ui` / `zh-TW` / `en` (`assistant.reply_language`).
- Changing reply language rebuilds the Gemini session with an updated system instruction.

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

## Before publishing to a public GitHub repo

1. Confirm `.gitignore` excludes real `butler_config.json`, memory, private skills, zips.
2. Search the tree for API keys, JWTs, passwords, personal names, home IPs.
3. Rotate any credentials that ever lived in git history.
4. Keep MeshCentral / VNC passwords only in local config.
5. Prefer Release Assets for large Windows installer zips.

## License

MIT — see [LICENSE](LICENSE).
