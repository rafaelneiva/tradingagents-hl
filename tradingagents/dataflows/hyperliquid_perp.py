"""HyperLiquid perp market structure: funding, open interest, order book.

Replaces company fundamentals for this fork. Everything comes from the public
``/info`` endpoint (no key) and works for any listed perp. Interpretation
thresholds follow the Aethron bot's ``indicators.js`` so both tools read the
market the same way:

- funding is hourly on HyperLiquid; it is also shown as the 8h equivalent (the
  convention most venues and traders quote) and annualized. 8h-equivalent above
  +0.03% is crowded long, above +0.05% extreme; the mirror for shorts.
- order book imbalance is (bid - ask) / (bid + ask) over the top 10 levels'
  notional; beyond +/-0.15 one side dominates.

Funding history is historical, so it is served point-in-time. Open interest,
the perp context and the order book exist only as live snapshots, so a run
dated in the past gets a withheld notice instead of today's values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import pandas as pd

from .errors import NoMarketDataError
from .hyperliquid import _now_ms, _post, resolve_hl_coin
from .utils import get_current_date

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# HyperLiquid's funding always carries a fixed interest component of 0.01% per
# 8h (0.00125% per hour); a rate at this level means a premium of ~zero.
BASELINE_HOURLY_FUNDING = 0.0000125

# Aethron thresholds (indicators.js), on the 8h-equivalent rate in percent.
FUNDING_8H_HIGH_PCT = 0.03
FUNDING_8H_EXTREME_PCT = 0.05
BOOK_IMBALANCE_THRESHOLD = 0.15
BOOK_IMBALANCE_LEVELS = 10

# fundingHistory returns at most this many hourly rows per request.
_FUNDING_PAGE_ROWS = 500
_MAX_FUNDING_LOOKBACK_DAYS = 90

# Venue names as predictedFundings spells them.
_VENUE_LABELS = {"HlPerp": "HyperLiquid", "BinPerp": "Binance", "BybitPerp": "Bybit"}


def _pct(value: float, digits: int = 4) -> str:
    return f"{value * 100:+.{digits}f}%"


def _funding_bias(hourly_rate: float) -> str:
    """Aethron's funding regime label for an hourly rate."""
    pct_8h = hourly_rate * 8 * 100
    if pct_8h > FUNDING_8H_EXTREME_PCT:
        return "EXTREME long crowding (longs pay heavily)"
    if pct_8h > FUNDING_8H_HIGH_PCT:
        return "HIGH, slightly crowded long"
    if pct_8h < -FUNDING_8H_EXTREME_PCT:
        return "EXTREME short crowding (shorts pay heavily)"
    if pct_8h < -FUNDING_8H_HIGH_PCT:
        return "HIGH, slightly crowded short"
    return "NORMAL"


def _funding_line(hourly_rate: float) -> str:
    return (
        f"{_pct(hourly_rate, 5)}/h = {_pct(hourly_rate * 8)}/8h = "
        f"{_pct(hourly_rate * 24 * 365, 2)} APR"
    )


def _live_only_notice(curr_date: str, what: str, coin: str) -> str | None:
    """Withheld notice for a live-only snapshot in a past-dated run, else None.

    "Today" is either the local or the UTC date, so a run dated today in the
    user's timezone is served even after UTC has rolled over.
    """
    today = min(get_current_date(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if not curr_date or curr_date >= today:
        return None
    return (
        f"# {what} for {coin}\n# Point-in-time as of: {curr_date}\n\n"
        f"WITHHELD: HyperLiquid publishes {what.lower()} only as a live snapshot "
        f"with no history, so today's values would put post-decision information "
        f"into a {curr_date} analysis. Report it as unavailable for this date; "
        f"funding history is available point-in-time."
    )


def _day_end_ms(curr_date: str) -> int:
    """Exclusive end of ``curr_date``'s UTC day, capped at now."""
    end = pd.Timestamp(curr_date, tz="UTC").normalize() + pd.Timedelta(days=1)
    return min(int(end.timestamp() * 1000), _now_ms())


def _funding_rows(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Hourly funding in ``[start_ms, end_ms)``, paging past the per-request cap."""
    rows: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        page = _post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms - 1})
        if not page:
            break
        rows.extend(page)
        last = max(int(r["time"]) for r in page)
        if len(page) < _FUNDING_PAGE_ROWS or last + 1 <= cursor:
            break
        cursor = last + 1
    if not rows:
        return pd.DataFrame(columns=["time", "rate", "premium"])
    frame = pd.DataFrame({
        "time": pd.to_datetime([int(r["time"]) for r in rows], unit="ms", utc=True),
        "rate": pd.to_numeric([r["fundingRate"] for r in rows], errors="coerce"),
        "premium": pd.to_numeric([r.get("premium") for r in rows], errors="coerce"),
    })
    frame = frame.dropna(subset=["rate"]).drop_duplicates(subset="time")
    return frame[frame["time"] < pd.Timestamp(end_ms, unit="ms", tz="UTC")].sort_values("time")


def get_hl_funding_history(
    symbol: Annotated[str, "ticker symbol of the asset"],
    curr_date: Annotated[str, "analysis date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "days of hourly funding to summarize"] = 14,
) -> str:
    """Funding regime over the look-back window, point-in-time as of curr_date."""
    coin = resolve_hl_coin(symbol)
    days = max(1, min(int(look_back_days), _MAX_FUNDING_LOOKBACK_DAYS))
    end_ms = _day_end_ms(curr_date)
    frame = _funding_rows(coin, end_ms - days * DAY_MS, end_ms)
    if frame.empty:
        raise NoMarketDataError(symbol, coin, f"no funding history in the {days} days to {curr_date}")

    latest = frame.iloc[-1]
    last_24h = frame[frame["time"] > latest["time"] - pd.Timedelta(hours=24)]
    lines = [
        f"# HyperLiquid funding for {coin}: {days} days to {curr_date} (hourly settlements, UTC)",
        "# Positive funding: longs pay shorts. Baseline 0.00125%/h (0.01%/8h) is the fixed "
        "interest component, i.e. a premium of about zero.",
        "",
        "## Summary",
        f"- Latest ({latest['time']:%Y-%m-%d %H:%M} UTC): {_funding_line(latest['rate'])} "
        f"-> {_funding_bias(latest['rate'])}",
        f"- Last 24h average: {_funding_line(last_24h['rate'].mean())} -> {_funding_bias(last_24h['rate'].mean())}",
        f"- {days}-day average: {_funding_line(frame['rate'].mean())} -> {_funding_bias(frame['rate'].mean())}",
        f"- {days}-day range (hourly): {_pct(frame['rate'].min(), 5)} to {_pct(frame['rate'].max(), 5)}",
        f"- Hours with positive funding: {(frame['rate'] > 0).sum()} of {len(frame)} "
        f"({(frame['rate'] > 0).mean():.0%}); above baseline: "
        f"{(frame['rate'] > BASELINE_HOURLY_FUNDING).mean():.0%}",
        f"- Cumulative funding over the window: {_pct(frame['rate'].sum(), 3)} of notional "
        f"(paid by longs if positive)",
        f"- Latest premium (mark vs oracle, as used for funding): {_pct(latest['premium'])}",
        "",
        "## Daily",
        "| Date (UTC) | Avg hourly | 8h equiv | APR | Avg premium | Hours > 0 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    daily = frame.groupby(frame["time"].dt.strftime("%Y-%m-%d"))
    for day, group in sorted(daily, reverse=True):
        rate = group["rate"].mean()
        lines.append(
            f"| {day} | {_pct(rate, 5)} | {_pct(rate * 8)} | {_pct(rate * 24 * 365, 2)} | "
            f"{_pct(group['premium'].mean())} | {(group['rate'] > 0).sum()}/{len(group)} |"
        )
    lines += [
        "",
        f"Regime thresholds on the 8h equivalent: beyond +/-{FUNDING_8H_HIGH_PCT}% one side is "
        f"crowded, beyond +/-{FUNDING_8H_EXTREME_PCT}% extreme. Crowded funding is a contrarian "
        "risk signal (squeeze fuel), not a direction call on its own.",
    ]
    return "\n".join(lines)


def _asset_context(coin: str) -> tuple[dict, dict, list[tuple[str, float]]]:
    """``coin``'s universe entry and live context, plus every perp's OI notional."""
    meta, contexts = _post({"type": "metaAndAssetCtxs"})
    universe = meta.get("universe", [])
    names = [asset["name"] for asset in universe]
    if coin not in names:
        raise NoMarketDataError(coin, coin, "no live context for this perp")
    oi_notional = []
    for asset, ctx in zip(universe, contexts, strict=False):
        try:
            oi_notional.append((asset["name"], float(ctx["openInterest"]) * float(ctx["markPx"])))
        except (KeyError, TypeError, ValueError):
            continue
    index = names.index(coin)
    return universe[index], contexts[index], oi_notional


def _predicted_funding_lines(coin: str) -> list[str]:
    """Next funding per venue, normalized to the 8h equivalent for comparison."""
    try:
        venues = dict(_post({"type": "predictedFundings"})).get(coin) or []
    except Exception:  # noqa: BLE001 — cross-venue context is optional
        return ["- Cross-venue predicted funding: unavailable"]
    lines = []
    for venue, info in venues:
        if not info:
            continue
        rate = float(info["fundingRate"])
        interval = float(info.get("fundingIntervalHours") or 1)
        label = _VENUE_LABELS.get(venue, venue)
        lines.append(
            f"  - {label}: {_pct(rate, 5)} per {interval:g}h = {_pct(rate * 8 / interval)}/8h equivalent"
        )
    if not lines:
        return ["- Cross-venue predicted funding: not listed for this coin"]
    return ["- Predicted next funding by venue (8h equivalent for comparison):", *lines]


def get_hl_open_interest(
    symbol: Annotated[str, "ticker symbol of the asset"],
    curr_date: Annotated[str, "analysis date, YYYY-mm-dd"],
) -> str:
    """Live open interest, basis, volume and funding context for the perp."""
    coin = resolve_hl_coin(symbol)
    withheld = _live_only_notice(curr_date, "Open interest and perp context", coin)
    if withheld:
        return withheld

    asset, ctx, oi_notional = _asset_context(coin)
    mark = float(ctx["markPx"])
    oracle = float(ctx["oraclePx"])
    prev_day = float(ctx["prevDayPx"])
    oi = float(ctx["openInterest"])
    oi_usd = oi * mark
    day_ntl = float(ctx["dayNtlVlm"])
    funding = float(ctx["funding"])
    ranked = sorted(oi_notional, key=lambda item: item[1], reverse=True)
    rank = next(i for i, (name, _) in enumerate(ranked, start=1) if name == coin)
    impact_bid, impact_ask = (float(p) for p in ctx.get("impactPxs") or (mark, mark))

    lines = [
        f"# HyperLiquid perp context for {coin} (live snapshot, "
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)",
        "",
        f"- Mark price: {mark:g} | Oracle (spot index): {oracle:g} | "
        f"Mark vs oracle: {_pct(mark / oracle - 1)}",
        f"- 24h change (mark vs previous day): {_pct(mark / prev_day - 1, 2)}",
        f"- Open interest: {oi:,.2f} {coin} = ${oi_usd:,.0f} notional "
        f"(rank #{rank} of {len(ranked)} HyperLiquid perps by OI)",
        f"- 24h notional volume: ${day_ntl:,.0f} | OI / 24h volume: "
        f"{(oi_usd / day_ntl if day_ntl else float('nan')):.2f}x",
        f"- Current funding: {_funding_line(funding)} -> {_funding_bias(funding)}",
        *_predicted_funding_lines(coin),
        f"- Impact prices (fill for a standard-size market order): bid {impact_bid:g} / ask "
        f"{impact_ask:g} ({(impact_ask - impact_bid) / mark * 1e4:.1f} bps apart)",
        f"- Max leverage: {asset.get('maxLeverage', 'N/A')}x"
        + (" | DELISTED: new positions are not allowed" if asset.get("isDelisted") else ""),
        "- Long/short account ratio: not published by HyperLiquid's public API; do not estimate it.",
        "- Open interest history: not published by the public API; only this snapshot exists, "
        "so OI trend claims cannot be made from it.",
        "",
        "Reading: a high OI / volume ratio means positioning is heavy relative to turnover "
        "(stale, leveraged positions that can be forced out); a mark above the oracle with "
        "positive funding means perp longs are paying up versus spot.",
    ]
    return "\n".join(lines)


def _side_notional(levels: list[dict], limit: int | None = None) -> float:
    return sum(float(lv["px"]) * float(lv["sz"]) for lv in levels[:limit])


def _imbalance(bid: float, ask: float) -> float | None:
    total = bid + ask
    return None if total == 0 else (bid - ask) / total


def _imbalance_label(value: float | None) -> str:
    if value is None:
        return "N/A"
    if value > BOOK_IMBALANCE_THRESHOLD:
        return f"{value:+.3f} (bid-heavy)"
    if value < -BOOK_IMBALANCE_THRESHOLD:
        return f"{value:+.3f} (ask-heavy)"
    return f"{value:+.3f} (balanced)"


def get_hl_order_book(
    symbol: Annotated[str, "ticker symbol of the asset"],
    curr_date: Annotated[str, "analysis date, YYYY-mm-dd"],
) -> str:
    """Live L2 book: spread, depth near mid and bid/ask notional imbalance."""
    coin = resolve_hl_coin(symbol)
    withheld = _live_only_notice(curr_date, "Order book", coin)
    if withheld:
        return withheld

    book = _post({"type": "l2Book", "coin": coin})
    bids, asks = (book.get("levels") or [[], []])[:2]
    if not bids or not asks:
        raise NoMarketDataError(symbol, coin, "empty order book")
    best_bid, best_ask = float(bids[0]["px"]), float(asks[0]["px"])
    mid = (best_bid + best_ask) / 2
    taken = pd.Timestamp(int(book.get("time") or _now_ms()), unit="ms", tz="UTC")

    lines = [
        f"# HyperLiquid L2 order book for {coin} (snapshot {taken:%Y-%m-%d %H:%M:%S} UTC, "
        f"{len(bids)} bid / {len(asks)} ask levels)",
        "",
        f"- Best bid {best_bid:g} / best ask {best_ask:g} | mid {mid:g} | spread "
        f"{(best_ask - best_bid) / mid * 1e4:.2f} bps",
        "",
        "| Depth | Bid notional | Ask notional | Imbalance |",
        "|---|---:|---:|---:|",
    ]
    for levels in (5, BOOK_IMBALANCE_LEVELS, None):
        bid, ask = _side_notional(bids, levels), _side_notional(asks, levels)
        label = f"Top {levels} levels" if levels else f"All {max(len(bids), len(asks))} levels"
        lines.append(f"| {label} | ${bid:,.0f} | ${ask:,.0f} | {_imbalance_label(_imbalance(bid, ask))} |")
    # The API returns a fixed number of levels, so on a liquid coin the visible
    # book can end well inside a band; summing it would present an unobserved
    # band as fully measured.
    reach = min(1 - float(bids[-1]["px"]) / mid, float(asks[-1]["px"]) / mid - 1)
    for band in (0.001, 0.005, 0.01):
        if band > reach:
            lines.append(f"| Within {band:.1%} of mid | n/a | n/a | beyond the visible book |")
            continue
        bid = _side_notional([lv for lv in bids if float(lv["px"]) >= mid * (1 - band)])
        ask = _side_notional([lv for lv in asks if float(lv["px"]) <= mid * (1 + band)])
        lines.append(
            f"| Within {band:.1%} of mid | ${bid:,.0f} | ${ask:,.0f} | {_imbalance_label(_imbalance(bid, ask))} |"
        )
    lines.append(f"\nThe visible book reaches about {reach:.2%} from mid on its shallower side; "
                 "wider bands are not observed, not empty.")

    walls = sorted(
        [("bid", lv) for lv in bids] + [("ask", lv) for lv in asks],
        key=lambda side_lv: float(side_lv[1]["px"]) * float(side_lv[1]["sz"]),
        reverse=True,
    )[:3]
    lines += ["", "Largest resting levels:"]
    for side, lv in walls:
        px, notional = float(lv["px"]), float(lv["px"]) * float(lv["sz"])
        lines.append(f"- {side} {px:g} ({_pct(px / mid - 1, 3)} from mid): ${notional:,.0f} "
                     f"from {lv.get('n', '?')} orders")
    lines += [
        "",
        f"Signal convention (Aethron): top-{BOOK_IMBALANCE_LEVELS} imbalance beyond "
        f"+/-{BOOK_IMBALANCE_THRESHOLD} means one side dominates the near book. It is a single "
        "instant and resting orders can be pulled (spoofing), so treat it as short-horizon "
        "context, weaker than funding and open interest.",
    ]
    return "\n".join(lines)
