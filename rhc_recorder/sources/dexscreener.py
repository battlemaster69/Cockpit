"""DexScreener API (no key): price, liquidity, volume and transactions for up to 30 tokens per call. It has no
endpoint that lists a chain's pools, which is why discovery comes from GeckoTerminal."""

BASE = "https://api.dexscreener.com"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _summary(pairs):
    """One token's view: its deepest pool for price and liquidity, its oldest pool for age."""
    best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")) or 0)
    tx = (best.get("txns") or {}).get("h24") or {}
    created = [p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")]
    return {"pair": best["pairAddress"].lower(), "dex": best.get("dexId", "") + ("-" + "-".join(best["labels"]) if best.get("labels") else ""),
            "symbol": best["baseToken"].get("symbol") or "", "name": best["baseToken"].get("name") or "",
            "price": _num(best.get("priceUsd")), "liquidity": _num((best.get("liquidity") or {}).get("usd")) or 0.0,
            "volume_24h": _num((best.get("volume") or {}).get("h24")) or 0.0,
            "txns_24h": int((tx.get("buys") or 0) + (tx.get("sells") or 0)),
            "market_cap": _num(best.get("marketCap")) or _num(best.get("fdv")),
            "oldest_pair_ms": min(created) if created else None,
            "pairs": sorted({p["pairAddress"].lower() for p in pairs})}


def tokens(net, chain, addresses):
    """{token address: summary} for the tokens DexScreener knows; unknown ones are simply absent."""
    out, addrs = {}, sorted({a.lower() for a in addresses})
    for i in range(0, len(addrs), 30):
        chunk = set(addrs[i:i + 30])
        data = net.request("dexscreener", "GET", f"{BASE}/tokens/v1/{chain}/{','.join(sorted(chunk))}") or []
        by = {}
        for p in data if isinstance(data, list) else data.get("pairs") or []:
            base = (p.get("baseToken") or {}).get("address", "").lower()
            if base in chunk and p.get("pairAddress"):
                by.setdefault(base, []).append(p)
        out.update({a: _summary(ps) for a, ps in by.items()})
    return out


def token_pairs(net, chain, address):
    """Every pool of one token (for the holder check's exclusion list)."""
    data = net.request("dexscreener", "GET", f"{BASE}/token-pairs/v1/{chain}/{address}") or []
    return sorted({p["pairAddress"].lower() for p in data if p.get("pairAddress")})
