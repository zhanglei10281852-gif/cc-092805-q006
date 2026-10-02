from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.privacy import mask_id_card, mask_phone, sanitize_payload, sanitize_text
from app.core.security import Principal
from app.database import get_connection, transaction
from app.identification.repository import IdentificationRepository

CANDIDATE_STATUS_EDITABLE = {"active"}
CLAIM_STATUS_OPEN = {"submitted", "material_verified"}


class IdentificationService:
    """身份候选与家属认领领域服务。

    关键不变量：
    - 线索与材料只增不改，任何决定都对当时证据做哈希快照；
    - 候选可以合并、排除、撤回，但记录不物理删除；
    - 材料校验与复核必须由两名不同工作人员完成；
    - 每个业务档案至多一份确认结论，冲突候选不能同时确认；
    - 查询结果按角色遮蔽证件号与联系方式。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = IdentificationRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ------------------------------------------------------------------ 候选

    def create_candidate(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.write")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            if not repo.case_exists(payload["case_id"]):
                raise NotFoundError("逝者业务档案不存在")
            if repo.resolution(payload["case_id"]) is not None:
                raise ConflictError("该档案身份已确认，不能再新增候选")
            cursor = connection.execute(
                "INSERT INTO identity_candidates(case_id,suggested_name,identity_number,source_type,source_ref,"
                "confidence,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?, 'active',?,?,?)",
                (
                    payload["case_id"], payload["suggested_name"].strip(), payload.get("identity_number", ""),
                    payload["source_type"], payload.get("source_ref", ""), payload["confidence"],
                    principal.display_name, now, now,
                ),
            )
            candidate_id = int(cursor.lastrowid)
            for clue in payload.get("clues", []):
                self._insert_clue(connection, candidate_id, clue, now)
            repo.event("case", payload["case_id"], "candidate.created", principal.display_name,
                       {"candidate_id": candidate_id, "source_type": payload["source_type"], "confidence": payload["confidence"]}, now)
            return self.get_candidate(candidate_id, principal, repo=repo)

    def add_clue(self, principal: Principal, candidate_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.write")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            candidate = self._require_candidate(repo, candidate_id)
            if candidate["status"] not in CANDIDATE_STATUS_EDITABLE:
                raise ConflictError("候选已合并、排除或撤回，不能再追加线索")
            clue = self._insert_clue(connection, candidate_id, payload, now)
            connection.execute("UPDATE identity_candidates SET updated_at=? WHERE id=?", (now, candidate_id))
            repo.event("candidate", candidate_id, "clue.added", principal.display_name,
                       {"clue_id": clue["id"], "clue_kind": payload["clue_kind"], "source_type": payload["source_type"]}, now)
            return self.get_candidate(candidate_id, principal, repo=repo)

    def merge_candidates(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.write")
        source_id, target_id = payload["source_candidate_id"], payload["target_candidate_id"]
        if source_id == target_id:
            raise ValidationError("不能将候选合并到自身")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            source = self._require_candidate(repo, source_id)
            target = self._require_candidate(repo, target_id)
            self._same_case(source, target)
            self._require_open_case(repo, source["case_id"])
            if source["status"] != "active" or target["status"] != "active":
                raise ConflictError("只有调查中的候选可以合并")
            if self._open_claims(repo, source_id):
                raise ConflictError("来源候选存在进行中的认领，请先办结再合并")
            connection.execute(
                "UPDATE identity_candidates SET status='merged',merged_into_id=?,status_reason=?,updated_at=? WHERE id=?",
                (target_id, payload["reason"], now, source_id),
            )
            self._record_decision(
                connection, repo, candidate=source, decision_type="merge", reason=payload["reason"],
                principal=principal, now=now, after_status="merged",
                extra={"merged_into_id": target_id},
                with_clues=[source_id],
            )
            repo.event("case", source["case_id"], "candidate.merged", principal.display_name,
                       {"source_candidate_id": source_id, "target_candidate_id": target_id}, now)
            return self.get_case_identification(source["case_id"], principal, repo=repo)

    def exclude_candidate(self, principal: Principal, candidate_id: int, reason: str) -> dict[str, Any]:
        return self._close_candidate(principal, candidate_id, reason, "excluded", "exclude", "candidate.excluded")

    def withdraw_candidate(self, principal: Principal, candidate_id: int, reason: str) -> dict[str, Any]:
        return self._close_candidate(principal, candidate_id, reason, "withdrawn", "withdraw", "candidate.withdrawn")

    def _close_candidate(self, principal: Principal, candidate_id: int, reason: str, status: str, decision_type: str, event_type: str) -> dict[str, Any]:
        principal.require("identity.write")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            candidate = self._require_candidate(repo, candidate_id)
            self._require_open_case(repo, candidate["case_id"])
            if candidate["status"] != "active":
                raise ConflictError("只有调查中的候选可以排除或撤回")
            if self._open_claims(repo, candidate_id):
                raise ConflictError("候选存在进行中的认领，请先办结再变更候选")
            connection.execute(
                "UPDATE identity_candidates SET status=?,status_reason=?,updated_at=? WHERE id=?",
                (status, reason, now, candidate_id),
            )
            self._record_decision(
                connection, repo, candidate=candidate, decision_type=decision_type, reason=reason,
                principal=principal, now=now, after_status=status, with_clues=[candidate_id],
            )
            repo.event("case", candidate["case_id"], event_type, principal.display_name,
                       {"candidate_id": candidate_id, "reason": reason}, now)
            return self.get_case_identification(candidate["case_id"], principal, repo=repo)

    # ------------------------------------------------------------------ 认领

    def create_claim(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.claim")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            candidate = self._require_candidate(repo, payload["candidate_id"])
            if candidate["case_id"] != payload["case_id"]:
                raise ValidationError("候选与业务档案不匹配")
            # 重复报送：同一幂等键直接回放既有结论，绝不生成第二份
            existing = repo.claim_by_key(payload["case_id"], payload["idempotency_key"])
            if existing is not None:
                if (existing["candidate_id"] != payload["candidate_id"]
                        or existing["claimant_name"] != payload["claimant_name"].strip()
                        or existing["claimant_identity"] != payload["claimant_identity"].strip()):
                    raise ConflictError("同一幂等键对应了不同认领内容")
                return self.get_claim(existing["id"], principal, repo=repo)
            if repo.resolution(payload["case_id"]) is not None:
                raise ConflictError("该档案身份已确认，不能再提交认领")
            if candidate["status"] != "active":
                raise ConflictError("候选已合并、排除或撤回，不能对其提交认领")
            # 同一家属针对同一候选的重复报送，即使换了幂等键也回放在途原记录；
            # 已办结或已撤回的认领不阻挡凭新幂等键重新提交
            natural = repo.claim_natural(
                payload["case_id"], payload["candidate_id"],
                payload["claimant_name"].strip(), payload["claimant_identity"].strip(),
            )
            if natural is not None and natural["status"] in CLAIM_STATUS_OPEN:
                return self.get_claim(natural["id"], principal, repo=repo)
            cursor = connection.execute(
                "INSERT INTO identity_claims(case_id,candidate_id,claimant_name,claimant_identity,relationship,"
                "contact_phone,idempotency_key,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?, 'submitted',?,?,?)",
                (
                    payload["case_id"], payload["candidate_id"], payload["claimant_name"].strip(),
                    payload["claimant_identity"].strip(), payload["relationship"], payload["contact_phone"],
                    payload["idempotency_key"], principal.display_name, now, now,
                ),
            )
            claim_id = int(cursor.lastrowid)
            for document in payload["documents"]:
                connection.execute(
                    "INSERT INTO identity_claim_documents(claim_id,doc_type,doc_reference,notes,created_at) VALUES(?,?,?,?,?)",
                    (claim_id, document["doc_type"], document["doc_reference"], document.get("notes", ""), now),
                )
            repo.event("case", payload["case_id"], "claim.submitted", principal.display_name,
                       {"claim_id": claim_id, "candidate_id": payload["candidate_id"], "relationship": payload["relationship"]}, now)
            return self.get_claim(claim_id, principal, repo=repo)

    def check_materials(self, principal: Principal, claim_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.material_check")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            claim = self._require_claim(repo, claim_id)
            self._require_open_case(repo, claim["case_id"])
            if claim["status"] != "submitted":
                raise ConflictError("只有待校验的认领可以登记材料校验结果")
            new_status = "material_verified" if payload["passed"] else "material_rejected"
            result = {"passed": bool(payload["passed"]), "opinion": payload.get("opinion", "")}
            connection.execute(
                "UPDATE identity_claims SET status=?,check_result_json=?,checker_id=?,checker_name=?,checked_at=?,updated_at=? WHERE id=?",
                (new_status, json.dumps(result, ensure_ascii=False, sort_keys=True),
                 principal.user_id, principal.display_name, now, now, claim_id),
            )
            self._record_decision(
                connection, repo, claim=claim, decision_type="material_check",
                reason=payload.get("opinion", ""), principal=principal, now=now,
                before_status="submitted", after_status=new_status,
                extra={"passed": bool(payload["passed"])}, with_documents=True,
            )
            repo.event("case", claim["case_id"], "claim.material_checked", principal.display_name,
                       {"claim_id": claim_id, "passed": bool(payload["passed"])}, now)
            return self.get_claim(claim_id, principal, repo=repo)

    def review_claim(self, principal: Principal, claim_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("identity.review")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            claim = self._require_claim(repo, claim_id)
            self._require_open_case(repo, claim["case_id"])
            candidate = self._require_candidate(repo, claim["candidate_id"])
            if claim["status"] != "material_verified":
                raise ConflictError("只有材料校验通过的认领可以提请复核")
            # 职责分离：复核人不能是本人提交的材料校验
            if claim["checker_id"] is not None and claim["checker_id"] == principal.user_id:
                raise ConflictError("材料校验与复核必须由不同工作人员完成")
            if not payload["approved"]:
                connection.execute(
                    "UPDATE identity_claims SET status='review_rejected',review_opinion=?,reviewer_id=?,reviewer_name=?,reviewed_at=?,updated_at=? WHERE id=?",
                    (payload.get("opinion", ""), principal.user_id, principal.display_name, now, now, claim_id),
                )
                self._record_decision(
                    connection, repo, claim=claim, candidate=candidate, decision_type="review_reject",
                    reason=payload.get("opinion", ""), principal=principal, now=now,
                    before_status="material_verified", after_status="review_rejected",
                    with_clues=[candidate["id"]], with_documents=True,
                )
                repo.event("case", claim["case_id"], "claim.review_rejected", principal.display_name,
                           {"claim_id": claim_id}, now)
                return self.get_claim(claim_id, principal, repo=repo)

            # 批准：冲突候选不能同时确认为同一人（主键 + 状态双保险）
            if repo.resolution(claim["case_id"]) is not None:
                raise ConflictError("该档案已存在确认结论，冲突候选不能同时确认")
            other_confirmed = connection.execute(
                "SELECT id FROM identity_candidates WHERE case_id=? AND status='confirmed' AND id<>?",
                (claim["case_id"], candidate["id"]),
            ).fetchone()
            if other_confirmed is not None:
                raise ConflictError("已有其他候选被确认为同一人")
            connection.execute(
                "UPDATE identity_claims SET status='review_approved',review_opinion=?,reviewer_id=?,reviewer_name=?,reviewed_at=?,updated_at=? WHERE id=?",
                (payload.get("opinion", ""), principal.user_id, principal.display_name, now, now, claim_id),
            )
            connection.execute(
                "UPDATE identity_candidates SET status='confirmed',updated_at=? WHERE id=?",
                (now, candidate["id"]),
            )
            # 确认之时冻结全部依据：候选线索与认领材料
            self._record_decision(
                connection, repo, claim=claim, candidate=candidate, decision_type="review_confirm",
                reason=payload.get("opinion", ""), principal=principal, now=now,
                before_status="material_verified", after_status="review_approved",
                with_clues=self._merge_family(repo, candidate["id"]), with_documents=True,
            )
            # 同一档案的其他在调候选随确认一并排除，排除依据同步快照
            for rival in repo.candidates(claim["case_id"]):
                if rival["id"] == candidate["id"] or rival["status"] != "active":
                    continue
                rival_reason = f"候选 {candidate['id']} 已确认为同一人，冲突候选排除"
                connection.execute(
                    "UPDATE identity_candidates SET status='excluded',status_reason=?,updated_at=? WHERE id=?",
                    (rival_reason, now, rival["id"]),
                )
                self._record_decision(
                    connection, repo, candidate=rival, decision_type="exclude", reason=rival_reason,
                    principal=principal, now=now, after_status="excluded",
                    extra={"confirmed_candidate_id": candidate["id"]}, with_clues=[rival["id"]],
                )
            # 其他在途认领（含同一候选的其他家属、冲突候选上的认领）随确认终结，材料留痕
            for other in repo.claims(claim["case_id"]):
                if other["id"] == claim_id or other["status"] not in CLAIM_STATUS_OPEN:
                    continue
                other_reason = f"档案已由认领 {claim_id} 确认身份，本认领被确认结论覆盖"
                connection.execute(
                    "UPDATE identity_claims SET status='superseded',review_opinion=?,reviewer_id=?,"
                    "reviewer_name=?,reviewed_at=?,updated_at=? WHERE id=?",
                    (other_reason, principal.user_id, principal.display_name, now, now, other["id"]),
                )
                self._record_decision(
                    connection, repo, claim=other, candidate=self._require_candidate(repo, other["candidate_id"]),
                    decision_type="supersede", reason=other_reason, principal=principal, now=now,
                    before_status=other["status"], after_status="superseded",
                    with_clues=[other["candidate_id"]], with_documents=True,
                )
                repo.event("case", claim["case_id"], "claim.terminated_by_confirmation", principal.display_name,
                           {"claim_id": other["id"], "resolved_claim_id": claim_id}, now)
            conclusion_key = f"case-{claim['case_id']}-candidate-{candidate['id']}-claim-{claim_id}"
            connection.execute(
                "INSERT INTO identity_resolutions(case_id,candidate_id,claim_id,confirmer_id,confirmer_name,conclusion_key,confirmed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (claim["case_id"], candidate["id"], claim_id, principal.user_id, principal.display_name, conclusion_key, now),
            )
            repo.event("case", claim["case_id"], "identity.confirmed", principal.display_name,
                       {"candidate_id": candidate["id"], "claim_id": claim_id}, now)
            return self.get_case_identification(claim["case_id"], principal, repo=repo)

    def withdraw_claim(self, principal: Principal, claim_id: int, reason: str) -> dict[str, Any]:
        principal.require("identity.claim")
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = IdentificationRepository(connection)
            claim = self._require_claim(repo, claim_id)
            if claim["status"] not in CLAIM_STATUS_OPEN:
                raise ConflictError("当前认领状态不能撤回")
            connection.execute(
                "UPDATE identity_claims SET status='withdrawn',updated_at=? WHERE id=?",
                (now, claim_id),
            )
            self._record_decision(
                connection, repo, claim=claim, decision_type="claim_withdraw", reason=reason,
                principal=principal, now=now, before_status=claim["status"], after_status="withdrawn",
            )
            repo.event("case", claim["case_id"], "claim.withdrawn", principal.display_name,
                       {"claim_id": claim_id, "reason": reason}, now)
            return self.get_claim(claim_id, principal, repo=repo)

    # ------------------------------------------------------------------ 查询

    def get_case_identification(self, case_id: int, principal: Principal, *, repo: IdentificationRepository | None = None) -> dict[str, Any]:
        principal.require("identity.read")
        repo = repo or self.repository
        if not repo.case_exists(case_id):
            raise NotFoundError("逝者业务档案不存在")
        candidates = repo.candidates(case_id)
        merge_sources = self._merge_source_map(candidates)
        candidate_view = []
        for candidate in candidates:
            view = self._candidate_view(repo, candidate, principal)
            source_ids = merge_sources.get(candidate["id"], [])
            view["merged_from_candidate_ids"] = source_ids
            view["merged_clues"] = [
                self._mask_clue(principal, dict(clue, candidate_id=source_id))
                for source_id in source_ids
                for clue in repo.clues(source_id)
            ]
            candidate_view.append(view)
        claims = [self._claim_view(repo, claim, principal) for claim in repo.claims(case_id)]
        decisions = []
        for decision in repo.decisions(case_id=case_id):
            decisions.append(self._decision_view(repo, decision, principal))
        resolution = repo.resolution(case_id)
        result = {
            "case_id": case_id,
            "resolved": resolution is not None,
            "resolution": self._mask_resolution(resolution) if resolution else None,
            "candidates": candidate_view,
            "excluded_candidates": [
                {"candidate_id": item["id"], "suggested_name": item["suggested_name"], "status_reason": item["status_reason"]}
                for item in candidate_view if item["status"] == "excluded"
            ],
            "claims": claims,
            "decisions": decisions,
            "timeline": repo.timeline("case", case_id),
        }
        return result

    def get_candidate(self, candidate_id: int, principal: Principal, *, repo: IdentificationRepository | None = None) -> dict[str, Any]:
        principal.require("identity.read")
        repo = repo or self.repository
        candidate = self._require_candidate(repo, candidate_id)
        return self._candidate_view(repo, candidate, principal)

    def get_claim(self, claim_id: int, principal: Principal, *, repo: IdentificationRepository | None = None) -> dict[str, Any]:
        principal.require("identity.read")
        repo = repo or self.repository
        return self._claim_view(repo, self._require_claim(repo, claim_id), principal)

    # ------------------------------------------------------------- 内部辅助

    def _insert_clue(self, connection: sqlite3.Connection, candidate_id: int, clue: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO identity_clues(candidate_id,source_type,clue_kind,raw_text,confidence,reporter,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (candidate_id, clue["source_type"], clue["clue_kind"], clue["raw_text"],
             clue["confidence"], clue["reporter"], now),
        )
        return dict(connection.execute("SELECT * FROM identity_clues WHERE id=?", (int(cursor.lastrowid),)).fetchone())

    def _record_decision(
        self,
        connection: sqlite3.Connection,
        repo: IdentificationRepository,
        *,
        decision_type: str,
        reason: str,
        principal: Principal,
        now: str,
        candidate: dict[str, Any] | None = None,
        claim: dict[str, Any] | None = None,
        before_status: str = "",
        after_status: str = "",
        extra: dict[str, Any] | None = None,
        with_clues: list[int] | None = None,
        with_documents: bool = False,
    ) -> int:
        case_id = (claim or candidate)["case_id"]
        cursor = connection.execute(
            "INSERT INTO identity_decisions(candidate_id,claim_id,case_id,decision_type,reason,actor_id,actor_name,"
            "before_status,after_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                candidate["id"] if candidate else (claim["candidate_id"] if claim else None),
                claim["id"] if claim else None,
                case_id, decision_type, reason, principal.user_id, principal.display_name,
                before_status, after_status, now,
            ),
        )
        decision_id = int(cursor.lastrowid)
        for candidate_id in with_clues or []:
            for clue in repo.clues(candidate_id):
                self._snapshot_evidence(connection, decision_id, "clue", clue["id"], clue, now)
        if with_documents and claim is not None:
            for document in repo.documents(claim["id"]):
                self._snapshot_evidence(connection, decision_id, "document", document["id"], document, now)
        if extra:
            # extra 仅用于事件追溯，不改变决定结构
            repo.event("decision", decision_id, f"decision.{decision_type}.detail", principal.display_name, extra, now)
        repo.event(
            "case", case_id, f"decision.{decision_type}", principal.display_name,
            {"decision_id": decision_id, "candidate_id": candidate["id"] if candidate else None, "claim_id": claim["id"] if claim else None},
            now,
        )
        return decision_id

    def _snapshot_evidence(self, connection: sqlite3.Connection, decision_id: int, kind: str, ref_id: int, row: dict[str, Any], now: str) -> None:
        snapshot = {key: value for key, value in row.items()}
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        connection.execute(
            "INSERT INTO identity_decision_evidence(decision_id,evidence_kind,ref_id,snapshot_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (decision_id, kind, ref_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), content_hash, now),
        )

    def _candidate_view(self, repo: IdentificationRepository, candidate: dict[str, Any], principal: Principal | None = None) -> dict[str, Any]:
        view = dict(candidate)
        view["clues"] = [self._mask_clue(principal, clue) for clue in repo.clues(candidate["id"])]
        if principal is None or not principal.can("identity.sensitive"):
            view["identity_number"] = mask_id_card(view["identity_number"]) or ""
        decisions = repo.decisions(candidate_id=candidate["id"])
        view["decision_ids"] = [item["id"] for item in decisions]
        return view

    def _claim_view(self, repo: IdentificationRepository, claim: dict[str, Any], principal: Principal) -> dict[str, Any]:
        view = dict(claim)
        documents = repo.documents(claim["id"])
        if not principal.can("identity.sensitive"):
            view["claimant_identity"] = mask_id_card(view["claimant_identity"]) or view["claimant_identity"]
            view["contact_phone"] = mask_phone(view["contact_phone"]) or view["contact_phone"]
            documents = [
                {**doc, "doc_reference": sanitize_text(doc["doc_reference"]), "notes": sanitize_text(doc["notes"])}
                for doc in documents
            ]
        view["documents"] = documents
        view["check_result"] = json.loads(view.pop("check_result_json")) if view.get("check_result_json") else None
        view["decision_ids"] = [item["id"] for item in repo.decisions(claim_id=claim["id"])]
        return view

    def _decision_view(self, repo: IdentificationRepository, decision: dict[str, Any], principal: Principal) -> dict[str, Any]:
        view = dict(decision)
        evidence = repo.evidence(decision["id"])
        if not principal.can("identity.sensitive"):
            for item in evidence:
                item["snapshot"] = sanitize_payload(item["snapshot"])
        view["evidence"] = evidence
        return view

    def _mask_clue(self, principal: Principal | None, clue: dict[str, Any]) -> dict[str, Any]:
        view = dict(clue)
        if principal is None or not principal.can("identity.sensitive"):
            view["raw_text"] = sanitize_text(view["raw_text"])
        return view

    def _mask_resolution(self, resolution: dict[str, Any]) -> dict[str, Any]:
        return dict(resolution)

    def _merge_family(self, repo: IdentificationRepository, confirmed_id: int) -> list[int]:
        """确认时收集候选自身及全部已并入该候选的来源候选，冻结完整证据链。"""
        case_id = repo.candidate(confirmed_id)["case_id"]
        ids = [confirmed_id]
        for candidate in repo.candidates(case_id):
            current = candidate.get("merged_into_id")
            seen: set[int] = set()
            while current is not None and current not in seen:
                seen.add(current)
                if current == confirmed_id:
                    ids.append(candidate["id"])
                    break
                parent = repo.candidate(current)
                current = parent.get("merged_into_id") if parent else None
        return list(dict.fromkeys(ids))

    def _merge_source_map(self, candidates: list[dict[str, Any]]) -> dict[int, list[int]]:
        by_id = {item["id"]: item for item in candidates}
        result: dict[int, list[int]] = {}
        for item in candidates:
            if item["status"] == "merged" and item["merged_into_id"] is not None:
                result.setdefault(self._merge_root(by_id, item), []).append(item["id"])
        return result

    def _merge_root(self, by_id: dict[int, dict[str, Any]], item: dict[str, Any]) -> int:
        current_id = item["merged_into_id"]
        seen: set[int] = set()
        while current_id not in seen:
            seen.add(current_id)
            parent = by_id.get(current_id)
            if parent is None or parent["status"] != "merged" or parent["merged_into_id"] is None:
                return current_id
            current_id = parent["merged_into_id"]
        return item["merged_into_id"]

    def _open_claims(self, repo: IdentificationRepository, candidate_id: int) -> list[dict[str, Any]]:
        return [claim for claim in repo.claims(repo.candidate(candidate_id)["case_id"])
                if claim["candidate_id"] == candidate_id and claim["status"] in CLAIM_STATUS_OPEN]

    def _require_candidate(self, repo: IdentificationRepository, candidate_id: int) -> dict[str, Any]:
        candidate = repo.candidate(candidate_id)
        if candidate is None:
            raise NotFoundError("身份候选不存在")
        return candidate

    def _require_claim(self, repo: IdentificationRepository, claim_id: int) -> dict[str, Any]:
        claim = repo.claim(claim_id)
        if claim is None:
            raise NotFoundError("认领记录不存在")
        return claim

    def _same_case(self, source: dict[str, Any], target: dict[str, Any]) -> None:
        if source["case_id"] != target["case_id"]:
            raise ValidationError("只能合并同一业务档案下的候选")

    def _require_open_case(self, repo: IdentificationRepository, case_id: int) -> None:
        if repo.resolution(case_id) is not None:
            raise ConflictError("该档案身份已确认，候选结论不可再变更")
