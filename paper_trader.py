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
LIVE_PATH = os.path.join(HERE, "early_live.json")
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
                        "sol_in", "sol_out", "pnl_sol", "pnl_pct", "strategy"])
        w.writerow(row)


def short(name):
    return (name or "?").split(" / ")[0][:24]


def totals(state, label=""):
    closed = state["closed"]
    pnl = sum(c["pnl_sol"] for c in closed)
    wins = sum(1 for c in closed if c["pnl_sol"] > 0)
    rate = f"{100 * wins / len(closed):.0f}%" if closed else "-"
    return (f"📊 {label}Bilan : {pnl:+.3f} SOL sur {len(closed)} trades clôturés "
            f"({rate} gagnants) · {len(state['positions'])} en cours · aujourd'hui {state['day']['pnl_sol']:+.3f} SOL")


def report():
    full = load_json(STATE_PATH, None)
    if not full:
        print("Pas encore de simulation.")
        return
    cfg = load_json(CONFIG_PATH, {"strategies": {}})
    for name, state in migrate(full)["strategies"].items():
        if name not in cfg["strategies"]:
            continue  # ancienne stratégie retirée du réglage
        print("\n" + totals(state, f"[{name}] "))
        report_one(state)


def report_one(state):
    for c in state["closed"][-20:]:
        print(f"  {c['closed_at'][:16]}  {short(c['name']):24} {c['pnl_sol']:+.3f} SOL ({c['pnl_pct']:+.0f}%)  {c['reason']}")
    for m, p in state["positions"].items():
        print(f"  EN COURS {short(p['name']):24} depuis {p['opened_at'][:16]}  reste {p['remaining']:.0%}")


def sell(pos, frac_of_initial, price_sol, cfg, reason, now_iso, events, mint):
    tag = cfg["_name"]
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
                 f"{sol_in:.4f}", f"{sol_out:.4f}", f"{sol_out - sol_in:+.4f}", f"{pct:+.1f}", tag])
    emoji = "🟢" if sol_out >= sol_in else "🔴"
    events.append(f"{emoji} [{tag}] VENTE {short(pos['name'])} ({frac_of_initial:.0%}) · {reason} · "
                  f"{sol_out - sol_in:+.3f} SOL ({pct:+.0f}%)")


def empty_strategy(today):
    return {"positions": {}, "closed": [], "day": {"date": today, "pnl_sol": 0.0}}


def migrate(full):
    """Ancien format (une seule stratégie) -> {"processed_until", "strategies": {...}}."""
    if "strategies" in full:
        return full
    return {"processed_until": full.get("processed_until", ""),
            "strategies": {"A_rapide": {k: full[k] for k in ("positions", "closed", "day")}}}


def strategy_configs(cfg):
    common = {k: v for k, v in cfg.items() if k not in ("strategies", "enabled", "mode")}
    out = {}
    for name, over in cfg["strategies"].items():
        c = dict(common)
        c.update(over)
        c["_name"] = name
        out[name] = c
    return out


def run_strategy(state, cfg, px, sol_usd, new_alerts, now, events):
    now_iso = now.isoformat(timespec="seconds")
    today = now.date().isoformat()
    tag = cfg["_name"]
    if state["day"]["date"] != today:
        state["day"] = {"date": today, "pnl_sol": 0.0}

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
            last = pos.get("last_price_usd") or 0
            if last > 0 and not (1 / cfg["max_price_jump_x"] <= p_usd / last <= cfg["max_price_jump_x"]):
                # saut x20 entre deux passages (2 min) : donnée de prix douteuse, on attend la suivante
                log(f"[{tag}] prix douteux ignoré pour {short(pos['name'])} (x{p_usd / last:.1f})")
                continue  # jamais accepté automatiquement : il faut un prix cohérent
            pos["last_price_usd"] = p_usd
            price_sol = p_usd / sol_usd
            ratio = price_sol / pos["entry_price_sol"]
            pos["min_ratio"] = min(pos.get("min_ratio", 1.0), ratio)
            pos["max_ratio"] = max(pos.get("max_ratio", 1.0), ratio)
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
                                    "min_ratio": round(pos.get("min_ratio", 1.0), 4),
                                    "max_ratio": round(pos.get("max_ratio", 1.0), 4),
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
            log(f"[{tag}] alerte trop vieille ({alert_age:.0f} min) : {short(row['name'])} ignoré")
            continue
        if len(state["positions"]) >= cfg["max_open_positions"]:
            log(f"[{tag}] {cfg['max_open_positions']} positions déjà ouvertes : {short(row['name'])} ignoré")
            continue
        if state["day"]["pnl_sol"] <= -cfg["max_daily_loss_sol"]:
            events.append(f"⛔ [{tag}] perte max du jour atteinte ({state['day']['pnl_sol']:+.3f} SOL) : plus d'achat aujourd'hui")
            break
        p_usd = px.get(mint) or float(row["price_usd"])
        price_sol = p_usd / sol_usd
        cost = cfg["cost_pct_per_side"] / 100
        size = cfg["size_sol"]
        state["positions"][mint] = {
            "name": row["name"], "opened_at": now_iso, "size_sol": size,
            "entry_price_sol": price_sol, "tokens": size * (1 - cost) / price_sol,
            "remaining": 1.0, "tp1_done": False, "sol_out": 0.0, "last_price_usd": p_usd,
        }
        write_trade([now_iso, mint, row["name"], "ACHAT", p_usd, "1.00", f"{size:.4f}", "", "", "", tag])
        events.append(f"🛒 [{tag}] ACHAT {short(row['name'])} · {size} SOL · MC ${float(row['mcap_usd'])/1e3:.0f}k\n"
                      f"pump.fun/coin/{mint}")


def load_live():
    """Nouveaux coins vus par early_listener.py (None si absent ou vieux de plus d'1 min)."""
    live = load_json(LIVE_PATH, None)
    if not live or time.time() - live.get("updated", 0) > 60:
        return None
    return live["tokens"]


def dexscreener(mints):
    """Paires DexScreener par mint (30 par appel, gratuit, ~300 appels/min)."""
    out = {}
    mints = list(dict.fromkeys(mints))
    for i in range(0, len(mints), 30):
        url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30])
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "paper-trader/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                pairs = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            log(f"DexScreener indisponible : {e}")
            continue
        for p in pairs or []:
            m = p["baseToken"]["address"]
            # garde la paire la plus liquide / la plus active
            if m not in out or (p.get("volume") or {}).get("h24", 0) > (out[m].get("volume") or {}).get("h24", 0):
                out[m] = p
    return out


def early_candidates(live, cfg, sol_usd, now_iso, ds):
    """Coins de quelques minutes qui démarrent fort et passent les filtres anti-arnaque."""
    out = []
    for mint, t in live.items():
        age_min = (time.time() - t["created_at"]) / 60
        if not (cfg["early_min_age_minutes"] <= age_min <= cfg["early_max_age_minutes"]):
            continue
        if t["dev_initial_pct"] > cfg["early_max_dev_pct"]:
            continue
        p = ds.get(mint)
        if not p:
            continue
        m5 = (p.get("txns") or {}).get("m5") or {}
        buys, sells = m5.get("buys", 0), m5.get("sells", 0)
        if buys < cfg["early_min_buys_5m"] or buys / max(sells, 1) < cfg["early_min_buy_sell_ratio"]:
            continue
        if (p.get("quoteToken") or {}).get("address") != SOL_MINT:
            continue
        price_sol = float(p.get("priceNative") or 0)
        mcap_sol = price_sol * 1_000_000_000
        if not price_sol or mcap_sol < t["mcap_sol_start"] * (1 + cfg["early_min_mcap_growth_pct"] / 100):
            continue
        if f_pct(p, "m5") < cfg["early_min_change_5m_pct"]:
            continue
        out.append((buys, {
            "time_utc": now_iso, "mint": mint, "name": f"{t['name']} ({t['symbol']})",
            "price_usd": price_sol * sol_usd, "mcap_usd": mcap_sol * sol_usd,
        }))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out]


def f_pct(pair, key):
    try:
        return float((pair.get("priceChange") or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


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
    strategies = strategy_configs(cfg)

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    full = load_json(STATE_PATH, None)
    full = migrate(full) if full else {"processed_until": "", "strategies": {}}
    for name in strategies:
        full["strategies"].setdefault(name, empty_strategy(today))

    # nouvelles alertes du scanner
    new_alerts = []
    if os.path.exists(ALERTS_PATH):
        with open(ALERTS_PATH, newline="") as fh:
            for row in csv.DictReader(fh):
                if row["time_utc"] > full["processed_until"]:
                    new_alerts.append(row)

    # prix courants (un seul appel pour toutes les stratégies)
    need = [m for name in strategies for m in full["strategies"][name]["positions"]]
    need += [r["mint"] for r in new_alerts]
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
        full["processed_until"] = max(r["time_utc"] for r in new_alerts)

    # coins tout jeunes + positions de la stratégie « tôt » : DexScreener (plus frais, couvre la bonding curve)
    live = load_live()
    early_names = [n for n, c in strategies.items() if c.get("entry_source") == "early"]
    # 2e source de prix : DexScreener pour toutes les positions (+ coins tout jeunes pour la stratégie « tôt »)
    want = [m for n in strategies for m in full["strategies"][n]["positions"]]
    if early_names and live:
        want += [m for m, t in live.items() if time.time() - t["created_at"] <= 6 * 60]
    ds = dexscreener(want)
    for m, p in ds.items():
        if not p.get("priceNative") or (p.get("quoteToken") or {}).get("address") != SOL_MINT:
            continue
        ds_usd = float(p["priceNative"]) * sol_usd
        gt_usd = px.get(m)
        if gt_usd and max(gt_usd, ds_usd) / min(gt_usd, ds_usd) > 2:
            # les deux sources ne sont pas d'accord : on garde la plus prudente (la plus basse)
            log(f"prix en désaccord pour {m[:6]}… (GT {gt_usd:.3g} / DS {ds_usd:.3g}) : on garde le plus bas")
            px[m] = min(gt_usd, ds_usd)
        else:
            px[m] = ds_usd

    full["listener"] = {"checked_at": now.isoformat(timespec="seconds"),
                        "ok": live is not None, "new_coins_10min": len(live or {})}
    # nettoyage : trades à gain impossible (> x30) dus à des prix aberrants avant le garde-fou
    for n in strategies:
        st = full["strategies"][n]
        bad = [c for c in st["closed"] if c["pnl_pct"] > 3000]
        if bad:
            st.setdefault("rejected", []).extend(bad)
            st["closed"] = [c for c in st["closed"] if c["pnl_pct"] <= 3000]
            for c in bad:
                if c["closed_at"][:10] == st["day"]["date"]:
                    st["day"]["pnl_sol"] -= c["pnl_sol"]
            log(f"[{n}] {len(bad)} trade(s) aberrant(s) retiré(s) du bilan")

    events = []
    for name, scfg in strategies.items():
        if scfg.get("entry_source") == "early":
            if live is None:
                log(f"[{name}] pas de données temps réel (listener arrêté ?)")
            alerts = early_candidates(live, scfg, sol_usd, now.isoformat(timespec="seconds"), ds) if live else []
        else:
            alerts = new_alerts
        run_strategy(full["strategies"][name], scfg, px, sol_usd, alerts, now, events)

    open_count = sum(len(full["strategies"][n]["positions"]) for n in strategies)
    log(f"{len(new_alerts)} nouvelles alertes, {open_count} positions ouvertes, {len(events)} évènements")
    if events:
        bilans = "\n".join(totals(full["strategies"][n], f"[{n}] ") for n in strategies)
        telegram("🧪 PAPER TRADING (aucun vrai SOL)\n\n" + "\n\n".join(events) + "\n\n" + bilans, dry)
    if not dry:
        with open(STATE_PATH, "w") as fh:
            json.dump(full, fh, indent=1)


if __name__ == "__main__":
    main()
