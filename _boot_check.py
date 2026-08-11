import traceback
import sys
sys.path.insert(0, r"C:\homelab\antigravity-butler")
err_path = r"C:\homelab\antigravity-butler\_boot_err.txt"
try:
    import butler_api
    open(err_path, "w", encoding="utf-8").write("IMPORT_OK\n")
except Exception:
    open(err_path, "w", encoding="utf-8").write(traceback.format_exc())

# also try create app briefly
try:
    from butler_api import app
    open(err_path, "a", encoding="utf-8").write("APP_OK\n")
except Exception:
    open(err_path, "a", encoding="utf-8").write(traceback.format_exc())
