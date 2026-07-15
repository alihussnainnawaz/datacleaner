// clean_tools.js
// Toolbar tool functions for the Clean view.
// All tools call /api/clean/tools/* — endpoints that operate directly on the
// saved cleaned parquet. No transformer session or file_id required.

// ── Dataset readiness ──────────────────────────────────────────────────────
// The toolbar tools (Trim, Date Format, Standardize, Title Case, Regex Clean)
// all operate on the saved *_cleaned.parquet for the current
// project/IP. That file only exists after the pipeline has been run at least
// once. A project being selected (state.cleanDataType set) does NOT guarantee
// this — switching projects before running the pipeline leaves no cleaned
// parquet, and every tool call 404s ("No cleaned parquet found. Run the
// pipeline first."). This checks the same signal the Clean tab itself uses
// to show its empty state, so we can warn before opening a tool modal rather
// than after the user picks a column.
function _hasCleanedDataset() {
  if (!state.cleanDataType) return false;
  const wrap = document.getElementById("cleanDatasetWrap");
  return !!wrap && wrap.style.display !== "none";
}

// ── Direct tool API fetch ─────────────────────────────────────────────────────

async function _toolFetch(endpoint, body) {
  const res = await fetch(API_BASE + "/clean/tools" + endpoint, {
    method:  "POST",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body:    JSON.stringify(body),
  });
  let data;
  try { data = await res.json(); } catch { throw new Error(`Non-JSON response (${res.status})`); }
  if (!res.ok) throw new Error(data?.detail || data?.error || `Request failed (${res.status})`);
  return data;
}

// Returns { data_type, ip_name } for the current clean project
function _ctx() {
  const dt = state.cleanDataType;
  const ip = state.cleanIpName || null;
  if (!dt) throw new Error("No project selected. Load a dataset first.");
  return { data_type: dt, ip_name: ip };
}

// ── Refresh dataset table ─────────────────────────────────────────────────────

async function _refreshCleanDataset() {
  if (typeof loadCleanDataset === "function") {
    await loadCleanDataset(state.cleanPage || 1);
  }
}

// ── Flag classes ──────────────────────────────────────────────────────────────

function _flagClasses(flags) {
  const classMap = {
    "special_at":       "flag-special-at",
    "special_bang":     "flag-special-bang",
    "special_question": "flag-special-question",
    "special_angle":    "flag-special-angle",
    "special_curly":    "flag-special-curly",
    "special_hash":     "flag-special-hash",
    "special_dollar":   "flag-special-dollar",
    "special_percent":  "flag-special-percent",
    "special_caret":    "flag-special-caret",
    "special_star":     "flag-special-star",
    "special_pipe":     "flag-special-pipe",
    "special_tilde":    "flag-special-tilde",
    "special_quote":    "flag-special-quote",
    "special_bracket":  "flag-special-bracket",
    "special_plus":     "flag-special-plus",
    "special_equals":   "flag-special-equals",
    "special_semi":     "flag-special-semi",
    "special_colon":    "flag-special-colon",
    "name_error":       "flag-name",
    "cnic_error":       "flag-cnic",
    "gender_error":     "flag-gender",
    "uuid_dupe":        "flag-uuid",
    "null_value":       "flag-null",
    "flag-val-fail":    "flag-uuid",
    "duplicate_row":    "flag-duplicate-row",
    "repeating_digit":  "flag-repeating",
  };
  return flags.map(f => classMap[f] || "").filter(Boolean).join(" ");
}

// ── Flag summary chips ────────────────────────────────────────────────────────

function _renderFlagSummary(rows, summaryId = "cleanFlagSummary") {
  const counts = { special:0, name:0, cnic:0, gender:0, uuid:0, null:0, dupe:0, repeat:0 };
  rows.forEach(cells => {
    cells.forEach(cell => {
      const flags = cell.flags || [];
      flags.forEach(f => {
        if (f.startsWith("special_"))  counts.special++;
        if (f === "name_error")        counts.name++;
        if (f === "cnic_error")        counts.cnic++;
        if (f === "gender_error")      counts.gender++;
        if (f === "uuid_dupe")         counts.uuid++;
        if (f === "null_value")        counts.null++;
        if (f === "duplicate_row")     counts.dupe++;
        if (f === "repeating_digit")   counts.repeat++;
        if (f === "flag-val-fail")     counts.uuid++;
      });
    });
  });

  const chips = [
    { key:"special", label:"Special char", cls:"flag-count-chip--special" },
    { key:"name",    label:"Name error",   cls:"flag-count-chip--name"    },
    { key:"cnic",    label:"CNIC error",   cls:"flag-count-chip--cnic"    },
    { key:"gender",  label:"Gender error", cls:"flag-count-chip--gender"  },
    { key:"uuid",    label:"UUID dupe",    cls:"flag-count-chip--uuid"    },
    { key:"null",    label:"Null",         cls:"flag-count-chip--null"    },
    { key:"dupe",    label:"Dupe row",     cls:"flag-count-chip--dupe"    },
    { key:"repeat",  label:"Repeating",    cls:"flag-count-chip--repeat"  },
  ].filter(c => counts[c.key] > 0)
   .map(c => `<span class="flag-count-chip ${c.cls}">${counts[c.key].toLocaleString()} ${c.label}</span>`)
   .join("");

  const wrap = document.getElementById(summaryId);
  if (wrap) wrap.innerHTML = chips;
}

// ── Modal helpers ─────────────────────────────────────────────────────────────

function openModal(id)  { document.getElementById(id).style.display = "flex"; }
function closeModal(id) { document.getElementById(id).style.display = "none"; }

document.addEventListener("DOMContentLoaded", () => {
  ["trimModal", "dateModal", "standardizeModal", "regexModal", "titleCaseModal"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", e => { if (e.target === el) closeModal(id); });
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") ["trimModal", "dateModal", "standardizeModal", "regexModal", "titleCaseModal"].forEach(closeModal);
  });
});

// ── UUID column selector ──────────────────────────────────────────────────────

function onUuidColumnChangeClean(val) {
  state.uuidColumnClean = val || null;
}

function _populateCleanUuidSelect(columns) {
  const sel = document.getElementById("uuidColumnSelectClean");
  if (!sel) return;
  sel.innerHTML =
    `<option value="">— none —</option>` +
    columns.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  sel.value = state.uuidColumnClean || "";
}

function _populateStdColumnSelectClean(columns) {
  const sel = document.getElementById("stdColumnSelect");
  if (!sel) return;
  sel.innerHTML =
    `<option value="">— select a column —</option>` +
    columns.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
}

document.addEventListener("DOMContentLoaded", () => {
  if (state.columns && state.columns.length) {
    _populateCleanUuidSelect(state.columns);
    _populateStdColumnSelectClean(state.columns);
  }
});

// The Auto Clean tool/button was removed: it ran the cleaning engine with
// no enabled_rules argument, bypassing Column Rules gating entirely and
// re-applying every schema-driven rule to every matching column regardless
// of what the user configured. Nothing runs now except what's explicitly
// configured via the "Run Pipeline" flow (Column Rules, global rules,
// validation filters).

// ══════════════════════════════════════════════════════════
// TRIM SPACES
// ══════════════════════════════════════════════════════════

function openTrimModal() {
  const cols = state.columns || [];
  if (!cols.length && !state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  document.getElementById("trimColsList").innerHTML = cols.map(col => `
    <label class="checkbox-item">
      <input type="checkbox" value="${escapeHtml(col)}" />
      ${escapeHtml(col)}
    </label>`).join("");

  const radio = document.querySelector('input[name="trimScope"][value="all"]');
  if (radio) radio.checked = true;
  toggleTrimColumns(false);
  openModal("trimModal");
}

function toggleTrimColumns(show) {
  const g = document.getElementById("trimColsGroup");
  if (g) g.style.display = show ? "block" : "none";
}

async function applyTrim() {
  const scope = document.querySelector('input[name="trimScope"]:checked')?.value;
  let columns = null;

  if (scope === "selected") {
    columns = [...document.querySelectorAll('#trimColsList input:checked')].map(cb => cb.value);
    if (!columns.length) { showToast("Select at least one column.", "error"); return; }
  }

  closeModal("trimModal");
  showLoader("Trimming whitespace…");

  try {
    await _toolFetch("/trim", { ..._ctx(), columns });
    await _refreshCleanDataset();
    showToast(columns
      ? `Whitespace trimmed from ${columns.length} column(s).`
      : "Whitespace trimmed from all columns.", "success");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}

// ══════════════════════════════════════════════════════════
// NOT NULL CHECK (plain missing-value flag — separate from the
// Unique/Not-Null primary-key handling that also checks duplicates)
// ══════════════════════════════════════════════════════════

function openNotNullModal() {
  const cols = state.columns || [];
  if (!cols.length && !state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  document.getElementById("notNullColsList").innerHTML = cols.map(col => `
    <label class="checkbox-item">
      <input type="checkbox" value="${escapeHtml(col)}" />
      ${escapeHtml(col)}
    </label>`).join("");

  const radio = document.querySelector('input[name="notNullScope"][value="all"]');
  if (radio) radio.checked = true;
  toggleNotNullColumns(false);
  document.getElementById("notNullResults").style.display = "none";
  openModal("notNullModal");
}

function toggleNotNullColumns(show) {
  const g = document.getElementById("notNullColsGroup");
  if (g) g.style.display = show ? "block" : "none";
}

async function applyNotNull() {
  const scope = document.querySelector('input[name="notNullScope"]:checked')?.value;
  let columns = null;

  if (scope === "selected") {
    columns = [...document.querySelectorAll('#notNullColsList input:checked')].map(cb => cb.value);
    if (!columns.length) { showToast("Select at least one column.", "error"); return; }
  }

  showLoader("Checking for null/empty values…");

  try {
    const res = await _toolFetch("/not-null", { ..._ctx(), columns });
    await _refreshCleanDataset();

    const counts = res.per_column_counts || {};
    const rows = Object.entries(counts)
      .filter(([, n]) => n > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([col, n]) => `
        <div class="review-row" style="padding:8px 12px">
          <div class="review-row-main">
            <span class="review-row-col">${escapeHtml(col)}</span>
            <span class="review-row-count">${n.toLocaleString()} null/empty cell${n !== 1 ? "s" : ""}</span>
          </div>
        </div>`).join("");

    const resultsEl = document.getElementById("notNullResults");
    resultsEl.innerHTML = rows
      ? `<div class="review-popup-count" style="margin-top:12px">${res.total_flagged.toLocaleString()} cell(s) flagged for review</div>
         <div class="review-popup-list" style="max-height:240px">${rows}</div>`
      : `<div class="review-popup-empty" style="padding:12px 0">No null/empty values found — nothing flagged.</div>`;
    resultsEl.style.display = "block";

    showToast(res.message, res.total_flagged ? "success" : "success");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}

// ══════════════════════════════════════════════════════════
// DATE FORMAT
// ══════════════════════════════════════════════════════════

function openDateModal() {
  const cols = state.columns || [];
  if (!cols.length && !state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  const dateCols = cols.filter(c =>
    ["date","dt","dob","doj","day","time"].some(h => c.toLowerCase().includes(h))
  );
  const showCols = dateCols.length ? dateCols : cols;

  document.getElementById("dateColsList").innerHTML = showCols.map(col => `
    <label class="checkbox-item">
      <input type="checkbox" value="${escapeHtml(col)}" ${dateCols.includes(col) ? "checked" : ""} />
      ${escapeHtml(col)}
    </label>`).join("");

  openModal("dateModal");
}

async function applyDateFormat() {
  const columns = [...document.querySelectorAll('#dateColsList input:checked')].map(cb => cb.value);
  if (!columns.length) { showToast("Select at least one column.", "error"); return; }

  const fmt = document.querySelector('input[name="dateFormat"]:checked')?.value || "%d-%m-%Y";

  closeModal("dateModal");
  showLoader("Formatting dates…");

  try {
    const res = await _toolFetch("/dates", { ..._ctx(), columns, fmt });
    await _refreshCleanDataset();
    const failed = res.failed_cells?.length || 0;
    showToast(
      failed
        ? `Dates formatted. ${failed} cell(s) could not be parsed.`
        : `Dates formatted in ${columns.length} column(s).`,
      failed ? "warning" : "success"
    );
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}

// ══════════════════════════════════════════════════════════
// STANDARDIZE VALUES
// ══════════════════════════════════════════════════════════

let _selectedVals = [];

async function openStandardizeModal() {
  if (!state.cleanDataType && !state.fileId) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  _selectedVals = [];

  // Ensure we have column names — fetch from parquet if state.columns is empty
  let cols = state.columns || [];
  if (!cols.length) {
    try {
      const dt  = state.cleanDataType;
      const ip  = state.cleanIpName || null;
      const url = ip
        ? `${API_BASE}/dataset/${encodeURIComponent(dt)}/${encodeURIComponent(ip)}?page=1&page_size=1`
        : `${API_BASE}/dataset/${encodeURIComponent(dt)}?page=1&page_size=1`;
      const res  = await fetch(url);
      const data = await res.json();
      cols = data.columns || [];
      if (cols.length) state.columns = cols;
    } catch { /* proceed with empty */ }
  }

  _populateStdColumnSelectClean(cols);

  document.getElementById("stdValuesGroup").style.display  = "none";
  document.getElementById("mappingSection").style.display  = "none";
  document.getElementById("uniqueValuesGrid").innerHTML    = "";
  document.getElementById("mappingSourceTags").innerHTML   = "";
  document.getElementById("mappingTargetInput").value      = "";

  openModal("standardizeModal");
}

async function loadUniqueValues() {
  const column = document.getElementById("stdColumnSelect").value;
  if (!column) return;

  _selectedVals = [];
  document.getElementById("mappingSection").style.display = "none";
  document.getElementById("mappingSourceTags").innerHTML  = "";

  const grid = document.getElementById("uniqueValuesGrid");
  grid.innerHTML = `<span style="color:var(--text-4);font-size:12px;">Loading values…</span>`;
  document.getElementById("stdValuesGroup").style.display = "block";

  try {
    const res = await _toolFetch("/unique", { ..._ctx(), column });
    const values = res.values || [];

    if (!values.length) {
      grid.innerHTML = `<span style="color:var(--text-4);font-size:12px;">No unique values found.</span>`;
      return;
    }

    grid.innerHTML = values.map(val => `
      <span class="unique-val-chip"
            onclick="toggleUniqueVal(this, '${escapeAttr(val)}')"
            title="${escapeHtml(val)}">
        ${escapeHtml(val)}
      </span>`).join("");

  } catch (err) {
    grid.innerHTML = `<span style="color:var(--red);font-size:12px;">${escapeHtml(err.message)}</span>`;
  }
}

function toggleUniqueVal(chip, value) {
  const idx = _selectedVals.indexOf(value);
  if (idx === -1) { _selectedVals.push(value); chip.classList.add("selected"); }
  else            { _selectedVals.splice(idx, 1); chip.classList.remove("selected"); }

  document.getElementById("mappingSourceTags").innerHTML =
    _selectedVals.map(v => `<span class="mapping-tag">${escapeHtml(v)}</span>`).join("");

  document.getElementById("mappingSection").style.display =
    _selectedVals.length ? "block" : "none";
}

async function applyStandardize() {
  const column = document.getElementById("stdColumnSelect").value;
  const target = document.getElementById("mappingTargetInput").value.trim();

  if (!column)               { showToast("Select a column.", "error");        return; }
  if (!_selectedVals.length) { showToast("Select values to remap.", "error"); return; }
  if (!target)               { showToast("Enter the target value.", "error"); return; }

  const mapping = {};
  _selectedVals.forEach(v => { mapping[v] = target; });

  closeModal("standardizeModal");
  showLoader("Applying standardization…");

  try {
    const res = await _toolFetch("/standardize", { ..._ctx(), column, mapping });
    await _refreshCleanDataset();
    showToast(`${res.changes} value(s) updated in "${column}".`, "success");
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}


// ══════════════════════════════════════════════════════════
// TITLE CASE
// ══════════════════════════════════════════════════════════

const _CASE_STYLE_OPTIONS = `
  <option value="title">Title Case</option>
  <option value="upper">UPPER CASE</option>
  <option value="lower">lower case</option>
  <option value="camel">camelCase</option>
`;

function openTitleCaseModal() {
  const cols = state.columns || [];
  if (!cols.length && !state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  document.getElementById("titleCaseColsList").innerHTML = cols.map(col => `
    <div class="case-style-row">
      <label class="checkbox-item">
        <input type="checkbox" class="case-style-checkbox" value="${escapeHtml(col)}" checked/>
        ${escapeHtml(col)}
      </label>
      <select class="select-input case-style-select" data-col="${escapeHtml(col)}">
        ${_CASE_STYLE_OPTIONS}
      </select>
    </div>`).join("");

  const radio = document.querySelector('input[name="titleCaseScope"][value="all"]');
  if (radio) radio.checked = true;
  document.getElementById("titleCaseDefaultStyle").value = "title";
  toggleTitleCaseColumns(false);
  openModal("titleCaseModal");
}

function toggleTitleCaseColumns(show) {
  const g = document.getElementById("titleCaseColsGroup");
  if (g) g.style.display = show ? "block" : "none";
  const d = document.getElementById("titleCaseDefaultStyleGroup");
  // The default-style picker applies to "All text columns" mode; in
  // "Selected columns" mode each row has its own style picker instead.
  if (d) d.style.display = show ? "none" : "block";
}

async function applyTitleCase() {
  const scope = document.querySelector('input[name="titleCaseScope"]:checked')?.value;
  const defaultStyle = document.getElementById("titleCaseDefaultStyle")?.value || "title";
  let columns = null;
  let columnStyles = {};

  if (scope === "selected") {
    const checked = [...document.querySelectorAll('#titleCaseColsList .case-style-checkbox:checked')];
    if (!checked.length) { showToast("Select at least one column.", "error"); return; }
    columns = checked.map(cb => cb.value);
    columns.forEach(col => {
      const sel = document.querySelector(`#titleCaseColsList .case-style-select[data-col="${CSS.escape(col)}"]`);
      columnStyles[col] = sel?.value || "title";
    });
  }

  closeModal("titleCaseModal");
  showLoader("Applying case style…");

  try {
    const res = await _toolFetch("/title-case", {
      ..._ctx(), columns,
      case_style: defaultStyle,
      column_styles: scope === "selected" ? columnStyles : {},
    });
    await _refreshCleanDataset();
    showToast(
      columns
        ? `Case style applied to ${columns.length} column(s) — ${res.changes} cell(s) changed.`
        : `${defaultStyle === "title" ? "Title case" : defaultStyle + " case"} applied to all text columns — ${res.changes} cell(s) changed.`,
      "success"
    );
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}

// ══════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════

function escapeAttr(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ══════════════════════════════════════════════════════════
// REGEX CLEAN  (Auto mode — finds & clusters inconsistencies)
// ══════════════════════════════════════════════════════════

// Local editable state for the current column's clusters. Rebuilt every time
// a new column is analysed; mutated in place as the user edits/removes things.
let _regexClusters = [];

async function openRegexModal() {
  if (!state.cleanDataType && !state.fileId) { showToast("Load a dataset first.", "error"); return; }
  if (!_hasCleanedDataset()) {
    showToast("No cleaned dataset yet for this project — run the pipeline first.", "error", 6000);
    return;
  }

  // Ensure columns are loaded
  let cols = state.columns || [];
  if (!cols.length) {
    try {
      const dt  = state.cleanDataType;
      const ip  = state.cleanIpName || null;
      const url = ip
        ? `${API_BASE}/dataset/${encodeURIComponent(dt)}/${encodeURIComponent(ip)}?page=1&page_size=1`
        : `${API_BASE}/dataset/${encodeURIComponent(dt)}?page=1&page_size=1`;
      const data = await (await fetch(url)).json();
      cols = data.columns || [];
      if (cols.length) state.columns = cols;
    } catch { /* proceed */ }
  }

  // Populate column select and reset
  const sel = document.getElementById("regexColumnSelect");
  sel.innerHTML = `<option value="">— select a column —</option>` +
    cols.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");

  _regexClusters = [];
  document.getElementById("regexPreviewSection").style.display = "none";
  document.getElementById("regexPreviewMsg").textContent       = "";
  document.getElementById("regexApplyBtn").disabled            = true;
  document.getElementById("regexChangeCount").textContent      = "";

  openModal("regexModal");
}

// Triggered when column is selected — auto-analyse immediately into clusters
async function regexPreview() {
  const column   = document.getElementById("regexColumnSelect").value;
  const msgEl    = document.getElementById("regexPreviewMsg");
  const applyBtn = document.getElementById("regexApplyBtn");

  _regexClusters = [];

  if (!column) {
    document.getElementById("regexPreviewSection").style.display = "none";
    msgEl.textContent = "";
    applyBtn.disabled = true;
    return;
  }

  msgEl.style.color   = "var(--text-4)";
  msgEl.textContent   = "Analysing column…";
  applyBtn.disabled   = true;
  document.getElementById("regexPreviewSection").style.display = "none";

  try {
    const res = await _toolFetch("/regex-clusters", { ..._ctx(), column });
    _regexClusters = res.clusters || [];

    document.getElementById("regexChangeCount").textContent =
      _regexClusters.length
        ? `— ${res.cluster_count} group${res.cluster_count !== 1 ? "s" : ""} · ${res.total_affected} value${res.total_affected !== 1 ? "s" : ""} affected`
        : "— no inconsistencies found";

    if (!_regexClusters.length) {
      msgEl.textContent = "Values in this column look consistent — nothing to standardise.";
      applyBtn.disabled = true;
      return;
    }

    _renderRegexClusters();
    document.getElementById("regexPreviewSection").style.display = "block";

    msgEl.style.color = "var(--accent)";
    msgEl.textContent = "⚠ Edit the target name or remove a wrongly-grouped value, then click Apply Changes.";
    applyBtn.disabled = false;

  } catch (err) {
    msgEl.style.color = "var(--red)";
    msgEl.textContent = err.message;
    applyBtn.disabled = true;
  }
}

// Render each cluster as an editable card: target value (editable text) +
// member chips (each removable). State lives in _regexClusters; every edit
// mutates it directly so Apply always sends exactly what's on screen.
function _renderRegexClusters() {
  const list = document.getElementById("regexClusterList");
  if (!list) return;

  if (!_regexClusters.length) {
    list.innerHTML = `<div class="review-popup-empty">All groups resolved — nothing left to apply.</div>`;
    return;
  }

  list.innerHTML = _regexClusters.map((c, ci) => `
    <div class="regex-cluster-card" data-cluster-idx="${ci}">
      <div class="regex-cluster-header">
        <span class="review-tag ${c.status === "auto" ? "review-tag--green" : "review-tag--yellow"}">
          ${c.status === "auto" ? "Auto" : "Review"}
        </span>
        <span class="regex-cluster-arrow">→</span>
        <input type="text" class="regex-cluster-target" value="${escapeAttr(c.canonical)}"
               data-cluster-idx="${ci}" placeholder="Target value" />
        <button type="button" class="regex-cluster-delete" data-cluster-idx="${ci}" title="Discard this entire group">
          Discard group
        </button>
      </div>
      <div class="regex-cluster-members" data-cluster-idx="${ci}">
        ${c.members.map((m, mi) => `
          <span class="bad-pattern-chip regex-member-chip" data-cluster-idx="${ci}" data-member-idx="${mi}">
            ${escapeHtml(m.value)} <span class="regex-member-count">×${m.count}</span>
            <button type="button" class="bad-pattern-chip-remove regex-member-remove" title="Remove from this group">✕</button>
          </span>`).join("")}
      </div>
    </div>`).join("");
}

// Delegated handlers for the cluster editor: rename target, remove a member,
// discard a whole group. All mutate _regexClusters in place.
document.addEventListener("input", (e) => {
  if (!e.target.classList?.contains("regex-cluster-target")) return;
  const idx = parseInt(e.target.dataset.clusterIdx);
  if (_regexClusters[idx]) _regexClusters[idx].canonical = e.target.value;
});

document.addEventListener("click", (e) => {
  const removeBtn = e.target.closest(".regex-member-remove");
  if (removeBtn) {
    const chip = removeBtn.closest(".regex-member-chip");
    const ci = parseInt(chip.dataset.clusterIdx);
    const mi = parseInt(chip.dataset.memberIdx);
    if (_regexClusters[ci]) {
      _regexClusters[ci].members.splice(mi, 1);
      // Drop the whole cluster if nothing's left to remap.
      if (_regexClusters[ci].members.length === 0) _regexClusters.splice(ci, 1);
      _renderRegexClusters();
      const applyBtn = document.getElementById("regexApplyBtn");
      if (applyBtn) applyBtn.disabled = _regexClusters.length === 0;
    }
    return;
  }
  const discardBtn = e.target.closest(".regex-cluster-delete");
  if (discardBtn) {
    const ci = parseInt(discardBtn.dataset.clusterIdx);
    _regexClusters.splice(ci, 1);
    _renderRegexClusters();
    const applyBtn = document.getElementById("regexApplyBtn");
    if (applyBtn) applyBtn.disabled = _regexClusters.length === 0;
  }
});

async function applyRegex() {
  const column = document.getElementById("regexColumnSelect").value;
  if (!column) { showToast("Select a column.", "error"); return; }
  if (!_regexClusters.length) { showToast("No groups to apply.", "error"); return; }

  closeModal("regexModal");
  showLoader("Applying changes…");

  try {
    const payload = _regexClusters.map(c => ({
      canonical:          c.canonical,
      original_canonical: c.original_canonical,
      members:            c.members.map(m => ({ value: m.value })),
    }));

    const res = await _toolFetch("/regex-apply-clusters", { ..._ctx(), column, clusters: payload });

    // Build a flat before/after preview from what we just applied, so the
    // existing table-highlight helper (which expects row pairs) still works.
    const preview = [];
    payload.forEach(c => {
      const target = c.canonical;
      const sources = [c.original_canonical, ...c.members.map(m => m.value)].filter(v => v && v !== target);
      sources.forEach(before => preview.push({ before, after: target }));
    });

    state._lastRegexResult = { column, preview, review_rows: [] };
    await _refreshCleanDataset();

    showToast(
      `Regex Clean — ${res.changes} cell${res.changes !== 1 ? "s" : ""} updated in "${column}".`,
      "success", 5000
    );
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}

function _highlightRegexResults(column, preview, reviewRows) {
  const table = document.getElementById("cleanTable");
  if (!table) return;

  // Build lookup: row-number (1-based display row) → { type, before, after }
  // preview rows use row = df_index + 2 (header row = 1, data starts at 2)
  const changedRows  = new Map(preview.map(r => [r.row, { type: "changed", before: r.before, after: r.after }]));
  const reviewRowSet = new Map(reviewRows.map(r => [r.row, { type: "review", value: r.value }]));

  // Find which column index in the table corresponds to `column`
  const thead = table.querySelector("thead tr");
  if (!thead) return;
  const headers = [...thead.querySelectorAll("th")].map(th => th.textContent.trim());
  // headers[0] = "#", headers[1..] = column names
  const colIdx = headers.indexOf(column); // 0-based in headers array, includes # col
  if (colIdx === -1) return;

  // The page offset: row number shown in the # column
  const startRow = (state.cleanPage - 1) * 100 + 2;

  const tbody = table.querySelector("tbody");
  if (!tbody) return;

  // Remove any previous regex highlights
  tbody.querySelectorAll(".regex-changed, .regex-review").forEach(el => {
    el.classList.remove("regex-changed", "regex-review");
    el.removeAttribute("data-regex-before");
    el.removeAttribute("title");
  });

  [...tbody.querySelectorAll("tr")].forEach((tr, rowIdx) => {
    const displayRow = startRow + rowIdx;
    const td = tr.querySelectorAll("td")[colIdx]; // colIdx includes # col
    if (!td) return;

    if (changedRows.has(displayRow)) {
      const info = changedRows.get(displayRow);
      td.classList.add("regex-changed");
      td.setAttribute("data-regex-before", info.before);
      td.title = `Auto-fixed: "${info.before}" → "${info.after}"`;
    } else if (reviewRowSet.has(displayRow)) {
      const info = reviewRowSet.get(displayRow);
      td.classList.add("regex-review");
      td.title = `Needs review: "${info.value}"`;
    }
  });
}
