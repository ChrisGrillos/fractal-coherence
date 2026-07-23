"""Deterministic v0 coherence reviewer (placeholder for the COHERENCE.md agent).

This module stands in for the LLM reviewer described in COHERENCE.md. It is
intentionally NOT an LLM: it applies two conservative, fully deterministic
cross-branch rules so the integration can be tested end-to-end without a model
in the loop. In production this is replaced by a Fractal agent backend
(impl/claude.py, impl/grok.py, ...) invoked against COHERENCE.md, whose JSON
output is parsed into the same CoherenceReviewResult contract.

Keeping the reviewer behind a single function signature is deliberate: the
barrier/freeze/arrival/release machinery does not care whether the reviewer is
deterministic code or a frontier model. Swapping one for the other is a
one-line change at the call site.
"""

from __future__ import annotations

from fractal_coherence.models import (
    CoherenceReviewResult,
    CompactNodeRecord,
    DirectiveAction,
    EpochDirective,
    Priority,
)


def review(
    *,
    epoch_id: int,
    membership_hash: str,
    records_by_address: dict[str, CompactNodeRecord],
) -> CoherenceReviewResult:
    """Return a validated review result for one frozen epoch snapshot.

    Rule A (duplicate objective): sibling/related nodes that publish the same
    normalized objective are told to reconcile against each other.
    Rule B (declared conflict): a node whose ``known_conflicts`` names another
    frozen member is told to reconcile with that member.

    At most one directive per address (enforced here and re-validated by the
    store on release).
    """
    addressed: set[str] = set()
    directives: list[EpochDirective] = []

    # Rule A: duplicate objectives across the frozen set.
    by_objective: dict[str, list[str]] = {}
    for address, record in records_by_address.items():
        by_objective.setdefault(record.objective.strip().lower(), []).append(address)
    for objective, group in by_objective.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group)
        owner = group_sorted[-1]  # deterministic: deepest/last address owns the fix
        if owner in addressed:
            continue
        targets = [a for a in group_sorted if a != owner]
        directives.append(
            EpochDirective(
                address=owner,
                action=DirectiveAction.reconcile,
                priority=Priority.high,
                rationale=(
                    'duplicate objective shared with '
                    f'{", ".join(targets)}; only one node should own it'
                ),
                targets=targets,
                claims=[],
                instructions=(
                    'Reconcile ownership of this objective with the listed '
                    'nodes; narrow or drop the overlap before continuing.'
                ),
            )
        )
        addressed.add(owner)

    # Rule B: explicitly declared cross-branch conflicts naming another member.
    for address, record in sorted(records_by_address.items()):
        if address in addressed:
            continue
        named = [c for c in record.known_conflicts if c in records_by_address and c != address]
        if not named:
            continue
        directives.append(
            EpochDirective(
                address=address,
                action=DirectiveAction.reconcile,
                priority=Priority.normal,
                rationale=f'declares a conflict with frozen member(s) {", ".join(named)}',
                targets=named,
                claims=[],
                instructions=(
                    'Resolve the declared interface/assumption conflict with the '
                    'named node(s) before either side merges.'
                ),
            )
        )
        addressed.add(address)

    if directives:
        summary = (
            f'{len(directives)} cross-branch correction(s) across '
            f'{len(records_by_address)} frozen nodes.'
        )
    else:
        summary = f'No cross-branch corrections across {len(records_by_address)} frozen nodes.'

    return CoherenceReviewResult(
        epoch_id=epoch_id,
        membership_hash=membership_hash,
        summary=summary,
        directives=directives,
        escalations=[],
    )
