"""CSV and state files. Every write goes to a temporary file that is then renamed, so a crash mid-write cannot
corrupt a file. Fast-growing files are split by UTC day, so each commit touches a small file."""

import csv
import io
import json
import os
from pathlib import Path

DATA = Path(os.environ.get("RHC_DATA") or Path(__file__).resolve().parent / "data")  # RHC_DATA: a scratch folder for tests

UNIVERSE = ("token_address", "symbol", "name", "pair_address", "dex", "pool_created_utc", "first_seen_utc", "found_by",
            "liquidity_at_discovery", "tracked")
SNAPSHOT = ("run_utc", "token_address", "pair_address", "age_days", "price_usd", "liquidity_usd", "volume_24h_usd", "txns_24h",
            "market_cap_usd", "peak_price_usd", "drawdown_pct", "filters_passed", "pass", "status")
COHORT = ("token_address", "symbol", "cohort", "assigned_utc", "age_days_at_assign", "price_at_assign", "liquidity_at_assign",
          "matched_passer", "peak_source", "mechanism")
RUNS = ("run_utc", "kind", "duration_s", "calls_geckoterminal", "calls_dexscreener", "calls_rpc", "new_pools", "tokens_seen",
        "tracked", "new_passers", "new_controls", "random_seed", "stopped", "errors")


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def append(path, fields, rows, dry=False):
    if not rows:
        return
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    if not path.exists():
        w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
    if dry:
        print(f"[dry run] {path.relative_to(DATA.parent)}: {len(rows)} rows, e.g. {buf.getvalue().splitlines()[-1][:160]}")
        return
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    _atomic(path, old + buf.getvalue())


def read(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_state():
    p = DATA / "state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state, dry=False):
    if not dry:
        _atomic(DATA / "state.json", json.dumps(state, separators=(",", ":"), sort_keys=True))
