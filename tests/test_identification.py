from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from app.database import close_connection
from app.main import app


def create_case(client, ref: str = "ID-CASE-001") -> dict:
    response = client.post(
        "/api/mortuary/cases?actor=intake-clerk",
        json={
            "external_ref": ref,
            "decedent_name": "无名氏",
            "identity_number": None,
            "death_time": "2026-09-30T08:30:00Z",
            "received_from": "市第一医院",
            "family_contact": "值班社工",
            "family_phone": "13800000000",
            "special_notes": "身份待核实",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_user(client, admin, username: str, role: str) -> dict:
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Worker!23456", "display_name": username, "role_codes": [role]},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Worker!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def staff(client, admin) -> dict:
    return {
        "clerk": make_user(client, admin, "id.clerk", "identity_clerk"),
        "checker": make_user(client, admin, "id.checker", "identity_checker"),
        "reviewer": make_user(client, admin, "id.reviewer", "identity_reviewer"),
        "dual": make_dual_user(client, admin, "id.dual", ["identity_checker", "identity_reviewer"]),
    }


def make_dual_user(client, admin, username: str, roles: list[str]) -> dict:
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Worker!23456", "display_name": username, "role_codes": roles},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Worker!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}}


CANDIDATE_A = {
    "suggested_name": "张德安",
    "identity_number": "ID-440100-19580301-1010",
    "source_type": "hospital",
    "source_ref": "市一院腕带-77",
    "confidence": "medium",
    "clues": [
        {"source_type": "hospital", "clue_kind": "name_fragment", "raw_text": "腕带残字：德安", "confidence": "medium", "reporter": "护士站周琳"},
        {"source_type": "staff", "clue_kind": "belongings", "raw_text": "随身钥匙一串、旧手表，内刻电话13900001111", "confidence": "low", "reporter": "接收员李强"},
    ],
}

CANDIDATE_B = {
    "suggested_name": "王建国",
    "identity_number": "",
    "source_type": "police",
    "source_ref": "派出所协查-12",
    "confidence": "low",
    "clues": [
        {"source_type": "police", "clue_kind": "physical", "raw_text": "体貌与失踪人员王某近似", "confidence": "low", "reporter": "民警赵峰"},
    ],
}


def add_candidate(client, headers, case_id: int, body: dict) -> dict:
    response = client.post("/api/identification/candidates", headers=headers, json={**body, "case_id": case_id})
    assert response.status_code == 201, response.text
    return response.json()


def submit_claim(client, headers, case_id: int, candidate_id: int, key: str = "claim-0001", *, phone: str = "13912345678") -> Response:
    response = client.post(
        "/api/identification/claims",
        headers=headers,
        json={
            "case_id": case_id,
            "candidate_id": candidate_id,
            "claimant_name": "张小明",
            "claimant_identity": "ID-330100-19900101-2233",
            "relationship": "儿子",
            "contact_phone": phone,
            "idempotency_key": key,
            "documents": [
                {"doc_type": "户口簿", "doc_reference": "HK-2026-0099", "notes": "记载父子关系"},
                {"doc_type": "死亡医学证明旁证", "doc_reference": "ZM-医院-77", "notes": ""},
            ],
        },
    )
    return response


def test_candidate_clues_keep_raw_text_and_confidence(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    assert candidate["status"] == "active"
    assert [clue["confidence"] for clue in candidate["clues"]] == ["medium", "low"]
    assert candidate["clues"][0]["raw_text"] == "腕带残字：德安"

    extra = client.post(
        f"/api/identification/candidates/{candidate['id']}/clues",
        headers=staff["clerk"]["headers"],
        json={"source_type": "family", "clue_kind": "kinship", "raw_text": "家属称左肩有旧疤", "confidence": "high", "reporter": "张小明"},
    )
    assert extra.status_code == 201
    assert len(extra.json()["clues"]) == 3


def test_clerk_sees_masked_documents_and_contacts_but_reviewer_sees_plain(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate["id"]).json()

    clerk_view = client.get(f"/api/identification/cases/{case['id']}", headers=staff["clerk"]["headers"]).json()
    clerk_candidate = clerk_view["candidates"][0]
    clerk_claim = clerk_view["claims"][0]
    assert clerk_candidate["identity_number"] != "ID-440100-19580301-1010"
    assert "19580301" not in clerk_candidate["identity_number"]
    assert "139****5678" == clerk_claim["contact_phone"]
    assert "2233" in clerk_claim["claimant_identity"] and "19900101" not in clerk_claim["claimant_identity"]
    assert "139****1111" in clerk_candidate["clues"][1]["raw_text"]

    reviewer_view = client.get(f"/api/identification/cases/{case['id']}", headers=staff["reviewer"]["headers"]).json()
    assert reviewer_view["candidates"][0]["identity_number"] == "ID-440100-19580301-1010"
    assert reviewer_view["claims"][0]["contact_phone"] == "13912345678"
    assert reviewer_view["claims"][0]["claimant_identity"] == "ID-330100-19900101-2233"
    assert "13900001111" in reviewer_view["candidates"][0]["clues"][1]["raw_text"]
    assert claim["id"] == reviewer_view["claims"][0]["id"]


def test_merge_candidates_keeps_both_records_and_carries_clues(client, admin, staff):
    case = create_case(client)
    first = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    second = add_candidate(client, staff["clerk"]["headers"], case["id"], {**CANDIDATE_B, "suggested_name": "张德安（曾用名）", "source_type": "family"})
    merged = client.post(
        "/api/identification/candidates/merge",
        headers=staff["clerk"]["headers"],
        json={"source_candidate_id": second["id"], "target_candidate_id": first["id"], "reason": "家属确认系同一人曾用名"},
    )
    assert merged.status_code == 200, merged.text
    view = merged.json()
    target = next(item for item in view["candidates"] if item["id"] == first["id"])
    source = next(item for item in view["candidates"] if item["id"] == second["id"])
    assert source["status"] == "merged" and source["merged_into_id"] == first["id"]
    assert target["merged_from_candidate_ids"] == [second["id"]]
    assert len(target["merged_clues"]) == 1
    merge_decisions = [d for d in view["decisions"] if d["decision_type"] == "merge"]
    assert merge_decisions and merge_decisions[0]["evidence"][0]["evidence_kind"] == "clue"
    assert merge_decisions[0]["evidence"][0]["content_hash"]


def test_excluded_and_withdrawn_candidates_cannot_be_edited(client, admin, staff):
    case = create_case(client)
    first = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    second = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_B)
    excluded = client.post(
        f"/api/identification/candidates/{second['id']}/exclude",
        headers=staff["clerk"]["headers"],
        json={"reason": "DNA 比对不符"},
    )
    assert excluded.status_code == 200
    blocked = client.post(
        f"/api/identification/candidates/{second['id']}/clues",
        headers=staff["clerk"]["headers"],
        json={"source_type": "police", "clue_kind": "other", "raw_text": "补充", "confidence": "low", "reporter": "民警赵峰"},
    )
    assert blocked.status_code == 409
    withdrawn = client.post(
        f"/api/identification/candidates/{first['id']}/withdraw",
        headers=staff["clerk"]["headers"],
        json={"reason": "来源撤回线索"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["candidates"][0]["status"] == "withdrawn"


def test_open_claim_blocks_candidate_merge_and_exclusion(client, admin, staff):
    case = create_case(client)
    first = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    second = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_B)
    submit_claim(client, staff["clerk"]["headers"], case["id"], first["id"])
    response = client.post(
        "/api/identification/candidates/merge",
        headers=staff["clerk"]["headers"],
        json={"source_candidate_id": first["id"], "target_candidate_id": second["id"], "reason": "尝试合并"},
    )
    assert response.status_code == 409


def test_duplicate_claim_submission_replays_same_record(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    payload = {
        "case_id": case["id"],
        "candidate_id": candidate["id"],
        "claimant_name": "张小明",
        "claimant_identity": "ID-330100-19900101-2233",
        "relationship": "儿子",
        "contact_phone": "13912345678",
        "idempotency_key": "claim-dup-01",
        "documents": [{"doc_type": "户口簿", "doc_reference": "HK-2026-0099", "notes": ""}],
    }
    first = client.post("/api/identification/claims", headers=staff["clerk"]["headers"], json=payload)
    assert first.status_code == 201
    repeated = client.post("/api/identification/claims", headers=staff["clerk"]["headers"], json=payload)
    assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]

    different = {**payload, "idempotency_key": "claim-dup-01", "claimant_name": "李大勇"}
    conflict = client.post("/api/identification/claims", headers=staff["clerk"]["headers"], json=different)
    assert conflict.status_code == 409

    # 换幂等键但同一家属同一候选，在途期间仍回放原记录
    other_key = {**payload, "idempotency_key": "claim-dup-02"}
    replayed = client.post("/api/identification/claims", headers=staff["clerk"]["headers"], json=other_key)
    assert replayed.json()["id"] == first.json()["id"]


def test_material_check_and_review_require_different_people(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate["id"]).json()

    checked = client.post(
        f"/api/identification/claims/{claim['id']}/material-check",
        headers=staff["dual"]["headers"],
        json={"passed": True, "opinion": "户口簿与旁证一致"},
    )
    assert checked.status_code == 200 and checked.json()["status"] == "material_verified"

    # 同一人即便同时拥有两类权限，也不能既校验又复核
    own_review = client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["dual"]["headers"],
        json={"approved": True, "opinion": "自己复核"},
    )
    assert own_review.status_code == 409

    # 没有复核权限的校验员/协办员不能确认
    forbidden = client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["checker"]["headers"],
        json={"approved": True, "opinion": "越权"},
    )
    assert forbidden.status_code == 403
    clerk_forbidden = client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["clerk"]["headers"],
        json={"approved": True, "opinion": "越权"},
    )
    assert clerk_forbidden.status_code == 403


def test_full_confirmation_excludes_conflicts_and_freezes_evidence(client, admin, staff):
    case = create_case(client)
    candidate_a = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    candidate_b = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_B)
    claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate_a["id"]).json()
    client.post(
        f"/api/identification/claims/{claim['id']}/material-check",
        headers=staff["checker"]["headers"],
        json={"passed": True, "opinion": "材料齐全"},
    )
    approved = client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["reviewer"]["headers"],
        json={"approved": True, "opinion": "确认同一人"},
    )
    assert approved.status_code == 200, approved.text
    view = approved.json()
    assert view["resolved"] is True
    assert view["resolution"]["candidate_id"] == candidate_a["id"]
    assert view["resolution"]["claim_id"] == claim["id"]

    a_view = next(item for item in view["candidates"] if item["id"] == candidate_a["id"])
    b_view = next(item for item in view["candidates"] if item["id"] == candidate_b["id"])
    assert a_view["status"] == "confirmed"
    assert b_view["status"] == "excluded"
    # 确认后仍能说明排除了哪些候选及原因
    excluded_summary = view["excluded_candidates"]
    assert [item["candidate_id"] for item in excluded_summary] == [candidate_b["id"]]
    assert "已确认为同一人" in excluded_summary[0]["status_reason"]

    # 冲突候选不能同时确认：B 已被排除，无法再被认领
    rival_claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate_b["id"], "claim-rival-1")
    assert rival_claim.status_code == 409

    # 确认决定冻结了线索与材料，且内容哈希齐全
    confirm = next(d for d in view["decisions"] if d["decision_type"] == "review_confirm")
    kinds = {row["evidence_kind"] for row in confirm["evidence"]}
    assert kinds == {"clue", "document"}
    assert all(row["content_hash"] for row in confirm["evidence"])
    clue_texts = [row["snapshot"]["raw_text"] for row in confirm["evidence"] if row["evidence_kind"] == "clue"]
    doc_refs = [row["snapshot"]["doc_reference"] for row in confirm["evidence"] if row["evidence_kind"] == "document"]
    assert any("腕带残字" in value for value in clue_texts)
    assert "HK-2026-0099" in doc_refs

    # 确认后档案锁定：不能再加候选
    locked = client.post(
        "/api/identification/candidates",
        headers=staff["clerk"]["headers"],
        json={**CANDIDATE_B, "case_id": case["id"]},
    )
    assert locked.status_code == 409


def test_competing_claims_are_superseded_without_losing_documents(client, admin, staff):
    case = create_case(client)
    candidate_a = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    candidate_b = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_B)
    winning = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate_a["id"], "claim-win-01").json()
    # 另一家属就冲突候选提交在途认领
    losing = client.post(
        "/api/identification/claims",
        headers=staff["clerk"]["headers"],
        json={
            "case_id": case["id"],
            "candidate_id": candidate_b["id"],
            "claimant_name": "王二梅",
            "claimant_identity": "ID-440100-19850707-4455",
            "relationship": "妹妹",
            "contact_phone": "13700007788",
            "idempotency_key": "claim-lose-01",
            "documents": [{"doc_type": "身份证", "doc_reference": "SF-88-4455", "notes": "自述"}],
        },
    )
    assert losing.status_code == 201, losing.text
    losing_id = losing.json()["id"]
    client.post(
        f"/api/identification/claims/{winning['id']}/material-check",
        headers=staff["checker"]["headers"],
        json={"passed": True, "opinion": "通过"},
    )
    confirmed = client.post(
        f"/api/identification/claims/{winning['id']}/review",
        headers=staff["reviewer"]["headers"],
        json={"approved": True, "opinion": "确认"},
    )
    assert confirmed.status_code == 200
    losing_view = next(item for item in confirmed.json()["claims"] if item["id"] == losing_id)
    assert losing_view["status"] == "superseded"
    # 被覆盖认领的材料仍然可查，决定证据未消失
    supersede = next(d for d in confirmed.json()["decisions"] if d["decision_type"] == "supersede")
    assert supersede["claim_id"] == losing_id
    assert {row["evidence_kind"] for row in supersede["evidence"]} == {"clue", "document"}


def test_rejected_materials_end_claim_and_claim_can_be_withdrawn(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate["id"], "claim-rej-01").json()
    rejected = client.post(
        f"/api/identification/claims/{claim['id']}/material-check",
        headers=staff["checker"]["headers"],
        json={"passed": False, "opinion": "证件无法核验"},
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "material_rejected"
    # 复核员不能批准校验未通过的认领
    approve = client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["reviewer"]["headers"],
        json={"approved": True, "opinion": "强行批准"},
    )
    assert approve.status_code == 409
    # 材料被拒后可以凭新幂等键重新报送
    resubmitted_response = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate["id"], "claim-rej-02")
    assert resubmitted_response.status_code == 201
    resubmitted = resubmitted_response.json()
    assert resubmitted["id"] != claim["id"]

    withdrawn = client.post(
        f"/api/identification/claims/{resubmitted['id']}/withdraw",
        headers=staff["clerk"]["headers"],
        json={"reason": "家属暂不继续"},
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    again = client.post(
        f"/api/identification/claims/{resubmitted['id']}/withdraw",
        headers=staff["clerk"]["headers"],
        json={"reason": "再次撤回"},
    )
    assert again.status_code == 409


def test_restart_and_repeated_report_do_not_create_second_conclusion(client, admin, staff):
    case = create_case(client)
    candidate = add_candidate(client, staff["clerk"]["headers"], case["id"], CANDIDATE_A)
    claim = submit_claim(client, staff["clerk"]["headers"], case["id"], candidate["id"], "claim-restart-1").json()
    client.post(
        f"/api/identification/claims/{claim['id']}/material-check",
        headers=staff["checker"]["headers"],
        json={"passed": True, "opinion": "通过"},
    )
    client.post(
        f"/api/identification/claims/{claim['id']}/review",
        headers=staff["reviewer"]["headers"],
        json={"approved": True, "opinion": "确认"},
    )

    # 模拟服务重启：同一数据库重新走一遍生命周期
    close_connection()
    with TestClient(app) as restarted:
        repeated = submit_claim(restarted, staff["clerk"]["headers"], case["id"], candidate["id"], "claim-restart-1")
        assert repeated.status_code == 201 and repeated.json()["id"] == claim["id"]
        view = restarted.get(f"/api/identification/cases/{case['id']}", headers=staff["reviewer"]["headers"]).json()
        assert view["resolved"] is True
        assert len([c for c in view["candidates"] if c["status"] == "confirmed"]) == 1
        assert len(view["claims"]) == 1
        assert view["resolution"]["conclusion_key"] == f"case-{case['id']}-candidate-{candidate['id']}-claim-{claim['id']}"


def test_anonymous_and_unprivileged_access_denied(client, admin, staff):
    case = create_case(client)
    assert client.get(f"/api/identification/cases/{case['id']}").status_code == 401
    other = make_user(client, admin, "plain.clerk", "clerk")
    denied = client.get(f"/api/identification/cases/{case['id']}", headers=other["headers"])
    assert denied.status_code == 403
