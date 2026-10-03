"""
Crypto Cockpit v2 collector.

Runs every hour on GitHub Actions.
  * Every run (hourly): leverage gauges, positioning across Binance/Bybit/OKX (Coinalyze),
    shortlist prices, shortlist OI history, shortlist exchange-wallet flows on
    Ethereum, BNB Chain and Solana, alerts.
  * Every `universe_every_hours` (default 4h): the full universe (top 500 coins),
    financials from DefiLlama, automatic scores, macro, stablecoins, MVRV.

Outputs (served by GitHub Pages):
  docs/universe.json  - every coin, scored (rewritten on universe runs)
  docs/data.json      - gauges, shortlist detail, flows, macro, alerts (every run)
Reads:
  docs/shortlist.json - CoinGecko ids you shortlisted from the dashboard
"""

import hashlib
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DOCS = ROOT / "docs"
DATA = ROOT / "data"
HISTORY_PATH = DATA / "history.json"
STATE_PATH = DATA / "state.json"
CACHE_PATH = DATA / "cache.json"
UNIVERSE_PATH = DOCS / "universe.json"
OUT_PATH = DOCS / "data.json"
SHORTLIST_PATH = DOCS / "shortlist.json"

NOW = datetime.now(timezone.utc)
NOW_TS = int(NOW.timestamp())
TH = CONFIG["thresholds"]
CAT_NAMES = ["Fundamentals", "Not priced in", "Value capture", "Catalyst", "Safety"]
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "crypto-cockpit/2.0"
ERRORS = {}
CG_KEY = os.getenv("COINGECKO_API_KEY", "").strip()
CG_HEADERS = {"x-cg-demo-api-key": CG_KEY} if CG_KEY else None
CZ_KEY = os.getenv("COINALYZE_API_KEY", "").strip()
BSC_RPCS = ["https://bsc-rpc.publicnode.com", "https://bsc.drpc.org"]
SOL_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WALLET_KEYS = {"eth": "exchange_wallets", "bsc": "exchange_wallets_bsc", "sol": "exchange_wallets_sol"}


# ------------------------------------------------------------------ helpers

def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def get_json(url, params=None, headers=None, method="GET", body=None, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            if method == "POST":
                r = SESSION.post(url, json=body, headers=headers, timeout=40)
            else:
                r = SESSION.get(url, params=params, headers=headers, timeout=40)
            if r.status_code == 429:
                last = "HTTP 429 rate limited"
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def module(name):
    def wrap(fn):
        def inner(*a, **kw):
            t = time.time()
            try:
                out = fn(*a, **kw)
                log(f"{name}: ok ({time.time() - t:.1f}s)")
                return out
            except Exception as e:  # noqa: BLE001
                ERRORS[name] = str(e)[:300]
                log(f"{name}: FAILED - {e}")
                return None
        return inner
    return wrap


def fnum(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1) * 100


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def series_at(series, seconds_ago, idx=1, tolerance=None):
    if not series:
        return None
    target = NOW_TS - seconds_ago
    best = min(series, key=lambda p: abs(p[0] - target))
    tol = tolerance if tolerance is not None else max(4 * 3600, seconds_ago * 0.3)
    return best[idx] if abs(best[0] - target) <= tol else None


def trim(series, days):
    cutoff = NOW_TS - days * 86400
    return [p for p in series if p[0] >= cutoff]


def r2(v, d=4):
    return None if v is None else round(v, d)


def rpc(urls, method, params):
    """JSON-RPC call to a free public node, falling back to the next node on failure."""
    last = None
    for url in urls:
        try:
            d = get_json(url, method="POST", body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         retries=1)
            if "error" in d:
                raise RuntimeError(f"{method}: {(d['error'] or {}).get('message', d['error'])}")
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(str(last))


_CZ_CALLS = []


def cz_get(path, symbols=None, **params):
    """Coinalyze GET. Free plan: 40 calls a minute, and every symbol in a request counts as one call."""
    if not CZ_KEY:
        raise RuntimeError("COINALYZE_API_KEY secret not set")
    cost = len(symbols) if symbols else 1
    while True:
        now = time.time()
        _CZ_CALLS[:] = [t for t in _CZ_CALLS if now - t < 61]
        if len(_CZ_CALLS) + cost <= 38:
            break
        time.sleep(max(0.5, 61 - (now - _CZ_CALLS[0])))
    _CZ_CALLS.extend([time.time()] * cost)
    if symbols:
        params["symbols"] = ",".join(symbols)
    return get_json(f"https://api.coinalyze.net/v1/{path}", params=params, headers={"api_key": CZ_KEY})


def wallet_map(chain):
    """address -> exchange name for one chain (EVM addresses lower-cased, Solana kept as is)."""
    out = {}
    for a, n in CONFIG.get(WALLET_KEYS[chain], {}).items():
        if a.startswith("_"):
            continue
        out[a.lower() if a.startswith("0x") else a] = n
    return out


# ------------------------------------------------------------------ sources

@module("Hyperliquid (leverage)")
def fetch_hyperliquid():
    meta, ctxs = get_json("https://api.hyperliquid.xyz/info", method="POST",
                          body={"type": "metaAndAssetCtxs"})
    out = {}
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        px, oi = fnum(ctx.get("markPx")), fnum(ctx.get("openInterest"))
        if not px or oi is None:
            continue
        out[asset["name"]] = {
            "price": px,
            "oi_usd": oi * px,
            "funding_8h_pct": fnum(ctx.get("funding"), 0) * 8 * 100,  # hourly -> 8h, in %
            "vol_24h_usd": fnum(ctx.get("dayNtlVlm"), 0),
            "chg_24h_pct": pct_change(px, fnum(ctx.get("prevDayPx"))),
        }
    if not out:
        raise RuntimeError("empty response")
    return out


def cg_markets(ids=None, pages=1, sparkline=True):
    rows = []
    for page in range(1, pages + 1):
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page,
                  "sparkline": str(sparkline).lower(),
                  "price_change_percentage": "24h,7d,30d,200d"}
        if ids:
            params["ids"] = ",".join(ids)
        rows += get_json("https://api.coingecko.com/api/v3/coins/markets", params=params, headers=CG_HEADERS)
        if ids:
            break
        time.sleep(2.5)
    return rows


def daily_volatility(hourly):
    if not hourly or len(hourly) < 48:
        return None
    daily = hourly[::24]
    rets = [(b / a - 1) * 100 for a, b in zip(daily, daily[1:]) if a]
    return statistics.pstdev(rets) if len(rets) >= 3 else None


def market_row(r):
    spark = (r.get("sparkline_in_7d") or {}).get("price") or []
    return {
        "id": r["id"], "sym": (r.get("symbol") or "").upper(), "name": r.get("name"),
        "rank": r.get("market_cap_rank"),
        "price": fnum(r.get("current_price")), "mcap": fnum(r.get("market_cap")),
        "fdv": fnum(r.get("fully_diluted_valuation")), "vol": fnum(r.get("total_volume")),
        "ath_chg": fnum(r.get("ath_change_percentage")),
        "c24": fnum(r.get("price_change_percentage_24h_in_currency")),
        "c7": fnum(r.get("price_change_percentage_7d_in_currency")),
        "c30": fnum(r.get("price_change_percentage_30d_in_currency")),
        "c200": fnum(r.get("price_change_percentage_200d_in_currency")),
        "dvol": daily_volatility(spark),
        "_spark": spark,
    }


def excluded(row):
    if row["sym"] in set(CONFIG.get("exclude_symbols", [])):
        return True
    name = f' {(row["name"] or "").lower()} '
    if any(k in name for k in CONFIG.get("exclude_name_keywords", [])):
        return True
    # stablecoin heuristic: sits at ~$1 and barely moves
    p, c30 = row["price"], row["c30"]
    if p and 0.97 <= p <= 1.03 and c30 is not None and abs(c30) < 2:
        return True
    return False


@module("CoinGecko (universe)")
def fetch_universe_markets():
    size = CONFIG.get("universe_size", 500)
    pages = math.ceil(size * 1.3 / 250)  # over-fetch to replace excluded stables/wrapped tokens
    rows = [market_row(r) for r in cg_markets(pages=pages)]
    rows = [r for r in rows if r["mcap"] and not excluded(r)]
    rows.sort(key=lambda r: r["mcap"], reverse=True)
    return rows[:size]


@module("CoinGecko (shortlist)")
def fetch_shortlist_markets(ids):
    if not ids:
        return {}
    return {r["id"]: market_row(r) for r in cg_markets(ids=ids)}


@module("CoinGecko (global)")
def fetch_global():
    g = get_json("https://api.coingecko.com/api/v3/global", headers=CG_HEADERS)["data"]
    return {"btc_dom": fnum(g["market_cap_percentage"].get("btc")),
            "eth_dom": fnum(g["market_cap_percentage"].get("eth")),
            "total_mcap": fnum(g["total_market_cap"].get("usd"))}


@module("DefiLlama (financials)")
def fetch_financials():
    """Revenue, holders revenue and TVL for every protocol and chain, keyed by CoinGecko id."""
    base = "https://api.llama.fi/overview/fees"
    params = {"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}
    rev = get_json(base, params={**params, "dataType": "dailyRevenue"})["protocols"]
    time.sleep(1)
    hold = get_json(base, params={**params, "dataType": "dailyHoldersRevenue"})["protocols"]
    time.sleep(1)
    protocols = get_json("https://api.llama.fi/protocols")
    time.sleep(1)
    chains = get_json("https://api.llama.fi/v2/chains")
    time.sleep(1)
    # versions ("Uniswap V3") carry no gecko_id; their parent ("parent#uniswap") does
    parents = get_json("https://api.llama.fi/lite/protocols2").get("parentProtocols") or []

    by_id, tvl = {}, {}
    by_parent = {pp["id"]: pp["gecko_id"] for pp in parents if pp.get("id") and pp.get("gecko_id")}
    for p in protocols:
        g = p.get("gecko_id")
        if p.get("id") and g:
            by_id[str(p["id"])] = g
        if p.get("parentProtocol") and g:
            by_parent.setdefault(p["parentProtocol"], g)
    for p in protocols:
        g = p.get("gecko_id") or by_parent.get(p.get("parentProtocol"))
        if g:
            tvl[g] = tvl.get(g, 0) + (fnum(p.get("tvl"), 0) or 0)
    chain_gecko = {}
    for c in chains:
        if c.get("gecko_id"):
            chain_gecko[(c.get("name") or "").lower()] = c["gecko_id"]
            tvl[c["gecko_id"]] = max(tvl.get(c["gecko_id"], 0), fnum(c.get("tvl"), 0) or 0)

    def gecko_for(p):
        g = p.get("gecko_id")
        if g:
            return g
        if p.get("parentProtocol") in by_parent:
            return by_parent[p["parentProtocol"]]
        did = str(p.get("defillamaId") or p.get("id") or "")
        if did in by_id:
            return by_id[did]
        if (p.get("protocolType") == "chain" or p.get("category") == "Chain"):
            return chain_gecko.get((p.get("name") or "").lower())
        return None

    fin = {}
    for p in rev:
        g = gecko_for(p)
        if g:
            f = fin.setdefault(g, {"rev30": 0.0, "hold30": 0.0})
            f["rev30"] += fnum(p.get("total30d"), 0) or 0
    for p in hold:
        g = gecko_for(p)
        if g and g in fin:
            fin[g]["hold30"] += fnum(p.get("total30d"), 0) or 0
    out = {}
    for g in set(fin) | set(tvl):
        f = fin.get(g, {})
        rev30 = f.get("rev30") or 0
        out[g] = {
            "rev": rev30 * 12 if rev30 > 0 else None,
            "hold_share": (f["hold30"] / rev30 * 100) if rev30 > 0 else None,
            "tvl": tvl.get(g) or None,
        }
    return out


@module("DefiLlama (stablecoins)")
def fetch_stablecoins():
    pts = get_json("https://stablecoins.llama.fi/stablecoincharts/all")
    series = sorted([int(p["date"]), sum(fnum(v, 0) or 0 for v in (p.get("totalCirculatingUSD") or {}).values())]
                    for p in pts)
    series = series[-120:]
    last = series[-1][1]
    return {"total": last,
            "chg_7d": pct_change(last, series[-8][1]) if len(series) > 8 else None,
            "chg_30d": pct_change(last, series[-31][1]) if len(series) > 31 else None,
            "series": [round(p[1] / 1e9, 2) for p in series[-90:]]}


@module("FRED (macro)")
def fetch_fred():
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FRED_API_KEY secret not set")
    out = {}
    for sid, label in (("DGS10", "US 10-year yield"), ("DFII10", "US 10-year real yield"),
                       ("DTWEXBGS", "Dollar index (broad)")):
        d = get_json("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 120})
        obs = [(o["date"], fnum(o["value"])) for o in d["observations"]]
        obs = [(dt, v) for dt, v in obs if v is not None][::-1]
        if not obs:
            continue
        latest = obs[-1][1]
        month_ago = obs[-22][1] if len(obs) >= 22 else obs[0][1]
        out[sid] = {"label": label, "latest": latest, "date": obs[-1][0], "chg_30d": latest - month_ago,
                    "series": [v for _, v in obs[-90:]]}
        time.sleep(0.5)
    return out


@module("CoinMetrics (BTC cycle)")
def fetch_mvrv():
    start = (NOW - timedelta(days=400)).strftime("%Y-%m-%d")
    d = get_json("https://community-api.coinmetrics.io/v4/timeseries/asset-metrics", params={
        "assets": "btc", "metrics": "CapMVRVCur,PriceUSD", "frequency": "1d",
        "start_time": start, "page_size": 1000})
    rows = [(r["time"][:10], fnum(r.get("CapMVRVCur")), fnum(r.get("PriceUSD"))) for r in d.get("data", [])]
    rows = [r for r in rows if r[1] and r[2]]
    if not rows:
        raise RuntimeError("no MVRV rows")
    date, mvrv, price = rows[-1]
    return {"date": date, "mvrv": mvrv, "price": price, "realized_price": price / mvrv,
            "series": [round(r[1], 3) for r in rows[-365:]][::3]}


NATIVE = {"ethereum": "eth", "binancecoin": "bsc", "solana": "sol"}
PLATFORMS = {"ethereum": "eth", "binance-smart-chain": "bsc", "solana": "sol"}


@module("CoinGecko (contract lookup)")
def refresh_contracts(ids, cache):
    """Pick the chain to watch for each shortlisted coin: its home chain if we track it (Ethereum,
    BNB Chain, Solana), else any of those it is issued on. Cached 7 days, max 12 lookups per run."""
    contracts = cache.setdefault("contracts", {})
    todo = [i for i in ids if contracts.get(i, {}).get("v") != 2
            or NOW_TS - contracts[i].get("ts", 0) > 7 * 86400][:12]
    for cid in todo:
        if cid in NATIVE:
            contracts[cid] = {"v": 2, "ts": NOW_TS, "track": {"chain": NATIVE[cid], "kind": "native"}}
            continue
        try:
            d = get_json(f"https://api.coingecko.com/api/v3/coins/{cid}", params={
                "localization": "false", "tickers": "false", "market_data": "false",
                "community_data": "false", "developer_data": "false", "sparkline": "false"},
                headers=CG_HEADERS, retries=1)
            plats = {k: (v or "").strip() for k, v in (d.get("platforms") or {}).items()}
            plat = next((p for p in [d.get("asset_platform_id"), *PLATFORMS] if p in PLATFORMS and plats.get(p)), None)
            track = None
            if plat:
                addr = plats[plat] if plat == "solana" else plats[plat].lower()  # Solana mints are case-sensitive
                dec = ((d.get("detail_platforms") or {}).get(plat) or {}).get("decimal_place")
                track = {"chain": PLATFORMS[plat], "kind": "token", "addr": addr, "dec": dec}
            contracts[cid] = {"v": 2, "ts": NOW_TS, "track": track}
        except Exception:  # noqa: BLE001
            contracts[cid] = {"v": 2, "ts": NOW_TS - 6 * 86400, "track": None}  # retry in a day
        time.sleep(2.5)
    return contracts


def transfer_event(eid, ts, cid, sym, frm, to, wallets, amount, price, tx, chain):
    direction = "in" if to in wallets else "out"
    return {"id": eid, "ts": ts, "cid": cid, "sym": sym, "dir": direction, "chain": chain,
            "exchange": wallets[to] if direction == "in" else wallets[frm],
            "amount": amount, "usd": amount * price, "hash": tx}


@module("Etherscan (exchange flows)")
def fetch_eth_transfers(targets, prices, seen, history):
    """Token transfers into and out of known Ethereum exchange wallets (last 100 per wallet and token)."""
    if not targets:
        return []
    key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ETHERSCAN_API_KEY secret not set")
    wallets = wallet_map("eth")
    cutoff = NOW_TS - 8 * 86400
    new, calls, fails = [], 0, []
    for cid, track in targets.items():
        price = (prices.get(cid) or {}).get("price")
        sym = (prices.get(cid) or {}).get("sym") or cid.upper()
        if not price:
            continue
        for wallet in wallets:
            calls += 1
            try:
                d = get_json("https://api.etherscan.io/v2/api", params={
                    "chainid": 1, "module": "account", "action": "tokentx", "contractaddress": track["addr"],
                    "address": wallet, "page": 1, "offset": 100, "sort": "desc", "apikey": key}, retries=1)
            except Exception as e:  # noqa: BLE001
                fails.append(str(e))
                continue
            time.sleep(0.22)  # free tier: 5 calls/sec
            result = d.get("result")
            if not isinstance(result, list):
                if d.get("message") != "No transactions found":
                    fails.append(str(result)[:120])
                continue
            for t in result:
                ts = int(t["timeStamp"])
                if ts < cutoff:
                    break
                frm, to = t["from"].lower(), t["to"].lower()
                if frm in wallets and to in wallets:
                    continue
                eid = f'{t["hash"]}:{frm}:{to}:{t["value"]}'
                if eid in seen:
                    continue
                seen.add(eid)
                amount = int(t["value"]) / 10 ** int(t.get("tokenDecimal") or 18)
                new.append(transfer_event(eid, ts, cid, sym, frm, to, wallets, amount, price, t["hash"], "eth"))
    if calls and len(fails) == calls:
        raise RuntimeError(f"every call failed: {fails[0]}")
    return new


@module("BNB Chain (exchange flows)")
def fetch_bsc_transfers(targets, prices, seen, history):
    """BEP-20 transfers into and out of known BNB Chain exchange wallets, read from a free public node
    (Etherscan's free plan no longer covers BNB Chain). Scans the blocks since the last run, at most 3 hours."""
    cursors = history.setdefault("bsc_cursor", {})
    for cid in list(cursors):
        if cid not in targets:
            del cursors[cid]
    if not targets:
        return []
    wallets = wallet_map("bsc")
    wtopics = ["0x" + "0" * 24 + a[2:] for a in wallets]
    head = int(rpc(BSC_RPCS, "eth_blockNumber", []), 16)
    t_head = int(rpc(BSC_RPCS, "eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
    t_old = int(rpc(BSC_RPCS, "eth_getBlockByNumber", [hex(head - 20000), False])["timestamp"], 16)
    spb = max((t_head - t_old) / 20000, 0.05)  # seconds per block
    new = []
    for cid, track in targets.items():
        price = (prices.get(cid) or {}).get("price")
        sym = (prices.get(cid) or {}).get("sym") or cid.upper()
        if not price:
            continue
        dec = int(track.get("dec") or 18)
        frm = max(cursors.get(cid, head - int(3600 / spb)) + 1, head - int(3 * 3600 / spb))
        while frm <= head:
            to = min(frm + 2999, head)
            for topics in ([TRANSFER_TOPIC, None, wtopics], [TRANSFER_TOPIC, wtopics]):  # into, out of
                logs = rpc(BSC_RPCS, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                      "address": track["addr"], "topics": topics}])
                for lg in logs:
                    if len(lg.get("topics") or []) < 3:
                        continue
                    src, dst = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
                    if src in wallets and dst in wallets:
                        continue
                    eid = f'{lg["transactionHash"]}:{int(lg["logIndex"], 16)}'
                    if eid in seen:
                        continue
                    seen.add(eid)
                    amount = int(lg["data"], 16) / 10 ** dec if lg.get("data") not in (None, "0x") else 0
                    ts = int(t_head - (head - int(lg["blockNumber"], 16)) * spb)
                    new.append(transfer_event(eid, ts, cid, sym, src, dst, wallets, amount, price,
                                              lg["transactionHash"], "bsc"))
            frm = to + 1
        cursors[cid] = head
    return new


def sol_token_balance(owner, mint):
    r = rpc(SOL_RPCS, "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    return sum(fnum(a["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmountString"), 0) or 0
               for a in r["value"])


@module("Exchange balances")
def fetch_exchange_balances(targets, history):
    """Hourly snapshot of what known exchange wallets hold, for native coins (ETH, BNB, SOL) and Solana
    tokens, where transfer-by-transfer tracking isn't possible for free. Net flow = change in holdings.
    A coin is skipped for the hour if any wallet fails, so a gap never looks like an outflow."""
    res = history.setdefault("reserves", {})
    for cid in list(res):
        if cid not in targets:
            del res[cid]
    failed = []
    for cid, track in targets.items():
        chain = track["chain"]
        wallets = wallet_map(chain)
        if chain == "eth":
            wallets = dict(list(wallets.items())[:20])  # balancemulti takes up to 20 addresses
        per = {}
        try:
            if chain == "eth":
                key = os.getenv("ETHERSCAN_API_KEY", "").strip()
                if not key:
                    raise RuntimeError("ETHERSCAN_API_KEY secret not set")
                d = get_json("https://api.etherscan.io/v2/api", params={
                    "chainid": 1, "module": "account", "action": "balancemulti", "address": ",".join(wallets),
                    "tag": "latest", "apikey": key})
                if not isinstance(d.get("result"), list) or len(d["result"]) != len(wallets):
                    raise RuntimeError(str(d.get("result"))[:120])
                for r in d["result"]:
                    n = wallets[r["account"].lower()]
                    per[n] = per.get(n, 0) + int(r["balance"]) / 1e18
            elif chain == "bsc":
                for a, n in wallets.items():
                    per[n] = per.get(n, 0) + int(rpc(BSC_RPCS, "eth_getBalance", [a, "latest"]), 16) / 1e18
            elif track["kind"] == "native":
                r = rpc(SOL_RPCS, "getMultipleAccounts", [list(wallets), {"encoding": "base64",
                                                                         "dataSlice": {"offset": 0, "length": 0}}])
                for (a, n), v in zip(wallets.items(), r["value"]):
                    per[n] = per.get(n, 0) + (v or {}).get("lamports", 0) / 1e9
            else:
                for a, n in wallets.items():
                    per[n] = per.get(n, 0) + sol_token_balance(a, track["addr"])
                    time.sleep(0.25)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{cid}: {str(e)[:120]}")
            continue
        sig = hashlib.sha1(f'{track.get("addr", "native")}|{",".join(sorted(wallets))}'.encode()).hexdigest()[:12]
        r = res.get(cid)
        if not r or r.get("sig") != sig:  # wallet list changed: restart the series
            r = {"sig": sig, "pts": []}
        r["pts"] = trim(r["pts"] + [[NOW_TS, {k: round(v, 4) for k, v in per.items()}]], 8)
        res[cid] = r
    if failed:
        raise RuntimeError("; ".join(failed)[:300])
    return res


def reserve_flows(r, price):
    """Turn hourly balance snapshots into the same in/out shape as transfer flows."""
    pts = (r or {}).get("pts") or []
    if not pts or not price:
        return None
    tot = [(t, sum(v.values())) for t, v in pts]
    out = {"in_24h": 0.0, "out_24h": 0.0, "in_7d": 0.0, "out_7d": 0.0, "method": "balance", "since": tot[0][0],
           "balance": tot[-1][1], "balance_usd": tot[-1][1] * price,
           "by_exchange": {k: round(v * price) for k, v in sorted(pts[-1][1].items(), key=lambda kv: -kv[1])
                           if v * price >= 1e5}}
    for (_, a), (t1, b) in zip(tot, tot[1:]):
        d = (b - a) * price
        side = "in" if d > 0 else "out"
        if NOW_TS - t1 <= 7 * 86400:
            out[f"{side}_7d"] += abs(d)
        if NOW_TS - t1 <= 86400:
            out[f"{side}_24h"] += abs(d)
    return out


# ------------------------------------------------------------------ positioning (Coinalyze)

def cz_markets(cache):
    """Perpetual markets on Binance, Bybit and OKX, by base asset. Cached for 3 days."""
    m = cache.get("cz")
    if m and NOW_TS - m.get("ts", 0) < 3 * 86400:
        return m["m"]
    codes = {e["code"]: e["name"] for e in cz_get("exchanges")
             if (e.get("name") or "").lower().startswith(("binance", "bybit", "okx"))}
    quotes = ("USDT", "USDC", "USD")
    best = {}
    for f in cz_get("future-markets"):
        if not f.get("is_perpetual") or f.get("margined") != "STABLE" or f.get("exchange") not in codes \
                or f.get("quote_asset") not in quotes:
            continue
        k, rank = ((f.get("base_asset") or "").upper(), f["exchange"]), quotes.index(f["quote_asset"])
        if k not in best or rank < best[k][0]:
            best[k] = (rank, [f["symbol"], codes[f["exchange"]], bool(f.get("has_long_short_ratio_data")),
                              bool(f.get("has_buy_sell_data"))])
    out = {}
    for (base, _), (_, row) in best.items():
        out.setdefault(base, []).append(row)
    if not out:
        raise RuntimeError("no Binance/Bybit/OKX perps listed")
    cache["cz"] = {"ts": NOW_TS, "m": out}
    return out


def cz_history(path, symbols, seconds, **extra):
    out = {}
    for i in range(0, len(symbols), 20):
        rows = cz_get(path, symbols[i:i + 20], interval="1hour", **{"from": NOW_TS - seconds, "to": NOW_TS}, **extra)
        for row in rows:
            out[row["symbol"]] = row.get("history") or []
    return out


def pct_val(v):
    v = fnum(v)
    return None if v is None else v * 100 if v <= 1 else v  # Coinalyze longs % may come as 0.62 or 62


@module("Coinalyze (positioning)")
def fetch_positioning(targets, cache):
    """Open interest, liquidations, long/short account ratio and taker buy share, summed or averaged
    across Binance, Bybit and OKX. targets: key -> (ticker, include taker volume)."""
    mk = cz_markets(cache)
    sets = {}
    for key, (sym, _) in targets.items():
        rows = mk.get(sym.upper()) or mk.get("1000" + sym.upper())
        if rows:
            sets[key] = rows
    if not sets:
        raise RuntimeError("no matching perp markets")
    every = sorted({r[0] for rows in sets.values() for r in rows})
    oi = cz_history("open-interest-history", every, 8 * 86400, convert_to_usd="true")
    liq = cz_history("liquidation-history", every, 25 * 3600, convert_to_usd="true")
    ls = cz_history("long-short-ratio-history", sorted({r[0] for rows in sets.values() for r in rows if r[2]}), 25 * 3600)
    tk = sorted({r[0] for k, rows in sets.items() if targets[k][1] for r in rows if r[3]})
    ohlcv = cz_history("ohlcv-history", tk, 25 * 3600) if tk else {}
    day = NOW_TS - 86400
    out = {}
    for key, rows in sets.items():
        syms = [r[0] for r in rows]
        # open interest summed across exchanges, at the hours every exchange reported
        maps = [{p["t"]: fnum(p.get("c")) for p in oi[s]} for s in syms if oi.get(s)]
        series = []
        if maps:
            common = set(maps[0]).intersection(*maps[1:])
            series = [[t, sum(m[t] for m in maps)] for t in sorted(common) if all(m[t] is not None for m in maps)]
        recent = [v for t, v in series if t >= NOW_TS - 7 * 86400]
        longs_now, longs_then, buy = [], [], []
        for s in syms:
            h = ls.get(s) or []
            if h:
                longs_now.append(pct_val(h[-1].get("l")))
                longs_then.append(pct_val(min(h, key=lambda p: abs(p["t"] - day)).get("l")))
            c = [p for p in ohlcv.get(s, []) if p["t"] >= day]
            v = sum(fnum(p.get("v"), 0) or 0 for p in c)
            if v:
                buy.append(sum(fnum(p.get("bv"), 0) or 0 for p in c) / v * 100)
        mean = lambda xs: (sum(x for x in xs if x is not None) / len([x for x in xs if x is not None])  # noqa: E731
                           if any(x is not None for x in xs) else None)
        oi_now = series[-1][1] if series else None
        out[key] = {
            "oi_usd": oi_now, "oi_series": series,
            "oi_chg_24h": pct_change(oi_now, series_at(series, 86400, tolerance=3 * 3600)),
            "oi_chg_7d": pct_change(oi_now, series_at(series, 7 * 86400, tolerance=6 * 3600)),
            "oi_off_7d_max": pct_change(oi_now, max(recent)) if recent else None,
            "liq_long_24h": sum(fnum(p.get("l"), 0) or 0 for s in syms for p in liq.get(s, []) if p["t"] >= day),
            "liq_short_24h": sum(fnum(p.get("s"), 0) or 0 for s in syms for p in liq.get(s, []) if p["t"] >= day),
            "long_pct": mean(longs_now), "long_pct_24h": mean(longs_then),
            "taker_buy_pct": mean(buy),
            "exchanges": sorted({r[1] for r in rows}),
        }
    return out


def deriv_summary(d):
    if not d:
        return None
    return {k: (r2(v, 2) if isinstance(v, float) else v) for k, v in d.items() if k != "oi_series"}


# ------------------------------------------------------------------ mapping

def map_perps(coins, hl):
    """CoinGecko id -> Hyperliquid perp name. Matches ticker (incl. 1000x 'k' perps) and checks price."""
    out, taken = {}, set()
    for c in sorted(coins, key=lambda c: -(c.get("mcap") or 0)):
        for name, mult in ((c["sym"], 1), ("k" + c["sym"], 1000)):
            h = hl.get(name)
            if not h or name in taken or not c.get("price"):
                continue
            if abs(h["price"] / mult / c["price"] - 1) <= 0.15:
                out[c["id"]] = name
                taken.add(name)
                break
    return out


# ------------------------------------------------------------------ scoring

def auto_scores(c, fin, btc):
    """Five report-card categories, 1-10. None = no data for that category."""
    rev = fin.get("rev") if fin else None
    tvl = fin.get("tvl") if fin else None
    mcap = c.get("mcap") or 0
    mcap_rev = mcap / rev if rev else None

    # Fundamentals: real revenue first, TVL as a weaker signal
    if rev:
        F = 10 if rev > 500e6 else 9 if rev > 100e6 else 8 if rev > 50e6 else 7 if rev > 20e6 \
            else 6 if rev > 5e6 else 5 if rev > 1e6 else 4
    elif tvl and tvl > 50e6:
        F = 6 if tvl > 1e9 else 5 if tvl > 200e6 else 4
    else:
        F = None

    # Not priced in: distance from ATH, minus recent pumps, adjusted by valuation
    NPI = None
    if c.get("ath_chg") is not None:
        n = clamp(-c["ath_chg"] / 10, 1, 10)
        c30 = c.get("c30")
        if c30 is not None:
            n -= 3 if c30 > 50 else 2 if c30 > 25 else 1 if c30 > 10 else 0
        if mcap_rev is not None:
            n += 1 if mcap_rev < 15 else -1 if mcap_rev > 200 else 0
        NPI = round(clamp(n, 1, 10))

    # Value capture: share of revenue that reaches token holders, minus dilution
    VC = None
    if rev:
        hs = fin.get("hold_share") or 0
        VC = 9 if hs >= 75 else 8 if hs >= 50 else 7 if hs >= 25 else 6 if hs > 0 else 3
        ratio = (c.get("fdv") or mcap) / mcap if mcap else 1
        VC -= 2 if ratio > 2.5 else 1 if ratio > 1.5 else 0
        VC = clamp(VC, 1, 10)

    # Catalyst (automatic proxy): relative strength vs BTC, 30d and 200d
    CAT = None
    if c.get("c30") is not None and btc.get("c30") is not None:
        rs30 = c["c30"] - btc["c30"]
        rs200 = (c.get("c200") or 0) - (btc.get("c200") or 0)
        CAT = round(clamp(5 + clamp(rs30 / 10, -3, 3) + clamp(rs200 / 40, -2, 2), 1, 10))

    # Safety: size, volatility, dilution
    s = 8 if mcap > 50e9 else 7 if mcap > 10e9 else 6 if mcap > 2e9 else 5 if mcap > 500e6 else 4 if mcap > 100e6 else 3
    if c.get("dvol") is not None:
        s -= 2 if c["dvol"] > 6 else 1 if c["dvol"] > 4 else 0
    if c.get("fdv") and mcap and c["fdv"] / mcap > 2.5:
        s -= 1
    SAFE = round(clamp(s, 1, 10))

    return [F, NPI, VC, CAT, SAFE], mcap_rev


def score_coin(c, fin, btc):
    vals, mcap_rev = auto_scores(c, fin, btc)
    src = ["auto" if v is not None else "none" for v in vals]
    ov = CONFIG["overrides"].get(c["id"])
    if ov:
        for i, k in ((0, "F"), (2, "VC"), (3, "CAT")):
            if ov.get(k) is not None:
                vals[i], src[i] = ov[k], "manual"
    filled = [v if v is not None else 3 for v in vals]  # missing data counts as 3 ("unproven")
    return {
        "score": vals, "src": src, "total": sum(filled),
        "chat": ov.get("chat") if ov else None,
        "no_fin": not (fin and (fin.get("rev") or fin.get("tvl"))),
        "mcap_rev": mcap_rev,
    }


def checklist(c, perp, oi_hist, flows, has_flow_tracking, agg=None):
    items = []
    if perp:
        f = perp["funding_8h_pct"]
        items.append([f <= TH["funding_neutral_8h_pct"], f"Funding {f:+.4f}% per 8h"])
    else:
        items.append([None, "No perp market"])

    if agg and agg.get("oi_off_7d_max") is not None and len(agg.get("oi_series") or []) > 72:
        off = agg["oi_off_7d_max"]
        items.append([off <= -TH["flush_oi_drop_pct"], f"OI {off:+.1f}% vs 7-day high, all exchanges"])
    elif perp and oi_hist and series_at(oi_hist, 3 * 86400, tolerance=36 * 3600) is not None:
        recent = [p[1] for p in oi_hist if p[0] >= NOW_TS - 7 * 86400] + [perp["oi_usd"]]
        off = pct_change(perp["oi_usd"], max(recent))
        items.append([off <= -TH["flush_oi_drop_pct"], f"OI {off:+.1f}% vs 7-day high"])
    else:
        items.append([None, "Building OI history"])

    if has_flow_tracking and flows and flows.get("method") == "balance" and NOW_TS - flows["since"] < 86400:
        items.append([None, "Building exchange-balance history"])
    elif has_flow_tracking and flows and (flows["in_7d"] or flows["out_7d"]):
        net = flows["in_7d"] - flows["out_7d"]
        if flows.get("method") == "balance":
            items.append([net < 0, f"Exchange balances {'up' if net > 0 else 'down'} ${abs(net) / 1e6:.1f}M over 7d"])
        else:
            items.append([net < 0, f"Net {'inflow' if net > 0 else 'outflow'} ${abs(net) / 1e6:.1f}M over 7d"])
    else:
        items.append([None, "Not tracked on this coin's chain" if not has_flow_tracking else "No large moves"])

    soon = [u for u in CONFIG.get("unlocks", []) if u["sym"].upper() == c["sym"]
            and 0 <= (datetime.fromisoformat(u["date"]).replace(tzinfo=timezone.utc) - NOW).days
            <= TH["unlock_window_days"]]
    items.append([False, f'Unlock {soon[0]["date"]}: {soon[0]["note"]}'] if soon else [True, "None listed"])

    if c.get("c7") is not None:
        items.append([c["c7"] < TH["chase_7d_pct"], f"{c['c7']:+.1f}% in 7 days"])
    else:
        items.append([None, "No price data"])

    known = [i for i in items if i[0] is not None]
    passed = sum(1 for i in known if i[0])
    light = "grey" if len(known) < 3 else "green" if passed >= 4 else "amber" if passed == 3 else "red"
    return {"light": light, "passed": passed, "known": len(known), "items": items}


# ------------------------------------------------------------------ leverage gauges

def pressure(entry, hist, agg=None):
    funding, oi, vol = entry["funding_8h_pct"], entry["oi_usd"], entry["vol_24h_usd"] or 0
    oi_24h, oi_7d = series_at(hist, 86400), series_at(hist, 7 * 86400)
    oi_max = max((p[1] for p in hist if p[0] >= NOW_TS - 7 * 86400), default=None)
    px_7d = series_at(hist, 7 * 86400, idx=2)
    oi_chg_24h, oi_chg_7d = pct_change(oi, oi_24h), pct_change(oi, oi_7d)
    px_chg_7d, off_max = pct_change(entry["price"], px_7d), pct_change(oi, oi_max)
    oi_shown, warming = oi, oi_7d is None
    if agg and agg.get("oi_usd"):
        # open-interest changes from Binance + Bybit + OKX: a wider view than Hyperliquid alone, with
        # 8 days of history from day one. Same rules; OI vs volume stays on Hyperliquid's own numbers.
        oi_shown, oi_chg_24h, oi_chg_7d, off_max = agg["oi_usd"], agg["oi_chg_24h"], agg["oi_chg_7d"], agg["oi_off_7d_max"]
        warming = oi_chg_7d is None

    score = clamp(funding / 0.05, 0, 1) * 45
    if oi_chg_7d is not None:
        score += clamp(oi_chg_7d / 30, 0, 1) * 30
        if px_chg_7d is not None and abs(px_chg_7d) < 5 and oi_chg_7d > 8:
            score += 10
    if vol:
        score += clamp((oi / vol - 0.5) / 2, 0, 1) * 15
    score = round(clamp(score, 0, 100))

    flushed = off_max is not None and off_max <= -TH["flush_oi_drop_pct"] and funding <= TH["funding_neutral_8h_pct"]
    if flushed:
        state = "Leverage cleared"
    elif funding < -0.005:
        state = "Shorts crowded"
    elif funding >= TH["funding_hot_8h_pct"]:
        state = "Longs crowded"
    elif score >= 65:
        state = "Flush risk high"
    elif score >= 35:
        state = "Leverage building"
    else:
        state = "Calm"
    return {"pressure": score, "state": state, "flushed": flushed, "funding_8h_pct": round(funding, 4),
            "oi_usd": oi_shown, "oi_chg_24h": oi_chg_24h, "oi_chg_7d": oi_chg_7d, "oi_off_7d_max": off_max,
            "price": entry["price"], "chg_24h": entry.get("chg_24h_pct"), "warming_up": warming,
            "deriv": deriv_summary(agg)}


def build_markets(hl, history, deriv=None):
    hist = history.setdefault("markets", {})
    entries = {m: hl[m] for m in CONFIG["markets"] if m in hl}
    alts = [v for k, v in hl.items() if k not in CONFIG["markets"]]
    tot = sum(a["oi_usd"] for a in alts)
    if tot:
        w = lambda key: sum((a[key] or 0) * a["oi_usd"] for a in alts) / tot  # noqa: E731
        prev = hist.get("ALTS", [])
        last_idx = prev[-1][2] if prev else 100.0
        last_ts = prev[-1][0] if prev else NOW_TS
        step = w("chg_24h_pct") * clamp((NOW_TS - last_ts) / 86400, 0, 1)
        entries["ALTS"] = {"price": last_idx * (1 + step / 100), "oi_usd": tot,
                           "funding_8h_pct": w("funding_8h_pct"),
                           "vol_24h_usd": sum(a["vol_24h_usd"] for a in alts), "chg_24h_pct": w("chg_24h_pct")}
    out = {}
    for name, e in entries.items():
        s = hist.setdefault(name, [])
        s.append([NOW_TS, e["oi_usd"], e["price"], e["funding_8h_pct"]])
        hist[name] = trim(s, 45)
        out[name] = pressure(e, hist[name], (deriv or {}).get(name))
    return out


def flow_summary(events):
    out = {}
    for e in events:
        s = out.setdefault(e["cid"], {"in_24h": 0, "out_24h": 0, "in_7d": 0, "out_7d": 0})
        age = NOW_TS - e["ts"]
        if age <= 7 * 86400:
            s[f'{e["dir"]}_7d'] += e["usd"]
        if age <= 86400:
            s[f'{e["dir"]}_24h'] += e["usd"]
    return out


# ------------------------------------------------------------------ alerts

def send_alerts(alerts, state):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    sent = state.setdefault("sent", {})
    recent = state.setdefault("recent", [])
    for a in alerts:
        if NOW_TS - sent.get(a["key"], 0) < TH["alert_cooldown_hours"] * 3600:
            continue
        sent[a["key"]] = NOW_TS
        recent.insert(0, {"ts": NOW_TS, "title": a["title"], "body": a["body"], "level": a["level"]})
        if not topic:
            continue
        try:
            SESSION.post(f"{server}/{topic}", data=a["body"].encode("utf-8"), timeout=15, headers={
                "Title": a["title"].encode("ascii", "ignore").decode(),
                "Priority": "high" if a["level"] == "hot" else "default",
                "Tags": {"hot": "rotating_light", "good": "white_check_mark"}.get(a["level"], "bell")})
        except Exception as e:  # noqa: BLE001
            log(f"alert failed: {e}")
    state["recent"] = recent[:40]
    state["sent"] = {k: v for k, v in sent.items() if NOW_TS - v < 7 * 86400}


def collect_alerts(markets, shortlist_out, events, mvrv, state):
    alerts = []
    names = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "ALTS": "Alts"}
    for m, d in markets.items():
        n = names.get(m, m)
        if d["funding_8h_pct"] >= TH["funding_hot_8h_pct"]:
            alerts.append({"key": f"fund:{m}", "level": "hot", "title": f"{n}: longs crowded",
                           "body": f"Funding {d['funding_8h_pct']:+.3f}%/8h. A long flush is likely. Don't chase."})
        if d["oi_chg_24h"] is not None and d["oi_chg_24h"] >= TH["buildup_oi_rise_pct"] and abs(d["chg_24h"] or 0) < 3:
            alerts.append({"key": f"build:{m}", "level": "hot", "title": f"{n}: leverage piling in",
                           "body": f"Open interest {d['oi_chg_24h']:+.0f}% in 24h while price is flat. Flush risk rising."})
        if d["flushed"]:
            alerts.append({"key": f"flush:{m}", "level": "good", "title": f"{n}: leverage flushed",
                           "body": f"OI {d['oi_off_7d_max']:+.0f}% off its 7-day high and funding reset. Check entry lights."})
        dv = d.get("deriv") or {}
        if dv.get("oi_usd"):
            bar = TH.get("liq_alert_pct_of_oi", 1.5) / 100 * dv["oi_usd"]
            if (dv.get("liq_long_24h") or 0) >= bar:
                alerts.append({"key": f"liqL:{m}", "level": "good", "title": f"{n}: ${dv['liq_long_24h'] / 1e6:,.0f}M of longs liquidated",
                               "body": "Forced selling in the last 24h (Binance, Bybit, OKX). Long flushes like this often "
                                       "mark a local low. Check entry lights."})
            if (dv.get("liq_short_24h") or 0) >= bar:
                alerts.append({"key": f"liqS:{m}", "level": "hot", "title": f"{n}: ${dv['liq_short_24h'] / 1e6:,.0f}M of shorts liquidated",
                               "body": "A short squeeze in the last 24h pushed price up on forced buying. Don't chase it."})
    prev = state.get("lights", {})
    for c in shortlist_out:
        if c["light"] == "green" and prev.get(c["id"]) != "green":
            alerts.append({"key": f"green:{c['id']}", "level": "good", "title": f"{c['sym']}: entry light green",
                           "body": f"{c['passed']}/{c['known']} checks pass. Score {c['total']}/50."})
    state["lights"] = {c["id"]: c["light"] for c in shortlist_out}
    for e in events or []:
        if e["ts"] >= NOW_TS - 3 * 3600 and e["usd"] >= TH["whale_alert_usd"]:
            verb = "into" if e["dir"] == "in" else "out of"
            alerts.append({"key": f"whale:{e['id']}", "level": "hot" if e["dir"] == "in" else "good",
                           "title": f"{e['sym']}: ${e['usd'] / 1e6:.1f}M moved {verb} {e['exchange']}",
                           "body": f"{e['amount']:,.0f} {e['sym']} {verb} {e['exchange']}. "
                                   + ("Possible sell pressure." if e["dir"] == "in" else "Withdrawal: usually holding.")})
    if mvrv and mvrv["mvrv"] < 1:
        alerts.append({"key": "mvrv<1", "level": "good", "title": "BTC MVRV below 1",
                       "body": f"MVRV {mvrv['mvrv']:.2f}. Price is under the average holder's cost, a classic bear-bottom zone."})
    return alerts


# ------------------------------------------------------------------ assembly

def coin_record(c, fin, btc, perp_name, hl, oi_hist, flows, tracked, agg=None):
    sc = score_coin(c, fin, btc)
    perp = hl.get(perp_name) if perp_name else None
    oi7 = None
    if perp and oi_hist:
        oi7 = pct_change(perp["oi_usd"], series_at(oi_hist, 7 * 86400, tolerance=36 * 3600))
    chk = checklist(c, perp, oi_hist, flows, tracked, agg)
    return {
        "id": c["id"], "sym": c["sym"], "name": c["name"], "rank": c.get("rank"),
        "price": c["price"], "mcap": c["mcap"], "fdv": c.get("fdv"), "vol": c.get("vol"),
        "ath_chg": r2(c.get("ath_chg"), 1), "c24": r2(c.get("c24"), 2), "c7": r2(c.get("c7"), 2),
        "c30": r2(c.get("c30"), 2), "c200": r2(c.get("c200"), 1),
        "fin": {"rev": fin.get("rev"), "hold_share": r2(fin.get("hold_share"), 1), "tvl": fin.get("tvl"),
                "mcap_rev": r2(sc["mcap_rev"], 2)} if fin and (fin.get("rev") or fin.get("tvl")) else None,
        "perp": {"name": perp_name, "funding": round(perp["funding_8h_pct"], 5), "oi": perp["oi_usd"],
                 "oi7": r2(oi7, 1)} if perp else None,
        "score": sc["score"], "src": sc["src"], "total": sc["total"], "chat": sc["chat"], "no_fin": sc["no_fin"],
        "light": chk["light"], "checks": chk["items"], "passed": chk["passed"], "known": chk["known"],
    }


def main():
    force_full = "--full" in sys.argv
    history = load(HISTORY_PATH, {})
    state = load(STATE_PATH, {})
    cache = load(CACHE_PATH, {})
    shortlist = load(SHORTLIST_PATH, {"coins": []}).get("coins", [])
    shortlist = list(dict.fromkeys(s.strip() for s in shortlist if isinstance(s, str) and s.strip()))

    full = force_full or NOW_TS - state.get("last_full", 0) >= CONFIG.get("universe_every_hours", 4) * 3600 - 600
    log(f"run type: {'FULL (universe + shortlist)' if full else 'shortlist only'}; shortlist={len(shortlist)}")

    hl = fetch_hyperliquid() or {}

    # -------- universe (every few hours)
    universe_doc = load(UNIVERSE_PATH, None)
    if full:
        rows = fetch_universe_markets()
        fin_all = fetch_financials()
        if fin_all is not None:
            cache["fin"] = fin_all
        fin_all = cache.get("fin", {})
        glob = fetch_global()
        if glob:
            cache["global"] = glob
            dom = history.setdefault("btc_dom", [])
            dom.append([NOW_TS, glob["btc_dom"]])
            history["btc_dom"] = trim(dom, 90)
        for name, fn in (("stables", fetch_stablecoins), ("macro", fetch_fred), ("mvrv", fetch_mvrv)):
            v = fn()
            if v is not None:
                cache[name] = v

        if rows:
            btc = next((r for r in rows if r["id"] == "bitcoin"), {})
            cache["btc"] = {"c30": btc.get("c30"), "c200": btc.get("c200")}
            perp_map = map_perps(rows, hl)
            cache["perp_map"] = perp_map
            # daily OI snapshot for every perp in the universe (feeds the "flushed" check)
            u_oi = history.setdefault("u_oi", {})
            for cid, pname in perp_map.items():
                s = u_oi.setdefault(cid, [])
                if not s or NOW_TS - s[-1][0] >= 20 * 3600:
                    s.append([NOW_TS, hl[pname]["oi_usd"]])
                u_oi[cid] = trim(s, 15)
            for cid in list(u_oi):
                if cid not in perp_map:
                    u_oi[cid] = trim(u_oi[cid], 15)
                    if not u_oi[cid]:
                        del u_oi[cid]
            coins = [coin_record(r, fin_all.get(r["id"]), cache["btc"], perp_map.get(r["id"]), hl,
                                 u_oi.get(r["id"]), None, False) for r in rows]
            coins.sort(key=lambda x: (x["total"], x["mcap"] or 0), reverse=True)
            universe_doc = {"generated_at": NOW.isoformat(timespec="seconds"), "count": len(coins),
                            "categories": CAT_NAMES, "coins": coins}
            UNIVERSE_PATH.write_text(json.dumps(universe_doc, separators=(",", ":"), default=lambda o: None))
            state["last_full"] = NOW_TS
        elif universe_doc is None:
            ERRORS.setdefault("Universe", "No universe yet; will retry next run")

    # -------- shortlist (every run)
    sl_rows = fetch_shortlist_markets(shortlist) or {}
    contracts = refresh_contracts(shortlist, cache) or cache.get("contracts", {})
    tracks = {cid: (contracts.get(cid) or {}).get("track") for cid in shortlist}
    tracks = {cid: t for cid, t in tracks.items() if t}

    # exchange flows: transfer by transfer for Ethereum and BNB Chain tokens...
    events = [e for e in history.get("transfers", []) if e["ts"] >= NOW_TS - 8 * 86400]
    for e in events:
        e.setdefault("chain", "eth")
    seen = {e["id"] for e in events}
    for fn, chain in ((fetch_eth_transfers, "eth"), (fetch_bsc_transfers, "bsc")):
        tg = {cid: t for cid, t in tracks.items() if t["kind"] == "token" and t["chain"] == chain}
        events += fn(tg, sl_rows, seen, history) or []
    events.sort(key=lambda e: e["ts"], reverse=True)
    history["transfers"] = events
    flows = flow_summary(events)
    # ...and from hourly exchange balances for native coins and Solana tokens
    bal = {cid: t for cid, t in tracks.items() if t["kind"] == "native" or t["chain"] == "sol"}
    reserves = fetch_exchange_balances(bal, history) if bal else {}
    reserves = reserves or history.get("reserves", {})
    for cid in bal:
        f = reserve_flows(reserves.get(cid), (sl_rows.get(cid) or {}).get("price"))
        if f:
            flows[cid] = f

    # -------- positioning across exchanges, then the leverage gauges
    targets = {m: (m, True) for m in CONFIG["markets"]}
    targets.update({cid: (r["sym"], False) for cid, r in sl_rows.items()})
    deriv = fetch_positioning(targets, cache) or {}
    markets = build_markets(hl, history, deriv) if hl else {}

    perp_map = dict(cache.get("perp_map", {}))
    if sl_rows:
        perp_map.update(map_perps([r for r in sl_rows.values() if r["id"] not in perp_map], hl))
    s_hist = history.setdefault("coins", {})
    btc = cache.get("btc", {})
    fin_all = cache.get("fin", {})
    shortlist_out = []
    for cid in shortlist:
        row = sl_rows.get(cid)
        if not row:
            continue
        pname = perp_map.get(cid)
        if pname and pname in hl:
            s = s_hist.setdefault(cid, [])
            s.append([NOW_TS, hl[pname]["oi_usd"], hl[pname]["price"], hl[pname]["funding_8h_pct"]])
            s_hist[cid] = trim(s, 45)
        agg = deriv.get(cid)
        rec = coin_record(row, fin_all.get(cid), btc, pname, hl, s_hist.get(cid),
                          flows.get(cid), cid in tracks, agg)
        rec["spark"] = [round(p, 8) for p in row["_spark"][::4]]
        if agg and agg.get("oi_series"):
            rec["oi_series"], rec["oi_src"] = [round(p[1] / 1e6, 2) for p in agg["oi_series"][-168:]], "all"
        else:
            rec["oi_series"], rec["oi_src"] = [round(p[1] / 1e6, 2) for p in s_hist.get(cid, [])[-168:]], "hl"
        rec["deriv"] = deriv_summary(agg)
        rec["flows"] = flows.get(cid)
        rec["track"] = tracks.get(cid)
        shortlist_out.append(rec)
    for cid in list(s_hist):
        if cid not in shortlist:
            del s_hist[cid]

    alerts = collect_alerts(markets, shortlist_out, events, cache.get("mvrv"), state)
    send_alerts(alerts, state)

    out = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "universe_at": (universe_doc or {}).get("generated_at"),
        "markets": markets,
        "shortlist": shortlist_out,
        "stablecoins": cache.get("stables"),
        "dominance": {**cache["global"], "series": [round(p[1], 2) for p in history.get("btc_dom", [])[-90:]]}
        if cache.get("global") else None,
        "macro": cache.get("macro"),
        "mvrv": cache.get("mvrv"),
        "transfers": [e for e in events if e["usd"] >= TH["whale_list_usd"]][:50],
        "wallet_counts": {c: len(wallet_map(c)) for c in WALLET_KEYS},
        "alerts": state.get("recent", []),
        "errors": ERRORS,
        "thresholds": TH,
        "categories": CAT_NAMES,
    }
    OUT_PATH.write_text(json.dumps(out, separators=(",", ":"), default=lambda o: None))
    HISTORY_PATH.write_text(json.dumps(history, separators=(",", ":")))
    STATE_PATH.write_text(json.dumps(state, separators=(",", ":")))
    CACHE_PATH.write_text(json.dumps(cache, separators=(",", ":"), default=lambda o: None))
    log(f"done: universe={'refreshed' if full else 'kept'}, shortlist={len(shortlist_out)}, errors={len(ERRORS)}")
    if not hl and not sl_rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
