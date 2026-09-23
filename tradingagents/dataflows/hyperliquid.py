"""HyperLiquid perp market data vendor (OHLCV and locally computed indicators).

Uses the public ``/info`` endpoint, so no API key is needed. Candles are the
perp's daily bars on UTC day boundaries; each row's ``Date`` is the UTC day the
bar opened. The current day's bar is still forming: it is kept (it carries the
latest traded price, which is what a live decision is made against) but flagged
as in-progress everywhere it is reported, so an agent never reads its ``Close``
as a settled close.

Symbols resolve against the live perp universe, so every listed coin works
without a hand-kept table: ``BTC``, ``btc``, ``BTC-USD``, ``BTCUSDT``,
``BTC-PERP`` and ``BTC/USDC:USDC`` all resolve to ``BTC``, and case-sensitive
names such as ``kPEPE`` resolve from any casing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Annotated, Any

import pandas as pd
import requests
from stockstats import wrap

from .errors import NoMarketDataError, VendorRateLimitError
from .stockstats_utils import _assert_ohlcv_not_stale
from .y_finance import INDICATOR_DESCRIPTIONS

logger = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

_REQUEST_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.0

# Daily bars requested per coin; the API caps a snapshot at 5000 candles.
_HISTORY_DAYS = 5 * 365

# Crypto trades every day, so a latest bar more than a few days before the
# requested date means a delisted coin or a broken response, not a weekend.
_MAX_STALE_DAYS = 3

# One analysis calls the price tools a dozen times for the same coin within a
# minute or two. Candles are reused for this long, so a run sees one consistent
# series while a live price is at most this old.
_CANDLE_TTL_SECONDS = 60
_UNIVERSE_TTL_SECONDS = 3600

# Suffixes other venues and the pipeline put after the coin, stripped one at a
# time so compound forms like ``BTC-USD-PERP`` resolve too. Longest first
# within each family so ``USDT``/``USDC`` match before ``USD``.
_SYMBOL_SUFFIXES = (
    "/USDC:USDC", "/USDT:USDT", "/USDC", "/USDT", "/USD",
    "-PERP", "PERP",
    "-USDC", "-USDT", "-USD", "USDC", "USDT", "USD",
)

_candle_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_universe_cache: tuple[float, dict[str, str]] | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _post(payload: dict[str, Any]) -> Any:
    """POST to the info endpoint, retrying throttles and transient outages.

    A throttle, a 5xx or an unreachable host is a fact about the vendor, not the
    coin, so it surfaces as ``VendorRateLimitError`` for the router to report as
    unavailable. Callers only send coins already validated against the universe,
    which is why a 5xx is read as an outage rather than an unknown coin.
    """
    last_problem = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = requests.post(HL_INFO_URL, json=payload, timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            last_problem = f"unreachable ({type(exc).__name__})"
        else:
            if response.status_code == 429 or response.status_code >= 500:
                last_problem = f"HTTP {response.status_code}"
            else:
                response.raise_for_status()
                return response.json()
        if attempt < _MAX_ATTEMPTS - 1:
            delay = _RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
            logger.warning("HyperLiquid %s, retrying in %.0fs", last_problem, delay)
            time.sleep(delay)
    raise VendorRateLimitError(f"HyperLiquid info API failed: {last_problem}")


def _perp_universe() -> dict[str, str]:
    """Map each listed perp's upper-cased name to its exact (case-sensitive) name.

    Delisted coins stay in the map: their history is still served, and the
    staleness check reports them as no longer trading.
    """
    global _universe_cache
    now = time.monotonic()
    if _universe_cache is not None and now - _universe_cache[0] < _UNIVERSE_TTL_SECONDS:
        return _universe_cache[1]
    meta = _post({"type": "meta"})
    universe = {asset["name"].upper(): asset["name"] for asset in meta.get("universe", [])}
    _universe_cache = (now, universe)
    return universe


def _symbol_candidates(raw: str) -> list[str]:
    """The raw symbol, then each form left by stripping one more known suffix."""
    current = raw.strip().rstrip("+").upper()
    candidates = [current]
    stripped = True
    while stripped:
        stripped = False
        for suffix in _SYMBOL_SUFFIXES:
            if current.endswith(suffix) and len(current) > len(suffix):
                current = current[: -len(suffix)]
                candidates.append(current)
                stripped = True
                break
    return candidates


def resolve_hl_coin(symbol: str) -> str:
    """Resolve a user/pipeline symbol to the exact HyperLiquid perp coin name.

    Raises ``NoMarketDataError`` when no form of the symbol is a listed perp, so
    the router reports the symbol as unavailable instead of pricing something
    else (a stock ticker such as ``NVDA`` lands here).
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise NoMarketDataError(str(symbol), None, "empty symbol")
    universe = _perp_universe()
    for candidate in _symbol_candidates(symbol):
        if candidate in universe:
            return universe[candidate]
    raise NoMarketDataError(symbol, None, "not a listed HyperLiquid perp")


def _fetch_daily_candles(coin: str) -> pd.DataFrame:
    """All daily candles for ``coin`` up to now, cached for ``_CANDLE_TTL_SECONDS``.

    Columns: Date (naive UTC day), Open, High, Low, Close, Volume (base asset),
    Trades, InProgress (the bar's close time is still in the future).
    """
    now = time.monotonic()
    cached = _candle_cache.get(coin)
    if cached is not None and now - cached[0] < _CANDLE_TTL_SECONDS:
        return cached[1]

    end_ms = _now_ms()
    start_ms = end_ms - _HISTORY_DAYS * 86_400_000
    candles = _post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d", "startTime": start_ms, "endTime": end_ms},
    })
    if not candles:
        raise NoMarketDataError(coin, coin, "no daily candles returned")

    raw = pd.DataFrame(candles)
    frame = pd.DataFrame({
        # UTC explicitly: a local-time conversion would shift every bar a day
        # for users west of UTC.
        "Date": pd.to_datetime(raw["t"], unit="ms", utc=True).dt.tz_localize(None).dt.normalize(),
        "Open": pd.to_numeric(raw["o"], errors="coerce"),
        "High": pd.to_numeric(raw["h"], errors="coerce"),
        "Low": pd.to_numeric(raw["l"], errors="coerce"),
        "Close": pd.to_numeric(raw["c"], errors="coerce"),
        "Volume": pd.to_numeric(raw["v"], errors="coerce"),
        "Trades": pd.to_numeric(raw["n"], errors="coerce").fillna(0).astype(int),
        "InProgress": raw["T"].astype("int64") >= end_ms,
    })
    # Bars with no trades predate the listing (the API backfills them from the
    # oracle with zero volume); they are not market prices.
    frame = frame[(frame["Trades"] > 0) & frame["Close"].notna()]
    frame = frame.drop_duplicates(subset="Date", keep="last").sort_values("Date").reset_index(drop=True)
    if frame.empty:
        raise NoMarketDataError(coin, coin, "no traded daily candles returned")

    _candle_cache[coin] = (now, frame)
    return frame


def load_hl_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Daily OHLCV for ``symbol`` on or before ``curr_date`` (no look-ahead).

    Returns Date/Open/High/Low/Close/Volume/Trades. ``attrs["in_progress_last_bar"]``
    says whether the last row is the still-forming current-day bar, and
    ``attrs["coin"]`` holds the resolved HyperLiquid coin name.
    """
    coin = resolve_hl_coin(symbol)
    curr_date_dt = pd.to_datetime(curr_date).normalize()
    frame = _fetch_daily_candles(coin)
    data = frame[frame["Date"] <= curr_date_dt].copy()
    if data.empty:
        raise NoMarketDataError(symbol, coin, f"no daily candles on or before {curr_date}")
    _assert_ohlcv_not_stale(data, curr_date, symbol, coin, max_stale_days=_MAX_STALE_DAYS)

    in_progress = bool(data["InProgress"].iloc[-1])
    data = data.drop(columns=["InProgress"]).reset_index(drop=True)
    data.attrs["in_progress_last_bar"] = in_progress
    data.attrs["coin"] = coin
    return data


def in_progress_note(data: pd.DataFrame) -> str:
    """One line warning that the last row is still forming, or "" when settled."""
    if not data.attrs.get("in_progress_last_bar"):
        return ""
    day = data["Date"].iloc[-1]
    closes_at = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return (
        f"The {day.strftime('%Y-%m-%d')} row is the in-progress daily candle: its Close is "
        f"the latest traded price, not a settled close (the candle closes {closes_at} "
        f"00:00 UTC). Indicator values for that day move until then."
    )


def _label(symbol: str, coin: str) -> str:
    return coin if symbol.strip() == coin else f"{coin} (from {symbol})"


def get_hl_stock_data(
    symbol: Annotated[str, "ticker symbol of the asset"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """HyperLiquid perp daily OHLCV between start_date and end_date, inclusive."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    data = load_hl_ohlcv(symbol, end_date)
    coin = data.attrs["coin"]
    note = in_progress_note(data)
    data = data[data["Date"] >= pd.to_datetime(start_date)]
    if data.empty:
        raise NoMarketDataError(symbol, coin, f"no daily candles between {start_date} and {end_date}")

    out = data.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    out["Volume"] = out["Volume"].round(4)

    header = f"# HyperLiquid perp daily OHLCV for {_label(symbol, coin)} from {start_date} to {end_date}\n"
    header += f"# Daily candles on UTC days; Volume is in {coin} (base asset); Trades is the fill count\n"
    if note:
        header += f"# {note}\n"
    header += f"# Total records: {len(out)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + out.to_csv(index=False)


def get_hl_indicators_window(
    symbol: Annotated[str, "ticker symbol of the asset"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """One stockstats indicator per day over the look-back window, from HL candles."""
    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS)}"
        )
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - timedelta(days=look_back_days)

    data = load_hl_ohlcv(symbol, curr_date)
    note = in_progress_note(data)
    df = wrap(data[["Date", "Open", "High", "Low", "Close", "Volume"]].copy())
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]  # trigger stockstats to calculate the indicator
    values = {
        row["Date"]: "N/A" if pd.isna(row[indicator]) else str(row[indicator])
        for _, row in df.iterrows()
    }

    lines = []
    day = curr_date_dt
    while day >= before:
        key = day.strftime("%Y-%m-%d")
        lines.append(f"{key}: {values.get(key, 'N/A: no candle for this day')}")
        day -= timedelta(days=1)

    result = f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
    result += "\n".join(lines) + "\n"
    if note:
        result += f"\nNote: {note}\n"
    return result + "\n\n" + INDICATOR_DESCRIPTIONS[indicator]
