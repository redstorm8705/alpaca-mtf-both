"""Shared instrument classifications used at tier safety boundaries.

This module deliberately has no dependency on a strategy module. Long-horizon
tiers must reject structurally incompatible products without importing Day-tier
routing code or inheriting its runtime state.

Signed: OpenAI Codex, 2026-10-15.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class InstrumentPolicy:
    kind: str
    leverage: str
    inverse: bool
    source: str
    as_of: str


_AS_OF = "2026-10-15"
_SOURCE = "Alpaca asset metadata plus issuer product descriptions"


def _policy(
    kind: str, leverage: str = "1x", inverse: bool = False
) -> InstrumentPolicy:
    return InstrumentPolicy(kind, leverage, inverse, _SOURCE, _AS_OF)


# This registry is an admission list, not only a denylist. A new F6 candidate is
# unknown until its product structure is reviewed and entered here. That makes a
# missing classification fail closed instead of silently treating an ETP as stock.
INSTRUMENT_POLICIES: dict[str, InstrumentPolicy] = {
    # Current F6 candidates.
    **{
        s: _policy("equity")
        for s in ("TSLA", "GOOGL", "AMZN", "CRWD", "META", "NVDA", "MU", "SNDK")
    },
    "DRAM": _policy("unleveraged_etf"),
    "EWY": _policy("unleveraged_etf"),
    # Broad-index and sector leveraged products known to the bot.
    **{s: _policy("leveraged_etf", k) for s, k in {
        "TQQQ": "3x", "SQQQ": "-3x", "SOXL": "3x", "SOXS": "-3x",
    }.items()},
    # Day-tier 2x bull products and alternates.
    **{s: _policy("leveraged_etf", "2x") for s in (
        "TSLL", "NVDL", "METU", "AMDL", "AMZU", "AAPU", "MSFU",
        "GGLL", "AVL", "NFXL", "MUU", "CONL", "PLTU", "SMCL",
        "UBRL", "SNXX", "TSLT", "NVDU", "FBL", "AVGX", "SMCX", "SNDG",
    )},
    # Day-tier 2x inverse products and alternates.
    **{s: _policy("inverse_etf", "-2x", True) for s in (
        "NVD", "TSLQ", "DAMD", "SNDQ", "CONI", "NVDQ", "TSLZ",
    )},
    # Day-tier 1x inverse products. They are not leveraged, but a tactical
    # inverse exposure is incompatible with permanent long-only accumulation.
    **{s: _policy("inverse_etf", "-1x", True) for s in (
        "PLTD", "AVS", "AMZD", "AAPD", "MUD", "MSFD", "GGLS",
        "NFXS", "METD", "AMDD",
    )},
}

LEVERAGED_ETF_TICKERS = frozenset(
    symbol
    for symbol, meta in INSTRUMENT_POLICIES.items()
    if meta.leverage not in {"1x", "-1x"}
)
INVERSE_ETF_TICKERS = frozenset(
    symbol for symbol, meta in INSTRUMENT_POLICIES.items() if meta.inverse
)


def instrument_policy(symbol: object) -> InstrumentPolicy | None:
    return INSTRUMENT_POLICIES.get(str(symbol or "").strip().upper())


def is_leveraged_etf(symbol: object) -> bool:
    """Return whether ``symbol`` is a known >1x daily-reset ETF."""
    return str(symbol or "").strip().upper() in LEVERAGED_ETF_TICKERS


def _mapped_tactical_product(symbol: str) -> bool:
    """Return True when an existing bot map identifies a transformed product.

    The explicit admission registry documents reviewed F6 candidates.  This
    independent veto follows the live Day router and the shared config sets, so
    adding a tactical leveraged/inverse product there automatically bars it here.
    Import failures fail closed because product identity is a money-path fact.
    """
    try:
        import config
        from strategy.day_tier_leverage import exposure_sign

        if symbol in {
            *(str(item).strip().upper() for item in config.LEVERAGED_TICKERS),
            *(str(item).strip().upper() for item in config.LEVERAGED_3X_TICKERS),
        }:
            return True
        configured_bull = getattr(config, "DAYTRADE_LEVERAGED_BULL_MAP", None)
        if isinstance(configured_bull, dict) and symbol in {
            str(item or "").strip().upper()
            for item in configured_bull.values()
            if str(item or "").strip()
        }:
            return True
        underlying, _sign = exposure_sign(symbol, "long")
        return str(underlying or "").strip().upper() != symbol
    except Exception:
        return True


def _asset_value(asset: object, field: str) -> object:
    value = asset.get(field) if isinstance(asset, dict) else getattr(asset, field, None)
    return getattr(value, "value", value)


def f6_live_asset_eligible(symbol: object, asset: object) -> tuple[bool, str]:
    """Validate one synchronous Alpaca asset response at the money boundary.

    Alpaca classifies both stocks and ETFs as ``us_equity``.  The reviewed
    registry supplies product type; the broker response independently proves
    exact symbol identity plus current active/tradable status and vetoes names
    that advertise tactical daily leverage or inverse exposure.
    """
    sym = str(symbol or "").strip().upper()
    if asset is None:
        return False, "Alpaca asset classification is unavailable"
    broker_symbol = str(_asset_value(asset, "symbol") or "").strip().upper()
    if broker_symbol != sym:
        return False, "Alpaca asset identity does not match the order symbol"
    status = str(_asset_value(asset, "status") or "").strip().lower()
    if status != "active":
        return False, "Alpaca asset is not active"
    if _asset_value(asset, "tradable") is not True:
        return False, "Alpaca asset is not tradable"
    asset_class = _asset_value(asset, "asset_class")
    if asset_class is None:
        asset_class = _asset_value(asset, "class")
    asset_class = str(asset_class or "").strip().lower()
    if asset_class != "us_equity":
        return False, "Alpaca asset class is not US equity"
    name = str(_asset_value(asset, "name") or "").strip()
    if not name:
        return False, "Alpaca asset name is missing"
    upper_name = name.upper()
    tactical_terms = (
        " 2X ", " 3X ", " -1X ", " -2X ", " -3X ",
        "LEVERAGED", "INVERSE", "ULTRAPRO", "ULTRASHORT",
        " DAILY BEAR ", " DAILY SHORT ",
    )
    padded_name = f" {upper_name} "
    if any(term in padded_name for term in tactical_terms):
        return False, "Alpaca asset name identifies leveraged/inverse exposure"
    return True, "ok"


def f6_symbol_eligible(symbol: object, universe: object) -> tuple[bool, str]:
    """Fail closed unless a symbol is classified, configured, and F6-compatible."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return False, "blank symbol"
    try:
        if isinstance(universe, (str, bytes)) or not isinstance(universe, Iterable):
            return False, "F6 universe unreadable"
        configured = {
            str(item or "").strip().upper()
            for item in cast(Iterable[object], universe)
            if str(item or "").strip()
        }
    except (TypeError, ValueError):
        return False, "F6 universe unreadable"
    if sym not in configured:
        return False, "symbol is not in FOREVER6_UNIVERSE"
    if _mapped_tactical_product(sym):
        return False, "existing bot map identifies a leveraged/inverse product"
    meta = instrument_policy(sym)
    if meta is None:
        return False, "instrument classification is missing"
    if meta.inverse:
        return False, "inverse ETF is incompatible with F6"
    if meta.kind == "leveraged_etf":
        return False, "leveraged ETF is incompatible with F6"
    if meta.kind not in {"equity", "unleveraged_etf"}:
        return False, f"instrument kind {meta.kind!r} is incompatible with F6"
    return True, "ok"
