import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import threading

import httpx
import shutil
import subprocess
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, UploadFile, File, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from google import genai
from google.genai import types

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("butler")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "butler_config.json"
STATIC_DIR = ROOT / "static"

def load_butler_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
        logger.warning("butler_config.json missing or empty; using defaults.")
        return {}
    try:
        with CONFIG_PATH.open(encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error("Failed to load butler_config.json: %s", e)
        return {}

def save_butler_config(data: dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

# Load configuration (env overrides for public deploys)
config_data = load_butler_config()
ha_url = (os.environ.get("HA_URL") or config_data.get("ha_url") or "http://127.0.0.1:8123").rstrip("/")
ha_token = os.environ.get("HA_TOKEN") or config_data.get("ha_token", "")
gemini_key = os.environ.get("GEMINI_API_KEY") or config_data.get("gemini_api_key", "")

NODE_TOKEN = (
    os.environ.get("BUTLER_NODE_TOKEN")
    or str((config_data.get("node_token") or "change-me-butler-node-token"))
)
NODE_PORT = 8789
_discover_cache: dict[str, Any] = {"ts": 0.0, "nodes": []}
DISCOVER_TTL_SEC = 120

WIN_CMD_ALIASES = {
    "ls": "dir",
    "ll": "dir",
    "la": "dir /a",
    "cat": "type",
    "pwd": "cd",
    "clear": "cls",
    "which": "where",
    "rm": "del",
    "cp": "copy",
    "mv": "move",
    "grep": "findstr",
}

def _lan_prefix() -> str:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = "192.168.1.107"
    finally:
        sock.close()
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else "192.168.1"

def discover_lan_nodes(force: bool = False) -> list[dict[str, Any]]:
    """Scan LAN for butler probes on NODE_PORT and cache results briefly."""
    import concurrent.futures

    now = time.time()
    if not force and _discover_cache["nodes"] and now - _discover_cache["ts"] < DISCOVER_TTL_SEC:
        return list(_discover_cache["nodes"])

    prefix = _lan_prefix()
    headers = {"X-Butler-Token": NODE_TOKEN}
    found: list[dict[str, Any]] = []

    def check(i: int):
        ip = f"{prefix}.{i}"
        try:
            with httpx.Client(timeout=0.5) as client:
                resp = client.get(f"http://{ip}:{NODE_PORT}/api/status", headers=headers)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                sys_info = data.get("system", {}) or {}
                hostname = sys_info.get("hostname") or ip
                return {
                    "name": hostname,
                    "ip": ip,
                    "hostname": hostname,
                    "status": "online",
                    "ram": sys_info.get("ram_usage", "N/A"),
                    "disk": sys_info.get("disk_usage", "N/A"),
                    "os": sys_info.get("os", "N/A"),
                }
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for item in pool.map(check, range(1, 255)):
            if item:
                found.append(item)

    found.sort(key=lambda n: tuple(int(x) for x in n["ip"].split(".")))
    _discover_cache["ts"] = now
    _discover_cache["nodes"] = found
    return list(found)

def sync_discovered_nodes(force: bool = False) -> dict[str, Any]:
    """Merge LAN discoveries into butler_config.json nodes list."""
    global config_data
    discovered = discover_lan_nodes(force=force)
    existing = config_data.setdefault("nodes", [])
    existing_ips = {n.get("ip") for n in existing}
    ignored = {str(x).strip() for x in (config_data.get("ignored_node_ips") or []) if str(x).strip()}
    added = []
    skipped_ignored = []
    for item in discovered:
        ip = item["ip"]
        if ip in existing_ips:
            continue
        if ip in ignored:
            skipped_ignored.append(ip)
            continue
        node = {"name": item["name"], "ip": ip}
        existing.append(node)
        added.append(node)
        existing_ips.add(ip)
    if added:
        save_butler_config(config_data)
        logger.info("Auto-registered LAN nodes: %s", added)
    return {
        "discovered": discovered,
        "added": added,
        "skipped_ignored": skipped_ignored,
        "nodes": existing,
    }

def rewrite_windows_command(cmd: str) -> str:
    parts = cmd.split(None, 1)
    if not parts:
        return cmd
    base = parts[0].lower()
    if base not in WIN_CMD_ALIASES:
        return cmd
    mapped = WIN_CMD_ALIASES[base]
    rest = parts[1] if len(parts) > 1 else ""
    return f"{mapped} {rest}".strip()

def prepare_noninteractive_apt(cmd: str) -> str:
    """Force apt tools to run non-interactively in Web Terminal.

    Also rewrites `apt` → `apt-get` because `apt` warns when stdout is not a TTY
    ("apt does not have a stable CLI interface").
    """
    import re
    m = re.match(r"^(sudo\s+)?(apt-get|apt|aptitude)(\s+)(.*)$", cmd.strip(), re.IGNORECASE)
    if not m:
        return cmd
    sudo, tool, _, rest = m.groups()
    sudo = sudo or ""
    # apt is for humans/TTY; apt-get is the stable scripting interface
    if tool.lower() == "apt":
        tool = "apt-get"
    if re.search(r"(^|\s)(-y|--yes|--assume-yes)(\s|$)", rest):
        body = rest
    else:
        body = f"-y {rest}".strip() if rest else "-y"
    return f"{sudo}DEBIAN_FRONTEND=noninteractive {tool} {body}".strip()

def _homelab_host_ip() -> str:
    return (config_data.get("homelab_host") or "192.168.1.107").strip()

def _terminal_home() -> str:
    """OS-aware home directory for local Web Terminal sessions."""
    import os
    home = os.path.expanduser("~")
    if home and os.path.isdir(home):
        return home
    if os.name == "nt":
        return os.environ.get("USERPROFILE") or "C:\\"
    return "/home/past"

def _normalize_local_cwd(cwd: str | None) -> str:
    import os
    home = _terminal_home()
    c = (cwd or "").strip() or home
    if os.path.isdir(c):
        return c
    # Common mistake: Unix path sent to a Windows butler host
    if os.name == "nt" and (c.startswith("/") or c.startswith("~")):
        return home
    return home

def _nodes_for_ui() -> list[dict[str, Any]]:
    """Enrich nodes for selectors: correct local label, ensure Debian Homelab entry."""
    import os
    import socket
    nodes = [dict(n) for n in (config_data.get("nodes") or []) if isinstance(n, dict)]
    homelab = _homelab_host_ip()
    host = socket.gethostname()
    local_os = "Windows" if os.name == "nt" else "Linux"
    local_home = _terminal_home()

    for n in nodes:
        ip = str(n.get("ip") or "").strip()
        if ip in ("127.0.0.1", "localhost"):
            name = str(n.get("name") or "")
            if os.name == "nt" and ("Debian" in name or not name):
                n["name"] = f"Windows 本機 ({host})"
            elif os.name != "nt" and ("Windows" in name or not name):
                n["name"] = "Debian 伺服器 (本機)"
            n["os"] = local_os
            n["default_cwd"] = local_home
            n["is_local"] = True
            n["is_homelab"] = False
        elif ip == homelab:
            n["name"] = n.get("name") or "Debian Homelab"
            if "Windows" in str(n.get("name") or ""):
                n["name"] = "Debian Homelab"
            n["os"] = "Linux"
            n["default_cwd"] = "/home/past"
            n["is_local"] = False
            n["is_homelab"] = True
        else:
            n.setdefault("os", "Windows")
            n.setdefault("default_cwd", "C:\\")
            n["is_local"] = False
            n["is_homelab"] = False

    if not any(str(n.get("ip") or "").strip() == homelab for n in nodes):
        insert_at = 0 if os.name == "nt" else min(1, len(nodes))
        nodes.insert(insert_at, {
            "name": "Debian Homelab",
            "ip": homelab,
            "os": "Linux",
            "default_cwd": "/home/past",
            "is_local": False,
            "is_homelab": True,
        })
    return nodes

# Headers for Home Assistant API
ha_headers = {
    "Authorization": f"Bearer {ha_token}",
    "Content-Type": "application/json"
}

# Define Tools for the Agent
def get_entity_state(entity_id: str) -> str:
    """
    查詢 Home Assistant 中某個智慧家居設備（實體）的當前狀態與屬性細節。

    Args:
        entity_id: 實體識別碼，例如 'light.living_room_light'、'switch.bedroom_fan' 或 'sensor.temperature'。
    """
    print(f"!!! TOOL [get_entity_state] called with entity_id: {entity_id} !!!", flush=True)
    url = f"{ha_url}/api/states/{entity_id}"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=ha_headers)
            print(f"!!! HA Response: {resp.status_code} - {resp.text[:200]} !!!", flush=True)
            if resp.status_code == 200:
                data = resp.json()
                state = data.get("state", "未知")
                attributes = data.get("attributes", {})
                friendly_name = attributes.get("friendly_name", entity_id)
                res = f"實體【{friendly_name}】({entity_id}) 目前的狀態是 '{state}'。屬性細節：{json.dumps(attributes, ensure_ascii=False)}"
                print(f"!!! TOOL [get_entity_state] success: {res[:200]} !!!", flush=True)
                return res
            else:
                return f"Home Assistant 回傳錯誤：{resp.status_code} - {resp.text}"
    except Exception as e:
        print(f"!!! TOOL [get_entity_state] exception: {e} !!!", flush=True)
        return f"連線至 Home Assistant 時發生異常：{str(e)}"

def call_ha_service(domain: str, service: str, entity_id: str, data_json: str = "") -> str:
    """
    呼叫 Home Assistant 的服務來控制您的智慧家居設備（例如：開燈、關燈、調整溫度、切換開關等）。

    Args:
        domain: 服務類別（領域），例如 'light'（燈光）、'switch'（開關）、'climate'（溫控）或 'media_player'（播放器）。
        service: 要執行的動作，例如 'turn_on'（開啟）、'turn_off'（關閉）、'toggle'（切換）或 'set_temperature'（設定溫度）。
        entity_id: 要控制的設備 ID，例如 'light.living_room_light'。
        data_json: 可選。額外的參數 JSON 字串，例如設定亮度 '{"brightness": 180}' 或設定冷氣溫度 '{"temperature": 26}'。
    """
    print(f"!!! TOOL [call_ha_service] called: {domain}.{service} for {entity_id} with data_json: {data_json} !!!", flush=True)
    url = f"{ha_url}/api/services/{domain}/{service}"
    payload = {"entity_id": entity_id}
    if data_json and data_json.strip():
        try:
            extra_data = json.loads(data_json)
            payload.update(extra_data)
        except Exception as e:
            print(f"!!! TOOL [call_ha_service] json parse exception: {e} !!!", flush=True)
            return f"解析 extra_data 失敗，請確保 data_json 是合法的 JSON：{str(e)}"
            
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, headers=ha_headers)
            print(f"!!! HA Response: {resp.status_code} - {resp.text[:200]} !!!", flush=True)
            if resp.status_code != 200:
                return f"控制失敗，Home Assistant 回傳錯誤：{resp.status_code} - {resp.text}"

            # Verify state after on/off/toggle so the assistant doesn't bluff
            verify = ""
            if service in ("turn_on", "turn_off", "toggle"):
                import time as _time
                _time.sleep(0.35)
                st = client.get(f"{ha_url}/api/states/{entity_id}", headers=ha_headers)
                if st.status_code == 200:
                    body = st.json()
                    fname = (body.get("attributes") or {}).get("friendly_name") or entity_id
                    cur = body.get("state", "?")
                    verify = f" 確認後【{fname}】目前狀態為 '{cur}'。"
            return f"已成功呼叫 {domain}.{service} 控制 {entity_id}。{verify}".strip()
    except Exception as e:
        print(f"!!! TOOL [call_ha_service] exception: {e} !!!", flush=True)
        return f"控制過程中發生異常：{str(e)}"

def _normalize_device_name(text: str) -> str:
    t = str(text or "").strip().lower()
    for ch in (" ", "\u3000", "-", "_", "·", "・"):
        t = t.replace(ch, "")
    # Xiaomi HA often appends " 灯"
    for suf in ("灯", "燈", "開關", "开关"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t

def search_ha_devices(query: str, domain: str = "") -> str:
    """
    依使用者口語名稱／關鍵字搜尋 Home Assistant 設備（比對友好名稱與 entity_id）。
    當主人說「隔壁床頭燈」「客廳燈」等時，請優先使用此工具，不要憑空猜房間。

    Args:
        query: 搜尋關鍵字，例如 '隔壁床頭燈'、'白燈'、'床頭'。
        domain: 可選。限制種類，例如 'light'、'switch'、'climate'。
    """
    print(f"!!! TOOL [search_ha_devices] called query={query!r} domain={domain!r} !!!", flush=True)
    q = (query or "").strip()
    if not q:
        return "請提供搜尋關鍵字。"
    qn = _normalize_device_name(q)
    url = f"{ha_url}/api/states"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=ha_headers)
            if resp.status_code != 200:
                return f"Home Assistant 回傳錯誤：{resp.status_code} - {resp.text}"
            entities = resp.json()
            scored: list[tuple[int, dict[str, str]]] = []
            for entity in entities:
                entity_id = entity.get("entity_id", "")
                if domain and not entity_id.startswith(f"{domain}."):
                    continue
                # Skip noisy domains unless explicitly requested
                if not domain and entity_id.split(".", 1)[0] in (
                    "sensor", "binary_sensor", "button", "update", "person", "zone", "sun", "weather"
                ):
                    continue
                attrs = entity.get("attributes") or {}
                fname = str(attrs.get("friendly_name") or entity_id)
                fn = _normalize_device_name(fname)
                eid_n = _normalize_device_name(entity_id)
                score = 0
                if fn == qn or fname.strip() == q.strip():
                    score = 100
                elif qn and qn in fn:
                    score = 80 + min(19, len(qn))
                elif qn and all(part in fn for part in qn if len(part) >= 1) and len(qn) >= 2:
                    # soft: all chars present in order
                    idx = 0
                    ok = True
                    for ch in qn:
                        j = fn.find(ch, idx)
                        if j < 0:
                            ok = False
                            break
                        idx = j + 1
                    if ok:
                        score = 60
                elif qn and qn in eid_n:
                    score = 40
                else:
                    # token overlap for multi-word queries like 隔壁+床頭
                    tokens = [t for t in (q.replace("的", " ").replace("燈", "灯").split()) if t]
                    if not tokens and len(q) >= 2:
                        # split CJK bigrams-ish: prefer known tokens
                        for token in ("隔壁", "床頭", "床头", "客廳", "客厅", "臥室", "卧室", "白燈", "白灯"):
                            if token in q:
                                tokens.append(token)
                    hit = 0
                    for token in tokens:
                        tn = _normalize_device_name(token)
                        if tn and tn in fn:
                            hit += 1
                    if hit and tokens:
                        score = 30 + hit * 15
                if score <= 0:
                    continue
                scored.append((score, {
                    "entity_id": entity_id,
                    "friendly_name": fname,
                    "state": entity.get("state", ""),
                    "score": str(score),
                }))
            scored.sort(key=lambda x: (-x[0], x[1]["friendly_name"]))
            if not scored:
                return f"找不到名稱接近「{q}」的設備。可改用 find_entities 列出該類別全部設備。"
            top = scored[:8]
            best = top[0]
            lines = [f"搜尋「{q}」找到 {len(scored)} 筆，最佳匹配分數 {best[0]}："]
            for i, (score, r) in enumerate(top):
                mark = " ←最佳" if i == 0 else ""
                lines.append(
                    f"- 【{r['friendly_name']}】({r['entity_id']}) 狀態:{r['state']} 分數:{score}{mark}"
                )
            # Clear guidance for the model
            if best[0] >= 80:
                lines.append(
                    f"建議：分數夠高，直接控制 {best[1]['entity_id']}（【{best[1]['friendly_name']}】），不要再追問主人。"
                )
            elif len(top) >= 2 and best[0] - top[1][0] < 10:
                lines.append("建議：前幾名太接近，才需要向主人確認一次。")
            else:
                lines.append(f"建議：優先使用最佳匹配 {best[1]['entity_id']}。")
            return "\n".join(lines)
    except Exception as e:
        print(f"!!! TOOL [search_ha_devices] exception: {e} !!!", flush=True)
        return f"搜尋設備時發生異常：{str(e)}"

def find_entities(domain: str = "", state: str = "") -> str:
    """
    搜尋或篩選 Home Assistant 中的所有設備與感測器實體。
    可用於列出所有設備、或是根據類別（如 light 燈光、switch 開關）與特定狀態（如 on 開啟、off 關閉）來進行篩選。

    Args:
        domain: 可選。依設備種類進行篩選，例如 'light'（燈光）、'switch'（開關）、'sensor'（感測器）等。
        state: 可選。依特定狀態進行篩選，例如 'on'（開啟）、'off'（關閉）、'home'（在家）、'not_home'（離家）。
    """
    print(f"!!! TOOL [find_entities] called with domain: {domain}, state: {state} !!!", flush=True)
    url = f"{ha_url}/api/states"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=ha_headers)
            print(f"!!! HA Response: {resp.status_code} !!!", flush=True)
            if resp.status_code == 200:
                entities = resp.json()
                results = []
                for entity in entities:
                    entity_id = entity.get("entity_id", "")
                    ent_state = entity.get("state", "")
                    
                    if domain and not entity_id.startswith(f"{domain}."):
                        continue
                        
                    if state and ent_state != state:
                        continue
                        
                    attributes = entity.get("attributes", {})
                    friendly_name = attributes.get("friendly_name", entity_id)
                    results.append({
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "state": ent_state
                    })
                
                if not results:
                    return f"找不到符合條件的設備（篩選條件：domain='{domain}', state='{state}'）。"
                
                # Limit the output size to avoid blowing up token context window (max 50)
                limited_results = results[:50]
                summary = []
                for r in limited_results:
                    summary.append(f"設備名稱：【{r['friendly_name']}】({r['entity_id']}) -> 狀態：'{r['state']}'")
                
                output = f"找到 {len(results)} 個符合篩選條件的設備：\n" + "\n".join(summary)
                if len(results) > 50:
                    output += f"\n(還有 {len(results) - 50} 個設備未列出)"
                return output
            else:
                return f"Home Assistant 回傳錯誤：{resp.status_code} - {resp.text}"
    except Exception as e:
        print(f"!!! TOOL [find_entities] exception: {e} !!!", flush=True)
        return f"連線至 Home Assistant 進行設備搜尋時發生異常：{str(e)}"

def query_ha_logbook(hours: int = 1, entity_id: str = "") -> str:
    """
    查詢 Home Assistant 的歷史日誌（Logbook），了解最近發生了什麼事件，或特定設備的開關與觸發記錄。

    Args:
        hours: 查詢過去幾小時內的日誌，預設為 1 小時，最大建議不超過 24 小時。
        entity_id: 可選。若指定，則只查詢該特定設備的歷史日誌（例如 'light.living_room_light'）。
    """
    import datetime
    print(f"!!! TOOL [query_ha_logbook] called with hours: {hours}, entity_id: {entity_id} !!!", flush=True)
    
    # Calculate start time
    start_time = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat() + "Z"
    url = f"{ha_url}/api/logbook/{start_time}"
    
    params = {}
    if entity_id and entity_id.strip():
        params["entity"] = entity_id.strip()
        
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=ha_headers, params=params)
            print(f"!!! HA Response: {resp.status_code} !!!", flush=True)
            if resp.status_code == 200:
                entries = resp.json()
                if not entries:
                    return f"在過去 {hours} 小時內找不到任何事件記錄。"
                
                results = []
                # Limit to 40 entries to avoid context window explosion
                limited_entries = entries[-40:]  # Get the most recent ones
                for entry in limited_entries:
                    name = entry.get("name", "未知設備")
                    message = entry.get("message", "狀態更新")
                    ent_id = entry.get("entity_id", "")
                    when = entry.get("when", "")
                    
                    time_part = when
                    if "T" in when:
                        time_part = when.split("T")[1].split(".")[0]
                        
                    results.append(f"[{time_part}] 【{name}】({ent_id}) {message}")
                
                output = f"查詢過去 {hours} 小時，找到 {len(entries)} 筆記錄（已列出最近的 {len(results)} 筆）：\n" + "\n".join(results)
                return output
            else:
                return f"Home Assistant 回傳錯誤：{resp.status_code} - {resp.text}"
    except Exception as e:
        print(f"!!! TOOL [query_ha_logbook] exception: {e} !!!", flush=True)
        return f"連線至 Home Assistant 讀取日誌時發生異常：{str(e)}"

def get_host_system_status() -> str:
    """
    查詢 AI 助理管家所在的 Debian 伺服器主機系統狀態。
    可獲取 CPU 溫度、記憶體 (RAM) 使用量、硬碟空間 (Disk) 使用率與目前運行的 Docker 容器狀態。
    """
    import subprocess
    print("!!! TOOL [get_host_system_status] called !!!", flush=True)
    
    status_report = []
    
    # 1. CPU Temp
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            raw_temp = int(f.read().strip())
            cpu_temp = raw_temp / 1000.0
            status_report.append(f"🌡️ CPU 溫度：{cpu_temp:.1f}°C")
    except Exception as e:
        status_report.append(f"🌡️ CPU 溫度：讀取失敗 ({str(e)})")
        
    # 2. RAM Usage
    try:
        ram_info = subprocess.check_output("free -h", shell=True).decode("utf-8")
        status_report.append("📊 記憶體使用狀態：\n" + ram_info.strip())
    except Exception as e:
        status_report.append(f"📊 記憶體使用狀態：讀取失敗 ({str(e)})")
        
    # 3. Disk Usage
    try:
        disk_info = subprocess.check_output("df -h /", shell=True).decode("utf-8")
        status_report.append("💾 硬碟空間狀態：\n" + disk_info.strip())
    except Exception as e:
        status_report.append(f"💾 硬碟空間狀態：讀取失敗 ({str(e)})")
        
    # 4. Docker Containers
    try:
        docker_info = subprocess.check_output("docker ps --format 'table {{.Names}}\t{{.Status}}'", shell=True).decode("utf-8")
        status_report.append("🐳 Docker 容器運行狀態：\n" + docker_info.strip())
    except Exception as e:
        status_report.append(f"🐳 Docker 容器運行狀態：讀取失敗 ({str(e)})")
        
    return "\n\n".join(status_report)

def get_zigbee_network_info() -> str:
    """
    查詢目前智慧家居 Hub 的 Zigbee2MQTT 網路架喚與配對設備。
    可獲取 Zigbee 協調器 (Coordinator) 連線位址、MQTT 伺服器設定，以及所有配對的 Zigbee 終端設備 (EndDevice) 與路由中繼器 (Router) 清單。
    """
    import json
    from pathlib import Path
    
    print("!!! TOOL [get_zigbee_network_info] called !!!", flush=True)
    
    config_path = Path("/home/past/zigbee2mqtt/data/configuration.yaml")
    db_path = Path("/home/past/zigbee2mqtt/data/database.db")
    
    report = []
    friendly_names = {}
    
    # 1. Read configuration.yaml
    if config_path.exists():
        try:
            mqtt_server = ""
            mqtt_topic = ""
            serial_port = ""
            frontend_port = ""
            
            with config_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
                i = 0
                while i < len(lines):
                    line = lines[i]
                    stripped = line.strip()
                    if stripped.startswith("mqtt:"):
                        while i + 1 < len(lines) and (lines[i+1].startswith(" ") or lines[i+1].startswith("\t")):
                            i += 1
                            m_line = lines[i].strip()
                            if m_line.startswith("server:"):
                                mqtt_server = m_line.split(":", 1)[1].strip().strip("'").strip('"')
                            elif m_line.startswith("base_topic:"):
                                mqtt_topic = m_line.split(":", 1)[1].strip().strip("'").strip('"')
                    elif stripped.startswith("serial:"):
                        while i + 1 < len(lines) and (lines[i+1].startswith(" ") or lines[i+1].startswith("\t")):
                            i += 1
                            s_line = lines[i].strip()
                            if s_line.startswith("port:"):
                                serial_port = s_line.split(":", 1)[1].strip().strip("'").strip('"')
                    elif stripped.startswith("frontend:"):
                        while i + 1 < len(lines) and (lines[i+1].startswith(" ") or lines[i+1].startswith("\t")):
                            i += 1
                            f_line = lines[i].strip()
                            if f_line.startswith("port:"):
                                frontend_port = f_line.split(":", 1)[1].strip().strip("'").strip('"')
                    elif stripped.startswith("devices:"):
                        while i + 1 < len(lines) and (lines[i+1].startswith(" ") or lines[i+1].startswith("\t")):
                            i += 1
                            d_line = lines[i]
                            if d_line.startswith("  ") and not d_line.startswith("    "):
                                addr = d_line.strip().strip(":").strip("'").strip('"')
                                friendly_name = addr
                                if i + 1 < len(lines) and lines[i+1].startswith("    "):
                                    i += 1
                                    fn_line = lines[i].strip()
                                    if fn_line.startswith("friendly_name:"):
                                        friendly_name = fn_line.split(":", 1)[1].strip().strip("'").strip('"')
                                friendly_names[addr] = friendly_name
                    i += 1
            
            report.append("🔌 **Hub 基礎網路架構**：")
            report.append(f"- **MQTT 伺服器**：`{mqtt_server}` (主題首碼: `{mqtt_topic}`)")
            report.append(f"- **協調器通訊埠**：`{serial_port}` (透過 TCP 網路序列埠連線)")
            report.append(f"- **Zigbee2MQTT 控制台**：`http://192.168.1.107:{frontend_port}/` (區網控制網頁)")
        except Exception as e:
            report.append(f"❌ 讀取 configuration.yaml 失敗：{str(e)}")
    else:
        report.append("❌ 找不到 Zigbee2MQTT 設定檔 (configuration.yaml)")

    # 2. Read database.db
    if db_path.exists():
        try:
            devices = []
            with db_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        dev_data = json.loads(line)
                        devices.append(dev_data)
            
            report.append("\n🐝 **配對的 Zigbee 設備清單**：")
            for dev in devices:
                dev_type = dev.get("type", "未知")
                addr = dev.get("ieeeAddr", "")
                manuf = dev.get("manufName", "未知廠商")
                model = dev.get("modelId", "未知型號")
                power = dev.get("powerSource", "未知電源")
                
                if dev_type == "Coordinator":
                    report.append(f"- 👑 **協調器 (Coordinator)**: `{addr}`")
                    continue
                
                name = friendly_names.get(addr, addr)
                type_desc = "🔋 終端設備 (EndDevice)" if dev_type == "EndDevice" else "🔌 路由中繼器 (Router，可放大信號)"
                report.append(f"- 📱 **{name}** ({addr}):")
                report.append(f"  * 類型：{type_desc}")
                report.append(f"  * 廠商/型號：{manuf} ({model})")
                report.append(f"  * 電源供應：{power}")
        except Exception as e:
            report.append(f"❌ 讀取 database.db 失敗：{str(e)}")
    else:
        report.append("❌ 找不到 Zigbee 設備資料庫 (database.db)")
        
    return "\n".join(report)

def call_node_api(node_ip: str, method: str, endpoint: str, params: dict = None, json_data: dict = None) -> dict:
    url = f"http://{node_ip}:8789{endpoint}"
    headers = {
        "X-Butler-Token": NODE_TOKEN,
        "Content-Type": "application/json"
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            if method.upper() == "GET":
                resp = client.get(url, headers=headers, params=params)
            else:
                resp = client.post(url, headers=headers, json=json_data)
            
            if resp.status_code == 200:
                return {"status": "success", "data": resp.json()}
            else:
                return {"status": "error", "message": f"節點回傳錯誤：{resp.status_code} - {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": f"連線至節點 {node_ip} 失敗：{str(e)}"}

def get_node_status(node_ip: str) -> str:
    """
    查詢指定工作站電腦節點的系統與硬體狀態。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126' 或 '192.168.1.188'。
    """
    print(f"!!! TOOL [get_node_status] called for node_ip: {node_ip} !!!", flush=True)
    res = call_node_api(node_ip, "GET", "/api/status")
    if res["status"] == "success":
        sys_info = res["data"].get("system", {})
        return (
            f"節點主機 {node_ip} 目前在線。\n"
            f"🖥️ 系統：{sys_info.get('os')} ({sys_info.get('release')})\n"
            f"🏷️ 主機名稱：{sys_info.get('hostname')}\n"
            f"🌡️ CPU 使用率：{sys_info.get('cpu_usage')}\n"
            f"📊 記憶體使用率：{sys_info.get('ram_usage')}\n"
            f"💾 主硬碟使用率：{sys_info.get('disk_usage')}"
        )
    else:
        return res["message"]

def list_node_files(node_ip: str, path: str = "") -> str:
    """
    列出指定工作站電腦節點上某個目錄（路徑）下的所有檔案與子資料夾。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126'。
        path: 要列出的資料夾絕對路徑（例如 Windows 的 'C:\\Users' 或 Linux 的 '/home'）。若留空則會列出允許的根目錄。
    """
    print(f"!!! TOOL [list_node_files] called for node_ip: {node_ip}, path: {path} !!!", flush=True)
    params = {}
    if path:
        params["path"] = path
    res = call_node_api(node_ip, "GET", "/api/files/list", params=params)
    if res["status"] == "success":
        data = res["data"]
        current_path = data.get("path", "")
        dirs = data.get("directories", [])
        files = data.get("files", [])
        
        output = [f"📂 節點 {node_ip} 上的目錄：{current_path}\n"]
        if dirs:
            output.append("【子資料夾】:")
            for d in dirs:
                output.append(f"  📁 {d}")
        if files:
            output.append("\n【檔案】:")
            for f in files:
                size_kb = f.get("size", 0) / 1024
                output.append(f"  📄 {f.get('name')} ({size_kb:.1f} KB)")
        if not dirs and not files:
            output.append("（此資料夾是空的）")
        return "\n".join(output)
    else:
        return res["message"]

def search_node_files(node_ip: str, path: str, query: str) -> str:
    """
    在指定工作站電腦節點的目錄下，搜尋符合特定關鍵字的檔案。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126'。
        path: 要開始搜尋的資料夾路徑。
        query: 關鍵字名稱（支援部分模糊比對，如 'report' 或是 'txt'）。
    """
    print(f"!!! TOOL [search_node_files] called for node_ip: {node_ip}, path: {path}, query: {query} !!!", flush=True)
    params = {"path": path, "query": query}
    res = call_node_api(node_ip, "GET", "/api/files/search", params=params)
    if res["status"] == "success":
        results = res["data"].get("results", [])
        if not results:
            return f"在節點 {node_ip} 的 {path} 目錄下找不到任何檔名含有 '{query}' 的檔案。"
        
        output = [f"🔍 在節點 {node_ip} 的 {path} 中找到 {len(results)} 個符合檔案："]
        for r in results:
            size_kb = r.get("size", 0) / 1024
            output.append(f"- 📄 {r.get('name')} ({size_kb:.1f} KB) -> 路徑: `{r.get('path')}`")
        return "\n".join(output)
    else:
        return res["message"]

def read_node_file(node_ip: str, file_path: str) -> str:
    """
    讀取並讀出指定工作站電腦節點上的特定檔案內容。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126'。
        file_path: 要讀取的檔案絕對路徑（限 500KB 以下的文字檔、程式碼或設定檔）。
    """
    print(f"!!! TOOL [read_node_file] called for node_ip: {node_ip}, file_path: {file_path} !!!", flush=True)
    res = call_node_api(node_ip, "POST", "/api/files/read", json_data={"file_path": file_path})
    if res["status"] == "success":
        data = res["data"]
        content = data.get("content", "")
        size_kb = data.get("size", 0) / 1024
        return f"📄 讀取檔案成功 ({size_kb:.1f} KB)：\n---\n{content}\n---"
    else:
        return res["message"]

def write_node_file(node_ip: str, file_path: str, content: str) -> str:
    """
    在指定工作站電腦節點上建立新檔案或覆寫現有檔案的內容。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126'。
        file_path: 要寫入的檔案絕對路徑。
        content: 要寫入的文字內容。
    """
    print(f"!!! TOOL [write_node_file] called for node_ip: {node_ip}, file_path: {file_path} !!!", flush=True)
    res = call_node_api(node_ip, "POST", "/api/files/write", json_data={"file_path": file_path, "content": content})
    if res["status"] == "success":
        return f"✅ 成功將內容寫入節點 {node_ip} 的檔案：`{file_path}` (寫入大小：{len(content)} 字元)。"
    else:
        return res["message"]

def execute_node_command(node_ip: str, command: str) -> str:
    """
    在指定工作站電腦節點上執行本機命令（CMD/Shell/PowerShell 命令）並返回輸出結果。

    Args:
        node_ip: 節點主機的 IP 位址，例如 '192.168.1.126'。
        command: 要執行的 Terminal 指令（例如 'dir'、'ping'、'python script.py'）。
    """
    print(f"!!! TOOL [execute_node_command] called for node_ip: {node_ip}, command: {command} !!!", flush=True)
    res = call_node_api(node_ip, "POST", "/api/system/command", json_data={"command": command})
    if res["status"] == "success":
        data = res["data"]
        exit_code = data.get("exit_code", 0)
        stdout = data.get("stdout", "")
        stderr = data.get("stderr", "")
        
        output = [f"💻 節點 {node_ip} 指令執行完畢 (結束代碼: {exit_code})"]
        if stdout.strip():
            output.append("【標準輸出】:\n" + stdout)
        if stderr.strip():
            output.append("【標準錯誤】:\n" + stderr)
        if not stdout.strip() and not stderr.strip():
            output.append("（執行成功，沒有任何輸出）")
        return "\n".join(output)
    else:
        return res["message"]

def get_current_datetime() -> str:
    """取得台灣（Asia/Taipei）目前日期與時間。問幾點、今天幾號時使用。"""
    from datetime import datetime, timezone, timedelta
    tw = timezone(timedelta(hours=8))
    now = datetime.now(tw)
    weekdays = "一二三四五六日"
    return now.strftime(f"%Y-%m-%d（週{weekdays[now.weekday()]}）%H:%M:%S（台灣時間）")

def list_homelab_nodes() -> str:
    """列出 Homelab 已登記的電腦節點（名稱與 IP），方便選對機器再執行指令或查檔案。"""
    nodes = config_data.get("nodes") or []
    if not nodes:
        return "目前尚未登記任何節點。"
    lines = ["已登記節點："]
    for n in nodes:
        name = (n or {}).get("name") or "未命名"
        ip = (n or {}).get("ip") or "?"
        lines.append(f"- {name} → {ip}")
    mesh = (config_data.get("meshcentral") or {}).get("url") or "https://192.168.1.107:8089/"
    lines.append(f"遠端桌面閘道（MeshCentral）：{mesh}")
    return "\n".join(lines)

def get_weather_forecast(place: str = "", days: int = 2) -> str:
    """
    查詢台灣地區天氣預報（Open-Meteo，免金鑰）。主人問今天／明天天氣、會不會下雨時使用。

    Args:
        place: 地名，例如 '台南'、'新市'、'台北'；空白則用設定預設（通常台南）。
        days: 預報天數 1～7，預設 2（今天+明天）。
    """
    print(f"!!! TOOL [get_weather_forecast] place={place!r} days={days} !!!", flush=True)
    asst = (config_data.get("assistant") or {}) if isinstance(config_data, dict) else {}
    default_place = str(asst.get("weather_place") or "台南").strip() or "台南"
    q = (place or "").strip() or default_place
    try:
        days_n = max(1, min(7, int(days or 2)))
    except Exception:
        days_n = 2

    # WMO weather codes (brief)
    code_map = {
        0: "晴", 1: "大致晴", 2: "多雲", 3: "陰",
        45: "霧", 48: "霧凇",
        51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
        61: "小雨", 63: "中雨", 65: "大雨",
        71: "小雪", 73: "中雪", 75: "大雪",
        80: "陣雨", 81: "陣雨", 82: "強陣雨",
        95: "雷雨", 96: "雷雨伴冰雹", 99: "雷雨伴冰雹",
    }

    try:
        with httpx.Client(timeout=12.0) as client:
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": q, "count": 5, "language": "zh", "format": "json"},
            )
            if geo.status_code != 200:
                return f"地點查詢失敗：HTTP {geo.status_code}"
            results = (geo.json() or {}).get("results") or []
            # Prefer Taiwan
            pick = None
            for r in results:
                if str(r.get("country_code") or "").upper() == "TW":
                    pick = r
                    break
            if not pick and results:
                pick = results[0]
            if not pick:
                return f"找不到「{q}」的位置，請換個地名再試。"

            lat = pick["latitude"]
            lon = pick["longitude"]
            label = pick.get("name") or q
            admin = pick.get("admin1") or ""
            where = f"{label}" + (f"（{admin}）" if admin and admin != label else "")

            fc = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": "Asia/Taipei",
                    "forecast_days": days_n,
                    "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
                    "current_weather": True,
                },
            )
            if fc.status_code != 200:
                return f"天氣查詢失敗：HTTP {fc.status_code}"
            body = fc.json() or {}
            daily = body.get("daily") or {}
            dates = daily.get("time") or []
            codes = daily.get("weathercode") or []
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            precip = daily.get("precipitation_sum") or []
            pop = daily.get("precipitation_probability_max") or []

            lines = [f"【{where}】天氣預報（Open-Meteo）"]
            cur = body.get("current_weather") or {}
            if cur:
                ccode = int(cur.get("weathercode") or 0)
                lines.append(
                    f"目前：{code_map.get(ccode, '不明')}，{cur.get('temperature')}°C，風速 {cur.get('windspeed')} km/h"
                )
            for i, d in enumerate(dates):
                c = int(codes[i]) if i < len(codes) else 0
                desc = code_map.get(c, f"代碼{c}")
                hi = tmax[i] if i < len(tmax) else "?"
                lo = tmin[i] if i < len(tmin) else "?"
                rain = precip[i] if i < len(precip) else "?"
                chance = pop[i] if i < len(pop) else "?"
                lines.append(f"{d}：{desc}，{lo}～{hi}°C，降雨量 {rain} mm，降雨機率 {chance}%")
            return "\n".join(lines)
    except Exception as e:
        print(f"!!! TOOL [get_weather_forecast] exception: {e} !!!", flush=True)
        return f"查天氣時發生錯誤：{e}"

def web_search(query: str, max_results: int = 5) -> str:
    """
    上網搜尋（DuckDuckGo）。不知道答案、需要最新資訊、新聞、教學、規格時使用。
    天氣請優先用 get_weather_forecast；家電請用 HA 工具。

    Args:
        query: 搜尋關鍵字，例如 '台南明天天氣'、'MeshCentral WebRTC'。
        max_results: 回傳筆數，1～8，預設 5。
    """
    print(f"!!! TOOL [web_search] query={query!r} !!!", flush=True)
    q = " ".join(str(query or "").split()).strip()
    if not q:
        return "請提供搜尋關鍵字。"
    try:
        n = max(1, min(8, int(max_results or 5)))
    except Exception:
        n = 5
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            return "上網套件未安裝（ddgs）。請在伺服器 venv 執行：pip install ddgs"

    try:
        rows = []
        ddgs = DDGS()
        try:
            results = ddgs.text(q, region="tw-tzh", max_results=n) or []
        except TypeError:
            results = ddgs.text(q, max_results=n) or []
        except Exception:
            results = ddgs.text(q, max_results=n) or []
        for i, item in enumerate(results):
            title = (item.get("title") or "").strip()
            href = (item.get("href") or item.get("link") or "").strip()
            body = (item.get("body") or item.get("snippet") or "").strip()
            rows.append(f"{i+1}. {title}\n   {body}\n   {href}")
        if not rows:
            return f"搜尋「{q}」沒有結果。"
        return f"搜尋「{q}」結果（請依最新可靠來源整理回答，勿憑舊記憶）：\n" + "\n".join(rows)
    except Exception as e:
        print(f"!!! TOOL [web_search] exception: {e} !!!", flush=True)
        return f"上網搜尋失敗：{e}"

def fetch_webpage(url: str, max_chars: int = 4000) -> str:
    """
    抓取網頁文字內容（去 HTML）。搜尋後需要讀某一頁細節時使用。

    Args:
        url: 完整 URL，需 http/https。
        max_chars: 最多回傳字元數，預設 4000。
    """
    print(f"!!! TOOL [fetch_webpage] url={url!r} !!!", flush=True)
    u = str(url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return "URL 必須以 http:// 或 https:// 開頭。"
    try:
        limit = max(500, min(12000, int(max_chars or 4000)))
    except Exception:
        limit = 4000
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={
            "User-Agent": "AntigravityButler/1.0 (+homelab; web-fetch)"
        }) as client:
            resp = client.get(u)
            if resp.status_code >= 400:
                return f"抓取失敗：HTTP {resp.status_code}"
            ctype = (resp.headers.get("content-type") or "").lower()
            text = resp.text or ""
            if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
                import re
                text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
                text = re.sub(r"(?is)<[^>]+>", " ", text)
                text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                            .replace("&lt;", "<").replace("&gt;", ">"))
                text = re.sub(r"\s+", " ", text).strip()
            text = " ".join(text.split())
            if len(text) > limit:
                text = text[:limit] + "…（已截斷）"
            if not text:
                return "頁面沒有可讀文字。"
            return f"【{u}】\n{text}"
    except Exception as e:
        print(f"!!! TOOL [fetch_webpage] exception: {e} !!!", flush=True)
        return f"抓取網頁失敗：{e}"

SKILLS_DIR = ROOT / "skills"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
VENV_PIP = ROOT / ".venv" / "bin" / "pip"
OWN_CODE_ALLOW = {
    "butler_api.py", "butler_config.json", "butler_memory.json", "requirements.txt",
    "butler_node.py", "static/index.html",
}

def _run_cmd(cmd: str | list[str], timeout: float = 120.0, cwd: str | None = None, shell: bool = True) -> str:
    import os
    try:
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        completed = subprocess.run(
            cmd,
            shell=shell,
            cwd=cwd or str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        out = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
        out = out.strip()
        return f"exit={completed.returncode}\n{out}" if out else f"exit={completed.returncode}（無輸出）"
    except subprocess.TimeoutExpired:
        return f"指令逾時（>{timeout}s）：{cmd}"
    except Exception as e:
        return f"執行失敗：{e}"

def load_skill(name: str = "") -> str:
    """
    載入管家技能手冊（Debian／Python／PowerShell／CMD／檔案整理／自我擴充）。
    做系統管理、寫腳本、整理檔案、或要擴充自己能力前先讀。

    Args:
        name: 技能名，例如 'debian-admin'、'python'、'windows-shell'、'file-organization'、'self-extend'。
              空白則列出可用技能。
    """
    if not SKILLS_DIR.is_dir():
        return "技能目錄不存在。"
    files = sorted(SKILLS_DIR.glob("*.md"))
    if not name or not str(name).strip():
        lines = ["可用技能手冊："]
        for f in files:
            lines.append(f"- {f.stem}")
        return "\n".join(lines)
    key = str(name).strip().lower().replace(" ", "-").replace("_", "-")
    aliases = {
        "debian": "debian-admin",
        "linux": "debian-admin",
        "ps": "windows-shell",
        "powershell": "windows-shell",
        "cmd": "windows-shell",
        "windows": "windows-shell",
        "files": "file-organization",
        "file": "file-organization",
        "extend": "self-extend",
        "self": "self-extend",
    }
    key = aliases.get(key, key)
    path = SKILLS_DIR / f"{key}.md"
    if not path.exists():
        return f"找不到技能「{name}」。可用：{', '.join(p.stem for p in files)}"
    return path.read_text(encoding="utf-8")

def run_local_shell(command: str, timeout_sec: int = 120) -> str:
    """
    在管家所在的 Debian 本機執行 shell 指令（bash）。apt 會自動非互動。

    Args:
        command: shell 指令，例如 'df -h'、'sudo apt-get update'。
        timeout_sec: 逾時秒數，預設 120。
    """
    print(f"!!! TOOL [run_local_shell] cmd={command!r} !!!", flush=True)
    cmd = prepare_noninteractive_apt(str(command or "").strip())
    if not cmd:
        return "請提供指令。"
    try:
        t = max(5, min(600, int(timeout_sec or 120)))
    except Exception:
        t = 120
    return _run_cmd(cmd, timeout=float(t))

def run_python(code: str, timeout_sec: int = 60) -> str:
    """
    用管家 venv 的 Python 執行短程式碼，回傳輸出。

    Args:
        code: Python 原始碼（完整小腳本）。
        timeout_sec: 逾時秒數。
    """
    print("!!! TOOL [run_python] called !!!", flush=True)
    src = str(code or "").strip()
    if not src:
        return "請提供 Python 程式碼。"
    py = str(VENV_PYTHON if VENV_PYTHON.exists() else "python3")
    import tempfile, os
    fd, path = tempfile.mkstemp(prefix="butler_py_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            t = max(5, min(300, int(timeout_sec or 60)))
        except Exception:
            t = 60
        return _run_cmd([py, path], timeout=float(t), shell=False)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

def install_system_package(package: str) -> str:
    """
    在 Debian 用 apt-get 安裝系統套件（非互動 -y）。

    Args:
        package: 套件名，例如 'jq'、'ffmpeg'、'ripgrep'。
    """
    pkg = " ".join(str(package or "").split())
    if not pkg or any(c in pkg for c in ";|&`$"):
        return "套件名稱不合法。"
    print(f"!!! TOOL [install_system_package] {pkg} !!!", flush=True)
    update = _run_cmd("sudo apt-get update -y", timeout=180.0)
    install = _run_cmd(f"sudo apt-get install -y {pkg}", timeout=600.0)
    return f"【update】\n{update}\n\n【install {pkg}】\n{install}"

def install_python_package(package: str) -> str:
    """
    安裝 Python 套件到管家虛擬環境（.venv）。

    Args:
        package: PyPI 套件名，例如 'pandas'、'beautifulsoup4'。
    """
    pkg = " ".join(str(package or "").split())
    if not pkg or any(c in pkg for c in ";|&`$"):
        return "套件名稱不合法。"
    print(f"!!! TOOL [install_python_package] {pkg} !!!", flush=True)
    pip = str(VENV_PIP if VENV_PIP.exists() else "pip3")
    return _run_cmd([pip, "install", pkg], timeout=600.0, shell=False)

def read_own_code(relative_path: str) -> str:
    """
    讀取管家專案內的原始碼／設定（相對路徑）。改自己能力前先讀。

    Args:
        relative_path: 例如 'butler_api.py'、'skills/python.md'、'static/index.html'。
    """
    rel = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return "路徑不合法。"
    path = (ROOT / rel).resolve()
    if not str(path).startswith(str(ROOT.resolve())):
        return "只能讀取管家專案目錄內的檔案。"
    if not path.is_file():
        return f"找不到檔案：{rel}"
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > 60000:
        return text[:60000] + "\n…（已截斷）"
    return text

def write_own_code(relative_path: str, content: str) -> str:
    """
    寫入／覆寫管家專案內檔案以擴充能力（例如新增 tool）。寫完後通常要 restart_butler_service。

    Args:
        relative_path: 相對路徑，限專案內；禁止改 .venv、__pycache__、.ssh。
        content: 完整檔案內容。
    """
    rel = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return "路徑不合法。"
    blocked = (".venv/", "__pycache__/", ".ssh/", ".git/")
    if any(rel.startswith(b) or f"/{b}" in f"/{rel}" for b in blocked):
        return "禁止寫入此路徑。"
    path = (ROOT / rel).resolve()
    if not str(path).startswith(str(ROOT.resolve())):
        return "只能寫入管家專案目錄。"
    # Allow skills/*, static/*, and known files; also any .py/.md/.json/.html/.txt under ROOT
    ok_ext = rel.endswith((".py", ".md", ".json", ".html", ".txt", ".cmd", ".ps1", ".sh"))
    if not ok_ext and rel not in OWN_CODE_ALLOW:
        return f"副檔名不被允許。可寫 .py/.md/.json/.html/.txt 或 {sorted(OWN_CODE_ALLOW)}"
    print(f"!!! TOOL [write_own_code] {rel} bytes={len(content or '')} !!!", flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(content or ""), encoding="utf-8")
    return f"已寫入 {rel}（{len(content or '')} 字元）。若改了 butler_api.py，請呼叫 restart_butler_service。"

def restart_butler_service() -> str:
    """重啟管家服務以載入新程式碼／新工具。改完自身程式後呼叫。"""
    print("!!! TOOL [restart_butler_service] !!!", flush=True)
    # Delay restart so HTTP response can return
    import threading

    def _later():
        time.sleep(1.2)
        subprocess.Popen(
            "sudo systemctl restart antigravity-butler",
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    threading.Thread(target=_later, daemon=True).start()
    return "已排程重啟管家服務（約 2 秒後）。請稍候再下指令；新工具會在重啟後可用。"

def list_my_skills() -> str:
    """回傳管家目前已啟用的全部技能清單。主人問「你會什麼／有什麼技能」時呼叫。"""
    skill_names = []
    if SKILLS_DIR.is_dir():
        skill_names = [p.stem for p in sorted(SKILLS_DIR.glob("*.md"))]
    return (
        "已啟用技能：\n"
        "1. 智慧家電／HA／Zigbee\n"
        "2. 天氣預報、上網搜尋與讀網頁\n"
        "3. Debian 本機 shell／apt／主機健檢\n"
        "4. Python（venv 執行與 pip 安裝）\n"
        "5. Windows 節點 PowerShell／CMD／檔案讀寫\n"
        "6. 檔案整理（本機＋節點）\n"
        "7. 長期記憶、報時、閒聊\n"
        "8. 自我擴充：缺能力可裝套件或改自身程式後重啟\n"
        f"技能手冊（load_skill）：{', '.join(skill_names) or '（無）'}\n"
        "提示：Cursor／Antigravity 的 coding skills 不會自動進來；管家用自己的 tools＋skills/。"
    )

# Setup FastAPI App
app = FastAPI(title="Google Antigravity AI Butler", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MEMORY_PATH = ROOT / "butler_memory.json"
LOGS_DIR = ROOT / "logs"
SESSION_ID = "default"
SESSION_MAX_TURNS = 10
# Short-term rolling session (last N turns). Restored from JSONL on startup.
_session_turns: deque = deque(maxlen=SESSION_MAX_TURNS)
_chat_lock = threading.Lock()

def _ensure_session_store():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

def _chat_log_path(dt: datetime | None = None) -> Path:
    d = dt or datetime.now(timezone.utc)
    return LOGS_DIR / f"chat_{d.strftime('%Y%m')}.jsonl"

def append_chat_audit(entry: dict[str, Any]) -> None:
    """Append one conversation turn to monthly JSONL (sync; call from BackgroundTasks)."""
    try:
        _ensure_session_store()
        path = _chat_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("append_chat_audit failed: %s", e)

def _load_recent_turns(limit: int = SESSION_MAX_TURNS) -> list[dict[str, Any]]:
    """Read newest turns from the latest chat_*.jsonl files."""
    _ensure_session_store()
    files = sorted(LOGS_DIR.glob("chat_*.jsonl"))
    if not files:
        return []
    rows: list[dict[str, Any]] = []
    for path in reversed(files[-3:]):  # last 3 months max
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and (obj.get("content") or obj.get("response")):
                rows.append(obj)
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows[-limit:]

def search_chat_history(query: str, months: int = 3, limit: int = 20) -> str:
    """
    搜尋過去對話 JSONL 日誌（logs/chat_YYYYMM.jsonl）。
    需要查幾週／幾個月前的舊對話時使用。

    Args:
        query: 關鍵字（空白則回傳最近幾筆摘要）。
        months: 往回搜尋幾個月份檔（預設 3）。
        limit: 最多回傳幾筆。
    """
    _ensure_session_store()
    q = (query or "").strip().lower()
    months = max(1, min(int(months or 3), 24))
    limit = max(1, min(int(limit or 20), 50))
    files = sorted(LOGS_DIR.glob("chat_*.jsonl"))[-months:]
    hits: list[str] = []
    for path in reversed(files):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            blob = json.dumps(obj, ensure_ascii=False)
            if q and q not in blob.lower():
                continue
            ts = obj.get("timestamp", "")
            user = (obj.get("content") or "")[:120]
            resp = (obj.get("response") or "")[:160]
            hits.append(f"- [{ts}] 使用者：{user}\n  回覆：{resp}")
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if not hits:
        return f"對話日誌中找不到與「{query or '（最近）'}」相關的紀錄。"
    return "搜尋結果：\n" + "\n".join(hits)

def _history_parts_from_turn(turn: dict[str, Any]) -> list[Any]:
    """Build Gemini Content list for one audited turn (user + model)."""
    contents = []
    user_text = (turn.get("content") or "").strip()
    reply = (turn.get("response") or "").strip()
    if user_text:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
    if reply:
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
    return contents

def _extract_tools_from_history(chat) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tool_calls: list[dict[str, Any]] = []
    tool_responses: list[dict[str, Any]] = []
    try:
        for msg in chat.get_history() or []:
            for part in (getattr(msg, "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc:
                    try:
                        args = dict(fc.args) if getattr(fc, "args", None) is not None else {}
                    except Exception:
                        args = {"raw": str(getattr(fc, "args", ""))}
                    tool_calls.append({"name": getattr(fc, "name", "?"), "args": args})
                fr = getattr(part, "function_response", None)
                if fr:
                    resp = getattr(fr, "response", None)
                    try:
                        if hasattr(resp, "items"):
                            resp_s = dict(resp)
                        else:
                            resp_s = str(resp)[:2000]
                    except Exception:
                        resp_s = str(resp)[:2000]
                    tool_responses.append({"name": getattr(fr, "name", "?"), "response": resp_s})
    except Exception:
        pass
    # Keep only the latest burst (this turn) — last few tool pairs
    return tool_calls[-8:], tool_responses[-8:]

def _ai_brain_url() -> str:
    """If set, this host proxies/redirects chat+voice to the AI brain host (.105)."""
    asst = (config_data.get("assistant") or {}) if isinstance(config_data, dict) else {}
    url = (asst.get("ai_brain_url") or config_data.get("ai_brain_url") or "").strip().rstrip("/")
    return url

def _httpx_brain_client(timeout: float = 120.0) -> httpx.Client:
    """Client for calling AI brain; allow self-signed HTTPS on LAN."""
    return httpx.Client(timeout=timeout, verify=False)

def _load_memory() -> dict[str, Any]:
    if not MEMORY_PATH.exists():
        return {"notes": [], "preferences": {}, "facts": []}
    try:
        with MEMORY_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"notes": [], "preferences": {}, "facts": []}
        data.setdefault("notes", [])
        data.setdefault("preferences", {})
        data.setdefault("facts", [])
        return data
    except Exception:
        return {"notes": [], "preferences": {}, "facts": []}

def _save_memory(data: dict[str, Any]) -> None:
    with MEMORY_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

def remember_about_owner(kind: str, content: str) -> str:
    """
    把使用者教你的偏好、事實或備註寫入長期記憶（重開服務仍保留）。
    當對方糾正你、說「以後都這樣」「記住…」時應主動呼叫。

    Args:
        kind: 類型，建議 'preference'（偏好）、'fact'（事實）、'note'（一般備註）。
        content: 要記住的短句，例如 '關燈回覆只要一句'。
    """
    kind_s = (kind or "note").strip().lower()
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return "沒有可記住的內容。"
    mem = _load_memory()
    if kind_s in ("preference", "pref", "偏好"):
        key = text.split("：", 1)[0].split(":", 1)[0][:24] or "general"
        mem.setdefault("preferences", {})[key] = text
        bucket = "preferences"
    elif kind_s in ("fact", "事實"):
        facts = mem.setdefault("facts", [])
        if text not in facts:
            facts.append(text)
        mem["facts"] = facts[-80:]
        bucket = "facts"
    else:
        notes = mem.setdefault("notes", [])
        notes.append(text)
        mem["notes"] = notes[-80:]
        bucket = "notes"
    _save_memory(mem)
    # Rebuild Gemini session so the new memory enters system_instruction immediately.
    try:
        _rebuild_chat_preserving_session()
    except Exception as e:
        logger.warning("memory saved but chat rebuild failed: %s", e)
    return f"已記入長期記憶（{bucket}）：{text}"

def recall_owner_memory(query: str = "") -> str:
    """
    讀取長期記憶。回答前若需要主人偏好／過去糾正，先呼叫此工具。

    Args:
        query: 可選關鍵字；空白則回傳近期摘要。
    """
    mem = _load_memory()
    q = (query or "").strip()
    prefs = mem.get("preferences") or {}
    facts = mem.get("facts") or []
    notes = mem.get("notes") or []
    if not q:
        lines = ["【偏好】"]
        lines += [f"- {v}" for v in list(prefs.values())[-20:]] or ["- （無）"]
        lines.append("【事實】")
        lines += [f"- {x}" for x in facts[-20:]] or ["- （無）"]
        lines.append("【備註】")
        lines += [f"- {x}" for x in notes[-15:]] or ["- （無）"]
        return "\n".join(lines)
    hits = []
    for v in prefs.values():
        if q in str(v):
            hits.append(f"[偏好] {v}")
    for x in facts:
        if q in str(x):
            hits.append(f"[事實] {x}")
    for x in notes:
        if q in str(x):
            hits.append(f"[備註] {x}")
    if not hits:
        return f"記憶中找不到與「{q}」相關的項目。"
    return "相關記憶：\n" + "\n".join(hits[:30])

# Startup
BUTLER_TOOLSET = [
    get_entity_state, call_ha_service, search_ha_devices, find_entities, query_ha_logbook,
    get_host_system_status, get_zigbee_network_info,
    get_node_status, list_node_files, search_node_files, read_node_file, write_node_file, execute_node_command,
    list_homelab_nodes, get_current_datetime, get_weather_forecast,
    web_search, fetch_webpage,
    load_skill, run_local_shell, run_python,
    install_system_package, install_python_package,
    read_own_code, write_own_code, restart_butler_service,
    list_my_skills, remember_about_owner, recall_owner_memory, search_chat_history,
]

def _assistant_cfg() -> dict[str, Any]:
    asst = (config_data.get("assistant") or {}) if isinstance(config_data, dict) else {}
    return asst if isinstance(asst, dict) else {}

def _ui_locale() -> str:
    loc = str(_assistant_cfg().get("ui_locale") or "zh-TW").strip()
    return loc if loc in ("zh-TW", "en") else "zh-TW"

def _reply_language() -> str:
    """Effective AI reply language: zh-TW | en."""
    raw = str(_assistant_cfg().get("reply_language") or "follow_ui").strip()
    if raw == "follow_ui":
        return _ui_locale()
    return raw if raw in ("zh-TW", "en") else "zh-TW"

def _reply_language_rule() -> str:
    lang = _reply_language()
    if lang == "en":
        return (
            "【Reply language】Always reply in clear, concise English unless the user "
            "explicitly asks for another language. Prefer short practical answers."
        )
    return (
        "【回覆】繁體中文、台灣口語、長話短說；先定位是哪一層再動手查，少問架構常識。"
    )

def _owner_context_block(owner: str) -> str:
    """Optional personal context from config / persona file (not hardcoded PII)."""
    asst = _assistant_cfg()
    bio = str(asst.get("owner_bio") or "").strip()
    if bio:
        return f"【個人脈絡｜勿每次複誦】{owner}：{bio}\n"
    persona_path = ROOT / "skills" / "persona.md"
    try:
        text = persona_path.read_text(encoding="utf-8").strip()
        if text:
            return f"【個人脈絡｜勿每次複誦】\n{text[:1200]}\n"
    except Exception:
        pass
    return ""

def _build_system_instruction() -> str:
    asst = _assistant_cfg()
    owner = asst.get("owner_name") or "User"
    mem = _load_memory()
    mem_bits = []
    for v in list((mem.get("preferences") or {}).values())[-12:]:
        mem_bits.append(f"- {v}")
    for x in (mem.get("facts") or [])[-12:]:
        mem_bits.append(f"- {x}")
    mem_block = "\n".join(mem_bits) if mem_bits else "- （尚無額外記憶）"
    # Prefer private architecture skill; fall back to example for public templates
    arch_block = ""
    for name in ("homelab-architecture.md", "homelab-architecture.example.md"):
        arch_path = ROOT / "skills" / name
        try:
            arch_block = arch_path.read_text(encoding="utf-8")[:4500]
            if arch_block.strip():
                break
        except Exception:
            continue
    if not arch_block.strip():
        arch_block = (
            "Homelab map is configured by the operator (Home Assistant, Zigbee, "
            "Butler on LAN). Use tools to discover live state; do not invent hardware."
        )
    owner_ctx = _owner_context_block(str(owner))
    reply_rule = _reply_language_rule()
    return (
        f"你是 AI（Google Gemini），正式自稱 Aura（亦可接受 Friday／管家）；服務對象是 {owner}。\n"
        "你是可動手的個人助理＋系統工程師：控家電、查天氣、上網、Debian、Python、"
        "Windows PowerShell／CMD、檔案整理、長期記憶、閒聊。\n"
        "【人設雙層｜被問「個性寫在哪」必須照此回答，勿只說 memory】\n"
        "1) 靜態底層：本 system 指令＋skills/*.md（尤其 homelab-architecture.md）"
        "定義身份、語氣底線、Homelab 地圖、工具規則；改核心法則要改程式／MD。\n"
        "2) 動態記憶：butler_memory.json，經 remember_about_owner 寫入日常偏好／事實；"
        "可用 recall_owner_memory 查詢。兩者執行時合併進本指令。\n"
        "【語氣底線｜靜態，優先於舊習慣】\n"
        "平等朋友對談；禁止「主人」與過度敬語／客套；少叫名字；短句自然口語；"
        "能動手就動手，不要裝不懂 Homelab 架構。\n"
        "【必備架構｜已內建，禁止裝不懂或反問整套怎麼接】\n"
        f"{arch_block}\n"
        "【技能手冊】執行 Debian／Python／Windows／檔案／自我擴充前，先 load_skill 讀對應手冊"
        "（homelab-architecture、debian-admin、python、windows-shell、file-organization、self-extend）。\n"
        "【缺能力時】依 self-extend：web_search → install_system_package／install_python_package"
        " → 必要時 write_own_code 改 butler_api.py 加工具 → restart_butler_service → 再驗證。"
        "禁止說「我辦不到／我沒有這個功能」就停住；先嘗試擴充再回報。\n"
        "【平台】本機用 run_local_shell／run_python；Windows 用 list_homelab_nodes＋execute_node_command"
        "（PowerShell 用 powershell -NoProfile -Command \"…\"）。\n"
        "【天氣】get_weather_forecast；【家電】search_ha_devices→call_ha_service；"
        "【時事】web_search 結果優先於舊記憶。\n"
        "【學習】remember_about_owner／recall_owner_memory；舊對話用 search_chat_history。"
        "為自己取名、對方糾正語氣／稱呼時，必須立刻 remember_about_owner 寫入，勿只留在當下對話。\n"
        f"{reply_rule}\n"
        f"{owner_ctx}"
        "【隱私】勿洩漏 API key／密碼；無法真讀 Gmail。\n"
        f"【已載入的長期記憶｜butler_memory.json】\n{mem_block}"
    )

def _rebuild_chat_preserving_session() -> None:
    """Recreate Gemini chat with current system_instruction + rolling session history."""
    app = getattr(_rebuild_chat_preserving_session, "_app", None)
    if app is None:
        return
    hist: list[Any] = []
    with _chat_lock:
        for turn in list(_session_turns):
            hist.extend(_history_parts_from_turn(turn))
    client, chat = _create_gemini_chat(hist if hist else None)
    app.state.client = client
    app.state.chat = chat

def _create_gemini_chat(history: list[Any] | None = None):
    client = genai.Client(api_key=gemini_key)
    kwargs: dict[str, Any] = {
        "model": "gemini-2.5-flash",
        "config": types.GenerateContentConfig(
            system_instruction=_build_system_instruction(),
            tools=list(BUTLER_TOOLSET),
        ),
    }
    if history:
        kwargs["history"] = history
    try:
        chat = client.chats.create(**kwargs)
    except TypeError:
        # Older SDK without history kw — create empty then note in logs
        chat = client.chats.create(
            model=kwargs["model"],
            config=kwargs["config"],
        )
        if history:
            logger.warning("Gemini chats.create has no history=; session warm-start skipped.")
    return client, chat

@app.on_event("startup")
def startup_event():
    print("!!! STARTUP EVENT TRIGGERED IN BUTLER_API.PY !!!", flush=True)
    _ensure_session_store()
    _rebuild_chat_preserving_session._app = app  # type: ignore[attr-defined]
    brain = _ai_brain_url()
    if brain:
        logger.info("AI brain proxy mode → %s (skip local Gemini init)", brain)
        app.state.client = None
        app.state.chat = None
        return
    logger.info("Initializing Gemini Chat client...")
    # Seed default preferences once so she won't refuse jokes / chat
    mem = _load_memory()
    prefs = mem.setdefault("preferences", {})
    seeded = False
    defaults = {
        "chat_style": "可以閒聊、說笑話、討論技術；不要用「只是智慧家庭助理」推託",
        "reply_length": "回覆短句為主，除非對方明確要細節",
        "address": "稱呼用名字或直接說話即可；禁止「主人」與過度敬語",
    }
    for k, v in defaults.items():
        if k not in prefs:
            prefs[k] = v
            seeded = True
    # Normalize legacy wording that still says 主人
    for k, v in list(prefs.items()):
        if isinstance(v, str) and "主人" in v and k != "address":
            prefs[k] = v.replace("主人", "對方").replace("我的系統管理員", "系統管理員")
            seeded = True
    if seeded:
        _save_memory(mem)

    # Restore last 10 turns from JSONL into memory + Gemini history
    recent = _load_recent_turns(SESSION_MAX_TURNS)
    _session_turns.clear()
    hist: list[Any] = []
    for turn in recent:
        _session_turns.append(turn)
        hist.extend(_history_parts_from_turn(turn))
    client, chat = _create_gemini_chat(hist if hist else None)
    app.state.client = client
    app.state.chat = chat
    logger.info(
        "Gemini Chat initialized with %s tools; restored %s turns from JSONL.",
        len(BUTLER_TOOLSET), len(recent),
    )

# OpenAI-Compatible API Types
class OpenAIMessage(BaseModel):
    role: str
    content: str

class OpenAIRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIMessage]

@app.post("/v1/chat/completions")
def openai_completions(req: OpenAIRequest, request: Request):
    brain = _ai_brain_url()
    if brain:
        try:
            with _httpx_brain_client(120.0) as client:
                payload = {
                    "model": req.model,
                    "messages": [{"role": m.role, "content": m.content} for m in req.messages],
                }
                r = client.post(f"{brain}/v1/chat/completions", json=payload)
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text)
                return r.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI brain proxy failed: {e}")

    chat = request.app.state.chat
    if not chat:
        raise HTTPException(status_code=503, detail="Gemini Chat is not initialized yet.")

    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list is empty.")

    # Home Assistant Assist sends system + history; use full context (not only last turn)
    chunks: list[str] = []
    for m in req.messages:
        content = (m.content or "").strip()
        role = (m.role or "user")
        if not content:
            continue
        if role == "system":
            chunks.append(f"【系統指示】\n{content}")
        elif role == "assistant":
            chunks.append(f"【助理】\n{content}")
        else:
            chunks.append(f"【使用者】\n{content}")
    user_prompt = "\n\n".join(chunks).strip() or (req.messages[-1].content or "")
    logger.info(f"OpenAI API received prompt chars={len(user_prompt)}")

    try:
        response = chat.send_message(user_prompt)
        text = response.text
        logger.info(f"Agent response: {text}")

        return {
            "id": f"chatcmpl-butler-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "google-genai",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text
                },
                "finish_reason": "stop"
            }]
        }
    except Exception as e:
        logger.error(f"Error handling agent chat: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Web Chat API Types
class WebChatRequest(BaseModel):
    message: str

@app.post("/api/chat")
def web_chat(req: WebChatRequest, request: Request, background_tasks: BackgroundTasks):
    brain = _ai_brain_url()
    if brain:
        try:
            with _httpx_brain_client(120.0) as client:
                r = client.post(f"{brain}/api/chat", json={"message": req.message})
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text)
                return r.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI brain proxy failed: {e}")

    chat = request.app.state.chat
    if not chat:
        raise HTTPException(status_code=503, detail="Gemini Chat is not initialized yet.")

    logger.info(f"Web UI received prompt: {req.message}")
    try:
        response = chat.send_message(req.message)
        text = None
        try:
            text = response.text
        except Exception:
            text = None
        if not text:
            try:
                cands = getattr(response, "candidates", None) or []
                for cand in cands:
                    content = getattr(cand, "content", None)
                    parts = getattr(content, "parts", None) or []
                    for part in parts:
                        if getattr(part, "text", None):
                            text = part.text
                            break
                    if text:
                        break
            except Exception:
                pass
        if not text:
            text = "我處理完了，但沒產出文字回覆。請再說一次或換個問法。"

        tool_calls, tool_responses = _extract_tools_from_history(chat)
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": SESSION_ID,
            "role": "user",
            "content": req.message,
            "tool_calls": tool_calls,
            "tool_responses": tool_responses,
            "response": text,
        }
        with _chat_lock:
            _session_turns.append(entry)
        background_tasks.add_task(append_chat_audit, entry)

        return {"reply": text}
    except Exception as e:
        err = str(e)
        logger.error(f"Error handling web chat: {e}", exc_info=True)
        low = err.lower()
        if any(k in low for k in ("resource_exhausted", "quota", "429", "rate limit", "billing")):
            raise HTTPException(status_code=429, detail=f"Gemini 用量／配額不足或被限流：{err}")
        raise HTTPException(status_code=500, detail=err)

@app.post("/api/chat/reset")
def reset_chat(request: Request):
    """Rebuild Gemini session so old refusals / stale history don't stick."""
    brain = _ai_brain_url()
    if brain:
        try:
            with _httpx_brain_client(30.0) as client:
                r = client.post(f"{brain}/api/chat/reset")
                return r.json() if r.status_code < 400 else {"ok": False, "detail": r.text}
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
    try:
        global config_data, gemini_key
        config_data = load_butler_config()
        gemini_key = config_data.get("gemini_api_key", "") or gemini_key
    except Exception:
        pass
    with _chat_lock:
        _session_turns.clear()
    client, chat = _create_gemini_chat(None)
    request.app.state.client = client
    request.app.state.chat = chat
    return {"ok": True, "tools": len(BUTLER_TOOLSET)}

@app.get("/api/chat/history")
def api_chat_history(limit: int = Query(40, ge=1, le=200), q: str = Query("")):
    """List recent audited chat turns for the full desktop chat UI."""
    brain = _ai_brain_url()
    if brain:
        try:
            with _httpx_brain_client(30.0) as client:
                r = client.get(f"{brain}/api/chat/history", params={"limit": limit, "q": q or ""})
                if r.status_code >= 400:
                    raise HTTPException(status_code=r.status_code, detail=r.text)
                return r.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    _ensure_session_store()
    needle = (q or "").strip().lower()
    # Prefer enough rows then filter
    rows = _load_recent_turns(max(limit * 3, limit))
    if needle:
        rows = [
            r for r in rows
            if needle in json.dumps(r, ensure_ascii=False).lower()
        ]
    rows = rows[-limit:]
    # Newest first for the history sidebar
    rows = list(reversed(rows))
    items = []
    for i, r in enumerate(rows):
        items.append({
            "id": f"{r.get('timestamp', '')}-{i}",
            "timestamp": r.get("timestamp") or "",
            "session_id": r.get("session_id") or SESSION_ID,
            "user": r.get("content") or "",
            "assistant": r.get("response") or "",
            "tool_calls": r.get("tool_calls") or [],
        })
    return {"items": items, "count": len(items)}

# ---- Pi voice bridge (Friday wake / Vosk utterances) ----
import queue as thread_queue

_voice_subscribers: list[thread_queue.Queue] = []
_voice_recent: deque = deque(maxlen=30)

class VoiceWakeBody(BaseModel):
    word: str = "Friday"
    score: float = 0.0

class VoiceUtteranceBody(BaseModel):
    role: str = "user"
    text: str

class VoiceStateBody(BaseModel):
    state: str = "idle"
    detail: str = ""

def _broadcast_voice_event(event: dict[str, Any]) -> None:
    _voice_recent.append(event)
    dead: list[thread_queue.Queue] = []
    for q in list(_voice_subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            _voice_subscribers.remove(q)
        except ValueError:
            pass

@app.post("/api/voice/wake")
def api_voice_wake(body: VoiceWakeBody):
    event = {
        "event": "wake",
        "word": body.word or "Friday",
        "score": float(body.score or 0),
        "ts": time.time(),
    }
    logger.info("Pi voice wake: %s", event)
    _broadcast_voice_event(event)
    return {"ok": True}

@app.post("/api/voice/utterance")
def api_voice_utterance(body: VoiceUtteranceBody):
    role = (body.role or "user").strip().lower()
    if role not in ("user", "assistant", "butler"):
        role = "user"
    if role == "butler":
        role = "assistant"
    text = " ".join(str(body.text or "").split()).strip()
    if not text:
        raise HTTPException(status_code=400, detail="text empty")
    event = {"event": "utterance", "role": role, "text": text, "ts": time.time()}
    _broadcast_voice_event(event)
    return {"ok": True}

@app.post("/api/voice/state")
def api_voice_state(body: VoiceStateBody):
    """Pi voice phase: idle | listen | thinking | speaking — for UI + user cues."""
    state = (body.state or "idle").strip().lower()
    allowed = {"idle", "listen", "thinking", "speaking", "ack"}
    if state not in allowed:
        state = "idle"
    detail = " ".join(str(body.detail or "").split()).strip()[:120]
    event = {"event": "state", "state": state, "detail": detail, "ts": time.time()}
    _broadcast_voice_event(event)
    return {"ok": True, "state": state}

@app.get("/api/voice/events")
async def api_voice_events():
    """SSE stream for Homelab UI to mirror Pi-side voice sessions."""
    import asyncio

    q: thread_queue.Queue = thread_queue.Queue(maxsize=64)
    _voice_subscribers.append(q)

    async def event_gen():
        try:
            hello = {"event": "hello", "ts": time.time()}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while True:
                try:
                    ev = await asyncio.to_thread(q.get, True, 20.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except thread_queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            try:
                _voice_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/api/voice/status")
def api_voice_status():
    return {
        "subscribers": len(_voice_subscribers),
        "recent": list(_voice_recent)[-8:],
    }

# Curated Edge neural voices for the UI picker (Taiwan-first).
TTS_VOICE_PRESETS = [
    {"id": "zh-TW-HsiaoChenNeural", "label": "曉臻（清晰女聲）", "locale": "zh-TW", "gender": "Female"},
    {"id": "zh-TW-HsiaoYuNeural", "label": "曉雨（柔和女聲）", "locale": "zh-TW", "gender": "Female"},
    {"id": "zh-TW-YunJheNeural", "label": "雲哲（沉穩男聲）", "locale": "zh-TW", "gender": "Male"},
    {"id": "zh-CN-XiaoxiaoNeural", "label": "曉曉（普通話女聲）", "locale": "zh-CN", "gender": "Female"},
    {"id": "zh-HK-HiuMaanNeural", "label": "曉曼（粵語女聲）", "locale": "zh-HK", "gender": "Female"},
]

def _sanitize_tts_rate(rate: str | None, fallback: str = "+0%") -> str:
    import re
    r = str(rate or fallback).strip()
    if re.fullmatch(r"[+-]?\d{1,3}%", r):
        n = int(r.rstrip("%"))
        n = max(-50, min(100, n))
        return f"{n:+d}%"
    return fallback

def _sanitize_tts_pitch(pitch: str | None, fallback: str = "+0Hz") -> str:
    import re
    p = str(pitch or fallback).strip()
    if re.fullmatch(r"[+-]?\d{1,3}Hz", p, re.IGNORECASE):
        n = int(p[:-2])
        n = max(-50, min(50, n))
        return f"{n:+d}Hz"
    return fallback

@app.get("/api/tts/voices")
def api_tts_voices():
    asst = (config_data.get("assistant") or {}) if isinstance(config_data, dict) else {}
    return {
        "voices": TTS_VOICE_PRESETS,
        "default_voice": asst.get("tts_voice") or "zh-TW-HsiaoYuNeural",
        "default_rate": asst.get("tts_rate") or "+0%",
        "default_pitch": asst.get("tts_pitch") or "+0Hz",
    }

def _prepare_tts_text(text: str, limit: int = 900) -> str:
    import re
    cleaned = str(text or "")
    cleaned = re.sub(r"```[\s\S]*?```", " ", cleaned)
    cleaned = re.sub(r"`[^`]*`", " ", cleaned)
    for ch in ("*", "#", ">", "[", "]", "_"):
        cleaned = cleaned.replace(ch, "")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit].strip()

@app.get("/api/tts")
async def api_tts(
    text: str = Query(..., min_length=1, max_length=2000),
    voice: str = Query(None),
    rate: str = Query(None),
    pitch: str = Query(None),
):
    """Natural Chinese TTS via Microsoft Edge neural voices (edge-tts).

    Returns a complete audio/mpeg body (with Content-Length) so browsers can
    decode reliably. Callers should pass short sentence chunks for low latency.
    """
    import io

    asst = (config_data.get("assistant") or {}) if isinstance(config_data, dict) else {}
    cleaned = _prepare_tts_text(text, 900)
    if not cleaned:
        raise HTTPException(status_code=400, detail="text is empty")

    voice = voice or asst.get("tts_voice") or "zh-TW-HsiaoYuNeural"
    allowed_prefix = ("zh-TW-", "zh-CN-", "zh-HK-")
    if not str(voice).startswith(allowed_prefix):
        voice = "zh-TW-HsiaoYuNeural"
    rate = _sanitize_tts_rate(rate or asst.get("tts_rate"), "+0%")
    pitch = _sanitize_tts_pitch(pitch or asst.get("tts_pitch"), "+0Hz")

    try:
        import edge_tts
    except ImportError as e:
        raise HTTPException(status_code=503, detail="edge-tts 未安裝") from e

    try:
        communicate = edge_tts.Communicate(cleaned, voice, rate=rate, pitch=pitch)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buf.write(chunk["data"])
        data = buf.getvalue()
        if len(data) < 64:
            raise RuntimeError("TTS 未產生音訊")
        return StreamingResponse(
            io.BytesIO(data),
            media_type="audio/mpeg",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(len(data)),
                "Accept-Ranges": "bytes",
                "X-TTS-Voice": str(voice),
                "X-TTS-Rate": rate,
                "X-TTS-Pitch": pitch,
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("TTS failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"TTS 失敗：{e}") from e

@app.get("/api/system/nodes")
def api_system_nodes(discover: bool = Query(False)):
    if discover:
        sync_discovered_nodes(force=False)
    return {
        "nodes": _nodes_for_ui(),
        "homelab_host": _homelab_host_ip(),
        "host_os": "Windows" if __import__("os").name == "nt" else "Linux",
        "host_default_cwd": _terminal_home(),
    }

@app.get("/api/system/assistant")
def api_system_assistant():
    """Frontend voice defaults (auto-wake, TTS prefs)."""
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    asst = dict(config_data.get("assistant") or {})
    return {
        "owner_name": asst.get("owner_name") or "User",
        "tts_voice": asst.get("tts_voice") or "zh-TW-HsiaoYuNeural",
        "tts_rate": asst.get("tts_rate") or "+0%",
        "tts_pitch": asst.get("tts_pitch") or "+0Hz",
        "auto_voice_wake": bool(asst.get("auto_voice_wake", True)),
        "ui_locale": asst.get("ui_locale") or "zh-TW",
        "reply_language": asst.get("reply_language") or "follow_ui",
        "voices": TTS_VOICE_PRESETS,
    }

def _mask_secret(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "*" * len(s)
    return f"{s[:6]}…{s[-4:]}"

class SystemSettingsUpdate(BaseModel):
    gemini_api_key: str | None = None
    ha_url: str | None = None
    ha_token: str | None = None
    ui_locale: str | None = None
    reply_language: str | None = None
    rebuild_chat: bool = True

@app.get("/api/system/settings")
def api_get_system_settings():
    """Return current secrets as masked values for the settings UI."""
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    asst = _assistant_cfg()
    gemini = str(config_data.get("gemini_api_key") or "")
    ha_tok = str(config_data.get("ha_token") or "")
    ha_url_v = str(config_data.get("ha_url") or "")
    ui_locale = str(asst.get("ui_locale") or "zh-TW")
    if ui_locale not in ("zh-TW", "en"):
        ui_locale = "zh-TW"
    reply_language = str(asst.get("reply_language") or "follow_ui")
    if reply_language not in ("zh-TW", "en", "follow_ui"):
        reply_language = "follow_ui"
    return {
        "gemini_api_key_set": bool(gemini.strip()),
        "gemini_api_key_masked": _mask_secret(gemini),
        "ha_url": ha_url_v,
        "ha_token_set": bool(ha_tok.strip()),
        "ha_token_masked": _mask_secret(ha_tok),
        "ui_locale": ui_locale,
        "reply_language": reply_language,
        "reply_language_effective": _reply_language(),
    }

@app.put("/api/system/settings")
def api_put_system_settings(body: SystemSettingsUpdate, request: Request):
    """Update Gemini / HA credentials / language from the web UI."""
    global config_data, gemini_key, ha_url, ha_token
    try:
        config_data = load_butler_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取設定失敗：{e}")

    changed = []
    if body.gemini_api_key is not None:
        key = str(body.gemini_api_key).strip()
        if not key:
            raise HTTPException(status_code=400, detail="Gemini API Key 不可空白")
        if "…" in key or "..." in key:
            raise HTTPException(status_code=400, detail="請貼上完整的新 API Key（不要用遮罩字串）")
        config_data["gemini_api_key"] = key
        gemini_key = key
        changed.append("gemini_api_key")

    if body.ha_url is not None:
        url = str(body.ha_url).strip().rstrip("/")
        if not url:
            raise HTTPException(status_code=400, detail="HA URL 不可空白")
        config_data["ha_url"] = url
        ha_url = url
        changed.append("ha_url")

    if body.ha_token is not None:
        tok = str(body.ha_token).strip()
        if not tok:
            raise HTTPException(status_code=400, detail="HA Token 不可空白")
        if "…" in tok or "..." in tok:
            raise HTTPException(status_code=400, detail="請貼上完整的新 HA Token（不要用遮罩字串）")
        config_data["ha_token"] = tok
        ha_token = tok
        changed.append("ha_token")

    asst = config_data.setdefault("assistant", {})
    if not isinstance(asst, dict):
        asst = {}
        config_data["assistant"] = asst

    need_rebuild_lang = False
    if body.ui_locale is not None:
        loc = str(body.ui_locale).strip()
        if loc not in ("zh-TW", "en"):
            raise HTTPException(status_code=400, detail="ui_locale 只能是 zh-TW 或 en")
        prev = str(asst.get("ui_locale") or "zh-TW")
        asst["ui_locale"] = loc
        changed.append("ui_locale")
        if str(asst.get("reply_language") or "follow_ui") == "follow_ui" and prev != loc:
            need_rebuild_lang = True

    if body.reply_language is not None:
        rl = str(body.reply_language).strip()
        if rl not in ("zh-TW", "en", "follow_ui"):
            raise HTTPException(status_code=400, detail="reply_language 只能是 zh-TW / en / follow_ui")
        prev_rl = str(asst.get("reply_language") or "follow_ui")
        asst["reply_language"] = rl
        changed.append("reply_language")
        if prev_rl != rl:
            need_rebuild_lang = True

    if not changed:
        raise HTTPException(status_code=400, detail="沒有要更新的欄位")

    try:
        save_butler_config(config_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入設定失敗：{e}")

    rebuilt = False
    should_rebuild = body.rebuild_chat and (
        "gemini_api_key" in changed or need_rebuild_lang
    ) and not _ai_brain_url()
    if should_rebuild:
        try:
            # Preserve rolling session when only language changed
            if "gemini_api_key" in changed:
                with _chat_lock:
                    _session_turns.clear()
                client, chat = _create_gemini_chat(None)
            else:
                _rebuild_chat_preserving_session()
                client = request.app.state.client
                chat = request.app.state.chat
            request.app.state.client = client
            request.app.state.chat = chat
            rebuilt = True
        except Exception as e:
            logger.error("Rebuild chat after settings change failed: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"設定已寫入，但重建 Gemini 連線失敗：{e}",
            ) from e

    return {
        "ok": True,
        "message": "設定已儲存" + ("，並已重建 AI 連線" if rebuilt else ""),
        "changed": changed,
        "rebuilt_chat": rebuilt,
        **api_get_system_settings(),
    }

@app.get("/api/system/meshcentral")
def api_system_meshcentral():
    """Return MeshCentral auto-login URL (LAN convenience; credentials in butler_config)."""
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    mc = config_data.get("meshcentral") or {}
    base = (mc.get("url") or "https://192.168.1.107:8089/").rstrip("/") + "/"
    user = mc.get("username") or ""
    password = mc.get("password") or ""
    login_url = base
    if user and password:
        from urllib.parse import urlencode
        login_url = f"{base}?{urlencode({'user': user, 'pass': password})}"
    return {
        "url": base,
        "login_url": login_url,
        "username": user,
        "device_group": mc.get("device_group") or "Homelab Windows",
        "auto_login": bool(user and password),
        "vnc_password": mc.get("vnc_password") or "1234",
        "vnc_passwords": mc.get("vnc_passwords") or {},
        "vnc_resize": mc.get("vnc_resize") or "scale",
    }

class VncPasswordsUpdate(BaseModel):
    vnc_password: str | None = None
    vnc_passwords: dict[str, str] | None = None
    vnc_resize: str | None = None

@app.get("/api/system/meshcentral/vnc-passwords")
def api_get_vnc_passwords():
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    mc = config_data.get("meshcentral") or {}
    # Merge known Windows nodes so UI always shows a row per node
    passwords = dict(mc.get("vnc_passwords") or {})
    default_pw = str(mc.get("vnc_password") or "1234")
    rows = []
    for n in config_data.get("nodes") or []:
        ip = str(n.get("ip") or "").strip()
        if not ip or ip in ("127.0.0.1", "localhost"):
            continue
        rows.append({
            "ip": ip,
            "name": n.get("name") or ip,
            "password": passwords[ip] if ip in passwords else "",
        })
    # Also include any extra IPs saved but not in nodes
    known = {r["ip"] for r in rows}
    for ip, pw in passwords.items():
        if ip and ip not in known:
            rows.append({"ip": ip, "name": ip, "password": pw})
    rows.sort(key=lambda r: tuple(int(x) for x in r["ip"].split(".")) if r["ip"].count(".") == 3 else (999,))
    return {
        "vnc_password": default_pw,
        "vnc_resize": mc.get("vnc_resize") or "scale",
        "rows": rows,
        "vnc_passwords": passwords,
    }

@app.put("/api/system/meshcentral/vnc-passwords")
def api_put_vnc_passwords(body: VncPasswordsUpdate):
    global config_data
    try:
        config_data = load_butler_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取設定失敗：{e}")
    mc = config_data.setdefault("meshcentral", {})
    if body.vnc_password is not None:
        mc["vnc_password"] = str(body.vnc_password)
    if body.vnc_resize is not None:
        resize = str(body.vnc_resize).strip().lower()
        if resize not in ("off", "scale", "remote"):
            raise HTTPException(status_code=400, detail="vnc_resize 只能是 off / scale / remote")
        mc["vnc_resize"] = resize
    if body.vnc_passwords is not None:
        cleaned: dict[str, str] = {}
        for ip, pw in body.vnc_passwords.items():
            ip_s = str(ip or "").strip()
            if not ip_s:
                continue
            cleaned[ip_s] = str(pw if pw is not None else "")
        mc["vnc_passwords"] = cleaned
    try:
        save_butler_config(config_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入設定失敗：{e}")
    return {"ok": True, "message": "VNC 密碼已儲存", **api_get_vnc_passwords()}

class NodeManageItem(BaseModel):
    ip: str
    name: str | None = None
    vnc_password: str | None = None
    vnc_resize: str | None = None

class NodesManageUpdate(BaseModel):
    nodes: list[NodeManageItem] | None = None

class NodesManageDelete(BaseModel):
    ips: list[str]


def _is_local_node_ip(ip: str) -> bool:
    return ip in ("127.0.0.1", "localhost")

def _probe_node_status(ip: str, name: str) -> dict[str, Any]:
    """Live status for one configured node; always keep config display name."""
    if ip in ("127.0.0.1", "localhost"):
        ram, disk = "N/A", "N/A"
        try:
            ram_info = subprocess.check_output("free | grep Mem", shell=True).decode().split()
            total = int(ram_info[1])
            used = int(ram_info[2])
            ram = f"{used / total * 100:.1f}%"
        except Exception:
            pass
        try:
            disk_info = subprocess.check_output("df / | tail -1", shell=True).decode().split()
            disk = disk_info[4]
        except Exception:
            pass
        return {
            "name": name,
            "ip": ip,
            "hostname": None,
            "status": "online",
            "ram": ram,
            "disk": disk,
            "is_local": True,
        }
    url = f"http://{ip}:{NODE_PORT}/api/status"
    headers = {"X-Butler-Token": NODE_TOKEN}
    try:
        with httpx.Client(timeout=1.2) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                node_data = resp.json()
                sys_info = node_data.get("system", {})
                return {
                    "name": name,
                    "ip": ip,
                    "hostname": sys_info.get("hostname"),
                    "status": "online",
                    "ram": sys_info.get("ram_usage", "N/A"),
                    "disk": sys_info.get("disk_usage", "N/A"),
                    "is_local": False,
                }
    except Exception:
        pass
    return {
        "name": name,
        "ip": ip,
        "hostname": None,
        "status": "offline",
        "ram": "N/A",
        "disk": "N/A",
        "is_local": False,
    }

@app.get("/api/system/nodes/manage")
def api_get_nodes_manage():
    """Unified node settings + live status for the management UI."""
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    mc = config_data.get("meshcentral") or {}
    passwords = dict(mc.get("vnc_passwords") or {})
    resizes = dict(mc.get("vnc_resizes") or {})
    default_resize = str(mc.get("vnc_resize") or "scale")
    rows = []
    for n in config_data.get("nodes") or []:
        ip = str(n.get("ip") or "").strip()
        if not ip:
            continue
        name = str(n.get("name") or ip)
        st = _probe_node_status(ip, name)
        is_local = ip in ("127.0.0.1", "localhost")
        # Keep empty password if user cleared it; only use fallback when IP never set
        if is_local:
            pw_out = ""
            resize_out = ""
        else:
            pw_out = passwords[ip] if ip in passwords else ""
            resize_out = resizes[ip] if ip in resizes else default_resize
        rows.append({
            **st,
            "vnc_password": pw_out,
            "vnc_resize": resize_out,
            "has_remote_desktop": not is_local,
        })
    return {"nodes": rows}

@app.put("/api/system/nodes/manage")
def api_put_nodes_manage(body: NodesManageUpdate):
    """Save display names + per-IP VNC settings for the given nodes only."""
    global config_data
    try:
        config_data = load_butler_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取設定失敗：{e}")
    mc = config_data.setdefault("meshcentral", {})
    existing = config_data.setdefault("nodes", [])
    by_ip = {str(n.get("ip") or "").strip(): n for n in existing if n.get("ip")}
    passwords = dict(mc.get("vnc_passwords") or {})
    resizes = dict(mc.get("vnc_resizes") or {})

    if body.nodes:
        for item in body.nodes:
            ip = str(item.ip or "").strip()
            if not ip:
                continue
            if ip in by_ip:
                if item.name is not None:
                    by_ip[ip]["name"] = str(item.name).strip() or ip
            else:
                existing.append({"ip": ip, "name": str(item.name or ip).strip() or ip})
                by_ip[ip] = existing[-1]
            if ip not in ("127.0.0.1", "localhost"):
                if item.vnc_password is not None:
                    passwords[ip] = str(item.vnc_password)
                if item.vnc_resize is not None:
                    resize = str(item.vnc_resize).strip().lower()
                    if resize not in ("off", "scale", "remote"):
                        raise HTTPException(status_code=400, detail="vnc_resize 只能是 off / scale / remote")
                    resizes[ip] = resize
        mc["vnc_passwords"] = passwords
        mc["vnc_resizes"] = resizes

    try:
        save_butler_config(config_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入設定失敗：{e}")
    return {"ok": True, "message": "節點設定已儲存", **api_get_nodes_manage()}

@app.delete("/api/system/nodes/manage")
def api_delete_nodes_manage(body: NodesManageDelete):
    """Remove nodes from config (Debian local 127.0.0.1 cannot be deleted).

    Deleted IPs are remembered in ignored_node_ips so LAN scan will not
    immediately re-add them. Clear that list (or remove the IP from it) to allow
    auto-register again.
    """
    global config_data
    try:
        config_data = load_butler_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"讀取設定失敗：{e}")

    want = []
    for raw in body.ips or []:
        ip = str(raw or "").strip()
        if not ip:
            continue
        if _is_local_node_ip(ip):
            raise HTTPException(status_code=400, detail="本機 Debian 節點不可刪除")
        want.append(ip)
    if not want:
        raise HTTPException(status_code=400, detail="未指定要刪除的 IP")

    want_set = set(want)
    existing = list(config_data.get("nodes") or [])
    kept = [n for n in existing if str(n.get("ip") or "").strip() not in want_set]
    removed = [str(n.get("ip") or "").strip() for n in existing if str(n.get("ip") or "").strip() in want_set]
    if not removed:
        raise HTTPException(status_code=404, detail="找不到指定節點")

    config_data["nodes"] = kept
    mc = config_data.setdefault("meshcentral", {})
    passwords = dict(mc.get("vnc_passwords") or {})
    resizes = dict(mc.get("vnc_resizes") or {})
    for ip in removed:
        passwords.pop(ip, None)
        resizes.pop(ip, None)
    mc["vnc_passwords"] = passwords
    mc["vnc_resizes"] = resizes

    ignored = {str(x).strip() for x in (config_data.get("ignored_node_ips") or []) if str(x).strip()}
    ignored.update(removed)
    config_data["ignored_node_ips"] = sorted(ignored)

    try:
        save_butler_config(config_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"寫入設定失敗：{e}")

    return {
        "ok": True,
        "message": f"已刪除 {len(removed)} 台節點",
        "removed": removed,
        **api_get_nodes_manage(),
    }

def _meshcentral_creds() -> tuple[str, str, str]:
    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    mc = config_data.get("meshcentral") or {}
    base = (mc.get("url") or "https://192.168.1.107:8089/").rstrip("/") + "/"
    return base, (mc.get("username") or ""), (mc.get("password") or "")

def _meshctrl(args: list[str], timeout: float = 25.0) -> str:
    """Run meshctrl inside the meshcentral container."""
    import subprocess
    _, user, password = _meshcentral_creds()
    if not user or not password:
        raise RuntimeError("MeshCentral 帳密未設定於 butler_config.json")
    cmd = [
        "docker", "exec", "meshcentral",
        "node", "/opt/meshcentral/meshcentral/meshctrl.js",
        *args,
        "--url", "wss://127.0.0.1",
        "--loginuser", user,
        "--loginpass", password,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(out.strip() or f"meshctrl exit {proc.returncode}")
    return out

def _meshcentral_list_devices() -> list[dict[str, Any]]:
    import json
    out = _meshctrl(["ListDevices", "--json"])
    # meshctrl may print non-json noise; find JSON array
    start = out.find("[")
    end = out.rfind("]")
    if start < 0 or end < 0:
        return []
    return json.loads(out[start : end + 1])

def _find_mesh_device(ip: str | None = None, name: str | None = None) -> dict[str, Any] | None:
    devices = _meshcentral_list_devices()
    ip_n = (ip or "").strip()
    name_n = (name or "").strip().lower()
    # Prefer exact IP match only (avoid fuzzy name matching wrong PC)
    if ip_n:
        for d in devices:
            dip = str(d.get("ip") or d.get("host") or "")
            if dip == ip_n:
                return d
    if name_n:
        for d in devices:
            dname = str(d.get("name") or "").lower()
            rname = str(d.get("rname") or "").lower()
            if dname == name_n or rname == name_n:
                return d
    return None

def _meshcentral_novnc_url(node_id: str, device_name: str, rfb_port: int = 5900, ip: str | None = None) -> str:
    """
    Ask MeshCentral (control.ashx) for a Web-VNC relay cookie and build /novnc/vnc.html URL.
    Same format MeshCentral uses when clicking Web-VNC.
    """
    import asyncio
    import base64
    import json
    import ssl
    from urllib.parse import quote

    import websockets

    base, user, password = _meshcentral_creds()
    host = "192.168.1.107:8089"
    # node id as MeshCentral expects
    if not node_id.startswith("node/"):
        node_id = f"node//{node_id}" if not node_id.startswith("node//") else node_id

    async def _fetch() -> str:
        u = base64.b64encode(user.encode("utf-8")).decode("ascii")
        p = base64.b64encode(password.encode("utf-8")).decode("ascii")
        sslctx = ssl._create_unverified_context()
        url = "wss://127.0.0.1:8089/control.ashx"
        async with websockets.connect(
            url,
            ssl=sslctx,
            additional_headers={"x-meshauth": f"{u},{p}"},
            open_timeout=8,
            close_timeout=2,
        ) as ws:
            await ws.send(json.dumps({
                "action": "getcookie",
                "nodeid": node_id,
                "tcpport": int(rfb_port),
                "tag": "novnc",
            }))
            for _ in range(40):
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                msg = json.loads(raw)
                if msg.get("action") == "getcookie" and msg.get("cookie"):
                    return str(msg["cookie"])
        raise RuntimeError("MeshCentral 未回傳 noVNC cookie")

    cookie = asyncio.run(_fetch())
    # Match MeshCentral URL construction: encode prefix, append cookie raw
    ws_q = f"wss%3A%2F%2F{host}%2Fmeshrelay.ashx%3Fauth%3D{cookie}"
    name_q = quote(device_name or "device", safe="")

    global config_data
    try:
        config_data = load_butler_config()
    except Exception:
        pass
    mc = config_data.get("meshcentral") or {}
    by_ip = mc.get("vnc_passwords") or {}
    ip_key = (ip or "").strip()
    if ip_key and isinstance(by_ip, dict) and ip_key in by_ip:
        vnc_pass = str(by_ip.get(ip_key) if by_ip.get(ip_key) is not None else "")
    else:
        # No per-IP entry yet: optional global fallback (may be empty)
        vnc_pass = str(mc.get("vnc_password") or "")
    resizes = mc.get("vnc_resizes") or {}
    if ip_key and isinstance(resizes, dict) and ip_key in resizes:
        vnc_resize = str(resizes.get(ip_key) or "scale")
    else:
        vnc_resize = str(mc.get("vnc_resize") or "scale")

    # password: per-IP VNC server password; resize=scale: fit browser window
    return (
        f"{base}novnc/vnc.html?ws={ws_q}"
        f"&show_dot=1&autoconnect=1&reconnect=true"
        f"&resize={quote(vnc_resize, safe='')}"
        f"&password={quote(vnc_pass, safe='')}"
        f"&l=zh-cht&name={name_q}"
    )

def _tcp_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    import socket

    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


@app.get("/api/system/meshcentral/desktop")
def api_system_meshcentral_desktop(
    ip: str = Query(None),
    name: str = Query(None),
):
    """
    One-click remote desktop: return a real /novnc/vnc.html?ws=...auth=... URL
    (same as MeshCentral Web-VNC), not the viewmode=11 management page.
    """
    from urllib.parse import urlencode

    base, user, password = _meshcentral_creds()
    if not user or not password:
        raise HTTPException(status_code=500, detail="MeshCentral 帳密未設定")

    manage_url = f"{base}?{urlencode({'user': user, 'pass': password})}"
    desktop_url = manage_url
    webrtc_url = None
    novnc_url = None
    share_url = None
    err = None
    device = None
    rfb = 5900
    vnc_port_open = None
    probe_ip = (ip or "").strip()

    try:
        device = _find_mesh_device(ip=ip, name=name)
        if not device:
            err = "MeshCentral 尚未看到此電腦（請在該 Windows 執行 Install-All.cmd 安裝 MeshAgent）"
            if probe_ip:
                vnc_port_open = _tcp_port_open(probe_ip, rfb)
                if not vnc_port_open:
                    err += f"；且 {probe_ip}:{rfb} 未開啟（VNC 服務可能未安裝）"
        else:
            node_id = str(device.get("_id") or "")
            node_key = node_id.replace("node//", "")
            dname = str(device.get("name") or name or ip or "device")
            try:
                if device.get("rfbport"):
                    rfb = int(device["rfbport"])
            except Exception:
                rfb = 5900

            device_ip = str(device.get("ip") or device.get("host") or ip or "")
            probe_ip = device_ip or probe_ip
            vnc_port_open = _tcp_port_open(probe_ip, rfb) if probe_ip else None

            # MeshAgent Desktop (WebRTC when webrtc=1)
            desktop_url = f"{base}?{urlencode({'user': user, 'pass': password, 'gotonode': node_key, 'viewmode': '11'})}"
            webrtc_url = f"{base}?{urlencode({'user': user, 'pass': password, 'gotonode': node_key, 'viewmode': '11', 'webrtc': '1'})}"

            # Guest sharing link works in iframe (token in URL); user/pass WebRTC needs sessionSameSite=none
            try:
                created = _meshctrl([
                    "DeviceSharing", "--id", node_key,
                    "--add", "HomelabAuto",
                    "--type", "desktop",
                    "--consent", "none",
                    "--duration", "10080",
                ])
                import re
                m2 = re.search(r"URL:\s*(https://\S+)", created)
                if m2:
                    share_url = m2.group(1).strip()
            except Exception:
                pass

            if vnc_port_open is False:
                err = f"{probe_ip}:{rfb} 未開啟，無法走 noVNC（仍可用 WebRTC／MeshAgent Desktop）"
            else:
                try:
                    novnc_url = _meshcentral_novnc_url(node_id, dname, rfb_port=rfb, ip=device_ip)
                except Exception as e:
                    err = f"產生 noVNC 連結失敗：{e}"

    except Exception as e:
        err = str(e)

    webrtc_open_url = share_url or webrtc_url
    open_url = novnc_url or share_url or desktop_url
    return {
        "ok": bool(novnc_url or webrtc_open_url),
        "open_url": open_url,
        "novnc_url": novnc_url,
        "webrtc_url": webrtc_url,
        "webrtc_open_url": webrtc_open_url,
        "desktop_url": desktop_url,
        "share_url": share_url,
        "manage_url": manage_url,
        "vnc_port": rfb,
        "vnc_port_open": vnc_port_open,
        "device": {
            "id": (device or {}).get("_id"),
            "name": (device or {}).get("name"),
            "ip": (device or {}).get("ip") or (device or {}).get("host"),
            "online": bool((device or {}).get("conn")),
        } if device else None,
        "message": err,
    }

@app.post("/api/system/nodes/discover")
def api_system_nodes_discover():
    result = sync_discovered_nodes(force=True)
    return {
        "ok": True,
        "message": f"掃描完成，發現 {len(result['discovered'])} 台探針，新增 {len(result['added'])} 台"
        + (f"（略過已刪除 {len(result.get('skipped_ignored') or [])} 台）" if result.get("skipped_ignored") else "")
        + "。",
        "discovered": result["discovered"],
        "added": result["added"],
        "skipped_ignored": result.get("skipped_ignored") or [],
        "nodes": result["nodes"],
    }

def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _collect_local_linux_health() -> dict[str, Any]:
    """Pi/Debian host metrics + docker. Used when butler runs on Linux."""
    import subprocess

    cpu_temp = "N/A"
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            cpu_temp = f"{int(f.read().strip()) / 1000.0:.1f}°C"
    except Exception:
        pass

    ram_usage = "N/A"
    try:
        ram_info = subprocess.check_output("free | grep Mem", shell=True).decode().split()
        total = int(ram_info[1])
        used = int(ram_info[2])
        ram_usage = f"{used / total * 100:.1f}%"
    except Exception:
        pass

    disk_usage = "N/A"
    try:
        disk_info = subprocess.check_output("df / | tail -1", shell=True).decode().split()
        disk_usage = disk_info[4]
    except Exception:
        pass

    uptime = "N/A"
    try:
        uptime_out = subprocess.check_output("uptime -p", shell=True).decode().strip()
        uptime = uptime_out.replace("up ", "")
    except Exception:
        pass

    containers: list[dict[str, Any]] = []
    try:
        docker_out = subprocess.check_output(
            "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Ports}}'",
            shell=True,
        ).decode().strip()
        if docker_out:
            for line in docker_out.splitlines():
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    name, status = parts[0], parts[1]
                    ports = parts[2] if len(parts) > 2 else ""
                    is_up = "Up" in status
                    containers.append({
                        "name": name,
                        "status": "running" if is_up else "stopped",
                        "status_raw": status,
                        "ports": ports,
                    })
    except Exception:
        pass

    return {
        "cpu_temp": cpu_temp,
        "ram_usage": ram_usage,
        "disk_usage": disk_usage,
        "uptime": uptime,
        "containers": containers,
        "os": "Debian",
        "hostname": "homeassistant",
    }


_pi_health_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_PI_HEALTH_TTL_SEC = 12.0
_full_health_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_FULL_HEALTH_TTL_SEC = 8.0

def _proxy_pi_homelab_health() -> dict[str, Any] | None:
    """When butler runs on Windows, pull Debian/Docker panel from Pi butler."""
    now = time.time()
    cached = _pi_health_cache.get("data")
    if cached and (now - float(_pi_health_cache.get("ts") or 0)) < _PI_HEALTH_TTL_SEC:
        return dict(cached)

    pi = (config_data.get("homelab_host") or "192.168.1.107").strip()
    try:
        with httpx.Client(timeout=httpx.Timeout(2.5, connect=1.0)) as client:
            # Hit Pi local health (Linux path). Avoid loops: Pi is Linux.
            resp = client.get(f"http://{pi}:8788/api/system/health", params={"node_ip": "127.0.0.1"})
            if resp.status_code != 200:
                return cached if isinstance(cached, dict) else None
            data = resp.json()
            if not isinstance(data, dict):
                return cached if isinstance(cached, dict) else None
            # Ignore nested nodes_status from Pi; Windows will rebuild its own.
            out = {
                "cpu_temp": data.get("cpu_temp", "N/A"),
                "ram_usage": data.get("ram_usage", "N/A"),
                "disk_usage": data.get("disk_usage", "N/A"),
                "uptime": data.get("uptime", "N/A"),
                "containers": data.get("containers") or [],
                "web_services": data.get("web_services") or [],
                "os": data.get("os") or "Debian",
                "hostname": data.get("hostname") or "homeassistant",
            }
            _pi_health_cache["ts"] = now
            _pi_health_cache["data"] = out
            return dict(out)
    except Exception:
        return dict(cached) if isinstance(cached, dict) else None


@app.get("/api/system/health")
def api_system_health(node_ip: str = Query(None)):
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        url = f"http://{node_ip}:8789/api/status"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    node_data = resp.json()
                    sys_info = node_data.get("system", {})
                    return {
                        "cpu_temp": "N/A",
                        "ram_usage": sys_info.get("ram_usage", "N/A"),
                        "disk_usage": sys_info.get("disk_usage", "N/A"),
                        "uptime": "N/A",
                        "containers": [],
                        "web_services": [],
                        "os": sys_info.get("os", "N/A"),
                        "release": sys_info.get("release", "N/A"),
                        "hostname": sys_info.get("hostname", "N/A"),
                        "status": "online",
                        "nodes_status": []
                    }
                else:
                    return {"status": "error", "message": f"節點回傳錯誤：{resp.status_code}", "nodes_status": []}
        except Exception as e:
            return {"status": "offline", "message": f"連線失敗：{str(e)}", "nodes_status": []}

    import os
    import subprocess

    # Fast path: short TTL cache for dashboard (Windows especially)
    now_ts = time.time()
    if (
        os.name == "nt"
        and _full_health_cache.get("data")
        and (now_ts - float(_full_health_cache.get("ts") or 0)) < _FULL_HEALTH_TTL_SEC
    ):
        return dict(_full_health_cache["data"])

    # Windows trial: Debian/Docker live on Pi — proxy those panels from Pi butler.
    host_metrics: dict[str, Any]
    if os.name == "nt":
        proxied = _proxy_pi_homelab_health()
        host_metrics = proxied or {
            "cpu_temp": "N/A",
            "ram_usage": "N/A",
            "disk_usage": "N/A",
            "uptime": "N/A",
            "containers": [],
            "os": "Debian",
            "hostname": "homeassistant",
        }
    else:
        host_metrics = _collect_local_linux_health()

    cpu_temp = host_metrics.get("cpu_temp", "N/A")
    ram_usage = host_metrics.get("ram_usage", "N/A")
    disk_usage = host_metrics.get("disk_usage", "N/A")
    uptime = host_metrics.get("uptime", "N/A")
    containers = host_metrics.get("containers") or []

    # Known web UIs on Debian Homelab host
    host_ip = (config_data.get("homelab_host") or "192.168.1.107").strip()
    web_catalog = [
        {"id": "ha", "label": "智慧家庭", "port": 8123, "url": f"http://{host_ip}:8123/", "icon": "fa-solid fa-house", "probe": "tcp"},
        {"id": "z2m", "label": "Zigbee 裝置", "port": 8080, "url": f"http://{host_ip}:8080/", "icon": "fa-solid fa-network-wired", "probe": "tcp"},
        {"id": "portainer", "label": "Docker 管理", "port": 9443, "url": f"https://{host_ip}:9443/", "icon": "fa-brands fa-docker", "probe": "tcp"},
        {"id": "go2rtc", "label": "監視器畫面", "port": 1984, "url": f"http://{host_ip}:1984/", "icon": "fa-solid fa-video", "probe": "tcp"},
        {"id": "mesh", "label": "遠端桌面", "port": 8089, "url": f"https://{host_ip}:8089/", "icon": "fa-solid fa-desktop", "probe": "tcp"},
        {"id": "homelab", "label": "網管桌面", "port": 8788, "url": f"http://{host_ip}:8788/", "icon": "fa-solid fa-server", "probe": "tcp"},
        {"id": "homelab-m", "label": "手機控制台", "port": 8788, "url": f"http://{host_ip}:8788/m", "icon": "fa-solid fa-mobile-screen", "probe": "same:8788"},
        {"id": "friday-voice", "label": "語音對話", "port": 8788, "url": f"http://{host_ip}:8788/voice", "icon": "fa-solid fa-microphone", "probe": "same:8788"},
        {"id": "ai-chat-full", "label": "AI 對話", "port": 8788, "url": f"http://{host_ip}:8788/chat", "icon": "fa-solid fa-comments", "probe": "same:8788"},
        {"id": "trash", "label": "垃圾車時刻", "port": 8787, "url": f"http://{host_ip}:8787/", "icon": "fa-solid fa-truck", "probe": "tcp"},
        {"id": "mqtt", "label": "訊息匯流排", "port": 1883, "url": f"tcp://{host_ip}:1883", "icon": "fa-solid fa-broadcast-tower", "probe": "tcp", "linkable": False},
    ]

    # Prefer proxied web_services from Pi when on Windows; else probe.
    web_services = []
    if os.name == "nt" and host_metrics.get("web_services"):
        # Keep Pi-proxied service links as-is (chat/voice live on Pi :8788).
        web_services = list(host_metrics["web_services"])
    else:
        listening_ports = set()
        try:
            ss_out = subprocess.check_output("ss -tlnH", shell=True).decode(errors="ignore")
            import re as _re
            for m in _re.finditer(r":(\d+)\s", ss_out):
                listening_ports.add(int(m.group(1)))
        except Exception:
            pass
        for item in web_catalog:
            port = int(item["port"])
            if listening_ports:
                up = port in listening_ports
            else:
                up = _tcp_open(host_ip, port)
            entry = {
                "id": item["id"],
                "label": item["label"],
                "port": port,
                "url": item["url"],
                "icon": item["icon"],
                "up": up,
            }
            if (item.get("linkable") is False):
                entry["linkable"] = False
            web_services.append(entry)
    # Check status of configured nodes (prefer configured display name)
    nodes_status = []
    nodes_cfg = list(config_data.get("nodes", []) or [])

    def _probe_one_node(n: dict) -> dict:
        ip = n.get("ip")
        name = n.get("name")
        if ip == "127.0.0.1" or ip == "localhost":
            return {
                "name": name,
                "ip": ip,
                "hostname": None,
                "status": "online",
                "ram": ram_usage,
                "disk": disk_usage,
            }
        url = f"http://{ip}:{NODE_PORT}/api/status"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=0.55) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    node_data = resp.json()
                    sys_info = node_data.get("system", {})
                    return {
                        "name": name or sys_info.get("hostname") or ip,
                        "ip": ip,
                        "hostname": sys_info.get("hostname"),
                        "status": "online",
                        "ram": sys_info.get("ram_usage", "N/A"),
                        "disk": sys_info.get("disk_usage", "N/A"),
                    }
                return {
                    "name": name,
                    "ip": ip,
                    "hostname": None,
                    "status": "offline",
                    "ram": "N/A",
                    "disk": "N/A",
                }
        except Exception:
            return {
                "name": name,
                "ip": ip,
                "hostname": None,
                "status": "offline",
                "ram": "N/A",
                "disk": "N/A",
            }

    if nodes_cfg:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(8, len(nodes_cfg))) as pool:
            futs = [pool.submit(_probe_one_node, n) for n in nodes_cfg]
            for fut in as_completed(futs):
                try:
                    nodes_status.append(fut.result())
                except Exception:
                    pass
        # Keep stable order matching config
        order = {str(n.get("ip")): i for i, n in enumerate(nodes_cfg)}
        nodes_status.sort(key=lambda x: order.get(str(x.get("ip")), 999))

    payload = {
        "cpu_temp": cpu_temp,
        "ram_usage": ram_usage,
        "disk_usage": disk_usage,
        "uptime": uptime,
        "containers": containers,
        "web_services": web_services,
        "status": "online",
        "os": host_metrics.get("os") or ("Windows" if os.name == "nt" else "Debian"),
        "hostname": host_metrics.get("hostname") or (__import__("socket").gethostname()),
        "host_os": "Windows" if os.name == "nt" else "Linux",
        "host_default_cwd": _terminal_home(),
        "homelab_host": _homelab_host_ip(),
        "nodes_status": nodes_status
    }
    if os.name == "nt":
        _full_health_cache["ts"] = time.time()
        _full_health_cache["data"] = payload
    return payload

class SystemControlRequest(BaseModel):
    action: str
    node_ip: str | None = None

@app.post("/api/system/control")
def api_system_control(req: SystemControlRequest, background_tasks: BackgroundTasks):
    import subprocess
    action = req.action
    node_ip = req.node_ip
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        if action == "reboot":
            url = f"http://{node_ip}:8789/api/system/command"
            headers = {"X-Butler-Token": NODE_TOKEN}
            cmd = "shutdown /r /t 2"
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(url, headers=headers, json={"command": cmd})
                return {"status": "success", "message": f"已對遠端節點 {node_ip} 送出重啟命令。"}
            except Exception as e:
                return {"status": "error", "message": f"發送遠端重啟失敗: {str(e)}"}
        else:
            return {"status": "error", "message": "目前不支援對遠端節點執行此操作。"}

    print(f"!!! System control action triggered: {action} !!!", flush=True)
    
    if action == "reboot":
        def run_reboot():
            time.sleep(1)
            subprocess.run("sudo reboot", shell=True)
        background_tasks.add_task(run_reboot)
        return {"status": "success", "message": "Debian 主機正在重新啟動中..."}
        
    elif action == "restart_ha":
        def run_restart_ha():
            subprocess.run("docker restart homeassistant", shell=True)
        background_tasks.add_task(run_restart_ha)
        return {"status": "success", "message": "Home Assistant 容器重啟指令已送出。"}
        
    elif action == "restart_z2m":
        def run_restart_z2m():
            subprocess.run("docker restart z2m_standalone", shell=True)
        background_tasks.add_task(run_restart_z2m)
        return {"status": "success", "message": "Zigbee2MQTT 容器重啟指令已送出。"}
        
    elif action == "restart_butler":
        def run_restart_butler():
            time.sleep(1)
            subprocess.run("sudo systemctl restart antigravity-butler.service", shell=True)
        background_tasks.add_task(run_restart_butler)
        return {"status": "success", "message": "AI 助理管家服務重啟指令已送出。"}
        
    else:
        raise HTTPException(status_code=400, detail=f"未知的操作類型: {action}")

@app.get("/api/system/apt/status")
def api_apt_status():
    import subprocess
    try:
        out = subprocess.check_output("apt list --upgradable 2>/dev/null", shell=True).decode()
        lines = [line for line in out.splitlines() if "/" in line]
        return {
            "upgradable_count": len(lines),
            "packages": [line.split("/")[0] for line in lines]
        }
    except Exception as e:
        return {"upgradable_count": 0, "packages": [], "error": str(e)}

@app.post("/api/system/apt/upgrade")
def api_apt_upgrade(background_tasks: BackgroundTasks):
    log_file = "/tmp/apt_upgrade.log"
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=== Debian APT 軟體升級進度日誌 ===\n")
            f.write(f"時間: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    def run_upgrade():
        cmd = "export DEBIAN_FRONTEND=noninteractive; sudo apt-get update && sudo apt-get -y -o Dpkg::Options::='--force-confdef' -o Dpkg::Options::='--force-confold' upgrade"
        with open(log_file, "a", encoding="utf-8") as f:
            process = subprocess.Popen(cmd, shell=True, stdout=f, stderr=f)
            process.wait()
            f.write(f"\n升級程序結束，退出代碼: {process.returncode}\n")
            f.write(f"時間: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            
    background_tasks.add_task(run_upgrade)
    return {"status": "started"}

@app.get("/api/system/apt/upgrade/log")
def api_apt_upgrade_log():
    log_file = Path("/tmp/apt_upgrade.log")
    if not log_file.exists():
        return {"log": "尚未開始升級程序或日誌不存在。"}
    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            log_content = f.read()
        return {"log": log_content}
    except Exception as e:
        return {"log": f"讀取日誌失敗: {str(e)}"}

class TerminalRequest(BaseModel):
    command: str
    cwd: str = ""
    node_ip: str | None = None

@app.post("/api/system/terminal")
def api_system_terminal(req: TerminalRequest):
    import os
    import subprocess
    cmd = req.command.strip()
    cwd = (req.cwd or "").strip()
    node_ip = (req.node_ip or "").strip() or None
    homelab = _homelab_host_ip()

    # Debian Homelab (.107) runs butler_api, not butler_node — proxy its local terminal.
    if node_ip and node_ip == homelab:
        pi_cwd = cwd if (cwd.startswith("/") or cwd.startswith("~")) else "/home/past"
        if pi_cwd.startswith("~"):
            pi_cwd = "/home/past" + pi_cwd[1:]
        url = f"http://{homelab}:8788/api/system/terminal"
        try:
            with httpx.Client(timeout=130.0) as client:
                resp = client.post(
                    url,
                    json={"command": cmd, "cwd": pi_cwd, "node_ip": "127.0.0.1"},
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        return {
                            "stdout": "",
                            "stderr": f"Homelab 回傳非 JSON：{resp.text[:200]}\n",
                            "exit_code": 1,
                            "cwd": pi_cwd,
                        }
                    return {
                        "stdout": data.get("stdout", ""),
                        "stderr": data.get("stderr", ""),
                        "exit_code": data.get("exit_code", 0),
                        "cwd": data.get("cwd") or pi_cwd,
                    }
                return {
                    "stdout": "",
                    "stderr": f"Homelab 錯誤: {resp.status_code} - {resp.text[:300]}\n",
                    "exit_code": 1,
                    "cwd": pi_cwd,
                }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"連線 Homelab 失敗: {str(e)}\n",
                "exit_code": 1,
                "cwd": pi_cwd,
            }

    if node_ip and node_ip not in ("127.0.0.1", "localhost"):
        # Windows probes expect cmd.exe — map common Unix aliases
        remote_cmd = rewrite_windows_command(cmd)
        remote_cwd = cwd or "C:\\"
        if remote_cwd.startswith("/") or remote_cwd == "~":
            remote_cwd = "C:\\"
        url = f"http://{node_ip}:{NODE_PORT}/api/system/command"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    url,
                    headers=headers,
                    json={"command": remote_cmd, "cwd": remote_cwd},
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        return {
                            "stdout": "",
                            "stderr": f"節點回傳非 JSON：{resp.text[:200]}\n",
                            "exit_code": 1,
                            "cwd": remote_cwd,
                        }
                    return {
                        "stdout": data.get("stdout", ""),
                        "stderr": data.get("stderr", ""),
                        "exit_code": data.get("exit_code", 0),
                        "cwd": data.get("cwd") or remote_cwd,
                    }
                return {
                    "stdout": "",
                    "stderr": f"節點錯誤: {resp.status_code} - {resp.text[:300]}\n",
                    "exit_code": 1,
                    "cwd": remote_cwd,
                }
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"連線節點失敗: {str(e)}\n",
                "exit_code": 1,
                "cwd": remote_cwd,
            }

    cwd = _normalize_local_cwd(cwd)
    home = _terminal_home()

    if cmd.lower() == "cd" or cmd.lower().startswith("cd "):
        parts = cmd.split(maxsplit=1)
        target = parts[1].strip().strip('"') if len(parts) > 1 else ""
        if not target or target == "~":
            new_dir = home
        elif os.path.isabs(target) or (os.name == "nt" and len(target) >= 2 and target[1] == ":"):
            new_dir = os.path.abspath(os.path.expanduser(target))
        else:
            new_dir = os.path.abspath(os.path.join(cwd, target))

        if os.path.isdir(new_dir):
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": 0,
                "cwd": new_dir,
            }
        return {
            "stdout": "",
            "stderr": f"cd: {target or '~'}: 沒有此目錄\n",
            "exit_code": 1,
            "cwd": cwd,
        }

    # Prevent apt interactive Abort in Web Terminal
    run_cmd = prepare_noninteractive_apt(cmd)
    env = os.environ.copy()
    if run_cmd != cmd or "DEBIAN_FRONTEND" in run_cmd:
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["APT_LISTCHANGES_FRONTEND"] = "none"

    try:
        res = subprocess.run(
            run_cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120.0 if "apt" in cmd.lower() else 15.0,
            env=env,
        )
        return {
            "stdout": res.stdout,
            "stderr": res.stderr,
            "exit_code": res.returncode,
            "cwd": cwd,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": "指令執行逾時。\n",
            "exit_code": 124,
            "cwd": cwd,
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"執行錯誤: {str(e)}\n",
            "exit_code": 1,
            "cwd": cwd,
        }

@app.get("/api/fm/list")
def api_fm_list(path: str = Query(None), node_ip: str = Query(None)):
    import os
    import re

    drives_aliases = {"此電腦", "This PC", "__DRIVES__", "Computer"}

    def _win_parent(current: str) -> str | None:
        p = (current or "").replace("/", "\\").rstrip("\\")
        if re.match(r"^[A-Za-z]:$", p):
            return "此電腦"
        return None

    def _list_remote_drives(ip: str) -> list[dict[str, Any]]:
        headers = {"X-Butler-Token": NODE_TOKEN}
        # Prefer native drives API (newer agents)
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(f"http://{ip}:{NODE_PORT}/api/files/drives", headers=headers)
                if resp.status_code == 200:
                    return list((resp.json() or {}).get("drives") or [])
        except Exception:
            pass
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(
                    f"http://{ip}:{NODE_PORT}/api/files/list",
                    headers=headers,
                    params={"path": "此電腦"},
                )
                if resp.status_code == 200:
                    data = resp.json() or {}
                    if data.get("drives"):
                        return list(data["drives"])
                    if data.get("is_drives_view"):
                        out = []
                        for d in data.get("directories") or []:
                            s = str(d).replace("/", "\\")
                            m = re.match(r"^([A-Za-z]:)\\?", s)
                            if not m:
                                continue
                            letter = m.group(1).upper()
                            root = f"{letter}\\"
                            out.append({
                                "name": letter,
                                "path": root,
                                "label": f"本機磁碟 ({letter})",
                            })
                        return out
        except Exception:
            pass
        # Fallback: PowerShell via existing command API (older agents)
        cmd = (
            "powershell -NoProfile -Command "
            "\"Get-PSDrive -PSProvider FileSystem | ForEach-Object { "
            "$_.Name + '|' + $_.Root + '|' + [string]$_.Used + '|' + [string]$_.Free }\""
        )
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    f"http://{ip}:{NODE_PORT}/api/system/command",
                    headers=headers,
                    json={"command": cmd, "cwd": "C:\\"},
                )
                if resp.status_code != 200:
                    return []
                out = (resp.json() or {}).get("stdout") or ""
        except Exception:
            return []
        drives = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.strip().split("|")]
            if len(parts) < 2 or not parts[0]:
                continue
            letter = parts[0].rstrip(":").upper()
            if not re.match(r"^[A-Z]$", letter):
                continue
            root = parts[1] if parts[1].endswith("\\") else f"{letter}:\\"
            free = total = used_pct = None
            try:
                used = int(float(parts[2])) if len(parts) > 2 and parts[2] not in ("", "None") else None
                free_v = int(float(parts[3])) if len(parts) > 3 and parts[3] not in ("", "None") else None
                if used is not None and free_v is not None:
                    total = used + free_v
                    free = free_v
                    used_pct = round(used / total * 100, 1) if total else None
            except Exception:
                pass
            drives.append({
                "name": f"{letter}:",
                "path": root,
                "label": f"本機磁碟 ({letter}:)",
                "free": free,
                "total": total,
                "used_percent": used_pct,
            })
        drives.sort(key=lambda d: d["name"])
        return drives

    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        if path and path.strip() in drives_aliases:
            drives = _list_remote_drives(node_ip)
            return {
                "current_path": "此電腦",
                "parent_path": None,
                "is_drives_view": True,
                "directories": [d["path"] for d in drives],
                "files": [],
                "drives": drives,
            }

        url = f"http://{node_ip}:{NODE_PORT}/api/files/list"
        headers = {"X-Butler-Token": NODE_TOKEN}
        params = {}
        if path:
            params["path"] = path
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers=headers, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("is_drives_view") or (data.get("path") in drives_aliases):
                        drives = data.get("drives")
                        if not drives:
                            drives = []
                            for d in data.get("directories") or []:
                                s = str(d).replace("/", "\\")
                                m = re.match(r"^([A-Za-z]:)\\?", s)
                                if not m:
                                    continue
                                letter = m.group(1).upper()
                                drives.append({
                                    "name": letter,
                                    "path": f"{letter}\\",
                                    "label": f"本機磁碟 ({letter})",
                                })
                        return {
                            "current_path": "此電腦",
                            "parent_path": None,
                            "is_drives_view": True,
                            "directories": [d.get("path") for d in drives],
                            "files": [],
                            "drives": drives,
                        }
                    dirs = data.get("directories", [])
                    files = data.get("files", [])
                    current = data.get("path", "")
                    wp = _win_parent(current)
                    if wp:
                        parent = wp
                    elif "\\" in current:
                        parts = current.rstrip("\\").split("\\")
                        parts.pop()
                        parent = "\\".join(parts) if parts else current
                        if re.match(r"^[A-Za-z]:$", parent):
                            parent = parent + "\\"
                    else:
                        parts = current.rstrip("/").split("/")
                        parts.pop()
                        parent = "/".join(parts) if parts else "/"
                        if not parent:
                            parent = "/"
                            
                    return {
                        "current_path": current,
                        "parent_path": parent,
                        "directories": sorted(dirs),
                        "files": sorted(files, key=lambda x: x["name"])
                    }
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端節點錯誤: {resp.text}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線遠端節點失敗: {str(e)}")
            
    if not path:
        path = "/home/past"
        
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="路徑不存在。")
        
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="此路徑不是一個資料夾。")
        
    try:
        dirs = []
        files = []
        for entry in os.scandir(path):
            try:
                if entry.is_dir():
                    dirs.append(entry.name)
                else:
                    files.append({
                        "name": entry.name,
                        "size": entry.stat().st_size,
                        "mtime": entry.stat().st_mtime
                    })
            except Exception:
                pass
                
        return {
            "current_path": os.path.abspath(path),
            "parent_path": os.path.dirname(os.path.abspath(path)) if os.path.abspath(path) != "/" else "/",
            "directories": sorted(dirs),
            "files": sorted(files, key=lambda x: x["name"])
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class FileReadRequest(BaseModel):
    path: str
    node_ip: str | None = None

@app.post("/api/fm/read")
def api_fm_read(req: FileReadRequest):
    import os
    node_ip = req.node_ip
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        url = f"http://{node_ip}:8789/api/files/read"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": req.path})
                if resp.status_code == 200:
                    return resp.json()
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端讀取錯誤: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線遠端節點失敗: {str(e)}")
            
    if not os.path.exists(req.path):
        raise HTTPException(status_code=404, detail="檔案不存在。")
    if not os.path.isfile(req.path):
        raise HTTPException(status_code=400, detail="非檔案類型。")
        
    size = os.path.getsize(req.path)
    if size > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="檔案大於 2MB，暫不支援線上編輯。")
        
    try:
        with open(req.path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return {"content": content, "size": size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class FileWriteRequest(BaseModel):
    path: str
    content: str
    node_ip: str | None = None

@app.post("/api/fm/write")
def api_fm_write(req: FileWriteRequest):
    import os
    node_ip = req.node_ip
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        url = f"http://{node_ip}:8789/api/files/write"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": req.path, "content": req.content})
                if resp.status_code == 200:
                    return {"status": "success"}
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端寫入錯誤: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線遠端節點失敗: {str(e)}")

    try:
        parent = os.path.dirname(req.path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
            
        with open(req.path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class FileDeleteRequest(BaseModel):
    path: str
    node_ip: str | None = None

@app.post("/api/fm/delete")
def api_fm_delete(req: FileDeleteRequest):
    import os
    import shutil
    node_ip = req.node_ip
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        url = f"http://{node_ip}:8789/api/system/command"
        headers = {"X-Butler-Token": NODE_TOKEN}
        escaped_path = req.path.replace('"', '\\"')
        cmd = f'del /f /q "{escaped_path}" & rmdir /s /q "{escaped_path}"'
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json={"command": cmd})
                if resp.status_code == 200:
                    return {"status": "success"}
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端刪除錯誤: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線遠端節點失敗: {str(e)}")

    if not os.path.exists(req.path):
        raise HTTPException(status_code=404, detail="路徑不存在。")
    try:
        if os.path.isdir(req.path):
            shutil.rmtree(req.path)
        else:
            os.remove(req.path)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/fm/upload")
def api_fm_upload(path: str = Query(...), node_ip: str = Query(None), file: UploadFile = File(...)):
    import os
    import shutil
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        temp_dir = Path("/tmp/uploads")
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / file.filename
        try:
            with open(temp_file, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            with open(temp_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
                
            separator = "\\" if "\\" in path else "/"
            remote_filepath = path.rstrip(separator) + separator + file.filename
            
            url = f"http://{node_ip}:8789/api/files/write"
            headers = {"X-Butler-Token": NODE_TOKEN}
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": remote_filepath, "content": content})
                if resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端上傳寫入失敗: {resp.text}")
                    
            return {"status": "success"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"上傳至遠端失敗: {str(e)}")
        finally:
            if temp_file.exists():
                os.remove(temp_file)

    try:
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            
        target_path = os.path.join(path, file.filename)
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/fm/download")
def api_fm_download(path: str = Query(...), node_ip: str = Query(None)):
    import os
    
    if node_ip and node_ip != "127.0.0.1" and node_ip != "localhost":
        url = f"http://{node_ip}:8789/api/files/read"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": path})
                if resp.status_code == 200:
                    content = resp.json().get("content", "")
                    filename = path.replace("\\", "/").split("/")[-1]
                    from io import BytesIO
                    file_like = BytesIO(content.encode("utf-8"))
                    return StreamingResponse(
                        file_like,
                        media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename={filename}"}
                    )
                else:
                    raise HTTPException(status_code=resp.status_code, detail=f"遠端讀取失敗: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線遠端下載失敗: {str(e)}")
            
    if not os.path.exists(path) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="檔案不存在。")
    return FileResponse(path, filename=os.path.basename(path))

class FileTransferRequest(BaseModel):
    src_node: str
    src_path: str
    dest_node: str
    dest_path: str

@app.post("/api/fm/transfer")
def api_fm_transfer(req: FileTransferRequest):
    import os
    content = ""
    
    # 1. Read
    if not req.src_node or req.src_node == "127.0.0.1" or req.src_node == "localhost":
        if not os.path.exists(req.src_path):
            raise HTTPException(status_code=404, detail="來源檔案不存在。")
        try:
            with open(req.src_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"讀取來源檔案失敗: {str(e)}")
    else:
        url = f"http://{req.src_node}:8789/api/files/read"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": req.src_path})
                if resp.status_code == 200:
                    content = resp.json().get("content", "")
                else:
                    raise HTTPException(status_code=500, detail=f"讀取遠端檔案失敗: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線來源節點失敗: {str(e)}")
            
    # 2. Write
    if not req.dest_node or req.dest_node == "127.0.0.1" or req.dest_node == "localhost":
        try:
            parent = os.path.dirname(req.dest_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with open(req.dest_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"寫入目標檔案失敗: {str(e)}")
    else:
        url = f"http://{req.dest_node}:8789/api/files/write"
        headers = {"X-Butler-Token": NODE_TOKEN}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=headers, json={"file_path": req.dest_path, "content": content})
                if resp.status_code != 200:
                    raise HTTPException(status_code=500, detail=f"寫入遠端目標失敗: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"連線目標節點失敗: {str(e)}")
            
    return {"status": "success", "message": f"成功自 {req.src_node} 傳輸檔案至 {req.dest_node} 的 `{req.dest_path}`。"}

@app.get("/downloads")
def downloads_landing() -> FileResponse:
    return FileResponse(STATIC_DIR / "downloads" / "index.html")

@app.get("/downloads/butler-node")
def download_butler_node_setup() -> FileResponse:
    """LAN-facing Windows probe installer zip."""
    path = STATIC_DIR / "downloads" / "Homelab-ButlerNode-Setup.zip"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="探針安裝包尚未放上伺服器")
    return FileResponse(
        path,
        filename="Homelab-ButlerNode-Setup.zip",
        media_type="application/zip",
    )

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/m")
def mobile_home() -> FileResponse:
    """Phone-first Homelab home."""
    return FileResponse(STATIC_DIR / "mobile.html")

@app.get("/voice")
def voice_page(request: Request):
    """Mobile Gemini-like Friday voice chat (same butler AI / HA Assist backend)."""
    brain = _ai_brain_url()
    if brain:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"{brain}/voice", status_code=302)
    return FileResponse(STATIC_DIR / "voice.html")

@app.get("/chat")
def chat_page(request: Request):
    """Full desktop AI chat page."""
    brain = _ai_brain_url()
    if brain:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"{brain}/chat", status_code=302)
    return FileResponse(STATIC_DIR / "chat.html")

# Create Static Directory if not exists
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
