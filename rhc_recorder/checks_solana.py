"""Solana checks from RugCheck's free token report (api.rugcheck.xyz), recorded as flags, never filters.

The public Solana RPC refused holder queries (HTTP 429, 2026-10-09), and RugCheck already reads what matters on
Solana: whether someone can still create tokens (mint authority) or freeze wallets (freeze authority), how much of the
pool's liquidity is locked or burned, top holders with an insider flag on each, wallet networks that move tokens
between themselves, Token-2022 extensions (transfer fees, a permanent delegate that can move anyone's tokens), and its
own risk score. Seed-wallet overlap (the Robinhood check) needs paid transaction history, so the insider flags stand in."""

BASE = "https://api.rugcheck.xyz/v1"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
POOLISH = {"AMM", "LOCKER", "MARKET", "POOL"}

FIELDS = ("mint_authority", "freeze_authority", "lp_locked_pct", "holders", "top10_pct", "insiders_top20",
          "insider_network", "token2022_risk", "rugcheck_score", "creator_pct", "rugged", "launchpad", "danger_risks",
          "checks_note")


def report(net, mint, dex=""):
    r = net.request("rugcheck", "GET", f"{BASE}/tokens/{mint}/report") or {}
    if not r or not r.get("mint"):
        return {"checks_note": "no RugCheck report"}
    known = r.get("knownAccounts") or {}
    pool_like = {a for a, v in known.items() if str((v or {}).get("type", "")).upper() in POOLISH}
    holders = [h for h in r.get("topHolders") or [] if h.get("owner") not in pool_like and h.get("address") not in pool_like]
    markets = sorted(r.get("markets") or [], key=lambda m: -(((m.get("lp") or {}).get("lpLockedUSD") or 0) + ((m.get("lp") or {}).get("quoteUSD") or 0)))
    lp = (markets[0].get("lp") or {}) if markets else {}
    supply = ((r.get("token") or {}).get("supply")) or 0
    ext = r.get("token_extensions") or {}
    fee = r.get("transferFee") or {}
    t22 = []
    if (fee.get("pct") or 0) > 0:
        t22.append(f"transfer fee {fee['pct']}%")
    if isinstance(ext, dict) and ext.get("permanentDelegate"):
        t22.append("permanent delegate")
    nets = r.get("insiderNetworks") or []
    return {"mint_authority": bool(r.get("mintAuthority")), "freeze_authority": bool(r.get("freezeAuthority")),
            "lp_locked_pct": round(lp["lpLockedPct"], 1) if lp.get("lpLockedPct") is not None else None,
            "holders": r.get("totalHolders"),
            "top10_pct": round(sum(h.get("pct") or 0 for h in holders[:10]), 2) if holders else None,
            "insiders_top20": sum(1 for h in holders[:20] if h.get("insider")),
            "insider_network": max((n.get("size") or 0) for n in nets) if nets else 0,
            "token2022_risk": "; ".join(t22) if t22 else ("token-2022" if r.get("tokenProgram") == TOKEN_2022 else ""),
            "rugcheck_score": r.get("score_normalised"),
            "creator_pct": round((r.get("creatorBalance") or 0) / supply * 100, 2) if supply else None,
            "rugged": bool(r.get("rugged")), "launchpad": r.get("launchpad") or (markets[0].get("marketType") if markets else "") or dex,
            "danger_risks": "; ".join(x.get("name", "") for x in r.get("risks") or [] if x.get("level") == "danger")[:200],
            "checks_note": ""}
