"""SQLite epoch protocol for the synchronized coherence prototype (v0.1.2)."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import CompactNodeRecord, CoherenceReviewResult, LifecycleStatus

_ADDRESS = re.compile(r'^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$')


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _membership_hash(addresses: Sequence[str]) -> str:
    canonical = json.dumps(sorted(addresses), separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _validate_address(address: str) -> str:
    if address != address.strip() or not address:
        raise ValueError('address must be non-empty without surrounding whitespace')
    if len(address) > 250 or not _ADDRESS.fullmatch(address):
        raise ValueError(f'invalid Fractal branch address: {address!r}')
    return address


def _validate_nonnegative(value: int | float | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    if value < 0:
        raise ValueError(f'{name} must be non-negative')


@dataclass(frozen=True)
class TargetSpec:
    """Frozen structural identity for one epoch participant."""

    address: str
    parent: str | None
    depth: int
    lifecycle_status: LifecycleStatus | str

    def normalized(self) -> tuple[str, str | None, int, str]:
        """Validate and return values ready for SQLite insertion."""
        address = _validate_address(self.address)
        parent = _validate_address(self.parent) if self.parent is not None else None
        expected_parent = address.rpartition('.')[0] or None
        if parent != expected_parent:
            raise ValueError(
                f'target parent {parent!r} does not match address-derived parent'
                f' {expected_parent!r}'
            )
        expected_depth = address.count('.')
        if self.depth != expected_depth:
            raise ValueError(
                f'target depth {self.depth} does not match address-derived depth'
                f' {expected_depth}'
            )
        try:
            lifecycle = LifecycleStatus(self.lifecycle_status).value
        except ValueError as error:
            raise ValueError(
                f'unknown Fractal lifecycle status: {self.lifecycle_status!r}'
            ) from error
        return address, parent, self.depth, lifecycle


class CoherenceStore:
    """Own atomic persistence transitions for coherence epochs."""

    def __init__(self, connection: sqlite3.Connection):
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.execute('PRAGMA busy_timeout = 5000')
        if self.conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise RuntimeError('SQLite foreign-key enforcement could not be enabled')

    @classmethod
    def from_path(cls, path: str | Path) -> CoherenceStore:
        """Open or create a file-backed protocol store."""
        connection = sqlite3.connect(str(path), timeout=5.0)
        store = cls(connection)
        store.init_schema()
        return store

    @classmethod
    def in_memory(cls) -> CoherenceStore:
        """Create an isolated in-memory protocol store."""
        store = cls(sqlite3.connect(':memory:'))
        store.init_schema()
        return store

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self.conn.close()

    def init_schema(self) -> None:
        """Create the standalone protocol tables."""
        schema_path = Path(__file__).with_name('schema.sql')
        self.conn.executescript(schema_path.read_text(encoding='utf-8'))
        self.conn.commit()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Cursor]:
        """Run one writer transaction with an immediate SQLite reservation."""
        self.conn.execute('BEGIN IMMEDIATE')
        cursor = self.conn.cursor()
        try:
            yield cursor
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def create_epoch(
        self,
        root_address: str,
        targets: Sequence[TargetSpec],
        *,
        created_by: str = 'operator',
        timeout_seconds: int = 120,
        reviewer_agent: str | None = None,
    ) -> tuple[int, str]:
        """Create an active epoch and atomically freeze its membership."""
        root_address = _validate_address(root_address)
        if not created_by.strip():
            raise ValueError('created_by must be non-empty')
        if timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive')
        if not targets:
            raise ValueError('targets must be non-empty')

        normalized = [target.normalized() for target in targets]
        addresses = [target[0] for target in normalized]
        if len(addresses) != len(set(addresses)):
            raise ValueError('duplicate target addresses')
        outside = [
            address
            for address in addresses
            if address != root_address and not address.startswith(f'{root_address}.')
        ]
        if outside:
            raise ValueError(f'targets are outside subtree {root_address}: {outside}')

        membership_hash = _membership_hash(addresses)
        with self._immediate() as cursor:
            placeholders = ', '.join('?' for _ in addresses)
            cursor.execute(
                'SELECT t.address FROM coherence_targets t'
                ' JOIN coherence_epochs e ON e.epoch_id = t.epoch_id'
                f" WHERE e.status IN ('active', 'reviewing')"
                f' AND t.address IN ({placeholders}) LIMIT 1',
                tuple(addresses),
            )
            overlap = cursor.fetchone()
            if overlap is not None:
                raise ValueError(
                    f"target {overlap['address']} already belongs to a live epoch"
                )
            cursor.execute(
                """
                INSERT INTO coherence_epochs (
                    created_by, root_address, policy, timeout_seconds,
                    status, membership_hash, reviewer_agent
                ) VALUES (?, ?, 'augment', ?, 'active', ?, ?)
                """,
                (
                    created_by.strip(),
                    root_address,
                    timeout_seconds,
                    membership_hash,
                    reviewer_agent,
                ),
            )
            epoch_id = cursor.lastrowid
            assert epoch_id is not None
            cursor.executemany(
                """
                INSERT INTO coherence_targets (
                    epoch_id, address, parent, depth, lifecycle_status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [(epoch_id, *target) for target in normalized],
            )
        return int(epoch_id), membership_hash

    def arrive(self, epoch_id: int, record: CompactNodeRecord) -> None:
        """Publish one record, idempotently when the canonical bytes match."""
        state_bytes = record.canonical_bytes()
        state_json = state_bytes.decode('utf-8')
        state_hash = hashlib.sha256(state_bytes).hexdigest()

        with self._immediate() as cursor:
            cursor.execute(
                'SELECT status FROM coherence_epochs WHERE epoch_id = ?',
                (epoch_id,),
            )
            epoch = cursor.fetchone()
            if epoch is None:
                raise ValueError(f'unknown epoch {epoch_id}')
            if epoch['status'] != 'active':
                raise ValueError(
                    f"epoch {epoch_id} is not accepting arrivals"
                    f" (status={epoch['status']})"
                )

            cursor.execute(
                """
                SELECT parent, depth, lifecycle_status
                  FROM coherence_targets
                 WHERE epoch_id = ? AND address = ?
                """,
                (epoch_id, record.address),
            )
            target = cursor.fetchone()
            if target is None:
                raise ValueError(
                    f'address {record.address} is not a frozen target of epoch'
                    f' {epoch_id}'
                )
            frozen = (
                target['parent'],
                target['depth'],
                target['lifecycle_status'],
            )
            published = (
                record.parent,
                record.depth,
                record.lifecycle_status.value,
            )
            if published != frozen:
                raise ValueError(
                    f'record identity/status differs from frozen target:'
                    f' published={published!r}, frozen={frozen!r}'
                )

            cursor.execute(
                """
                SELECT state_json, state_hash
                  FROM coherence_records
                 WHERE epoch_id = ? AND address = ?
                """,
                (epoch_id, record.address),
            )
            existing = cursor.fetchone()
            if existing is not None:
                same_hash = hmac.compare_digest(existing['state_hash'], state_hash)
                if same_hash and existing['state_json'] == state_json:
                    return
                raise ValueError(
                    f'address {record.address} already arrived with different state'
                )

            cursor.execute(
                """
                INSERT INTO coherence_records (
                    epoch_id, address, run_id, iter_id, step_id,
                    state_json, state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    epoch_id,
                    record.address,
                    record.run_id,
                    record.iter_id,
                    record.step_id,
                    state_json,
                    state_hash,
                ),
            )

    def try_start_review(self, epoch_id: int) -> str | None:
        """Atomically claim a complete epoch and return its review token."""
        review_token = secrets.token_hex(16)
        now = _utcnow()
        with self._immediate() as cursor:
            cursor.execute(
                """
                UPDATE coherence_epochs
                   SET status = 'reviewing',
                       review_started_at = ?,
                       review_token = ?
                 WHERE epoch_id = ?
                   AND status = 'active'
                   AND NOT EXISTS (
                       SELECT 1
                         FROM coherence_targets t
                        WHERE t.epoch_id = ?
                          AND NOT EXISTS (
                              SELECT 1
                                FROM coherence_records r
                               WHERE r.epoch_id = t.epoch_id
                                 AND r.address = t.address
                          )
                   )
                """,
                (now, review_token, epoch_id, epoch_id),
            )
            won = cursor.rowcount == 1
        return review_token if won else None

    def get_membership(self, epoch_id: int) -> set[str]:
        """Return the frozen addresses for an epoch."""
        rows = self.conn.execute(
            'SELECT address FROM coherence_targets WHERE epoch_id = ?',
            (epoch_id,),
        ).fetchall()
        return {row['address'] for row in rows}

    def get_records(self, epoch_id: int) -> dict[str, CompactNodeRecord]:
        """Load records only after verifying their exact stored hashes."""
        rows = self.conn.execute(
            """
            SELECT address, state_json, state_hash
              FROM coherence_records
             WHERE epoch_id = ?
            """,
            (epoch_id,),
        ).fetchall()
        records: dict[str, CompactNodeRecord] = {}
        for row in rows:
            state_bytes = row['state_json'].encode('utf-8')
            actual_hash = hashlib.sha256(state_bytes).hexdigest()
            if not hmac.compare_digest(actual_hash, row['state_hash']):
                raise ValueError(
                    f"state hash mismatch for epoch {epoch_id}, address"
                    f" {row['address']}"
                )
            record = CompactNodeRecord.model_validate_json(state_bytes)
            if record.address != row['address']:
                raise ValueError(
                    f"stored record identity mismatch for address {row['address']}"
                )
            records[row['address']] = record
        return records

    def get_epoch(self, epoch_id: int) -> sqlite3.Row:
        """Return one epoch or raise for an unknown id."""
        row = self.conn.execute(
            'SELECT * FROM coherence_epochs WHERE epoch_id = ?',
            (epoch_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f'unknown epoch {epoch_id}')
        return row

    def release(
        self,
        epoch_id: int,
        result: CoherenceReviewResult,
        *,
        owner_address: str,
        review_token: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Validate and atomically persist one claimed review result."""
        owner_address = _validate_address(owner_address)
        _validate_nonnegative(input_tokens, 'input_tokens')
        _validate_nonnegative(output_tokens, 'output_tokens')
        _validate_nonnegative(cost_usd, 'cost_usd')
        if not review_token:
            raise ValueError('review_token must be non-empty')

        with self._immediate() as cursor:
            cursor.execute(
                'SELECT * FROM coherence_epochs WHERE epoch_id = ?',
                (epoch_id,),
            )
            epoch = cursor.fetchone()
            if epoch is None:
                raise ValueError(f'unknown epoch {epoch_id}')
            if epoch['root_address'] != owner_address:
                raise ValueError(
                    f'{owner_address} does not own coherence epoch {epoch_id}'
                )
            if epoch['status'] != 'reviewing':
                raise ValueError(
                    f"epoch {epoch_id} is not reviewing (status={epoch['status']})"
                )
            stored_token = epoch['review_token'] or ''
            if not hmac.compare_digest(stored_token, review_token):
                raise ValueError('review token mismatch')

            membership = self.get_membership(epoch_id)
            records = self.get_records(epoch_id)
            result.validate_against_epoch(
                expected_epoch_id=epoch_id,
                expected_membership_hash=epoch['membership_hash'],
                membership=membership,
                records_by_address=records,
            )

            for directive in result.directives:
                cursor.execute(
                    """
                    INSERT INTO coherence_directives (
                        epoch_id, address, action, priority, rationale,
                        targets_json, claims_json, instructions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        epoch_id,
                        directive.address,
                        directive.action.value,
                        directive.priority.value,
                        directive.rationale,
                        json.dumps(directive.targets, separators=(',', ':')),
                        json.dumps(directive.claims, separators=(',', ':')),
                        directive.instructions,
                    ),
                )
            for escalation in result.escalations:
                cursor.execute(
                    """
                    INSERT INTO coherence_escalations (
                        epoch_id, description, addresses_json, evidence_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        epoch_id,
                        escalation.description,
                        json.dumps(escalation.addresses, separators=(',', ':')),
                        json.dumps(escalation.evidence, separators=(',', ':')),
                    ),
                )

            cursor.execute(
                """
                UPDATE coherence_epochs
                   SET status = 'released',
                       released_at = ?,
                       summary = ?,
                       reviewer_input_tokens = ?,
                       reviewer_output_tokens = ?,
                       reviewer_cost_usd = ?
                 WHERE epoch_id = ?
                   AND status = 'reviewing'
                   AND review_token = ?
                """,
                (
                    _utcnow(),
                    result.summary,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    epoch_id,
                    review_token,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError('failed to transition epoch to released')

    def abort(self, epoch_id: int, reason: str, *, owner_address: str) -> None:
        """Abort a live epoch when called under its owning subtree root."""
        owner_address = _validate_address(owner_address)
        reason = reason.strip()
        if not reason:
            raise ValueError('abort reason must be non-empty')
        with self._immediate() as cursor:
            cursor.execute(
                'SELECT root_address FROM coherence_epochs WHERE epoch_id = ?',
                (epoch_id,),
            )
            epoch = cursor.fetchone()
            if epoch is None:
                raise ValueError(f'unknown epoch {epoch_id}')
            if epoch['root_address'] != owner_address:
                raise ValueError(
                    f'{owner_address} does not own coherence epoch {epoch_id}'
                )
            cursor.execute(
                """
                UPDATE coherence_epochs
                   SET status = 'aborted',
                       aborted_at = ?,
                       abort_reason = ?
                 WHERE epoch_id = ?
                   AND status IN ('active', 'reviewing')
                """,
                (_utcnow(), reason, epoch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f'epoch {epoch_id} could not be aborted from its current status'
                )

    def _abort_timed_out(
        self,
        epoch_id: int,
        status: str,
        reason: str,
    ) -> bool:
        """Abort only if the supervisor's observed status is still current."""
        with self._immediate() as cursor:
            cursor.execute(
                """
                UPDATE coherence_epochs
                   SET status = 'aborted',
                       aborted_at = ?,
                       abort_reason = ?
                 WHERE epoch_id = ? AND status = ?
                """,
                (_utcnow(), reason, epoch_id, status),
            )
            return cursor.rowcount == 1

    def supervise_timeouts(self, now: datetime | None = None) -> list[int]:
        """Abort expired arrival barriers and reviewer claims."""
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('supervisor time must be timezone-aware')

        rows = self.conn.execute(
            """
            SELECT epoch_id, status, created_at, review_started_at, timeout_seconds
              FROM coherence_epochs
             WHERE status IN ('active', 'reviewing')
            """
        ).fetchall()
        aborted: list[int] = []
        for row in rows:
            status = row['status']
            started = row['review_started_at'] if status == 'reviewing' else row['created_at']
            if not started:
                continue
            started_at = datetime.fromisoformat(started.replace('Z', '+00:00'))
            elapsed = (now - started_at).total_seconds()
            timeout = int(row['timeout_seconds'])
            if elapsed < timeout:
                continue
            phase = 'review' if status == 'reviewing' else 'arrival barrier'
            reason = f'{phase} timeout after {elapsed:.1f}s'
            epoch_id = int(row['epoch_id'])
            if self._abort_timed_out(epoch_id, status, reason):
                aborted.append(epoch_id)
        return aborted

    def get_directives(self, epoch_id: int) -> list[sqlite3.Row]:
        """Return persisted directives ordered by address."""
        rows = self.conn.execute(
            """
            SELECT * FROM coherence_directives
             WHERE epoch_id = ?
             ORDER BY address
            """,
            (epoch_id,),
        ).fetchall()
        return list(rows)
