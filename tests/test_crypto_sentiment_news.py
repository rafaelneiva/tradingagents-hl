"""Crypto routing for the sentiment and news analysts (issue #3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

import tradingagents.dataflows.reddit as reddit
from tradingagents.default_config import DEFAULT_CONFIG


@pytest.mark.unit
class TestSubreddits:
    @pytest.mark.parametrize("ticker, expected", [
        ("BTC-USD", ("Bitcoin", "CryptoCurrency", "CryptoMarkets")),
        ("ETHUSDT", ("ethereum", "CryptoCurrency", "CryptoMarkets")),
        ("SOL-USD", ("solana", "CryptoCurrency", "CryptoMarkets")),
        ("BCH-USD", ("CryptoCurrency", "CryptoMarkets")),   # no own community mapped
        ("AAPL", reddit.DEFAULT_SUBREDDITS),
    ])
    def test_crypto_pairs_search_crypto_communities(self, ticker, expected):
        assert reddit.subreddits_for(ticker) == expected

    def test_fetch_defaults_to_the_tickers_subreddits(self, monkeypatch):
        seen = {}

        def fake_fetch(ticker, sub, limit, timeout, _retry=True):
            seen.update(ticker=ticker, sub=sub)
            return []

        monkeypatch.setattr(reddit, "_fetch_subreddit_rss", fake_fetch)
        out = reddit.fetch_reddit_posts("BTC-USD")
        assert seen == {"ticker": "BTC", "sub": "Bitcoin+CryptoCurrency+CryptoMarkets"}
        assert "r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets" in out

    def test_explicit_subreddits_still_win(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(reddit, "_fetch_subreddit_rss",
                            lambda t, sub, *a, **k: seen.setdefault("sub", sub) and [])
        reddit.fetch_reddit_posts("BTC-USD", ("wallstreetbets",))
        assert seen["sub"] == "wallstreetbets"


@pytest.mark.unit
def test_sentiment_prompt_names_the_subreddits_it_searched():
    from tradingagents.agents.analysts.sentiment_analyst import _build_system_message

    msg = _build_system_message(
        ticker="BTC-USD", start_date="2026-09-16", end_date="2026-09-23",
        news_block="n", stocktwits_block="s", reddit_block="r",
        subreddits=reddit.subreddits_for("BTC-USD"),
    )
    assert "### Reddit posts — r/Bitcoin, r/CryptoCurrency, r/CryptoMarkets" in msg
    assert "r/wallstreetbets, r/stocks, r/investing (past" not in msg


@pytest.mark.unit
def test_global_news_queries_cover_the_crypto_market_in_short_terms():
    queries = DEFAULT_CONFIG["global_news_queries"]
    joined = " ".join(queries).lower()
    assert "crypto" in joined and "stablecoin" in joined
    assert all(len(q.split()) <= 2 for q in queries)  # long queries return nothing


@pytest.mark.unit
def test_global_news_interleaves_searches_so_every_topic_appears(monkeypatch):
    import tradingagents.dataflows.yfinance_news as ynews

    def page(tag, n):
        return [{"title": f"{tag} {i}", "publisher": "P", "link": "l",
                 "providerPublishTime": 1790150400} for i in range(n)]  # 2026-09-23

    pages = {"crypto": page("CRYPTO", 8), "macro": page("MACRO", 8)}

    class FakeSearch:
        def __init__(self, query, **kwargs):
            self.news = pages[query]

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    monkeypatch.setattr(ynews, "get_config", lambda: {
        "global_news_lookback_days": 7, "global_news_article_limit": 4,
        "global_news_queries": ["crypto", "macro"],
    })
    out = ynews.get_global_news_yfinance("2026-09-23")
    titles = [line[4:].split(" (source")[0] for line in out.splitlines() if line.startswith("### ")]
    assert titles == ["CRYPTO 0", "MACRO 0", "CRYPTO 1", "MACRO 1"]


def _run_news_analyst(asset_type):
    from tradingagents.agents.analysts.news_analyst import create_news_analyst

    llm = MagicMock()
    llm.bind_tools.return_value = RunnableLambda(lambda _: AIMessage(content="report"))
    captured = {}

    def bind(tools):
        captured["tools"] = [t.name for t in tools]
        return RunnableLambda(lambda prompt: captured.setdefault("prompt", prompt.to_string())
                              and AIMessage(content="report"))

    llm.bind_tools.side_effect = bind
    node = create_news_analyst(llm)
    out = node({"trade_date": "2026-09-23", "company_of_interest": "BTC-USD",
                "asset_type": asset_type, "messages": [("human", "BTC-USD")]})
    assert out["news_report"] == "report"
    return captured


@pytest.mark.unit
class TestNewsAnalyst:
    def test_macro_tool_is_offered_only_with_a_fred_key(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        without = _run_news_analyst("crypto")
        assert "get_macro_indicators" not in without["tools"]
        assert "get_macro_indicators" not in without["prompt"]

        monkeypatch.setenv("FRED_API_KEY", "key")
        with_key = _run_news_analyst("crypto")
        assert "get_macro_indicators" in with_key["tools"]
        assert "get_macro_indicators(indicator, curr_date, look_back_days)" in with_key["prompt"]

    def test_crypto_runs_get_a_crypto_focus_and_a_prediction_market_cap(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        crypto = _run_news_analyst("crypto")["prompt"]
        assert "This is a crypto asset" in crypto
        assert "at most three topics" in crypto
        assert "This is a crypto asset" not in _run_news_analyst("stock")["prompt"]
