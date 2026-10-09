"""docs/alpha.json for the Cockpit's "RH Chain" tab, written at the end of every run.

Nothing here is validated: the filters are an experiment until the evaluation (30+ passers with 7 days of data).
So the board leads with the experiment's scoreboard (passers next to their random controls), and the live lists
are labelled as unproven. Prices of the listed tokens are refreshed every run (1-2 DexScreener calls); the full
dataset still records every 4 hours."""

import csv
import glob
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

import filters
import store
from sources import dexscreener as ds

HORIZONS = (3, 7, 14)
NAMES = {"liquidity": "liquidity", "volume": "24h volume", "turnover": "turnover", "txns": "24h trades", "drawdown": "drawdown"}


def _path(cfg):
    name = cfg.get("board_file", "alpha.json")  # alpha.json (Robinhood), alpha_solana.json
    if os.environ.get("RHC_DATA"):  # tests write beside their scratch data
        return store.DATA / name
    return Path(__file__).resolve().parent.parent / "docs" / name


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _snapshots(since_ts):
    """Snapshot rows from the daily files back to `since_ts`, grouped by token, oldest first."""
    by = {}
    first_day = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%d")
    for p in sorted(glob.glob(str(store.DATA / "snapshots" / "*.csv"))):
        if Path(p).stem < first_day:
            continue
        with open(p, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                by.setdefault(r["token_address"], []).append(r)
    return by


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _miss(row, res, f):
    """Plain words for the filter a token misses."""
    out = []
    liq, vol = row["liquidity_usd"] or 0, row["volume_24h_usd"] or 0
    for k in ("liquidity", "volume", "turnover", "txns", "drawdown"):
        if res[k]:
            continue
        if k == "liquidity":
            out.append(f"liquidity ${liq:,.0f} (needs ${f['liquidity_usd_min']:,.0f})")
        elif k == "volume":
            out.append(f"24h volume ${vol:,.0f} (needs ${f['volume_24h_usd_min']:,.0f})")
        elif k == "turnover":
            t = vol / liq if liq else 0
            out.append(f"turnover {t:.1f}x ({'above ' + str(f['turnover_max']) + ': likely wash trading' if t > f['turnover_max'] else 'under ' + str(f['turnover_min']) + ': barely traded'})")
        elif k == "txns":
            out.append(f"{row['txns_24h'] or 0} trades in 24h (needs {f['txns_24h_min']})")
        elif k == "drawdown":
            out.append(f"{row['drawdown_pct']:.0f}% below its peak (max {f['drawdown_max_pct']})" if row["drawdown_pct"] is not None else "no peak yet")
    return out


def build(net, state, cfg, now, run_utc, dry=False, errors=None):
    f, c = cfg["filters"], cfg["cohort"]
    tracked = state["tracked"]
    cohort = store.read(store.DATA / "cohort.csv")
    checks = {x["token_address"]: x for x in store.read(store.DATA / "checks.csv")}
    band = [a for a, t in tracked.items() if filters.in_band((now - t["created"]) / 86400, f)]
    want = sorted(set(band) | {x["token_address"] for x in cohort})
    try:
        fresh = ds.tokens(net, cfg["chain"]["ds_chain"], want) if want else {}
    except Exception as e:  # noqa: BLE001  the board must not cost the run
        fresh = {}
        if errors is not None:
            errors.append(f"board prices: {str(e)[:120]}")
    oldest = min([_ts(x["assigned_utc"]) for x in cohort] + [now - 3 * 86400])
    snaps = _snapshots(oldest)

    def spark(a, live=None):
        pts = [_num(r["price_usd"]) for r in snaps.get(a, [])[-30:] if _num(r["price_usd"])]
        return pts + ([live] if live else [])

    def flags(a):
        k = checks.get(a)
        if not k:
            return None
        b = lambda v: v == "True"  # noqa: E731
        if "mint_authority" in k:  # Solana (RugCheck)
            return {"chain": "solana", "mint_auth": b(k["mint_authority"]), "freeze_auth": b(k["freeze_authority"]),
                    "lp_locked": _num(k["lp_locked_pct"]), "holders": _num(k["holders"]), "top10": _num(k["top10_pct"]),
                    "insiders": _num(k["insiders_top20"]), "insider_net": _num(k["insider_network"]), "t22": k["token2022_risk"],
                    "score": _num(k["rugcheck_score"]), "creator": _num(k["creator_pct"]), "rugged": b(k["rugged"]),
                    "launchpad": k["launchpad"], "danger": k["danger_risks"], "note": k["checks_note"]}
        return {"mint": b(k["has_mint"]), "owner": b(k["has_owner"]), "pause": b(k["can_pause"]), "blacklist": b(k["can_blacklist"]),
                "fee": b(k["can_set_fee"]), "proxy": b(k["is_proxy"]), "holders": _num(k["holders"]), "top20": _num(k["top20_pct"]),
                "seeds": _num(k["seed_overlap"]), "seed_flag": b(k["overlap_flag"]), "launchpad": k["launchpad"], "note": k["checks_note"]}

    # ---- live lists: every tracked token aged 2-10 days, re-evaluated at this run's prices
    rows = []
    for a in band:
        t, s = tracked[a], fresh.get(a)
        if not s or not s["price"]:
            continue
        t["peak"] = max(t.get("peak") or 0, s["price"])
        row = {"liquidity_usd": s["liquidity"], "volume_24h_usd": s["volume_24h"], "txns_24h": s["txns_24h"],
               "drawdown_pct": (1 - s["price"] / t["peak"]) * 100 if t["peak"] else None, "age_days": (now - t["created"]) / 86400}
        res = filters.evaluate(row, f)
        rows.append({"address": a, "symbol": t.get("symbol"), "age": round(row["age_days"], 1), "price": s["price"],
                     "liquidity": round(s["liquidity"]), "volume": round(s["volume_24h"]), "txns": s["txns_24h"],
                     "turnover": round(s["volume_24h"] / s["liquidity"], 1) if s["liquidity"] else None,
                     "drawdown": round(row["drawdown_pct"], 1) if row["drawdown_pct"] is not None else None,
                     "change_24h": s["change_24h"], "mcap": s["market_cap"], "dex": t.get("dex"), "url": s["url"],
                     "passed": sum(res.values()), "res": res, "miss": _miss(row, res, f), "flags": flags(a), "spark": spark(a, s["price"])})
    passing = sorted([r for r in rows if r["passed"] == 6], key=lambda r: -r["liquidity"])
    close = sorted([r for r in rows if r["passed"] == 5], key=lambda r: -r["liquidity"])
    funnel = {"in_band": len(rows), **{k: sum(r["res"][k] for r in rows) for k in ("liquidity", "volume", "turnover", "txns", "drawdown")},
              "all": len(passing)}
    for r in rows:
        r.pop("res")

    # ---- scoreboard: every cohort token, return since it qualified (dead = -100%), and at 3/7/14 days
    board, groups = [], {"passer": [], "control": []}
    for x in cohort:
        a, p0, t0 = x["token_address"], _num(x["price_at_assign"]), _ts(x["assigned_utc"])
        s, hist = fresh.get(a), snaps.get(a, [])
        dead = (s is None and hist and hist[-1]["status"] == "dead") or (s is not None and s["liquidity"] < c["dead_liquidity_usd"])
        price = s["price"] if s else (_num(hist[-1]["price_usd"]) if hist else None)
        ret = -100.0 if dead else ((price / p0 - 1) * 100 if price and p0 else None)
        at = {}
        for h in HORIZONS:
            target = t0 + h * 86400
            if now < target:
                continue
            near = [r for r in hist if abs(_ts(r["run_utc"]) - target) <= 4 * 3600]
            if near:
                r = min(near, key=lambda r: abs(_ts(r["run_utc"]) - target))
                at[h] = -100.0 if r["status"] == "dead" else ((_num(r["price_usd"]) / p0 - 1) * 100 if _num(r["price_usd"]) and p0 else None)
            elif hist and hist[-1]["status"] == "dead" and _ts(hist[-1]["run_utc"]) <= target:
                at[h] = -100.0
        item = {"address": a, "symbol": x["symbol"], "cohort": x["cohort"], "assigned": x["assigned_utc"], "days": round((now - t0) / 86400, 1),
                "age_at_assign": _num(x["age_days_at_assign"]), "price_at_assign": p0, "price": price, "ret": round(ret, 1) if ret is not None else None,
                "dead": bool(dead), "at": {str(h): round(v, 1) for h, v in at.items() if v is not None}, "matched": x["matched_passer"] or None,
                "liquidity": round(s["liquidity"]) if s else None, "url": (s or {}).get("url") or f"https://dexscreener.com/robinhood/{a}",
                "flags": flags(a), "spark": spark(a, s["price"] if s else None)}
        board.append(item)
        groups[x["cohort"]].append(item)

    def summary(items):
        rets = [i["ret"] for i in items if i["ret"] is not None]
        d7 = [i["at"]["7"] for i in items if "7" in i["at"]]
        return {"n": len(items), "median": round(statistics.median(rets), 1) if rets else None,
                "up2x": sum(1 for v in rets if v >= 100), "dead80": sum(1 for v in rets if v <= -80),
                "n7": len(d7), "median7": round(statistics.median(d7), 1) if d7 else None}

    # ---- recorder health over the last 24 hours
    runs = store.read(store.DATA / "runs.csv")
    day = [r for r in runs if now - _ts(r["run_utc"]) <= 86400]
    gaps = [r["stopped"] for r in day if "discovery gap" in r["stopped"]]
    gap_min = 0
    for g in gaps:
        try:
            a, b = g.split("launched ")[1].split(" were")[0].split(" to ")
            gap_min += (_ts(b) - _ts(a)) / 60
        except (IndexError, ValueError):
            pass
    sampled = 0  # Solana: minutes of launch time each run's sample covered
    for r in day:
        for part in r["stopped"].split("; "):
            if part.startswith("sample: launches "):
                try:
                    a, b = part[len("sample: launches "):].split(" to ")
                    sampled += (_ts(b) - _ts(a)) / 60
                except ValueError:
                    pass
    health = {"last_run": run_utc, "runs_24h": len(day), "new_pools_24h": sum(int(r["new_pools"] or 0) for r in day),
              "tracked": len(tracked), "gap_runs_24h": len(gaps), "gap_minutes_24h": round(gap_min),
              "sampled_pct_24h": round(sampled / 1440 * 100) if sampled else None,
              "last_snapshot": _iso(state["last_snapshot"]) if state.get("last_snapshot") else None,
              "errors_24h": sum(1 for r in day if r["errors"])}

    out = {"generated_at": _iso(now), "chain": cfg["chain"].get("name", "robinhood"),
           "settings": {"filters": f, "verdict_passers": 30, "verdict_days": 7},
           "summary": {k: summary(v) for k, v in groups.items()}, "cohort": board, "passing": passing, "close": close[:25],
           "funnel": funnel, "health": health}
    if dry:
        print(f"[dry run] alpha.json: {len(passing)} passing, {len(close)} close, {len(board)} cohort rows")
        return out
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":"), default=lambda o: None), encoding="utf-8")
    os.replace(tmp, path)
    return out
