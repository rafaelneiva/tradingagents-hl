"""Per-agent LLM token usage and estimated cost for a run.

``UsageTracker`` is a LangChain callback attached to every LLM the graph
builds. Each call is attributed to the LangGraph node that made it (read from
the run metadata LangGraph propagates), so a report can show which agent spent
what. Cost is an estimate from list prices; models without a price entry are
counted in tokens and reported as unpriced.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

# USD per million tokens: (input, output). Anthropic first-party list prices.
# Output includes thinking tokens, which are billed even when not displayed.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Prompt-cache multipliers on the input price (5-minute cache writes).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# Calls made outside any graph node (reflection, signal extraction).
OUTSIDE_GRAPH = "(outside graph)"


@dataclass
class CallUsage:
    node: str
    model: str
    input_tokens: int = 0          # uncached input
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0         # includes thinking

    @property
    def cost(self) -> float | None:
        prices = price_for(self.model)
        if prices is None:
            return None
        inp, out = prices
        return (
            self.input_tokens * inp
            + self.cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * inp * CACHE_READ_MULTIPLIER
            + self.output_tokens * out
        ) / 1_000_000


def price_for(model: str) -> tuple[float, float] | None:
    """List price for ``model``, tolerating provider prefixes and date suffixes."""
    if not model:
        return None
    name = model.lower().rsplit("/", 1)[-1].removeprefix("anthropic.")
    # Longest key first so "claude-opus-5-5" is not priced as "claude-opus-5".
    for key in sorted(MODEL_PRICES_PER_MTOK, key=len, reverse=True):
        if name == key or name.startswith(key + "-") or name.startswith(key + "@"):
            return MODEL_PRICES_PER_MTOK[key]
    return None


def _usage_from(message: AIMessage) -> dict[str, int]:
    """Split a message's usage into uncached input, cache reads/writes and output."""
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_write = int(details.get("cache_creation") or 0)
    total_in = int(usage.get("input_tokens") or 0)
    return {
        # LangChain reports input_tokens inclusive of cache reads and writes.
        "input_tokens": max(0, total_in - cache_read - cache_write),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


class UsageTracker(BaseCallbackHandler):
    """Record every LLM call's tokens, keyed by graph node and model."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._pending: dict[Any, tuple[str, str]] = {}
        self.calls: list[CallUsage] = []

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        node = (metadata or {}).get("langgraph_node") or OUTSIDE_GRAPH
        params = kwargs.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or ""
        with self._lock:
            self._pending[run_id] = (node, model)

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        with self._lock:
            node, model = self._pending.pop(run_id, (OUTSIDE_GRAPH, ""))
        try:
            message = response.generations[0][0].message
        except (IndexError, AttributeError, TypeError):
            return
        if not isinstance(message, AIMessage):
            return
        meta = message.response_metadata or {}
        model = meta.get("model_name") or meta.get("model") or model
        call = CallUsage(node=node, model=model, **_usage_from(message))
        with self._lock:
            self.calls.append(call)

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self._lock:
            self._pending.pop(run_id, None)

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self.calls.clear()

    def summary(self) -> dict[str, Any]:
        """Per-node rows in first-call order, plus totals."""
        with self._lock:
            calls = list(self.calls)
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for call in calls:
            row = rows.setdefault((call.node, call.model), {
                "node": call.node, "model": call.model, "calls": 0,
                "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "output_tokens": 0, "cost_usd": 0.0, "priced": True,
            })
            row["calls"] += 1
            for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
                row[key] += getattr(call, key)
            if call.cost is None:
                row["priced"] = False
            else:
                row["cost_usd"] += call.cost
        ordered = list(rows.values())
        total = {
            key: sum(r[key] for r in ordered)
            for key in ("calls", "input_tokens", "cache_read_tokens", "cache_write_tokens",
                        "output_tokens", "cost_usd")
        }
        total["unpriced_models"] = sorted({r["model"] or "(unknown)" for r in ordered if not r["priced"]})
        return {"by_node": ordered, "total": total, "calls": [asdict(c) for c in calls]}

    def total_cost(self) -> float:
        return self.summary()["total"]["cost_usd"]

    def write(self, save_path) -> Path:
        """Write ``usage.json`` and ``usage.md`` into ``save_path``; return the md path."""
        save_path = Path(save_path)
        save_path.mkdir(parents=True, exist_ok=True)
        data = self.summary()
        (save_path / "usage.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        md_path = save_path / "usage.md"
        md_path.write_text(render_usage_markdown(data), encoding="utf-8")
        return md_path


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render_usage_markdown(data: dict[str, Any]) -> str:
    total = data["total"]
    lines = [
        "# LLM usage and estimated cost",
        "",
        f"**Total: ${total['cost_usd']:.4f}** over {total['calls']} LLM calls "
        f"({_tok(total['input_tokens'] + total['cache_read_tokens'] + total['cache_write_tokens'])} "
        f"input, {_tok(total['output_tokens'])} output).",
        "",
        "| Agent | Model | Calls | Input | Cache read | Output | Cost | Share |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in data["by_node"]:
        share = row["cost_usd"] / total["cost_usd"] if total["cost_usd"] else 0
        cost = f"${row['cost_usd']:.4f}" if row["priced"] else "unpriced"
        lines.append(
            f"| {row['node']} | {row['model'] or '?'} | {row['calls']} | {_tok(row['input_tokens'])} | "
            f"{_tok(row['cache_read_tokens'])} | {_tok(row['output_tokens'])} | {cost} | {share:.0%} |"
        )
    lines += [
        "",
        "Estimate from list prices (USD per million tokens); output includes thinking tokens, "
        "which are billed even though they do not appear in the reports.",
    ]
    if total["unpriced_models"]:
        lines.append(f"No price entry for: {', '.join(total['unpriced_models'])}; their cost is not included.")
    return "\n".join(lines) + "\n"
