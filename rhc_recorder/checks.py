"""Contract and holder checks through the RPC node, recorded as flags (never filters).

Contract: function selectors in the bytecode (PUSH4 <selector> in the dispatcher), or in the implementation when the
token is an EIP-1967 proxy; owner() / getOwner() read directly. This catches the common patterns, not renamed or
hidden functions, which is why these are flags to test rather than reasons to drop a token.
Holders: balances rebuilt from every Transfer log since launch."""

from sources.rpc import TooManyLogs

SELECTORS = {
    "has_mint": ["40c10f19", "a0712d68", "449a52f8"],                       # mint(address,uint256), mint(uint256), mintTo(address,uint256)
    "can_pause": ["8456cb59"],                                              # pause()
    "can_blacklist": ["f9f92be4", "44337ea1", "153b0d1e", "455a4396", "9cfe42da", "9c0db5f3", "d34628cc"],  # blacklist / bots setters
    "can_set_fee": ["69fe0e2d", "c647b20e", "0cc835a3", "8b4cee08"],        # setFee, setTaxes, setBuyFee, setSellFee
}
ADMIN_ROLE = "a217fddf"                                                     # DEFAULT_ADMIN_ROLE(): OpenZeppelin access control
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
TOTAL_SUPPLY, OWNER, GET_OWNER = "0x18160ddd", "0x8da5cb5b", "0x893d20e8"
ZERO = "0x" + "0" * 40

FIELDS = ("src_verified", "has_mint", "has_owner", "can_pause", "can_blacklist", "can_set_fee", "is_proxy",
          "holders", "top20_pct", "launchpad", "seed_overlap", "overlap_flag", "checks_note")


def _addr(word):
    return ("0x" + word[-40:]).lower() if word and len(word) >= 42 else None


def contract_flags(rpc, token):
    code = rpc.codes([token])[token].lower()
    if code in ("0x", ""):
        return {"checks_note": "no contract code"}
    impl = _addr(rpc.call("eth_getStorageAt", [token, EIP1967_IMPL, "latest"]))
    out = {"is_proxy": bool(impl and impl != ZERO)}
    if out["is_proxy"]:
        code = rpc.codes([impl])[impl].lower()
    for flag, sels in SELECTORS.items():
        out[flag] = any("63" + s in code for s in sels)
    owners = rpc.batch([("eth_call", [{"to": token, "data": OWNER}, "latest"]), ("eth_call", [{"to": token, "data": GET_OWNER}, "latest"])])
    owned = [a for a in map(_addr, owners) if a and a != ZERO]
    out["has_owner"] = bool(owned) or ("63" + ADMIN_ROLE in code)
    return out


def holder_stats(rpc, token, start_block, end_block, exclude, seeds, cfg):
    """Holder count, top-20 share (pools and contracts removed) and seed-wallet overlap."""
    try:
        logs = rpc.transfers(token, start_block, end_block, cfg["max_log_calls_per_token"])
    except TooManyLogs:
        return {"checks_note": "too_many_transfers"}
    bal = {}
    for _, frm, to, amount in logs:
        bal[frm] = bal.get(frm, 0) - amount
        bal[to] = bal.get(to, 0) + amount
    # no mint in the history, or balances below zero: supply was created off-log (some launchpad and v4-hook tokens
    # trade without moving ordinary balances), so the holder figures below are not reliable for this token
    minted = any(frm == ZERO for _, frm, _, _ in logs)
    note = "no_mint_event" if logs and not minted else ("partial_history" if any(v < 0 for a, v in bal.items() if a != ZERO) else "")
    held = {a: v for a, v in bal.items() if v > 0 and a not in exclude}
    top = sorted(held, key=held.get, reverse=True)[:cfg["top_holders_code_check"]]
    codes = rpc.codes(top) if top else {}
    people = [a for a in top if codes.get(a, "0x") in ("0x", "")][:20]
    supply_hex = rpc.call("eth_call", [{"to": token, "data": TOTAL_SUPPLY}, "latest"])
    supply = int(supply_hex, 16) if supply_hex and supply_hex != "0x" else 0
    overlap = len(seeds & set(held)) if seeds else None
    return {"holders": len(held), "top20_pct": round(sum(held[a] for a in people) / supply * 100, 2) if supply else None,
            "seed_overlap": overlap, "overlap_flag": (overlap >= cfg["overlap_min"]) if overlap is not None else None,
            "checks_note": note}
