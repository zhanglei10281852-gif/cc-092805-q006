from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClueCreate(BaseModel):
    source_type: Literal["hospital", "police", "family"]
    clue_kind: Literal["name_fragment", "belonging", "kinship"]
    original_text: str = Field(min_length=2, max_length=2000)
    trust_level: Literal["high", "medium", "low"]
    reporter: str = Field(min_length=2, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=120)


class ClueWithdraw(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class CandidateCreate(BaseModel):
    candidate_name: str = Field(min_length=2, max_length=120)
    identity_number: str | None = Field(default=None, max_length=80)
    contact_name: str = Field(default="", max_length=120)
    contact_phone: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=1000)
    clue_ids: list[int] = Field(default_factory=list, max_length=50)


class CandidateMerge(BaseModel):
    target_candidate_id: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)


class CandidateResolve(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class ClaimMaterial(BaseModel):
    material_type: str = Field(min_length=2, max_length=60)
    reference: str = Field(min_length=2, max_length=120)
    note: str = Field(default="", max_length=500)


class ClaimCreate(BaseModel):
    candidate_id: int = Field(gt=0)
    claimant_name: str = Field(min_length=2, max_length=120)
    claimant_identity: str = Field(min_length=4, max_length=80)
    claimant_phone: str = Field(min_length=5, max_length=40)
    relation_to_decedent: str = Field(min_length=1, max_length=60)
    materials: list[ClaimMaterial] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=8, max_length=120)


class MaterialsVerify(BaseModel):
    passed: bool
    note: str = Field(default="", max_length=500)


class ClaimReview(BaseModel):
    decision: Literal["confirm", "reject"]
    rationale: str = Field(min_length=2, max_length=1000)
