"""1в-2 pre-screen: cross-venue OI-delta divergence vs Binance (public REST, bounded).

One-sided falsification of cross-venue OI aggregation (1в-2). The liquidation
forward-zone model is mass-preserving (leverage weights renormalized), so replacing
Binance OI with the cross-venue sum changes the map only if the ΔOI-series SHAPE
diverges, not its scale. Metric = cosine(binance ΔOI⁺, Σ-venue ΔOI⁺) over 200×1h.

High cosine (majors, Binance-dominated OI) → 1в-2 is a no-op after normalization →
killed by fact. Low cosine (low-liquidity, non-Binance OI dominant) → 1в-2 justified.
Run in the project env: ``uv run python research/maps_prescreen/oi_divergence_prescreen.py``.
"""
from __future__ import annotations

import math

import ccxt

VENUES = ["binance", "bybit", "okx"]  # bitget: no fetchOpenInterestHistory
SYMS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "DOGE/USDT:USDT"]


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else float("nan")


def _series(ex: ccxt.Exchange, sym: str) -> dict[int, float]:
    rows = ex.fetch_open_interest_history(sym, "1h", None, 200)
    out: dict[int, float] = {}
    for r in rows:
        hour = int(r["timestamp"] // 3_600_000)
        val = r.get("openInterestValue") or r.get("openInterestAmount")
        if val:
            out[hour] = float(val)
    return out


def _delta_pos(vals: list[float]) -> list[float]:
    return [max(vals[i] - vals[i - 1], 0.0) for i in range(1, len(vals))]


def main() -> None:
    exs: dict[str, ccxt.Exchange] = {}
    for v in VENUES:
        ex = getattr(ccxt, v)({"options": {"defaultType": "swap", "defaultSubType": "linear"},
                               "enableRateLimit": True})
        ex.load_markets()
        exs[v] = ex

    print(f"{'symbol':16} {'cos(sum,bin)':12} {'nonbin%':8} {'cos(byb,bin)':12} {'cos(okx,bin)':12}")
    for sym in SYMS:
        try:
            data = {v: _series(exs[v], sym) for v in VENUES}
            common = sorted(set.intersection(*[set(d.keys()) for d in data.values()]))
            if len(common) < 20:
                print(f"{sym:16} too few common bars")
                continue
            d = {v: _delta_pos([data[v][t] for t in common]) for v in VENUES}
            bind = d["binance"]
            sumd = [sum(d[v][i] for v in VENUES) for i in range(len(bind))]
            nonbin = (sum(sumd) - sum(bind)) / sum(sumd) if sum(sumd) else 0.0
            print(f"{sym:16} {cosine(bind, sumd):<12.4f} {nonbin:<8.1%} "
                  f"{cosine(bind, d['bybit']):<12.4f} {cosine(bind, d['okx']):<12.4f}")
        except Exception as exc:  # noqa: BLE001 - research script: report and continue
            print(f"{sym:16} ERR {repr(exc)[:80]}")


if __name__ == "__main__":
    main()
