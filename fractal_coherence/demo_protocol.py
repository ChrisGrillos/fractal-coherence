#!/usr/bin/env python3
"""Protocol demo for the Fractal synchronized coherence epoch prototype.

Two scenarios:
  1. Happy path – all nodes arrive, simulated review output is released.
  2. Timeout path – review starts but supervisor aborts after timeout.

Run from the artifacts directory:
    python -m fractal_coherence.demo_protocol
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from .models import (
    Claim,
    CompactNodeRecord,
    CoherenceReviewResult,
    DirectiveAction,
    EpochDirective,
    LifecycleStatus,
    Priority,
    RequestedAction,
    VerificationStatus,
    WorkState,
)
from .store import CoherenceStore, TargetSpec


def make_record(
    address: str,
    parent: str | None,
    depth: int,
    objective: str,
    delta: str,
    claim_text: str,
    claim_id: str = "c1",
) -> CompactNodeRecord:
    return CompactNodeRecord(
        address=address,
        parent=parent,
        depth=depth,
        lifecycle_status=LifecycleStatus.active,
        work_state=WorkState.progressing,
        spent_usd=0.42,
        remaining_usd=1.58,
        objective=objective,
        delta=delta,
        claims=[
            Claim(
                id=claim_id,
                text=claim_text,
                verification=VerificationStatus.partial,
                evidence=[f"src/{address.replace('.', '/')}.py"],
            )
        ],
        blockers=[],
        known_conflicts=[],
        requested_action=RequestedAction.none,
        run_id=10,
        iter_id=3,
        step_id=2,
    )


def scenario_happy_path(store: CoherenceStore) -> None:
    print("\n=== Scenario 1: Happy path (augment + sparse directives) ===\n")

    targets = [
        TargetSpec("main", None, 0, LifecycleStatus.active),
        TargetSpec("main.parser", "main", 1, LifecycleStatus.active),
        TargetSpec("main.lexer", "main", 1, LifecycleStatus.active),
    ]
    epoch_id, mhash = store.create_epoch(
        "main", targets, created_by="demo", timeout_seconds=120, reviewer_agent="demo-reviewer"
    )
    print(f"Created epoch {epoch_id}  membership_hash={mhash[:12]}…")

    # Nodes publish compact state at the barrier
    store.arrive(
        epoch_id,
        make_record(
            "main",
            None,
            0,
            objective="Ship a correct lexer+parser for escaped delimiters",
            delta="Root orchestration only; waiting on children",
            claim_text="Top-level contract still assumes byte offsets",
        ),
    )
    store.arrive(
        epoch_id,
        make_record(
            "main.parser",
            "main",
            1,
            objective="Parse tokens into AST, preserve source offsets",
            delta="Parser still uses character offsets; 2 tests fail against lexer",
            claim_text="Offsets remain character-based",
            claim_id="c_parser",
        ),
    )
    store.arrive(
        epoch_id,
        make_record(
            "main.lexer",
            "main",
            1,
            objective="Tokenize with correct byte offsets for escaped delimiters",
            delta="Lexer emits byte offsets; 11 unit tests pass",
            claim_text="Offsets are byte-based",
            claim_id="c_lexer",
        ),
    )
    print("All 3 nodes arrived.")

    review_token = store.try_start_review(epoch_id)
    assert review_token is not None, "expected to win the reviewer CAS"
    print("CAS → reviewing (this caller is the reviewer)")

    # Simulated coherence reviewer output (in production this comes from COHERENCE.md)
    result = CoherenceReviewResult(
        epoch_id=epoch_id,
        membership_hash=mhash,
        summary=(
            "Lexer and parser disagree on offset units. "
            "Parser must adopt byte offsets or lexer must emit character offsets."
        ),
        directives=[
            EpochDirective(
                address="main.parser",
                action=DirectiveAction.revise,
                priority=Priority.high,
                rationale="Child claim contradicts sibling lexer claim on the same surface",
                targets=["main.lexer"],
                claims=["c_parser"],
                instructions=(
                    "Switch parser offset arithmetic to byte offsets to match "
                    "main.lexer. Re-run the two failing integration tests."
                ),
            ),
            # main and main.lexer receive no directive → no correction
        ],
        escalations=[],
    )
    store.release(
        epoch_id,
        result,
        owner_address="main",
        review_token=review_token,
        input_tokens=1800,
        output_tokens=420,
        cost_usd=0.031,
    )

    epoch = store.get_epoch(epoch_id)
    print(f"Status          : {epoch['status']}")
    print(f"Summary         : {epoch['summary']}")
    print(f"Reviewer cost   : ${epoch['reviewer_cost_usd']:.3f}")
    print("Directives:")
    for d in store.get_directives(epoch_id):
        print(f"  → {d['address']}: {d['action']} ({d['priority']}) — {d['instructions'][:70]}…")
    print("No directive rows were stored for main or main.lexer.")


def scenario_timeout(store: CoherenceStore) -> None:
    print("\n=== Scenario 2: Review timeout (supervisor aborts) ===\n")

    targets = [
        TargetSpec("svc", None, 0, LifecycleStatus.active),
        TargetSpec("svc.auth", "svc", 1, LifecycleStatus.active),
    ]
    epoch_id, _ = store.create_epoch(
        "svc", targets, created_by="demo", timeout_seconds=15
    )
    print(f"Created epoch {epoch_id} with 15s timeout")

    store.arrive(epoch_id, make_record("svc", None, 0, "Orchestrate auth", "Waiting", "Root ok"))
    store.arrive(
        epoch_id,
        make_record("svc.auth", "svc", 1, "Implement JWT", "Tokens issued", "JWT valid"),
    )
    assert store.try_start_review(epoch_id)
    print("Review started… simulating hung reviewer")

    # Supervisor runs before timeout → no action
    aborted = store.supervise_timeouts()
    print(f"Supervisor (early): aborted={aborted}")

    # Jump past timeout
    future = datetime.now(timezone.utc) + timedelta(seconds=20)
    aborted = store.supervise_timeouts(now=future)
    print(f"Supervisor (late) : aborted={aborted}")

    epoch = store.get_epoch(epoch_id)
    print(f"Final status      : {epoch['status']}")
    print(f"Abort reason      : {epoch['abort_reason']}")


def main() -> None:
    store = CoherenceStore.in_memory()
    scenario_happy_path(store)
    scenario_timeout(store)
    print("\nProtocol demo complete (review output was simulated).\n")


if __name__ == "__main__":
    main()
