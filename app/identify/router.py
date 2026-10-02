from fastapi import APIRouter, Query

from app.identify.schemas import CandidateCreate, CandidateMerge, CandidateResolve, ClaimCreate, ClaimReview, ClueCreate, ClueWithdraw, MaterialsVerify
from app.identify.service import IdentifyService

router = APIRouter(prefix="/api/identify", tags=["identify"])


@router.post("/cases/{case_id}/clues", status_code=201)
def report_clue(case_id: int, payload: ClueCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().report_clue(case_id, payload.model_dump(), actor)


@router.post("/clues/{clue_id}/withdraw")
def withdraw_clue(clue_id: int, payload: ClueWithdraw, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().withdraw_clue(clue_id, payload.reason, actor)


@router.get("/cases/{case_id}/clues")
def list_clues(case_id: int, role: str = Query(default="public", min_length=2, max_length=40)) -> list[dict]:
    return IdentifyService().list_clues(case_id, role)


@router.post("/cases/{case_id}/candidates", status_code=201)
def create_candidate(case_id: int, payload: CandidateCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().create_candidate(case_id, payload.model_dump(), actor)


@router.post("/candidates/{candidate_id}/merge")
def merge_candidate(candidate_id: int, payload: CandidateMerge, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().merge_candidate(candidate_id, payload.model_dump(), actor)


@router.post("/candidates/{candidate_id}/exclude")
def exclude_candidate(candidate_id: int, payload: CandidateResolve, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().exclude_candidate(candidate_id, payload.reason, actor)


@router.post("/candidates/{candidate_id}/withdraw")
def withdraw_candidate(candidate_id: int, payload: CandidateResolve, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().withdraw_candidate(candidate_id, payload.reason, actor)


@router.get("/cases/{case_id}/candidates")
def list_candidates(case_id: int, role: str = Query(default="public", min_length=2, max_length=40)) -> list[dict]:
    return IdentifyService().list_candidates(case_id, role)


@router.post("/cases/{case_id}/claims", status_code=201)
def submit_claim(case_id: int, payload: ClaimCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return IdentifyService().submit_claim(case_id, payload.model_dump(), actor)


@router.post("/claims/{claim_id}/verify-materials")
def verify_materials(claim_id: int, payload: MaterialsVerify, actor: str = Query(min_length=2, max_length=80), role: str = Query(min_length=2, max_length=40)) -> dict:
    return IdentifyService().verify_materials(claim_id, payload.model_dump(), actor, role)


@router.post("/claims/{claim_id}/review")
def review_claim(claim_id: int, payload: ClaimReview, actor: str = Query(min_length=2, max_length=80), role: str = Query(min_length=2, max_length=40)) -> dict:
    return IdentifyService().review_claim(claim_id, payload.model_dump(), actor, role)


@router.get("/cases/{case_id}/claims")
def list_claims(case_id: int, role: str = Query(default="public", min_length=2, max_length=40)) -> list[dict]:
    return IdentifyService().list_claims(case_id, role)


@router.get("/cases/{case_id}/conclusion")
def get_conclusion(case_id: int, role: str = Query(default="public", min_length=2, max_length=40)) -> dict:
    return IdentifyService().get_conclusion(case_id, role)


@router.get("/cases/{case_id}/timeline")
def get_timeline(case_id: int) -> list[dict]:
    return IdentifyService().timeline(case_id)
