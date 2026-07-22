"""Validated contracts for synchronized coherence epochs (v0.1.2)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ADDRESS = re.compile(r'^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$')
_HEX64 = re.compile(r'^[0-9a-f]{64}$')


class LifecycleStatus(str, Enum):
    """Lifecycle values used by Fractal node status records."""

    active = 'active'
    paused = 'paused'
    idle = 'idle'
    completed = 'completed'
    stopped = 'stopped'
    exited = 'exited'
    killed = 'killed'
    failed = 'failed'
    retired = 'retired'


class WorkState(str, Enum):
    """Small semantic state supplied at the synchronization boundary."""

    progressing = 'progressing'
    blocked = 'blocked'
    complete = 'complete'
    failed = 'failed'


class VerificationStatus(str, Enum):
    """Verification state for a published claim."""

    unverified = 'unverified'
    partial = 'partial'
    verified = 'verified'
    failed = 'failed'


class RequestedAction(str, Enum):
    """Action a node asks the coherence reviewer to consider."""

    none = 'none'
    coordinate = 'coordinate'
    escalate = 'escalate'
    revise = 'revise'
    stop = 'stop'


class DirectiveAction(str, Enum):
    """Corrections the reviewer may route back to a participating node."""

    revise = 'revise'
    reconcile = 'reconcile'
    reassign = 'reassign'
    verify = 'verify'
    narrow = 'narrow'
    stop = 'stop'
    escalate = 'escalate'
    merge_order = 'merge_order'


class Priority(str, Enum):
    """Directive urgency."""

    low = 'low'
    normal = 'normal'
    high = 'high'


def _clean_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError('empty string not allowed')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('control characters not allowed')
    return value


def _clean_address(value: str) -> str:
    value = _clean_text(value)
    if len(value) > 250:
        raise ValueError('address exceeds Fractal branch length limit')
    if not _ADDRESS.fullmatch(value):
        raise ValueError('address must contain dotted alphanumeric/underscore segments')
    return value


def _finite_money(value: float | None) -> float | None:
    if value is None:
        return value
    if not math.isfinite(value):
        raise ValueError('non-finite monetary value')
    if value < 0:
        raise ValueError('monetary value must be non-negative')
    return value


class Claim(BaseModel):
    """One bounded claim and references to its evidence."""

    model_config = ConfigDict(extra='forbid')

    id: str = Field(min_length=1, max_length=32)
    text: str = Field(min_length=1, max_length=280)
    verification: VerificationStatus
    evidence: list[str] = Field(default_factory=list, max_length=6)

    @field_validator('id', 'text')
    @classmethod
    def clean(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator('evidence')
    @classmethod
    def clean_evidence(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_text(value) for value in values]
        if any(len(value) > 200 for value in cleaned):
            raise ValueError('evidence entry exceeds 200 characters')
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('evidence entries must be unique')
        return cleaned


class CompactNodeRecord(BaseModel):
    """Canonical state published once by a frozen epoch participant."""

    model_config = ConfigDict(extra='forbid')

    address: str
    parent: str | None = None
    depth: int = Field(ge=0, le=64)
    lifecycle_status: LifecycleStatus
    work_state: WorkState

    spent_usd: float | None = None
    remaining_usd: float | None = None
    cost_uncertain: bool = False

    objective: str = Field(min_length=1, max_length=300)
    delta: str = Field(min_length=1, max_length=400)
    claims: list[Claim] = Field(default_factory=list, max_length=6)
    blockers: list[str] = Field(default_factory=list, max_length=4)
    known_conflicts: list[str] = Field(default_factory=list, max_length=6)
    requested_action: RequestedAction = RequestedAction.none

    run_id: int | None = Field(None, ge=1)
    iter_id: int | None = Field(None, ge=1)
    step_id: int | None = Field(None, ge=1)

    @field_validator('address')
    @classmethod
    def clean_record_address(cls, value: str) -> str:
        return _clean_address(value)

    @field_validator('parent')
    @classmethod
    def clean_parent_address(cls, value: str | None) -> str | None:
        return _clean_address(value) if value is not None else None

    @field_validator('objective', 'delta')
    @classmethod
    def clean_semantic_text(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator('spent_usd', 'remaining_usd')
    @classmethod
    def finite_money(cls, value: float | None) -> float | None:
        return _finite_money(value)

    @field_validator('blockers', 'known_conflicts')
    @classmethod
    def clean_short_lists(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_text(value) for value in values]
        if any(len(value) > 200 for value in cleaned):
            raise ValueError('list entry exceeds 200 characters')
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('list entries must be unique')
        return cleaned

    @model_validator(mode='after')
    def validate_identity_lineage_and_size(self) -> CompactNodeRecord:
        expected_parent = self.address.rpartition('.')[0] or None
        if self.parent != expected_parent:
            raise ValueError(
                f'parent {self.parent!r} does not match address-derived parent'
                f' {expected_parent!r}'
            )
        expected_depth = self.address.count('.')
        if self.depth != expected_depth:
            raise ValueError(
                f'depth {self.depth} does not match address-derived depth'
                f' {expected_depth}'
            )
        if self.step_id is not None and (self.iter_id is None or self.run_id is None):
            raise ValueError('step_id requires iter_id and run_id')
        if self.iter_id is not None and self.run_id is None:
            raise ValueError('iter_id requires run_id')
        claim_ids = [claim.id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError('claim ids must be unique within a record')
        if len(self.canonical_bytes()) > 1600:
            raise ValueError('canonical record exceeds 1600 UTF-8 bytes')
        return self

    def canonical_bytes(self) -> bytes:
        """Return stable JSON bytes used for persistence and hashing."""
        data = self.model_dump(mode='json', exclude_none=True)
        serialized = json.dumps(
            data,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        )
        return serialized.encode('utf-8')

    def state_hash(self) -> str:
        """Return SHA-256 of :meth:`canonical_bytes`."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class EpochDirective(BaseModel):
    """One identity-bound correction from the coherence reviewer."""

    model_config = ConfigDict(extra='forbid')

    address: str
    action: DirectiveAction
    priority: Priority = Priority.normal
    rationale: str = Field(min_length=1, max_length=400)
    targets: list[str] = Field(default_factory=list, max_length=8)
    claims: list[str] = Field(default_factory=list, max_length=8)
    instructions: str = Field(min_length=1, max_length=600)

    @field_validator('address')
    @classmethod
    def clean_directive_address(cls, value: str) -> str:
        return _clean_address(value)

    @field_validator('rationale', 'instructions')
    @classmethod
    def clean_directive_text(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator('targets')
    @classmethod
    def clean_targets(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_address(value) for value in values]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('directive targets must be unique')
        return cleaned

    @field_validator('claims')
    @classmethod
    def clean_claim_references(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_text(value) for value in values]
        if any(len(value) > 32 for value in cleaned):
            raise ValueError('claim reference exceeds 32 characters')
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('claim references must be unique')
        return cleaned

    @model_validator(mode='after')
    def validate_action_requirements(self) -> EpochDirective:
        needs_targets = {
            DirectiveAction.reconcile,
            DirectiveAction.reassign,
            DirectiveAction.merge_order,
        }
        if self.action in needs_targets and not self.targets:
            raise ValueError(f'{self.action.value} requires non-empty targets')
        return self


class EpochEscalation(BaseModel):
    """One operator-level issue that cannot be resolved from compact records."""

    model_config = ConfigDict(extra='forbid')

    description: str = Field(min_length=1, max_length=400)
    addresses: list[str] = Field(default_factory=list, max_length=16)
    evidence: list[str] = Field(default_factory=list, max_length=16)

    @field_validator('description')
    @classmethod
    def clean_description(cls, value: str) -> str:
        return _clean_text(value)

    @field_validator('addresses')
    @classmethod
    def clean_addresses(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_address(value) for value in values]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('escalation addresses must be unique')
        return cleaned

    @field_validator('evidence')
    @classmethod
    def clean_evidence(cls, values: list[str]) -> list[str]:
        cleaned = [_clean_text(value) for value in values]
        if any(len(value) > 200 for value in cleaned):
            raise ValueError('escalation evidence exceeds 200 characters')
        if len(cleaned) != len(set(cleaned)):
            raise ValueError('escalation evidence must be unique')
        return cleaned


class CoherenceReviewResult(BaseModel):
    """Strict structured output expected from the coherence reviewer."""

    model_config = ConfigDict(extra='forbid')

    epoch_id: int = Field(ge=1)
    membership_hash: str
    summary: str = Field(min_length=1, max_length=600)
    directives: list[EpochDirective] = Field(default_factory=list, max_length=32)
    escalations: list[EpochEscalation] = Field(default_factory=list, max_length=16)

    @field_validator('membership_hash')
    @classmethod
    def validate_hash_format(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError('membership_hash must be 64-character lowercase hex')
        return value

    @field_validator('summary')
    @classmethod
    def clean_summary(cls, value: str) -> str:
        return _clean_text(value)

    def validate_against_epoch(
        self,
        *,
        expected_epoch_id: int,
        expected_membership_hash: str,
        membership: set[str],
        records_by_address: dict[str, CompactNodeRecord],
    ) -> None:
        """Validate reviewer output against the frozen epoch snapshot."""
        if self.epoch_id != expected_epoch_id:
            raise ValueError('epoch_id mismatch')
        if self.membership_hash != expected_membership_hash:
            raise ValueError('membership_hash mismatch')
        if set(records_by_address) != membership:
            raise ValueError('record addresses do not exactly match frozen membership')
        for address, record in records_by_address.items():
            if record.address != address:
                raise ValueError(f'record identity does not match storage key: {address}')

        seen: set[str] = set()
        for directive in self.directives:
            if directive.address not in membership:
                raise ValueError(
                    f'directive address not in membership: {directive.address}'
                )
            if directive.address in seen:
                raise ValueError(f'duplicate directive for {directive.address}')
            seen.add(directive.address)
            unknown_targets = set(directive.targets) - membership
            if unknown_targets:
                raise ValueError(
                    f'directive targets not in membership: {sorted(unknown_targets)}'
                )
            claim_ids = {
                claim.id for claim in records_by_address[directive.address].claims
            }
            unknown_claims = set(directive.claims) - claim_ids
            if unknown_claims:
                raise ValueError(
                    f'unknown claim ids on {directive.address}:'
                    f' {sorted(unknown_claims)}'
                )

        known_evidence: set[str] = set()
        for address, record in records_by_address.items():
            for claim in record.claims:
                known_evidence.add(f'{address}:{claim.id}')
                known_evidence.update(claim.evidence)
        for escalation in self.escalations:
            unknown_addresses = set(escalation.addresses) - membership
            if unknown_addresses:
                raise ValueError(
                    f'escalation addresses not in membership:'
                    f' {sorted(unknown_addresses)}'
                )
            unknown_evidence = set(escalation.evidence) - known_evidence
            if unknown_evidence:
                raise ValueError(
                    f'escalation references unknown evidence:'
                    f' {sorted(unknown_evidence)}'
                )
