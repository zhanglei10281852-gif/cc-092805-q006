from __future__ import annotations

from fastapi.testclient import TestClient


def create_case(client, ref: str = "UID-2026-001") -> dict:
    response = client.post(
        "/api/mortuary/cases?actor=intake-clerk",
        json={"external_ref": ref, "decedent_name": "无名氏-2026-001", "identity_number": None,
              "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院",
              "family_contact": "待确认", "family_phone": "13800000000", "special_notes": "身份不明逝者"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def report_clue(client, case_id: int, **overrides) -> dict:
    payload = {"source_type": "hospital", "clue_kind": "name_fragment", "original_text": "入院手环写着「王×根」",
               "trust_level": "high", "reporter": "市第二医院", "idempotency_key": "clue-0001"}
    payload.update(overrides)
    response = client.post(f"/api/identify/cases/{case_id}/clues?actor=coordinator-chen", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def prepare_candidate(client, case_id: int, name: str = "王守根", identity: str = "440100193801011234", clue_key: str = "clue-2001") -> dict:
    clue = report_clue(client, case_id, idempotency_key=clue_key)
    response = client.post(
        f"/api/identify/cases/{case_id}/candidates?actor=coordinator-chen",
        json={"candidate_name": name, "identity_number": identity, "contact_name": "王小根",
              "contact_phone": "13911112222", "clue_ids": [clue["id"]]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def claim_payload(candidate_id: int, key: str = "claim-0001", claimant_identity: str = "330102196505052211") -> dict:
    return {"candidate_id": candidate_id, "claimant_name": "王小根", "claimant_identity": claimant_identity,
            "claimant_phone": "13911112222", "relation_to_decedent": "父子",
            "materials": [{"material_type": "户口簿", "reference": "HHK-330102-7788", "note": "户主关系页"},
                          {"material_type": "DNA鉴定报告", "reference": "DNA-2026-0930-01"}],
            "idempotency_key": key}


def submit_claim(client, case_id: int, candidate_id: int, key: str = "claim-0001", claimant_identity: str = "330102196505052211") -> dict:
    response = client.post(f"/api/identify/cases/{case_id}/claims?actor=coordinator-chen", json=claim_payload(candidate_id, key, claimant_identity))
    assert response.status_code == 201, response.text
    return response.json()


def verify_and_confirm(client, claim_id: int, rationale: str = "材料与公安比对一致，确认身份") -> dict:
    verified = client.post(f"/api/identify/claims/{claim_id}/verify-materials?actor=verifier-zhang&role=verifier", json={"passed": True, "note": "材料齐全"})
    assert verified.status_code == 200, verified.text
    reviewed = client.post(f"/api/identify/claims/{claim_id}/review?actor=reviewer-li&role=reviewer", json={"decision": "confirm", "rationale": rationale})
    assert reviewed.status_code == 200, reviewed.text
    return reviewed.json()


def test_clues_keep_original_trust_and_idempotent(client):
    case = create_case(client)
    first = report_clue(client, case["id"])
    report_clue(client, case["id"], source_type="police", clue_kind="belonging", original_text="衣兜内发现写有「李」字的打火机",
                trust_level="medium", reporter="城东派出所", idempotency_key="clue-0002")
    report_clue(client, case["id"], source_type="family", clue_kind="kinship", original_text="报失人自称逝者胞弟，联系电话13812345678",
                trust_level="low", reporter="寻亲家庭", idempotency_key="clue-0003")
    replay = client.post(f"/api/identify/cases/{case['id']}/clues?actor=coordinator-chen",
                         json={"source_type": "hospital", "clue_kind": "name_fragment", "original_text": "入院手环写着「王×根」",
                               "trust_level": "high", "reporter": "市第二医院", "idempotency_key": "clue-0001"})
    assert replay.status_code == 201 and replay.json()["id"] == first["id"]
    conflict = client.post(f"/api/identify/cases/{case['id']}/clues?actor=coordinator-chen",
                           json={"source_type": "police", "clue_kind": "name_fragment", "original_text": "内容不一致的同名线索",
                                 "trust_level": "low", "reporter": "城东派出所", "idempotency_key": "clue-0001"})
    assert conflict.status_code == 409
    clues = client.get(f"/api/identify/cases/{case['id']}/clues?role=reviewer").json()
    assert [clue["source_type"] for clue in clues] == ["hospital", "police", "family"]
    assert [clue["trust_level"] for clue in clues] == ["high", "medium", "low"]
    assert clues[2]["original_text"] == "报失人自称逝者胞弟，联系电话13812345678"
    public_clues = client.get(f"/api/identify/cases/{case['id']}/clues?role=family").json()
    assert "13812345678" not in public_clues[2]["original_text"]
    assert "138****5678" in public_clues[2]["original_text"]


def test_clue_withdraw_before_decision_keeps_record(client):
    case = create_case(client, "UID-2026-010")
    clue = report_clue(client, case["id"], idempotency_key="clue-9101")
    withdrawn = client.post(f"/api/identify/clues/{clue['id']}/withdraw?actor=coordinator-chen", json={"reason": "报送单位更正"})
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    again = client.post(f"/api/identify/clues/{clue['id']}/withdraw?actor=coordinator-chen", json={"reason": "重复撤回"})
    assert again.status_code == 200 and again.json()["status"] == "withdrawn"
    candidate = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen",
                            json={"candidate_name": "王守根", "clue_ids": [clue["id"]]})
    assert candidate.status_code == 409
    stored = client.get(f"/api/identify/cases/{case['id']}/clues?role=reviewer").json()[0]
    assert stored["original_text"] == clue["original_text"]
    assert stored["withdrawn_reason"] == "报送单位更正"


def test_candidate_merge_exclude_withdraw(client):
    case = create_case(client, "UID-2026-002")
    clue_one = report_clue(client, case["id"], idempotency_key="clue-1001")
    clue_two = report_clue(client, case["id"], source_type="police", clue_kind="belonging", original_text="随身携带老式怀表",
                           trust_level="medium", reporter="城东派出所", idempotency_key="clue-1002")
    first = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen",
                        json={"candidate_name": "王守根", "identity_number": "440100193801011234", "clue_ids": [clue_one["id"]]})
    assert first.status_code == 201
    second = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen",
                         json={"candidate_name": "王守根（曾用名王根）", "clue_ids": [clue_two["id"]]})
    assert second.status_code == 201
    merged = client.post(f"/api/identify/candidates/{second.json()['id']}/merge?actor=coordinator-chen",
                         json={"target_candidate_id": first.json()["id"], "reason": "两条候选指向同一走失人员"})
    assert merged.status_code == 200 and merged.json()["status"] == "merged"
    remerged = client.post(f"/api/identify/candidates/{second.json()['id']}/merge?actor=coordinator-chen",
                           json={"target_candidate_id": first.json()["id"], "reason": "重复合并"})
    assert remerged.status_code == 200 and remerged.json()["merged_into_id"] == first.json()["id"]
    candidates = client.get(f"/api/identify/cases/{case['id']}/candidates?role=coordinator").json()
    survivor = next(item for item in candidates if item["id"] == first.json()["id"])
    assert sorted(survivor["clue_ids"]) == sorted([clue_one["id"], clue_two["id"]])
    third = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen", json={"candidate_name": "李有才"}).json()
    excluded = client.post(f"/api/identify/candidates/{third['id']}/exclude?actor=coordinator-chen", json={"reason": "公安排除比对"})
    assert excluded.status_code == 200 and excluded.json()["status"] == "excluded"
    again = client.post(f"/api/identify/candidates/{third['id']}/exclude?actor=coordinator-chen", json={"reason": "重复操作"})
    assert again.status_code == 200 and again.json()["resolution_reason"] == "公安排除比对"
    fourth = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen", json={"candidate_name": "赵二"}).json()
    withdrawn = client.post(f"/api/identify/candidates/{fourth['id']}/withdraw?actor=coordinator-chen", json={"reason": "报送方撤回"})
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    remerge = client.post(f"/api/identify/candidates/{third['id']}/merge?actor=coordinator-chen",
                          json={"target_candidate_id": first.json()["id"], "reason": "已排除候选不能合并"})
    assert remerge.status_code == 409


def test_claim_review_separation_of_duties_and_conclusion(client):
    case = create_case(client, "UID-2026-003")
    candidate = prepare_candidate(client, case["id"])
    other = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen", json={"candidate_name": "李有才"}).json()
    claim = submit_claim(client, case["id"], candidate["id"])
    replay = client.post(f"/api/identify/cases/{case['id']}/claims?actor=coordinator-chen", json=claim_payload(candidate["id"]))
    assert replay.status_code == 201 and replay.json()["id"] == claim["id"]
    changed = dict(claim_payload(candidate["id"]), claimant_phone="13911113333")
    conflict = client.post(f"/api/identify/cases/{case['id']}/claims?actor=coordinator-chen", json=changed)
    assert conflict.status_code == 409
    wrong_role = client.post(f"/api/identify/claims/{claim['id']}/verify-materials?actor=verifier-zhang&role=reviewer", json={"passed": True})
    assert wrong_role.status_code == 403
    verified = client.post(f"/api/identify/claims/{claim['id']}/verify-materials?actor=verifier-zhang&role=verifier", json={"passed": True, "note": "材料齐全"})
    assert verified.status_code == 200 and verified.json()["status"] == "materials_verified"
    assert all(material["checked"] == 1 for material in verified.json()["materials"])
    same_person = client.post(f"/api/identify/claims/{claim['id']}/review?actor=verifier-zhang&role=reviewer",
                              json={"decision": "confirm", "rationale": "比对一致"})
    assert same_person.status_code == 409
    wrong_review_role = client.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=verifier",
                                    json={"decision": "confirm", "rationale": "比对一致"})
    assert wrong_review_role.status_code == 403
    reviewed = client.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=reviewer",
                           json={"decision": "confirm", "rationale": "材料与公安比对一致，确认身份"})
    assert reviewed.status_code == 200
    conclusion = reviewed.json()
    assert conclusion["confirmed_candidate"]["id"] == candidate["id"]
    assert conclusion["confirmed_candidate"]["status"] == "confirmed"
    assert conclusion["claim"]["status"] == "confirmed"
    assert len(conclusion["cited_clues"]) == 1
    excluded_names = {item["candidate_name"]: item["reason"] for item in conclusion["excluded_candidates"]}
    assert "李有才" in excluded_names
    fetched = client.get(f"/api/identify/cases/{case['id']}/conclusion?role=reviewer")
    assert fetched.status_code == 200 and fetched.json()["id"] == conclusion["id"]
    remaining = client.get(f"/api/identify/cases/{case['id']}/candidates?role=coordinator").json()
    assert next(item for item in remaining if item["id"] == other["id"])["status"] == "excluded"
    timeline = client.get(f"/api/identify/cases/{case['id']}/timeline").json()
    event_types = [event["event_type"] for event in timeline]
    assert event_types == ["clue.reported", "candidate.created", "candidate.created", "claim.submitted",
                           "claim.materials_verified", "candidate.excluded", "candidate.confirmed", "claim.confirmed", "decision.recorded"]


def test_review_requires_verified_materials(client):
    case = create_case(client, "UID-2026-009")
    candidate = prepare_candidate(client, case["id"], clue_key="clue-9001")
    claim = submit_claim(client, case["id"], candidate["id"], key="claim-9001")
    early = client.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=reviewer",
                        json={"decision": "confirm", "rationale": "提前复核"})
    assert early.status_code == 409
    failed = client.post(f"/api/identify/claims/{claim['id']}/verify-materials?actor=verifier-zhang&role=verifier",
                         json={"passed": False, "note": "户口簿复印件模糊"})
    assert failed.status_code == 200 and failed.json()["status"] == "rejected"
    confirm = client.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=reviewer",
                          json={"decision": "confirm", "rationale": "尝试确认"})
    assert confirm.status_code == 409
    empty = client.post(f"/api/identify/cases/{case['id']}/claims?actor=coordinator-chen",
                        json={"candidate_id": candidate["id"], "claimant_name": "王小根", "claimant_identity": "330102196505052211",
                              "claimant_phone": "13911112222", "relation_to_decedent": "父子", "materials": [], "idempotency_key": "claim-9002"})
    assert empty.status_code == 422


def test_conflicting_candidates_cannot_both_confirm(client):
    case = create_case(client, "UID-2026-004")
    first = prepare_candidate(client, case["id"], name="王守根", identity="440100193801011234", clue_key="clue-4001")
    second = prepare_candidate(client, case["id"], name="李守根", identity="440100193801019999", clue_key="clue-4002")
    claim_one = submit_claim(client, case["id"], first["id"], key="claim-4001")
    claim_two = submit_claim(client, case["id"], second["id"], key="claim-4002", claimant_identity="330102196505053333")
    for claim_id, verifier in ((claim_one["id"], "verifier-zhang"), (claim_two["id"], "verifier-wang")):
        verified = client.post(f"/api/identify/claims/{claim_id}/verify-materials?actor={verifier}&role=verifier", json={"passed": True})
        assert verified.status_code == 200
    verify_and_confirm(client, claim_one["id"], "确认王守根")
    blocked = client.post(f"/api/identify/claims/{claim_two['id']}/review?actor=reviewer-li&role=reviewer",
                          json={"decision": "confirm", "rationale": "确认李守根"})
    assert blocked.status_code == 409
    claims = client.get(f"/api/identify/cases/{case['id']}/claims?role=reviewer").json()
    assert next(item for item in claims if item["id"] == claim_two["id"])["status"] == "rejected"
    other_case = create_case(client, "UID-2026-005")
    twin = prepare_candidate(client, other_case["id"], name="王守根", identity="440100193801011234", clue_key="clue-4003")
    twin_claim = submit_claim(client, other_case["id"], twin["id"], key="claim-4003")
    verified = client.post(f"/api/identify/claims/{twin_claim['id']}/verify-materials?actor=verifier-zhang&role=verifier", json={"passed": True})
    assert verified.status_code == 200
    clash = client.post(f"/api/identify/claims/{twin_claim['id']}/review?actor=reviewer-li&role=reviewer",
                        json={"decision": "confirm", "rationale": "同一证件号重复确认"})
    assert clash.status_code == 409


def test_evidence_and_confirmed_candidate_locked_after_decision(client):
    case = create_case(client, "UID-2026-006")
    candidate = prepare_candidate(client, case["id"], clue_key="clue-6001")
    rival = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen", json={"candidate_name": "李有才"}).json()
    claim = submit_claim(client, case["id"], candidate["id"], key="claim-6001")
    verify_and_confirm(client, claim["id"])
    clue_id = client.get(f"/api/identify/cases/{case['id']}/clues?role=reviewer").json()[0]["id"]
    blocked = client.post(f"/api/identify/clues/{clue_id}/withdraw?actor=coordinator-chen", json={"reason": "试图撤回"})
    assert blocked.status_code == 409
    stored = client.get(f"/api/identify/cases/{case['id']}/clues?role=reviewer").json()[0]
    assert stored["status"] == "active" and stored["original_text"]
    for action in ("exclude", "withdraw"):
        response = client.post(f"/api/identify/candidates/{candidate['id']}/{action}?actor=coordinator-chen", json={"reason": "试图变更已确认候选"})
        assert response.status_code == 409
    merged = client.post(f"/api/identify/candidates/{candidate['id']}/merge?actor=coordinator-chen",
                         json={"target_candidate_id": rival["id"], "reason": "试图合并已确认候选"})
    assert merged.status_code == 409
    late_candidate = client.post(f"/api/identify/cases/{case['id']}/candidates?actor=coordinator-chen", json={"candidate_name": "周不明"})
    assert late_candidate.status_code == 409
    late_claim = client.post(f"/api/identify/cases/{case['id']}/claims?actor=coordinator-chen", json=claim_payload(candidate["id"], "claim-6002"))
    assert late_claim.status_code == 409
    conclusion = client.get(f"/api/identify/cases/{case['id']}/conclusion?role=reviewer").json()
    assert conclusion["cited_clues"][0]["id"] == clue_id
    assert {item["candidate_name"] for item in conclusion["excluded_candidates"]} == {"李有才"}


def test_role_based_masking(client):
    case = create_case(client, "UID-2026-007")
    candidate = prepare_candidate(client, case["id"], clue_key="clue-7001")
    submit_claim(client, case["id"], candidate["id"], key="claim-7001")
    public_candidates = client.get(f"/api/identify/cases/{case['id']}/candidates?role=family").json()
    assert public_candidates[0]["identity_number"] == "440100********1234"
    assert public_candidates[0]["contact_phone"] == "139****2222"
    staff_candidates = client.get(f"/api/identify/cases/{case['id']}/candidates?role=coordinator").json()
    assert staff_candidates[0]["identity_number"] == "440100193801011234"
    assert staff_candidates[0]["contact_phone"] == "13911112222"
    public_claims = client.get(f"/api/identify/cases/{case['id']}/claims?role=guest").json()
    assert public_claims[0]["claimant_identity"] == "330102********2211"
    assert public_claims[0]["claimant_phone"] == "139****2222"
    assert public_claims[0]["materials"][0]["reference"] == "HHK-33********7788"
    staff_claims = client.get(f"/api/identify/cases/{case['id']}/claims?role=verifier").json()
    assert staff_claims[0]["claimant_identity"] == "330102196505052211"
    assert staff_claims[0]["materials"][0]["reference"] == "HHK-330102-7788"
    default_claims = client.get(f"/api/identify/cases/{case['id']}/claims").json()
    assert default_claims[0]["claimant_phone"] == "139****2222"


def test_restart_and_replay_do_not_duplicate_conclusion(client):
    case = create_case(client, "UID-2026-008")
    candidate = prepare_candidate(client, case["id"], clue_key="clue-8001")
    claim = submit_claim(client, case["id"], candidate["id"], key="claim-8001")
    first = verify_and_confirm(client, claim["id"])
    replay = client.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=reviewer",
                         json={"decision": "confirm", "rationale": "确认身份"})
    assert replay.status_code == 200 and replay.json()["id"] == first["id"]
    from app.main import app
    with TestClient(app) as restarted:
        again = restarted.post(f"/api/identify/claims/{claim['id']}/review?actor=reviewer-li&role=reviewer",
                               json={"decision": "confirm", "rationale": "确认身份"})
        assert again.status_code == 200 and again.json()["id"] == first["id"]
        resubmit = restarted.post(f"/api/identify/cases/{case['id']}/claims?actor=coordinator-chen", json=claim_payload(candidate["id"], "claim-8001"))
        assert resubmit.status_code == 201 and resubmit.json()["id"] == claim["id"]
        conclusion = restarted.get(f"/api/identify/cases/{case['id']}/conclusion?role=reviewer")
        assert conclusion.status_code == 200 and conclusion.json()["id"] == first["id"]
        claims = restarted.get(f"/api/identify/cases/{case['id']}/claims?role=reviewer").json()
        assert len(claims) == 1
