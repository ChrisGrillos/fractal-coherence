# coherence_overseer v0.1

Per-action coherence oversight for one bounded agent workflow. Extends the
`fractal_coherence` epoch/barrier data layer with the piece it explicitly did
not implement: **autonomous violation detection** — fast path by default,
synchronized review by exception, persistent verified state outside any
model's context window.

## What this demonstrates

- **Compact observation, not context sharing.** The overseer sees a bounded
  `ActionUpdate` per action (tool, destination, one-line intent, argument
  summary, data tags, taint bit) — never the acting model's full context.
- **Fast path at machine speed.** Actions consistent with the objective
  contract continue automatically. A clean workflow uses **zero** review
  epochs (scenario 1).
- **Exception-triggered synchronized review.** Authority expansion,
  constraint violations, destination violations, protected-data egress,
  suspected prompt injection, contradiction with a verified decision, and
  objective drift each pause the workflow at a barrier and produce a verdict
  (`allow_with_note` / `correct` / `block` / `isolate`) plus one sparse,
  rationale-bearing directive — mirroring the `EpochDirective` discipline.
- **Persistent verified state.** Decisions verified in one session bind every
  later session that opens the same state file. A larger context window is
  not a substitute for memory: scenario 6 catches a repeated violation in a
  brand-new session at step 1.
- **Tamper-evident audit.** Every audit entry is SHA-256 hash-chained;
  `verify_chain()` detects silent edits to history.

## What this does NOT demonstrate

Keeping to the same standard as the parent repository:

- No live model reviewer is invoked. `DeterministicReviewer` is rule-based so
  the demo is exactly reproducible with no API key. A model-backed defensive
  reviewer implements the same one-method `Reviewer` protocol and receives
  the same bounded, untrusted-data-only bundle.
- The injection and drift detectors are deliberately simple heuristics, and
  the taint bit is assumed to be supplied by the runtime. No claim is made
  about detection rates against adaptive adversaries without measurements.
- This is a protocol prototype against a scripted workflow, not an
  integration with any agent framework, gateway, or production system.

## Quick start

Stdlib only — no installs needed (Python 3.11+):

```bash
python -m coherence_overseer.demo
python -m coherence_overseer.test_overseer     # 13 behavioral tests
# or, matching the parent repo's tooling:
python -m pytest coherence_overseer/test_overseer.py -v
```

Captured results: `DEMO_OUTPUT.txt`.

## Intended integration point

The natural host for this observation loop is any **typed execution
boundary** that already sits between agents and real capabilities — an MCP
gateway, tool router, or service mesh that knows the method, arguments,
caller, and destination of every call. At such a boundary the
`ActionUpdate` is nearly free to construct, the fast path adds one local
check per call, and the exception path is the only place a stronger
defensive model is ever consulted.

## Threat classes exercised

| Scenario | Threat | Verdict |
|---|---|---|
| 2 | prompt injection via tool output | block |
| 3 | authority / privilege expansion | block |
| 4 | protected-data exfiltration | isolate |
| 5 | contradiction with verified decision | correct |
| — (tests) | objective drift streak | correct |

## Layout

```text
contracts.py       Objective/authority contract, compact action updates, verdicts
state.py           Persistent verified state + hash-chained audit log
overseer.py        Fast-path checks and escalation
review.py          Synchronized review barrier + Reviewer protocol
demo.py            Six-scenario bounded-workflow demo
test_overseer.py   Behavioral tests (13)
DEMO_OUTPUT.txt    Captured demo run
```
