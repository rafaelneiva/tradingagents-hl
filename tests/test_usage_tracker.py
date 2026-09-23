"""Per-agent token usage and cost attribution (tradingagents/usage.py)."""

from __future__ import annotations

import json
from typing import Any, TypedDict

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph

from tradingagents.usage import OUTSIDE_GRAPH, UsageTracker, price_for


class FakeChat(BaseChatModel):
    """Chat model that reports fixed usage, like ChatAnthropic does."""

    model_name: str = "claude-sonnet-5"
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model_name}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(
            content="ok",
            usage_metadata={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "input_token_details": {"cache_read": self.cache_read},
            },
            response_metadata={"model_name": self.model_name},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _State(TypedDict):
    n: int


def _two_node_graph(quick, deep):
    def analyst(state):
        quick.invoke("analyze")
        quick.invoke("analyze again")
        return {"n": state["n"] + 1}

    def manager(state):
        deep.invoke("decide")
        return {"n": state["n"] + 1}

    graph = StateGraph(_State)
    graph.add_node("Market Analyst", analyst)
    graph.add_node("Portfolio Manager", manager)
    graph.add_edge(START, "Market Analyst")
    graph.add_edge("Market Analyst", "Portfolio Manager")
    graph.add_edge("Portfolio Manager", END)
    return graph.compile()


@pytest.mark.unit
class TestPricing:
    @pytest.mark.parametrize("model, prices", [
        ("claude-sonnet-5", (2.0, 10.0)),
        ("claude-opus-5", (5.0, 25.0)),
        ("claude-opus-5-5", (4.0, 20.0)),          # not priced as opus-5
        ("claude-haiku-4-5-20251001", (1.0, 5.0)),  # dated snapshot
        ("anthropic.claude-opus-5", (5.0, 25.0)),   # Bedrock prefix
        ("CLAUDE-SONNET-5", (2.0, 10.0)),
    ])
    def test_known_models(self, model, prices):
        assert price_for(model) == prices

    @pytest.mark.parametrize("model", ["gpt-5.6", "", "claude-sonnet-50"])
    def test_unknown_models_are_unpriced(self, model):
        assert price_for(model) is None


@pytest.mark.unit
class TestAttribution:
    def test_calls_are_attributed_to_their_graph_node(self):
        tracker = UsageTracker()
        quick = FakeChat(callbacks=[tracker])
        deep = FakeChat(model_name="claude-opus-5", input_tokens=4000, output_tokens=1000,
                        callbacks=[tracker])
        _two_node_graph(quick, deep).invoke({"n": 0})

        rows = {r["node"]: r for r in tracker.summary()["by_node"]}
        assert list(rows) == ["Market Analyst", "Portfolio Manager"]
        assert rows["Market Analyst"]["calls"] == 2
        assert rows["Market Analyst"]["model"] == "claude-sonnet-5"
        # 2 x (1000 in @ $2 + 200 out @ $10) per MTok
        assert rows["Market Analyst"]["cost_usd"] == pytest.approx(2 * (1000 * 2 + 200 * 10) / 1e6)
        assert rows["Portfolio Manager"]["cost_usd"] == pytest.approx((4000 * 5 + 1000 * 25) / 1e6)
        assert tracker.total_cost() == pytest.approx(0.008 + 0.045)

    def test_a_call_outside_the_graph_is_still_counted(self):
        tracker = UsageTracker()
        FakeChat(callbacks=[tracker]).invoke("reflect")
        assert tracker.summary()["by_node"][0]["node"] == OUTSIDE_GRAPH

    def test_cache_reads_are_billed_at_a_tenth(self):
        tracker = UsageTracker()
        FakeChat(input_tokens=10_000, output_tokens=0, cache_read=8_000, callbacks=[tracker]).invoke("x")
        row = tracker.summary()["by_node"][0]
        assert row["input_tokens"] == 2_000
        assert row["cache_read_tokens"] == 8_000
        assert row["cost_usd"] == pytest.approx((2_000 * 2 + 8_000 * 2 * 0.1) / 1e6)

    def test_unpriced_model_is_counted_but_flagged(self):
        tracker = UsageTracker()
        FakeChat(model_name="gpt-5.6", callbacks=[tracker]).invoke("x")
        total = tracker.summary()["total"]
        assert total["calls"] == 1
        assert total["cost_usd"] == 0
        assert total["unpriced_models"] == ["gpt-5.6"]

    def test_reset_starts_a_new_run(self):
        tracker = UsageTracker()
        FakeChat(callbacks=[tracker]).invoke("x")
        tracker.reset()
        assert tracker.summary()["total"]["calls"] == 0


@pytest.mark.unit
class TestReportFiles:
    def test_write_produces_json_and_markdown(self, tmp_path):
        tracker = UsageTracker()
        quick = FakeChat(callbacks=[tracker])
        deep = FakeChat(model_name="claude-opus-5", input_tokens=4000, output_tokens=1000,
                        callbacks=[tracker])
        _two_node_graph(quick, deep).invoke({"n": 0})

        md_path = tracker.write(tmp_path)
        md = md_path.read_text()
        assert md.startswith("# LLM usage and estimated cost")
        assert "**Total: $0.0530**" in md
        assert "| Portfolio Manager | claude-opus-5 | 1 | 4.0k | 0 | 1.0k | $0.0450 | 85% |" in md
        data = json.loads((tmp_path / "usage.json").read_text())
        assert data["total"]["calls"] == 3
        assert len(data["calls"]) == 3

    def test_graph_save_reports_writes_usage(self, tmp_path):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.usage_tracker = UsageTracker()
        FakeChat(callbacks=[graph.usage_tracker]).invoke("x")
        graph.save_reports({"market_report": "m"}, "BTC", tmp_path / "run")
        assert (tmp_path / "run" / "usage.md").exists()


@pytest.mark.unit
def test_cli_summary_line_names_the_biggest_agents():
    from cli.main import format_usage_summary

    tracker = UsageTracker()
    quick = FakeChat(callbacks=[tracker])
    deep = FakeChat(model_name="claude-opus-5", input_tokens=4000, output_tokens=1000,
                    callbacks=[tracker])
    _two_node_graph(quick, deep).invoke({"n": 0})
    line = format_usage_summary(tracker.summary())
    assert "Estimated LLM cost: $0.053" in line
    assert line.index("Portfolio Manager") < line.index("Market Analyst")
