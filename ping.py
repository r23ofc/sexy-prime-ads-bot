import os
import urllib.request

url = os.getenv("PING_URL", "").strip()
if not url:
    raise SystemExit("PING_URL não configurada.")
with urllib.request.urlopen(url, timeout=30) as response:
    print(response.status)
