#!/usr/bin/env python3
"""
build_seed_wallets.py: one-off. Wallets that were early buyers in at least 2 of the seed tokens (known_addresses.yaml).

An early buyer received tokens directly from a selling contract (a pool, the Uniswap v4 pool manager or a launchpad
curve) in the first 72 hours after the token's first pool. Those sellers are the token's busiest sending addresses
that have contract code; wallets that only got airdrops or wallet-to-wallet transfers don't count. Contracts are
dropped from the final list. Writes data/seed_wallets.csv. Rebuild by hand every few weeks with newer winners.

    python rhc_recorder/build_seed_wallets.py
"""

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402
from net import Net  # noqa: E402
from sources.rpc import RPC  # noqa: E402

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
KNOWN = yaml.safe_load((HERE / "known_addresses.yaml").read_text(encoding="utf-8"))
WINDOW_H, MIN_TOKENS, TOP_SENDERS, MAX_LOG_CALLS = 72, 2, 15, 400


def main():
    # a one-off: allow far more calls than a run, and wait out rate limits instead of stopping
    net_cfg = {**CFG["net"], "max_calls_per_run": {"geckoterminal": 0, "dexscreener": 20, "rpc": 3000}, "stop_after_429s": 12}
    net = Net(net_cfg)
    rpc = RPC(net, CFG["chain"]["rpc_url"])
    buyers_by_token = {}
    for sym, token in KNOWN["seed_tokens"].items():
        token = token.lower()
        pairs = net.request("dexscreener", "GET", f"https://api.dexscreener.com/token-pairs/v1/{CFG['chain']['ds_chain']}/{token}") or []
        first = min(p["pairCreatedAt"] for p in pairs if p.get("pairCreatedAt")) / 1000
        start, _ = rpc.block_at(first - 3600)
        end, _ = rpc.block_at(first + WINDOW_H * 3600)
        logs = rpc.transfers(token, start, end, MAX_LOG_CALLS)
        senders = [a for a, _ in Counter(f for _, f, _, _ in logs).most_common(TOP_SENDERS)]
        codes = rpc.codes(senders)
        sellers = {a for a in senders if codes[a] not in ("0x", "")}
        buyers = {to for _, frm, to, _ in logs if frm in sellers and to not in sellers}
        buyers_by_token[sym] = buyers
        print(f"{sym}: first pool {datetime.fromtimestamp(first, timezone.utc):%Y-%m-%d %H:%M} UTC, {len(logs)} transfers in {WINDOW_H}h, "
              f"{len(sellers)} selling contracts, {len(buyers)} buyers ({net.calls['rpc']} RPC calls so far)", flush=True)
    counts = Counter(w for b in buyers_by_token.values() for w in b)
    multi = sorted(w for w, n in counts.items() if n >= MIN_TOKENS)
    codes = rpc.codes(multi) if multi else {}
    wallets = [w for w in multi if codes.get(w, "0x") in ("0x", "")]
    rows = [{"wallet": w, "n_tokens": counts[w], "tokens": " ".join(s for s, b in buyers_by_token.items() if w in b)} for w in wallets]
    path = store.DATA / "seed_wallets.csv"
    if path.exists():
        path.unlink()
    store.append(path, ("wallet", "n_tokens", "tokens"), rows)
    print(f"{len(multi)} wallets early in {MIN_TOKENS}+ seed tokens, {len(multi) - len(wallets)} dropped as contracts; "
          f"wrote {len(rows)} to {path}. RPC calls: {net.calls['rpc']}")


if __name__ == "__main__":
    main()
