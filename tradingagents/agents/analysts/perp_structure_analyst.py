from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_funding_history,
    get_instrument_context_from_state,
    get_language_instruction,
    get_open_interest,
    get_order_book_imbalance,
)


def create_perp_structure_analyst(llm):
    """Perp market structure analyst: funding, open interest and order book.

    Runs in the graph's "fundamentals" slot (wire key and ``fundamentals_report``
    kept for saved-config and upstream compatibility): a HyperLiquid perp has no
    company financials, and its positioning is the closest thing to them.
    """

    def perp_structure_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_funding_history,
            get_open_interest,
            get_order_book_imbalance,
        ]

        system_message = (
            "You are a derivatives market-structure researcher analyzing how a HyperLiquid perpetual"
            " future is positioned. Write a report on who is crowded, how leveraged the market is,"
            " and where liquidity sits, so traders can judge squeeze and liquidation risk alongside"
            " the technical, sentiment and news reports."
            " Call all three tools: `get_funding_history` (use the default 14-day window; widen it"
            " only to put an unusual regime in context), `get_open_interest`, and"
            " `get_order_book_imbalance`."
            " Cover, with the exact figures the tools return:"
            " (1) Funding regime: latest, 24h and window averages, the trend across the daily table,"
            " and whether longs or shorts are crowded. Funding is hourly on HyperLiquid; compare"
            " rates on the 8h equivalent the tools print. The 0.00125%/h baseline is neutral, not"
            " bullish."
            " (2) Positioning and leverage: open interest notional and rank, the OI / 24h volume"
            " ratio, the mark-vs-oracle basis, and whether HyperLiquid's predicted funding diverges"
            " from Binance and Bybit (a divergence points to venue-specific positioning)."
            " (3) Liquidity: spread, near-book imbalance and the largest resting levels, noting that"
            " a book snapshot is a single instant and can be pulled."
            " (4) What the combination implies: crowded funding with heavy OI is squeeze fuel"
            " against the crowded side; neutral funding with thin OI means positioning is not the"
            " driver. State which risks this raises, without turning it into a trade call."
            " Only claim what the tools show. The public API has no long/short account ratio and no"
            " open interest history: say so rather than estimating them. If a live-only tool is"
            " withheld for a past analysis date, report it as unavailable and rely on funding"
            " history."
            " Append a Markdown table at the end summarizing each metric, its value and its reading."
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return perp_structure_analyst_node
