from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
import pandas as pd
from schemas import ValidationReport, ValidationFailure, TransformResponse
try:
    from config import DATE_FORMAT
except Exception:
    DATE_FORMAT = "%m/%d/%Y"

NULLS = {"", "nan", "none", "null", "n/a", "na"}

def _norm(c):
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")

def _find(df, hints):
    for c in df.columns:
        n=_norm(c)
        if any(h in n for h in hints): return c
    return None

def _identifier_digits(value):
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() in NULLS:
        return ""
    try:
        if re.fullmatch(r"[+-]?\d+(\.0+)?", text) or re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)[eE][+-]?\d+", text):
            return str(int(Decimal(text).to_integral_value()))
    except (InvalidOperation, ValueError):
        pass
    return re.sub(r"\D", "", text)

def _display_value(val, col=None):
    if pd.isna(val):
        return "None"
    text = str(val).strip()
    if text.lower() in NULLS:
        return "None"
    if col and "cnic" in str(col).lower():
        digits = _identifier_digits(val)
        if digits:
            return digits
    return str(val)

def _fail(row, col, val, rule, action, msg):
    return ValidationFailure(row=int(row)+2, column=str(col), value=_display_value(val, col), rule=rule, action=action, message=msg)

def _normalize_column_names(df):
    df = df.copy()
    df.columns = [_norm(c) for c in df.columns]
    return df

def transform_dataframe(file_id: str, df: pd.DataFrame, apply_fixes: bool=True, donor_tag: str=None):
    df = _normalize_column_names(df)
    failures=[]

    # Dates: vectorized parse on date-like columns
    for c in [x for x in df.columns if any(h in x for h in ("date", "dob", "time", "created", "updated")) or x.endswith("_at")]:
        parsed = pd.to_datetime(df[c], errors="coerce", dayfirst=False, format="mixed")
        good = df[c].notna() & parsed.notna()
        if apply_fixes and good.any():
            df.loc[good, c] = parsed[good].dt.strftime(DATE_FORMAT)
        bad = df[c].notna() & parsed.isna() & ~df[c].astype(str).str.strip().str.lower().isin(NULLS)
        failures += [_fail(i,c,df.loc[i,c],"date_format","record_flagged","Could not parse date.") for i in df.index[bad]]

    # Dynamic duplicate UUID/CNIC/business keys
    id_cols=[c for c in df.columns if any(h in c for h in ("uuid","uu_id","cnic"))]
    for c in id_cols:
        s=df[c].astype("string").str.strip()
        mask=s.notna() & ~s.str.lower().isin(NULLS) & s.duplicated(keep=False)
        failures += [_fail(i,c,df.loc[i,c],"unique_identifier","record_flagged",f"Duplicate value in {c}.") for i in df.index[mask]]

    # CNIC validity
    for c in [x for x in df.columns if "cnic" in x]:
        digits=df[c].map(_identifier_digits).astype("string")
        fake=digits.isin({"4330190000000","0000000000000","1111111111111","9999999999999"}) | digits.str.fullmatch(r"(\d)\1{12}").fillna(False)
        bad=df[c].notna() & (digits.str.len().ne(13) | fake)
        failures += [_fail(i,c,df.loc[i,c],"cnic_validity","record_flagged","CNIC must be unique, 13 digits, and not fake/repeating.") for i in df.index[bad]]

    # Geo completeness: any UC exists without district or reverse
    district=_find(df,("district",)); uc=_find(df,("union_council","jh_uc","uc")); tehsil=_find(df,("tehsil","taluka"))
    if district and uc:
        d_empty=df[district].astype(str).str.strip().str.lower().isin(NULLS)
        u_empty=df[uc].astype(str).str.strip().str.lower().isin(NULLS)
        mask=d_empty ^ u_empty
        failures += [_fail(i, district if d_empty.loc[i] else uc, "None", "geo_completeness", "record_flagged", "District and UC should both be present.") for i in df.index[mask]]
    if tehsil and district:
        t_empty=df[tehsil].astype(str).str.strip().str.lower().isin(NULLS)
        d_empty=df[district].astype(str).str.strip().str.lower().isin(NULLS)
        mask=t_empty ^ d_empty
        failures += [_fail(i, tehsil if t_empty.loc[i] else district, "None", "geo_completeness", "record_flagged", "District and tehsil should both be present.") for i in df.index[mask]]

    if donor_tag:
        fs=_find(df,("funding_source","donor","payment_by_ifis","ifis")) or "funding_source"
        if fs not in df.columns: df[fs]=donor_tag
        else:
            empty=df[fs].astype(str).str.strip().str.lower().isin(NULLS)
            df.loc[empty,fs]=donor_tag

    failed_rows = len({f.row for f in failures})
    report=ValidationReport(file_id=file_id,total_rows=len(df),passed=max(len(df)-failed_rows,0),failed=failed_rows,failures=failures)
    resp=TransformResponse(success=True,file_id=file_id,output_file=f"{file_id}_cleaned.xlsx",rows_exported=len(df),message=f"Transformation complete. {failed_rows} failed row(s), {len(failures)} issue(s).")
    return df, report, resp
