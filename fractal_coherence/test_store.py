"""Behavioral and adversarial tests for coherence protocol v0.1.2."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

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


def _record(
    address: str,
    *,
    lifecycle_status: LifecycleStatus = LifecycleStatus.active,
    objective: str = 'Do the work',
    delta: str = 'Made progress',
    claim_id: str = 'c1',
) -> CompactNodeRecord:
    parent = address.rpartition('.')[0] or None
    return CompactNodeRecord(
        address=address,
        parent=parent,
        depth=address.count('.'),
        lifecycle_status=lifecycle_status,
        work_state=WorkState.progressing,
        spent_usd=0.5,
        remaining_usd=1.5,
        objective=objective,
        delta=delta,
        claims=[
            Claim(
                id=claim_id,
                text='Basic claim',
                verification=VerificationStatus.partial,
                evidence=[f'src/{address.replace(".", "/")}.py'],
            )
        ],
        blockers=[],
        known_conflicts=[],
        requested_action=RequestedAction.none,
        run_id=1,
        iter_id=2,
        step_id=3,
    )


def _targets(*addresses: str) -> list[TargetSpec]:
    return [
        TargetSpec(
            address,
            address.rpartition('.')[0] or None,
            address.count('.'),
            LifecycleStatus.active,
        )
        for address in addresses
    ]


def _result(
    epoch_id: int,
    membership_hash: str,
    directives: list[EpochDirective] | None = None,
) -> CoherenceReviewResult:
    return CoherenceReviewResult(
        epoch_id=epoch_id,
        membership_hash=membership_hash,
        summary='Review complete.',
        directives=directives or [],
        escalations=[],
    )


@pytest.fixture
def store() -> CoherenceStore:
    value = CoherenceStore.in_memory()
    try:
        yield value
    finally:
        value.close()


def test_create_epoch_and_membership(store: CoherenceStore) -> None:
    epoch_id, membership_hash = store.create_epoch(
        'main',
        _targets('main', 'main.parser', 'main.lexer'),
        created_by='test',
    )

    assert epoch_id >= 1
    assert len(membership_hash) == 64
    assert store.get_membership(epoch_id) == {
        'main',
        'main.parser',
        'main.lexer',
    }
    epoch = store.get_epoch(epoch_id)
    assert epoch['status'] == 'active'
    assert epoch['policy'] == 'augment'
    assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_create_epoch_rejects_live_membership_overlap(store: CoherenceStore) -> None:
    first, _ = store.create_epoch('main', _targets('main.child'))
    with pytest.raises(ValueError, match='already belongs to a live epoch'):
        store.create_epoch('main', _targets('main.child'))

    store.abort(first, 'test cleanup', owner_address='main')
    second, _ = store.create_epoch('main', _targets('main.child'))
    assert second > first


def test_target_identity_must_match_dotted_address(store: CoherenceStore) -> None:
    invalid = [TargetSpec('main.child', 'wrong', 1, LifecycleStatus.active)]
    with pytest.raises(ValueError, match='address-derived parent'):
        store.create_epoch('main', invalid)


def test_arrive_is_idempotent_and_rejects_changed_state(
    store: CoherenceStore,
) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    record = _record('main')
    store.arrive(epoch_id, record)
    store.arrive(epoch_id, record)

    with pytest.raises(ValueError, match='different state'):
        store.arrive(epoch_id, _record('main', delta='Different delta'))


def test_arrive_rejects_unknown_address(store: CoherenceStore) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    with pytest.raises(ValueError, match='not a frozen target'):
        store.arrive(epoch_id, _record('main.unknown'))


def test_arrive_rejects_status_different_from_frozen_target(
    store: CoherenceStore,
) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    paused = _record('main', lifecycle_status=LifecycleStatus.paused)
    with pytest.raises(ValueError, match='differs from frozen target'):
        store.arrive(epoch_id, paused)


def test_try_start_review_requires_complete_membership(
    store: CoherenceStore,
) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main', 'main.child'))
    assert store.try_start_review(epoch_id) is None

    store.arrive(epoch_id, _record('main'))
    assert store.try_start_review(epoch_id) is None

    store.arrive(epoch_id, _record('main.child'))
    token = store.try_start_review(epoch_id)
    assert token is not None
    assert store.try_start_review(epoch_id) is None
    epoch = store.get_epoch(epoch_id)
    assert epoch['status'] == 'reviewing'
    assert epoch['review_token'] == token


def test_concurrent_review_claim_has_exactly_one_winner(tmp_path) -> None:
    database = tmp_path / 'coherence.db'
    setup = CoherenceStore.from_path(database)
    epoch_id, _ = setup.create_epoch('main', _targets('main'))
    setup.arrive(epoch_id, _record('main'))
    setup.close()

    barrier = threading.Barrier(2)
    tokens: list[str | None] = []
    errors: list[BaseException] = []

    def contender() -> None:
        contender_store = CoherenceStore.from_path(database)
        try:
            barrier.wait()
            tokens.append(contender_store.try_start_review(epoch_id))
        except BaseException as error:
            errors.append(error)
        finally:
            contender_store.close()

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(token is not None for token in tokens) == 1


def test_release_writes_sparse_directives_atomically(
    store: CoherenceStore,
) -> None:
    epoch_id, membership_hash = store.create_epoch(
        'main',
        _targets('main', 'main.child'),
    )
    store.arrive(epoch_id, _record('main'))
    store.arrive(epoch_id, _record('main.child'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    directive = EpochDirective(
        address='main.child',
        action=DirectiveAction.revise,
        priority=Priority.normal,
        rationale='Align with the parent contract',
        claims=['c1'],
        instructions='Update offsets to match the parent assumptions.',
    )
    store.release(
        epoch_id,
        _result(epoch_id, membership_hash, [directive]),
        owner_address='main',
        review_token=review_token,
        input_tokens=1200,
        output_tokens=300,
        cost_usd=0.02,
    )

    epoch = store.get_epoch(epoch_id)
    assert epoch['status'] == 'released'
    assert epoch['reviewer_input_tokens'] == 1200
    directives = store.get_directives(epoch_id)
    assert [row['address'] for row in directives] == ['main.child']


def test_release_rejects_wrong_epoch_identity(store: CoherenceStore) -> None:
    epoch_id, membership_hash = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    bad = _result(epoch_id + 99, membership_hash)
    with pytest.raises(ValueError, match='epoch_id mismatch'):
        store.release(
            epoch_id,
            bad,
            owner_address='main',
            review_token=review_token,
        )


def test_release_rejects_wrong_membership_hash(store: CoherenceStore) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    bad = _result(epoch_id, '0' * 64)
    with pytest.raises(ValueError, match='membership_hash mismatch'):
        store.release(
            epoch_id,
            bad,
            owner_address='main',
            review_token=review_token,
        )


def test_release_rejects_address_outside_membership(
    store: CoherenceStore,
) -> None:
    epoch_id, membership_hash = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    directive = EpochDirective(
        address='main.not_a_target',
        action=DirectiveAction.stop,
        rationale='Invalid target',
        instructions='Stop.',
    )
    with pytest.raises(ValueError, match='not in membership'):
        store.release(
            epoch_id,
            _result(epoch_id, membership_hash, [directive]),
            owner_address='main',
            review_token=review_token,
        )


def test_release_rejects_unknown_claim_reference(store: CoherenceStore) -> None:
    epoch_id, membership_hash = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    directive = EpochDirective(
        address='main',
        action=DirectiveAction.verify,
        rationale='Check the claim.',
        claims=['missing'],
        instructions='Verify it.',
    )
    with pytest.raises(ValueError, match='unknown claim ids'):
        store.release(
            epoch_id,
            _result(epoch_id, membership_hash, [directive]),
            owner_address='main',
            review_token=review_token,
        )


def test_release_requires_owner_and_winning_token(store: CoherenceStore) -> None:
    epoch_id, membership_hash = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None
    result = _result(epoch_id, membership_hash)

    with pytest.raises(ValueError, match='does not own'):
        store.release(
            epoch_id,
            result,
            owner_address='other',
            review_token=review_token,
        )
    with pytest.raises(ValueError, match='token mismatch'):
        store.release(
            epoch_id,
            result,
            owner_address='main',
            review_token='wrong-token',
        )


def test_state_hash_drift_is_detected(store: CoherenceStore) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    store.arrive(epoch_id, _record('main'))
    row = store.conn.execute(
        'SELECT state_json, state_hash FROM coherence_records WHERE epoch_id = ?',
        (epoch_id,),
    ).fetchone()
    data = json.loads(row['state_json'])
    data['delta'] = 'Tampered but still valid'
    tampered = json.dumps(
        data,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    )
    assert hashlib.sha256(tampered.encode()).hexdigest() != row['state_hash']
    store.conn.execute(
        'UPDATE coherence_records SET state_json = ? WHERE epoch_id = ?',
        (tampered, epoch_id),
    )
    store.conn.commit()

    with pytest.raises(ValueError, match='state hash mismatch'):
        store.get_records(epoch_id)


def test_abort_requires_epoch_owner(store: CoherenceStore) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    with pytest.raises(ValueError, match='does not own'):
        store.abort(epoch_id, 'timeout', owner_address='other')

    store.abort(epoch_id, 'timeout', owner_address='main')
    epoch = store.get_epoch(epoch_id)
    assert epoch['status'] == 'aborted'
    assert epoch['abort_reason'] == 'timeout'


def test_database_rejects_directive_outside_membership(
    store: CoherenceStore,
) -> None:
    epoch_id, _ = store.create_epoch('main', _targets('main'))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            """
            INSERT INTO coherence_directives (
                epoch_id, address, action, rationale, instructions
            ) VALUES (?, ?, 'stop', 'invalid', 'invalid')
            """,
            (epoch_id, 'main.unknown'),
        )
    store.conn.rollback()


def test_sparse_output_absence_means_no_correction(
    store: CoherenceStore,
) -> None:
    epoch_id, membership_hash = store.create_epoch(
        'main',
        _targets('main', 'main.child'),
    )
    store.arrive(epoch_id, _record('main'))
    store.arrive(epoch_id, _record('main.child'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None

    directive = EpochDirective(
        address='main.child',
        action=DirectiveAction.revise,
        rationale='Fix drift.',
        instructions='Align with the parent.',
    )
    store.release(
        epoch_id,
        _result(epoch_id, membership_hash, [directive]),
        owner_address='main',
        review_token=review_token,
    )
    assert [row['address'] for row in store.get_directives(epoch_id)] == [
        'main.child'
    ]


def test_supervisor_aborts_expired_review(store: CoherenceStore) -> None:
    epoch_id, _ = store.create_epoch(
        'main',
        _targets('main'),
        timeout_seconds=30,
    )
    store.arrive(epoch_id, _record('main'))
    assert store.try_start_review(epoch_id) is not None
    future = datetime.now(timezone.utc) + timedelta(seconds=45)

    assert store.supervise_timeouts(now=future) == [epoch_id]
    assert store.get_epoch(epoch_id)['status'] == 'aborted'


def test_supervisor_aborts_expired_arrival_barrier(
    store: CoherenceStore,
) -> None:
    epoch_id, _ = store.create_epoch(
        'main',
        _targets('main', 'main.child'),
        timeout_seconds=10,
    )
    store.arrive(epoch_id, _record('main'))
    future = datetime.now(timezone.utc) + timedelta(seconds=20)

    assert store.supervise_timeouts(now=future) == [epoch_id]
    assert store.get_epoch(epoch_id)['status'] == 'aborted'


def test_supervisor_ignores_released_epoch(store: CoherenceStore) -> None:
    epoch_id, membership_hash = store.create_epoch(
        'main',
        _targets('main'),
        timeout_seconds=5,
    )
    store.arrive(epoch_id, _record('main'))
    review_token = store.try_start_review(epoch_id)
    assert review_token is not None
    store.release(
        epoch_id,
        _result(epoch_id, membership_hash),
        owner_address='main',
        review_token=review_token,
    )

    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert store.supervise_timeouts(now=future) == []
    assert store.get_epoch(epoch_id)['status'] == 'released'
