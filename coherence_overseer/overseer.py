"""The coherence overseer: fast path by default, review by exception.

For every action the overseer receives a compact update (never the agent's
full context) and answers one question: is this action still consistent with
the verified objective, state, and authority?

Normal actions continue automatically at machine speed — no reviewer, no
human gate. Only drift, contradiction, authority expansion, suspected
injection, or protected-data egress trigger a synchronized review, whose
verdict and rationale land in the persistent verified state.
"""

from __future__ import annotations

import re

from .contracts import (
    ActionUpdate,
    Decision,
    Finding,
    FindingKind,
    ObjectiveContract,
    Outcome,
    Verdict,
)
from .review import DeterministicReviewer, ReviewBundle, Reviewer
from .state import VerifiedState

# Deliberately simple content heuristics: they only ever run in combination
# with the runtime taint bit, and they only escalate — the reviewer decides.
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(the\s+)?(system|above)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"new\s+(system\s+)?instructions?\s*:", re.IGNORECASE),
    re.compile(r"do\s+not\s+(tell|inform|log)", re.IGNORECASE),
)

_RECENT_WINDOW = 8


class CoherenceOverseer:
    """Observes one bounded workflow against one ObjectiveContract."""

    def __init__(
        self,
        contract: ObjectiveContract,
        state: VerifiedState,
        reviewer: Reviewer | None = None,
    ):
        self.contract = contract
        self.state = state
        self.reviewer = reviewer or DeterministicReviewer()
        self._recent: list[dict] = []
        self._off_objective_streak = 0

    # ------------------------------------------------------------- main hook

    def observe(self, action: ActionUpdate) -> Outcome:
        """Fast-path check for one action; escalate on findings."""
        prospective_streak = self._streak_after(action)
        findings = self._fast_path_findings(action, prospective_streak)
        self._remember(action)

        if not findings:
            self.state.log_fast_path(action)
            self._off_objective_streak = prospective_streak
            return Outcome(decision=Decision.allow)

        return self._synchronized_review(action, findings)

    # ------------------------------------------------------ fast-path checks

    def _fast_path_findings(self, action: ActionUpdate, prospective_streak: int) -> tuple:
        findings: list[Finding] = []

        grant = self.contract.grant_for(action.tool)
        if grant is None:
            findings.append(
                Finding(
                    FindingKind.unauthorized_tool,
                    f"tool {action.tool!r} is outside the granted authority",
                )
            )
        else:
            for arg, ceiling in grant.constraints.items():
                value = action.args_summary.get(arg)
                if isinstance(value, (int, float)) and value > ceiling:
                    findings.append(
                        Finding(
                            FindingKind.constraint_violation,
                            f"{action.tool} {arg}={value} exceeds ceiling {ceiling}",
                        )
                    )
            if not _destination_allowed(action.target, grant.destinations):
                findings.append(
                    Finding(
                        FindingKind.destination_violation,
                        f"destination {action.target!r} not in "
                        f"{list(grant.destinations)}",
                    )
                )

        protected = set(action.data_tags) & set(self.contract.protected_data)
        granted_destinations = grant.destinations if grant else ()
        if protected and _is_external(action.target) and not _destination_allowed(
            action.target, granted_destinations
        ):
            findings.append(
                Finding(
                    FindingKind.protected_egress,
                    f"protected data {sorted(protected)} routed to "
                    f"{action.target!r}",
                )
            )

        decision = self.state.matching_decision(action)
        if decision is not None and decision["rule"] == "forbid":
            findings.append(
                Finding(
                    FindingKind.contradiction,
                    f"contradicts verified decision: {decision['rationale']}",
                )
            )

        if action.tainted:
            injected = _injection_evidence(action)
            if injected:
                findings.append(
                    Finding(
                        FindingKind.injection_suspected,
                        f"instruction-shaped content in tainted action: {injected!r}",
                    )
                )

        if prospective_streak > self.contract.max_off_objective_streak:
            findings.append(
                Finding(
                    FindingKind.objective_drift,
                    f"{prospective_streak} consecutive actions map to no open "
                    f"requirement (limit "
                    f"{self.contract.max_off_objective_streak})",
                )
            )

        return tuple(findings)

    # -------------------------------------------------------- exception path

    def _synchronized_review(self, action: ActionUpdate, findings: tuple) -> Outcome:
        bundle = ReviewBundle(
            objective=self.contract.objective,
            action=action.compact(),
            findings=findings,
            recent_actions=tuple(self._recent[-_RECENT_WINDOW:]),
            verified_decisions=tuple(self.state.data["verified_decisions"]),
            open_requirements=tuple(self.state.open_requirements()),
        )
        directive = self.reviewer.review(bundle)
        self.state.record_review(action, findings, directive.verdict, directive)

        if directive.verdict in (Verdict.block, Verdict.isolate):
            self.state.record_decision(
                rule="forbid",
                match={"tool": action.tool, "args_summary": action.args_summary},
                rationale=directive.rationale[:200],
            )
        self._off_objective_streak = 0
        return Outcome(
            decision=Decision.escalate,
            verdict=directive.verdict,
            findings=findings,
            directive=directive,
        )

    # -------------------------------------------------------------- plumbing

    def _remember(self, action: ActionUpdate) -> None:
        self._recent.append(action.compact())
        del self._recent[:-_RECENT_WINDOW]

    def _maps_to_open_requirement(self, action: ActionUpdate) -> bool:
        return action.requirement in self.state.open_requirements()

    def _streak_after(self, action: ActionUpdate) -> int:
        if self._maps_to_open_requirement(action):
            return 0
        return self._off_objective_streak + 1


def _destination_allowed(target: str, destinations: tuple) -> bool:
    if "any" in destinations:
        return True
    base = target.split(":", 1)[0]
    return base in destinations


def _is_external(target: str) -> bool:
    return target.split(":", 1)[0] not in ("internal",)


def _injection_evidence(action: ActionUpdate) -> str | None:
    texts = [action.intent] + [
        value for value in action.args_summary.values() if isinstance(value, str)
    ]
    for text in texts:
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None
