"""1в-2 pre-screen, regime-conditioned: does the ΔOI collinearity hold when it matters?

Companion to oi_divergence_prescreen.py addressing the regime-robustness caveat: a
forward liquidation map matters most in the run-up to volatility (rapid OI build).
So recompute cosine(binance ΔOI⁺, Σ-venue ΔOI⁺) restricted to (a) the top-quartile
cross-venue ΔOI⁺-magnitude bars (accumulation spikes) and (b) the top-quartile
|1h return| bars, and compare to the full-window cosine. If majors stay collinear in
the volatile subset, the "kill 1в-2 for majors" verdict is regime-robust in-window.
`max OI drop` flags whether the window even contained a real cascade (−15%…−40%).
Binance retains ~20 days of OI history, so a genuine cascade may be out of reach.
Run: ``uv run python research/maps_prescreen/oi_regime.py``.
"""
from __future__ import annotations

import math

import ccxt

VENUES = ["binance", "bybit", "okx"]
SYMS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"]


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else float("nan")


def _series(ex: ccxt.Exchange, sym: str) -> dict[int, float]:
    rows = ex.fetch_open_interest_history(sym, "1h", None, 500)
    out: dict[int, float] = {}
    for r in rows:
        hour = int(r["timestamp"] // 3_600_000)
        val = r.get("openInterestValue") or r.get("openInterestAmount")
        if val:
            out[hour] = float(val)
    return out


def main() -> None:
    exs = {v: getattr(ccxt, v)({"options": {"defaultType": "swap", "defaultSubType": "linear"},
                                "enableRateLimit": True}) for v in VENUES}
    for ex in exs.values():
        ex.load_markets()

    print(f"{'symbol':14} {'bars':5} {'cos_all':8} {'cos_hiACT':9} {'cos_hiVOL':9} {'maxOIdrop':10}")
    for sym in SYMS:
        try:
            data = {v: _series(exs[v], sym) for v in VENUES}
            common = sorted(set.intersection(*[set(d.keys()) for d in data.values()]))
            if len(common) < 40:
                print(f"{sym:14} too few common bars")
                continue
            kl = exs["binance"].fetch_ohlcv(sym, "1h", None, len(common) + 5)
            px = {int(k[0] // 3_600_000): k[4] for k in kl}
            d = {v: [max(data[v][common[i]] - data[v][common[i - 1]], 0.0)
                     for i in range(1, len(common))] for v in VENUES}
            bind = d["binance"]
            sumd = [sum(d[v][i] for v in VENUES) for i in range(len(bind))]
            n = len(bind)
            act = sorted(range(n), key=lambda i: sumd[i], reverse=True)[:max(1, n // 4)]
            rets = [abs(px.get(common[i + 1], 0) / px.get(common[i], 1) - 1) if px.get(common[i]) else 0.0
                    for i in range(n)]
            vol = sorted(range(n), key=lambda i: rets[i], reverse=True)[:max(1, n // 4)]
            cos_act = cosine([bind[i] for i in act], [sumd[i] for i in act])
            cos_vol = cosine([bind[i] for i in vol], [sumd[i] for i in vol])
            blv = [data["binance"][t] for t in common]
            maxdrop = min(blv[i] / blv[i - 1] - 1 for i in range(1, len(blv)))
            print(f"{sym:14} {n:<5} {cosine(bind, sumd):<8.3f} {cos_act:<9.3f} {cos_vol:<9.3f} {maxdrop:<10.1%}")
        except Exception as exc:  # noqa: BLE001 - research script: report and continue
            print(f"{sym:14} ERR {repr(exc)[:70]}")


if __name__ == "__main__":
    main()
