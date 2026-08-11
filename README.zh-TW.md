# Antigravity Homelab Butler

**Language / 語言:** [English](README.md) · [繁體中文](README.zh-TW.md)

家用 Homelab 的區網中控＋ Gemini 助理：儀表板、檔案管理、網頁終端、節點探針、語音／對話介面。

> **安全提醒：** 預設**沒有登入牆**。請綁定可信區網、用防火牆限制，或在對外前加反向代理＋基本認證。

## 功能

- 網頁儀表板（`/`）、手機版（`/m`）、完整對話（`/chat`）、語音（`/voice`）
- Home Assistant 工具、Zigbee／MQTT 感知、Debian APT 輔助
- Windows **Butler Node** 探針（`:8789`）遠端檔案／終端
- 可選 MeshCentral／VNC／WebRTC 遠端桌面連結
- 介面語言：**zh-TW / en**；AI 回覆語言可跟隨介面或固定
- 系統設定頁可更新 Gemini API Key、HA URL／Token、語言

## 快速開始（Debian／Raspberry Pi）

```bash
cd antigravity-butler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp butler_config.example.json butler_config.json
# 編輯 butler_config.json — 填入 gemini_api_key、ha_token 等

cp butler_memory.example.json butler_memory.json
cp skills/homelab-architecture.example.md skills/homelab-architecture.md
# 可選：cp skills/persona.example.md skills/persona.md

uvicorn butler_api:app --host 0.0.0.0 --port 8788
```

瀏覽 `http://<主機>:8788/`。

### 環境變數覆寫

| 變數 | 用途 |
|------|------|
| `GEMINI_API_KEY` | 覆寫設定檔中的 `gemini_api_key` |
| `HA_URL` | 覆寫 Home Assistant URL |
| `HA_TOKEN` | 覆寫 HA long-lived token |
| `BUTLER_NODE_TOKEN` | Windows 探針驗證用 token |

## Windows 探針

1. 在 Homelab 主機把 `Homelab-ButlerNode-Setup.zip` 放到 `static/downloads/`（不進 git；請自行打包）。
2. 區網電腦從 `http://<主機>:8788/downloads/butler-node` 下載。
3. 以系統管理員執行 `Install-All.cmd`，再到「節點管理」按**掃描區網**。

探針原始碼：`butler_node.py`（token 須與 `node_token`／`BUTLER_NODE_TOKEN` 一致）。

## 設定檔（請勿提交密鑰）

| 檔案 | 是否進 git |
|------|------------|
| `butler_config.example.json` | 是 |
| `butler_config.json` | **否**（已 gitignore） |
| `butler_memory.example.json` | 是 |
| `butler_memory.json` | **否** |
| `skills/homelab-architecture.example.md` | 是 |
| `skills/homelab-architecture.md` | **否**（你的私人架構圖） |
| `skills/persona.example.md` | 是 |
| `skills/persona.md` | **否** |

## 介面／AI 語言

- 側欄 **中 | EN** 切換介面語言（`assistant.ui_locale`）。
- **系統設定 → AI 回覆語言**：`follow_ui`／`zh-TW`／`en`（`assistant.reply_language`）。
- 變更回覆語言會重建 Gemini session，並更新 system instruction。

## systemd 範例

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

## 公開 GitHub 前檢查

1. 確認 `.gitignore` 排除真實 `butler_config.json`、記憶、私人 skills、zip。
2. 搜尋樹中是否有 API key、JWT、密碼、真實姓名、家用 IP。
3. 若密鑰曾進過 git history，請 rotate 並清歷史。
4. MeshCentral／VNC 密碼只放本機設定。
5. 大型 Windows 安裝包建議用 Release Assets。

## 授權

MIT — 見 [LICENSE](LICENSE)。
