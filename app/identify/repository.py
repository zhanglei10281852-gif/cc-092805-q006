from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS identify_clues (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 source_type TEXT NOT NULL CHECK(source_type IN ('hospital','police','family')),
 clue_kind TEXT NOT NULL CHECK(clue_kind IN ('name_fragment','belonging','kinship')),
 original_text TEXT NOT NULL,
 trust_level TEXT NOT NULL CHECK(trust_level IN ('high','medium','low')),
 reporter TEXT NOT NULL,
 idempotency_key TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
 withdrawn_by TEXT NOT NULL DEFAULT '',
 withdrawn_reason TEXT NOT NULL DEFAULT '',
 withdrawn_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(case_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_identify_clues_case ON identify_clues(case_id,id);
CREATE TABLE IF NOT EXISTS identify_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 candidate_name TEXT NOT NULL,
 identity_number TEXT,
 contact_name TEXT NOT NULL DEFAULT '',
 contact_phone TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','merged','excluded','withdrawn','confirmed')),
 merged_into_id INTEGER REFERENCES identify_candidates(id),
 resolution_reason TEXT NOT NULL DEFAULT '',
 resolved_by TEXT NOT NULL DEFAULT '',
 resolved_at TEXT,
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identify_candidates_case ON identify_candidates(case_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_identify_confirmed_identity ON identify_candidates(identity_number) WHERE status='confirmed';
CREATE TABLE IF NOT EXISTS identify_candidate_clues (
 candidate_id INTEGER NOT NULL REFERENCES identify_candidates(id),
 clue_id INTEGER NOT NULL REFERENCES identify_clues(id),
 linked_by TEXT NOT NULL,
 linked_at TEXT NOT NULL,
 PRIMARY KEY(candidate_id,clue_id)
);
CREATE TABLE IF NOT EXISTS identify_claims (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 candidate_id INTEGER NOT NULL REFERENCES identify_candidates(id),
 claimant_name TEXT NOT NULL,
 claimant_identity TEXT NOT NULL,
 claimant_phone TEXT NOT NULL,
 relation_to_decedent TEXT NOT NULL,
 request_digest TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','materials_verified','confirmed','rejected')),
 materials_verified_by TEXT NOT NULL DEFAULT '',
 materials_verified_at TEXT,
 materials_note TEXT NOT NULL DEFAULT '',
 reviewed_by TEXT NOT NULL DEFAULT '',
 reviewed_at TEXT,
 review_note TEXT NOT NULL DEFAULT '',
 created_by TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identify_claims_case ON identify_claims(case_id,id);
CREATE TABLE IF NOT EXISTS identify_claim_materials (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 claim_id INTEGER NOT NULL REFERENCES identify_claims(id),
 material_type TEXT NOT NULL,
 reference TEXT NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 checked INTEGER NOT NULL DEFAULT 0,
 checked_by TEXT NOT NULL DEFAULT '',
 checked_at TEXT,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identify_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL UNIQUE REFERENCES mortuary_cases(id),
 claim_id INTEGER NOT NULL REFERENCES identify_claims(id),
 confirmed_candidate_id INTEGER NOT NULL REFERENCES identify_candidates(id),
 rationale TEXT NOT NULL DEFAULT '',
 excluded_json TEXT NOT NULL DEFAULT '[]',
 decided_by TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identify_decision_clues (
 decision_id INTEGER NOT NULL REFERENCES identify_decisions(id),
 clue_id INTEGER NOT NULL REFERENCES identify_clues(id),
 PRIMARY KEY(decision_id,clue_id)
);
CREATE TABLE IF NOT EXISTS identify_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 case_id INTEGER NOT NULL,
 event_type TEXT NOT NULL,
 actor TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identify_events_case ON identify_events(case_id,id);
'''


class IdentifyRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, case_id: int, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute("INSERT INTO identify_events(case_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (case_id, event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def clue(self, clue_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_clues WHERE id=?", (clue_id,)).fetchone())

    def clue_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_clues WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def clues_for_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identify_clues WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def clue_in_decision(self, clue_id: int) -> bool:
        row = self.connection.execute("SELECT 1 FROM identify_decision_clues WHERE clue_id=? LIMIT 1", (clue_id,)).fetchone()
        return row is not None

    def candidate(self, candidate_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_candidates WHERE id=?", (candidate_id,)).fetchone())

    def candidates_for_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identify_candidates WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def candidate_clue_ids(self, candidate_id: int) -> list[int]:
        rows = self.connection.execute("SELECT clue_id FROM identify_candidate_clues WHERE candidate_id=? ORDER BY clue_id", (candidate_id,)).fetchall()
        return [int(row[0]) for row in rows]

    def confirmed_identity(self, identity_number: str, exclude_candidate_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_candidates WHERE status='confirmed' AND identity_number=? AND id!=? LIMIT 1", (identity_number, exclude_candidate_id)).fetchone())

    def claim(self, claim_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_claims WHERE id=?", (claim_id,)).fetchone())

    def claim_key(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_claims WHERE idempotency_key=?", (key,)).fetchone())

    def claims_for_case(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identify_claims WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def claim_materials(self, claim_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identify_claim_materials WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        return [dict(row) for row in rows]

    def decision_for_case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM identify_decisions WHERE case_id=?", (case_id,)).fetchone())

    def decision_clues(self, decision_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT c.* FROM identify_clues c JOIN identify_decision_clues d ON d.clue_id=c.id WHERE d.decision_id=? ORDER BY c.id", (decision_id,)).fetchall()
        return [dict(row) for row in rows]

    def timeline(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM identify_events WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
