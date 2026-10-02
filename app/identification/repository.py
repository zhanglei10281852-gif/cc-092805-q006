from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.mortuary.repository import SCHEMA as MORTUARY_SCHEMA

SCHEMA = r'''
CREATE TABLE IF NOT EXISTS identity_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 suggested_name TEXT NOT NULL,
 identity_number TEXT NOT NULL DEFAULT '',
 source_type TEXT NOT NULL CHECK(source_type IN ('hospital','police','family','staff','other')),
 source_ref TEXT NOT NULL DEFAULT '',
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
 status TEXT NOT NULL DEFAULT 'active'
   CHECK(status IN ('active','merged','excluded','withdrawn','confirmed')),
 merged_into_id INTEGER REFERENCES identity_candidates(id),
 status_reason TEXT NOT NULL DEFAULT '',
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_candidates_case ON identity_candidates(case_id,status);

CREATE TABLE IF NOT EXISTS identity_clues (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 candidate_id INTEGER NOT NULL REFERENCES identity_candidates(id),
 source_type TEXT NOT NULL CHECK(source_type IN ('hospital','police','family','staff','other')),
 clue_kind TEXT NOT NULL CHECK(clue_kind IN ('name_fragment','belongings','kinship','document','physical','other')),
 raw_text TEXT NOT NULL,
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low')),
 reporter TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_clues_candidate ON identity_clues(candidate_id,id);

CREATE TABLE IF NOT EXISTS identity_claims (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 candidate_id INTEGER NOT NULL REFERENCES identity_candidates(id),
 claimant_name TEXT NOT NULL,
 claimant_identity TEXT NOT NULL,
 relationship TEXT NOT NULL,
 contact_phone TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'submitted'
   CHECK(status IN ('submitted','material_verified','material_rejected','review_approved','review_rejected','withdrawn','superseded')),
 check_result_json TEXT,
 checker_id INTEGER REFERENCES users(id),
 checker_name TEXT NOT NULL DEFAULT '',
 checked_at TEXT,
 review_opinion TEXT NOT NULL DEFAULT '',
 reviewer_id INTEGER REFERENCES users(id),
 reviewer_name TEXT NOT NULL DEFAULT '',
 reviewed_at TEXT,
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(case_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_identity_claims_case ON identity_claims(case_id,id);

CREATE TABLE IF NOT EXISTS identity_claim_documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 claim_id INTEGER NOT NULL REFERENCES identity_claims(id) ON DELETE RESTRICT,
 doc_type TEXT NOT NULL,
 doc_reference TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_documents_claim ON identity_claim_documents(claim_id,id);

CREATE TABLE IF NOT EXISTS identity_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 candidate_id INTEGER REFERENCES identity_candidates(id),
 claim_id INTEGER REFERENCES identity_claims(id),
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 decision_type TEXT NOT NULL CHECK(decision_type IN
   ('merge','exclude','withdraw','material_check','review_confirm','review_reject','claim_withdraw','supersede')),
 reason TEXT NOT NULL DEFAULT '',
 actor_id INTEGER,
 actor_name TEXT NOT NULL,
 before_status TEXT NOT NULL DEFAULT '',
 after_status TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_decisions_case ON identity_decisions(case_id,id);
CREATE INDEX IF NOT EXISTS idx_identity_decisions_claim ON identity_decisions(claim_id,id);

-- 决定一旦做出，所依据的线索/材料即被冻结快照，任何后续状态变化都不能消除它
CREATE TABLE IF NOT EXISTS identity_decision_evidence (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 decision_id INTEGER NOT NULL REFERENCES identity_decisions(id),
 evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('clue','document')),
 ref_id INTEGER NOT NULL,
 snapshot_json TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_evidence_decision ON identity_decision_evidence(decision_id,id);

-- 每个业务档案至多一份确认结论；重复报送与服务重启都无法制造第二份
CREATE TABLE IF NOT EXISTS identity_resolutions (
 case_id INTEGER PRIMARY KEY REFERENCES mortuary_cases(id),
 candidate_id INTEGER NOT NULL REFERENCES identity_candidates(id),
 claim_id INTEGER NOT NULL REFERENCES identity_claims(id),
 confirmer_id INTEGER,
 confirmer_name TEXT NOT NULL,
 conclusion_key TEXT NOT NULL UNIQUE,
 confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 aggregate_type TEXT NOT NULL,
 aggregate_id TEXT NOT NULL,
 event_type TEXT NOT NULL,
 actor TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_event ON identity_events(aggregate_type,aggregate_id,id);
'''


class IdentificationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        # 外键引用 mortuary_cases，先保证殡葬领域表存在
        self.connection.executescript(MORTUARY_SCHEMA)
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO identity_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def case_exists(self, case_id: int) -> bool:
        return self.connection.execute("SELECT 1 FROM mortuary_cases WHERE id=?", (case_id,)).fetchone() is not None

    def candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identity_candidates WHERE id=?", (candidate_id,)).fetchone())

    def candidates(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identity_candidates WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def clues(self, candidate_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identity_clues WHERE candidate_id=? ORDER BY id", (candidate_id,)).fetchall()
        return [dict(row) for row in rows]

    def claim(self, claim_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identity_claims WHERE id=?", (claim_id,)).fetchone())

    def claims(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identity_claims WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def claim_by_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identity_claims WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def claim_natural(self, case_id: int, candidate_id: int, claimant_name: str, claimant_identity: str) -> dict[str, Any] | None:
        return self.one(
            self.connection.execute(
                "SELECT * FROM identity_claims WHERE case_id=? AND candidate_id=? AND claimant_name=? AND claimant_identity=?",
                (case_id, candidate_id, claimant_name, claimant_identity),
            ).fetchone()
        )

    def documents(self, claim_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identity_claim_documents WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        return [dict(row) for row in rows]

    def decision(self, decision_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identity_decisions WHERE id=?", (decision_id,)).fetchone())

    def decisions(self, *, case_id: int | None = None, claim_id: int | None = None, candidate_id: int | None = None) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if case_id is not None:
            conditions.append("case_id=?")
            params.append(case_id)
        if claim_id is not None:
            conditions.append("claim_id=?")
            params.append(claim_id)
        if candidate_id is not None:
            conditions.append("candidate_id=?")
            params.append(candidate_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.connection.execute("SELECT * FROM identity_decisions" + where + " ORDER BY id", params).fetchall()
        return [dict(row) for row in rows]

    def evidence(self, decision_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identity_decision_evidence WHERE decision_id=? ORDER BY id", (decision_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            result.append(item)
        return result

    def resolution(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identity_resolutions WHERE case_id=?", (case_id,)).fetchone())

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM identity_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id",
            (kind, str(aggregate_id)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
