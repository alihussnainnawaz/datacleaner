// validation_engine.js
// ─────────────────────────────────────────────────────────────────────────────
// Responsibilities:
//   1. COND_LABELS / condLabel()   – human-readable condition names used
//                                    everywhere in the UI.
//   2. normalize()                 – canonical string comparison helper.
//   3. loadGeneratedFilters()      – reads all .rule-card DOM nodes and builds
//                                    window.allConfigs for POSTing to /api/clean.
//   4. loadSheetFiltersIntoConfigs() – loads a saved profile's filters into
//                                    allConfigs (used by the Data-Val-Utility
//                                    legacy path; kept for compatibility).
//   5. evaluateRow()               – pure function: evaluates ONE filter config
//                                    against one row of data, returns
//                                    { pass, label, actual, expected }.
//                                    Used by the client-side validation preview
//                                    in the Validate view.
//   6. runClientValidation()       – entry point called by the Validate view.
//                                    Takes an array of { columns, rows } data
//                                    already fetched from /api/dataset and runs
//                                    all active filters client-side, producing
//                                    per-row highlighted results exactly like
//                                    the Clean section's renderCleanTable().
//   7. runValidation()             – legacy entry point kept for the standalone
//                                    Data-Validation-Utility flow (writes HTML
//                                    into #resultsArea).  Kept fully intact.
// ─────────────────────────────────────────────────────────────────────────────

// ── 1. COND_LABELS ────────────────────────────────────────────────────────────

const COND_LABELS = {
    "eq":           "Equal To",
    "neq":          "Not Equal To",
    "gt":           "Greater Than",
    "lt":           "Less Than",
    "gte":          "Greater Than or Equal To",
    "lte":          "Less Than or Equal To",
    "btwn":         "Between",
    "nbtwn":        "Not Between",
    "empty":        "Missing / Null",
    "dup":          "Duplicate",
    "cross":        "Cross Check",
    "doublecross":  "Double Cross Check",
    "and":          "AND",
    "or":           "OR",
    "compare":      "Compare",
    "compare2":     "Compare (Dataset 2)",
    "coords":       "Coordinate Check",
    // Date sub-conditions
    "date":          "Date Filter",
    "date_eq":       "Date Equal To",
    "date_neq":      "Date Not Equal To",
    "date_before":   "Date Before",
    "date_after":    "Date After",
    "date_before_eq":"Date Before or Equal To",
    "date_after_eq": "Date After or Equal To",
    "date_btwn":     "Date Between",
    "date_nbtwn":    "Date Not Between",
    "date_empty":    "Missing / Null Date",
    "date_invalid":  "Invalid Date Format",
    "date_year_eq":  "Year Equals",
    "date_year_gt":  "Year Greater Than",
    "date_year_lt":  "Year Less Than",
    "date_month_eq": "Month Equals",
    "date_day_eq":   "Day Equals",
    "date_weekday":  "Weekday Equals",
    "date_future":   "Date in the Future",
    "date_past":     "Date in the Past",
};

function condLabel(cond) {
    return COND_LABELS[cond] || cond;
}

// ── 2. HELPERS ────────────────────────────────────────────────────────────────

// Canonical string normalization: trim, lowercase, strip trailing ".0",
// map nan/undefined/null → "".
function normalize(v) {
    let s = String(v ?? "").trim().toLowerCase();
    if (s === "nan" || s === "undefined" || s === "null") s = "";
    if (s.endsWith(".0")) s = s.slice(0, -2);
    return s;
}

// Parse a raw cell value into a midnight-normalised Date, or null.
// Handles ISO 8601, MM/DD/YYYY, DD/MM/YYYY, DD-MM-YYYY, and "Excel serial"
// numbers (integer days since 1900-01-01).
function _parseDate(raw) {
    if (raw === "" || raw == null) return null;
    const s = String(raw).trim();
    if (s === "" || normalize(s) === "") return null;

    // ISO / RFC / natural language → native parse first
    const d = new Date(s);
    if (!isNaN(d.getTime())) { d.setHours(0, 0, 0, 0); return d; }

    // DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    const dmy = s.match(/^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{4})$/);
    if (dmy) {
        const d2 = new Date(`${dmy[3]}-${dmy[2].padStart(2,"0")}-${dmy[1].padStart(2,"0")}`);
        if (!isNaN(d2.getTime())) { d2.setHours(0, 0, 0, 0); return d2; }
    }

    // Excel serial number (numeric string representing days since 1900-01-00)
    const serial = parseInt(s, 10);
    if (!isNaN(serial) && String(serial) === s && serial > 1 && serial < 100000) {
        // Excel epoch: December 30, 1899
        const excelEpoch = new Date(1899, 11, 30);
        const d3 = new Date(excelEpoch.getTime() + serial * 86400000);
        if (!isNaN(d3.getTime())) { d3.setHours(0, 0, 0, 0); return d3; }
    }

    return null;
}

function _dateOnly(d) {
    if (!d) return null;
    const c = new Date(d);
    c.setHours(0, 0, 0, 0);
    return c;
}

// ── 3. loadGeneratedFilters ───────────────────────────────────────────────────
// Reads every .rule-card in #tablesContainer and builds window.allConfigs.
// Called immediately before POSTing to /api/clean and before runValidation().

let allConfigs = [];
window.allConfigs = allConfigs;

function loadGeneratedFilters() {
    allConfigs = [];
    window.allConfigs = allConfigs;

    if (typeof syncColumns === "function") syncColumns();

    document.querySelectorAll(".rule-card").forEach((box) => {
        const idMatch = box.id && box.id.match(/tableBox_(\d+)/);
        if (!idMatch) return;
        const id    = parseInt(idMatch[1]);
        const cond1 = document.getElementById(`condSelect_${id}`)?.value;
        if (!cond1) return;

        // ── AND / OR ─────────────────────────────────────────────────────────
        if (cond1 === "and" || cond1 === "or") {
            const subfilters = [];
            box.querySelectorAll(".subfilter").forEach(row => {
                const subCol  = row.querySelector("select[id^='sub_col_']");
                const subCond = row.querySelector("select[id^='sub_cond_']");
                const subVal  = row.querySelector("input[id^='sub_val_']");
                if (subCol?.value && subCond?.value) {
                    subfilters.push({
                        colIdx:   parseInt(subCol.value),
                        cond:     subCond.value,
                        matchVal: subVal?.value || "",
                    });
                }
            });
            if (subfilters.length > 0) allConfigs.push({ cond: cond1, subfilters });
            return;
        }

        const col1Str = document.getElementById(`colSelect_${id}`)?.value;

        // ── COORDS ────────────────────────────────────────────────────────────
        if (cond1 === "coords") {
            const lngStr = document.getElementById(`coordLngCol_${id}`)?.value ?? "";
            const latStr = document.getElementById(`coordLatCol_${id}`)?.value ?? "";
            const level  = document.getElementById(`coordLevel_${id}`)?.value  || "";
            const verStr = document.getElementById(`coordVerifyCol_${id}`)?.value ?? "";
            if (lngStr === "" || latStr === "" || !level || verStr === "") return;
            const cols = window.allColumns || [];
            allConfigs.push({
                cond:          "coords",
                lngColIdx:     parseInt(lngStr),
                lngColName:    cols[parseInt(lngStr)]?.col1 || "",
                latColIdx:     parseInt(latStr),
                latColName:    cols[parseInt(latStr)]?.col1 || "",
                coordLevel:    level,
                verifyColIdx:  parseInt(verStr),
                verifyColName: cols[parseInt(verStr)]?.col1 || "",
            });
            return;
        }

        // ── DATE ──────────────────────────────────────────────────────────────
        if (cond1 === "date") {
            if (!col1Str) return;
            const dateCond = document.getElementById(`dateCond_${id}`)?.value   || "";
            const dateVal1 = document.getElementById(`dateVal1_${id}`)?.value   || "";
            const dateVal2 = document.getElementById(`dateVal2_${id}`)?.value   || "";
            allConfigs.push({ colIdx: parseInt(col1Str), cond: "date", dateCond, dateVal1, dateVal2 });
            return;
        }

        // ── COMPARE / COMPARE WITH DATASET 2 ────────────────────────────────────
        if (cond1 === "compare" || cond1 === "compare2") {
            const subCond  = document.getElementById(`compareSubCond_${id}`)?.value;
            if (!subCond) return;

            const dataset2Missing = !window.allColumns2 || window.allColumns2.length === 0;
            if (cond1 === "compare2" && dataset2Missing) {
                console.warn("Skipping Compare with Dataset 2 filter — Dataset 2 not loaded");
                return;
            }

            // Dataset 1's join key is the project's already-configured
            // UUID/ID column (Settings → UUID/ID column) — reused
            // automatically, no separate picker. Dataset 2's join key is
            // chosen once per filter via the "Dataset 2 UUID Column"
            // dropdown. Both are required for compare2 — without a real
            // join key on both sides there's nothing to match rows by.
            let uuidFields = {};
            if (cond1 === "compare2") {
                const d1UuidName = (typeof state !== "undefined" ? state.uuidColumn : null) || null;
                const d1UuidIdx  = d1UuidName != null
                    ? (window.allColumns || []).findIndex(c => c.col1 === d1UuidName)
                    : -1;
                const d2UuidStr  = document.getElementById(`compareD2Uuid_${id}`)?.value;
                if (!d1UuidName || d1UuidIdx < 0) {
                    console.warn("Skipping Compare with Dataset 2 filter — no UUID/ID column configured for Dataset 1 (Settings → UUID/ID column)");
                    return;
                }
                if (d2UuidStr === undefined || d2UuidStr === null || d2UuidStr === "") {
                    console.warn("Skipping Compare with Dataset 2 filter — no Dataset 2 UUID column selected");
                    return;
                }
                uuidFields = {
                    uuidColIdx:   d1UuidIdx,
                    uuidColName:  d1UuidName,
                    d2UuidColIdx: parseInt(d2UuidStr),
                    d2UuidColName: (window.allColumns2 || [])[parseInt(d2UuidStr)]?.col1 || "",
                };
            }

            if (subCond === "and" || subCond === "or") {
                const subfilters = [];
                document.querySelectorAll(`#compareSubRows_${id} .subfilter`).forEach(row => {
                    const colA  = row.querySelector("select[id^='cmp_colA_']");
                    const cond  = row.querySelector("select[id^='cmp_cond_']");
                    const colB  = row.querySelector("select[id^='cmp_colB_']");
                    if (colA && cond && colB && cond.value) {
                        subfilters.push({ colAIdx: parseInt(colA.value), cond: cond.value, colBIdx: parseInt(colB.value) });
                    }
                });
                allConfigs.push({ cond: cond1, subCond, compareSubfilters: subfilters, ...uuidFields });
                return;
            }

            if (subCond === "date") {
                // For compare2, Column 1 is implicit — the column the
                // filter was dragged onto (colSelect_id) — not a separate
                // "Date Column 1" picker (compare2 removes that picker
                // from the UI entirely; see handleCompareSubCondUI).
                const col1d = cond1 === "compare2"
                    ? document.getElementById(`colSelect_${id}`)?.value
                    : document.getElementById(`cmpDateCol1_${id}`)?.value;
                const col2d = document.getElementById(`cmpDateCol2_${id}`)?.value;
                const dc    = document.getElementById(`compareDateCond_${id}`)?.value || "";
                if (!col1d || !col2d || !dc) return;
                allConfigs.push({
                    cond: cond1, subCond: "date",
                    colIdx: parseInt(col1d), col2Idx: parseInt(col2d),
                    cmpDateMode: "columns", dateCond: dc,
                    ...uuidFields,
                });
                return;
            }

            // Simple sub-condition compare (eq, neq, gt, lt, gte, lte, btwn, nbtwn, empty, dup)
            const col2Str = document.getElementById(`compareCol2_${id}`)?.value;
            allConfigs.push({
                cond:    cond1,
                colIdx:  col1Str !== "" && col1Str != null ? parseInt(col1Str) : "",
                subCond,
                col2Idx: col2Str !== "" && col2Str != null ? parseInt(col2Str) : "",
                ...uuidFields,
            });
            return;
        }

        // ── CROSS / DOUBLE CROSS ──────────────────────────────────────────────
        if (!col1Str) return;
        const dataset2Missing = !window.allColumns2 || window.allColumns2.length === 0;
        if ((cond1 === "cross" || cond1 === "doublecross") && dataset2Missing) {
            console.warn("Skipping cross filter — Dataset 2 not loaded");
            return;
        }

        if (cond1 === "doublecross") {
            const d2col1 = document.getElementById(`d2col1_${id}`)?.value ?? "";
            const col2   = document.getElementById(`col2_${id}`)?.value   ?? "";
            const d2col2 = document.getElementById(`d2col2_${id}`)?.value ?? "";
            allConfigs.push({
                cond:         "doublecross",
                colIdx:       parseInt(col1Str),
                col2Idx:      d2col1 !== "" ? parseInt(d2col1) : "",
                colExtraIdx:  col2   !== "" ? parseInt(col2)   : "",
                colExtraIdx2: d2col2 !== "" ? parseInt(d2col2) : "",
            });
            return;
        }

        // ── CROSS ─────────────────────────────────────────────────────────────
        if (cond1 === "cross") {
            let crossCol = document.getElementById(`crossCol_${id}`)?.value || "";
            if (crossCol === "") {
                // Auto-match by column name
                const sourceName = (window.allColumns || [])[parseInt(col1Str)]?.col1?.toLowerCase().trim();
                if (sourceName && window.allColumns2) {
                    const matchIdx = window.allColumns2.findIndex(
                        c => (c.col1 || "").toLowerCase().trim() === sourceName
                    );
                    if (matchIdx !== -1) crossCol = String(matchIdx);
                }
            }
            const cols = window.allColumns || [];
            allConfigs.push({
                cond:     "cross",
                colIdx:   parseInt(col1Str),
                colName:  cols[parseInt(col1Str)]?.col1 || "",
                col2Idx:  crossCol !== "" ? parseInt(crossCol) : "",
            });
            return;
        }

        // ── STANDARD (eq, neq, gt, lt, gte, lte, btwn, nbtwn, empty, dup) ───
        const cols = window.allColumns || [];
        const colIdxInt = parseInt(col1Str);
        const comboInput = document.querySelector(`#tableBox_${id} .col-combo-input`);
        const colName = cols[colIdxInt]?.col1 || comboInput?.value || "";
        allConfigs.push({
            colIdx:   colIdxInt,
            colName,
            cond:     cond1,
            matchVal: document.getElementById(`matchInput_${id}`)?.value || "",
        });
    });

    window.allConfigs = allConfigs;
}

window.loadGeneratedFilters = loadGeneratedFilters;

// ── 4. loadSheetFiltersIntoConfigs (legacy compatibility) ─────────────────────

async function loadSheetFiltersIntoConfigs() {
    const activeId = window.selectedSheetId;
    if (!activeId) return;

    let sheet = (window.SHEET_LIST || []).find(s => s.id === activeId);
    if (!sheet || !Array.isArray(sheet.filters)) {
        try {
            const res    = await fetch(API_BASE + "/get_sheets");
            const sheets = await res.json();
            sheet = sheets.find(s => s.id === activeId);
        } catch (e) { return; }
    }
    if (!sheet || !Array.isArray(sheet.filters) || sheet.filters.length === 0) return;

    const dataset2Missing = !window.allColumns2 || window.allColumns2.length === 0;

    sheet.filters.forEach(f => {
        if ((f.cond === "cross" || f.cond === "doublecross") && dataset2Missing) return;
        if (f.cond === "coords") {
            allConfigs.push({
                ...f,
                lngColIdx:    f.lngColIdx    != null && f.lngColIdx    !== "" ? parseInt(f.lngColIdx)    : "",
                latColIdx:    f.latColIdx    != null && f.latColIdx    !== "" ? parseInt(f.latColIdx)    : "",
                verifyColIdx: f.verifyColIdx != null && f.verifyColIdx !== "" ? parseInt(f.verifyColIdx) : "",
            });
            return;
        }
        allConfigs.push({
            ...f,
            colIdx:  f.colIdx  != null && f.colIdx  !== "" ? parseInt(f.colIdx)  : "",
            col2Idx: f.col2Idx != null && f.col2Idx !== "" ? parseInt(f.col2Idx) : "",
        });
    });
}

// ── 5. evaluateRow  ───────────────────────────────────────────────────────────
// Pure function. Takes one filter config and one row (as an array of cell
// values aligned to the `columns` header array), returns:
//   { pass: bool, label: string, actual: string, expected: string }
//
// `columns`   – array of column name strings (from /api/dataset response)
// `rowValues` – array of raw cell-value strings in the same order
// `config`    – one element of allConfigs
// `allRowValues` – full 2-D array of all rows (needed for duplicate checks)
// `d2Pools`   – Map<colIdx, Set<normalizedString>> for cross-check

function evaluateRow(columns, rowValues, config, allRowValues, d2Pools) {
    const colIdx = config.colIdx;

    // ── helpers local to this call ──────────────────────────────────────────
    function cellVal(idx) {
        return idx != null && idx !== "" ? String(rowValues[idx] ?? "") : "";
    }
    function cellNorm(idx) { return normalize(cellVal(idx)); }
    function cellNum(idx)  { return parseFloat(cellVal(idx)); }
    function isEmpty(idx) {
        const v = cellVal(idx);
        const n = normalize(v);
        return n === "";
    }

    const label    = _filterLabel(config);
    const expected = _filterExpected(config, columns);

    // ── EMPTY ────────────────────────────────────────────────────────────────
    if (config.cond === "empty") {
        const flagged = isEmpty(colIdx);
        return { pass: !flagged, label, actual: cellVal(colIdx), expected };
    }

    // ── DUPLICATE ────────────────────────────────────────────────────────────
    if (config.cond === "dup") {
        const v = cellNorm(colIdx);
        if (v === "") return { pass: true, label, actual: cellVal(colIdx), expected };
        const count = allRowValues.filter(r => normalize(String(r[colIdx] ?? "")) === v).length;
        return { pass: count <= 1, label, actual: cellVal(colIdx), expected: "unique" };
    }

    // ── NUMERIC VALUE CONDITIONS ─────────────────────────────────────────────
    if (["eq","neq","gt","lt","gte","lte"].includes(config.cond)) {
        const v    = cellNorm(colIdx);
        const num  = cellNum(colIdx);
        const thr  = String(config.matchVal || "").trim().toLowerCase();
        const thrN = parseFloat(config.matchVal || "");
        let pass;
        switch (config.cond) {
            case "eq":  pass = v === thr; break;
            case "neq": pass = v !== thr; break;
            case "gt":  pass = !isNaN(num) && !isNaN(thrN) && num > thrN; break;
            case "lt":  pass = !isNaN(num) && !isNaN(thrN) && num < thrN; break;
            case "gte": pass = !isNaN(num) && !isNaN(thrN) && num >= thrN; break;
            case "lte": pass = !isNaN(num) && !isNaN(thrN) && num <= thrN; break;
            default:    pass = true;
        }
        return { pass, label, actual: cellVal(colIdx), expected: config.matchVal || "" };
    }

    // ── BETWEEN / NOT BETWEEN ────────────────────────────────────────────────
    if (config.cond === "btwn" || config.cond === "nbtwn") {
        const num = cellNum(colIdx);
        if (!config.matchVal || !config.matchVal.includes(","))
            return { pass: true, label, actual: cellVal(colIdx), expected: "min,max" };
        const [lo, hi] = config.matchVal.split(",").map(Number);
        const inRange  = !isNaN(num) && num >= lo && num <= hi;
        const pass     = config.cond === "btwn" ? inRange : !inRange;
        return { pass, label, actual: cellVal(colIdx), expected: `${lo} – ${hi}` };
    }

    // ── CROSS CHECK ──────────────────────────────────────────────────────────
    if (config.cond === "cross") {
        const v    = cellNorm(colIdx);
        const pool = d2Pools && d2Pools.get(config.col2Idx);
        if (!pool) return { pass: true, label, actual: cellVal(colIdx), expected: "in Dataset 2" };
        // Empty cells are not flagged by cross-check
        if (v === "") return { pass: true, label, actual: "", expected: "in Dataset 2" };
        return { pass: pool.has(v), label, actual: cellVal(colIdx), expected: "in Dataset 2" };
    }

    // ── DOUBLE CROSS ─────────────────────────────────────────────────────────
    if (config.cond === "doublecross") {
        // d2Pools carries a special "_pairSet" key for doublecross
        const pairSet = d2Pools && d2Pools.get("_pairSet_" + config.colIdx);
        if (!pairSet) return { pass: true, label, actual: "", expected: "pair in Dataset 2" };
        const v1   = cellNorm(colIdx);
        const v2   = cellNorm(config.colExtraIdx);
        const pass = pairSet.has(v1 + "|" + v2);
        return { pass, label, actual: `${cellVal(colIdx)} | ${cellVal(config.colExtraIdx)}`, expected: "matching pair in Dataset 2" };
    }

    // ── AND / OR ─────────────────────────────────────────────────────────────
    if (config.cond === "and" || config.cond === "or") {
        const subResults = (config.subfilters || []).map(sf => {
            return evaluateRow(columns, rowValues, sf, allRowValues, d2Pools);
        });
        const allPass  = subResults.every(r => r.pass);
        const anyPass  = subResults.some(r => r.pass);
        const pass     = config.cond === "and" ? allPass : anyPass;
        const failed   = subResults.filter(r => !r.pass).map(r => r.label).join(", ");
        return {
            pass,
            label,
            actual:   subResults.map((r, i) => `${(config.subfilters[i].colIdx != null ? columns[config.subfilters[i].colIdx] || "" : "")}=${r.actual}`).join(" | "),
            expected: config.cond === "and" ? "all pass" : "any pass",
            subResults,
        };
    }

    // ── COMPARE ──────────────────────────────────────────────────────────────
    if (config.cond === "compare") {
        return _evaluateCompare(config, columns, rowValues);
    }

    // ── COMPARE WITH DATASET 2 ──────────────────────────────────────────────
    // Row-wise comparison against Dataset 2 needs the same positional
    // alignment the backend does (row i of D1 vs row i of D2) — deferred to
    // the server, same as "coords" below, rather than duplicating that
    // alignment logic here for an approximate instant-count preview.
    if (config.cond === "compare2") {
        return { pass: true, label, actual: "", expected: "resolved by server" };
    }

    // ── DATE ─────────────────────────────────────────────────────────────────
    if (config.cond === "date") {
        return _evaluateDate(config, colIdx, cellVal(colIdx));
    }

    // ── COORDS ───────────────────────────────────────────────────────────────
    if (config.cond === "coords") {
        // Client-side coord resolution is expensive; the server handles it.
        // Return pass=true so we don't double-flag in the client preview.
        return { pass: true, label, actual: "", expected: "resolved by server" };
    }

    return { pass: true, label, actual: cellVal(colIdx), expected: "" };
}

// ── Date evaluation helper ────────────────────────────────────────────────────
function _evaluateDate(config, colIdx, rawVal) {
    const label    = _filterLabel(config);
    const dateCond = config.dateCond || "";
    const val1     = config.dateVal1 || "";
    const val2     = config.dateVal2 || "";
    const today    = _dateOnly(new Date());

    const isEmpty  = normalize(rawVal) === "";
    const parsed   = isEmpty ? null : _parseDate(rawVal);
    const isValid  = !isEmpty && parsed !== null;
    const cellDate = isValid ? _dateOnly(parsed) : null;
    const ref1     = val1 ? _dateOnly(_parseDate(val1) || new Date(val1)) : null;
    const ref2     = val2 ? _dateOnly(_parseDate(val2) || new Date(val2)) : null;
    const refNum   = val1 ? parseInt(val1, 10) : NaN;

    let flagged = false;

    switch (dateCond) {
        case "date_empty":   flagged = isEmpty; break;
        case "date_invalid": flagged = !isEmpty && !isValid; break;
        case "date_future":  flagged = isValid && cellDate > today; break;
        case "date_past":    flagged = isValid && cellDate < today; break;
        case "date_eq":      flagged = !(isValid && ref1 && cellDate.getTime() === ref1.getTime()); break;
        case "date_neq":     flagged = !(isValid && ref1 && cellDate.getTime() !== ref1.getTime()); break;
        case "date_before":  flagged = !(isValid && ref1 && cellDate < ref1); break;
        case "date_after":   flagged = !(isValid && ref1 && cellDate > ref1); break;
        case "date_before_eq": flagged = !(isValid && ref1 && cellDate <= ref1); break;
        case "date_after_eq":  flagged = !(isValid && ref1 && cellDate >= ref1); break;
        case "date_btwn":    flagged = !(isValid && ref1 && ref2 && cellDate >= ref1 && cellDate <= ref2); break;
        case "date_nbtwn":   flagged = !(isValid && ref1 && ref2 && (cellDate < ref1 || cellDate > ref2)); break;
        case "date_year_eq": flagged = !(isValid && !isNaN(refNum) && cellDate.getFullYear() === refNum); break;
        case "date_year_gt": flagged = !(isValid && !isNaN(refNum) && cellDate.getFullYear() > refNum); break;
        case "date_year_lt": flagged = !(isValid && !isNaN(refNum) && cellDate.getFullYear() < refNum); break;
        case "date_month_eq":flagged = !(isValid && !isNaN(refNum) && (cellDate.getMonth() + 1) === refNum); break;
        case "date_day_eq":  flagged = !(isValid && !isNaN(refNum) && cellDate.getDate() === refNum); break;
        case "date_weekday": {
            const jsDay  = cellDate ? cellDate.getDay() : -1;
            const isoDay = jsDay === 0 ? 7 : jsDay;
            flagged = !(isValid && !isNaN(refNum) && isoDay === refNum);
            break;
        }
        default: flagged = false;
    }

    const rangeStr = val2 ? `${val1} – ${val2}` : val1;
    return {
        pass:     !flagged,
        label,
        actual:   rawVal || "[empty]",
        expected: `${condLabel(dateCond)}${rangeStr ? " " + rangeStr : ""}`,
    };
}

// ── Compare evaluation helper ─────────────────────────────────────────────────
function _evaluateCompare(config, columns, rowValues) {
    const label = _filterLabel(config);

    function v(idx) { return idx != null && idx !== "" ? String(rowValues[idx] ?? "") : ""; }
    function n(idx) { return parseFloat(v(idx)); }
    function s(idx) { return normalize(v(idx)); }
    function empty(idx) { return normalize(v(idx)) === ""; }

    // AND / OR over column-pair sub-filters
    if (config.subCond === "and" || config.subCond === "or") {
        const subs = (config.compareSubfilters || []).map(sf => {
            const sA = s(sf.colAIdx), sB = s(sf.colBIdx);
            const nA = n(sf.colAIdx), nB = n(sf.colBIdx);
            let pass;
            switch (sf.cond) {
                case "eq":  pass = sA === sB; break;
                case "neq": pass = sA !== sB; break;
                case "gt":  pass = !isNaN(nA) && !isNaN(nB) && nA > nB; break;
                case "lt":  pass = !isNaN(nA) && !isNaN(nB) && nA < nB; break;
                case "gte": pass = !isNaN(nA) && !isNaN(nB) && nA >= nB; break;
                case "lte": pass = !isNaN(nA) && !isNaN(nB) && nA <= nB; break;
                default:    pass = true;
            }
            return { pass, colA: sf.colAIdx, colB: sf.colBIdx, cond: sf.cond };
        });
        const pass = config.subCond === "and"
            ? subs.every(r => r.pass)
            : subs.some(r => r.pass);
        const actual = subs.map(r =>
            `${columns[r.colA] || r.colA}=${v(r.colA)} vs ${columns[r.colB] || r.colB}=${v(r.colB)}`
        ).join(" | ");
        return { pass, label, actual, expected: config.subCond === "and" ? "all pairs match" : "any pair matches" };
    }

    // Date column-vs-column compare
    if (config.subCond === "date") {
        const rawA   = v(config.colIdx);
        const rawB   = v(config.col2Idx);
        const dA     = _parseDate(rawA);
        const dB     = _parseDate(rawB);
        const dc     = config.dateCond || "";
        const emptyA = normalize(rawA) === "";
        const emptyB = normalize(rawB) === "";
        let issue    = false;

        if (dc === "date_empty")   { issue = emptyA !== emptyB; }
        else if (dc === "date_invalid") { issue = (!emptyA && !dA) !== (!emptyB && !dB); }
        else if (!dA || !dB)       { issue = true; }
        else {
            const a = dA.getTime(), b = dB.getTime();
            switch (dc) {
                case "date_eq":        issue = a !== b; break;
                case "date_neq":       issue = a === b; break;
                case "date_before":    issue = !(a < b); break;
                case "date_after":     issue = !(a > b); break;
                case "date_before_eq": issue = !(a <= b); break;
                case "date_after_eq":  issue = !(a >= b); break;
                case "date_year_eq":   issue = dA.getFullYear() !== dB.getFullYear(); break;
                case "date_month_eq":  issue = (dA.getMonth() + 1) !== (dB.getMonth() + 1); break;
                case "date_day_eq":    issue = dA.getDate() !== dB.getDate(); break;
                case "date_weekday":   issue = dA.getDay() !== dB.getDay(); break;
                default:               issue = false;
            }
        }
        return {
            pass:     !issue,
            label,
            actual:   `${rawA || "[empty]"} vs ${rawB || "[empty]"}`,
            expected: `${condLabel(dc)} (col-vs-col)`,
        };
    }

    // Simple sub-condition column-vs-column
    const sA    = s(config.colIdx), sB = s(config.col2Idx);
    const nA    = n(config.colIdx), nB = n(config.col2Idx);
    const eA    = empty(config.colIdx), eB = empty(config.col2Idx);
    let pass;
    switch (config.subCond) {
        case "eq":    pass = sA === sB; break;
        case "neq":   pass = sA !== sB; break;
        case "gt":    pass = !isNaN(nA) && !isNaN(nB) && nA > nB; break;
        case "lt":    pass = !isNaN(nA) && !isNaN(nB) && nA < nB; break;
        case "gte":   pass = !isNaN(nA) && !isNaN(nB) && nA >= nB; break;
        case "lte":   pass = !isNaN(nA) && !isNaN(nB) && nA <= nB; break;
        case "btwn":  pass = !isNaN(nA) && !isNaN(nB) && nA >= nB && nA <= nB; break;  // nA in range [nB, nB] — ambiguous for col-vs-col; kept for symmetry
        case "nbtwn": pass = !isNaN(nA) && !isNaN(nB) && (nA < nB || nA > nB); break;
        case "empty": pass = eA === eB; break;
        case "dup": {
            // Flag if BOTH are equal non-empty (same value in two cols = potential duplicate entry)
            pass = !(sA !== "" && sA === sB);
            break;
        }
        default: pass = true;
    }
    return {
        pass,
        label,
        actual:   `${v(config.colIdx) || "[empty]"} vs ${v(config.col2Idx) || "[empty]"}`,
        expected: `${condLabel(config.subCond)} (col-vs-col)`,
    };
}

// ── Label / expected helpers ──────────────────────────────────────────────────
function _filterLabel(config) {
    const cols  = Array.isArray(window.allColumns)  ? window.allColumns  : [];
    const cols2 = Array.isArray(window.allColumns2) ? window.allColumns2 : [];

    if (config.cond === "date") {
        const colName = cols[config.colIdx]?.col1 || `Col ${config.colIdx}`;
        const dcLabel = condLabel(config.dateCond || "");
        const range   = config.dateVal2
            ? `${config.dateVal1} – ${config.dateVal2}`
            : (config.dateVal1 || "");
        return `${colName}: ${dcLabel}${range ? " " + range : ""}`;
    }
    if (config.cond === "compare" || config.cond === "compare2") {
        const isD2 = config.cond === "compare2";
        const c2list = isD2 ? cols2 : cols;
        const c1 = cols[config.colIdx]?.col1  || `Col ${config.colIdx}`;
        const c2 = c2list[config.col2Idx]?.col1 || `Col ${config.col2Idx}`;
        const sc = config.subCond === "date"
            ? condLabel(config.dateCond || "")
            : condLabel(config.subCond || "");
        return `${isD2 ? "Compare (D2)" : "Compare"}: ${c1} ${sc} ${c2}`;
    }
    if (config.cond === "and" || config.cond === "or") {
        const parts = (config.subfilters || []).map(sf =>
            cols[sf.colIdx]?.col1 || `Col ${sf.colIdx}`
        );
        return `${condLabel(config.cond)}: ${parts.join(", ")}`;
    }
    if (config.cond === "coords") {
        return `Coord Check (${config.lngColName || "Lng"} / ${config.latColName || "Lat"} → ${config.coordLevel || "?"})`;
    }
    if (config.cond === "cross" || config.cond === "doublecross") {
        const colName = cols[config.colIdx]?.col1 || `Col ${config.colIdx}`;
        return `${condLabel(config.cond)}: ${colName}`;
    }
    const colName = cols[config.colIdx]?.col1 || (config.colName) || `Col ${config.colIdx}`;
    return `${colName}: ${condLabel(config.cond)}`;
}

function _filterExpected(config, columns) {
    if (["eq","neq"].includes(config.cond))              return config.matchVal || "";
    if (["gt","lt","gte","lte"].includes(config.cond))   return `${condLabel(config.cond)} ${config.matchVal || "?"}`;
    if (["btwn","nbtwn"].includes(config.cond))          return config.matchVal || "min,max";
    if (config.cond === "empty")                          return "not empty";
    if (config.cond === "dup")                            return "unique";
    if (config.cond === "cross")                          return "value in Dataset 2";
    if (config.cond === "doublecross")                    return "pair in Dataset 2";
    if (config.cond === "date")                           return `${condLabel(config.dateCond || "")} ${config.dateVal1 || ""}${config.dateVal2 ? " – " + config.dateVal2 : ""}`.trim();
    if (config.cond === "compare")                        return `${condLabel(config.subCond || "")} (col-vs-col)`;
    if (config.cond === "compare2")                       return `${condLabel(config.subCond || "")} (D1 vs D2, by row)`;
    if (config.cond === "coords")                         return `within ${config.coordLevel || "boundary"}`;
    if (config.cond === "and")                            return "all sub-conditions pass";
    if (config.cond === "or")                             return "any sub-condition passes";
    return "";
}

// ── 6. runClientValidation ────────────────────────────────────────────────────
// Called by the Validate view in main.js after loading dataset rows from
// /api/dataset.  Takes the same { columns, rows } shape that renderCleanTable()
// already receives, runs all active allConfigs against every row client-side,
// and returns an object the Validate view can use to render a highlighted table
// identical to the Clean section.
//
// Return shape:
//   {
//     columns: string[],
//     rows: Array<Array<{ value, flags, filterLabel?, actual?, expected? }>>,
//     total_rows: number,
//     filter_results: Array<{ label, cond, flagged_count }>,
//     passed: number,
//     failed: number,
//   }
//
// `datasetPage` = { columns, rows } from /api/dataset — rows is an array of
// cell-object arrays: [{value, flags[]}, ...]

window.runClientValidation = function(datasetPage) {
    if (typeof syncColumns === "function") syncColumns();
    loadGeneratedFilters();
    const configs = window.allConfigs || [];

    const { columns, rows, total_rows } = datasetPage;
    if (!columns || !rows) return null;

    // Build plain row-value arrays (string[][]) for evaluateRow()
    const rawRows = rows.map(cells => cells.map(c => c.value));

    // Build Dataset-2 pools for cross / doublecross filters
    const d2Pools = _buildD2Pools(configs);

    // Per-row result: each entry = { pass, failedFilters: [{label, actual, expected}] }
    const rowResults = rawRows.map(rv => ({ pass: true, failedFilters: [] }));

    // Per-filter summary
    const filterSummaries = configs.map(cfg => ({
        label:         _filterLabel(cfg),
        cond:          cfg.cond,
        flagged_count: 0,
    }));

    // Run every filter over every row
    configs.forEach((cfg, fi) => {
        rawRows.forEach((rv, ri) => {
            const result = evaluateRow(columns, rv, cfg, rawRows, d2Pools);
            if (!result.pass) {
                rowResults[ri].pass = false;
                rowResults[ri].failedFilters.push({
                    label:    result.label,
                    actual:   result.actual,
                    expected: result.expected,
                });
                filterSummaries[fi].flagged_count++;
            }
        });
    });

    // Rebuild cell arrays with added val-fail / val-pass flags
    const outRows = rows.map((cells, ri) => {
        const rr = rowResults[ri];
        return cells.map((cell, ci) => {
            const flags = [...(cell.flags || [])];
            if (configs.length > 0) {
                // Check if any failing filter references this column
                const colName     = columns[ci];
                const cellFailed  = rr.failedFilters.some(ff => {
                    // Heuristic: label contains column name
                    return ff.label && ff.label.includes(colName);
                });
                // Row-level flag
                if (!rr.pass && !flags.includes("flag-val-fail")) flags.push("flag-val-fail");
                // Cell-level refinement for exact column matches
                if (!rr.pass && cellFailed && !flags.includes("flag-val-cell-fail")) {
                    flags.push("flag-val-cell-fail");
                }
            }
            return {
                value:        cell.value,
                flags,
                // Attach filter info to the first cell so the row tooltip can show it
                ...(ci === 0 && !rr.pass ? {
                    _filterDetails: rr.failedFilters,
                } : {}),
            };
        });
    });

    const passed = rowResults.filter(r => r.pass).length;
    const failed = rowResults.length - passed;

    return {
        columns,
        rows:            outRows,
        total_rows:      total_rows || rows.length,
        filter_results:  filterSummaries,
        passed,
        failed,
        // Full per-row fail detail (matches the shape of /api/validation_results)
        failures: rowResults
            .map((r, i) => r.pass ? null : {
                row:            i + 2,
                filters_failed: r.failedFilters.map(f => f.label),
                filter_details: r.failedFilters.map(f => ({
                    label:    f.label,
                    pass:     false,
                    actual:   f.actual,
                    expected: f.expected,
                })),
                status: "FAIL",
            })
            .filter(Boolean),
    };
};

// Build Dataset-2 lookup pools from allColumns2 (for cross / doublecross)
function _buildD2Pools(configs) {
    const pools = new Map();
    if (!window.allColumns2 || window.allColumns2.length === 0) return pools;

    const crossConfigs = configs.filter(c => c.cond === "cross" || c.cond === "doublecross");
    if (crossConfigs.length === 0) return pools;

    window.allColumns2.forEach((col, idx) => {
        const colVals = new Set(
            (col.values || []).map(v => normalize(String(v ?? "")))
        );
        pools.set(idx, colVals);
    });

    // doublecross: precompute pair sets keyed by "_pairSet_<col1Idx>"
    crossConfigs.filter(c => c.cond === "doublecross").forEach(cfg => {
        const colA = window.allColumns2[cfg.col2Idx];
        const colB = window.allColumns2[cfg.colExtraIdx2];
        if (!colA || !colB) return;
        const pairSet = new Set();
        const len = Math.min(colA.values.length, colB.values.length);
        for (let k = 0; k < len; k++) {
            pairSet.add(normalize(String(colA.values[k] ?? "")) + "|" + normalize(String(colB.values[k] ?? "")));
        }
        pools.set("_pairSet_" + cfg.colIdx, pairSet);
    });

    return pools;
}

// ── 7. runValidation (legacy – Data-Validation-Utility standalone flow) ────────
// Kept 100% intact from the original. Writes HTML into #resultsArea.
// Uses window.allColumns (with .values populated) rather than the API dataset.

async function runValidation() {
    allConfigs = [];
    window.allConfigs = allConfigs;
    loadGeneratedFilters();

    const rangeErrors = Array.from(
        document.querySelectorAll('[id^="rangeMsg_"]')
    ).some(div => div.innerText.trim() !== "");
    if (rangeErrors) { alert("Fix range errors first"); return; }

    const area = document.getElementById("resultsArea");
    if (!area) return;
    area.innerHTML = "";
    if (typeof showExportBar === "function") showExportBar();

    // ── Dataset-2 pools ────────────────────────────────────────────────────
    let d2ColumnPools = {};
    if (window.allColumns2) {
        window.allColumns2.forEach((col, colIdx) => {
            d2ColumnPools[colIdx] = new Set(
                (col.values || []).map(v => {
                    let s = String(v ?? "").trim().toLowerCase();
                    if (s === "nan" || s === "null") s = "";
                    if (s.endsWith(".0")) s = s.slice(0, -2);
                    return s;
                })
            );
        });
    }

    // ── Inner condition evaluator (scalar, for single-row use) ─────────────
    function evaluateCondition(cond, val, num, isEmpty, valClean, config) {
        switch (cond) {
            case "empty": return isEmpty;
            case "cross": {
                const pool = d2ColumnPools[config.col2Idx];
                if (!pool) return false;
                const vn = normalize(val);
                return vn !== "" && !pool.has(vn);
            }
            case "eq":   return valClean === String(config.matchVal || "").trim().toLowerCase();
            case "neq":  return valClean !== String(config.matchVal || "").trim().toLowerCase();
            case "gt":   return !isNaN(num) && num >  parseFloat(config.matchVal || 0);
            case "lt":   return !isNaN(num) && num <  parseFloat(config.matchVal || 0);
            case "gte":  return !isNaN(num) && num >= parseFloat(config.matchVal || 0);
            case "lte":  return !isNaN(num) && num <= parseFloat(config.matchVal || 0);
            case "btwn": {
                if (!config.matchVal?.includes(",")) return false;
                const [lo, hi] = config.matchVal.split(",").map(Number);
                return !isNaN(num) && num >= lo && num <= hi;
            }
            case "nbtwn": {
                if (!config.matchVal?.includes(",")) return false;
                const [lo, hi] = config.matchVal.split(",").map(Number);
                return !isNaN(num) && (num < lo || num > hi);
            }
            default: return false;
        }
    }

    // ── renderResults ──────────────────────────────────────────────────────
    function renderResults(results, sheetName, title, headers, getRowDataFn) {
        const rowHtml = results.length
            ? results.map(r => `<tr>${getRowDataFn(r).map(v => `<td>${v}</td>`).join("")}</tr>`).join("")
            : `<tr><td colspan="${headers.length}">No matching issues</td></tr>`;

        area.innerHTML += `
            <div class="result-card">
                <div class="result-header"><h3>${title}</h3></div>
                <div class="scroll-box">
                    <table>
                        <thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>
                        <tbody>${rowHtml}</tbody>
                    </table>
                </div>
                <div style="margin-top:8px;font-weight:bold">Total Records Found: ${results.length}</div>
                <hr>
            </div>`;
    }

    // ── Process each config ────────────────────────────────────────────────
    allConfigs.forEach(config => {

        // ── COORDINATE CHECK ──────────────────────────────────────────────
        if (config.cond === "coords") {
            const lngData    = (window.allColumns || [])[config.lngColIdx];
            const latData    = (window.allColumns || [])[config.latColIdx];
            const verifyData = (window.allColumns || [])[config.verifyColIdx];
            if (!lngData || !latData || !verifyData) return;

            const geoFeatures = window._coordsGeoFeatures;
            if (!geoFeatures || geoFeatures.length === 0) {
                renderResults([], config.sheetName,
                    "Coordinate Check — GeoJSON not loaded",
                    ["Note"], () => ["GeoJSON boundary data not loaded. Refresh the page."]);
                return;
            }

            const levelKey   = { district: "ds", tehsil: "th", uc: "uc" }[config.coordLevel] || "ds";
            const levelLabel = { district: "District", tehsil: "Tehsil", uc: "Union Council" }[config.coordLevel] || config.coordLevel;

            function pointInPolygon(lng, lat, ring) {
                let inside = false;
                for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
                    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
                    if (((yi > lat) !== (yj > lat)) && (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi))
                        inside = !inside;
                }
                return inside;
            }

            function minDistSq(lng, lat, ring) {
                let best = Infinity;
                for (const c of ring) {
                    const d = (c[0]-lng)*(c[0]-lng) + (c[1]-lat)*(c[1]-lat);
                    if (d < best) best = d;
                }
                return best;
            }

            // Build spatial index
            const spatialIdx = {};
            for (const feature of geoFeatures) {
                const geom = feature.geometry; if (!geom) continue;
                const rings = geom.type === "Polygon"
                    ? [geom.coordinates[0]]
                    : geom.type === "MultiPolygon" ? geom.coordinates.map(p => p[0]) : [];
                for (const ring of rings) {
                    let mnLng=Infinity, mxLng=-Infinity, mnLat=Infinity, mxLat=-Infinity;
                    for (const c of ring) {
                        if (c[0]<mnLng)mnLng=c[0]; if (c[0]>mxLng)mxLng=c[0];
                        if (c[1]<mnLat)mnLat=c[1]; if (c[1]>mxLat)mxLat=c[1];
                    }
                    for (let gx=Math.floor(mnLng); gx<=Math.floor(mxLng); gx++) {
                        for (let gy=Math.floor(mnLat); gy<=Math.floor(mxLat); gy++) {
                            const key = gx+","+gy;
                            if (!spatialIdx[key]) spatialIdx[key] = [];
                            spatialIdx[key].push({ feature, ring });
                        }
                    }
                }
            }

            function resolveAdminName(lng, lat) {
                if (isNaN(lng) || isNaN(lat)) return { name: null, exact: false };
                const gx = Math.floor(lng), gy = Math.floor(lat);
                const candidates = new Set();
                for (let dx=-1; dx<=1; dx++)
                    for (let dy=-1; dy<=1; dy++) {
                        const b = spatialIdx[(gx+dx)+","+(gy+dy)];
                        if (b) b.forEach(e => candidates.add(e));
                    }
                for (const { feature, ring } of candidates)
                    if (pointInPolygon(lng, lat, ring))
                        return { name: (feature.properties[levelKey] || "").toString().trim(), exact: true };
                let bestD = 0.25, bestName = null;
                for (const { feature, ring } of candidates) {
                    const d = minDistSq(lng, lat, ring);
                    if (d < bestD) { bestD = d; bestName = (feature.properties[levelKey] || "").toString().trim(); }
                }
                return bestName ? { name: bestName, exact: false } : { name: null, exact: false };
            }

            const hru = lngData.hru_ids || Array(lngData.values.length).fill("N/A");
            const results = [];
            for (let i = 0; i < lngData.values.length; i++) {
                const lngVal    = parseFloat(lngData.values[i]);
                const latVal    = parseFloat(latData.values[i]);
                const verifyRaw = String(verifyData.values[i] ?? "").trim();
                const { name: resolved, exact } = resolveAdminName(lngVal, latVal);
                const normRes = (resolved || "").toLowerCase().trim();
                const normVer = verifyRaw.toLowerCase().trim();
                let mismatch = false, note = "";
                if (isNaN(lngVal) || isNaN(latVal))    { mismatch = true; note = "Invalid coordinates"; }
                else if (resolved === null)             { mismatch = true; note = "No nearby boundary found"; }
                else if (normRes !== normVer)           { mismatch = true; note = exact ? "" : "⚠ Nearest (outside polygon)"; }
                if (mismatch) results.push({ row: i+2, hru: hru[i], lng: lngData.values[i], lat: latData.values[i], resolved: resolved||"(no match)", exact, verify: verifyRaw, note });
            }
            const title = `${config.sheetName || "Filter"} → Coordinate Check (${lngData.col1} / ${latData.col1} → ${levelLabel} vs ${verifyData.col1})`;
            renderResults(results, config.sheetName, title,
                ["Row #", lngData.col1+" (Lng)", latData.col1+" (Lat)", `Resolved ${levelLabel}`, `${verifyData.col1} (Expected)`, "Note"],
                r => [r.row, r.lng, r.lat,
                    r.resolved !== "(no match)" ? `<span style="color:#1d4ed8;font-weight:600">${r.resolved}${r.exact?"":"  ⚠"}</span>` : `<span style="color:#9ca3af">${r.resolved}</span>`,
                    `<span style="color:#b91c1c;font-weight:600">${r.verify||"[Empty]"}</span>`,
                    r.note ? `<span style="color:#92400e;font-size:.85em">${r.note}</span>` : ""]);
            return;
        }

        // ── DOUBLE CROSS ──────────────────────────────────────────────────
        if (config.cond === "doublecross") {
            const colData   = (window.allColumns||[])[config.colIdx];
            const extraData = (window.allColumns||[])[config.colExtraIdx];
            if (!colData || !extraData) return;
            const d2Pairs    = new Set();
            const d2Col1Vals = new Set();
            if (window.allColumns2) {
                const cA = window.allColumns2[parseInt(config.col2Idx)];
                const cB = window.allColumns2[parseInt(config.colExtraIdx2)];
                if (cA && cB) {
                    for (let k = 0; k < cA.values.length; k++) {
                        const p1 = normalize(cA.values[k]), p2 = normalize(cB.values[k]);
                        d2Pairs.add(p1+"|"+p2); d2Col1Vals.add(p1);
                    }
                }
            }
            const hru = colData.hru_ids || Array(colData.values.length).fill("N/A");
            const results = [];
            for (let i = 0; i < colData.values.length; i++) {
                const v1 = normalize(colData.values[i]), v2 = normalize(extraData.values[i]);
                if (!d2Pairs.has(v1+"|"+v2))
                    results.push({ row:i+2, hru:hru[i], val1:colData.values[i]||"[Empty]", val2:extraData.values[i]||"[Empty]", col1Matched:d2Col1Vals.has(v1) });
            }
            const title = `${config.sheetName||"Filter"} → Double Cross (${colData.col1} ↔ ${extraData.col1})`;
            renderResults(results, config.sheetName, title,
                ["Excel Index", colData.col1, extraData.col1],
                r => [r.row,
                    r.col1Matched ? `<span style="color:#2e7d32;font-weight:600">${r.val1}</span>` : r.val1,
                    r.col1Matched ? `<span style="color:#e53935;font-weight:600">${r.val2}</span>` : r.val2]);
            return;
        }

        // ── AND / OR ──────────────────────────────────────────────────────
        if (config.cond === "and" || config.cond === "or") {
            if (!window.allColumns || window.allColumns.length === 0) { alert("Upload Dataset first!"); return; }
            const length  = Math.max(...(window.allColumns||[]).map(c => (c.values||[]).length));
            const results = [];
            for (let i = 0; i < length; i++) {
                const matches = (config.subfilters||[]).map(sf => {
                    const colDataSub = (window.allColumns||[])[sf.colIdx];
                    if (!colDataSub) return false;
                    const val = colDataSub.values[i];
                    return evaluateCondition(sf.cond, val, parseFloat(val),
                        val===""||val==null, normalize(val), sf);
                });
                const finalMatch = config.cond==="and" ? matches.every(Boolean) : matches.some(Boolean);
                if (finalMatch) {
                    const joined = (config.subfilters||[]).map(sf => (window.allColumns||[])[sf.colIdx]?.values[i]||"[Empty]").join(" | ");
                    const hruCol = (window.allColumns||[])[(config.subfilters||[])[0]?.colIdx];
                    results.push({ row:i+2, hru:hruCol?.hru_ids?.[i]||"N/A", val:joined });
                }
            }
            const colNames = (config.subfilters||[]).map(sf=>`(${(window.allColumns||[])[sf.colIdx]?.col1||"Col"})`).join(` ${condLabel(config.cond)} `);
            renderResults(results, config.sheetName, `${config.sheetName||"Filter"} → ${condLabel(config.cond)}: ${colNames}`,
                ["Excel Index","Values"], r=>[r.row, r.val]);
            return;
        }

        // ── COMPARE ───────────────────────────────────────────────────────
        if (config.cond === "compare") {
            const results  = [];
            const colData1 = config.colIdx!==""&&config.colIdx!=null ? (window.allColumns||[])[config.colIdx] : null;

            if (config.subCond==="and"||config.subCond==="or") {
                const subs = config.compareSubfilters||[];
                const len  = subs.length ? Math.max(...subs.map(sf=>(window.allColumns||[])[sf.colAIdx]?.values.length||0)) : 0;
                for (let i=0; i<len; i++) {
                    const rowM = subs.map(sf => {
                        const vA=normalize((window.allColumns||[])[sf.colAIdx]?.values[i]??""), vB=normalize((window.allColumns||[])[sf.colBIdx]?.values[i]??"");
                        const nA=parseFloat(vA), nB=parseFloat(vB);
                        switch(sf.cond){
                            case "eq": return vA===vB; case "neq": return vA!==vB;
                            case "gt": return !isNaN(nA)&&!isNaN(nB)&&nA>nB; case "lt": return !isNaN(nA)&&!isNaN(nB)&&nA<nB;
                            case "gte":return !isNaN(nA)&&!isNaN(nB)&&nA>=nB; case "lte":return !isNaN(nA)&&!isNaN(nB)&&nA<=nB;
                            default: return false;
                        }
                    });
                    const finalM = config.subCond==="and" ? rowM.every(Boolean) : rowM.some(Boolean);
                    if (!finalM) {
                        const vals = subs.map(sf=>`${(window.allColumns||[])[sf.colAIdx]?.col1||"ColA"}=${(window.allColumns||[])[sf.colAIdx]?.values[i]||"[Empty]"} vs ${(window.allColumns||[])[sf.colBIdx]?.col1||"ColB"}=${(window.allColumns||[])[sf.colBIdx]?.values[i]||"[Empty]"}`).join(" | ");
                        results.push({ row:i+2, hru:colData1?.hru_ids?.[i]||"N/A", val:vals });
                    }
                }
                renderResults(results, config.sheetName, `${config.sheetName||"Filter"} → Compare (${condLabel(config.subCond)})`,
                    ["Excel Index","Values"], r=>[r.row,r.val]);
                return;
            }

            if (config.subCond==="date") {
                const cD1=(window.allColumns||[])[config.colIdx], cD2=(window.allColumns||[])[config.col2Idx];
                if (!cD1||!cD2) return;
                const dc=config.dateCond||"";
                const isED=v=>v===""||v==null||String(v).trim().toLowerCase()==="nan";
                const isInvD=v=>!isED(v)&&!_parseDate(v);
                const len=Math.max(cD1.values.length,cD2.values.length);
                for (let i=0;i<len;i++){
                    const rA=cD1.values[i]??"", rB=cD2.values[i]??"";
                    const dA=_parseDate(rA), dB=_parseDate(rB);
                    const eA=isED(rA), eB=isED(rB), iA=isInvD(rA), iB=isInvD(rB);
                    let issue=false;
                    if (dc==="date_empty") issue=eA!==eB;
                    else if (dc==="date_invalid") issue=iA!==iB;
                    else if (!dA||!dB) issue=true;
                    else {
                        const a=dA.getTime(), b=dB.getTime();
                        switch(dc){
                            case "date_eq":        issue=a!==b; break; case "date_neq":issue=a===b; break;
                            case "date_before":    issue=!(a<b);break; case "date_after":issue=!(a>b);break;
                            case "date_before_eq": issue=!(a<=b);break; case "date_after_eq":issue=!(a>=b);break;
                            case "date_year_eq":   issue=dA.getFullYear()!==dB.getFullYear();break;
                            case "date_month_eq":  issue=(dA.getMonth()+1)!==(dB.getMonth()+1);break;
                            case "date_day_eq":    issue=dA.getDate()!==dB.getDate();break;
                            case "date_weekday":   issue=dA.getDay()!==dB.getDay();break;
                            default: issue=false;
                        }
                    }
                    if (issue) results.push({ row:i+2, hru:cD1.hru_ids?.[i]||"N/A", valA:rA||"[Empty]", valB:rB||"[Empty]" });
                }
                renderResults(results, config.sheetName, `${config.sheetName||"Filter"} → Compare Date: ${cD1.col1} ${condLabel(dc)} ${cD2.col1}`,
                    ["Excel Index",cD1.col1,cD2.col1], r=>[r.row,r.valA,r.valB]);
                return;
            }

            if (!colData1) return;
            const colData2 = (window.allColumns||[])[config.col2Idx];
            if (!colData2) return;
            for (let i=0; i<colData1.values.length; i++) {
                const vA=colData1.values[i]??"", vB=colData2.values[i]??"";
                const nA=parseFloat(vA), nB=parseFloat(vB);
                const sA=normalize(vA), sB=normalize(vB);
                const eA=vA===""||String(vA).toLowerCase()==="nan", eB=vB===""||String(vB).toLowerCase()==="nan";
                let match=false;
                switch(config.subCond){
                    case "eq":  match=sA!==sB;break; case "neq":match=sA===sB;break;
                    case "gt":  match=!(nA>nB);break; case "lt": match=!(nA<nB);break;
                    case "gte": match=!(nA>=nB);break;case "lte":match=!(nA<=nB);break;
                    case "btwn":  match=!(nA>=nB&&nA<=nB);break;
                    case "nbtwn": match=!(nA<nB||nA>nB);break;
                    case "empty": match=eA!==eB;break;
                    case "dup":   match=sA!==""&&sA===sB;break;
                    default: match=false;
                }
                if (match) results.push({ row:i+2, hru:colData1.hru_ids?.[i]||"N/A", val:`${vA||"[Empty]"} vs ${vB||"[Empty]"}` });
            }
            const c1n=colData1.col1||`Col ${config.colIdx}`, c2n=colData2.col1||`Col ${config.col2Idx}`;
            renderResults(results, config.sheetName, `${config.sheetName||"Filter"} → Compare: ${c1n} ${condLabel(config.subCond)} ${c2n}`,
                ["Excel Index",`${c1n} vs ${c2n}`], r=>[r.row,r.val]);
            return;
        }

        // ── DATE FILTER ───────────────────────────────────────────────────
        if (config.cond === "date") {
            const colData = (window.allColumns||[])[config.colIdx];
            if (!colData) return;
            const dateCond = config.dateCond||"", val1=config.dateVal1||"", val2=config.dateVal2||"";
            const today    = _dateOnly(new Date());
            const ref1     = val1 ? _dateOnly(_parseDate(val1)||new Date(val1)) : null;
            const ref2     = val2 ? _dateOnly(_parseDate(val2)||new Date(val2)) : null;
            const refNum   = val1 ? parseInt(val1,10) : NaN;
            const results  = [];
            (colData.values||[]).forEach((raw,i)=>{
                const r = _evaluateDate({ cond:"date", dateCond, dateVal1:val1, dateVal2:val2, colIdx:config.colIdx }, config.colIdx, String(raw??"")); 
                if (!r.pass) results.push({ row:i+2, val:raw||"[Empty]", sortKey:_parseDate(raw)?.getTime()||0 });
            });
            results.sort((a,b)=>a.sortKey-b.sortKey);
            const cName=(colData.col1||"").trim()||`Column ${config.colIdx}`;
            const range=val2?` ${val1} – ${val2}`:(val1?` ${val1}`:"");
            renderResults(results, config.sheetName,
                `${config.sheetName||"Filter"} → ${condLabel(dateCond)} (${cName})${range}`,
                ["Excel Index","Date Value"], r=>[r.row,r.val]);
            return;
        }

        // ── STANDARD (eq, neq, gt, lt, gte, lte, btwn, nbtwn, empty, dup, cross) ──
        const colData = (window.allColumns||[])[config.colIdx];
        if (!colData) return;
        const raw     = colData.values || [];
        const numeric = raw.some(v => !isNaN(parseFloat(v)) && v !== "");
        const freqMap = {};
        raw.forEach(v => { const s=normalize(v); if (s!=="") freqMap[s]=(freqMap[s]||0)+1; });
        const results = [];
        raw.forEach((val,i)=>{
            const num=parseFloat(val), isEmpty2=val===""||val==null||String(val).toLowerCase()==="nan";
            const valClean=normalize(val);
            let match=config.cond==="dup"
                ? !isEmpty2&&(freqMap[valClean]||0)>1
                : evaluateCondition(config.cond,val,num,isEmpty2,valClean,config);
            if (match) results.push({ row:i+2, val:isEmpty2?"[Empty]":val, sortKey:numeric?num:val });
        });
        results.sort((a,b)=>numeric?a.sortKey-b.sortKey:String(a.sortKey).localeCompare(String(b.sortKey)));
        const dName=(colData.col1||"").trim()||`Column ${config.colIdx}`;
        renderResults(results, config.sheetName,
            `${config.sheetName||"Filter"} → ${condLabel(config.cond)} (${dName})`,
            ["Excel Index","Values"], r=>[r.row,r.val]);
    });
}