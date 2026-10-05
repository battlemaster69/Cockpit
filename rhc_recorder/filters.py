"""The six survivor filters. They label a snapshot; they never decide what gets recorded."""

NAMES = ("age", "liquidity", "volume", "turnover", "txns", "drawdown")


def in_band(age_days, f):
    return f["age_days_min"] <= age_days <= f["age_days_max"]


def evaluate(row, f):
    """{filter: passed} for one snapshot row (age_days, liquidity_usd, volume_24h_usd, txns_24h, drawdown_pct)."""
    liq, vol = row["liquidity_usd"] or 0, row["volume_24h_usd"] or 0
    turnover = vol / liq if liq else 0
    return {"age": in_band(row["age_days"], f),
            "liquidity": liq >= f["liquidity_usd_min"],
            "volume": vol >= f["volume_24h_usd_min"],
            "turnover": f["turnover_min"] <= turnover <= f["turnover_max"],
            "txns": (row["txns_24h"] or 0) >= f["txns_24h_min"],
            "drawdown": row["drawdown_pct"] is not None and row["drawdown_pct"] <= f["drawdown_max_pct"]}
