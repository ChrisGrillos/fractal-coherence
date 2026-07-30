"""Persistent verified state with a tamper-evident audit chain.

The verified state lives in a JSON file on disk — outside any model's context
window. It survives across sessions and across overseer instances, so a
larger context window is never treated as a substitute for memory: a decision
verified in one session binds every later session that opens the same state.

Every audit entry is hash-chained (``entry_hash = SHA-256(prev_hash +
canonical entry)``), so silent edits to history are detectable with
:meth:`VerifiedState.verify_chain`.
"""

from __future__ import annotations

import os
import time

from .contracts import canonical_json, sha256_hex

GENESIS = "0" * 64


class TamperError(RuntimeError):
    """Raised when the audit chain fails verification."""


class VerifiedState:
    """Durable state for one objective: requirements, verified decisions,
    epoch counter, and the hash-chained audit log."""

    def __init__(self, path: str, objective_id: str, requirements: tuple):
        self.path = path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                self.data = _loads(handle.read())
            if self.data["objective_id"] != objective_id:
                raise ValueError(
                    f"state file belongs to objective {self.data['objective_id']!r}, "
                    f"not {objective_id!r}"
                )
        else:
            self.data = {
                "objective_id": objective_id,
                "epoch": 0,
                "sessions": 0,
                "requirements": {req: "open" for req in requirements},
                "verified_decisions": [],
                "audit": [],
            }
        self.data["sessions"] += 1
        self._append("session_open", f"session {self.data['sessions']} opened")
        self._save()

    # ------------------------------------------------------------------ audit

    def _append(self, kind: str, summary: str, extra: dict | None = None) -> None:
        prev_hash = self.data["audit"][-1]["hash"] if self.data["audit"] else GENESIS
        entry = {
            "seq": len(self.data["audit"]),
            "ts": round(time.time(), 3),
            "kind": kind,
            "summary": summary,
        }
        if extra:
            entry.update(extra)
        entry["prev_hash"] = prev_hash
        entry["hash"] = sha256_hex(prev_hash + canonical_json(
            {key: value for key, value in entry.items() if key != "hash"}
        ))
        self.data["audit"].append(entry)

    def verify_chain(self) -> int:
        """Verify the audit chain end to end; return entry count or raise."""
        prev_hash = GENESIS
        for entry in self.data["audit"]:
            body = {key: value for key, value in entry.items() if key != "hash"}
            if entry.get("prev_hash") != prev_hash:
                raise TamperError(f"audit entry {entry.get('seq')}: broken link")
            expected = sha256_hex(prev_hash + canonical_json(body))
            if entry.get("hash") != expected:
                raise TamperError(f"audit entry {entry.get('seq')}: hash mismatch")
            prev_hash = entry["hash"]
        return len(self.data["audit"])

    # ------------------------------------------------------- recorded events

    def log_fast_path(self, action) -> None:
        self._append(
            "fast_path_allow",
            f"step {action.step}: {action.tool} -> {action.target}",
            {"action_hash": action.action_hash()},
        )
        requirement = action.requirement
        if requirement in self.data["requirements"]:
            self.data["requirements"][requirement] = "advanced"
        self._save()

    def record_review(self, action, findings, verdict, directive) -> int:
        """Record a synchronized review and bump the epoch. Returns the epoch."""
        self.data["epoch"] += 1
        self._append(
            "synchronized_review",
            f"step {action.step}: {action.tool} -> {verdict.value}",
            {
                "epoch": self.data["epoch"],
                "action_hash": action.action_hash(),
                "findings": [finding.kind.value for finding in findings],
                "rationale": directive.rationale,
            },
        )
        self._save()
        return self.data["epoch"]

    def record_decision(self, rule: str, match: dict, rationale: str) -> None:
        """Persist a verified decision (e.g. ``forbid`` re-running an action
        matching ``match``). Future sessions enforce it without re-review."""
        self.data["verified_decisions"].append(
            {
                "rule": rule,
                "match": match,
                "rationale": rationale,
                "epoch": self.data["epoch"],
            }
        )
        self._append("verified_decision", f"{rule}: {rationale}", {"match": match})
        self._save()

    def matching_decision(self, action) -> dict | None:
        """Return the first verified decision whose match-pattern fits the
        action. Match keys are dotted paths into the compact action dict."""
        compact = action.compact()
        for decision in self.data["verified_decisions"]:
            if all(_lookup(compact, key) == value for key, value in decision["match"].items()):
                return decision
        return None

    # ----------------------------------------------------------------- misc

    def open_requirements(self) -> list:
        return [req for req, status in self.data["requirements"].items() if status == "open"]

    @property
    def epoch(self) -> int:
        return self.data["epoch"]

    @property
    def sessions(self) -> int:
        return self.data["sessions"]

    def _save(self) -> None:
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(self.data))
        os.replace(tmp_path, self.path)


def _lookup(data: dict, dotted_key: str):
    current = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _loads(text: str) -> dict:
    import json

    return json.loads(text)
