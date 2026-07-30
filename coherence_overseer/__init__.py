"""coherence_overseer: per-action coherence oversight for one bounded workflow.

Fast path by default, synchronized review by exception, persistent verified
state outside any model's context window. Extends the epoch/barrier data
layer in ``fractal_coherence`` with autonomous violation detection.

Stdlib-only. Run the demo with:  python -m coherence_overseer.demo
"""

from .contracts import (
    ActionUpdate,
    Decision,
    Directive,
    Finding,
    FindingKind,
    ObjectiveContract,
    Outcome,
    ToolGrant,
    Verdict,
)
from .overseer import CoherenceOverseer
from .review import DeterministicReviewer, Reviewer, ReviewBundle
from .state import TamperError, VerifiedState

__all__ = [
    "ActionUpdate",
    "CoherenceOverseer",
    "Decision",
    "DeterministicReviewer",
    "Directive",
    "Finding",
    "FindingKind",
    "ObjectiveContract",
    "Outcome",
    "Reviewer",
    "ReviewBundle",
    "TamperError",
    "ToolGrant",
    "Verdict",
    "VerifiedState",
]
