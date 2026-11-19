#!/usr/bin/env python3
"""
Trading en SIMULATION (paper trading) : aucun vrai SOL, aucune clé privée.

Entrées : les coins que scanner.py vient de signaler (scanner_alerts.csv).
Sorties  : take-profit, stop-loss et stop temporel définis dans paper_config.json.
Chaque achat/vente fictif part sur Telegram et s'écrit dans paper_trades.csv.

Coûts simulés : cost_pct_per_side (frais PumpPortal 0,5 % + slippage estimé) à l'achat ET à la vente.
Limite : les prix ne sont vérifiés qu'à chaque passage GitHub (~5 à 15 min), donc un
stop-loss peut être exécuté bien plus bas que -25 %. C'est voulu : ça reflète le vrai risque.

Usage :
  python3 paper_trader.py            # un passage (lancé par GitHub après scanner.py)
  python3 paper_trader.py --dry      # affiche sans envoyer sur Telegram ni sauvegarder
  python3 paper_trader.py --report   # bilan de la simulation
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "paper_config.json")
STATE_PATH = os.path.join(HERE, "paper_state.json")
TRADES_PATH = os.path.join(HERE, "paper_trades.csv")
ALERTS_PATH = os.path.join(HERE, "scanner_alerts.csv")
SOL_MINT = "So11111111111111111111111111111111111111112"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


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


def prices_usd(mints):
    """Prix USD de plusieurs tokens en un seul appel GeckoTerminal (max 30)."""
    out = {}
    mints = list(dict.fromkeys(mints))[:30]
    req = urllib.request.Request(f"{GT}/tokens/multi/{','.join(mints)}",
                                 headers={"Accept": "application/json", "User-Agent": "paper-trader/1.0"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode()).get("data", [])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                log("Limite GeckoTerminal atteinte, pause 30 s")
                time.sleep(30)
                continue
            raise
    for t in data:
        p = t["attributes"].get("price_usd")
        if p:
            out[t["attributes"]["address"]] = float(p)
    return out


DRY = "--dry" in sys.argv


def write_trade(row):
    if DRY:
        print("trade:", row)
        return
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time_utc", "mint", "name", "action", "price_usd", "fraction",
                        "sol_in", "sol_out", "pnl_sol", "pnl_pct"])
        w.writerow(row)


def short(name):
    return (name or "?").split(" / ")[0][:24]


def totals(state):
    closed = state["closed"]
    pnl = sum(c["pnl_sol"] for c in closed)
    wins = sum(1 for c in closed if c["pnl_sol"] > 0)
    rate = f"{100 * wins / len(closed):.0f}%" if closed else "-"
    return (f"📊 Bilan simu : {pnl:+.3f} SOL sur {len(closed)} trades clôturés "
            f"({rate} gagnants) · {len(state['positions'])} en cours · aujourd'hui {state['day']['pnl_sol']:+.3f} SOL")


def report():
    state = load_json(STATE_PATH, None)
    if not state:
        print("Pas encore de simulation.")
        return
    print(totals(state))
    for c in state["closed"][-20:]:
        print(f"  {c['closed_at'][:16]}  {short(c['name']):24} {c['pnl_sol']:+.3f} SOL ({c['pnl_pct']:+.0f}%)  {c['reason']}")
    for m, p in state["positions"].items():
        print(f"  EN COURS {short(p['name']):24} depuis {p['opened_at'][:16]}  reste {p['remaining']:.0%}")


def sell(pos, frac_of_initial, price_sol, cfg, reason, now_iso, events, mint):
    """Vend une fraction de la position initiale au prix courant (en SOL)."""
    cost = cfg["cost_pct_per_side"] / 100
    tokens = pos["tokens"] * frac_of_initial
    sol_out = tokens * price_sol * (1 - cost)
    sol_in = pos["size_sol"] * frac_of_initial
    pos["remaining"] = round(pos["remaining"] - frac_of_initial, 6)
    pos["sol_out"] += sol_out
    pos["last_reason"] = reason
    pct = (sol_out / sol_in - 1) * 100 if sol_in else 0
    write_trade([now_iso, mint, pos["name"], reason, pos["last_price_usd"], f"{frac_of_initial:.2f}",
                 f"{sol_in:.4f}", f"{sol_out:.4f}", f"{sol_out - sol_in:+.4f}", f"{pct:+.1f}"])
    emoji = "🟢" if sol_out >= sol_in else "🔴"
    events.append(f"{emoji} VENTE simu {short(pos['name'])} ({frac_of_initial:.0%}) · {reason} · "
                  f"{sol_out - sol_in:+.3f} SOL ({pct:+.0f}%)")


def main():
    if "--report" in sys.argv:
        report()
        return
    dry = "--dry" in sys.argv
    cfg = load_json(CONFIG_PATH, None)
    if not cfg or not cfg.get("enabled"):
        log("Simulation désactivée (paper_config.json).")
        return
    if cfg.get("mode") != "simulation":
        sys.exit("Seul le mode 'simulation' existe pour l'instant.")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    today = now.date().isoformat()
    state = load_json(STATE_PATH, None)
    if state is None:
        # premier passage : on ne rejoue pas l'historique, seulement les alertes récentes
        state = {"processed_until": "", "positions": {}, "closed": [],
                 "day": {"date": today, "pnl_sol": 0.0}}
    if state["day"]["date"] != today:
        state["day"] = {"date": today, "pnl_sol": 0.0}

    # nouvelles alertes du scanner
    new_alerts = []
    if os.path.exists(ALERTS_PATH):
        with open(ALERTS_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row["time_utc"] > state["processed_until"]:
                    new_alerts.append(row)

    # prix courants
    need = list(state["positions"]) + [r["mint"] for r in new_alerts]
    try:
        px = prices_usd([SOL_MINT] + need)
    except Exception as e:  # noqa: BLE001
        log(f"Prix indisponibles ({e}), on réessaie au prochain passage.")
        return
    sol_usd = px.get(SOL_MINT)
    if not sol_usd:
        log("Prix du SOL indisponible, passage ignoré.")
        return

    if new_alerts:
        state["processed_until"] = max(r["time_utc"] for r in new_alerts)
    events = []

    # 1. gestion des positions ouvertes
    for mint in list(state["positions"]):
        pos = state["positions"][mint]
        p_usd = px.get(mint)
        age_min = (now - datetime.fromisoformat(pos["opened_at"])).total_seconds() / 60
        if p_usd is None:
            if age_min / 60 >= cfg["close_as_rug_after_hours_without_price"]:
                pos["last_price_usd"] = 0
                sell(pos, pos["remaining"], 0.0, cfg, "plus de prix (rug ?)", now_iso, events, mint)
            else:
                continue
        else:
            pos["last_price_usd"] = p_usd
            price_sol = p_usd / sol_usd
            ratio = price_sol / pos["entry_price_sol"]
            if ratio <= 1 + cfg["stop_loss_pct"] / 100:
                sell(pos, pos["remaining"], price_sol, cfg, f"stop-loss (x{ratio:.2f})", now_iso, events, mint)
            else:
                if not pos["tp1_done"] and ratio >= 1 + cfg["take_profit_1_pct"] / 100:
                    sell(pos, cfg["take_profit_1_sell_fraction"], price_sol, cfg,
                         f"take-profit 1 (x{ratio:.2f})", now_iso, events, mint)
                    pos["tp1_done"] = True
                if pos["remaining"] > 0 and ratio >= 1 + cfg["take_profit_2_pct"] / 100:
                    sell(pos, pos["remaining"], price_sol, cfg, f"take-profit 2 (x{ratio:.2f})", now_iso, events, mint)
                elif pos["remaining"] > 0 and age_min >= cfg["time_stop_minutes"]:
                    sell(pos, pos["remaining"], price_sol, cfg,
                         f"stop temps {age_min:.0f} min (x{ratio:.2f})", now_iso, events, mint)
        if pos["remaining"] <= 1e-6:
            pnl = pos["sol_out"] - pos["size_sol"]
            state["day"]["pnl_sol"] += pnl
            state["closed"].append({"mint": mint, "name": pos["name"], "opened_at": pos["opened_at"],
                                    "closed_at": now_iso, "pnl_sol": round(pnl, 6),
                                    "pnl_pct": round(pnl / pos["size_sol"] * 100, 1),
                                    "reason": pos.get("last_reason", "")})
            del state["positions"][mint]

    # 2. nouvelles entrées
    traded_today = {c["mint"] for c in state["closed"] if c["closed_at"][:10] == today}
    for row in new_alerts:
        mint = row["mint"]
        alert_age = (now - datetime.fromisoformat(row["time_utc"])).total_seconds() / 60
        if mint in state["positions"] or mint in traded_today:
            continue
        if alert_age > cfg["max_alert_age_minutes"]:
            log(f"Alerte trop vieille ({alert_age:.0f} min) : {short(row['name'])} ignoré")
            continue
        if len(state["positions"]) >= cfg["max_open_positions"]:
            log(f"{cfg['max_open_positions']} positions déjà ouvertes : {short(row['name'])} ignoré")
            continue
        if state["day"]["pnl_sol"] <= -cfg["max_daily_loss_sol"]:
            events.append(f"⛔ Perte max du jour atteinte ({state['day']['pnl_sol']:+.3f} SOL) : plus d'achat aujourd'hui")
            break
        # prix d'entrée : prix actuel si dispo (plus réaliste), sinon prix de l'alerte
        p_usd = px.get(mint) or float(row["price_usd"])
        price_sol = p_usd / sol_usd
        cost = cfg["cost_pct_per_side"] / 100
        size = cfg["size_sol"]
        state["positions"][mint] = {
            "name": row["name"], "opened_at": now_iso, "size_sol": size,
            "entry_price_sol": price_sol, "tokens": size * (1 - cost) / price_sol,
            "remaining": 1.0, "tp1_done": False, "sol_out": 0.0, "last_price_usd": p_usd,
        }
        write_trade([now_iso, mint, row["name"], "ACHAT", p_usd, "1.00", f"{size:.4f}", "", "", ""])
        events.append(f"🛒 ACHAT simu {short(row['name'])} · {size} SOL · MC ${float(row['mcap_usd'])/1e3:.0f}k\n"
                      f"pump.fun/coin/{mint}")

    log(f"{len(new_alerts)} nouvelles alertes, {len(state['positions'])} positions ouvertes, {len(events)} évènements")
    if events:
        telegram("🧪 PAPER TRADING (aucun vrai SOL)\n\n" + "\n\n".join(events) + "\n\n" + totals(state), dry)
    if not dry:
        with open(STATE_PATH, "w") as fh:
            json.dump(state, fh, indent=1)


if __name__ == "__main__":
    main()
