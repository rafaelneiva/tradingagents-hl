"""Tests for the HyperLiquid perp market structure vendor and its analyst slot."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.hyperliquid as hl
import tradingagents.dataflows.hyperliquid_perp as perp
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.default_config import DEFAULT_CONFIG

HOUR_MS = 3_600_000
NOW = pd.Timestamp("2026-09-23 15:30", tz="UTC")
NOW_MS = int(NOW.timestamp() * 1000)
TODAY = "2026-09-23"

UNIVERSE = [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
    {"name": "MATIC", "szDecimals": 1, "maxLeverage": 20, "isDelisted": True},
]
CONTEXTS = [
    {"funding": "0.0000125", "openInterest": "40000.0", "prevDayPx": "80000.0",
     "dayNtlVlm": "2000000000.0", "premium": "0.0005", "oraclePx": "83900.0",
     "markPx": "84000.0", "midPx": "84000.5", "impactPxs": ["83999.0", "84002.0"]},
    {"funding": "0.00008", "openInterest": "900000.0", "prevDayPx": "3000.0",
     "dayNtlVlm": "900000000.0", "premium": "0.001", "oraclePx": "3100.0",
     "markPx": "3102.0", "midPx": "3102.0", "impactPxs": ["3101.9", "3102.1"]},
    {"funding": "0.0", "openInterest": "0.0", "prevDayPx": "0.2", "dayNtlVlm": "0.0",
     "premium": "0.0", "oraclePx": "0.2", "markPx": "0.2", "midPx": None, "impactPxs": None},
]


def _funding(hours: int, rate: float = 0.0000125, end_ms: int = NOW_MS) -> list[dict]:
    """Hourly funding rows ending at the last full hour before ``end_ms``."""
    last = end_ms - end_ms % HOUR_MS
    return [
        {"coin": "BTC", "fundingRate": str(rate), "premium": "0.0001", "time": last - i * HOUR_MS}
        for i in reversed(range(hours))
    ]


def _book(bid_sizes, ask_sizes, mid=84000.0, tick=1.0):
    bids = [{"px": str(mid - tick * (i + 1)), "sz": str(sz), "n": 3} for i, sz in enumerate(bid_sizes)]
    asks = [{"px": str(mid + tick * (i + 1)), "sz": str(sz), "n": 2} for i, sz in enumerate(ask_sizes)]
    return {"coin": "BTC", "time": NOW_MS, "levels": [bids, asks]}


@pytest.fixture
def fake_api(monkeypatch):
    """Serve the info endpoint from memory, honoring fundingHistory's page cap."""
    hl._candle_cache.clear()
    hl._universe_cache = None
    state = {
        "calls": [],
        "funding": {"BTC": _funding(24 * 14)},
        "book": _book([1.0] * 20, [1.0] * 20),
        "predicted": [["BTC", [
            ["BinPerp", {"fundingRate": "0.0001", "fundingIntervalHours": 8}],
            ["HlPerp", {"fundingRate": "0.0000125", "fundingIntervalHours": 1}],
            ["BybitPerp", {"fundingRate": "0.00005", "fundingIntervalHours": 8}],
        ]]],
    }

    def post(payload):
        state["calls"].append(payload)
        kind = payload["type"]
        if kind == "meta":
            return {"universe": UNIVERSE}
        if kind == "metaAndAssetCtxs":
            return [{"universe": UNIVERSE}, CONTEXTS]
        if kind == "fundingHistory":
            rows = [r for r in state["funding"].get(payload["coin"], [])
                    if payload["startTime"] <= r["time"] <= payload.get("endTime", NOW_MS)]
            return rows[: perp._FUNDING_PAGE_ROWS]
        if kind == "l2Book":
            return state["book"]
        if kind == "predictedFundings":
            return state["predicted"]
        raise AssertionError(f"unexpected request {payload}")

    monkeypatch.setattr(hl, "_post", post)
    monkeypatch.setattr(perp, "_post", post)
    monkeypatch.setattr(hl, "_now_ms", lambda: NOW_MS)
    monkeypatch.setattr(perp, "_now_ms", lambda: NOW_MS)
    monkeypatch.setattr(perp, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(perp, "datetime", _FrozenDatetime)
    set_config({"data_vendors": {"perp_structure_data": "hyperliquid"}})
    yield state
    hl._candle_cache.clear()
    hl._universe_cache = None


class _FrozenDatetime(perp.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.to_pydatetime() if tz else NOW.tz_localize(None).to_pydatetime()


@pytest.mark.unit
class TestRegistration:
    def test_category_defaults_to_hyperliquid(self):
        assert DEFAULT_CONFIG["data_vendors"]["perp_structure_data"] == "hyperliquid"
        for method in ("get_funding_history", "get_open_interest", "get_order_book_imbalance"):
            assert interface.get_category_for_method(method) == "perp_structure_data"
            assert "hyperliquid" in interface.VENDOR_METHODS[method]


@pytest.mark.unit
class TestFundingBias:
    @pytest.mark.parametrize("hourly, label", [
        (0.0000125, "NORMAL"),          # baseline: 0.01%/8h
        (0.00005, "HIGH, slightly crowded long"),     # 0.04%/8h
        (0.0001, "EXTREME long crowding"),            # 0.08%/8h
        (-0.00005, "HIGH, slightly crowded short"),
        (-0.0001, "EXTREME short crowding"),
    ])
    def test_aethron_thresholds_on_the_8h_equivalent(self, hourly, label):
        assert perp._funding_bias(hourly).startswith(label)


@pytest.mark.unit
class TestFundingHistory:
    def test_pages_past_the_500_row_cap(self, fake_api):
        fake_api["funding"]["BTC"] = _funding(24 * 30)
        out = interface.route_to_vendor("get_funding_history", "BTC", TODAY, 30)
        pages = [c for c in fake_api["calls"] if c["type"] == "fundingHistory"]
        assert len(pages) == 2
        assert f"of {24 * 30} " in out

    def test_summary_rates_and_regime(self, fake_api):
        out = interface.route_to_vendor("get_funding_history", "BTC-USD", TODAY, 14)
        assert out.startswith("# HyperLiquid funding for BTC: 14 days to 2026-09-23")
        assert "+0.00125%/h = +0.0100%/8h = +10.95% APR -> NORMAL" in out
        assert "above baseline: 0%" in out
        assert "| 2026-09-23 |" in out

    def test_crowded_longs_are_flagged(self, fake_api):
        fake_api["funding"]["BTC"] = _funding(48, rate=0.0001)
        out = perp.get_hl_funding_history("BTC", TODAY, 2)
        assert "EXTREME long crowding" in out

    def test_past_date_has_no_look_ahead(self, fake_api):
        out = perp.get_hl_funding_history("BTC", "2026-09-20", 2)
        assert "2026-09-21" not in out
        assert "| 2026-09-20 |" in out
        request = next(c for c in fake_api["calls"] if c["type"] == "fundingHistory")
        day_end = int(pd.Timestamp("2026-09-21", tz="UTC").timestamp() * 1000)
        assert request["endTime"] < day_end

    def test_no_rows_is_no_data(self, fake_api):
        fake_api["funding"]["BTC"] = []
        with pytest.raises(NoMarketDataError):
            perp.get_hl_funding_history("BTC", TODAY, 7)

    def test_look_back_is_clamped(self, fake_api):
        out = perp.get_hl_funding_history("BTC", TODAY, 10_000)
        assert f"{perp._MAX_FUNDING_LOOKBACK_DAYS} days to" in out


@pytest.mark.unit
class TestOpenInterest:
    def test_live_context(self, fake_api):
        out = interface.route_to_vendor("get_open_interest", "BTC", TODAY)
        assert "Open interest: 40,000.00 BTC = $3,360,000,000 notional (rank #1 of 3" in out
        assert "24h change (mark vs previous day): +5.00%" in out
        assert "OI / 24h volume: 1.68x" in out
        assert "Binance: +0.01000% per 8h = +0.0100%/8h equivalent" in out
        assert "HyperLiquid: +0.00125% per 1h = +0.0100%/8h equivalent" in out
        assert "Max leverage: 40x" in out
        assert "not published by HyperLiquid's public API" in out

    def test_withheld_for_a_past_date(self, fake_api):
        out = perp.get_hl_open_interest("BTC", "2026-09-01")
        assert "WITHHELD" in out
        assert not any(c["type"] == "metaAndAssetCtxs" for c in fake_api["calls"])

    def test_delisted_coin_says_so(self, fake_api):
        assert "DELISTED" in perp.get_hl_open_interest("MATIC", TODAY)

    def test_missing_cross_venue_data_does_not_fail(self, fake_api):
        fake_api["predicted"] = []
        assert "Cross-venue predicted funding: not listed" in perp.get_hl_open_interest("BTC", TODAY)


@pytest.mark.unit
class TestOrderBook:
    def test_balanced_book(self, fake_api):
        out = interface.route_to_vendor("get_order_book_imbalance", "BTC", TODAY)
        assert "Best bid 83999 / best ask 84001" in out
        top10 = next(line for line in out.splitlines() if line.startswith("| Top 10 levels"))
        assert "(balanced)" in top10

    def test_bid_heavy_book(self, fake_api):
        fake_api["book"] = _book([5.0] * 20, [1.0] * 20)
        out = perp.get_hl_order_book("BTC", TODAY)
        top10 = next(line for line in out.splitlines() if line.startswith("| Top 10 levels"))
        assert "(bid-heavy)" in top10
        assert "- bid " in out.split("Largest resting levels:")[1]

    def test_bands_beyond_the_visible_book_are_flagged(self, fake_api):
        out = perp.get_hl_order_book("BTC", TODAY)
        assert "The visible book reaches about 0.02% from mid" in out
        # 20 levels one tick apart around 84000 end ~0.024% out: every band is wider.
        assert "| Within 0.1% of mid | n/a | n/a | beyond the visible book |" in out

    def test_bands_inside_the_visible_book_are_measured(self, fake_api):
        fake_api["book"] = _book([1.0] * 20, [1.0] * 20, tick=100.0)  # reaches ~2.4%
        out = perp.get_hl_order_book("BTC", TODAY)
        assert "| Within 1.0% of mid | $" in out

    def test_withheld_for_a_past_date(self, fake_api):
        assert "WITHHELD" in perp.get_hl_order_book("BTC", "2026-09-01")

    def test_empty_book_is_no_data(self, fake_api):
        fake_api["book"] = {"coin": "BTC", "time": NOW_MS, "levels": [[], []]}
        with pytest.raises(NoMarketDataError):
            perp.get_hl_order_book("BTC", TODAY)


@pytest.mark.unit
class TestLiveWindow:
    def test_local_today_counts_as_live_after_utc_rollover(self, monkeypatch):
        # 22:00 in Sao Paulo is already tomorrow in UTC; a run dated with the
        # local day must still get live data.
        monkeypatch.setattr(perp, "get_current_date", lambda: "2026-09-23")

        class LateUtc(perp.datetime):
            @classmethod
            def now(cls, tz=None):
                return pd.Timestamp("2026-09-24 01:00", tz="UTC").to_pydatetime()

        monkeypatch.setattr(perp, "datetime", LateUtc)
        assert perp._live_only_notice("2026-09-23", "Order book", "BTC") is None
        assert perp._live_only_notice("2026-09-22", "Order book", "BTC") is not None


@pytest.mark.unit
class TestAnalystSlot:
    def test_fundamentals_slot_runs_the_perp_structure_analyst(self):
        from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

        spec = ANALYST_NODE_SPECS["fundamentals"]
        assert spec.agent_node == "Perp Structure Analyst"
        assert spec.report_key == "fundamentals_report"

    def test_graph_wires_perp_tools_into_the_slot(self, mock_llm_client):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        nodes = TradingAgentsGraph._create_tool_nodes(graph)
        assert set(nodes["fundamentals"].tools_by_name) == {
            "get_funding_history", "get_open_interest", "get_order_book_imbalance",
        }

    def test_analyst_binds_perp_tools_and_writes_the_slot_report(self):
        from unittest.mock import MagicMock

        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        from tradingagents.agents import create_perp_structure_analyst

        llm = MagicMock()
        # A runnable so ``prompt | llm.bind_tools(...)`` composes.
        llm.bind_tools.return_value = RunnableLambda(lambda _: AIMessage(content="Funding is neutral."))

        node = create_perp_structure_analyst(llm)
        out = node({"trade_date": TODAY, "company_of_interest": "BTC", "asset_type": "crypto",
                    "messages": [("human", "BTC")]})
        tools = [t.name for t in llm.bind_tools.call_args.args[0]]
        assert tools == ["get_funding_history", "get_open_interest", "get_order_book_imbalance"]
        assert out["fundamentals_report"] == "Funding is neutral."

    def test_old_company_fundamentals_analyst_is_gone(self):
        import importlib.util

        assert importlib.util.find_spec("tradingagents.agents.analysts.fundamentals_analyst") is None


@pytest.mark.integration
def test_live_perp_structure_for_btc():
    """Hits the real public API: funding, OI and book for the BTC perp."""
    hl._candle_cache.clear()
    hl._universe_cache = None
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    assert "Daily" in perp.get_hl_funding_history("BTC", today, 3)
    assert "Open interest:" in perp.get_hl_open_interest("BTC", today)
    assert "| Top 10 levels |" in perp.get_hl_order_book("BTC", today)
