from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.identification.schemas import (
    CandidateCreate,
    CandidateExclude,
    CandidateMerge,
    CandidateWithdraw,
    ClaimCreate,
    ClaimReview,
    ClaimWithdraw,
    ClueCreate,
    MaterialCheck,
)
from app.identification.service import IdentificationService

router = APIRouter(prefix="/api/identification", tags=["身份候选与认领"])


@router.post("/candidates", status_code=201)
def create_candidate(payload: CandidateCreate, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().create_candidate(principal, payload.model_dump())


@router.post("/candidates/{candidate_id}/clues", status_code=201)
def add_clue(candidate_id: int, payload: ClueCreate, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().add_clue(principal, candidate_id, payload.model_dump())


@router.post("/candidates/merge")
def merge_candidates(payload: CandidateMerge, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().merge_candidates(principal, payload.model_dump())


@router.post("/candidates/{candidate_id}/exclude")
def exclude_candidate(candidate_id: int, payload: CandidateExclude, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().exclude_candidate(principal, candidate_id, payload.reason)


@router.post("/candidates/{candidate_id}/withdraw")
def withdraw_candidate(candidate_id: int, payload: CandidateWithdraw, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().withdraw_candidate(principal, candidate_id, payload.reason)


@router.post("/claims", status_code=201)
def create_claim(payload: ClaimCreate, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().create_claim(principal, payload.model_dump())


@router.post("/claims/{claim_id}/material-check")
def check_materials(claim_id: int, payload: MaterialCheck, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().check_materials(principal, claim_id, payload.model_dump())


@router.post("/claims/{claim_id}/review")
def review_claim(claim_id: int, payload: ClaimReview, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().review_claim(principal, claim_id, payload.model_dump())


@router.post("/claims/{claim_id}/withdraw")
def withdraw_claim(claim_id: int, payload: ClaimWithdraw, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().withdraw_claim(principal, claim_id, payload.reason)


@router.get("/cases/{case_id}")
def case_identification(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().get_case_identification(case_id, principal)


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().get_candidate(candidate_id, principal)


@router.get("/claims/{claim_id}")
def get_claim(claim_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return IdentificationService().get_claim(claim_id, principal)
