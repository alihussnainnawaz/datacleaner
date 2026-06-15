# transform.py

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from typing import Optional
from collections import Counter
from io import BytesIO
import pandas as pd

from file_handler import load_dataframe, save_dataframe
from config import AUTO_DATE_FORMAT
from cleaner import (
    auto_clean,
    clean_dataframe,
    get_full_dataset,
    trim_whitespace,
    standardize_dates,
    standardize_values,
    get_column_unique_values,
    resolve_column_name,
    _detect_special_characters,
    _detect_name_issues,
    _detect_cnic_issues,
    _detect_gender_issues,
    _detect_duplicate_rows,
    _find_repeating_digit_cells,
    _to_display_value,
)
from transformer import transform_dataframe
from schemas import (
    TransformRequest,
    CleaningReport,
    ValidationReport,
    TransformResponse,
)

router = APIRouter()


# ── In-Memory Session Store ───────────────────────────────

_sessions: dict[str, object] = {}
_reports: dict[str, dict] = {}




def _json_value(value):
    """Make pandas/numpy values safe for JSON and report display."""
    if pd.isna(value):
        return "None"
    return str(value)


def _make_issue(row, column, issue_type, original, suggested_fix=None, confidence=1.0):
    return {
        "row": int(row) + 2,  # Excel-style row number, including header row
        "column": str(column),
        "original_value": _json_value(original),
        "suggested_fix": None if suggested_fix is None else _json_value(suggested_fix),
        "confidence": float(confidence),
        "issue_type": issue_type,
    }


def _issue_distribution(issues: list[dict]) -> dict:
    counts = Counter(i.get("issue_type", "other") for i in issues)
    duplicate = sum(counts.get(k, 0) for k in ("duplicate", "uuid_dupe", "uuid_duplicate", "duplicate_row", "duplicate_record", "cnic_duplicate"))
    formatting = sum(counts.get(k, 0) for k in ("casing", "trim", "text_cleaning", "date_standardization", "bool_standardization", "gender_standardization", "bank_standardization", "geo_standardization", "manual_trim", "manual_date_standardization", "manual_standardization"))
    invalid = sum(counts.get(k, 0) for k in ("invalid_value", "cnic_error", "cnic_invalid", "cnic_format", "date_invalid", "gender_invalid", "repeating_digit"))
    return {
        "missing": counts.get("missing", 0),
        "duplicate": duplicate,
        "casing": formatting,
        "spelling": counts.get("spelling", 0),
        "invalid_value": invalid,
        "business_rule": counts.get("business_rule", 0),
    }


def _issue_key(issue: dict) -> tuple[int, str, str]:
    return (
        int(issue.get("row") or 0),
        str(issue.get("column") or ""),
        str(issue.get("issue_type") or ""),
    )


def _refresh_cleaning_metrics(cleaning_audit: dict) -> None:
    issues = cleaning_audit.get("issues", [])
    auto_corrected = sum(1 for issue in issues if issue.get("suggested_fix") is not None)
    flagged_review = max(0, len(issues) - auto_corrected)
    quarantined_rows = {
        int(issue.get("row") or 0)
        for issue in issues
        if issue.get("suggested_fix") is None
        and issue.get("issue_type") in {"missing", "cnic_invalid", "cnic_duplicate", "uuid_duplicate", "duplicate_record"}
    }
    cleaning_audit["total_issues"] = len(issues)
    cleaning_audit["auto_corrected"] = auto_corrected
    cleaning_audit["flagged_review"] = flagged_review
    cleaning_audit["quarantined_rows"] = len(quarantined_rows)
    cleaning_audit["issue_distribution"] = _issue_distribution(issues)


def _mark_manual_resolved(file_id: str, columns: list[str] | None = None, issue_types: set[str] | None = None) -> int:
    cleaning_audit = _reports.get(file_id, {}).get("cleaning")
    if not cleaning_audit:
        return 0
    column_set = {str(c) for c in columns} if columns else None
    resolved = 0
    for issue in cleaning_audit.get("issues", []):
        if issue.get("suggested_fix") is not None:
            continue
        if column_set is not None and str(issue.get("column")) not in column_set:
            continue
        if issue_types is not None and str(issue.get("issue_type")) not in issue_types:
            continue
        issue["suggested_fix"] = "Manual update applied"
        issue["confidence"] = 1.0
        resolved += 1
    if resolved:
        _refresh_cleaning_metrics(cleaning_audit)
    return resolved


def _record_manual_changes(file_id: str, before_df, after_df, columns: list[str], issue_type: str) -> int:
    cleaning_audit = _reports.get(file_id, {}).get("cleaning")
    if not cleaning_audit:
        return 0
    existing = {
        (
            int(issue.get("row") or 0),
            str(issue.get("column") or ""),
            str(issue.get("issue_type") or ""),
        )
        for issue in cleaning_audit.get("issues", [])
    }
    added = 0
    for column in columns:
        if column not in before_df.columns or column not in after_df.columns:
            continue
        before = before_df[column]
        after = after_df[column]
        changed = before.astype("string").fillna("<NA>") != after.astype("string").fillna("<NA>")
        for idx in before_df.index[changed]:
            key = (int(idx) + 2, str(column), issue_type)
            if key in existing:
                continue
            cleaning_audit.setdefault("issues", []).append({
                "row": int(idx) + 2,
                "column": str(column),
                "original_value": _to_display_value(before.loc[idx], column),
                "suggested_fix": _to_display_value(after.loc[idx], column),
                "confidence": 1.0,
                "issue_type": issue_type,
            })
            existing.add(key)
            added += 1
    if added:
        cleaning_audit["baseline_issues"] = int(cleaning_audit.get("baseline_issues", cleaning_audit.get("total_issues", 0))) + added
        _refresh_cleaning_metrics(cleaning_audit)
    return added


def _build_cleaning_audit(file_id: str, before_df, after_df, summary: dict | None = None) -> dict:
    """Fast audit builder.
    The previous version rescanned every cell multiple times. For 1.2M rows that becomes
    the main bottleneck. The optimized cleaner already returns an issue sample and exact
    summary counts, so this function now consumes that report directly.
    """
    cr = summary.get("cleaning_report") if summary else None
    if cr:
        issues = [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in getattr(cr, "issues", [])]
        return {
            "total_rows": int(len(after_df)),
            "total_issues": int(getattr(cr, "total_issues", len(issues))),
            "auto_corrected": int(getattr(cr, "auto_corrected", 0)),
            "flagged_review": int(getattr(cr, "flagged_review", 0)),
            "quarantined_rows": int(getattr(cr, "quarantined_rows", 0)),
            "baseline_issues": int(getattr(cr, "total_issues", len(issues))),
            "issue_distribution": _issue_distribution(issues),
            "issues": issues,
        }
    return {
        "total_rows": int(len(after_df)),
        "total_issues": 0,
        "auto_corrected": 0,
        "flagged_review": 0,
        "quarantined_rows": 0,
        "baseline_issues": 0,
        "issue_distribution": _issue_distribution([]),
        "issues": [],
    }


def _resolved_change_flags(file_id: str) -> dict[tuple[int, str], list[str]]:
    cleaning_audit = _reports.get(file_id, {}).get("cleaning") or {}
    flags: dict[tuple[int, str], list[str]] = {}
    for issue in cleaning_audit.get("issues", []):
        if not issue.get("suggested_fix"):
            continue
        row = int(issue.get("row", 0)) - 2
        column = str(issue.get("column", ""))
        if row < 0 or not column:
            continue
        flags.setdefault((row, column), []).append("resolved_change")
    return flags

def _get_session_df(file_id: str):
    if file_id not in _sessions:
        df, _ = load_dataframe(file_id)
        _sessions[file_id] = df
    return _sessions[file_id]


def _update_session_df(file_id: str, df):
    _sessions[file_id] = df


def _clear_session(file_id: str):
    _sessions.pop(file_id, None)


def _invalidate_validation(file_id: str):
    if file_id in _reports:
        _reports[file_id].pop("validation", None)


# ══════════════════════════════════════════════════════════
# DATASET
# ══════════════════════════════════════════════════════════

@router.get(
    "/dataset/{file_id}",
    summary     = "Load full dataset with cell-level flags",
    description = "Returns all rows and columns with per-cell flags: special chars, CNIC errors, UUID dupes, null values, duplicate rows, repeating digits.",
)
async def get_dataset(
    file_id: str,
    uuid_column: Optional[str] = None,
    page: int = 1,
    page_size: int = 100,
    flag_filter: str = "all",
):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    try:
        dataset = get_full_dataset(
            df,
            uuid_column=uuid_column,
            extra_flags=_resolved_change_flags(file_id),
            page=page,
            page_size=page_size,
            flag_filter=flag_filter,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not process dataset: {str(e)}")

    return JSONResponse(content=dataset)


# ══════════════════════════════════════════════════════════
# AUTO PIPELINE
# ══════════════════════════════════════════════════════════

@router.post(
    "/auto-clean/{file_id}",
    summary     = "Run automated cleaning pipeline",
    description = """
Runs all cleaning steps automatically in sequence:
1. Trim leading / trailing whitespace
2. Standardize bank names (HBL → Habib Bank Limited)
3. Standardize geo names (district / tehsil / UC spellings)
4. Convert date columns to MM/DD/YYYY
5. Convert binary 0/1 columns to No/Yes
6. Flag repeating-digit CNICs and UUIDs
7. Run standard cleaning pipeline (casing, spelling, invalids, duplicates)
""",
)
async def run_auto_clean(file_id: str):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    original_df = df.copy()

    try:
        cleaned_df, summary = auto_clean(file_id, df)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto-clean failed: {str(e)}")

    _update_session_df(file_id, cleaned_df)
    _invalidate_validation(file_id)

    # Save cleaned version to disk
    try:
        save_dataframe(cleaned_df, file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save cleaned file: {str(e)}")

    # Build JSON-safe summary
    steps_out = [
        {
            "step":    s["step"],
            "changes": s["changes"],
            "detail":  s["detail"],
        }
        for s in summary["steps"]
    ]

    cleaning_audit = _build_cleaning_audit(file_id, original_df, cleaned_df, summary)
    _reports[file_id] = {"cleaning": cleaning_audit}
    cleaning_report = summary.get("cleaning_report")

    return JSONResponse(content={
        "success":         True,
        "file_id":         file_id,
        "total_changes":   summary["total_changes"],
        "steps":           steps_out,
        "cleaning_summary": {
            "total_issues":     cleaning_audit["total_issues"],
            "auto_corrected":   cleaning_audit["auto_corrected"],
            "flagged_review":   cleaning_audit["flagged_review"],
            "quarantined_rows": cleaning_audit["quarantined_rows"],
        },
        "repeating_digit_cells": [
            {"row": r, "col": c}
            for r, c in summary.get("repeating_digit_cells", [])
        ],
        "message": f"Auto-clean complete. {summary['total_changes']} total change(s) applied.",
    })


# ══════════════════════════════════════════════════════════
# FULL PIPELINE (clean + validate + export)
# ══════════════════════════════════════════════════════════

@router.post(
    "/run",
    response_model = TransformResponse,
    summary        = "Run full pipeline: auto-clean + validate + export",
)
async def run_full_pipeline(request: TransformRequest):
    try:
        df = _get_session_df(request.file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    # Step 1 — Auto clean
    try:
        cleaned_df, _ = auto_clean(request.file_id, df)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto-clean stage failed: {str(e)}")

    # Step 2 — Validate
    try:
        final_df, validation_report, transform_response = transform_dataframe(
            file_id     = request.file_id,
            df          = cleaned_df,
            apply_fixes = request.apply_fixes,
            donor_tag   = request.donor_tag.value if request.donor_tag else None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation stage failed: {str(e)}")

    # Step 3 — Save
    try:
        output_path = save_dataframe(final_df, request.file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save output file: {str(e)}")

    _update_session_df(request.file_id, final_df)
    _invalidate_validation(request.file_id)
    transform_response.output_file = output_path.name

    return transform_response


# ══════════════════════════════════════════════════════════
# MANUAL CLEANING ENDPOINTS
# ══════════════════════════════════════════════════════════

@router.post(
    "/trim",
    summary = "Trim leading and trailing whitespace",
)
async def trim_spaces(
    file_id: str              = Body(...),
    columns: list[str] | None = Body(None),
):
    try:
        df = _get_session_df(file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    before_df = df.copy()
    df = trim_whitespace(df, columns)
    _update_session_df(file_id, df)
    _invalidate_validation(file_id)
    affected_columns = columns or list(df.columns)
    manual_resolved = _mark_manual_resolved(file_id, affected_columns, {"trim"})
    manual_recorded = _record_manual_changes(file_id, before_df, df, affected_columns, "manual_trim")

    return JSONResponse(content={
        "success": True,
        "manual_resolved": manual_resolved,
        "manual_recorded": manual_recorded,
        "message": f"Whitespace trimmed from {'all columns' if not columns else ', '.join(columns)}.",
    })


@router.post(
    "/dates",
    summary = "Standardize date columns to MM-DD-YYYY",
)
async def format_dates(
    file_id: str       = Body(...),
    columns: list[str] = Body(...),
    fmt:     str       = Body(...),
):
    try:
        df = _get_session_df(file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    before_df = df.copy()
    try:
        df, failures = standardize_dates(df, columns, fmt)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Date formatting failed: {str(e)}")

    _update_session_df(file_id, df)
    _invalidate_validation(file_id)
    manual_resolved = _mark_manual_resolved(file_id, columns, {"date_standardization", "date_invalid"})
    manual_recorded = _record_manual_changes(file_id, before_df, df, columns, "manual_date_standardization")

    return JSONResponse(content={
        "success":        True,
        "columns":        columns,
        "format_applied": AUTO_DATE_FORMAT,
        "failed_cells":   failures,
        "manual_resolved": manual_resolved,
        "manual_recorded": manual_recorded,
        "message":        f"Dates formatted in {len(columns)} column(s). {len(failures)} cell(s) could not be parsed.",
    })


@router.get(
    "/unique/{file_id}/{column}",
    summary = "Get unique values in a column",
)
async def get_unique_values(file_id: str, column: str):
    try:
        df = _get_session_df(file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    resolved_column = resolve_column_name(df, column)
    values = get_column_unique_values(df, column)

    return JSONResponse(content={
        "column": resolved_column or column,
        "values": values,
        "count":  len(values),
    })


@router.post(
    "/standardize",
    summary = "Apply value standardization mapping to a column",
)
async def standardize_column(
    file_id: str  = Body(...),
    column:  str  = Body(...),
    mapping: dict = Body(...),
):
    try:
        df = _get_session_df(file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    resolved_column = resolve_column_name(df, column)
    if resolved_column is None:
        raise HTTPException(
            status_code = 400,
            detail      = f"Column '{column}' not found in dataset."
        )

    before_df = df.copy()
    try:
        df, changes = standardize_values(df, resolved_column, mapping)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Standardization failed: {str(e)}")

    _update_session_df(file_id, df)
    _invalidate_validation(file_id)
    manual_resolved = _mark_manual_resolved(file_id, [resolved_column])
    manual_recorded = _record_manual_changes(file_id, before_df, df, [resolved_column], "manual_standardization")

    return JSONResponse(content={
        "success": True,
        "column":  resolved_column,
        "changes": changes,
        "manual_resolved": manual_resolved,
        "manual_recorded": manual_recorded,
        "message": f"{changes} value(s) updated in '{column}'.",
    })


# ══════════════════════════════════════════════════════════
# STANDARD PIPELINE ENDPOINTS
# ══════════════════════════════════════════════════════════

@router.post(
    "/clean/{file_id}",
    response_model = CleaningReport,
    summary        = "Run standard cleaning pipeline on uploaded file",
)
async def clean_file(file_id: str):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    try:
        cleaned_df, report = clean_dataframe(file_id, df)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cleaning failed: {str(e)}")

    try:
        save_dataframe(cleaned_df, file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not save cleaned file: {str(e)}")

    _update_session_df(file_id, cleaned_df)
    _invalidate_validation(file_id)
    cleaning_audit = _build_cleaning_audit(
        file_id,
        df.copy(),
        cleaned_df,
        {"cleaning_report": report},
    )
    _reports[file_id] = {"cleaning": cleaning_audit}
    return report


@router.post(
    "/validate/{file_id}",
    response_model = ValidationReport,
    summary        = "Run validation checks on cleaned file",
)
async def validate_file(file_id: str):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    try:
        _, validation_report, _ = transform_dataframe(
            file_id     = file_id,
            df          = df,
            apply_fixes = False,
            donor_tag   = None,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")

    return validation_report


@router.get(
    "/report/{file_id}",
    summary = "Get combined cleaning + validation summary",
)
async def get_report(
    file_id: str,
    issue_page: int = 1,
    issue_page_size: int = 100,
    issue_status: str = "all",
    issue_column: str = "all",
    issue_type: str = "all",
):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    # IMPORTANT: do not re-clean the cleaned dataframe here.
    # Use the stored audit created by /auto-clean, otherwise the report will say 0 issues / 100%.
    cleaning_audit = _reports.get(file_id, {}).get("cleaning")
    if cleaning_audit is None:
        # Fallback for users who open Report before Auto Clean.
        original_df = df.copy()
        cleaned_df, summary = auto_clean(file_id, df.copy())
        cleaning_audit = _build_cleaning_audit(file_id, original_df, cleaned_df, summary)
        _reports[file_id] = {"cleaning": cleaning_audit}

    cached_validation = _reports.setdefault(file_id, {}).get("validation")
    if cached_validation is None:
        try:
            _, validation_report, _ = transform_dataframe(
                file_id     = file_id,
                df          = df,
                apply_fixes = False,
                donor_tag   = None,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")
        cached_validation = {
            "total_rows": validation_report.total_rows,
            "passed": validation_report.passed,
            "failed": validation_report.failed,
            "failures": [f.model_dump() for f in validation_report.failures],
        }
        _reports[file_id]["validation"] = cached_validation

    validation = {
        "total_rows": cached_validation["total_rows"],
        "passed":     cached_validation["passed"],
        "failed":     cached_validation["failed"],
        "failures":   [],
    }
    issue_rows = _build_issue_page(
        cleaning_audit,
        cached_validation["failures"],
        issue_page,
        issue_page_size,
        issue_status,
        issue_column,
        issue_type,
    )
    cleaning_public = {k: v for k, v in cleaning_audit.items() if k != "issues"}

    return JSONResponse(content={
        "file_id": file_id,
        "cleaning": cleaning_public,
        "validation": validation,
        "issue_rows": issue_rows,
        "overall_health": _health_score_from_counts(
            cleaning_audit["flagged_review"],
            cached_validation["failed"],
        ),
    })


def _build_issue_page(
    cleaning_audit: dict,
    validation_failures: list[dict],
    page: int,
    page_size: int,
    status_filter: str,
    column_filter: str,
    type_filter: str,
) -> dict:
    page = max(1, int(page or 1))
    page_size = min(500, max(25, int(page_size or 100)))
    rows = []

    for issue in cleaning_audit.get("issues", []):
        resolved = issue.get("suggested_fix") is not None
        rows.append({
            "row": issue.get("row"),
            "column": issue.get("column"),
            "issue_key": issue.get("issue_type") or "other",
            "original": issue.get("original_value"),
            "resolution": issue.get("suggested_fix") or "Manual review",
            "confidence": issue.get("confidence") or 0,
            "status_key": "resolved" if resolved else "review",
            "status": "Resolved" if resolved else "Needs Review",
        })

    for failure in validation_failures:
        rows.append({
            "row": failure.get("row"),
            "column": failure.get("column"),
            "issue_key": failure.get("rule") or "validation",
            "original": failure.get("value"),
            "resolution": failure.get("message"),
            "confidence": 1,
            "status_key": "review",
            "status": "Needs Review",
        })

    option_rows = rows
    columns = sorted({str(r["column"]) for r in option_rows if r.get("column")})
    types = sorted({str(r["issue_key"]) for r in option_rows if r.get("issue_key")})
    status_counts = Counter(r["status_key"] for r in option_rows)

    filtered = rows
    if status_filter != "all":
        filtered = [r for r in filtered if r["status_key"] == status_filter]
    if column_filter != "all":
        filtered = [r for r in filtered if str(r.get("column") or "") == column_filter]
    if type_filter != "all":
        filtered = [r for r in filtered if str(r.get("issue_key") or "") == type_filter]

    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "rows": filtered[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "columns": columns,
        "types": types,
        "status_counts": {
            "all": len(option_rows),
            "resolved": status_counts.get("resolved", 0),
            "review": status_counts.get("review", 0),
        },
    }


def _health_score_from_counts(manual_review: int, failed_rows: int) -> str:
    if failed_rows > 0 or manual_review > 0:
        return "Warning"
    return "Clean"


@router.get(
    "/download/{file_id}",
    summary = "Download the cleaned Excel file",
)
async def download_file(file_id: str):
    from pathlib import Path
    from config import UPLOAD_DIR

    if file_id in _sessions:
        save_dataframe(_sessions[file_id], file_id)

    output_path = UPLOAD_DIR / f"{file_id}_cleaned.xlsx"

    if not output_path.exists():
        if file_id in _sessions:
            output_path = save_dataframe(_sessions[file_id], file_id)
        else:
            raise HTTPException(
                status_code = 404,
                detail      = f"No cleaned file found for '{file_id}'. Run the pipeline first."
            )

    return FileResponse(
        path       = str(output_path),
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename   = f"cleaned_{file_id}.xlsx",
    )


@router.get(
    "/download-issues/{file_id}",
    summary = "Download rows that currently contain cell-level issues",
)
async def download_issue_rows(file_id: str, uuid_column: Optional[str] = None):
    try:
        df = _get_session_df(file_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load file: {str(e)}")

    review_notes: dict[int, list[str]] = {}
    report = _reports.get(file_id, {})

    cleaning = report.get("cleaning") or {}
    for issue in cleaning.get("issues", []):
        if issue.get("suggested_fix") is not None:
            continue
        row_idx = int(issue.get("row") or 0) - 2
        if row_idx < 0:
            continue
        column = str(issue.get("column") or "-")
        issue_type = str(issue.get("issue_type") or "manual_review")
        original = issue.get("original_value", "")
        review_notes.setdefault(row_idx, []).append(f"{column}: {issue_type} ({original})")

    validation = report.get("validation") or {}
    for failure in validation.get("failures", []):
        row_idx = int(failure.get("row") or 0) - 2
        if row_idx < 0:
            continue
        column = str(failure.get("column") or "-")
        rule = str(failure.get("rule") or "validation")
        message = str(failure.get("message") or "Needs review")
        review_notes.setdefault(row_idx, []).append(f"{column}: {rule} - {message}")

    if not review_notes:
        dataset = get_full_dataset(
            df,
            uuid_column,
            extra_flags=_resolved_change_flags(file_id),
            page_size=0,
        )
        for row_num, row in zip(dataset["row_numbers"], dataset["rows"]):
            row_idx = int(row_num) - 2
            for col, cell in zip(dataset["columns"], row):
                flags = [f for f in (cell.get("flags") or []) if f != "resolved_change"]
                if flags:
                    review_notes.setdefault(row_idx, []).append(f"{col}: {', '.join(flags)}")

    issue_indexes = sorted(i for i in review_notes if i in df.index)

    if issue_indexes:
        export_df = df.loc[issue_indexes].copy()
        export_df.insert(0, "_source_row", [i + 2 for i in issue_indexes])
        export_df["_manual_review_issues"] = ["; ".join(review_notes[i]) for i in issue_indexes]
    else:
        export_df = pd.DataFrame(columns=["_source_row", "_manual_review_issues", *df.columns.tolist()])

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Issue Rows")
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="manual_review_{file_id}.xlsx"'},
    )


@router.delete(
    "/session/{file_id}",
    summary = "Clear in-memory session for a file",
)
async def clear_session(file_id: str):
    _clear_session(file_id)
    return JSONResponse(content={
        "success": True,
        "message": f"Session cleared for '{file_id}'.",
    })


# ── Helper ────────────────────────────────────────────────

def _health_score(cleaning_report, validation_report) -> str:
    total_rows  = max(validation_report.total_rows, 1)
    issue_ratio = (cleaning_report.total_issues + validation_report.failed) / total_rows
    if issue_ratio == 0:     return "Clean"
    elif issue_ratio < 0.10: return "Warning"
    else:                    return "Critical"
