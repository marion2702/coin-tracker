#!/usr/bin/env python3
"""
Scanner pump.fun : repère les coins qui démarrent fort et envoie le top sur Telegram.

Ce n'est PAS une prédiction : il signale un élan (volume, acheteurs qui accélèrent,
achats > ventes) et élimine les signaux d'arnaque évidents. La plupart des coins
signalés retomberont quand même.

Source : API publique GeckoTerminal (gratuite, sans clé, ~10 à 30 appels/min).
Usage :
  python3 scanner.py           # un passage (c'est ce que lance GitHub toutes les 5 min)
  python3 scanner.py --dry     # affiche les candidats sans envoyer sur Telegram
"""

import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "scanner_config.json")
STATE_PATH = os.path.join(HERE, "scanner_state.json")
LOG_PATH = os.path.join(HERE, "scanner_alerts.csv")
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
CALL_GAP = 8  # secondes entre deux appels GeckoTerminal (reste sous la limite gratuite)

_last_call = [0.0]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def gt_get(path):
    wait = CALL_GAP - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    req = urllib.request.Request(GT + path, headers={"Accept": "application/json",
                                                     "User-Agent": "coin-scanner/1.0"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                log("Limite GeckoTerminal atteinte, pause 30 s")
                time.sleep(30)
                continue
            raise


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def usd(x):
    x = f(x)
    if x >= 1e6:
        return f"${x/1e6:.2f}M"
    if x >= 1e3:
        return f"${x/1e3:.1f}k"
    return f"${x:.0f}"


def telegram(text, dry):
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if dry or not token or not chat:
        print("\n----- message Telegram -----\n" + text + "\n----------------------------")
        return
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        log(f"Erreur Telegram : {e}")


# ------------------------------------------------------------------ étape 1 : élan

def momentum(pool, cfg, now):
    """Renvoie (score, détails) ou None si le pool ne passe pas les filtres d'élan."""
    a = pool["attributes"]
    tx = a.get("transactions") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}

    created = a.get("pool_created_at")
    if not created:
        return None
    age_h = (now - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 3600
    mc = f(a.get("market_cap_usd")) or f(a.get("fdv_usd"))
    v1 = f(vol.get("h1"))
    m15, h1 = tx.get("m15") or {}, tx.get("h1") or {}
    buyers15, buyers1 = m15.get("buyers", 0), h1.get("buyers", 0)
    buys1, sells1 = h1.get("buys", 0), h1.get("sells", 0)
    ch1, ch5 = f(chg.get("h1")), f(chg.get("m5"))

    if age_h > cfg["max_age_hours"]:
        return None
    if not (cfg["min_mcap_usd"] <= mc <= cfg["max_mcap_usd"]):
        return None
    if v1 < cfg["min_volume_h1_usd"]:
        return None
    if buyers15 < cfg["min_buyers_15m"] or buyers1 < cfg["min_buyers_1h"]:
        return None
    buy_ratio = buys1 / max(sells1, 1)
    if buy_ratio < cfg["min_buy_sell_ratio"]:
        return None
    accel = (buyers15 * 4) / max(buyers1, 1)  # >1 : le dernier quart d'heure va plus vite que l'heure
    if accel < cfg["min_acceleration"]:
        return None
    if ch1 <= 0 or ch5 < cfg["max_drop_5m_pct"]:
        return None

    score = (
        math.log10(max(v1, 1)) * 2
        + min(accel, 3) * 2
        + min(buy_ratio, 3)
        + math.log10(max(buyers1, 1))
        + (1 if age_h < 3 else 0)
    )
    return score, {
        "age_h": age_h, "mc": mc, "v1": v1, "buyers1": buyers1, "buyers15": buyers15,
        "buy_ratio": buy_ratio, "accel": accel, "ch1": ch1,
        "price": f(a.get("base_token_price_usd")), "name": a.get("name", "?"),
    }


# ------------------------------------------------------------------ étape 2 : anti-arnaque

def safety(info, cfg):
    """Renvoie (ok, bonus, notes) à partir de /tokens/{mint}/info."""
    a = info["data"]["attributes"]
    notes, bonus = [], 0.0

    def active(v):
        return v not in (None, "", "no", False, "false")

    if active(a.get("mint_authority")) or active(a.get("freeze_authority")):
        return False, 0, ["mint/freeze authority active"]
    if str(a.get("is_honeypot")).lower() == "yes":
        return False, 0, ["honeypot"]
    dev = a.get("developer_holding_percentage")
    if dev is not None and f(dev) > cfg["max_dev_holding_pct"]:
        return False, 0, [f"le créateur détient {f(dev):.0f}%"]
    holders = a.get("holders") or {}
    count = holders.get("count") or 0
    launch = a.get("launchpad_details") or {}
    graduated = bool(launch.get("completed"))
    top10 = f((holders.get("distribution_percentage") or {}).get("top_10"), None)
    # Le top 10 inclut la réserve (bonding curve avant graduation, pool de liquidité après) :
    # on ne rejette qu'au-delà d'un seuil large, et seulement après graduation.
    if graduated and top10 is not None and top10 > cfg["max_top10_pct_graduated"]:
        return False, 0, [f"top 10 holders = {top10:.0f}%"]

    socials = [s for s in [a.get("twitter_handle"), a.get("telegram_handle")] if s] + (a.get("websites") or [])
    if socials:
        bonus += 1.5
    else:
        notes.append("aucun réseau social")
    if count >= 100:
        bonus += 1
    if dev is not None and f(dev) == 0:
        notes.append("le créateur a tout vendu")

    grad = launch.get("graduation_percentage")
    return True, bonus, notes + [
        f"{count} holders" + (f" (top 10 : {top10:.0f}%)" if graduated and top10 is not None else ""),
        "déjà gradué" if graduated else (f"graduation {f(grad):.0f}%" if grad is not None else ""),
    ] + ([f"X : x.com/{a['twitter_handle']}"] if a.get("twitter_handle") else [])


# ------------------------------------------------------------------ passage complet

def main():
    dry = "--dry" in sys.argv
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("scanner_config.json introuvable")
    state = load_json(STATE_PATH, {"alerted": {}})
    now = datetime.now(timezone.utc)

    # purge des alertes trop anciennes
    keep = cfg["realert_after_hours"] * 3600
    state["alerted"] = {m: t for m, t in state["alerted"].items() if time.time() - t["at"] < keep}

    # 1. récupère les pools pump.fun les plus actifs
    pools = []
    for path in cfg["sources"]:
        try:
            data = gt_get(path).get("data", [])
        except Exception as e:  # noqa: BLE001
            log(f"Erreur liste {path} : {e}")
            continue
        # garde uniquement les coins pump.fun (bonding curve ou gradués sur PumpSwap)
        pools += [p for p in data
                  if p["relationships"]["dex"]["data"]["id"] in cfg["dexes"]]

    # 2. filtre d'élan
    cands, seen = [], set()
    for p in pools:
        mint = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        if mint in seen or mint in state["alerted"] or mint in cfg.get("ignore", []):
            continue
        seen.add(mint)
        m = momentum(p, cfg, now)
        if m:
            cands.append((m[0], mint, m[1]))
    cands.sort(reverse=True)
    log(f"{len(pools)} pools scannés, {len(cands)} passent le filtre d'élan")

    # 3. vérif anti-arnaque sur les meilleurs
    picks = []
    for score, mint, d in cands[: cfg["max_safety_checks"]]:
        try:
            ok, bonus, notes = safety(gt_get(f"/tokens/{mint}/info"), cfg)
        except Exception as e:  # noqa: BLE001
            log(f"Info indisponible {mint[:6]} : {e}")
            continue
        log(f"{d['name'][:30]:30} score {score:.1f} {'OK' if ok else 'rejeté'} {notes[:1]}")
        if ok:
            picks.append((score + bonus, mint, d, [n for n in notes if n]))
    picks.sort(reverse=True)
    picks = picks[: cfg["max_alerts_per_run"]]

    # 4. alerte
    if picks:
        lines = ["🔎 Coins qui démarrent fort (pas un conseil d'achat)"]
        for i, (score, mint, d, notes) in enumerate(picks, 1):
            lines.append(
                f"\n{i}. {d['name'].split(' / ')[0]}  (score {score:.1f})\n"
                f"MC {usd(d['mc'])} | vol 1h {usd(d['v1'])} | 1h {d['ch1']:+.0f}%\n"
                f"{d['buyers1']} acheteurs/1h, dont {d['buyers15']} sur 15 min | "
                f"achats/ventes x{d['buy_ratio']:.1f} | âge {d['age_h']:.1f} h\n"
                + (f"⚠️ déjà {d['ch1']:+.0f}% sur 1h : risque d'arriver au sommet\n" if d["ch1"] > 200 else "")
                + f"{' · '.join(notes)}\n"
                f"pump.fun/coin/{mint}\n"
                f"dexscreener.com/solana/{mint}"
            )
            state["alerted"][mint] = {"at": time.time(), "price": d["price"], "mc": d["mc"]}
            new = not os.path.exists(LOG_PATH)
            with open(LOG_PATH, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(["time_utc", "mint", "name", "score", "price_usd", "mcap_usd",
                                "vol_h1", "buyers_h1", "age_h"])
                w.writerow([now.isoformat(timespec="seconds"), mint, d["name"], f"{score:.1f}",
                            d["price"], d["mc"], d["v1"], d["buyers1"], f"{d['age_h']:.1f}"])
        telegram("\n".join(lines), dry)
    else:
        log("Rien d'assez solide ce passage.")

    if not dry:
        with open(STATE_PATH, "w") as fh:
            json.dump(state, fh, indent=1)


if __name__ == "__main__":
    main()
