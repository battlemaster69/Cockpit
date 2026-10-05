#!/usr/bin/env python3
"""
Robinhood Chain survivor recorder: one run, then exit. A data recorder, not a trader: no entries, exits or orders.

Every run discovers new pools (GeckoTerminal keeps only the newest ~200, about an hour on this chain). A run at
least `snapshot.every_hours` after the last snapshot also catches up on missed tokens, snapshots every tracked token,
labels the survivor filters, assigns passers and matched controls, and runs the RPC checks on them.
Spec: the "Robinhood Chain Survivor Recorder — Spec" doc (Claude Docs).

    python rhc_recorder/run.py              # one run (snapshots when due)
    python rhc_recorder/run.py --snapshot   # force a snapshot run
    python rhc_recorder/run.py --dry-run    # print what would be recorded, write nothing
"""

import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks  # noqa: E402
import filters  # noqa: E402
import store  # noqa: E402
from net import Net, SourceStopped  # noqa: E402
from sources import dexscreener as ds  # noqa: E402
from sources import geckoterminal as gt  # noqa: E402
from sources.rpc import RPC  # noqa: E402

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
KNOWN = yaml.safe_load((HERE / "known_addresses.yaml").read_text(encoding="utf-8"))
SKIP_TOKENS = {a.lower() for a in KNOWN.get("skip_tokens") or {}}
NON_HOLDERS = {a.lower() for a in KNOWN.get("non_holders") or {}}
DRY = "--dry-run" in sys.argv
NOW = time.time()
RUN_UTC = datetime.fromtimestamp(NOW, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
DAY = RUN_UTC[:10]
CH = CFG["chain"]
ERRORS = []


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


def r(v, n=6):
    return None if v is None else float(f"{v:.{n}g}")


def guarded(name, fn, *a, default=None):
    """Run one step; a stopped source or an error is noted and the run carries on with what it has."""
    try:
        return fn(*a)
    except SourceStopped as e:
        ERRORS.append(f"{name}: stopped ({e})")
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"{name}: {str(e)[:160]}")
    return default


# ------------------------------------------------------------------ discovery and admission

def new_token(pool):
    """The side of a pool that is the new token: the base, unless the base is ETH/WETH/a stablecoin."""
    for side in ("base", "quote"):
        t = pool[side]
        if t["address"] not in SKIP_TOKENS:
            return t
    return None


def admit(net, state, cands, found_by, log_all=True):
    """Price candidate tokens and track those above the floor. Each token is logged to the day's universe file when
    first seen; a retry or re-price (log_all=False) logs it again only if it now joins the tracked set."""
    d, f = CFG["discovery"], CFG["filters"]
    known = set(state["tracked"]) | set(state["below"]) | set(state["skip"])
    cands = {a: c for a, c in cands.items() if a not in known}
    if not cands:
        return []
    priced = guarded("dexscreener (admission)", ds.tokens, net, CH["ds_chain"], list(cands), default={}) or {}
    rows = []
    for a, c in cands.items():
        s = priced.get(a)
        ds_created = (s or {}).get("oldest_pair_ms")
        created = min(x for x in (c.get("created"), ds_created / 1000 if ds_created else None, NOW) if x)
        age = (NOW - created) / 86400
        liq = (s or {}).get("liquidity") or 0
        tracked = bool(s) and liq >= d["track_liquidity_usd_min"] and age <= d["max_track_age_days"]
        rows.append({"token_address": a, "symbol": (s or {}).get("symbol") or c["symbol"], "name": (s or {}).get("name") or c["name"],
                     "pair_address": (s or {}).get("pair") or c["pool"], "dex": c["dex"], "pool_created_utc": utc(created),
                     "first_seen_utc": RUN_UTC, "found_by": found_by, "liquidity_at_discovery": r(liq), "tracked": tracked})
        if age > d["max_track_age_days"]:
            state["skip"][a] = NOW + 45 * 86400        # an old token with a new pool: never a 2-to-10-day survivor
        elif tracked:
            late = age > 1
            state["tracked"][a] = {"symbol": rows[-1]["symbol"], "created": created, "dex": c["dex"], "pair": s["pair"],
                                   "peak": s["price"] or 0, "peak_source": "candles_pending" if late else "recorder", "misses": 0}
        elif age < d["recheck_below_floor_days"]:
            state["below"][a] = {"created": created, "dex": c["dex"], "symbol": rows[-1]["symbol"], "pool": c["pool"],
                                 "checked": NOW if s else 0, "added": NOW}   # not on DexScreener yet: priced again next run
    log = rows if log_all else [x for x in rows if x["tracked"]]
    store.append(store.DATA / "universe" / f"{DAY}.csv", store.UNIVERSE, log, DRY)
    return log


def discover(net, state):
    """Page through GeckoTerminal's new pools until reaching pools already seen."""
    seen, cands, n_new = state["seen_pools"], {}, 0
    for page in range(1, CFG["discovery"]["new_pool_pages_max"] + 1):
        pools = guarded("geckoterminal new_pools", gt.new_pools, net, CH["gt_network"], page, default=None)
        if not pools:
            break
        fresh = [p for p in pools if p["pool"] not in seen]
        for p in fresh:
            seen[p["pool"]] = NOW
            t = new_token(p)
            if t:
                cands.setdefault(t["address"], {**t, "created": p["created"].timestamp(), "dex": p["dex"], "pool": p["pool"]})
        n_new += len(fresh)
        if len(fresh) < len(pools):
            break
    # tokens an earlier run saw before DexScreener had indexed them: price them again (before this run's new ones)
    unpriced = {a: {**b, "address": a, "name": ""} for a, b in state["below"].items() if not b.get("checked") and b.get("added", 0) < NOW}
    for a in unpriced:
        state["below"].pop(a)
    rows = admit(net, state, unpriced, "new_pools_retry", log_all=False) if unpriced else []
    rows += admit(net, state, cands, "new_pools")
    return n_new, rows


def catch_up(net, state):
    """Snapshot runs: top pools by 24h volume (tokens missed at launch) and a daily re-price of below-floor tokens."""
    cands = {}
    for page in range(1, CFG["discovery"]["top_volume_pages"] + 1):
        pools = guarded("geckoterminal top pools", gt.top_pools, net, CH["gt_network"], page, default=None)
        if not pools:
            break
        for p in pools:
            t = new_token(p)
            if t:
                cands.setdefault(t["address"], {**t, "created": p["created"].timestamp(), "dex": p["dex"], "pool": p["pool"]})
    rows = admit(net, state, cands, "top_volume")
    d = CFG["discovery"]
    for a, b in list(state["below"].items()):
        if (NOW - b["created"]) / 86400 > d["recheck_below_floor_days"]:
            del state["below"][a]
    due = {a: {**b, "address": a, "name": ""} for a, b in state["below"].items()
           if b.get("checked") and NOW - b["checked"] >= d["recheck_every_hours"] * 3600}
    for a in due:
        state["below"].pop(a)
    rows += admit(net, state, due, "recheck", log_all=False)  # still below the floor: admit() puts it back for tomorrow
    return rows


# ------------------------------------------------------------------ snapshot, labels, cohorts

def snapshot(net, state):
    f, c, sn = CFG["filters"], CFG["cohort"], CFG["snapshot"]
    tracked = state["tracked"]
    priced = guarded("dexscreener (snapshot)", ds.tokens, net, CH["ds_chain"], list(tracked), default=None)
    if priced is None:
        return []
    # late-found tokens: their peak comes from GeckoTerminal's daily candles, a few per run
    for a in [a for a, t in tracked.items() if t["peak_source"] == "candles_pending"][:sn["peak_from_candles_max"]]:
        hi = guarded("geckoterminal candles", gt.daily_high, net, CH["gt_network"], tracked[a]["pair"], default=None)
        if hi:
            tracked[a]["peak"], tracked[a]["peak_source"] = max(tracked[a]["peak"], hi), "candles"
    rows = []
    for a, t in list(tracked.items()):
        s = priced.get(a)
        age = (NOW - t["created"]) / 86400
        if not s:
            t["misses"] += 1
            if t["misses"] >= sn["missing_runs_dead"]:
                rows.append({"run_utc": RUN_UTC, "token_address": a, "pair_address": t["pair"], "age_days": round(age, 2), "status": "dead"})
                end(state, a)
            continue
        t["misses"], t["pair"] = 0, s["pair"]
        if s["price"]:
            t["peak"] = max(t["peak"] or 0, s["price"])
        dd = (1 - s["price"] / t["peak"]) * 100 if s["price"] and t["peak"] else None
        row = {"run_utc": RUN_UTC, "token_address": a, "pair_address": s["pair"], "age_days": round(age, 2), "price_usd": r(s["price"]),
               "liquidity_usd": round(s["liquidity"]), "volume_24h_usd": round(s["volume_24h"]), "txns_24h": s["txns_24h"],
               "market_cap_usd": round(s["market_cap"]) if s["market_cap"] else None, "peak_price_usd": r(t["peak"]),
               "drawdown_pct": round(dd, 1) if dd is not None else None, "status": "live"}
        if filters.in_band(age, f):
            res = filters.evaluate(row, f)
            row["filters_passed"], row["pass"] = sum(res.values()), all(res.values())
        if s["liquidity"] < c["dead_liquidity_usd"]:
            row["status"] = "dead"
            end(state, a)
        elif age > CFG["discovery"]["max_track_age_days"]:
            row["status"] = "ended"
            end(state, a)
        rows.append(row)
    store.append(store.DATA / "snapshots" / f"{DAY}.csv", store.SNAPSHOT, rows, DRY)
    return rows


def end(state, a):
    state["tracked"].pop(a, None)
    state["skip"][a] = NOW + 45 * 86400


def assign(state, rows):
    """A token's first full pass makes it a passer; each passer gets one random control from the same age band."""
    c, f = CFG["cohort"], CFG["filters"]
    in_cohort = {x["token_address"] for x in store.read(store.DATA / "cohort.csv")}
    seed = c["random_seed"] + int(NOW)
    rng = random.Random(seed)
    passers = [x for x in rows if x.get("pass") is True and x["token_address"] not in in_cohort]
    pool = [x for x in rows if x.get("pass") is False and x["token_address"] not in in_cohort and x["status"] == "live"
            and (x["liquidity_usd"] or 0) >= c["control_liquidity_usd_min"] and filters.in_band(x["age_days"], f)]
    rng.shuffle(pool)
    out = []
    for p in passers:
        out.append(cohort_row(state, p, "passer"))
        if pool:
            out.append(cohort_row(state, pool.pop(), "control", p["token_address"]))
    store.append(store.DATA / "cohort.csv", store.COHORT, out, DRY)
    for x in out:  # what the check needs, kept here in case the token dies before its turn
        t = state["tracked"].get(x["token_address"], {})
        state["check_queue"][x["token_address"]] = {"tries": 0, "created": t.get("created"), "dex": t.get("dex", "")}
    return out, seed


def cohort_row(state, x, kind, matched=None):
    t = state["tracked"].get(x["token_address"], {})
    return {"token_address": x["token_address"], "symbol": t.get("symbol"), "cohort": kind, "assigned_utc": RUN_UTC,
            "age_days_at_assign": x["age_days"], "price_at_assign": x["price_usd"], "liquidity_at_assign": x["liquidity_usd"],
            "matched_passer": matched, "peak_source": t.get("peak_source"), "mechanism": ""}


# ------------------------------------------------------------------ checks

def run_checks(net, state):
    cc = CFG["checks"]
    queue = state["check_queue"]
    if not queue:
        return 0
    rpc = RPC(net, CH["rpc_url"])
    seeds = {x["wallet"].lower() for x in store.read(store.DATA / "seed_wallets.csv") if x.get("wallet")}
    done, out = 0, []
    for a in list(queue)[:cc["max_tokens_per_run"]]:
        t = queue[a]
        try:
            row = {"token_address": a, "checked_utc": RUN_UTC, "src_verified": "", "launchpad": t.get("dex", "")}
            row.update(checks.contract_flags(rpc, a))
            if row.get("checks_note") != "no contract code":
                start, head = rpc.block_at((t.get("created") or NOW) - cc["history_margin_days"] * 86400)
                pairs = guarded("dexscreener pairs", ds.token_pairs, net, CH["ds_chain"], a, default=[]) or []
                exclude = NON_HOLDERS | {a} | {p for p in pairs if len(p) == 42}
                row.update(checks.holder_stats(rpc, a, start, head, exclude, seeds, cc))
            out.append(row)
            queue.pop(a)
            done += 1
        except SourceStopped as e:
            ERRORS.append(f"checks: stopped ({e})")
            break
        except Exception as e:  # noqa: BLE001
            queue[a]["tries"] += 1
            ERRORS.append(f"check {a[:10]}: {str(e)[:120]}")
            if queue[a]["tries"] >= cc["max_tries"]:
                out.append({"token_address": a, "checked_utc": RUN_UTC, "checks_note": f"failed: {str(e)[:80]}"})
                queue.pop(a)
    store.append(store.DATA / "checks.csv", ("token_address", "checked_utc") + checks.FIELDS, out, DRY)
    return done


# ------------------------------------------------------------------ main

def main():
    lock = store.DATA / ".lock"
    if lock.exists() and NOW - lock.stat().st_mtime < 2 * 3600 and not DRY:
        print("another run holds the lock; exiting")
        return
    store.DATA.mkdir(parents=True, exist_ok=True)
    if not DRY:
        lock.write_text(RUN_UTC)
    try:
        state = store.load_state()
        for k, v in (("seen_pools", {}), ("tracked", {}), ("below", {}), ("skip", {}), ("check_queue", {})):
            state.setdefault(k, v)
        state["seen_pools"] = {p: ts for p, ts in state["seen_pools"].items() if NOW - ts < 3 * 86400}
        state["skip"] = {a: until for a, until in state["skip"].items() if until > NOW}
        net = Net(CFG["net"])
        n_new, disc = discover(net, state)
        snap_due = "--snapshot" in sys.argv or NOW - state.get("last_snapshot", 0) >= CFG["snapshot"]["every_hours"] * 3600
        rows, cohort, seed, n_checked = [], [], None, 0
        if snap_due:
            disc += catch_up(net, state)
            rows = snapshot(net, state)
            if rows:
                cohort, seed = assign(state, rows)
                state["last_snapshot"] = NOW
        n_checked = run_checks(net, state) if state["check_queue"] else 0
        state["last_run"] = NOW
        run = {"run_utc": RUN_UTC, "kind": "snapshot" if snap_due else "discover", "duration_s": round(time.time() - NOW),
               "calls_geckoterminal": net.calls["geckoterminal"], "calls_dexscreener": net.calls["dexscreener"], "calls_rpc": net.calls["rpc"],
               "new_pools": n_new, "tokens_seen": len(disc), "tracked": len(state["tracked"]),
               "new_passers": sum(x["cohort"] == "passer" for x in cohort), "new_controls": sum(x["cohort"] == "control" for x in cohort),
               "random_seed": seed, "stopped": net.stopped_note(), "errors": " | ".join(ERRORS)[:900]}
        store.append(store.DATA / "runs.csv", store.RUNS, [run], DRY)
        store.save_state(state, DRY)
        print(f"{RUN_UTC} {run['kind']}: {n_new} new pools, {len(disc)} tokens priced ({sum(1 for x in disc if x['tracked'])} tracked), "
              f"{len(state['tracked'])} tracked in all, {len(rows)} snapshot rows, {len(cohort)} cohort rows, {n_checked} checked; "
              f"calls GT {net.calls['geckoterminal']} DS {net.calls['dexscreener']} RPC {net.calls['rpc']}; {run['duration_s']}s"
              + (f"; stopped: {run['stopped']}" if run["stopped"] else "") + (f"; errors: {len(ERRORS)}" if ERRORS else ""))
        for e in ERRORS:
            print("  -", e)
    finally:
        if lock.exists() and not DRY:
            lock.unlink()


if __name__ == "__main__":
    main()
