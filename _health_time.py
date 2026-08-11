import time, httpx
t0=time.time()
try:
  r=httpx.get("http://192.168.1.107:8788/api/system/health", params={"node_ip":"127.0.0.1"}, timeout=5.0)
  print("pi", r.status_code, round((time.time()-t0)*1000), "ms", "len", len(r.content))
except Exception as e:
  print("pi FAIL", round((time.time()-t0)*1000), e)
t1=time.time()
try:
  r=httpx.get("http://127.0.0.1:8788/api/system/health", params={"node_ip":"127.0.0.1"}, timeout=30.0)
  print("local105", r.status_code, round((time.time()-t1)*1000), "ms", "len", len(r.content))
except Exception as e:
  print("local105 FAIL", round((time.time()-t1)*1000), e)
