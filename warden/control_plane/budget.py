"""The budget ledger.

Two jobs, and the second one is why this exists at all:

1. Stop a runaway agent loop from burning the whole credit balance.
2. Give the fleet a real per-agent cost number, because "what does an incident
   cost" is the first question anyone running agents in production asks.

Token counts are read from Gemini's own `usage_metadata`, never estimated.
That metadata is emitted PER MODEL CALL, so a tool-using turn produces several
events — these are running sums, and reading the last event instead of summing
is the classic way to undercount by 5x.
"""

from __future__ import annotations

from dataclasses import dataclass

from warden.models import AgentManifest, Run, RunStatus

# Rough Vertex list prices, USD per 1M tokens. Kept here rather than fetched so
# the ledger works offline; refresh from the pricing page if it matters.
# The absolute numbers matter less than the relative signal — this exists to
# tell you "you are at 80% of your cap", not to reconcile an invoice.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (input, output)
    "gemini-3.5-flash": (0.075, 0.30),
    "gemini-3.5-flash-lite": (0.04, 0.15),
    "gemini-3.6-flash": (0.10, 0.40),
    "gemini-3.7-flash": (0.15, 0.60),
}
_DEFAULT_PRICE = (0.50, 1.50)


@dataclass(frozen=True)
class BudgetVerdict:
    ok: bool
    reason: str = ""


def estimate_usd(model: str, prompt_tokens: int, output_tokens: int) -> float:
    inp, out = PRICE_PER_MTOK.get(model, _DEFAULT_PRICE)
    return (prompt_tokens / 1_000_000) * inp + (output_tokens / 1_000_000) * out


class BudgetLedger:
    """Per-run enforcement. One instance per agent run."""

    def __init__(self, run: Run, manifest: AgentManifest) -> None:
        self._run = run
        self._spec = manifest.spec.budget
        self._agent = manifest.name

    @property
    def run(self) -> Run:
        return self._run

    def add_usage(self, prompt: int, candidates: int, total: int) -> None:
        """Accumulate one model call's usage. Call for every event that has it."""
        self._run.prompt_tokens += prompt or 0
        self._run.candidates_tokens += candidates or 0
        self._run.total_tokens += total or 0

    def add_tool_call(self) -> None:
        self._run.tool_calls += 1

    def check(self) -> BudgetVerdict:
        """Called by the proxy before every dispatch."""
        if self._run.tool_calls >= self._spec.max_tool_calls:
            return BudgetVerdict(
                False,
                f"agent {self._agent!r} reached maxToolCalls="
                f"{self._spec.max_tool_calls} for this run",
            )
        if self._run.total_tokens >= self._spec.max_tokens_per_run:
            return BudgetVerdict(
                False,
                f"agent {self._agent!r} reached maxTokensPerRun="
                f"{self._spec.max_tokens_per_run} ({self._run.total_tokens} used)",
            )
        return BudgetVerdict(True)

    def exceeded(self) -> None:
        self._run.status = RunStatus.budget_exceeded

    def cost_usd(self) -> float:
        return estimate_usd(self._run.model, self._run.prompt_tokens, self._run.candidates_tokens)
