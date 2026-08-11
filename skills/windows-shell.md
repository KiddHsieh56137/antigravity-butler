# PowerShell / CMD（Windows 節點）

透過區網 Butler Node 探針操作 Windows。

## 工具

- `list_homelab_nodes()`：取得名稱與 IP
- `execute_node_command(node_ip, command)`：在該機執行指令
- `list_node_files` / `read_node_file` / `write_node_file` / `search_node_files`

## 選擇 shell

| 需求 | 指令前綴／寫法 |
|------|----------------|
| CMD | 直接下（探針預設多為 cmd）；`dir`、`type`、`copy` |
| PowerShell | `powershell -NoProfile -Command "..."` |
| 一行複雜邏輯 | PowerShell 較穩 |

## 別名（CMD）

探針／後端可能把 `ls→dir`、`cat→type`、`rm→del`、`cp→copy`、`mv→move`、`grep→findstr`。

## 慣例

- 先 `list_homelab_nodes` 對好 IP（例如工作站 192.168.1.188、kiddpc 192.168.1.126）
- 路徑用 Windows 風格：`C:\Users\...`
- 長輸出注意編碼；繁中亂碼時用 PowerShell `[Console]::OutputEncoding`
- 安裝軟體：可用 `winget install -e --id …`（若節點有 winget）
