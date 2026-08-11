Homelab Windows 探針一鍵安裝
============================

適用：要加入 Homelab 的 Windows 電腦（區網）

安裝步驟
--------
1. 解壓縮本 zip
2. 右鍵「Install-All.cmd」→ 以系統管理員身分執行
3. 完成後會安裝：
   - Butler Node 探針（埠 8789：檔案管理／終端機）
   - MeshAgent（遠端桌面，MeshCentral）

4. 回到 http://192.168.1.107:8788/ → 節點管理 →「掃描區網」

只要探針（不要遠端畫面）
------------------------
執行 Setup-Butler-Node.cmd（系統管理員）

只要遠端桌面
------------
執行 Install-MeshAgent-Only.cmd（系統管理員）
（需同目錄有 MeshAgent64-HomelabWindows.exe）

注意
----
- 需 Windows 10/11，並可連到 192.168.1.107
- 若無 Python，安裝程式會嘗試用 winget 安裝 Python 3.12
- 防火牆會開放 TCP 8789
