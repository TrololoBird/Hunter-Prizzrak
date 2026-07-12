"""Select a REPRESENTATIVE manipulation-profile universe from Binance USDⓈ-M perps.

The manipulation strategy targets low-/mid-cap, often newly-listed alts that are
prone to pump-dump — NOT tokenized stocks (AMZN/GOOGL/COIN…) and NOT the top majors
(too liquid to sweep). This picks that population WITHOUT selecting on outcome
(no "had a big move" filter — that would be look-ahead / survivorship bias).

Prints a symbol list suitable for research/fetch/fetch_history.py SYMBOLS.

Run:  uv run python research/fetch/select_universe.py
"""
from __future__ import annotations

import re
import os
import sys

import ccxt

# Tokenized equities on Binance futures — an equity underlying, not crypto
# manipulation dynamics. Exclude by known tickers + obvious equity names.
_STOCK_TICKERS = {
    "AMZN", "GOOGL", "GOOG", "COIN", "JPM", "DELL", "META", "HYUNDAI", "EWZ",
    "CRCL", "NVDA", "AAPL", "MSFT", "TSLA", "MSTR", "HOOD", "SPY", "QQQ",
    "NFLX", "AMD", "PLTR", "BABA", "ARM", "SPCX", "RKLB", "CRWD", "MARA",
    "INTC", "AMZN", "GME", "PYPL", "UNH", "AVGO", "ORCL", "CRM", "MU",
}
# Top majors — too deep to manipulate with a sweep; not the strategy's target.
_MAJORS = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "TRX", "LINK", "AVAX",
    "DOT", "MATIC", "LTC", "BCH", "XLM", "ATOM", "UNI", "ETC", "FIL", "APT",
    "XAU", "XAG", "PAXG",  # metals
}


def _base(symbol: str) -> str:
    # "FOO/USDT:USDT" -> "FOO"
    return symbol.split("/", 1)[0]


def main() -> int:
    from research.fetch._ip_guard import assert_live_not_running
    assert_live_not_running(what="select_universe")
    ex = ccxt.binance({
        "options": {"defaultType": "future", "fetchMarkets": ["linear"]},
        "enableRateLimit": True,
    })
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()

    rows = []
    for sym, m in markets.items():
        if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")):
            continue
        base = m.get("base") or _base(sym)
        if base in _STOCK_TICKERS or base in _MAJORS:
            continue
        if re.search(r"\d{3,}", base):  # index-y names
            continue
        t = tickers.get(sym) or {}
        qv = t.get("quoteVolume")
        if qv is None:
            continue
        rows.append((base, float(qv)))

    rows.sort(key=lambda r: -r[1])
    # Mid/low band: skip the top-N most-liquid (still major-ish), take the next tranche
    # down to a liquidity floor — the low-/mid-cap alts the scanner actually trades.
    skip_top = int(os.getenv("HUNT_UNIVERSE_SKIP_TOP", "30") or 30)
    floor = float(os.getenv("HUNT_UNIVERSE_FLOOR_USD", "3000000") or 3_000_000)
    count = int(os.getenv("HUNT_UNIVERSE_COUNT", "50") or 50)
    band = [r for r in rows[skip_top:] if r[1] >= floor]
    picks = band[:count]

    print(f"# total linear USDT perps: {len(rows)} (after excluding stocks+majors)")
    print(f"# selected mid/low-cap band: {len(picks)} symbols (rank {skip_top}.., ≥${floor/1e6:.0f}M 24h vol)\n")
    print("SYMBOLS = [")
    for base, qv in picks:
        print(f'    "{base}/USDT:USDT",  # ${qv/1e6:.1f}M')
    print("]")
    if hasattr(ex, "close"):
        ex.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
