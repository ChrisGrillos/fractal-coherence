"""End-to-end demo: a coherence epoch over a Fractal subtree.

Loads Fractal's REAL core/schema.sql (nodes/runs/iters/steps) and the
coherence protocol schema into one SQLite database, seeds a small dotted tree
with a deliberate cross-branch conflict, and runs the epoch through the bridge.

Run:  python demo_fractal_epoch.py
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile

from fractal_coherence.store import CoherenceStore
import fractal_bridge


def find_fractal_schema() -> pathlib.Path:
    """Locate Fractal's core/schema.sql from the installed package.

    Order: $FRACTAL_SCHEMA override, then the installed `fractal` package.
    Requires `pip install plasma-fractal` (or a checkout on PYTHONPATH).
    """
    override = os.environ.get('FRACTAL_SCHEMA')
    if override:
        return pathlib.Path(override)
    try:
        import fractal  # the plasma-fractal package
        candidate = pathlib.Path(fractal.__file__).parent / 'core' / 'schema.sql'
        if candidate.is_file():
            return candidate
    except Exception:
        pass
    raise SystemExit(
        'Could not locate Fractal schema. Install Fractal (pip install '
        'plasma-fractal) or set FRACTAL_SCHEMA=/path/to/fractal/core/schema.sql'
    )


def seed_fractal_tree(conn: sqlite3.Connection) -> None:
    """Create a 3-node dotted tree: r (idle root), r.a and r.b (active leaves)."""
    conn.executescript(find_fractal_schema().read_text())
    now = '2026-07-22T12:00:00.000Z'
    nodes = [
        ('r', 'auth service', 'idle'),
        ('r.a', 'login endpoint', 'active'),
        ('r.b', 'login endpoint', 'active'),  # duplicate objective -> conflict
    ]
    for node, title, status in nodes:
        conn.execute(
            'INSERT INTO nodes (node, title, status) VALUES (?, ?, ?)',
            (node, title, status),
        )
    # minimal run/iter/step lineage for r.a so the bridge can attach FK ids
    conn.execute(
        "INSERT INTO runs (run_id, node, status, started_at) VALUES (1, 'r.a', 'active', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO iters (iter_id, node, run_id, iter, status, started_at)"
        " VALUES (1, 'r.a', 1, 1, 'active', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO steps (step_id, node, iter_id, run_id, step, step_name, status, started_at)"
        " VALUES (1, 'r.a', 1, 1, 1, 'BUILD', 'active', ?)",
        (now,),
    )
    conn.commit()


# What each node "publishes" at the SYNC boundary. In production this comes from
# the node itself; here it is fixed so the demo is deterministic.
SEMANTIC = {
    'r': dict(
        work_state='progressing',
        objective='Ship the auth service',
        delta='Coordinating child nodes for login.',
        requested_action='coordinate',
    ),
    'r.a': dict(
        work_state='progressing',
        objective='Implement the login endpoint',
        delta='Built POST /login returning a session cookie.',
        claims=[dict(id='c1', text='POST /login issues a cookie', verification='partial',
                     evidence=['tests/test_login.py'])],
    ),
    'r.b': dict(
        work_state='progressing',
        objective='Implement the login endpoint',  # same objective as r.a
        delta='Built POST /login returning a bearer token.',
        known_conflicts=['r.a'],
        claims=[dict(id='c1', text='POST /login issues a bearer token', verification='partial',
                     evidence=['tests/test_auth.py'])],
    ),
}


def record_source(branch: str, node_row: sqlite3.Row):
    return SEMANTIC[branch]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = pathlib.Path(tmp) / 'fractal.db'

        seed_conn = sqlite3.connect(db_path)
        seed_fractal_tree(seed_conn)

        store = CoherenceStore.from_path(db_path)
        store.init_schema()

        result = fractal_bridge.run_epoch(
            fractal_conn=seed_conn,
            store=store,
            root_branch='r',
            record_source=record_source,
        )

        print('epoch_id     :', result['epoch_id'])
        print('membership   :', result['membership'])
        print('final status :', result['status'])
        print('summary      :', result['summary'])
        print('directives   :')
        for d in result['directives']:
            print(f"  - {d['address']}: {d['action']} (priority={d['priority']},"
                  f" targets={d['targets']})")
            print(f"      rationale: {d['rationale']}")

        assert result['status'] == 'released', 'epoch did not reach released'
        assert result['directives'], 'expected at least one cross-branch directive'
        print('\nOK: barrier -> freeze -> arrival -> review -> release completed.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
