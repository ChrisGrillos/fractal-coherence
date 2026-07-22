# fractal-coherence v0.1.2

Standalone protocol prototype for an optional synchronized coherence epoch in
hierarchical agent trees, designed against Plasma AI's Fractal architecture.

## Status

This repository implements and tests the **SQLite epoch/barrier data layer**.
It is not a Fractal integration and is not production software.

- Implemented: membership freeze, bounded canonical records, idempotent
  arrivals, one-winner reviewer claim, sparse identity-bound directives,
  release/abort, and timeout supervision.
- Not implemented: a Fractal loop hook, node waiting/rebinding, directive
  injection into local REVIEW, or a live model reviewer invocation.
- The included demo supplies simulated reviewer output; it does not demonstrate
  autonomous contradiction detection.
- No claim of lower token use, higher speed, or improved correctness is made
  without comparative measurements.

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
