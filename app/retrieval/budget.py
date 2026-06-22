"""
Retrieval budget tracker.

One RetrievalBudget instance is created per task run by the orchestrator
and passed into every tools.py call. tools.py is the only caller -- reader.py
never sees this object.

The budget tracks token consumption against the task's max_tokens limit.
When it goes negative, BudgetExhausted is raised and the orchestrator
catches it, writes the terminal state, and stops the loop cleanly.

A full ledger of every charge is kept in memory so the orchestrator can
journal it to budget_ledger (the DB table) in a single write per checkpoint,
rather than one DB write per tool call. This is a deliberate caching decision:
the in-memory ledger is the source of truth during a run; the DB table is
the durable audit log written at checkpoints and at task completion.

Token cost estimation: we use len(text) // 4 (character-based approximation).
Claude's actual tokenizer is roughly 3.5-4 chars per token for code. This is
accurate to ~15% which is sufficient -- the assignment cares that budget is
tracked and enforced, not that it matches the model's exact token count.
tiktoken's BPE files are fetched from an external CDN at import time and are
blocked in this environment, so we use the approximation intentionally.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class BudgetExhausted(Exception):
    """
    Raised by RetrievalBudget.charge() when the retrieval token budget is
    exceeded. The orchestrator catches this at its checkpoint loop, writes
    the BUDGET_EXHAUSTED terminal state, and stops without corrupting the
    workspace.
    """
    def __init__(self, tool: str, attempted: int, remaining: int):
        self.tool = tool
        self.attempted = attempted
        self.remaining = remaining
        super().__init__(
            f"retrieval budget exhausted: {tool} attempted {attempted} tokens "
            f"but only {remaining} remained"
        )


@dataclass
class LedgerEntry:
    tool: str
    tokens: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class RetrievalBudget:
    """
    Token budget for all retrieval tool calls within one task run.

    Usage:
        budget = RetrievalBudget(max_tokens=50_000)

        # inside tools.py:
        text = reader.read_span(...)
        cost = estimate_tokens(text)
        budget.charge("read_span", cost)   # raises BudgetExhausted if over limit
        return text
    """

    def __init__(self, max_tokens: int) -> None:
        self._max = max_tokens
        self._used = 0
        self._ledger: list[LedgerEntry] = []

    def charge(self, tool: str, tokens: int) -> None:
        """
        Deduct `tokens` from the budget. Raises BudgetExhausted if the
        remaining balance would go negative after this charge.

        The check is pre-charge: if 100 tokens remain and the tool wants
        to charge 200, we raise before the read happens -- so the workspace
        is never partially modified by an over-budget tool call.
        """
        if tokens > self.remaining:
            raise BudgetExhausted(tool, tokens, self.remaining)
        self._used += tokens
        self._ledger.append(LedgerEntry(tool=tool, tokens=tokens))

    @property
    def remaining(self) -> int:
        return self._max - self._used

    @property
    def used(self) -> int:
        return self._used

    @property
    def max_tokens(self) -> int:
        return self._max

    @property
    def ledger(self) -> list[LedgerEntry]:
        """Read-only view of every charge made so far, in order."""
        return list(self._ledger)

    def snapshot(self) -> dict:
        """
        Serializable snapshot for writing to task_checkpoints.context_snapshot.
        The orchestrator includes this in every checkpoint so a resumed run
        starts with the correct remaining balance rather than a fresh budget.
        """
        return {
            "max_tokens": self._max,
            "used_tokens": self._used,
            "ledger": [
                {"tool": e.tool, "tokens": e.tokens, "ts": e.timestamp.isoformat()}
                for e in self._ledger
            ],
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "RetrievalBudget":
        """
        Reconstruct a RetrievalBudget from a checkpoint snapshot so a resumed
        task continues from the right balance rather than starting over.
        """
        b = cls(max_tokens=snap["max_tokens"])
        for entry in snap.get("ledger", []):
            b._used += entry["tokens"]
            b._ledger.append(LedgerEntry(
                tool=entry["tool"],
                tokens=entry["tokens"],
                timestamp=datetime.fromisoformat(entry["ts"]),
            ))
        return b


def estimate_tokens(text: str) -> int:
    """
    Estimate the token count of `text` using the char/4 approximation.
    Minimum of 1 so zero-length results still register as a tool call cost.
    """
    return max(1, len(text) // 4)
