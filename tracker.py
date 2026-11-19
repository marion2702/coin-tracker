#!/usr/bin/env python3
"""
Coin Tracker — suivi + alertes Telegram pour tes memecoins Solana (pump.fun).

Sources (gratuites, sans clé) :
  - DexScreener API : prix, market cap, volume, achats/ventes, liquidité (300 req/min)
  - RPC Helius (offre gratuite, optionnel) : top holders, pour détecter les grosses ventes

Usage :
  python3 tracker.py                 # tourne en boucle (Ctrl+C pour arrêter)
  python3 tracker.py --once          # un seul passage, affiche l'état
  python3 tracker.py --chat-id       # trouve ton chat_id Telegram
  python3 tracker.py --test-telegram # envoie un message test sur Telegram

Aucune dépendance à installer : uniquement la bibliothèque standard Python 3.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
HISTORY_PATH = os.path.join(HERE, "history.csv")

DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
UA = {"User-Agent": "coin-tracker/1.0", "Accept": "application/json"}


# ---------------------------------------------------------------- utilitaires

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def http_json(url, data=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(UA)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def usd(x):
    if x is None:
        return "?"
    x = float(x)
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}k"
    if x >= 1:
        return f"${x:.2f}"
    return f"${x:.8f}".rstrip("0")


def pct(x):
    return "?" if x is None else f"{x:+.1f}%"


# ---------------------------------------------------------------- Telegram

def telegram(cfg, text):
    tg = cfg.get("telegram", {})
    token = os.environ.get("TG_BOT_TOKEN") or tg.get("bot_token", "")
    chat = os.environ.get("TG_CHAT_ID") or tg.get("chat_id", "")
    if not token or not chat or "COLLE" in token or "COLLE" in str(chat):
        log("(Telegram non configuré) " + text.replace("\n", " | "))
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    try:
        http_json(url, payload)
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Erreur Telegram : {e}")
        return False


# ---------------------------------------------------------------- données

def fetch_market(mints):
    """Renvoie {mint: snapshot} à partir de DexScreener (paire la plus liquide)."""
    out = {}
    if not mints:
        return out
    url = DEX_URL.format(mints=",".join(mints))
    pairs = http_json(url)
    if isinstance(pairs, dict):  # ancien format éventuel
        pairs = pairs.get("pairs") or []
    for p in pairs or []:
        mint = (p.get("baseToken") or {}).get("address")
        if mint not in mints:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint in out and out[mint]["_liq"] >= liq:
            continue
        vol = p.get("volume") or {}
        chg = p.get("priceChange") or {}
        tx1 = (p.get("txns") or {}).get("h1") or {}
        tx24 = (p.get("txns") or {}).get("h24") or {}
        out[mint] = {
            "_liq": liq,
            "price": float(p["priceUsd"]) if p.get("priceUsd") else None,
            "mcap": p.get("marketCap") or p.get("fdv"),
            "liquidity": liq,
            "vol_h1": vol.get("h1"),
            "vol_h24": vol.get("h24"),
            "chg_m5": chg.get("m5"),
            "chg_h1": chg.get("h1"),
            "chg_h24": chg.get("h24"),
            "buys_h1": tx1.get("buys", 0),
            "sells_h1": tx1.get("sells", 0),
            "buys_h24": tx24.get("buys", 0),
            "sells_h24": tx24.get("sells", 0),
            "dex": p.get("dexId"),
            "url": p.get("url"),
        }
    return out


def fetch_holders(rpc, mint, top_n=10):
    """Top holders via RPC Solana. Renvoie {"supply": float, "top": [(compte, montant)]}."""
    sup = http_json(rpc, {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]})
    supply = float(sup["result"]["value"]["uiAmountString"])
    big = http_json(rpc, {"jsonrpc": "2.0", "id": 2, "method": "getTokenLargestAccounts", "params": [mint]})
    top = [(a["address"], float(a["uiAmountString"])) for a in big["result"]["value"][:top_n]]
    return {"supply": supply, "top": top}


# ---------------------------------------------------------------- alertes

def check_alerts(cfg, coin, snap, prev, holders, prev_holders, st):
    """Compare l'état actuel au précédent et renvoie une liste d'alertes texte."""
    a = cfg["alerts"]
    name = coin["name"]
    alerts = []

    if snap is None:
        return alerts

    # 1. Variation de prix depuis le dernier passage
    if prev and prev.get("price") and snap.get("price"):
        move = (snap["price"] / prev["price"] - 1) * 100
        if move >= a["price_up_pct"]:
            alerts.append(f"🚀 {name} +{move:.1f}% depuis le dernier check → {usd(snap['price'])}")
        elif move <= -a["price_down_pct"]:
            alerts.append(f"🔻 {name} {move:.1f}% depuis le dernier check → {usd(snap['price'])}")

    # 2. Paliers de market cap franchis (à la hausse ou à la baisse)
    mc, pmc = snap.get("mcap"), (prev or {}).get("mcap")
    if mc and pmc:
        for lvl in a["mcap_milestones_usd"]:
            if pmc < lvl <= mc:
                alerts.append(f"🏁 {name} passe au-dessus de {usd(lvl)} de market cap (MC {usd(mc)})")
            elif mc < lvl <= pmc:
                alerts.append(f"⚠️ {name} repasse sous {usd(lvl)} de market cap (MC {usd(mc)})")

    # 3. Pic de volume sur 1h
    v1 = snap.get("vol_h1") or 0
    last_vol_alert = st.get("last_vol_alert", 0)
    if v1 >= a["volume_h1_usd"] and time.time() - last_vol_alert > 3600:
        alerts.append(f"📈 {name} : {usd(v1)} de volume sur la dernière heure")
        st["last_vol_alert"] = time.time()

    # 4. Pression vendeuse
    b, s = snap.get("buys_h1") or 0, snap.get("sells_h1") or 0
    last_sell_alert = st.get("last_sell_alert", 0)
    if s >= a["min_txns_for_ratio"] and s >= a["sell_buy_ratio"] * max(b, 1) \
            and time.time() - last_sell_alert > 3600:
        alerts.append(f"🧯 {name} : {s} ventes contre {b} achats sur 1h (pression vendeuse)")
        st["last_sell_alert"] = time.time()

    # 5. Gros holder qui vend
    if holders and prev_holders:
        prev_map = dict(prev_holders["top"])
        supply = holders["supply"] or 1
        for acct, amt in holders["top"]:
            before = prev_map.get(acct)
            if before is None:
                continue
            drop_pct_supply = (before - amt) / supply * 100
            if drop_pct_supply >= a["whale_sell_pct_supply"]:
                alerts.append(
                    f"🐋 {name} : un top holder ({acct[:4]}…{acct[-4:]}) a lâché "
                    f"{drop_pct_supply:.2f}% de la supply"
                )
        # nouveau gros holder
        for acct, amt in holders["top"]:
            if acct not in prev_map and amt / supply * 100 >= a["new_whale_pct_supply"]:
                alerts.append(
                    f"🐳 {name} : nouveau gros holder ({acct[:4]}…{acct[-4:]}) "
                    f"avec {amt / supply * 100:.2f}% de la supply"
                )
    return alerts


def summary_line(coin, snap, holders):
    if snap is None:
        return f"• {coin['name']} : pas encore trouvé sur DexScreener"
    top10 = ""
    if holders and holders["supply"]:
        share = sum(x for _, x in holders["top"]) / holders["supply"] * 100
        top10 = f" | top10 {share:.0f}%"
    return (
        f"• {coin['name']} : {usd(snap['price'])} | MC {usd(snap['mcap'])} | "
        f"1h {pct(snap['chg_h1'])} | 24h {pct(snap['chg_h24'])} | "
        f"vol24h {usd(snap['vol_h24'])} | {snap['buys_h24']}🟢/{snap['sells_h24']}🔴{top10}"
    )


def append_history(coin, snap):
    if snap is None:
        return
    new = not os.path.exists(HISTORY_PATH)
    with open(HISTORY_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "coin", "price_usd", "mcap_usd", "vol_h1", "vol_h24",
                        "buys_h1", "sells_h1", "liquidity_usd"])
        w.writerow([now(), coin["name"], snap["price"], snap["mcap"], snap["vol_h1"],
                    snap["vol_h24"], snap["buys_h1"], snap["sells_h1"], snap["liquidity"]])


# ---------------------------------------------------------------- boucle

def run_once(cfg, state, verbose=False):
    coins = [c for c in cfg["coins"] if c.get("mint") and "COLLE" not in c["mint"]]
    if not coins:
        log("Aucune adresse de coin dans config.json. Colle les adresses (mint) puis relance.")
        return state

    try:
        market = fetch_market([c["mint"] for c in coins])
    except Exception as e:  # noqa: BLE001
        log(f"Erreur DexScreener : {e}")
        market = {}

    all_alerts, lines = [], []
    for c in coins:
        mint = c["mint"]
        st = state.setdefault(mint, {})
        snap = market.get(mint)
        holders = None
        rpc = cfg.get("solana_rpc", "")
        h_every = cfg.get("holders_every_minutes", 5) * 60
        if rpc.startswith("http") and time.time() - st.get("_holders_at", 0) >= h_every:
            try:
                holders = fetch_holders(rpc, mint)
                st["_holders_at"] = time.time()
            except Exception as e:  # noqa: BLE001
                log(f"Holders indisponibles pour {c['name']} : {e}")
            time.sleep(0.3)

        all_alerts += check_alerts(cfg, c, snap, st.get("last"), holders, st.get("holders"), st)
        lines.append(summary_line(c, snap, holders or st.get("holders")))
        append_history(c, snap)

        if snap:
            st["last"] = {k: v for k, v in snap.items() if not k.startswith("_")}
        if holders:
            st["holders"] = holders

    for line in lines:
        log(line)
    for al in all_alerts:
        telegram(cfg, al)

    # Récap périodique
    every = cfg.get("summary_every_minutes", 0)
    last_sum = state.get("_last_summary", 0)
    if verbose or (every and time.time() - last_sum >= every * 60):
        telegram(cfg, "📊 Récap coins\n" + "\n".join(lines))
        state["_last_summary"] = time.time()

    save_json(STATE_PATH, state)
    return state


def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json introuvable ou invalide (il doit être dans le même dossier que tracker.py).")

    if "--chat-id" in sys.argv:
        token = cfg.get("telegram", {}).get("bot_token", "")
        upd = http_json(f"https://api.telegram.org/bot{token}/getUpdates")
        ids = {u["message"]["chat"]["id"] for u in upd.get("result", []) if "message" in u}
        print(f"Ton chat_id : {', '.join(map(str, ids))}" if ids
              else "Aucun message reçu : envoie « salut » à ton bot sur Telegram puis relance.")
        return

    if "--test-telegram" in sys.argv:
        ok = telegram(cfg, "✅ Coin Tracker est bien connecté.")
        sys.exit(0 if ok else 1)

    state = load_json(STATE_PATH, {})

    if "--once" in sys.argv:
        run_once(cfg, state, verbose=True)
        return

    if "--cron" in sys.argv:  # un passage, récap seulement selon summary_every_minutes
        run_once(cfg, state)
        return

    interval = max(30, int(cfg.get("interval_seconds", 60)))
    log(f"Coin Tracker démarré : check toutes les {interval}s. Ctrl+C pour arrêter.")
    telegram(cfg, f"▶️ Coin Tracker démarré ({len(cfg['coins'])} coins suivis)")
    while True:
        try:
            state = run_once(cfg, state)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            log(f"Erreur inattendue : {e}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Arrêté.")
