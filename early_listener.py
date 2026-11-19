#!/usr/bin/env python3
"""
Écoute en temps réel les nouveaux coins pump.fun (WebSocket PumpPortal, gratuit, sans clé).

Écrit toutes les 10 s early_live.json (coins créés depuis moins de 10 min) :
nom, créateur, part achetée par le créateur au lancement, market cap de départ.
paper_trader.py (stratégie « tôt ») complète avec les trades récents via DexScreener.

Note : le suivi trade par trade chez PumpPortal demande une clé API approvisionnée
(au moins 0,02 SOL) ; on s'en passe ici.

Usage : python3 early_listener.py --minutes 330
"""

import asyncio
import json
import os
import sys
import time

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "early_live.json")
WS_URL = "wss://pumpportal.fun/api/data"
SUPPLY = 1_000_000_000      # supply fixe des coins pump.fun
KEEP_SECONDS = 10 * 60
WRITE_EVERY = 10

tokens = {}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] listener: {msg}", flush=True)


def snapshot():
    now = time.time()
    for m in [m for m, t in tokens.items() if now - t["created_at"] > KEEP_SECONDS]:
        del tokens[m]
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"updated": now, "tokens": tokens}, fh)
    os.replace(tmp, OUT_PATH)


async def session(deadline):
    async with websockets.connect(WS_URL, ping_interval=20, max_size=2**22) as ws:
        await ws.send(json.dumps({"method": "subscribeNewToken"}))
        next_write = 0
        while time.time() < deadline:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if ev.get("txType") == "create" and ev.get("mint"):
                    tokens[ev["mint"]] = {
                        "name": ev.get("name") or "?", "symbol": ev.get("symbol") or "?",
                        "creator": ev.get("traderPublicKey"), "created_at": time.time(),
                        "dev_initial_pct": round(float(ev.get("initialBuy") or 0) / SUPPLY * 100, 2),
                        "mcap_sol_start": float(ev.get("marketCapSol") or 0),
                    }
            except asyncio.TimeoutError:
                pass
            if time.time() >= next_write:
                next_write = time.time() + WRITE_EVERY
                snapshot()


async def main(minutes):
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            log("connexion")
            await session(deadline)
        except Exception as e:  # noqa: BLE001
            log(f"coupure ({e}), reconnexion dans 10 s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    mins = float(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 5
    asyncio.run(main(mins))
