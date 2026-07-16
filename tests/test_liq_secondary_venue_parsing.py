"""Secondary-venue liquidations must survive CCXT's unified Liquidation shape.

Root cause pinned here: the unified structure (ccxt.base.exchange.safe_liquidation)
carries ``contracts``/``contractSize``/``baseValue`` and has NO ``amount`` key, so the
old ``float(item.get("amount") or info.get("q") or 0)`` resolved only via the
Binance-RAW ``q``. Bybit/OKX therefore yielded qty=0.0 and were dropped by the
``qty > 0`` guard — meaning Bybit, the ONLY full-fidelity tape
(``_VENUE_LIQ_COMPLETENESS["bybit"] == "full"``), never produced a single event.

Three coupled sub-bugs, all pinned below:
  (a) qty read from the wrong key,
  (b) OKX ``contracts`` are CONTRACT units and need ×contractSize,
  (c) ccxt's bybit parser emits side="s" (``safe_string_lower(liq,'side','S')`` treats
      'S' as a DEFAULT VALUE, not a second key), which the old ``else -> long`` branch
      booked as a long-liquidation for every bybit SHORT liquidation.
"""
from __future__ import annotations

from typing import Any

from hunt_core.maps.liquidation import (
    liq_contract_size,
    liq_contract_units,
    liq_price,
    normalize_liq_side,
)


def _bybit_all_liquidation(side_raw: str, size: str = "20000") -> dict[str, Any]:
    """A bybit `allLiquidation` event exactly as ccxt.pro.bybit emits it.

    ccxt calls ``safe_string_lower(liquidation, 'side', 'S')`` — 'S' is the DEFAULT
    VALUE, so with the compact payload (side lives under "S") the unified side comes
    out as the literal string "s". That is reproduced faithfully here.
    """
    return {
        "info": {"T": 1739502302929, "s": "ROSEUSDT", "S": side_raw, "v": size, "p": "0.04499"},
        "symbol": "ROSE/USDT:USDT",
        "contracts": float(size),
        "contractSize": 1.0,
        "price": 0.04499,
        "side": "s",  # ccxt's default-value artefact — NOT a real side
        "baseValue": None,
        "quoteValue": None,
        "timestamp": 1739502302929,
    }


def _okx_liquidation() -> dict[str, Any]:
    """An OKX `liquidation-orders` event as ccxt.pro.okx emits it (sz in CONTRACTS)."""
    return {
        "info": {
            "details": [
                {"bkLoss": "0", "bkPx": "0.007831", "posSide": "short",
                 "side": "buy", "sz": "13", "ts": "1692266434010"}
            ],
            "instFamily": "IOST-USDT",
            "instId": "IOST-USDT-SWAP",
            "instType": "SWAP",
        },
        "symbol": "IOST/USDT:USDT",
        "contracts": 13.0,
        "contractSize": 0.01,   # OKX swap multiplier
        "price": 0.007831,
        "side": "buy",
        "timestamp": 1692266434010,
    }


def _binance_force_order(side_raw: str = "SELL") -> dict[str, Any]:
    """A binance forceOrder as ccxt.pro.binance emits it (info == the raw `o` object)."""
    return {
        "info": {"s": "BTCUSDT", "S": side_raw, "o": "LIMIT", "f": "IOC", "q": "1.437",
                 "p": "35100.81", "ap": "34959.70", "X": "FILLED", "l": "1.437",
                 "z": "1.437", "T": 1698871323059},
        "symbol": "BTC/USDT:USDT",
        "contracts": 1.437,
        "contractSize": 1.0,
        "price": 34959.70,
        "side": side_raw.lower(),
        "timestamp": 1698871323059,
    }


# --- (a) qty must come off `contracts`/raw, never the non-existent `amount` -------

def test_unified_liquidation_has_no_amount_key() -> None:
    # The premise of the whole fix: nothing in the unified structure is `amount`.
    for item in (_bybit_all_liquidation("Sell"), _okx_liquidation(), _binance_force_order()):
        assert "amount" not in item


def test_bybit_qty_is_positive() -> None:
    # Was 0.0 -> event dropped -> the full-fidelity tape produced nothing.
    item = _bybit_all_liquidation("Sell")
    qty = liq_contract_units(item, item["info"]) or 0.0
    assert qty == 20000.0
    assert qty * liq_contract_size(item, None) == 20000.0  # linear USDT: contractSize=1


def test_okx_qty_is_positive() -> None:
    item = _okx_liquidation()
    assert liq_contract_units(item, item["info"]) == 13.0


def test_bybit_snapshot_shape_qty_and_side() -> None:
    # The non-compact bybit `liquidation` topic uses size/side/price/symbol.
    item = {
        "info": {"price": "0.03803", "side": "Buy", "size": "1637",
                 "symbol": "GALAUSDT", "updatedTime": 1673251091822},
        "symbol": "GALA/USDT:USDT", "contracts": 1637.0, "contractSize": 1.0,
        "price": 0.03803, "side": "buy", "timestamp": 1673251091822,
    }
    assert liq_contract_units(item, item["info"]) == 1637.0
    assert normalize_liq_side(item, item["info"]) == "BUY"


# --- (b) OKX contracts are CONTRACT units: notional needs ×contractSize ----------

def test_okx_contract_size_scales_to_base_units() -> None:
    # sz=13 contracts × 0.01 = 0.13 base — NOT 13. Blind use of `contracts` would
    # overstate this liquidation's notional by 100×.
    item = _okx_liquidation()
    qty = (liq_contract_units(item, item["info"]) or 0.0) * liq_contract_size(item, None)
    assert abs(qty - 0.13) < 1e-9


def test_contract_size_falls_back_to_venue_market_then_one() -> None:
    assert liq_contract_size({}, {"contractSize": 0.01}) == 0.01
    # Unknown/absent multiplier must default to 1.0, never 0.0 (which would zero out
    # every event on that venue).
    assert liq_contract_size({}, None) == 1.0
    assert liq_contract_size({"contractSize": 0}, None) == 1.0
    assert liq_contract_size({"contractSize": None}, {"contractSize": None}) == 1.0


def test_item_contract_size_preferred_over_market() -> None:
    assert liq_contract_size({"contractSize": 0.01}, {"contractSize": 5.0}) == 0.01


# --- (c) side must be read from raw info; unknown => skip, never "long" ----------

def test_bybit_buy_is_not_mis_bucketed_as_long() -> None:
    # THE regression: ccxt hands us side="s"; the old code's `else` booked it long.
    # Bybit "Buy" closes a SHORT position => BUY => short-liq bucket downstream.
    item = _bybit_all_liquidation("Buy")
    assert item["side"] == "s"  # the poisoned unified value we must not trust
    assert normalize_liq_side(item, item["info"]) == "BUY"


def test_bybit_sell_is_long_liquidation() -> None:
    item = _bybit_all_liquidation("Sell")
    assert normalize_liq_side(item, item["info"]) == "SELL"


def test_okx_side_from_details() -> None:
    item = _okx_liquidation()
    # OKX detail side="buy" with posSide="short" => a short position was liquidated.
    assert normalize_liq_side(item, item["info"]) == "BUY"


def test_unknown_side_returns_none_not_long() -> None:
    # Never fabricate a direction — the caller SKIPs instead.
    assert normalize_liq_side({"side": "s"}, {}) is None
    assert normalize_liq_side({}, {}) is None
    assert normalize_liq_side({"side": ""}, {"S": None}) is None
    assert normalize_liq_side({"side": "garbage"}, {}) is None


def test_unknown_qty_returns_none_not_zero() -> None:
    assert liq_contract_units({}, {}) is None
    assert liq_contract_units({"contracts": 0}, {"q": "0"}) is None
    assert liq_contract_units({"contracts": "junk"}, {}) is None


# --- Binance (primary tape) must be BYTE-IDENTICAL to the pre-fix behaviour ------

def test_binance_side_and_qty_unchanged() -> None:
    item = _binance_force_order("SELL")
    info = item["info"]
    # SELL forceOrder -> long-liq bucket downstream (`side == "BUY"` -> short, else long).
    assert normalize_liq_side(item, info) == "SELL"
    # Resolved from raw `q` (ORIGINAL qty) exactly as before — deliberately NOT from
    # the unified `contracts`, which ccxt maps to `l` (LAST FILLED qty).
    assert liq_contract_units(item, info) == 1.437
    assert liq_contract_size(item, None) == 1.0  # USDⓈ-M linear -> multiply is a no-op
    assert liq_price(item, info) == 34959.70     # `ap`, as before


def test_binance_original_qty_wins_over_last_filled() -> None:
    # Partially-filled forceOrder: q=1.437 but l=0.5. The old chain used q; preferring
    # the unified `contracts` (=l) would silently under-count the primary tape.
    item = _binance_force_order("SELL")
    item["info"]["l"] = "0.5"
    item["contracts"] = 0.5
    assert liq_contract_units(item, item["info"]) == 1.437


def test_binance_buy_is_short_liquidation() -> None:
    assert normalize_liq_side(_binance_force_order("BUY"), _binance_force_order("BUY")["info"]) == "BUY"


# --- end-to-end through the real _record_liquidation ----------------------------


class _StubExchange:
    """Minimal ccxt-pro stand-in: unified symbol -> binance id + market metadata."""

    def __init__(self, contract_size: float = 1.0) -> None:
        self._contract_size = contract_size
        self.markets: dict[str, Any] = {}

    def market(self, symbol: str) -> dict[str, Any]:
        return {"contractSize": self._contract_size}


def _streams(monkeypatch: Any, binance_id: str) -> Any:
    from hunt_core.market import streams as streams_mod

    st = streams_mod.HuntCcxtStreams(client=None)  # type: ignore[arg-type]
    st._symbols = {binance_id}
    monkeypatch.setattr(
        streams_mod.HuntCcxtStreams, "_ws_binance_id", staticmethod(lambda ex, raw: binance_id)
    )
    # The map store is a live singleton; the buffers are what we assert on.
    monkeypatch.setattr(
        "hunt_core.maps.engine.get_map_store", lambda: type("_S", (), {"record_liquidation": lambda *a, **k: None})()
    )
    return st


def test_record_liquidation_books_bybit_event_with_correct_side(monkeypatch: Any) -> None:
    """THE regression: bybit produced ZERO events, and would have been mis-sided.

    Bybit "Buy" liquidates a SHORT => side must reach the buffer as "BUY" so the
    downstream `side == "BUY" -> short` bucket books it as a short-liquidation.
    """
    st = _streams(monkeypatch, "ROSEUSDT")
    st._record_liquidation(_bybit_all_liquidation("Buy"), exchange=_StubExchange(), venue="bybit")

    buf = st.liquidation_buffers()["bybit"]
    assert len(buf) == 1, "bybit event was dropped (qty resolved to 0)"
    ts_ms, sym, side, qty, price = buf[0]
    assert sym == "ROSEUSDT"
    assert side == "BUY"          # was "S" -> silently bucketed LONG
    assert qty == 20000.0         # was 0.0
    assert price == 0.04499
    # Secondary venues must NEVER land in the primary binance buffer.
    assert len(st.liquidation_buffers()["binance"]) == 0


def test_record_liquidation_scales_okx_contracts(monkeypatch: Any) -> None:
    st = _streams(monkeypatch, "IOSTUSDT")
    st._record_liquidation(_okx_liquidation(), exchange=_StubExchange(0.01), venue="okx")

    buf = st.liquidation_buffers()["okx"]
    assert len(buf) == 1
    _ts, _sym, side, qty, _px = buf[0]
    assert side == "BUY"                  # detail side=buy / posSide=short
    assert abs(qty - 0.13) < 1e-9         # 13 contracts × 0.01, not 13


def test_record_liquidation_skips_unknown_side(monkeypatch: Any) -> None:
    # Never fabricate a direction: an unparseable side must drop the event, not
    # silently book a long-liquidation.
    st = _streams(monkeypatch, "ROSEUSDT")
    item = _bybit_all_liquidation("Buy")
    item["info"]["S"] = "???"
    st._record_liquidation(item, exchange=_StubExchange(), venue="bybit")
    assert st.liquidation_buffers().get("bybit") is None


def test_record_liquidation_binance_path_unchanged(monkeypatch: Any) -> None:
    st = _streams(monkeypatch, "BTCUSDT")
    st._record_liquidation(_binance_force_order("SELL"), exchange=_StubExchange(), venue="binance")

    buf = st.liquidation_buffers()["binance"]
    assert len(buf) == 1
    _ts, sym, side, qty, price = buf[0]
    assert (sym, side, qty, price) == ("BTCUSDT", "SELL", 1.437, 34959.70)
