from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.privacy import mask_id_card, mask_phone, sanitize_text
from app.database import get_connection, transaction
from app.identify.repository import IdentifyRepository

# 可查看证件号码与联系方式原文的职责角色，其余角色一律遮蔽
FULL_DETAIL_ROLES = {"coordinator", "verifier", "reviewer"}
VERIFIER_ROLE = "verifier"
REVIEWER_ROLE = "reviewer"


def _mask_document(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) < 10:
        return value[:2] + "***" if len(value) > 2 else "***"
    return mask_id_card(value)


def _mask_contact(value: str | None) -> str | None:
    if not value:
        return value
    if len(value) < 7:
        return value[:1] + "***" if len(value) > 1 else "***"
    return mask_phone(value)


class IdentifyService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = IdentifyRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    @staticmethod
    def _require_case(repo: IdentifyRepository, case_id: int) -> dict[str, Any]:
        case = repo.case(case_id)
        if case is None:
            raise NotFoundError("逝者业务档案不存在")
        return case

    def _clue_view(self, clue: dict[str, Any], privileged: bool) -> dict[str, Any]:
        view = dict(clue)
        if not privileged:
            view["original_text"] = sanitize_text(view["original_text"])
        return view

    def _candidate_view(self, candidate: dict[str, Any], privileged: bool, repo: IdentifyRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        view = dict(candidate)
        view["clue_ids"] = repo.candidate_clue_ids(candidate["id"])
        if not privileged:
            view["identity_number"] = _mask_document(view["identity_number"])
            view["contact_phone"] = _mask_contact(view["contact_phone"])
        return view

    def _claim_view(self, claim: dict[str, Any], privileged: bool, repo: IdentifyRepository | None = None) -> dict[str, Any]:
        repo = repo or self.repository
        view = dict(claim)
        materials = repo.claim_materials(claim["id"])
        if not privileged:
            view["claimant_identity"] = _mask_document(view["claimant_identity"])
            view["claimant_phone"] = _mask_contact(view["claimant_phone"])
            materials = [dict(material, reference=_mask_document(material["reference"])) for material in materials]
        view["materials"] = materials
        return view

    # ---- 线索报送与撤回 ----

    def report_clue(self, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            self._require_case(repo, case_id)
            existing = repo.clue_key(case_id, payload["idempotency_key"])
            if existing:
                comparable = ("source_type", "clue_kind", "original_text", "trust_level", "reporter")
                if any(existing[key] != payload[key] for key in comparable):
                    raise ConflictError("同一幂等键对应了不同线索内容")
                return existing
            cursor = connection.execute(
                "INSERT INTO identify_clues(case_id,source_type,clue_kind,original_text,trust_level,reporter,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, payload["source_type"], payload["clue_kind"], payload["original_text"], payload["trust_level"], payload["reporter"], payload["idempotency_key"], now, now),
            )
            clue = repo.clue(int(cursor.lastrowid)) or {}
            repo.event(case_id, "clue.reported", actor, {"clue_id": clue["id"], "source_type": payload["source_type"], "clue_kind": payload["clue_kind"], "trust_level": payload["trust_level"]}, now)
            return clue

    def withdraw_clue(self, clue_id: int, reason: str, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            clue = repo.clue(clue_id)
            if clue is None:
                raise NotFoundError("线索不存在")
            if clue["status"] == "withdrawn":
                return clue
            if repo.clue_in_decision(clue_id):
                raise ConflictError("线索已用于认领结论，不能撤回")
            connection.execute("UPDATE identify_clues SET status='withdrawn',withdrawn_by=?,withdrawn_reason=?,withdrawn_at=?,updated_at=? WHERE id=?", (actor, reason, now, now, clue_id))
            repo.event(clue["case_id"], "clue.withdrawn", actor, {"clue_id": clue_id, "reason": reason}, now)
            return repo.clue(clue_id) or {}

    def list_clues(self, case_id: int, role: str = "public") -> list[dict[str, Any]]:
        self._require_case(self.repository, case_id)
        privileged = role in FULL_DETAIL_ROLES
        return [self._clue_view(clue, privileged) for clue in self.repository.clues_for_case(case_id)]

    # ---- 身份候选 ----

    def create_candidate(self, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        identity_number = (payload.get("identity_number") or "").strip().upper() or None
        clue_ids = list(dict.fromkeys(payload["clue_ids"]))
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            self._require_case(repo, case_id)
            if repo.decision_for_case(case_id):
                raise ConflictError("该档案已形成认领结论，不能再新增候选")
            for clue_id in clue_ids:
                clue = repo.clue(clue_id)
                if clue is None or clue["case_id"] != case_id:
                    raise ValidationError("关联线索不存在或不属于该档案")
                if clue["status"] != "active":
                    raise ConflictError("已撤回的线索不能关联新候选")
            cursor = connection.execute(
                "INSERT INTO identify_candidates(case_id,candidate_name,identity_number,contact_name,contact_phone,note,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (case_id, payload["candidate_name"], identity_number, payload["contact_name"], payload["contact_phone"], payload["note"], actor, now, now),
            )
            candidate_id = int(cursor.lastrowid)
            for clue_id in clue_ids:
                connection.execute("INSERT INTO identify_candidate_clues(candidate_id,clue_id,linked_by,linked_at) VALUES(?,?,?,?)", (candidate_id, clue_id, actor, now))
            candidate = repo.candidate(candidate_id) or {}
            repo.event(case_id, "candidate.created", actor, {"candidate_id": candidate_id, "candidate_name": payload["candidate_name"], "clue_ids": clue_ids}, now)
            return candidate

    def merge_candidate(self, candidate_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            source = repo.candidate(candidate_id)
            if source is None:
                raise NotFoundError("身份候选不存在")
            target = repo.candidate(payload["target_candidate_id"])
            if target is None:
                raise NotFoundError("合并目标候选不存在")
            if source["id"] == target["id"]:
                raise ValidationError("候选不能合并到自身")
            if source["case_id"] != target["case_id"]:
                raise ValidationError("只能合并同一档案下的候选")
            if source["status"] == "merged" and source["merged_into_id"] == target["id"]:
                return source
            if source["status"] != "open":
                raise ConflictError("仅待比对状态的候选可以合并")
            if target["status"] != "open":
                raise ConflictError("合并目标候选当前状态不可接收合并")
            connection.execute("INSERT OR IGNORE INTO identify_candidate_clues(candidate_id,clue_id,linked_by,linked_at) SELECT ?,clue_id,linked_by,linked_at FROM identify_candidate_clues WHERE candidate_id=?", (target["id"], source["id"]))
            connection.execute("UPDATE identify_candidates SET status='merged',merged_into_id=?,resolution_reason=?,resolved_by=?,resolved_at=?,updated_at=? WHERE id=?", (target["id"], payload["reason"], actor, now, now, source["id"]))
            repo.event(source["case_id"], "candidate.merged", actor, {"candidate_id": source["id"], "target_candidate_id": target["id"], "reason": payload["reason"]}, now)
            return repo.candidate(source["id"]) or {}

    def _resolve_candidate(self, candidate_id: int, status: str, reason: str, actor: str) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            candidate = repo.candidate(candidate_id)
            if candidate is None:
                raise NotFoundError("身份候选不存在")
            if candidate["status"] == status:
                return candidate
            if candidate["status"] != "open":
                raise ConflictError("候选当前状态不能变更", context={"status": candidate["status"]})
            connection.execute("UPDATE identify_candidates SET status=?,resolution_reason=?,resolved_by=?,resolved_at=?,updated_at=? WHERE id=?", (status, reason, actor, now, now, candidate_id))
            repo.event(candidate["case_id"], f"candidate.{status}", actor, {"candidate_id": candidate_id, "reason": reason}, now)
            return repo.candidate(candidate_id) or {}

    def exclude_candidate(self, candidate_id: int, reason: str, actor: str) -> dict[str, Any]:
        return self._resolve_candidate(candidate_id, "excluded", reason, actor)

    def withdraw_candidate(self, candidate_id: int, reason: str, actor: str) -> dict[str, Any]:
        return self._resolve_candidate(candidate_id, "withdrawn", reason, actor)

    def list_candidates(self, case_id: int, role: str = "public") -> list[dict[str, Any]]:
        self._require_case(self.repository, case_id)
        privileged = role in FULL_DETAIL_ROLES
        return [self._candidate_view(candidate, privileged) for candidate in self.repository.candidates_for_case(case_id)]

    # ---- 家属认领 ----

    def submit_claim(self, case_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = self.now()
        digest_source = {key: payload[key] for key in ("candidate_id", "claimant_name", "claimant_identity", "claimant_phone", "relation_to_decedent", "materials")}
        digest = hashlib.sha256(json.dumps(digest_source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            self._require_case(repo, case_id)
            existing = repo.claim_key(payload["idempotency_key"])
            if existing:
                if existing["case_id"] != case_id or existing["request_digest"] != digest:
                    raise ConflictError("同一幂等键对应了不同认领内容")
                return self._claim_view(existing, privileged=True, repo=repo)
            if repo.decision_for_case(case_id):
                raise ConflictError("该档案已形成认领结论，不能再提交认领")
            candidate = repo.candidate(payload["candidate_id"])
            if candidate is None or candidate["case_id"] != case_id:
                raise ValidationError("认领候选不存在或不属于该档案")
            if candidate["status"] != "open":
                raise ConflictError("候选当前状态不能发起认领")
            cursor = connection.execute(
                "INSERT INTO identify_claims(case_id,candidate_id,claimant_name,claimant_identity,claimant_phone,relation_to_decedent,request_digest,idempotency_key,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, payload["candidate_id"], payload["claimant_name"], payload["claimant_identity"], payload["claimant_phone"], payload["relation_to_decedent"], digest, payload["idempotency_key"], actor, now, now),
            )
            claim_id = int(cursor.lastrowid)
            for material in payload["materials"]:
                connection.execute("INSERT INTO identify_claim_materials(claim_id,material_type,reference,note,created_at) VALUES(?,?,?,?,?)", (claim_id, material["material_type"], material["reference"], material["note"], now))
            repo.event(case_id, "claim.submitted", actor, {"claim_id": claim_id, "candidate_id": payload["candidate_id"], "claimant_name": payload["claimant_name"]}, now)
            return self._claim_view(repo.claim(claim_id) or {}, privileged=True, repo=repo)

    def verify_materials(self, claim_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        if role != VERIFIER_ROLE:
            raise PermissionDeniedError("材料校验须由校验职责人员完成")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            claim = repo.claim(claim_id)
            if claim is None:
                raise NotFoundError("认领申请不存在")
            if claim["status"] == "materials_verified" and payload["passed"]:
                return self._claim_view(claim, privileged=True, repo=repo)
            if claim["status"] != "submitted":
                raise ConflictError("当前认领状态不能进行材料校验")
            connection.execute("UPDATE identify_claim_materials SET checked=1,checked_by=?,checked_at=? WHERE claim_id=?", (actor, now, claim_id))
            status = "materials_verified" if payload["passed"] else "rejected"
            connection.execute("UPDATE identify_claims SET status=?,materials_verified_by=?,materials_verified_at=?,materials_note=?,updated_at=? WHERE id=?", (status, actor, now, payload["note"], now, claim_id))
            event_type = "claim.materials_verified" if payload["passed"] else "claim.materials_rejected"
            repo.event(claim["case_id"], event_type, actor, {"claim_id": claim_id, "note": payload["note"]}, now)
            return self._claim_view(repo.claim(claim_id) or {}, privileged=True, repo=repo)

    def review_claim(self, claim_id: int, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        if role != REVIEWER_ROLE:
            raise PermissionDeniedError("认领复核须由复核职责人员完成")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentifyRepository(connection)
            claim = repo.claim(claim_id)
            if claim is None:
                raise NotFoundError("认领申请不存在")
            case_id = claim["case_id"]
            if claim["status"] == "confirmed":
                if payload["decision"] == "confirm":
                    return self._conclusion_view(repo, case_id, privileged=True)
                raise ConflictError("认领已确认，不能重复复核")
            if claim["status"] == "rejected":
                if payload["decision"] == "reject":
                    return self._claim_view(claim, privileged=True, repo=repo)
                raise ConflictError("认领已驳回，不能确认")
            if claim["status"] != "materials_verified":
                raise ConflictError("认领尚未完成材料校验，不能复核")
            if claim["materials_verified_by"] == actor:
                raise ConflictError("材料校验与复核须由不同职责人员完成")
            if payload["decision"] == "reject":
                connection.execute("UPDATE identify_claims SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?", (actor, now, payload["rationale"], now, claim_id))
                repo.event(case_id, "claim.rejected", actor, {"claim_id": claim_id, "rationale": payload["rationale"]}, now)
                return self._claim_view(repo.claim(claim_id) or {}, privileged=True, repo=repo)
            if repo.decision_for_case(case_id):
                raise ConflictError("该档案已存在认领结论，冲突候选不能重复确认")
            candidate = repo.candidate(claim["candidate_id"])
            if candidate is None or candidate["status"] != "open":
                raise ConflictError("候选身份状态已变化，不能确认认领")
            if candidate["identity_number"]:
                clash = repo.confirmed_identity(candidate["identity_number"], candidate["id"])
                if clash:
                    raise ConflictError("同一证件号码已被确认为其他档案的逝者身份", context={"case_id": clash["case_id"], "candidate_id": clash["id"]})
            excluded = []
            for other in repo.candidates_for_case(case_id):
                if other["id"] == candidate["id"]:
                    continue
                if other["status"] == "open":
                    reason = "身份已经认领结论确认，候选排除"
                    connection.execute("UPDATE identify_candidates SET status='excluded',resolution_reason=?,resolved_by=?,resolved_at=?,updated_at=? WHERE id=?", (reason, actor, now, now, other["id"]))
                    repo.event(case_id, "candidate.excluded", actor, {"candidate_id": other["id"], "reason": reason}, now)
                    excluded.append({"candidate_id": other["id"], "candidate_name": other["candidate_name"], "status": "excluded", "reason": reason})
                else:
                    excluded.append({"candidate_id": other["id"], "candidate_name": other["candidate_name"], "status": other["status"], "reason": other["resolution_reason"]})
            for other_claim in repo.claims_for_case(case_id):
                if other_claim["id"] == claim_id or other_claim["status"] not in ("submitted", "materials_verified"):
                    continue
                note = "身份已确认，认领结论已出"
                connection.execute("UPDATE identify_claims SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?", (actor, now, note, now, other_claim["id"]))
                repo.event(case_id, "claim.rejected", actor, {"claim_id": other_claim["id"], "rationale": note}, now)
            try:
                connection.execute("UPDATE identify_candidates SET status='confirmed',resolved_by=?,resolved_at=?,updated_at=? WHERE id=?", (actor, now, now, candidate["id"]))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("同一证件号码已被确认为其他档案的逝者身份") from exc
            connection.execute("UPDATE identify_claims SET status='confirmed',reviewed_by=?,reviewed_at=?,review_note=?,updated_at=? WHERE id=?", (actor, now, payload["rationale"], now, claim_id))
            try:
                cursor = connection.execute(
                    "INSERT INTO identify_decisions(case_id,claim_id,confirmed_candidate_id,rationale,excluded_json,decided_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (case_id, claim_id, candidate["id"], payload["rationale"], json.dumps(excluded, ensure_ascii=False, sort_keys=True), actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该档案已存在认领结论，冲突候选不能重复确认") from exc
            decision_id = int(cursor.lastrowid)
            clue_ids = repo.candidate_clue_ids(candidate["id"])
            for clue_id in clue_ids:
                connection.execute("INSERT OR IGNORE INTO identify_decision_clues(decision_id,clue_id) VALUES(?,?)", (decision_id, clue_id))
            repo.event(case_id, "candidate.confirmed", actor, {"candidate_id": candidate["id"], "claim_id": claim_id, "decision_id": decision_id}, now)
            repo.event(case_id, "claim.confirmed", actor, {"claim_id": claim_id, "decision_id": decision_id}, now)
            repo.event(case_id, "decision.recorded", actor, {"decision_id": decision_id, "confirmed_candidate_id": candidate["id"], "excluded_candidate_ids": [item["candidate_id"] for item in excluded], "cited_clue_ids": clue_ids}, now)
            return self._conclusion_view(repo, case_id, privileged=True)

    # ---- 查询 ----

    def list_claims(self, case_id: int, role: str = "public") -> list[dict[str, Any]]:
        self._require_case(self.repository, case_id)
        privileged = role in FULL_DETAIL_ROLES
        return [self._claim_view(claim, privileged) for claim in self.repository.claims_for_case(case_id)]

    def _conclusion_view(self, repo: IdentifyRepository, case_id: int, privileged: bool) -> dict[str, Any]:
        decision = repo.decision_for_case(case_id)
        if decision is None:
            raise NotFoundError("该档案尚未形成认领结论")
        claim = repo.claim(decision["claim_id"]) or {}
        candidate = repo.candidate(decision["confirmed_candidate_id"]) or {}
        return {
            "id": decision["id"],
            "case_id": decision["case_id"],
            "claim_id": decision["claim_id"],
            "confirmed_candidate_id": decision["confirmed_candidate_id"],
            "rationale": decision["rationale"],
            "decided_by": decision["decided_by"],
            "created_at": decision["created_at"],
            "confirmed_candidate": self._candidate_view(candidate, privileged, repo),
            "claim": self._claim_view(claim, privileged, repo),
            "cited_clues": [self._clue_view(clue, privileged) for clue in repo.decision_clues(decision["id"])],
            "excluded_candidates": json.loads(decision["excluded_json"]),
        }

    def get_conclusion(self, case_id: int, role: str = "public") -> dict[str, Any]:
        self._require_case(self.repository, case_id)
        return self._conclusion_view(self.repository, case_id, privileged=role in FULL_DETAIL_ROLES)

    def timeline(self, case_id: int) -> list[dict[str, Any]]:
        self._require_case(self.repository, case_id)
        return self.repository.timeline(case_id)
