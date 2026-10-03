"""Temporary backtest (runs on GitHub, where the Coinalyze key lives): do futures signals warn of calm-market pumps?
Universe: CoinGecko top 500 with $5M+ volume, Binance spot hourly candles (price, volume, aggressive buys),
Coinalyze hourly open interest (Binance perp, else Bybit/OKX) and funding over 30 days. Prints summary stats only."""
import statistics as stt
import sys
import time

import requests

sys.path.insert(0, "collector")
import collect as C  # noqa: E402

t_start = time.time()
rows = [C.market_row(r) for r in C.cg_markets(pages=3, sparkline=False)]
rows = sorted([r for r in rows if r["mcap"] and not C.excluded(r)], key=lambda r: -r["mcap"])
rows = [r for r in rows if r.get("rank") and r["rank"] <= 500 and (r["vol"] or 0) >= 5e6]
info = requests.get("https://data-api.binance.vision/api/v3/exchangeInfo", timeout=60).json()
spot = {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
cz = C.cz_markets({})
coins = {}
for r in rows:
    sym = r["sym"] + "USDT"
    if sym not in spot:
        continue
    k = requests.get("https://data-api.binance.vision/api/v3/klines", params={"symbol": sym, "interval": "1h", "limit": 720}, timeout=30).json()
    if len(k) < 700 or abs(float(k[-1][4]) / r["price"] - 1) > 0.1:
        continue
    perps = cz.get(r["sym"].upper()) or cz.get("1000" + r["sym"].upper()) or []
    perp = next((p for p in perps if p[1].lower().startswith("binance")), perps[0] if perps else None)
    coins[r["id"]] = {"sym": r["sym"], "rank": r["rank"], "perp": perp[0] if perp else None,
                      "k": {int(c[0]) // 1000: (float(c[2]), float(c[3]), float(c[4]), float(c[7]), float(c[10])) for c in k}}
    time.sleep(0.05)
perps = sorted({c["perp"] for c in coins.values() if c["perp"]})
print(f"{len(rows)} coins top-500 $5M+; {len(coins)} on Binance spot; {len(perps)} with a futures market on Coinalyze", flush=True)
oi = C.cz_history("open-interest-history", perps, 31 * 86400, convert_to_usd="true")
fr = C.cz_history("funding-rate-history", perps, 31 * 86400)
print(f"Coinalyze history fetched in {time.time() - t_start:.0f}s: OI for {sum(1 for p in perps if oi.get(p))}, funding for {sum(1 for p in perps if fr.get(p))}", flush=True)

T = sorted(set.intersection(*[set(c["k"]) for c in coins.values()]))
N = len(T)
S = {}
for cid, c in coins.items():
    s = {"sym": c["sym"], "rank": c["rank"], "h": [], "l": [], "c": [], "q": [], "tb": []}
    for t in T:
        h, l, cl, q, tb = c["k"][t]
        s["h"].append(h); s["l"].append(l); s["c"].append(cl); s["q"].append(q); s["tb"].append(tb)
    om = {p["t"]: C.fnum(p.get("c")) for p in oi.get(c["perp"], [])} if c["perp"] else {}
    fm = {p["t"]: C.fnum(p.get("c")) for p in fr.get(c["perp"], [])} if c["perp"] else {}
    # open interest in coins (dollar OI / price), so price moves alone don't move it
    s["oi"] = [(om.get(t) / s["c"][i]) if om.get(t) else None for i, t in enumerate(T)]
    s["fr"] = [fm.get(t) for t in T]
    s["has_oi"] = sum(1 for v in s["oi"] if v) > N * 0.8
    S[cid] = s
mret = [0.0] + [stt.median(S[c]["c"][i] / S[c]["c"][i - 1] - 1 for c in S) for i in range(1, N)]
mcum = [1.0]
for x in mret[1:]:
    mcum.append(mcum[-1] * (1 + x))
F = {}


def chg(a, b):
    return a / b - 1 if a and b else None


def feats(cid, h):
    if (cid, h) in F:
        return F[(cid, h)]
    s = S[cid]; q, c, o = s["q"], s["c"], s["oi"]
    base = stt.median(q[h - 72:h]) or 1e-9
    fr_now = s["fr"][h]
    f = {"volx": q[h] / base, "p1": c[h] / c[h - 1] - 1, "p6": c[h] / c[h - 6] - 1, "p12": c[h] / c[h - 12] - 1,
         "taker": s["tb"][h] / q[h] if q[h] else .5, "hi24": c[h] >= max(c[h - 24:h]), "mkt3": mcum[h] / mcum[h - 3] - 1,
         "oi6": chg(o[h], o[h - 6]), "oi12": chg(o[h], o[h - 12]), "oi24": chg(o[h], o[h - 24]),
         "fr": fr_now, "has_oi": s["has_oi"]}
    F[(cid, h)] = f
    return f


calm = lambda f: abs(f["mkt3"]) < .015  # noqa: E731
events = []
for cid, s in S.items():
    c = s["c"]; last = -999
    for t0 in range(224, N - 3):
        if t0 - last < 48:
            continue
        if max(c[t0:t0 + 3]) / c[t0 - 1] - 1 >= .10 and abs(mcum[t0 + 2] / mcum[t0 - 1] - 1) <= .02 and c[t0 - 1] / c[t0 - 25] - 1 < .08:
            events.append((cid, t0)); last = t0
ev_oi = [(cid, t0) for cid, t0 in events if S[cid]["has_oi"]]
hits = tot = 0
for cid, s in S.items():
    for h in range(224, N - 12, 3):
        tot += 1; hits += max(s["c"][h + 1:h + 13]) / s["c"][h] - 1 >= .08
base = hits / tot
DAYS = (N - 224) / 24
g = lambda f, k, lo: f[k] is not None and f[k] >= lo  # noqa: E731
RULES = {
    "[ref] 24h high, vol>=4x, +2-5%":           lambda f: f["hi24"] and f["volx"] >= 4 and .02 <= f["p1"] < .05 and calm(f),
    "OI(coins) +10% in 12h, price flat":        lambda f: g(f, "oi12", .10) and abs(f["p12"]) < .03 and calm(f),
    "OI +10%/12h flat, funding<=0":             lambda f: g(f, "oi12", .10) and abs(f["p12"]) < .03 and f["fr"] is not None and f["fr"] <= 0 and calm(f),
    "OI +20% in 24h, price flat":               lambda f: g(f, "oi24", .20) and abs(f["p12"]) < .04 and calm(f),
    "OI +5%/6h, vol>=2x, price quiet":          lambda f: g(f, "oi6", .05) and f["volx"] >= 2 and abs(f["p6"]) < .03 and calm(f),
    "OI +10%/12h flat, buyers>=55%":            lambda f: g(f, "oi12", .10) and abs(f["p12"]) < .03 and f["taker"] >= .55 and calm(f),
    "funding <= -0.03% (shorts crowded), flat": lambda f: f["fr"] is not None and f["fr"] <= -.03 and abs(f["p12"]) < .03 and calm(f),
    "24h-high break + OI +3% in 6h":            lambda f: f["hi24"] and f["volx"] >= 3 and .02 <= f["p1"] < .05 and g(f, "oi6", .03) and calm(f),
    "24h-high break + funding<=0":              lambda f: f["hi24"] and f["volx"] >= 3 and .02 <= f["p1"] < .05 and f["fr"] is not None and f["fr"] <= 0 and calm(f),
}
print(f"\n{len(S)} coins ({sum(1 for s in S.values() if s['has_oi'])} with futures data), {DAYS:.0f} days, "
      f"{len(events)} calm-market pumps ({len(ev_oi)} on coins with futures data), base rate {base * 100:.1f}%\n")
print(f"{'rule':42} {'early':>9} {'1st hr':>6} {'alerts/day':>10} {'hit':>6} {'lift':>5} {'24h avg':>8}")
for name, rule in RULES.items():
    evs = events if name.startswith("[ref]") else ev_oi
    early = sum(1 for cid, t0 in evs if any(rule(feats(cid, h)) for h in range(t0 - 6, t0)))
    first = early + sum(1 for cid, t0 in evs if rule(feats(cid, t0)) and S[cid]["c"][t0] / S[cid]["c"][t0 - 1] - 1 < .05)
    sig = good = 0; after = []
    for cid, s in S.items():
        last = -999
        for h in range(224, N - 24):
            if h - last < 12:
                continue
            if rule(feats(cid, h)):
                last = h; sig += 1
                good += max(s["c"][h + 1:h + 13]) / s["c"][h] - 1 >= .08
                after.append(s["c"][h + 24] / s["c"][h] - 1)
    p = good / sig if sig else 0
    print(f"{name:42} {early:3} ({early / max(1, len(evs)) * 100:2.0f}%) {first:5}  {sig / DAYS:9.1f}  {p * 100:5.1f}% {p / base:5.1f}x "
          f"{(stt.mean(after) * 100 if after else 0):+7.2f}%", flush=True)
print(f"\ndone in {time.time() - t_start:.0f}s")
