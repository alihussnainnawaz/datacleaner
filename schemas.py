# schemas.py

from pydantic import BaseModel, field_validator
from typing import Optional
from enum import Enum


# ── Enums ─────────────────────────────────────────────────

class ConstructionStage(str, Enum):
    plinth  = "Plinth"
    lintel  = "Lintel"
    roof    = "Roof"

class GRMStatus(str, Enum):
    open          = "Open"
    under_review  = "Under Review"
    resolved      = "Resolved"

class FundingSource(str, Enum):
    wb   = "WB"
    adb  = "ADB"
    isdb = "IsDB"
    eib  = "EIB"
    gos  = "GoS"
    gop  = "GoP"

class Gender(str, Enum):
    male   = "Male"
    female = "Female"


# ── Upload ────────────────────────────────────────────────

class UploadResponse(BaseModel):
    success:      bool
    file_name:    str
    file_id:      str          # UUID assigned on upload
    row_count:    int
    column_count: int
    columns:      list[str]
    message:      str


class DatabaseImportRequest(BaseModel):
    db_type:        str = "sqlite"
    host:           Optional[str] = None
    port:           Optional[int] = None
    database:       Optional[str] = None
    username:       Optional[str] = None
    password:       Optional[str] = None
    sqlite_path:    Optional[str] = None
    connection_uri: Optional[str] = None
    query:          str
    source_name:    Optional[str] = "database_query"


# ── Cleaning / Inconsistency Report ──────────────────────

class InconsistencyItem(BaseModel):
    row:            int            # Row index in the Excel file
    column:         str            # Column name
    original_value: str            # What was in the cell
    suggested_fix:  Optional[str]  # Best fuzzy match or standard value
    confidence:     float          # 0.0 - 1.0 match confidence
    issue_type:     str            # "casing" | "spelling" | "invalid_value" | "missing"

class CleaningReport(BaseModel):
    file_id:          str
    total_rows:       int
    total_issues:     int
    auto_corrected:   int          # Issues fixed automatically (confidence >= FUZZY_EXACT_THRESHOLD)
    flagged_review:   int          # Issues needing human review
    quarantined_rows: int          # Rows with missing mandatory fields
    issues:           list[InconsistencyItem]


# ── Validation Report ─────────────────────────────────────

class ValidationFailure(BaseModel):
    row:        int
    column:     str
    value:      str
    rule:       str        # e.g. "construction_sequence" | "disbursement_alignment"
    action:     str        # e.g. "record_flagged" | "stage_rejected" | "record_quarantined"
    message:    str

class ValidationReport(BaseModel):
    file_id:           str
    total_rows:        int
    passed:            int
    failed:            int
    failures:          list[ValidationFailure]


# ── Transform ─────────────────────────────────────────────

class TransformRequest(BaseModel):
    file_id:     str
    apply_fixes: bool = True       # Apply auto-corrections from cleaning report
    donor_tag:   Optional[FundingSource] = None  # Tag all records with a donor

class TransformResponse(BaseModel):
    success:        bool
    file_id:        str
    output_file:    str            # Path/name of cleaned output Excel file
    rows_exported:  int
    message:        str


# ── Generic ───────────────────────────────────────────────

class ErrorResponse(BaseModel):
    success: bool = False
    error:   str
    detail:  Optional[str] = None
