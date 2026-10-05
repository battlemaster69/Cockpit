"""GeckoTerminal public API (no key): new pools, top pools by 24h volume, daily candles. About 18 calls a minute
drew HTTP 429 on 2026-10-05, so net.py spaces calls 6.5 s apart."""

from datetime import datetime

BASE = "https://api.geckoterminal.com/api/v2"
HDR = {"Accept": "application/json;version=20230302"}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pools(net, network, path, page, **params):
    d = net.request("geckoterminal", "GET", f"{BASE}/networks/{network}/{path}",
                    params={"page": page, "include": "base_token,quote_token,dex", **params}, headers=HDR) or {}
    tokens = {x["id"]: x["attributes"] for x in d.get("included", []) if x.get("type") == "token"}
    out = []
    for p in d.get("data") or []:
        a, rel = p["attributes"], p["relationships"]

        def tok(side):
            tid = rel[side]["data"]["id"]
            t = tokens.get(tid, {})
            return {"address": tid.split("_", 1)[1].lower(), "symbol": t.get("symbol") or "", "name": t.get("name") or ""}
        out.append({"pool": a["address"].lower(), "created": datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")),
                    "dex": rel["dex"]["data"]["id"], "liquidity": _num(a.get("reserve_in_usd")),
                    "base": tok("base_token"), "quote": tok("quote_token")})
    return out


def new_pools(net, network, page):
    return _pools(net, network, "new_pools", page)


def top_pools(net, network, page):
    return _pools(net, network, "pools", page, sort="h24_volume_usd_desc")


def daily_high(net, network, pool, days=31):
    """Highest daily high (USD, the base token) over the pool's last `days` days."""
    d = net.request("geckoterminal", "GET", f"{BASE}/networks/{network}/pools/{pool}/ohlcv/day",
                    params={"limit": days, "currency": "usd", "token": "base"}, headers=HDR) or {}
    rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    highs = [_num(r[2]) for r in rows if len(r) > 2 and _num(r[2])]
    return max(highs) if highs else None
