"""Script simples para Cron Job pingar o bot no Render.

Uso no Render Cron Job:
    python ping.py

Configure a variável PING_URL com a URL de ping:
    https://SEU-SERVICO.onrender.com/webhook/seu-caminho-secreto
"""
import os
import sys
import urllib.request

url = os.getenv("PING_URL", "").strip()
if not url:
    print("ERRO: configure PING_URL")
    sys.exit(1)

try:
    with urllib.request.urlopen(url, timeout=30) as response:
        body = response.read(300).decode("utf-8", errors="ignore")
        print(f"PING OK | status={response.status} | body={body}")
except Exception as exc:
    print(f"PING ERRO: {exc}")
    sys.exit(1)
