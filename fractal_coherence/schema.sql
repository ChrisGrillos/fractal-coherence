-- Fractal Synchronized Coherence Mode v0.1.2
-- Standalone protocol schema. Fractal integration should add lineage FKs to
-- runs(run_id), iters(iter_id), and steps(step_id).

CREATE TABLE IF NOT EXISTS coherence_epochs (
    epoch_id                INTEGER PRIMARY KEY,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by              TEXT    NOT NULL,
    root_address            TEXT    NOT NULL,
    policy                  TEXT    NOT NULL DEFAULT 'augment'
                                    CHECK (policy = 'augment'),
    timeout_seconds         INTEGER NOT NULL DEFAULT 120
                                    CHECK (timeout_seconds > 0),
    status                  TEXT    NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active', 'reviewing', 'released', 'aborted')),
    membership_hash         TEXT    NOT NULL
                                    CHECK (
                                        length(membership_hash) = 64
                                        AND membership_hash NOT GLOB '*[^0-9a-f]*'
                                    ),
    review_started_at       TEXT,
    review_token            TEXT    UNIQUE,
    reviewer_agent          TEXT,
    reviewer_input_tokens   INTEGER CHECK (reviewer_input_tokens >= 0),
    reviewer_output_tokens  INTEGER CHECK (reviewer_output_tokens >= 0),
    reviewer_cost_usd       REAL    CHECK (reviewer_cost_usd >= 0.0),
    summary                 TEXT,
    released_at             TEXT,
    aborted_at              TEXT,
    abort_reason            TEXT
);

CREATE INDEX IF NOT EXISTS idx_coherence_epochs_status
    ON coherence_epochs(status);

CREATE TABLE IF NOT EXISTS coherence_targets (
    epoch_id            INTEGER NOT NULL,
    address             TEXT    NOT NULL,
    parent              TEXT,
    depth               INTEGER NOT NULL CHECK (depth >= 0),
    lifecycle_status    TEXT    NOT NULL
                                CHECK (lifecycle_status IN (
                                    'active', 'paused', 'idle', 'completed',
                                    'stopped', 'exited', 'killed', 'failed', 'retired'
                                )),
    PRIMARY KEY (epoch_id, address),
    FOREIGN KEY (epoch_id) REFERENCES coherence_epochs(epoch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coherence_targets_epoch
    ON coherence_targets(epoch_id);

-- One row is one arrival. state_hash covers the exact UTF-8 bytes in state_json.
CREATE TABLE IF NOT EXISTS coherence_records (
    epoch_id            INTEGER NOT NULL,
    address             TEXT    NOT NULL,
    run_id              INTEGER,
    iter_id             INTEGER,
    step_id             INTEGER,
    state_json          TEXT    NOT NULL CHECK (json_valid(state_json)),
    state_hash          TEXT    NOT NULL
                                CHECK (
                                    length(state_hash) = 64
                                    AND state_hash NOT GLOB '*[^0-9a-f]*'
                                ),
    published_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (epoch_id, address),
    FOREIGN KEY (epoch_id, address)
        REFERENCES coherence_targets(epoch_id, address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coherence_records_epoch
    ON coherence_records(epoch_id);

CREATE TABLE IF NOT EXISTS coherence_directives (
    directive_id        INTEGER PRIMARY KEY,
    epoch_id            INTEGER NOT NULL,
    address             TEXT    NOT NULL,
    action              TEXT    NOT NULL
                                CHECK (action IN (
                                    'revise', 'reconcile', 'reassign', 'verify',
                                    'narrow', 'stop', 'escalate', 'merge_order'
                                )),
    priority            TEXT    NOT NULL DEFAULT 'normal'
                                CHECK (priority IN ('low', 'normal', 'high')),
    rationale           TEXT    NOT NULL,
    targets_json        TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(targets_json)),
    claims_json         TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(claims_json)),
    instructions        TEXT    NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (epoch_id, address),
    FOREIGN KEY (epoch_id, address)
        REFERENCES coherence_targets(epoch_id, address) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coherence_directives_epoch
    ON coherence_directives(epoch_id);

CREATE TABLE IF NOT EXISTS coherence_escalations (
    escalation_id       INTEGER PRIMARY KEY,
    epoch_id            INTEGER NOT NULL,
    description         TEXT    NOT NULL,
    addresses_json      TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(addresses_json)),
    evidence_json       TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (epoch_id) REFERENCES coherence_epochs(epoch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coherence_escalations_epoch
    ON coherence_escalations(epoch_id);
