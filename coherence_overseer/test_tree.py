"""Tests for the tree layer: autonomous epoch opening and directive routing.

Run:  python -m pytest coherence_overseer/test_tree.py -v
  or: python -m coherence_overseer.test_tree
"""

from __future__ import annotations

import tempfile
import traceback

from fractal_coherence.store import CoherenceStore

from .contracts import ActionUpdate, ObjectiveContract, ToolGrant
from .tree import TreeOverseer


def contract(objective_id: str, objective: str) -> ObjectiveContract:
    return ObjectiveContract(
        objective_id=objective_id,
        objective=objective,
        requirements=("r1", "r2"),
        grants={"tool.a": ToolGrant("tool.a", constraints={"amount": 100.0})},
        protected_data=("secret",),
        max_off_objective_streak=2,
    )


def build_tree(tmp: str, duplicate_objectives: bool = False) -> TreeOverseer:
    tree = TreeOverseer("r", CoherenceStore.in_memory(), tmp)
    tree.add_node("r", contract("obj_r", "coordinate the work"))
    tree.add_node("r.a", contract("obj_a", "do part A"))
    tree.add_node("r.b", contract("obj_b", "do part A" if duplicate_objectives else "do part B"))
    return tree


def ok_action(step: int = 1) -> ActionUpdate:
    return ActionUpdate(step, "tool.a", "internal", "advance r1", "r1", {"amount": 10.0})


def bad_action(step: int = 1) -> ActionUpdate:
    return ActionUpdate(step, "tool.a", "internal", "overspend", "r1", {"amount": 500.0})


def test_fast_path_opens_no_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        result = build_tree(tmp).observe("r.a", ok_action())
        assert result.action_outcome.decision.value == "allow"
        assert result.epoch is None


def test_block_verdict_opens_epoch_autonomously():
    with tempfile.TemporaryDirectory() as tmp:
        result = build_tree(tmp).observe("r.a", bad_action())
        assert result.action_outcome.verdict.value == "block"
        assert result.epoch is not None
        assert result.epoch.epoch_id >= 1


def test_trigger_node_receives_high_priority_directive():
    with tempfile.TemporaryDirectory() as tmp:
        result = build_tree(tmp).observe("r.a", bad_action())
        directive = next(d for d in result.epoch.directives if d["address"] == "r.a")
        assert directive["action"] == "stop"
        assert directive["priority"] == "high"


def test_duplicate_objectives_get_reconcile_directive():
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(tmp, duplicate_objectives=True)
        result = tree.observe("r.a", bad_action())
        reconcile = [d for d in result.epoch.directives if d["action"] == "reconcile"]
        assert reconcile and reconcile[0]["address"] == "r.b"


def test_epoch_released_in_store():
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(tmp)
        result = tree.observe("r.a", bad_action())
        row = tree.store.get_epoch(result.epoch.epoch_id)
        assert row["status"] == "released"
        assert row["membership_hash"] == result.epoch.membership_hash


def test_correct_verdict_does_not_open_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(tmp)
        tree.nodes["r.a"].state.record_decision(
            "forbid", {"tool": "tool.a", "args_summary.order": "7"}, "already done"
        )
        result = tree.observe("r.a", ActionUpdate(
            1, "tool.a", "internal", "redo", "r1", {"order": "7", "amount": 5.0}
        ))
        assert result.action_outcome.verdict.value == "correct"
        assert result.epoch is None  # correct-severity stays per-action


def test_fast_path_resumes_after_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(tmp)
        assert tree.observe("r.a", bad_action()).epoch is not None
        follow_up = tree.observe("r.b", ok_action())
        assert follow_up.action_outcome.decision.value == "allow"
        assert follow_up.epoch is None


def test_second_violation_opens_second_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(tmp)
        first = tree.observe("r.a", bad_action(1))
        second = tree.observe("r.b", ActionUpdate(
            1, "ungranted.tool", "internal", "expand authority", None, {}
        ))
        assert first.epoch.epoch_id != second.epoch.epoch_id
        assert tree.store.get_epoch(second.epoch.epoch_id)["status"] == "released"


ALL_TESTS = [value for name, value in sorted(globals().items()) if name.startswith("test_")]


def main() -> int:
    failed = 0
    for test in ALL_TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} tests passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
