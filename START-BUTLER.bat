@echo off
cd /d "%~dp0"
title Antigravity Butler HTTPS :8788

if not exist ".venv\Scripts\python.exe" (
  echo 尚未安裝。請先執行 SETUP-ON-105.ps1
  pause
  exit /b 1
)

if not exist "certs\butler.crt" (
  echo 產生區網自簽憑證...
  ".venv\Scripts\python.exe" -m pip install cryptography -q
  ".venv\Scripts\python.exe" scripts\gen_https_cert.py
  if errorlevel 1 (
    echo 憑證產生失敗
    pause
    exit /b 1
  )
)

echo Starting Antigravity Butler on https://0.0.0.0:8788 ...
echo 本機: https://127.0.0.1:8788/
echo 區網: https://192.168.1.105:8788/
echo 首次開啟請在瀏覽器接受自簽憑證警告。
echo 關閉此視窗即停止服務。
echo.

".venv\Scripts\python.exe" -m uvicorn butler_api:app --host 0.0.0.0 --port 8788 --ssl-keyfile certs\butler.key --ssl-certfile certs\butler.crt
echo.
echo 服務已結束。
pause
