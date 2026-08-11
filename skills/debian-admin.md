# Debian 系統處理

在本機 Debian（管家所在伺服器，通常 127.0.0.1 / 192.168.1.107）執行系統工作時使用。

## 工具

- `run_local_shell(command)`：本機 bash（自動非互動 apt）
- `install_system_package(package)`：`apt-get install -y`
- `install_python_package(package)`：裝進管家 venv
- `get_host_system_status()`：CPU／RAM／磁碟／Docker

## 慣例

- apt 一律非互動：`DEBIAN_FRONTEND=noninteractive`、加 `-y`
- 危險指令（`rm -rf /`、`mkfs`、無條件刪庫）先向主人確認
- 改系統服務後用 `systemctl status/restart …` 驗證
- 查埠：`ss -tlnp`；查行程：`ps aux | grep …`
- Docker：`docker ps`、`docker logs --tail 100 <name>`

## 常見任務

| 需求 | 做法 |
|------|------|
| 更新套件索引 | `sudo apt-get update` |
| 安裝軟體 | `install_system_package` 或 apt-get install -y |
| 看日誌 | `journalctl -u antigravity-butler -n 80 --no-pager` |
| 重啟管家 | `restart_butler_service`（改完自身程式後） |
