from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.dataflows.date_window import as_of
from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_funding_history(
    symbol: Annotated[str, "ticker symbol of the asset, e.g. BTC"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    look_back_days: Annotated[int, "days of hourly funding to summarize (1-90)"] = 14,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve the perp's funding rate history: latest, 24h and window averages
    (hourly, 8h-equivalent and APR), crowding regime, cumulative funding and a
    daily table. Point-in-time as of curr_date.
    Uses the configured perp_structure_data vendor.
    Args:
        symbol (str): Ticker symbol of the asset, e.g. BTC, ETH, SOL
        curr_date (str): Current date you are trading at, yyyy-mm-dd
        look_back_days (int): Days of history to summarize, default 14
    Returns:
        str: A formatted funding report
    """
    return route_to_vendor("get_funding_history", symbol, as_of(curr_date, trade_date), look_back_days)


@tool
def get_open_interest(
    symbol: Annotated[str, "ticker symbol of the asset, e.g. BTC"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve the perp's live market context: open interest (size, notional,
    rank), mark vs oracle basis, 24h change and volume, OI/volume ratio,
    current and cross-venue predicted funding, impact prices and max leverage.
    Live snapshot only: withheld for past dates.
    Uses the configured perp_structure_data vendor.
    Args:
        symbol (str): Ticker symbol of the asset, e.g. BTC, ETH, SOL
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted perp context report
    """
    return route_to_vendor("get_open_interest", symbol, as_of(curr_date, trade_date))


@tool
def get_order_book_imbalance(
    symbol: Annotated[str, "ticker symbol of the asset, e.g. BTC"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve the perp's live L2 order book: spread, bid/ask notional and
    imbalance at several depths, and the largest resting levels.
    Live snapshot only: withheld for past dates.
    Uses the configured perp_structure_data vendor.
    Args:
        symbol (str): Ticker symbol of the asset, e.g. BTC, ETH, SOL
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted order book report
    """
    return route_to_vendor("get_order_book_imbalance", symbol, as_of(curr_date, trade_date))
