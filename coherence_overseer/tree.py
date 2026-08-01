"""Fractal tree oversight: per-node fast paths, subtree-synchronized epochs.

This is the layer that joins the repository's two halves into one system:

* ``coherence_overseer`` (stdlib) watches each node's actions on the fast
  path and detects violations autonomously.
* ``fractal_coherence`` (the v0.1.2 epoch/barrier store) freezes the subtree,
  collects one canonical ``CompactNodeRecord`` per node, runs one reviewer
  over the frozen set, and releases sparse identity-bound directives.

The v0.1.2 README stated honestly that its demo "does not demonstrate
autonomous contradiction detection" — the reviewer output was simulated.
This module closes that gap: a detected per-action violation is now what
*opens* the epoch, and the epoch reviewer sees the real findings.

Requires the parent package's dependencies (pydantic) — the base
``coherence_overseer`` package stays stdlib-only; only this tree layer and
its demo need the store.
"""

from __future__ import annotations

try:
    from fractal_coherence.models import (
        CoherenceReviewResult,
        CompactNodeRecord,
        EpochDirective,
        EpochEscalation,
    )
    from fractal_coherence.store import CoherenceStore, TargetSpec
except ImportError as error:  # pragma: no cover
    raise ImportError(
        "the tree layer needs the fractal_coherence package and its "
        "dependencies (pip install pydantic==2.13.4); the base "
        "coherence_overseer package remains stdlib-only"
    ) from error

import os
from dataclasses import dataclass, field

from .contracts import ActionUpdate, Decision, FindingKind, ObjectiveContract, Outcome, Verdict
from .overseer import CoherenceOverseer
from .review import Reviewer
from .state import VerifiedState

# Finding -> directive action for the triggering node, mirroring the sparse
# directive vocabulary of fractal_coherence.models.DirectiveAction.
_TRIGGER_DIRECTIVE_ACTION = {
    FindingKind.unauthorized_tool: "stop",
    FindingKind.constraint_violation: "stop",
    FindingKind.destination_violation: "stop",
    FindingKind.protected_egress: "stop",
    FindingKind.injection_suspected: "stop",
    FindingKind.contradiction: "verify",
    FindingKind.objective_drift: "narrow",
}

_EPOCH_SEVERITY_THRESHOLD = Verdict.block.severity  # block or isolate


@dataclass
class TreeNode:
    """One acting agent in the dotted tree."""

    address: str
    contract: ObjectiveContract
    state: VerifiedState
    overseer: CoherenceOverseer
    last_delta: str = "no action observed yet"

    @property
    def parent(self) -> str | None:
        return self.address.rpartition(".")[0] or None

    @property
    def depth(self) -> int:
        return self.address.count(".")


@dataclass(frozen=True)
class EpochOutcome:
    """Result of one synchronized epoch triggered by a detected violation."""

    epoch_id: int
    membership_hash: str
    summary: str
    directives: tuple = ()      # tuple[dict, ...] — address, action, rationale, instructions
    escalations: tuple = ()


@dataclass(frozen=True)
class TreeOutcome:
    """Per-action outcome plus the epoch it triggered, if any."""

    address: str
    action_outcome: Outcome
    epoch: EpochOutcome | None = None


class TreeOverseer:
    """Coordinates per-node overseers and the subtree epoch protocol."""

    def __init__(self, root_address: str, store: CoherenceStore, state_dir: str,
                 reviewer: Reviewer | None = None):
        self.root_address = root_address
        self.store = store
        self.state_dir = state_dir
        self.reviewer = reviewer
        self.nodes: dict[str, TreeNode] = {}
        os.makedirs(state_dir, exist_ok=True)

    def add_node(self, address: str, contract: ObjectiveContract) -> TreeNode:
        if address != self.root_address and not address.startswith(f"{self.root_address}."):
            raise ValueError(f"{address!r} is outside subtree {self.root_address!r}")
        state = VerifiedState(
            os.path.join(self.state_dir, f"{address}.json"),
            contract.objective_id,
            contract.requirements,
        )
        node = TreeNode(
            address=address,
            contract=contract,
            state=state,
            overseer=CoherenceOverseer(contract, state, self.reviewer),
        )
        self.nodes[address] = node
        return node

    # ------------------------------------------------------------- main hook

    def observe(self, address: str, action: ActionUpdate) -> TreeOutcome:
        """Fast-path check one node's action; open a synchronized epoch when
        the per-action verdict is block or isolate."""
        node = self.nodes[address]
        outcome = node.overseer.observe(action)
        node.last_delta = _clip(
            f"step {action.step}: {action.tool} -> "
            f"{outcome.verdict.value if outcome.verdict else 'allowed'}", 400
        )

        if (
            outcome.decision is Decision.escalate
            and outcome.verdict is not None
            and outcome.verdict.severity >= _EPOCH_SEVERITY_THRESHOLD
        ):
            epoch = self._run_epoch(node, action, outcome)
            return TreeOutcome(address=address, action_outcome=outcome, epoch=epoch)
        return TreeOutcome(address=address, action_outcome=outcome, epoch=None)

    # ----------------------------------------------------- epoch orchestration

    def _run_epoch(self, trigger: TreeNode, action: ActionUpdate,
                   outcome: Outcome) -> EpochOutcome:
        targets = [
            TargetSpec(node.address, node.parent, node.depth, "paused")
            for node in sorted(self.nodes.values(), key=lambda entry: entry.address)
        ]
        epoch_id, membership_hash = self.store.create_epoch(
            self.root_address,
            targets,
            created_by="coherence_overseer",
            reviewer_agent="deterministic_tree_reviewer_v0",
        )
        for node in self.nodes.values():
            self.store.arrive(epoch_id, self._record_for(node, trigger, action, outcome))

        review_token = self.store.try_start_review(epoch_id)
        if review_token is None:  # pragma: no cover — single-driver runtime
            raise RuntimeError(f"could not claim review for epoch {epoch_id}")

        records = self.store.get_records(epoch_id)
        result = self._tree_review(epoch_id, membership_hash, records, trigger, outcome)
        self.store.release(
            epoch_id, result,
            owner_address=self.root_address,
            review_token=review_token,
        )
        directive_rows = self.store.get_directives(epoch_id)
        return EpochOutcome(
            epoch_id=epoch_id,
            membership_hash=membership_hash,
            summary=result.summary,
            directives=tuple(
                {
                    "address": row["address"],
                    "action": row["action"],
                    "priority": row["priority"],
                    "rationale": row["rationale"],
                    "instructions": row["instructions"],
                }
                for row in directive_rows
            ),
            escalations=tuple(
                {"description": item.description, "addresses": list(item.addresses)}
                for item in result.escalations
            ),
        )

    def _record_for(self, node: TreeNode, trigger: TreeNode, action: ActionUpdate,
                    outcome: Outcome) -> CompactNodeRecord:
        is_trigger = node.address == trigger.address
        conflicts = []
        if is_trigger:
            conflicts = [
                _clip(f"{finding.kind.value}: {finding.detail}", 200)
                for finding in outcome.findings[:6]
            ]
        return CompactNodeRecord(
            address=node.address,
            parent=node.parent,
            depth=node.depth,
            lifecycle_status="paused",
            work_state="blocked" if is_trigger else "progressing",
            objective=_clip(node.contract.objective, 300),
            delta=_clip(node.last_delta, 400),
            known_conflicts=_dedupe(conflicts),
            requested_action="escalate" if is_trigger else "none",
        )

    # ------------------------------------------------------------ tree review

    def _tree_review(self, epoch_id: int, membership_hash: str,
                     records: dict, trigger: TreeNode,
                     outcome: Outcome) -> CoherenceReviewResult:
        """Deterministic tree reviewer: trigger directive from real findings,
        cross-branch checks over the frozen record set, sparse output only.

        A model-backed reviewer (per COHERENCE.md) would receive the same
        frozen records and return the same structured result type.
        """
        directives = []
        primary_kind = outcome.findings[0].kind if outcome.findings else None
        trigger_action = _TRIGGER_DIRECTIVE_ACTION.get(primary_kind, "stop")
        directives.append(
            EpochDirective(
                address=trigger.address,
                action=trigger_action,
                priority="high",
                rationale=_clip(
                    "; ".join(f.detail for f in outcome.findings) or "violation detected", 400
                ),
                instructions=_clip(outcome.directive.instructions, 600),
            )
        )

        # Cross-branch: every branch sharing a duplicated objective with
        # another frozen record gets a reconcile directive (the classic
        # conflict local review cannot see). The trigger node is excluded —
        # it already holds its high-priority directive.
        groups: dict[str, list] = {}
        for address, record in sorted(records.items()):
            groups.setdefault(record.objective.strip().lower(), []).append(address)
        for members in groups.values():
            if len(members) < 2:
                continue
            for address in members:
                if address == trigger.address:
                    continue
                target = next(other for other in members if other != address)
                directives.append(
                    EpochDirective(
                        address=address,
                        action="reconcile",
                        priority="normal",
                        rationale=_clip(f"objective duplicates {target}", 400),
                        targets=[target],
                        instructions=(
                            "Coordinate with the target branch to split or merge "
                            "the duplicated objective before further work."
                        ),
                    )
                )

        escalations = []
        if outcome.verdict is Verdict.isolate:
            escalations.append(
                EpochEscalation(
                    description=_clip(
                        f"{trigger.address} attempted protected-data egress; "
                        "operator release required", 400
                    ),
                    addresses=[trigger.address],
                )
            )

        summary = _clip(
            f"Epoch triggered autonomously by {trigger.address} "
            f"({', '.join(f.kind.value for f in outcome.findings)}); "
            f"{len(directives)} directive(s), {len(escalations)} escalation(s). "
            "Fast path resumes for unaffected branches on release.", 600
        )
        return CoherenceReviewResult(
            epoch_id=epoch_id,
            membership_hash=membership_hash,
            summary=summary,
            directives=directives,
            escalations=escalations,
        )


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) or "n/a"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dedupe(values: list) -> list:
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
