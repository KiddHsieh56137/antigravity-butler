# Python

在 Debian 本機或透過節點執行 Python。

## 工具

- `run_python(code)`：在管家 venv 執行短碼並回傳 stdout/stderr
- `install_python_package(package)`：`pip install` 進管家 venv
- `run_local_shell`：可跑 `python script.py`、建立虛擬環境等
- 缺函式庫時：**先 pip 安裝**，再重試；仍不夠就改自身程式加工具

## 慣例

- 優先用管家 venv：`/home/past/antigravity-butler/.venv/bin/python`
- 短探針用 `run_python`；長腳本寫到 `/tmp` 或專案目錄再執行
- 需要持久能力時：用 `write_own_code` 加 tool → `restart_butler_service`
- 不要把 API 金鑰寫死進程式；讀 `butler_config.json`
