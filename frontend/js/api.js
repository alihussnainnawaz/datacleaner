// api.js  —  Data Cleaning Tool
// Wired to: POST /api/upload/   POST /api/clean/{type}/{?ip}/{file_id}
//           GET  /api/clean/{type}/{?ip}/{stem}/download/cleaned

const API_BASE = "/api";  // relative — works on any host/port


// ══════════════════════════════════════════════════════════
// CORE FETCH WRAPPER
// ══════════════════════════════════════════════════════════

async function apiFetch(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`;
  try {
    const res = await fetch(url, {
      headers: { "Accept": "application/json", ...options.headers },
      ...options,
    });

    if (options._raw) return res;

    let data;
    try { data = await res.json(); }
    catch { throw new Error(`Server returned a non-JSON response (status ${res.status}).`); }

    if (!res.ok) {
      const message = data?.detail || data?.error || `Request failed (${res.status})`;
      throw new Error(message);
    }
    return data;

  } catch (err) {
    if (err.name === "TypeError" && err.message.includes("fetch")) {
      throw new Error("Cannot reach the server. Make sure the backend is running.");
    }
    throw err;
  }
}


// ══════════════════════════════════════════════════════════
// HEALTH
// ══════════════════════════════════════════════════════════

async function checkHealth() {
  const url = `/health`;
  try {
    const res  = await fetch(url, { headers: { "Accept": "application/json" } });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail || "Health check failed.");
    return data;
  } catch (err) {
    if (err.name === "TypeError" && err.message.includes("fetch")) {
      throw new Error("Cannot reach the server. Make sure the backend is running.");
    }
    throw err;
  }
}


// ══════════════════════════════════════════════════════════
// UPLOAD   POST /api/upload/
// ══════════════════════════════════════════════════════════

/**
 * Upload a CSV / XLSX / XLS file.
 * Returns: { success, file_id, file_name, row_count, column_count, columns, message }
 */
async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  return apiFetch("/upload/", { method: "POST", body: form });
}

/**
 * Fetch auto-detected datatypes for every column of an uploaded file.
 * Cheap: backend caches this at upload time, so this is normally an
 * instant read, not a fresh scan. Powers dragging the "Datatype" filter
 * onto "All Columns" in the Column Rule Preview screen.
 * Returns: { file_id, dtypes: { col: { type, confidence, sample } } }
 */
async function apiDetectDtypes(fileId) {
  return apiFetch(`/upload/${encodeURIComponent(fileId)}/dtypes`);
}


// ══════════════════════════════════════════════════════════
// CLEAN + VALIDATE   POST /api/clean/{type}/{?ip}/{file_id}
// Body: { filters: [...] }
// ══════════════════════════════════════════════════════════

/**
 * Run cleaning + validation pipeline.
 * filters: array of validation filter config objects (may be empty).
 */
// ══════════════════════════════════════════════════════════
// CLEAN + VALIDATE
// ══════════════════════════════════════════════════════════
async function cleanFileWithValidation(dataType, ipName, fileId, uuidColumn=null, filters=[], globalRules={}, columnRules={}, recoColumnRules=null, regexRules={}, dtypeRules={}, dataset2FileId=null) {
  const qp   = uuidColumn ? `?uuid_column=${encodeURIComponent(uuidColumn)}` : "";
  const path = ipName
    ? `/clean/${encodeURIComponent(dataType)}/${encodeURIComponent(ipName)}/${encodeURIComponent(fileId)}${qp}`
    : `/clean/${encodeURIComponent(dataType)}/${encodeURIComponent(fileId)}${qp}`;
  // regex_rules = state.regexRules ({col: {mapping:{...}} | {pattern,replacement,flags} | {auto:true}})
  // — "Regex Clean" rules, applied as the FIRST real step inside the
  // pipeline now, instead of a separate sequence of requests sent before
  // this one. Always included (defaults to {}) so the backend step simply
  // no-ops when there's nothing queued.
  // dtype_rules = state.dtypeRules ({col: expectedType}) — the "Datatype"
  // column rule from Column Rule Preview. Always included (defaults to {})
  // so the backend step no-ops when nothing was configured.
  const body = { filters, global_rules: globalRules, column_rules: columnRules, regex_rules: regexRules, dtype_rules: dtypeRules };
  // dataset2_file_id — only present when a Cross Check / Double Cross Check
  // filter actually needs it; omitted entirely (not even null) when unset,
  // so older/other callers see no behavior change.
  if (dataset2FileId) body.dataset2_file_id = dataset2FileId;
  // reco_column_rules = state.columnRules from the Column Rule Preview screen
  // ({col: [ruleKey,...]}) — gates which schema-driven cleaning steps the
  // backend actually runs. Only included when the caller passes it, so
  // older callers that don't know about this param keep working exactly
  // as before (backend treats "field absent" as "don't gate, old behaviour").
  if (recoColumnRules !== null) body.reco_column_rules = recoColumnRules;
  return apiFetch(path, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });
}

// Fetch a named backend file (report/duplicates/cleaned) and trigger a
// browser download of it via blob — used for the auto-download after a
// pipeline run completes, and by the manual download buttons.
async function downloadNamedFile(downloadPath, suggestedName) {
  const res = await fetch(downloadPath, { headers: { "Accept": "application/octet-stream" } });
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const blob   = await res.blob();
  const objUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objUrl; a.download = suggestedName;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(objUrl);
}

// ══════════════════════════════════════════════════════════
// PROJECTS
// ══════════════════════════════════════════════════════════
async function apiGetProjects()                       { return apiFetch("/projects"); }
async function apiCreateProject(name, description)    { return apiFetch("/projects", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({name,description}) }); }
async function apiUpdateProject(id, updates)          { return apiFetch(`/projects/${id}`, { method:"PUT",  headers:{"Content-Type":"application/json"}, body:JSON.stringify(updates) }); }
async function apiDeleteProject(id)                   { return apiFetch(`/projects/${id}`, { method:"DELETE" }); }


// Dataset viewer
async function getCleanedDataset(dataType, ipName, page=1, pageSize=100) {
  const base = ipName ? `/dataset/${encodeURIComponent(dataType)}/${encodeURIComponent(ipName)}`
                      : `/dataset/${encodeURIComponent(dataType)}`;
  return apiFetch(`${base}?page=${page}&page_size=${pageSize}`);
}
// Dataset-wide flagged-values lookup (powers column-header popup + bubble popup)
async function getFlaggedValues({ dataType, ipName, mode, column = null, flag = null, limit = 500 }) {
  return apiFetch("/clean/tools/flagged-values", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      data_type: dataType, ip_name: ipName || null,
      mode, column, flag, limit,
    }),
  });
}

async function getValidationResults(dataType, ipName) {
  const base = ipName ? `/validation_results/${encodeURIComponent(dataType)}/${encodeURIComponent(ipName)}`
                      : `/validation_results/${encodeURIComponent(dataType)}`;
  return apiFetch(base);
}

// ══════════════════════════════════════════════════════════
// ISSUE ROWS DOWNLOAD  (mirrors transformer project)
// ══════════════════════════════════════════════════════════

/**
 * Download flagged/issue rows as an XLSX from the transformer backend.
 * Requires a live file_id from the current upload session.
 * @param {string} fileId
 * @param {string} originalName
 * @param {string|null} uuidColumn
 */
async function downloadIssueRowsFile(fileId, originalName = "issue_rows", uuidColumn = null) {
  const query  = uuidColumn ? `?uuid_column=${encodeURIComponent(uuidColumn)}` : "";
  const res    = await apiFetch(`/transform/download-issues/${fileId}${query}`, { _raw: true });

  if (!res.ok) {
    let detail = `Issue export failed (${res.status})`;
    try { const err = await res.json(); detail = err.detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }

  const blob     = await res.blob();
  const url      = URL.createObjectURL(blob);
  const anchor   = document.createElement("a");
  const baseName = originalName.replace(/\.(xlsx|xls|csv)$/i, "");
  anchor.href     = url;
  anchor.download = `${baseName}_issues.xlsx`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

// ══════════════════════════════════════════════════════════
// REPORT RECORDS (per-row paginated view)
// ══════════════════════════════════════════════════════════

async function fetchReportPage(dataType, ipName, stem, cursor=null, pageSize=25) {
  const base = ipName
    ? `/report/${encodeURIComponent(dataType)}/${encodeURIComponent(ipName)}/${encodeURIComponent(stem)}/page`
    : `/report/${encodeURIComponent(dataType)}/${encodeURIComponent(stem)}/page`;
  const qs = `?page_size=${pageSize}` + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "");
  return apiFetch(base + qs);
}
