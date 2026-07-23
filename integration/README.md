# Fractal integration (v0) — barrier-synchronized coherence epoch

Runs a synchronized coherence epoch over a live Fractal subtree: freeze the
subtree, each node publishes a compact state record, one reviewer examines the
whole frozen set for cross-branch problems local SYNC can't see, augment-only
directives are written back under existing identities, nodes resume.

## Why it fits Fractal
- Fractal branches are already dotted paths (`node.py` `rsplit('.',1)`,
  `snapshot.py` `rpartition('.')`) -> a branch string *is* a coherence address.
- Coherence `LifecycleStatus` == Fractal `constants.STATUSES`.
- Epoch tables slot into the existing `.fractal/.db`.
- The node-side check rides the existing `SYNC.md` pre-step.

## Files
- `fractal_bridge.py` — reads a Fractal subtree from Fractal's DB and drives the
  epoch state machine (create -> arrive -> review -> release).
- `simple_reviewer.py` — deterministic v0 reviewer (placeholder for the
  COHERENCE.md agent; one-line-swappable interface).
- `demo_fractal_epoch.py` — end-to-end demo against Fractal's real `schema.sql`.

The reviewer contract is the repo-root `../COHERENCE.md` (untrusted-input,
augment-only, strict JSON output).

## Run the demo
```bash
pip install plasma-fractal pydantic
# from the repo root (so `fractal_coherence` is importable):
PYTHONPATH=. python integration/demo_fractal_epoch.py
```
Expected: a 3-node subtree freezes, all arrive, the reviewer flags a planted
cross-branch conflict, a directive is written back, the epoch reaches `released`.

## v0 scope
Operates on parked nodes (at a SYNC boundary), deterministic reviewer, reuses
Fractal's dotted-branch identity. Out of scope: mid-inference interruption, the
live LLM reviewer, autonomous contradiction detection.

## Open question (gates the SYNC-hook PR)
Do Fractal branch names ever contain characters outside dotted `[A-Za-z0-9_]`
segments? If not, branch->address is identity. If so, a canonical
branch->address policy is needed (the bridge fails loud rather than mangle it).
