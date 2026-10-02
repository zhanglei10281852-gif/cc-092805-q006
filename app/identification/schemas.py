from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    hospital = "hospital"
    police = "police"
    family = "family"
    staff = "staff"
    other = "other"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class ClueKind(str, Enum):
    name_fragment = "name_fragment"
    belongings = "belongings"
    kinship = "kinship"
    document = "document"
    physical = "physical"
    other = "other"


class ClueInput(BaseModel):
    source_type: SourceType
    clue_kind: ClueKind
    raw_text: str = Field(min_length=1, max_length=2000)
    confidence: Confidence
    reporter: str = Field(min_length=2, max_length=80)


class CandidateCreate(BaseModel):
    case_id: int = Field(gt=0)
    suggested_name: str = Field(min_length=1, max_length=120)
    identity_number: str = Field(default="", max_length=80)
    source_type: SourceType
    source_ref: str = Field(default="", max_length=160)
    confidence: Confidence
    clues: list[ClueInput] = Field(default_factory=list, max_length=50)


class ClueCreate(BaseModel):
    source_type: SourceType
    clue_kind: ClueKind
    raw_text: str = Field(min_length=1, max_length=2000)
    confidence: Confidence
    reporter: str = Field(min_length=2, max_length=80)


class CandidateMerge(BaseModel):
    source_candidate_id: int = Field(gt=0)
    target_candidate_id: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)


class CandidateExclude(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class CandidateWithdraw(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class ClaimDocumentInput(BaseModel):
    doc_type: str = Field(min_length=2, max_length=60)
    doc_reference: str = Field(min_length=2, max_length=120)
    notes: str = Field(default="", max_length=500)


class ClaimCreate(BaseModel):
    case_id: int = Field(gt=0)
    candidate_id: int = Field(gt=0)
    claimant_name: str = Field(min_length=2, max_length=120)
    claimant_identity: str = Field(min_length=4, max_length=80)
    relationship: str = Field(min_length=2, max_length=60)
    contact_phone: str = Field(min_length=5, max_length=40)
    idempotency_key: str = Field(min_length=8, max_length=120)
    documents: list[ClaimDocumentInput] = Field(min_length=1, max_length=20)


class MaterialCheck(BaseModel):
    passed: bool
    opinion: str = Field(default="", max_length=500)


class ClaimReview(BaseModel):
    approved: bool
    opinion: str = Field(default="", max_length=500)


class ClaimWithdraw(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
