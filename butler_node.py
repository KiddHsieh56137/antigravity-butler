import os
import json
import platform
import shutil
import subprocess
import fnmatch
from pathlib import Path
from typing import Any, List, Optional
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Initialize FastAPI App
app = FastAPI(title="Google Antigravity Butler Node Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load Node Configuration
CONFIG_PATH = Path(__file__).parent / "node_config.json"
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"port": 8789, "security_token": "change-me-butler-node-token", "allowed_paths": []}

config = load_config()
port = config.get("port", 8789)
security_token = config.get("security_token") or os.environ.get("BUTLER_NODE_TOKEN") or "change-me-butler-node-token"
allowed_paths = config.get("allowed_paths", [])

# Token Verification Dependency
def verify_token(x_butler_token: str = Header(None)):
    if not security_token:
        return
    if x_butler_token != security_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Butler-Token header.")

# Path Safety Validator
def validate_path(target_path: str):
    if not allowed_paths:
        return  # Allow all if empty
    
    try:
        resolved_target = os.path.realpath(target_path)
        for root in allowed_paths:
            resolved_root = os.path.realpath(root)
            common = os.path.commonpath([resolved_target, resolved_root])
            if common == resolved_root:
                return  # Safe path
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Path resolution error: {str(e)}")
    
    raise HTTPException(status_code=403, detail="Access denied. Path is outside of allowed directories.")

# Hardware System Stats Helper
def get_system_stats() -> dict:
    stats = {
        "os": platform.system(),
        "release": platform.release(),
        "hostname": platform.node(),
        "cpu_usage": "N/A",
        "ram_usage": "N/A",
        "disk_usage": "N/A"
    }
    
    try:
        import psutil
        stats["cpu_usage"] = f"{psutil.cpu_percent()}%"
        mem = psutil.virtual_memory()
        stats["ram_usage"] = f"{mem.percent}% (Used: {mem.used // (1024**2)}MB / Total: {mem.total // (1024**2)}MB)"
        disk = psutil.disk_usage('/')
        stats["disk_usage"] = f"{disk.percent}% (Free: {disk.free // (1024**3)}GB / Total: {disk.total // (1024**3)}GB)"
        return stats
    except ImportError:
        pass
        
    # Windows Fallback
    if stats["os"] == "Windows":
        try:
            cpu_cmd = "powershell -Command \"(Get-CimInstance Win32_Processor).LoadPercentage\""
            cpu_out = subprocess.check_output(cpu_cmd, shell=True).decode().strip()
            if cpu_out:
                stats["cpu_usage"] = f"{cpu_out}%"
                
            mem_cmd = "powershell -Command \"$m = Get-CimInstance Win32_PhysicalMemory; $t = ($m | Measure-Object Capacity -Sum).Sum; $f = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory * 1024; [math]::round((($t - $f)/$t)*100)\""
            mem_out = subprocess.check_output(mem_cmd, shell=True).decode().strip()
            if mem_out:
                stats["ram_usage"] = f"{mem_out}%"
                
            disk_cmd = "powershell -Command \"$d = Get-CimInstance Win32_LogicalDisk -Filter \\\"DeviceID='C:'\\\"; [math]::round((($d.Size - $d.FreeSpace)/$d.Size)*100)\""
            disk_out = subprocess.check_output(disk_cmd, shell=True).decode().strip()
            if disk_out:
                stats["disk_usage"] = f"{disk_out}%"
        except Exception:
            pass
            
    # Linux Fallback
    elif stats["os"] == "Linux":
        try:
            ram_info = subprocess.check_output("free | grep Mem", shell=True).decode().split()
            total = int(ram_info[1])
            used = int(ram_info[2])
            stats["ram_usage"] = f"{used/total*100:.1f}%"
            
            disk_info = subprocess.check_output("df / | tail -1", shell=True).decode().split()
            stats["disk_usage"] = disk_info[4]
        except Exception:
            pass
            
    return stats

# API: Status
@app.get("/api/status", dependencies=[Depends(verify_token)])
def api_status():
    return {
        "status": "online",
        "system": get_system_stats()
    }

# API: List Directory Files
DRIVES_VIEW_ALIASES = {"此電腦", "This PC", "__DRIVES__", "Computer"}

def _list_windows_drives() -> list[dict[str, Any]]:
    drives: list[dict[str, Any]] = []
    if platform.system() != "Windows":
        return drives
    import string
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        label = f"本機磁碟 ({letter}:)"
        free = total = None
        try:
            usage = shutil.disk_usage(root)
            free = usage.free
            total = usage.total
            used_pct = round((usage.used / usage.total) * 100, 1) if usage.total else None
        except Exception:
            used_pct = None
        try:
            # Volume label via WinAPI-ish: dirname of root is often empty; try Path
            vol = Path(root).drive
            label = f"本機磁碟 ({vol})"
        except Exception:
            pass
        drives.append({
            "name": f"{letter}:",
            "path": root,
            "label": label,
            "free": free,
            "total": total,
            "used_percent": used_pct,
        })
    return drives

@app.get("/api/files/drives", dependencies=[Depends(verify_token)])
def api_list_drives():
    return {"drives": _list_windows_drives(), "path": "此電腦"}

@app.get("/api/files/list", dependencies=[Depends(verify_token)])
def api_list_files(path: str = Query(None)):
    if path and path.strip() in DRIVES_VIEW_ALIASES:
        drives = _list_windows_drives()
        return {
            "path": "此電腦",
            "is_drives_view": True,
            "directories": [d["path"] for d in drives],
            "files": [],
            "drives": drives,
        }

    if not path:
        # If no path, return list of allowed paths, or current working directory
        if allowed_paths:
            return {"path": "Allowed Roots", "directories": allowed_paths, "files": []}
        path = os.getcwd()
        
    validate_path(path)
    
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Path does not exist.")
        
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Path is not a directory.")
        
    try:
        dirs = []
        files = []
        for entry in os.scandir(path):
            if entry.is_dir():
                dirs.append(entry.name)
            else:
                files.append({
                    "name": entry.name,
                    "size": entry.stat().st_size
                })
        return {
            "path": os.path.abspath(path),
            "directories": sorted(dirs),
            "files": sorted(files, key=lambda x: x["name"])
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# API: Search Files
@app.get("/api/files/search", dependencies=[Depends(verify_token)])
def api_search_files(path: str = Query(...), query: str = Query(...)):
    validate_path(path)
    
    if not os.path.exists(path) or not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Invalid path directory.")
        
    results = []
    try:
        # Search recursively up to 3 levels deep to prevent lockups on massive folders
        for root, dirs, filenames in os.walk(path):
            # Enforce depth check
            depth = root[len(path):].count(os.sep)
            if depth > 3:
                # Clear dirs to prevent going deeper
                dirs.clear()
                continue
                
            for filename in fnmatch.filter(filenames, f"*{query}*"):
                full_path = os.path.join(root, filename)
                results.append({
                    "name": filename,
                    "path": full_path,
                    "size": os.path.getsize(full_path)
                })
                if len(results) >= 100:  # Limit results to 100 max
                    break
            if len(results) >= 100:
                break
                
        return {"query": query, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# API: Read File
class ReadFileRequest(BaseModel):
    file_path: str

@app.post("/api/files/read", dependencies=[Depends(verify_token)])
def api_read_file(req: ReadFileRequest):
    validate_path(req.file_path)
    
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=404, detail="File not found.")
        
    if not os.path.isfile(req.file_path):
        raise HTTPException(status_code=400, detail="Path is not a file.")
        
    # Check file size (limit to 500KB to avoid memory bloat)
    size = os.path.getsize(req.file_path)
    if size > 500 * 1024:
        raise HTTPException(status_code=400, detail=f"File is too large ({size // 1024}KB). Max allowed size is 500KB.")
        
    try:
        with open(req.file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return {
            "file_path": req.file_path,
            "size": size,
            "content": content
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# API: Write File
class WriteFileRequest(BaseModel):
    file_path: str
    content: str

@app.post("/api/files/write", dependencies=[Depends(verify_token)])
def api_write_file(req: WriteFileRequest):
    validate_path(req.file_path)
    
    try:
        # Create directory if it doesn't exist
        parent_dir = os.path.dirname(req.file_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
            
        with open(req.file_path, "w", encoding="utf-8") as f:
            f.write(req.content)
            
        return {
            "status": "success",
            "file_path": req.file_path,
            "size": len(req.content)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# API: Run System Command
class CommandRequest(BaseModel):
    command: str
    cwd: Optional[str] = None

@app.post("/api/system/command", dependencies=[Depends(verify_token)])
def api_run_command(req: CommandRequest):
    print(f"!!! Node executing command: {req.command} !!!", flush=True)
    cwd = req.cwd.strip() if req.cwd else os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    cmd = req.command.strip()
    # Support cd for interactive terminal sessions
    if cmd.lower() == "cd" or cmd.lower().startswith("cd "):
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1:
            new_dir = os.path.expanduser("~")
        else:
            target = parts[1].strip().strip('"')
            if os.path.isabs(target) or (len(target) >= 2 and target[1] == ":"):
                new_dir = os.path.abspath(target)
            else:
                new_dir = os.path.abspath(os.path.join(cwd, target))
        if os.path.isdir(new_dir):
            return {"exit_code": 0, "stdout": "", "stderr": "", "cwd": new_dir}
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"cd: {parts[-1]}: The system cannot find the path specified.\n",
            "cwd": cwd,
        }

    # Common Unix → Windows cmd aliases
    aliases = {
        "ls": "dir", "ll": "dir", "la": "dir /a", "cat": "type",
        "pwd": "cd", "clear": "cls", "which": "where",
        "rm": "del", "cp": "copy", "mv": "move", "grep": "findstr",
    }
    parts = cmd.split(None, 1)
    if parts and parts[0].lower() in aliases:
        mapped = aliases[parts[0].lower()]
        rest = parts[1] if len(parts) > 1 else ""
        cmd = f"{mapped} {rest}".strip()

    def decode_bytes(data: bytes) -> str:
        for enc in ("utf-8", "cp950", "mbcs", "cp936", "big5"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15.0,
        )
        return {
            "exit_code": result.returncode,
            "stdout": decode_bytes(result.stdout),
            "stderr": decode_bytes(result.stderr),
            "cwd": cwd,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Command execution timed out after 15 seconds.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    print(f"Starting Antigravity Butler Node Agent on port {port}...", flush=True)
    uvicorn.run("butler_node:app", host="0.0.0.0", port=port)
