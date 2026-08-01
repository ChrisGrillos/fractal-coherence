# fractal-coherence v0.2

Standalone protocol prototype for coherence oversight in hierarchical agent
trees, designed against Plasma AI's Fractal architecture. Two layers:

- `fractal_coherence` — the synchronized **epoch/barrier data layer**
  (membership freeze, canonical records, one-winner review, sparse
  directives, atomic release).
- `coherence_overseer` — the **per-action oversight layer** (fast path by
  default, autonomous violation detection, persistent verified state,
  tamper-evident audit), whose `block`/`isolate` detections open real epochs
  through the data layer (`coherence_overseer/tree.py`).

## Status

This repository implements and tests the protocol layers. It is not a Fractal
integration and is not production software.

- Implemented (v0.1.2 data layer): membership freeze, bounded canonical
  records, idempotent arrivals, one-winner reviewer claim, sparse
  identity-bound directives, release/abort, and timeout supervision.
- Implemented (v0.2 oversight layer): per-action fast path over compact
  action updates, autonomous detection of authority expansion, constraint
  and destination violations, protected-data egress, suspected prompt
  injection, contradiction with verified decisions, and objective drift;
  persistent hash-chained verified state across sessions; detected
  violations autonomously opening synchronized epochs with cross-branch
  conflict directives (`coherence_overseer.demo_tree`).
- Not implemented: a Fractal loop hook, node waiting/rebinding, directive
  injection into local REVIEW, or a live model reviewer invocation (the
  reviewer is deterministic and pluggable; see `coherence_overseer/README.md`).
- Detection heuristics are deliberately simple; no claim of detection rates
  against adaptive adversaries, lower token use, higher speed, or improved
  correctness is made without comparative measurements.

## Intended mechanism

One tree or subtree temporarily enters a barrier-synchronized review epoch.
Each frozen participant publishes a compact state record. A single reviewer
examines the complete set for cross-branch conflicts and emits corrections only
for affected addresses. After atomic release, an eventual Fractal adapter would
inject each correction into that node's normal REVIEW step.

The v0 policy is **augment only**. Absence of a directive means no coherence
correction; it does not suppress local review.

## Repository layout

```text
README.md                         Project status and usage
COHERENCE.md                      Proposed reviewer prompt
DEPENDENCIES.txt                  Versions used for verification
TEST_OUTPUT.txt                   Captured test and demo results
fractal_coherence/
  models.py                       Pydantic input/output contracts
  schema.sql                      Standalone SQLite schema
  store.py                        Epoch protocol and timeout supervisor
  test_store.py                   Behavioral and adversarial tests
  demo_protocol.py                Protocol demo with simulated review output
  __init__.py
coherence_overseer/               Per-action oversight layer (see its README)
  contracts.py                    Objective/authority contract, compact updates
  overseer.py                     Fast-path checks and escalation
  state.py                        Persistent verified state, hash-chained audit
  review.py                       Review barrier + pluggable Reviewer protocol
  tree.py                         Detected violations open epochs via the store
  demo.py / demo_tree.py          Single-workflow and hierarchical demos
  test_overseer.py / test_tree.py Behavioral tests (13 + 8)
```

## Quick start

Python 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install pydantic==2.13.4 pytest==9.0.3

python -m pytest fractal_coherence/test_store.py -v
python -m fractal_coherence.demo_protocol
```

The verified v0.1.2 suite contains 21 tests.

## Protocol

1. `create_epoch` freezes a non-overlapping subtree membership and computes its
   membership hash in one writer transaction.
2. `arrive` accepts one canonical `CompactNodeRecord` per frozen address. The
   published identity, depth, and lifecycle status must match the frozen target.
3. `try_start_review` atomically changes a complete epoch from `active` to
   `reviewing`. Exactly one concurrent caller receives the review token.
4. `release` requires the subtree owner and winning token, revalidates stored
   state hashes and reviewer output, writes sparse directives, and changes the
   epoch to `released` in one transaction.
5. `abort` is owner-gated. `supervise_timeouts` can abort expired arrival or
   review phases using status-guarded updates.

## Fractal integration boundary

A real integration would still need to:

1. Adapt these operations to Fractal's existing database and `Record` patterns,
   including foreign keys to its integer run, iteration, and step rows.
2. Collect deterministic runtime fields and bounded semantic fields at the
   existing SYNC boundary.
3. Park each participating loop until release or abort without suppressing
   lifecycle signals.
4. Invoke `COHERENCE.md` once, validate its JSON result, and atomically release
   the epoch.
5. Inject only the addressed correction into each node's existing REVIEW
   context under the `augment` policy.

Prime/phase addressing is deliberately outside this prototype. Dotted Fractal
addresses already provide the identity-preserving binding required to test the
epoch mechanism first.

## Evaluation

The next evidence-producing step is a controlled comparison against ordinary
Fractal behavior on decomposable, cross-branch, sequential, and tool-heavy
tasks. Useful measures include escaped conflicts, duplicate work, corrections
accepted or reverted, total tokens, cost, and wall time.
