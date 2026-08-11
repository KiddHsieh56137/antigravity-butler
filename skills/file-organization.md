# 檔案整理

整理本機或 Windows 節點上的檔案。

## 工具

**本機 Debian**

- `run_local_shell`：`find`、`mv`、`mkdir`、`rsync`、`du -sh`
- `read_own_code` / `write_own_code`：只限管家專案目錄

**Windows 節點**

- `list_node_files`、`search_node_files`、`read_node_file`、`write_node_file`
- `execute_node_command`：批次重新命名、搬移

## 工作流程

1. 先列出／搜尋，確認範圍，再搬移或刪除
2. 大量整理先建目標資料夾，再依副檔名／日期分類
3. 刪除前向主人確認（尤其是非暫存檔）
4. 整理完成回報：動了哪些路徑、數量

## 範例（PowerShell 分類副檔名）

```powershell
powershell -NoProfile -Command "$src='D:\Inbox'; Get-ChildItem $src -File | ForEach-Object { $d=Join-Path $src $_.Extension.TrimStart('.'); New-Item -ItemType Directory -Force -Path $d | Out-Null; Move-Item $_.FullName $d }"
```
