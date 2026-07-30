"""Compact, disclosure-safe contracts for the per-action coherence overseer.

These dataclasses show the *shape* of an objective/authority contract and of
compact action updates. They are demo-grade on purpose: bounded, canonical,
and small enough to travel outside any model's context window. They are not a
production state contract.

Design lineage: the compact-record discipline follows
``fractal_coherence.models.CompactNodeRecord`` (bounded canonical JSON,
SHA-256 state hashes). This module is stdlib-only so the demo runs with no
installed dependencies.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum


class Decision(str, Enum):
    """Outcome of the overseer's fast-path check for one action."""

    allow = "allow"          # fast path: work continues automatically
    escalate = "escalate"    # exception path: synchronized review invoked


class Verdict(str, Enum):
    """Outcome of a synchronized review, ordered by severity."""

    allow_with_note = "allow_with_note"
    correct = "correct"      # action redirected; agent receives a directive
    block = "block"          # action refused; agent receives a directive
    isolate = "isolate"      # action refused and workflow quarantined

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {
    Verdict.allow_with_note: 0,
    Verdict.correct: 1,
    Verdict.block: 2,
    Verdict.isolate: 3,
}


class FindingKind(str, Enum):
    """Why the fast path refused to wave an action through."""

    unauthorized_tool = "unauthorized_tool"
    constraint_violation = "constraint_violation"
    destination_violation = "destination_violation"
    protected_egress = "protected_egress"
    injection_suspected = "injection_suspected"
    contradiction = "contradiction"
    objective_drift = "objective_drift"


def canonical_json(data) -> str:
    """Stable JSON used for hashing and persistence."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolGrant:
    """Authority granted for exactly one tool within one objective.

    ``constraints`` holds numeric ceilings keyed by argument name
    (e.g. ``{"amount": 200.0}``).  ``destinations`` lists the allowed
    destination classes for the tool's effects.
    """

    tool: str
    constraints: dict = field(default_factory=dict)
    destinations: tuple = ("internal",)


@dataclass(frozen=True)
class ObjectiveContract:
    """The verified objective, requirements, and authority for one bounded
    workflow.  Everything the overseer compares against lives here or in the
    persistent verified state — never in the acting model's context window.
    """

    objective_id: str
    objective: str
    requirements: tuple                  # ordered requirement ids
    grants: dict                         # tool name -> ToolGrant
    protected_data: tuple = ()           # data tags that must not leave allowed destinations
    max_off_objective_streak: int = 3    # consecutive unmapped actions tolerated

    def grant_for(self, tool: str) -> ToolGrant | None:
        return self.grants.get(tool)

    def contract_hash(self) -> str:
        data = asdict(self)
        data["grants"] = {name: asdict(grant) for name, grant in self.grants.items()}
        return sha256_hex(canonical_json(data))


@dataclass(frozen=True)
class ActionUpdate:
    """Compact per-action update observed by the overseer.

    This is what the overseer sees — not the agent's full context. ``tainted``
    is set by the runtime when the action was formed after ingesting untrusted
    tool output (the taint bit is cheap for a runtime to carry; the overseer
    combines it with content heuristics rather than trusting either alone).
    """

    step: int
    tool: str
    target: str                          # destination class, e.g. "internal", "customer", "external:acct-9931"
    intent: str                          # agent's one-line stated intent
    requirement: str | None              # requirement id this action claims to advance
    args_summary: dict = field(default_factory=dict)
    data_tags: tuple = ()                # data classes touched, e.g. ("customer_pii",)
    tainted: bool = False

    def compact(self) -> dict:
        data = asdict(self)
        data["data_tags"] = list(self.data_tags)
        return data

    def action_hash(self) -> str:
        return sha256_hex(canonical_json(self.compact()))


@dataclass(frozen=True)
class Finding:
    """One fast-path objection to an action."""

    kind: FindingKind
    detail: str


@dataclass(frozen=True)
class Directive:
    """Identity-bound correction returned to the agent after review.

    Mirrors the sparse-directive discipline of
    ``fractal_coherence.models.EpochDirective``: small, targeted, rationale
    included, never a rewrite of the agent's goals.
    """

    verdict: Verdict
    rationale: str
    instructions: str


@dataclass(frozen=True)
class Outcome:
    """What the overseer returns to the runtime for one observed action."""

    decision: Decision
    verdict: Verdict | None = None       # set only when a review ran
    findings: tuple = ()
    directive: Directive | None = None

    @property
    def allowed(self) -> bool:
        if self.decision is Decision.allow:
            return True
        return self.verdict is Verdict.allow_with_note
