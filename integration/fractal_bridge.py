"""Bridge: run a synchronized coherence epoch over a live Fractal subtree.

This is the glue the standalone `fractal_coherence` protocol was designed for.
It reads a Fractal tree from Fractal's own SQLite database (the `nodes` table
and the run/iter/step lineage), selects a subtree, and drives the coherence
epoch state machine (create -> arrive -> review -> release).

Identity mapping (the key design decision, and it is nearly free):
    Fractal identifies a node by its git branch, and Fractal branches are
    ALREADY dotted paths -- core/node.py derives a parent with
    `branch.rsplit('.', 1)` and snapshot.py indexes children with
    `branch.rpartition('.')`. The coherence protocol derives parent/depth the
    same way (`address.rpartition('.')`, `address.count('.')`). So a Fractal
    branch string *is* a coherence address, with one validation caveat handled
    below.

v0 scope (intentional):
    * Operates on a subtree whose nodes are parked (idle/paused/completed) --
      i.e. sitting at a SYNC boundary -- not mid-inference. This matches how
      Fractal nodes actually yield control.
    * The per-node semantic payload (objective/delta/claims) is supplied by a
      `record_source` rather than fabricated here. In production this payload is
      what each node publishes at its SYNC boundary; the bridge never invents a
      node's intent.
    * The reviewer is pluggable; the default deterministic reviewer is a
      placeholder for the COHERENCE.md agent.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Callable, Mapping

from fractal_coherence.models import CompactNodeRecord
from fractal_coherence.store import CoherenceStore, TargetSpec

import simple_reviewer

# Coherence addresses accept dotted [A-Za-z0-9_] segments. Fractal branch names
# are normally sanitized to this, but we validate explicitly and fail loud
# rather than silently mangling identity -- an integration guard, and an open
# question for the Fractal maintainers (see the systems-designer brief).
_ADDRESS_SEGMENTS = re.compile(r'^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$')

Reviewer = Callable[..., object]
RecordSource = Callable[[str, sqlite3.Row], Mapping[str, object]]


def read_subtree(
    fractal_conn: sqlite3.Connection,
    root_branch: str,
) -> list[sqlite3.Row]:
    """Return the `nodes` rows for `root_branch` and all its descendants.

    Mirrors Fractal's own children index: a node's descendants are the branches
    that extend its dotted path. Ordered root-first, then by branch.
    """
    fractal_conn.row_factory = sqlite3.Row
    cur = fractal_conn.execute(
        'SELECT * FROM nodes WHERE node = ? OR node LIKE ? ORDER BY node',
        (root_branch, f'{root_branch}.%'),
    )
    rows = list(cur)
    if not rows:
        raise ValueError(f'no Fractal nodes found under subtree {root_branch!r}')
    return rows


def _latest_lineage(fractal_conn: sqlite3.Connection, branch: str) -> dict[str, int | None]:
    """Best-effort newest run/iter/step ids for a node (lineage FK targets)."""
    out: dict[str, int | None] = {'run_id': None, 'iter_id': None, 'step_id': None}
    row = fractal_conn.execute(
        'SELECT MAX(run_id) AS run_id FROM runs WHERE node = ?', (branch,)
    ).fetchone()
    if row and row['run_id'] is not None:
        out['run_id'] = int(row['run_id'])
        r2 = fractal_conn.execute(
            'SELECT MAX(iter_id) AS iter_id FROM iters WHERE node = ? AND run_id = ?',
            (branch, out['run_id']),
        ).fetchone()
        if r2 and r2['iter_id'] is not None:
            out['iter_id'] = int(r2['iter_id'])
            r3 = fractal_conn.execute(
                'SELECT MAX(step_id) AS step_id FROM steps WHERE iter_id = ?',
                (out['iter_id'],),
            ).fetchone()
            if r3 and r3['step_id'] is not None:
                out['step_id'] = int(r3['step_id'])
    return out


def _validate_addressable(branches: list[str]) -> None:
    bad = [b for b in branches if not _ADDRESS_SEGMENTS.fullmatch(b)]
    if bad:
        raise ValueError(
            'Fractal branch names are not valid coherence addresses '
            f'(need dotted [A-Za-z0-9_] segments): {bad}. '
            'Resolve the identity-mapping policy before enabling coherence epochs.'
        )


def run_epoch(
    *,
    fractal_conn: sqlite3.Connection,
    store: CoherenceStore,
    root_branch: str,
    record_source: RecordSource,
    reviewer: Reviewer = simple_reviewer.review,
    created_by: str = 'operator',
    timeout_seconds: int = 120,
    reviewer_agent: str = 'deterministic-v0',
) -> dict[str, object]:
    """Run one full coherence epoch over a Fractal subtree. Returns a summary."""
    rows = read_subtree(fractal_conn, root_branch)
    branches = [r['node'] for r in rows]
    _validate_addressable(branches)

    # 1. Freeze membership (barrier opens).
    targets = [
        TargetSpec(
            address=r['node'],
            parent=r['node'].rpartition('.')[0] or None,
            depth=r['node'].count('.'),
            lifecycle_status=r['status'],
        )
        for r in rows
    ]
    epoch_id, membership_hash = store.create_epoch(
        root_branch,
        targets,
        created_by=created_by,
        timeout_seconds=timeout_seconds,
        reviewer_agent=reviewer_agent,
    )

    # 2. Each frozen node publishes its compact record (arrivals fill the barrier).
    for r in rows:
        branch = r['node']
        lineage = _latest_lineage(fractal_conn, branch)
        payload = dict(record_source(branch, r))
        payload.setdefault('address', branch)
        payload.setdefault('parent', branch.rpartition('.')[0] or None)
        payload.setdefault('depth', branch.count('.'))
        payload.setdefault('lifecycle_status', r['status'])
        for key in ('run_id', 'iter_id', 'step_id'):
            payload.setdefault(key, lineage[key])
        store.arrive(epoch_id, CompactNodeRecord(**payload))

    # 3. Claim the completed epoch (only succeeds once every target has arrived).
    token = store.try_start_review(epoch_id)
    if token is None:
        raise RuntimeError('barrier incomplete: not all frozen nodes arrived')

    # 4. Review the frozen snapshot (deterministic here; a model in production).
    records = store.get_records(epoch_id)
    result = reviewer(
        epoch_id=epoch_id,
        membership_hash=membership_hash,
        records_by_address=records,
    )

    # 5. Validate + write directives back atomically; nodes resume on release.
    store.release(epoch_id, result, owner_address=root_branch, review_token=token)

    epoch = store.get_epoch(epoch_id)
    directives = store.get_directives(epoch_id)
    return {
        'epoch_id': epoch_id,
        'membership': sorted(branches),
        'status': epoch['status'],
        'summary': epoch['summary'],
        'directives': [
            {
                'address': d['address'],
                'action': d['action'],
                'priority': d['priority'],
                'targets': d['targets_json'],
                'rationale': d['rationale'],
            }
            for d in directives
        ],
    }
