# 自我擴充（缺能力時）

當現有工具做不到主人要求時，依序嘗試：

1. **查技能**：`load_skill`（debian-admin / python / windows-shell / file-organization / self-extend）
2. **上網**：`web_search` / `fetch_webpage` 找作法
3. **裝軟體**：
   - 系統：`install_system_package`
   - Python：`install_python_package`
   - Windows：節點上 `winget` / `choco`（若有）
4. **改自己的程式**：
   - `read_own_code` 看 `butler_api.py` 等
   - `write_own_code` 新增 tool 或修正邏輯（僅限管家專案目錄）
   - `restart_butler_service` 讓新工具生效
5. **驗證**：用新能力實際做一次，再簡短回報主人

## 安全邊界

- 不要改 `/etc/passwd`、不要無確認清空磁碟
- 不要把 `gemini_api_key`、HA token、密碼印給聊天室以外的地方
- 寫入自身程式失敗就說明原因，改用 shell 暫時完成任務
