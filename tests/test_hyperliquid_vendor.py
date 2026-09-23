"""Tests for the HyperLiquid perp price vendor (OHLCV, indicators, snapshot)."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.hyperliquid as hl
import tradingagents.dataflows.market_data_validator as mdv
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.default_config import DEFAULT_CONFIG

DAY_MS = 86_400_000
# Mid-afternoon UTC on the last candle's day, so that candle is still forming.
NOW = pd.Timestamp("2026-09-23 15:00", tz="UTC")
NOW_MS = int(NOW.timestamp() * 1000)

UNIVERSE = [
    {"name": "BTC", "szDecimals": 5},
    {"name": "ETH", "szDecimals": 4},
    {"name": "kPEPE", "szDecimals": 0},
    {"name": "MATIC", "szDecimals": 1, "isDelisted": True},
]


def _candles(first="2026-06-01", last="2026-09-23", base=100.0, step=1.0, pre_listing_bars=3):
    """Daily candles in the API's shape, with zero-trade bars before the listing."""
    out = []
    days = pd.date_range(first, last, freq="D", tz="UTC")
    for i in range(pre_listing_bars):
        t = int(days[0].timestamp() * 1000) - (pre_listing_bars - i) * DAY_MS
        out.append({"t": t, "T": t + DAY_MS - 1, "o": "1", "h": "1", "l": "1", "c": "1",
                    "v": "0.0", "n": 0})
    for i, day in enumerate(days):
        t = int(day.timestamp() * 1000)
        close = base + i * step
        out.append({"t": t, "T": t + DAY_MS - 1, "s": "BTC", "i": "1d",
                    "o": str(close - step / 2), "h": str(close + step), "l": str(close - step),
                    "c": str(close), "v": "1234.56789", "n": 1000 + i})
    return out


@pytest.fixture
def fake_api(monkeypatch):
    """Serve meta and candleSnapshot from memory; record every request."""
    hl._candle_cache.clear()
    hl._universe_cache = None
    state = {"calls": [], "candles": {"BTC": _candles(), "kPEPE": _candles(base=0.004, step=0.00001)}}

    def post(payload):
        state["calls"].append(payload)
        if payload["type"] == "meta":
            return {"universe": UNIVERSE}
        if payload["type"] == "candleSnapshot":
            return state["candles"].get(payload["req"]["coin"], [])
        raise AssertionError(f"unexpected request {payload}")

    monkeypatch.setattr(hl, "_post", post)
    monkeypatch.setattr(hl, "_now_ms", lambda: NOW_MS)
    set_config({"data_vendors": {"core_stock_apis": "hyperliquid",
                                 "technical_indicators": "hyperliquid"}})
    yield state
    hl._candle_cache.clear()
    hl._universe_cache = None


@pytest.mark.unit
class TestDefaults:
    def test_price_categories_default_to_hyperliquid_only(self):
        vendors = DEFAULT_CONFIG["data_vendors"]
        assert vendors["core_stock_apis"] == "hyperliquid"
        assert vendors["technical_indicators"] == "hyperliquid"

    def test_registered_with_the_router(self):
        assert "hyperliquid" in interface.VENDOR_LIST
        assert interface.VENDOR_METHODS["get_stock_data"]["hyperliquid"] is hl.get_hl_stock_data
        assert interface.VENDOR_METHODS["get_indicators"]["hyperliquid"] is hl.get_hl_indicators_window


@pytest.mark.unit
class TestSymbolResolution:
    @pytest.mark.parametrize("raw", [
        "BTC", "btc", " BTC ", "BTC-USD", "BTCUSD", "BTCUSDT", "BTC-USDC",
        "BTC-PERP", "BTC/USDC:USDC", "BTC-USD-PERP", "BTCUSD+",
    ])
    def test_forms_resolve_to_the_coin(self, fake_api, raw):
        assert hl.resolve_hl_coin(raw) == "BTC"

    def test_case_sensitive_names_resolve_from_any_casing(self, fake_api):
        assert hl.resolve_hl_coin("KPEPE") == "kPEPE"
        assert hl.resolve_hl_coin("kpepe-usd") == "kPEPE"

    @pytest.mark.parametrize("raw", ["NVDA", "USD", "", "   "])
    def test_unlisted_symbol_is_no_data(self, fake_api, raw):
        with pytest.raises(NoMarketDataError):
            hl.resolve_hl_coin(raw)

    def test_universe_is_fetched_once(self, fake_api):
        hl.resolve_hl_coin("BTC")
        hl.resolve_hl_coin("ETH")
        assert [c["type"] for c in fake_api["calls"]].count("meta") == 1


@pytest.mark.unit
class TestLoadOhlcv:
    def test_dates_are_utc_days_and_pre_listing_bars_are_dropped(self, fake_api):
        data = hl.load_hl_ohlcv("BTC", "2026-09-23")
        assert data["Date"].iloc[0] == pd.Timestamp("2026-06-01")
        assert data["Date"].iloc[-1] == pd.Timestamp("2026-09-23")
        assert (data["Trades"] > 0).all()
        assert list(data.columns) == ["Date", "Open", "High", "Low", "Close", "Volume", "Trades"]

    def test_current_day_bar_is_flagged_in_progress(self, fake_api):
        data = hl.load_hl_ohlcv("BTC", "2026-09-23")
        assert data.attrs["in_progress_last_bar"] is True
        assert "in-progress daily candle" in hl.in_progress_note(data)

    def test_past_date_has_no_look_ahead_and_a_settled_last_bar(self, fake_api):
        data = hl.load_hl_ohlcv("BTC", "2026-08-15")
        assert data["Date"].max() == pd.Timestamp("2026-08-15")
        assert data.attrs["in_progress_last_bar"] is False
        assert hl.in_progress_note(data) == ""

    def test_candles_are_reused_within_a_run(self, fake_api):
        hl.load_hl_ohlcv("BTC", "2026-09-23")
        hl.load_hl_ohlcv("BTC-USD", "2026-09-01")
        assert [c["type"] for c in fake_api["calls"]].count("candleSnapshot") == 1

    def test_stale_series_is_no_data(self, fake_api):
        fake_api["candles"]["MATIC"] = _candles(first="2026-01-01", last="2026-03-01")
        with pytest.raises(NoMarketDataError, match="stale"):
            hl.load_hl_ohlcv("MATIC", "2026-09-23")

    def test_date_before_listing_is_no_data(self, fake_api):
        with pytest.raises(NoMarketDataError):
            hl.load_hl_ohlcv("BTC", "2025-01-01")

    def test_empty_snapshot_is_no_data(self, fake_api):
        fake_api["candles"]["ETH"] = []
        with pytest.raises(NoMarketDataError):
            hl.load_hl_ohlcv("ETH", "2026-09-23")


@pytest.mark.unit
class TestPost:
    def test_throttle_retries_then_reports_unavailable(self, monkeypatch):
        class Throttled:
            status_code = 429

        calls = []
        monkeypatch.setattr(hl.requests, "post", lambda *a, **k: calls.append(1) or Throttled())
        monkeypatch.setattr(hl.time, "sleep", lambda s: None)
        with pytest.raises(VendorRateLimitError, match="HTTP 429"):
            hl._post({"type": "meta"})
        assert len(calls) == hl._MAX_ATTEMPTS

    def test_unreachable_is_unavailable_not_no_data(self, monkeypatch):
        def boom(*a, **k):
            raise hl.requests.ConnectionError("down")

        monkeypatch.setattr(hl.requests, "post", boom)
        monkeypatch.setattr(hl.time, "sleep", lambda s: None)
        with pytest.raises(VendorRateLimitError, match="unreachable"):
            hl._post({"type": "meta"})


@pytest.mark.unit
class TestGetStockData:
    def test_routes_and_formats_the_requested_window(self, fake_api):
        out = interface.route_to_vendor("get_stock_data", "BTC-USD", "2026-09-20", "2026-09-23")
        assert out.startswith("# HyperLiquid perp daily OHLCV for BTC (from BTC-USD)")
        assert "Volume is in BTC (base asset)" in out
        assert "The 2026-09-23 row is the in-progress daily candle" in out
        assert "# Total records: 4" in out
        body = out.split("\n\n", 1)[1]
        assert body.splitlines()[0] == "Date,Open,High,Low,Close,Volume,Trades"
        assert body.splitlines()[1].startswith("2026-09-20,")

    def test_stock_ticker_returns_the_no_data_sentinel(self, fake_api):
        out = interface.route_to_vendor("get_stock_data", "NVDA", "2026-09-01", "2026-09-23")
        assert out.startswith("NO_DATA_AVAILABLE")
        assert "not a listed HyperLiquid perp" in out

    def test_outage_returns_the_unavailable_sentinel(self, fake_api, monkeypatch):
        def outage(payload):
            raise VendorRateLimitError("HyperLiquid info API failed: HTTP 502")

        monkeypatch.setattr(hl, "_post", outage)
        hl._universe_cache = None
        out = interface.route_to_vendor("get_stock_data", "BTC", "2026-09-01", "2026-09-23")
        assert out.startswith("DATA_UNAVAILABLE")


@pytest.mark.unit
class TestIndicators:
    def test_rsi_window_from_hl_candles(self, fake_api):
        out = interface.route_to_vendor("get_indicators", "BTC", "rsi", "2026-09-23", 5)
        assert out.startswith("## rsi values from 2026-09-18 to 2026-09-23:")
        lines = [line for line in out.splitlines() if line.startswith("2026-09-")]
        assert len(lines) == 6
        assert all("N/A" not in line for line in lines)
        # A strictly rising close series pins RSI at the top of its range.
        assert float(lines[0].split(": ")[1]) > 99
        assert "Note: The 2026-09-23 row is the in-progress daily candle" in out
        assert "RSI: Measures momentum" in out

    def test_unsupported_indicator_raises_value_error(self, fake_api):
        with pytest.raises(ValueError, match="not supported"):
            hl.get_hl_indicators_window("BTC", "made_up", "2026-09-23", 5)


@pytest.mark.unit
class TestVerifiedSnapshot:
    def test_snapshot_prices_from_hyperliquid_when_configured(self, fake_api, monkeypatch):
        monkeypatch.setattr(mdv, "load_ohlcv", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not price a HyperLiquid run from Yahoo")))
        snap = mdv.build_verified_market_snapshot("BTC", "2026-09-23", 3)
        assert "Latest trading row used: 2026-09-23" in snap
        assert "in-progress daily candle" in snap
        latest_close = 100.0 + (pd.Timestamp("2026-09-23") - pd.Timestamp("2026-06-01")).days
        assert f"| Close | {latest_close:.2f} |" in snap

    def test_sub_dollar_prices_keep_their_digits(self, fake_api):
        snap = mdv.build_verified_market_snapshot("kPEPE", "2026-08-15", 3)
        row = snap.split("Latest verified OHLCV row")[1].split("###")[0]
        assert "| Close | 0.00 |" not in row
        assert "| Close | 0.00475 |" in row  # 0.004 + 75 days * 0.00001
        assert mdv._fmt(0.0041234) == "0.0041234"
        assert mdv._fmt(12.345) == "12.35"

    def test_snapshot_keeps_yahoo_when_yahoo_is_configured(self, monkeypatch):
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        frame = pd.DataFrame({
            "Date": pd.date_range("2026-05-01", "2026-05-08", freq="D"),
            "Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10,
        })
        monkeypatch.setattr(mdv, "load_ohlcv", lambda *a, **k: frame)
        monkeypatch.setattr(mdv, "load_hl_ohlcv", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not call HyperLiquid for a Yahoo run")))
        assert "Latest trading row used: 2026-05-08" in mdv.build_verified_market_snapshot(
            "AAPL", "2026-05-08", 3)


@pytest.mark.integration
def test_live_btc_candles_and_rsi():
    """Hits the real public API: no key, real BTC perp candles."""
    hl._candle_cache.clear()
    hl._universe_cache = None
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    data = hl.load_hl_ohlcv("BTC", today)
    assert len(data) > 200
    assert data["Close"].iloc[-1] > 0
    out = hl.get_hl_indicators_window("BTC", "rsi", today, 3)
    assert out.count("N/A") == 0
