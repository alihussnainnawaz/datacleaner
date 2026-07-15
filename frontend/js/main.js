// main.js — Unified Data Pipeline v3 (Projects + Clean + Validate)

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  activeProject:   null,
  fileId:          null, fileName: null, rowCount: null, columns: [],
  dataType:        null, ipName: null, uuidColumn: null, columnRules: {}, caseStyles: {},
  // clean view
  cleanPage: 1, cleanTotalPages: 1, cleanDataType: null, cleanIpName: null,
  // datatype filter (Column Rule Preview)
  autoDtypes: {},   // { col: { type, confidence, sample } } — backend auto-detect cache, all-columns mode
  dtypeRules: {},   // { col: expectedType } — user-picked expected type, single-column mode
  // validate view
  valFailures: [], valPage: 1, valPageSize: 50, valSearch: "",
  valDataType: null, valIpName: null,
  valData:     null,
  // validate highlighted table (client-side run)
  valHlPage: 1, valHlTotalPages: 1, valHlData: null,
  // predefined validation results from last pipeline run (R01–R12)
  predefinedValidation: null,
  // active chip filter on validate table
  _valActiveChip: null, _valActiveRowCls: null,
  // projects cache
  allProjects: [],
  globalRules: { trim: true, null: true, special: true },
  // Dataset 2 — for Cross Check / Double Cross Check validation filters.
  // Uploaded separately from the main file via the pill shown in place of
  // "Dataset 2 Not Loaded" once a cross/doublecross filter is configured.
  dataset2FileId: null, dataset2FileName: null,
};

const REQUIRES_IP = { beneficiary: true, certificates: true, banks: false, financials: false };

const RULE_CATALOG = {
  CNIC_FORMAT:            { label: "CNIC Format",      hints: ["13-digit"],         color: "blue",   detect: c => /cnic|national.?id/i.test(c) },
  CELL_NO_NORMALIZED:     { label: "Cell No.",          hints: ["03XX", "+92 strip"], color: "green",  detect: c => /cell|phone|mobile/i.test(c) },
  DATE_STANDARDIZED:      { label: "Date",              hints: ["standardise"],      color: "purple", detect: c => /date|dob|birth/i.test(c) },
  GEO_STANDARDIZED:       { label: "Geo / Location",    hints: ["district", "UC"],   color: "teal",   detect: c => /district|tehsil|\buc\b|union.?council/i.test(c) },
  BANK_STANDARDIZED:      { label: "Bank Name",         hints: ["canonical"],        color: "amber",  detect: c => /bank.?name|\bbank\b/i.test(c) },
  BANK_ACCOUNT_NORMALISED:{ label: "IBAN / Acct",       hints: ["IBAN"],             color: "amber",  detect: c => /iban|account.?no/i.test(c) },
  GENDER_STANDARDIZED:    { label: "Gender",            hints: ["M→Male"],           color: "pink",   detect: c => /gender|\bsex\b/i.test(c) },
  BOOL_STANDARDIZED:      { label: "Boolean",           hints: ["Yes/No"],           color: "orange", detect: c => /\bis_|\bhas_|eligible|verified/i.test(c) },
  CATEGORY_STANDARDIZED:  { label: "Category",          hints: ["normalise"],        color: "indigo", detect: c => /category|\btype\b/i.test(c) },
  TITLE_CASED:            { label: "Text Case",         hints: ["choose style"],     color: "slate",  detect: c => /\bname\b|full.?name/i.test(c), needsStyle: true },
  UNIQUE_VALUES:          { label: "Unique",            hints: ["no dups"],          color: "indigo", detect: () => false },
  NOT_NULL:               { label: "Not Null",          hints: ["no blanks"],        color: "indigo", detect: () => false },
  REGEX_CLEAN:            { label: "Regex Clean",       hints: ["find/replace"],     color: "rose",   detect: () => false },
  DATATYPE_CHECK:         { label: "Datatype",          hints: ["drag: all = auto-detect · one column = pick type"], color: "cyan", detect: () => false, isDatatype: true },
};

// Human-readable labels for the datatypes the backend can detect/check for.
const DTYPE_LABELS = {
  integer: "Integer", decimal: "Decimal", boolean: "Boolean", email: "Email",
  phone: "Phone", cnic: "CNIC", iban: "IBAN", date: "Date", text: "Text", unknown: "Unknown",
};
let _dragState = null;

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeRecoModal(); closeValPopup(); closeNewProjModal(); }
  if (e.key === "Enter" && document.getElementById("newProjOverlay").style.display === "flex") createProject();
});

document.addEventListener("DOMContentLoaded", () => {
  initNav();
  initUploadZone();
  pingBackend();
  renderProjectCards();
  fetch("/api/coordinates").then(r => r.json()).then(d => {
    if (d && d.geojson) window._coordsGeoFeatures = d.geojson.features || [];
  }).catch(() => {});
});

async function pingBackend() {
  try { await checkHealth(); setStatus("active", "Backend connected"); }
  catch { setStatus("error", "Backend unreachable"); showToast("Backend unreachable. Make sure the server is running.", "error", 6000); }
}

// ── Navigation ────────────────────────────────────────────────────────────────
function initNav() {
  document.querySelectorAll(".nav-item").forEach(item => {
    item.addEventListener("click", e => {
      e.preventDefault();
      const v = item.dataset.view;
      switchView(v);
      if (v === "clean")    _syncCleanProjectDropdown();
      if (v === "validate") _syncValProjectDropdown();
      if (v === "report" && window._lastPipelineResult && typeof renderReport === "function")
        renderReport(window._lastPipelineResult);
    });
  });
}

function switchView(name) {
  document.querySelectorAll(".nav-item").forEach(i => i.classList.toggle("active", i.dataset.view === name));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view${capitalize(name)}`));
  const titles = {
    upload:   ["Upload",   "Select a project, upload your data, and run the pipeline"],
    clean:    ["Clean",    "Inspect the cleaned dataset and its flagged cells"],
    validate: ["Validate", "Per-row validation results based on your configured filters"],
    report:   ["Report",   "Combined summary of cleaning and validation across all pipeline runs"],
  };
  const [t, s] = titles[name] || ["—", ""];
  document.getElementById("pageTitle").textContent = t;
  document.getElementById("pageSub").textContent   = s;
}

// ── Projects ──────────────────────────────────────────────────────────────────
function openNewProjModal() {
  document.getElementById("newProjOverlay").style.display = "flex";
  setTimeout(() => document.getElementById("newProjName").focus(), 50);
}
function closeNewProjModal() {
  document.getElementById("newProjOverlay").style.display = "none";
  document.getElementById("newProjName").value = "";
  document.getElementById("newProjDesc").value = "";
}

async function createProject() {
  const name = document.getElementById("newProjName").value.trim();
  const desc = document.getElementById("newProjDesc").value.trim();
  if (!name) { document.getElementById("newProjName").focus(); return; }
  try {
    const res = await apiCreateProject(name, desc);
    if (res.success) {
      closeNewProjModal();
      state.allProjects = await apiGetProjects();
      renderProjectCards();
      selectProject(res.project);
      showToast(`Project "${name}" created`, "success");
    } else {
      showToast("Failed: " + (res.error || ""), "error");
    }
  } catch (e) { showToast(e.message, "error"); }
}

async function renderProjectCards() {
  const grid  = document.getElementById("projGrid");
  const empty = document.getElementById("projEmpty");
  try { state.allProjects = await apiGetProjects(); } catch { state.allProjects = []; }
  grid.querySelectorAll(".proj-card").forEach(c => c.remove());
  if (!state.allProjects.length) { if (empty) empty.style.display = "flex"; return; }
  if (empty) empty.style.display = "none";
  state.allProjects.forEach(proj => {
    const fc = (proj.filters || []).length;
    const dt = proj.dataType ? ` · ${proj.dataType}` : "";
    const card = document.createElement("div");
    card.className  = "proj-card" + (state.activeProject?.id === proj.id ? " active" : "");
    card.dataset.id = proj.id;
    card.innerHTML  = `
      <div class="proj-card-header">
        <div class="proj-card-icon">
          <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8">
            <rect x="3" y="3" width="14" height="14" rx="2"/>
            <path d="M7 8h6M7 11h4" stroke-linecap="round"/>
          </svg>
        </div>
        <button class="proj-card-del" title="Delete"
          onclick="deleteProject(event,${proj.id},'${(proj.name || "").replace(/'/g, "\\'")}')">✕</button>
      </div>
      <p class="proj-card-name">${escapeHtml(proj.name)}</p>
      ${proj.description ? `<p class="proj-card-desc">${escapeHtml(proj.description)}</p>` : ""}
      <div class="proj-card-meta">
        <span class="proj-card-pill">${fc} filter${fc !== 1 ? "s" : ""}${dt}</span>
        <span>Click to select</span>
      </div>`;
    card.addEventListener("click", e => { if (!e.target.closest(".proj-card-del")) selectProject(proj); });
    grid.appendChild(card);
  });
}

function selectProject(proj) {
  state.activeProject = proj;
  document.querySelectorAll(".proj-card").forEach(c =>
    c.classList.toggle("active", String(c.dataset.id) === String(proj.id)));
  document.getElementById("uploadSection").style.display = "flex";
  document.getElementById("activeProjectName").textContent = proj.name;
  if (proj.dataType) {
    state.dataType = proj.dataType;
    document.querySelectorAll(".type-tile").forEach(t => t.classList.toggle("selected", t.dataset.type === proj.dataType));
    const ipGroup = document.getElementById("ipNameGroup");
    ipGroup.style.display = REQUIRES_IP[proj.dataType] ? "flex" : "none";
    if (proj.ipName) { state.ipName = proj.ipName; document.getElementById("ipNameInput").value = proj.ipName; }
    else             { state.ipName = null;         document.getElementById("ipNameInput").value = ""; }
  } else {
    state.dataType = null;
    document.querySelectorAll(".type-tile").forEach(t => t.classList.remove("selected"));
    document.getElementById("ipNameGroup").style.display = "none";
  }
  if (state.fileId) {
    document.getElementById("configGrid").style.display = "grid";
    document.getElementById("runBar").style.display = "flex";
  }
  if (Array.isArray(proj.filters) && proj.filters.length > 0) {
    setTimeout(() => {
      if (typeof restoreFiltersIntoRuleCards === "function") restoreFiltersIntoRuleCards(proj.filters);
    }, 100);
  } else {
    clearFilterRuleCards();
  }
  _syncRunButton();
  _updateValTrigger();
  document.getElementById("uploadSection").scrollIntoView({ behavior: "smooth", block: "start" });
  showToast(`Project "${proj.name}" selected`, "success");
}

async function deleteProject(e, id, name) {
  e.stopPropagation();
  if (!confirm(`Delete project "${name}"?`)) return;
  await apiDeleteProject(id);
  if (state.activeProject?.id === id) {
    state.activeProject = null;
    document.getElementById("uploadSection").style.display = "none";
  }
  await renderProjectCards();
  showToast(`Project "${name}" deleted`);
}

async function saveProjectState(silent = true) {
  if (!state.activeProject) return;
  let filters = window.allConfigs || [];
  try {
    if (typeof syncColumns === "function") syncColumns();
    loadGeneratedFilters();
    filters = window.allConfigs || [];
  } catch (e) {
    // Don't let a validation-filter collection error prevent the cleaner
    // rules (columnRules/caseStyles/globalRules/dtypeRules) below from
    // saving — these are independent pieces of project state.
    console.error("saveProjectState: loadGeneratedFilters failed", e);
  }
  const columnRules = state.columnRules || {};
  const caseStyles  = state.caseStyles  || {};
  const globalRules = state.globalRules || {};
  const regexRules  = state.regexRules  || {};
  const dtypeRules  = state.dtypeRules  || {};
  try {
    const res = await apiUpdateProject(state.activeProject.id, {
      filters, dataType: state.dataType || null, ipName: state.ipName || null,
      columnRules, caseStyles, globalRules, regexRules, dtypeRules,
    });
    if (res.success) {
      Object.assign(state.activeProject, {
        filters, columnRules, caseStyles, globalRules, regexRules, dtypeRules,
      });
      renderProjectCards();
    } else if (!silent) {
      showToast("Failed to save project: " + (res.error || "unknown error"), "error");
    }
  } catch (e) {
    console.error("saveProjectState: apiUpdateProject failed", e);
    if (!silent) showToast("Failed to save project — check your connection.", "error");
  }
}

window.saveFiltersToProject = function (silent = true) { saveProjectState(silent); };
window.apiUrl = function (path) { return "/api" + path; };

// Restore saved filters into rule cards
//
// IMPORTANT: a filter's colIdx is only valid for the column order of the
// dataset that was active when it was saved. On a fresh upload the same
// project's columns can be reordered, added to, or renamed slightly — so we
// always re-resolve colIdx by matching the saved colName against the
// CURRENT dataset's column list first, and only fall back to the saved
// colIdx if no name match exists (e.g. the column was renamed/removed).
// Without this, a restored filter can silently point at the wrong column
// on new data while still looking correctly selected in the UI.
function _resolveColIdxByName(name, fallbackIdx) {
  const cols = window.allColumns || [];
  if (name) {
    const target = String(name).trim().toLowerCase();
    const idx = cols.findIndex(c => (c.col1 || "").trim().toLowerCase() === target);
    if (idx !== -1) return idx;
  }
  // No name match (renamed/removed column) — fall back to the saved index
  // only if it's still in range, otherwise leave unresolved.
  return (fallbackIdx != null && fallbackIdx >= 0 && fallbackIdx < cols.length) ? fallbackIdx : null;
}

function restoreFiltersIntoRuleCards(filters) {
  clearFilterRuleCards();
  if (!Array.isArray(filters) || !filters.length) return;
  if (typeof syncColumns === "function") syncColumns();
  const container = document.getElementById("tablesContainer");
  if (!container) return;
  let unresolvedCount = 0;
  filters.forEach(f => {
    if (typeof totalTables === "undefined") window.totalTables = 0;
    window.totalTables = (window.totalTables || 0) + 1;
    const i = window.totalTables;
    if (typeof addSingleFilter === "function") addSingleFilter(container, i);
    const condSel = document.getElementById(`condSelect_${i}`);
    if (condSel && f.cond) { condSel.value = f.cond; if (typeof handleConditionUI === "function") handleConditionUI(i); }
    setTimeout(() => {
      if (typeof syncColumns === "function") syncColumns();
      const cols = window.allColumns || [];
      const setCombo = (selId, idx, nameVal) => {
        const sel = document.getElementById(selId);
        if (!sel) return;
        sel.value = idx != null ? idx : "";
        sel.dispatchEvent(new Event("change"));
        const wrap = sel.closest?.(".col-combo-wrap");
        if (wrap) {
          const inp = wrap.querySelector(".col-combo-input");
          if (inp) { inp.value = nameVal || ""; if (nameVal) inp.classList.add("has-value"); }
        }
      };
      if (f.cond === "coords") {
        const lngIdx = _resolveColIdxByName(f.lngColName, f.lngColIdx);
        const latIdx = _resolveColIdxByName(f.latColName, f.latColIdx);
        const verIdx = _resolveColIdxByName(f.verifyColName, f.verifyColIdx);
        if (lngIdx == null || latIdx == null || verIdx == null) unresolvedCount++;
        setCombo(`coordLngCol_${i}`,    lngIdx, lngIdx != null ? cols[lngIdx]?.col1 : f.lngColName);
        setCombo(`coordLatCol_${i}`,    latIdx, latIdx != null ? cols[latIdx]?.col1 : f.latColName);
        setCombo(`coordVerifyCol_${i}`, verIdx, verIdx != null ? cols[verIdx]?.col1 : f.verifyColName);
        const lvl = document.getElementById(`coordLevel_${i}`);
        if (lvl && f.coordLevel) lvl.value = f.coordLevel;
      } else if (f.colIdx != null) {
        const resolvedIdx = _resolveColIdxByName(f.colName, f.colIdx);
        if (resolvedIdx == null) unresolvedCount++;
        setCombo(`colSelect_${i}`, resolvedIdx, resolvedIdx != null ? cols[resolvedIdx]?.col1 : (f.colName || ""));
      }
      // Stamp the saved column name onto the card itself — fpReflectAllRules()
      // (filter-palette.js) reads card.dataset.colName to re-place the column
      // chip when the validation popup is reopened. Without this it always
      // fell through to a less reliable fallback (whatever the dropdown
      // currently shows), which is what made filters appear "lost" from
      // their columns after closing and reopening the popup.
      const card = document.getElementById(`tableBox_${i}`);
      if (card && f.colName) card.dataset.colName = f.colName;
      const mv = document.getElementById(`matchInput_${i}`);
      if (mv && f.matchVal) mv.value = f.matchVal;
      // bad_pattern stores its values as a comma-joined string in matchVal —
      // rebuild the visible chip list from it (the hidden input alone isn't
      // enough, since the chips are what loadGeneratedFilters' UI reflects).
      if (f.cond === "bad_pattern" && f.matchVal) {
        window._badPatternValues = window._badPatternValues || {};
        window._badPatternValues[i] = String(f.matchVal).split(",").map(v => v.trim()).filter(Boolean);
        if (typeof _renderBadPatternChips === "function") _renderBadPatternChips(i);
      }
    }, 80);
  });
  setTimeout(() => {
    if (typeof window.fpReflectAllRules === "function") window.fpReflectAllRules();
    _updateValTrigger();
    if (unresolvedCount > 0) {
      showToast(
        `${unresolvedCount} saved filter${unresolvedCount !== 1 ? "s" : ""} reference column${unresolvedCount !== 1 ? "s" : ""} not found in this dataset — check before running.`,
        "error", 7000
      );
    }
  }, 400);
}

function clearFilterRuleCards() {
  const c = document.getElementById("tablesContainer");
  if (c) c.innerHTML = "";
  if (typeof totalTables !== "undefined") window.totalTables = 0;
  if (typeof fpClearAllChips === "function") fpClearAllChips();
  _updateValTrigger();
}

// ── Upload ────────────────────────────────────────────────────────────────────
function initUploadZone() {
  const zone  = document.getElementById("uploadZone");
  const input = document.getElementById("fileInput");
  zone.addEventListener("click",    e => { if (!e.target.classList.contains("upload-browse")) input.click(); });
  input.addEventListener("change",  () => { if (input.files[0]) handleFile(input.files[0]); });
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drag-over"); });
  zone.addEventListener("dragleave",() => zone.classList.remove("drag-over"));
  zone.addEventListener("drop",     e => { e.preventDefault(); zone.classList.remove("drag-over"); if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
}

async function handleFile(file) {
  if (!state.activeProject) { showToast("Select a project first.", "error"); return; }
  const ext = file.name.split(".").pop().toLowerCase();
  if (!["xlsx","xls","csv"].includes(ext)) { showToast(`'${file.name}' not supported.`, "error"); return; }
  if (file.size > 500 * 1024 * 1024)       { showToast("File exceeds 500 MB.",          "error"); return; }
  showLoader("Uploading file…");
  try {
    const res = await uploadFile(file);
    state.fileId   = res.file_id;
    state.fileName = res.file_name;
    state.rowCount = res.row_count;
    state.columns  = res.columns;
    state.autoDtypes = res.dtypes || {}; // backend-detected per-column types, precomputed at upload
    window.allColumns  = res.columns.map(c => ({ col1: c, values: [] }));
    window.allColumns2 = [];
    state.dataset2FileId = null; state.dataset2FileName = null;
    if (typeof _renderDataset2Slot === "function") _renderDataset2Slot();
    if (typeof syncColumns === "function") syncColumns();
    renderUploadSuccess(res);
    setStatus("active", "File loaded");
    if (typeof _populateCleanUuidSelect     === "function") _populateCleanUuidSelect(res.columns);
    if (typeof _populateStdColumnSelectClean === "function") _populateStdColumnSelectClean(res.columns);
    showToast(`${res.file_name} — ${res.row_count.toLocaleString()} rows.`, "success");
    if (typeof _fpRenderColGrid === "function") _fpRenderColGrid();
    if (state.activeProject?.filters?.length)
      setTimeout(() => restoreFiltersIntoRuleCards(state.activeProject.filters), 200);
  } catch (err) {
    showToast(err.message, "error");
    setStatus("error", "Upload failed");
  } finally {
    hideLoader();
  }
}

// ── Dataset 2 (Cross Check / Double Cross Check) ────────────────────────────
// Uploaded ONCE, independently of the main file, from the persistent
// "+ Dataset 2" slot in the file card on the main dataset page — never
// re-uploaded per filter, and never prompted for from inside a filter's
// modal (that used to imply each Cross Check needed its own upload; it
// didn't, but showing an upload button there made it look like it did).
// Every Cross Check / Double Cross Check filter reuses the same Dataset 2
// for the rest of the session. Never cleaned or run through the pipeline
// itself; reuses the same generic upload endpoint the primary file uses
// (POST /api/upload/ doesn't care which "slot" a file is for).
let _dataset2Input = null;

function triggerDataset2Upload() {
  if (!_dataset2Input) {
    _dataset2Input = document.createElement("input");
    _dataset2Input.type = "file";
    _dataset2Input.accept = ".csv,.xlsx,.xls";
    _dataset2Input.style.display = "none";
    document.body.appendChild(_dataset2Input);
  }
  _dataset2Input.onchange = () => {
    if (_dataset2Input.files[0]) handleFile2(_dataset2Input.files[0]);
    _dataset2Input.value = "";
  };
  _dataset2Input.click();
}
window.triggerDataset2Upload = triggerDataset2Upload;

async function handleFile2(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  if (!["xlsx", "xls", "csv"].includes(ext)) { showToast(`'${file.name}' not supported.`, "error"); return; }
  if (file.size > 500 * 1024 * 1024)         { showToast("File exceeds 500 MB.", "error"); return; }
  showLoader("Uploading Dataset 2…");
  try {
    const res = await uploadFile(file);
    state.dataset2FileId   = res.file_id;
    state.dataset2FileName = res.file_name;
    state.dataset2RowCount = res.row_count;
    window.allColumns2 = res.columns.map(c => ({ col1: c, values: [] }));
    showToast(`Dataset 2 loaded — ${res.file_name} (${res.row_count.toLocaleString()} rows). Reusable across every Cross Check filter.`, "success");
    _renderDataset2Slot();
    // Re-render every cross/doublecross rule card now that allColumns2 is
    // populated, since Dataset 2 is shared across all Cross Check / Double
    // Cross Check filters in this session, not per-filter.
    document.querySelectorAll(".rule-card").forEach(box => {
      const m = box.id && box.id.match(/tableBox_(\d+)/);
      if (!m) return;
      const id = parseInt(m[1]);
      const cond = document.getElementById(`condSelect_${id}`)?.value;
      if (cond === "cross" || cond === "doublecross" || cond === "compare2") {
        if (typeof handleConditionUI === "function") handleConditionUI(id);
      }
    });
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    hideLoader();
  }
}
window.handleFile2 = handleFile2;

function clearDataset2() {
  state.dataset2FileId = null; state.dataset2FileName = null; state.dataset2RowCount = null;
  window.allColumns2 = [];
  _renderDataset2Slot();
  document.querySelectorAll(".rule-card").forEach(box => {
    const m = box.id && box.id.match(/tableBox_(\d+)/);
    if (!m) return;
    const id = parseInt(m[1]);
    const cond = document.getElementById(`condSelect_${id}`)?.value;
    if (cond === "cross" || cond === "doublecross" || cond === "compare2") {
      if (typeof handleConditionUI === "function") handleConditionUI(id);
    }
  });
  showToast("Dataset 2 cleared.", "success");
}
window.clearDataset2 = clearDataset2;

// Renders the persistent Dataset 2 slot in the file card — either the
// "+ Dataset 2" upload button, or a loaded chip with the filename and a
// clear/re-upload control, reflecting state.dataset2FileId at all times.
function _renderDataset2Slot() {
  const slot = document.getElementById("fileCardDs2");
  if (!slot) return;
  if (state.dataset2FileId) {
    slot.innerHTML =
      `<span class="ds2-loaded" title="${escapeHtml(state.dataset2FileName || "")} — ${(state.dataset2RowCount || 0).toLocaleString()} rows">` +
      `<span class="ds2-loaded-name">${escapeHtml(state.dataset2FileName || "Dataset 2")}</span>` +
      `<button type="button" class="ds2-clear-btn" title="Remove Dataset 2" onclick="clearDataset2()">×</button>` +
      `</span>`;
  } else {
    slot.innerHTML =
      `<button type="button" class="btn-outline btn-sm" id="ds2UploadBtn" ` +
      `onclick="if (typeof triggerDataset2Upload === 'function') triggerDataset2Upload()" ` +
      `title="Upload a second dataset once — every Cross Check / Double Cross Check validation filter reuses it, no need to upload it again per filter">` +
      `+ Dataset 2</button>`;
  }
}
window._renderDataset2Slot = _renderDataset2Slot;

function renderUploadSuccess(res) {
  document.getElementById("uploadZone").style.display  = "none";
  document.getElementById("fileCard").style.display    = "flex";
  document.getElementById("fileCardName").textContent  = res.file_name;
  document.getElementById("fileCardStats").textContent = `${res.row_count.toLocaleString()} rows · ${res.columns.length} columns`;
  document.getElementById("topbarFile").style.display  = "flex";
  document.getElementById("topbarFilename").textContent = res.file_name;
  document.getElementById("clearBtn").style.display    = "inline-flex";
  document.getElementById("sidebarFileChip").style.display = "flex";
  document.getElementById("sidebarFilename").textContent   = truncate(res.file_name, 22);
  document.getElementById("configGrid").style.display = "grid";
  document.getElementById("runBar").style.display     = "flex";
  // A previously-selected UUID/ID column belongs to whatever file was
  // uploaded before this one. If it happens to share a column name with
  // the new file it's harmless to keep; if it doesn't, leaving it in
  // state.uuidColumn silently sends an invalid column name to the backend
  // on "Run Pipeline" — the dropdown itself shows "— none —" (the browser
  // can't select a <select> to a value with no matching <option>), so
  // there's no visual sign anything is wrong, but the pipeline quietly
  // falls back to synthetic ROW_N row identifiers instead of the real ID
  // column, because state.uuidColumn was never actually cleared. Reset it
  // unless the new file genuinely has a column with the same name.
  if (!res.columns.includes(state.uuidColumn)) {
    state.uuidColumn = null;
  }
  _populateUuidSelect(res.columns);
  buildRecoPanel(res.columns);
}

function selectType(el, type) {
  document.querySelectorAll(".type-tile").forEach(t => t.classList.remove("selected"));
  el.classList.add("selected");
  state.dataType = type;
  const ipGroup = document.getElementById("ipNameGroup");
  ipGroup.style.display = REQUIRES_IP[type] ? "flex" : "none";
  if (!REQUIRES_IP[type]) { document.getElementById("ipNameInput").value = ""; state.ipName = null; }
  _syncRunButton();
  saveProjectState();
}

function onIpNameInput(v) { state.ipName = v.trim() || null; _syncRunButton(); saveProjectState(); }
function onUuidColumnChange(v) { state.uuidColumn = v || null; }

function _populateUuidSelect(cols) {
  const sel = document.getElementById("uuidColumnSelect");
  if (!sel) return;
  sel.innerHTML = `<option value="">— none —</option>` +
    cols.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  sel.value = state.uuidColumn || "";
}

function _syncRunButton() {
  const btn     = document.getElementById("runPipelineBtn"); if (!btn) return;
  const needsIp = state.dataType && REQUIRES_IP[state.dataType];
  const ready   = state.fileId && state.dataType && (!needsIp || state.ipName);
  btn.disabled  = !ready;
  const fc = document.querySelectorAll("#tablesContainer .rule-card").length;
  document.getElementById("runBarTitle").textContent =
    ready ? `Ready — ${state.dataType}${state.ipName ? " / " + state.ipName : ""}` : "Configure cleaning type above";
  document.getElementById("runBarSub").textContent =
    ready
      ? (fc ? `${fc} validation filter${fc !== 1 ? "s" : ""} configured` : "No validation filters — all rows will pass.")
      : "Select a dataset type then click Run Pipeline.";
}

// ── Validation popup ──────────────────────────────────────────────────────────
function openValPopup() {
  window.allColumns  = state.columns.map(c => ({ col1: c, values: [] }));
  // Dataset 2 (if loaded via the Cross Check / Double Cross Check pill)
  // persists across popup opens — it's independent of the main file and
  // shouldn't need re-uploading just because the popup was closed.
  if (!Array.isArray(window.allColumns2)) window.allColumns2 = [];
  if (typeof syncColumns      === "function") syncColumns();
  if (typeof _fpRenderColGrid === "function") _fpRenderColGrid();
  // _fpRenderColGrid() rebuilds the column-card grid from scratch (wipes
  // every chip badge), but the underlying .rule-card elements in
  // #tablesContainer are untouched — any filters configured in a previous
  // open of this popup still exist, they just have no visual chip on their
  // column anymore. Re-stamp them now, or the popup looks empty even when
  // filters are actually applied (and the still-applied filters never show
  // up assigned to their columns until a full page reload).
  if (typeof window.fpReflectAllRules === "function") window.fpReflectAllRules();
  document.getElementById("valPopupOverlay").classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeValPopup() {
  document.getElementById("valPopupOverlay").classList.remove("open");
  document.body.style.overflow = "";
  _updateValTrigger();
  saveProjectState();
}
function clearAllFilters() {
  if (!confirm("Clear all validation filters?")) return;
  clearFilterRuleCards();
  _updateValTrigger();
  saveProjectState();
}
function _updateValTrigger() {
  const count = document.querySelectorAll("#tablesContainer .rule-card").length;
  const badge = document.getElementById("valTriggerBadge");
  const note  = document.getElementById("valFilterNote");
  const btn   = document.getElementById("valTriggerBtn");
  const cEl   = document.getElementById("fpFilterCountNum");
  const runFiltersBtn = document.getElementById("valRunFiltersBtn");
  if (runFiltersBtn) {
    runFiltersBtn.disabled = count === 0;
    runFiltersBtn.title = count === 0
      ? "Add at least one validation filter first"
      : "Run the currently configured validation filters against the cleaned dataset and highlight failures";
  }
  if (cEl) cEl.textContent = count;
  if (count > 0) {
    badge.textContent = count; badge.style.display = "inline";
    note.textContent  = `${count} filter${count !== 1 ? "s" : ""} · flagging enabled`;
    btn.classList.add("has-filters");
  } else {
    badge.style.display = "none";
    note.textContent    = "No filters";
    btn.classList.remove("has-filters");
  }
  _syncRunButton();
}

// ── Column reco (cleaning) ────────────────────────────────────────────────────
//
// buildRecoPanel() used to SEED state.columnRules/caseStyles by guessing from
// column names (RULE_CATALOG[k].detect) on every fresh dataset. That's been
// removed: Column Rules should behave exactly like Validation Filters —
// start empty for a project with nothing configured, and reflect ONLY what
// was previously saved for this project (see restoreColumnRulesFromProject),
// never an auto-guess. The RULE_CATALOG `detect` functions are kept (unused)
// in case a future "suggest rules" *opt-in* action wants them, but they no
// longer run implicitly.
function buildRecoPanel(columns) {
  if (state.activeProject && _hasSavedColumnRules(state.activeProject)) {
    restoreColumnRulesFromProject(columns, state.activeProject);
  } else {
    state.columnRules = {};
    state.caseStyles  = {};
    state.dtypeRules  = {};
    for (const col of columns) state.columnRules[col] = [];
    state.globalRules = (state.activeProject?.globalRules && Object.keys(state.activeProject.globalRules).length)
      ? JSON.parse(JSON.stringify(state.activeProject.globalRules))
      : { trim: true, null: true, special: true };
  }

  renderPalette();
  renderRecoColumns();
  _syncGlobalRuleChips();
  document.getElementById("recoTriggerBar").style.display = "block";
  updateRecoTriggerBadge();
}

function _hasSavedColumnRules(proj) {
  const cr = proj.columnRules;
  return !!(cr && Object.values(cr).some(rules => Array.isArray(rules) && rules.length > 0));
}

// Rebuild state.columnRules/caseStyles/dtypeRules/regexRules/globalRules from
// a saved project, matching by CURRENT column name (mirrors
// _resolveColIdxByName's rationale: columns can be reordered/renamed between
// saves, so name match is primary, and anything unresolved is simply
// dropped rather than mis-applied to the wrong column).
function restoreColumnRulesFromProject(columns, proj) {
  const norm = s => String(s || "").trim().toLowerCase();
  // Map normalized-name -> actual current column name, so a saved rule for
  // " District " or "district" still matches today's "District" column.
  const byNorm = new Map(columns.map(c => [norm(c), c]));

  state.columnRules = {};
  state.caseStyles  = {};
  state.dtypeRules  = {};
  for (const col of columns) state.columnRules[col] = [];

  for (const [savedCol, rules] of Object.entries(proj.columnRules || {})) {
    const col = byNorm.get(norm(savedCol));
    if (col && Array.isArray(rules)) state.columnRules[col] = [...rules];
  }
  for (const [savedCol, style] of Object.entries(proj.caseStyles || {})) {
    const col = byNorm.get(norm(savedCol));
    if (col) state.caseStyles[col] = style;
  }
  for (const [savedCol, dtype] of Object.entries(proj.dtypeRules || {})) {
    const col = byNorm.get(norm(savedCol));
    if (col) state.dtypeRules[col] = dtype;
  }
  if (proj.regexRules) {
    state.regexRules = {};
    for (const [savedCol, cfg] of Object.entries(proj.regexRules)) {
      const col = byNorm.get(norm(savedCol));
      if (col) state.regexRules[col] = cfg;
    }
  }
  state.globalRules = proj.globalRules && Object.keys(proj.globalRules).length
    ? JSON.parse(JSON.stringify(proj.globalRules))
    : { trim: true, null: true, special: true };
}

// Reflect state.globalRules onto the toggle chips + any custom global bubbles
// in the reco modal header — needed after a restore, since those chips
// default to "active" in the static HTML regardless of saved state.
function _syncGlobalRuleChips() {
  const bar = document.getElementById("recoGlobalsBar");
  if (!bar) return;
  bar.querySelectorAll(".rule-chip-global").forEach(chip => {
    const key = chip.dataset.global;
    const on  = state.globalRules?.[key] !== false;
    chip.classList.toggle("active",   on);
    chip.classList.toggle("inactive", !on);
  });
  bar.querySelectorAll(".rule-chip-global-custom").forEach(c => c.remove());
  const hint = bar.querySelector(".reco-globals-hint");
  for (const ruleKey of Object.keys(state.globalRules?.custom || {})) {
    const rule = RULE_CATALOG[ruleKey];
    if (!rule) continue;
    const chip = document.createElement("span");
    chip.className = "rule-chip-global-custom";
    chip.dataset.globalCustom = ruleKey;
    chip.innerHTML = `${escapeHtml(rule.label)} <span class="chip-x" title="Remove" onclick="_removeGlobalCustomRule(event,'${ruleKey}')">×</span>`;
    if (hint) bar.insertBefore(chip, hint); else bar.appendChild(chip);
  }
}
function openRecoModal()  { _initGlobalsDropZone(); const m = document.getElementById("recoModal");  if (m) { m.style.display = "flex"; document.body.style.overflow = "hidden"; } }
function closeRecoModal() {
  const m = document.getElementById("recoModal");
  if (m) { m.style.display = "none"; document.body.style.overflow = ""; }
  // Persist whatever column rules / global rules / datatype checks were
  // configured while the modal was open, so a saved project reapplies them
  // exactly next time instead of falling back to a fresh name-based guess.
  if (typeof saveFiltersToProject === "function") saveFiltersToProject(true);
}
function onRecoOverlayClick(e) { if (e.target === document.getElementById("recoModal")) closeRecoModal(); }
function updateRecoTriggerBadge() {
  const b    = document.getElementById("recoTriggerCount"); if (!b) return;
  const note = document.getElementById("recoFilterNote");
  const btn  = document.getElementById("recoTriggerBtn");
  const n    = state.columns.filter(c => (state.columnRules[c] || []).length > 0).length;
  b.textContent = n ? `${n} rules` : ""; b.style.display = n ? "inline-flex" : "none";
  if (note) note.textContent = n ? `${n} column${n !== 1 ? "s" : ""} configured` : "No rules";
  if (btn)  btn.classList.toggle("has-filters", n > 0);
}
function renderPalette() {
  const list = document.getElementById("paletteList"); if (!list) return;
  list.innerHTML = Object.entries(RULE_CATALOG).map(([k, r]) => _bubbleHTML(k, r, null)).join("");
  list.addEventListener("dragover",  e => { if (_dragState?.fromCol) { e.preventDefault(); list.classList.add("drop-over"); } });
  list.addEventListener("dragleave", e => { if (!list.contains(e.relatedTarget)) list.classList.remove("drop-over"); });
  list.addEventListener("drop", e => {
    e.preventDefault(); list.classList.remove("drop-over");
    if (!_dragState?.fromCol) return;
    const { ruleKey, fromCol } = _dragState; _dragState = null;
    state.columnRules[fromCol] = (state.columnRules[fromCol] || []).filter(k => k !== ruleKey);
    renderRecoColumns();
  });
  _bindBubbleDrag(list);
}
function renderRecoColumns() {
  const container = document.getElementById("recoColList"); if (!container) return;
  const cl = document.getElementById("recoColCountLabel");
  if (cl) {
    const n = state.columns.filter(c => (state.columnRules[c] || []).length > 0).length;
    cl.textContent = `${state.columns.length} columns · ${n} with rules`;
  }
  container.innerHTML = state.columns.map(col => {
    const rules = state.columnRules[col] || [];
    const cs    = escapeHtml(col);
    const badge = rules.length ? `${rules.length} rule${rules.length > 1 ? "s" : ""}` : "no rules";
    const bubbles = rules.map(k => { const r = RULE_CATALOG[k]; return r ? _bubbleHTML(k, r, col) : ""; }).join("");
    return `<div class="col-rule-card${rules.length ? "" : " is-empty"}" data-col="${cs}">
      <div class="col-rule-head">
        <span class="col-rule-name" title="${cs}">${cs}</span>
        <span class="col-rule-badge${rules.length ? " has-rules" : ""}">${badge}</span>
      </div>
      <div class="col-rule-zone">${bubbles}<div class="col-drop-hint">+ drop rule here</div></div>
    </div>`;
  }).join("");
  container.querySelectorAll(".col-rule-card").forEach(card => {
    const col  = card.dataset.col;
    const zone = card.querySelector(".col-rule-zone");
    zone.addEventListener("dragover",  e => { e.preventDefault(); card.classList.add("drop-over"); });
    zone.addEventListener("dragleave", e => { if (!card.contains(e.relatedTarget)) card.classList.remove("drop-over"); });
    zone.addEventListener("drop", e => {
      e.preventDefault(); card.classList.remove("drop-over");
      if (!_dragState) return;
      const { ruleKey, fromCol } = _dragState; _dragState = null;
      if (fromCol && fromCol !== col)
        state.columnRules[fromCol] = (state.columnRules[fromCol] || []).filter(k => k !== ruleKey);
      if (ruleKey === "DATATYPE_CHECK") { _openDatatypePopup(col); return; }
      if (!(state.columnRules[col] || []).includes(ruleKey)) {
        if (ruleKey === "REGEX_CLEAN") { _promptRegexRule(col); }
        else if (RULE_CATALOG[ruleKey]?.needsStyle) { _promptCaseStyleRule(col); }
        else { state.columnRules[col] = [...(state.columnRules[col] || []), ruleKey]; renderRecoColumns(); }
      } else { renderRecoColumns(); }
    });
  });
  _bindBubbleDrag(container);
  updateRecoTriggerBadge();
  _debouncedSaveColumnRules();
}

// Debounced so rapid successive drags (e.g. datatype "All columns" applying
// to 40+ columns) don't fire 40 separate saves — but every real mutation to
// columnRules/caseStyles/globalRules/dtypeRules still ends up persisted
// within ~600ms, instead of relying solely on the modal's "Done" click.
let _saveRulesTimer = null;
function _debouncedSaveColumnRules() {
  if (!state.activeProject) return;
  clearTimeout(_saveRulesTimer);
  _saveRulesTimer = setTimeout(() => {
    if (typeof saveFiltersToProject === "function") saveFiltersToProject(true);
  }, 600);
}
function _bindBubbleDrag(root) {
  root.querySelectorAll(".rule-bubble[draggable]").forEach(el => {
    el.addEventListener("dragstart", e => { _dragState = { ruleKey: el.dataset.rule, fromCol: el.dataset.col || null }; el.classList.add("is-dragging"); e.dataTransfer.effectAllowed = "move"; });
    el.addEventListener("dragend",   () => { el.classList.remove("is-dragging"); _dragState = null; });
  });
}
function _bubbleHTML(key, rule, col) {
  const isCol  = col !== null;
  const ca     = isCol ? ` data-col="${escapeHtml(col)}"` : "";
  const xBtn   = isCol ? `<button class="rule-bubble-x" onclick="removeRule(event,'${escapeHtml(col)}','${key}')" title="Remove">×</button>` : "";
  let hints    = rule.hints.join(" · ");
  let clickAttr = "";
  if (key === "TITLE_CASED" && isCol) {
    const style = state.caseStyles[col] || "title";
    hints = CASE_STYLE_LABELS[style] || hints;
    clickAttr = ` onclick="_promptCaseStyleRule('${escapeHtml(col)}')" style="cursor:pointer"`;
  }
  if (key === "DATATYPE_CHECK" && isCol) {
    const chosen = state.dtypeRules[col];
    hints = chosen ? `checking: ${DTYPE_LABELS[chosen] || chosen}` : "click to choose type";
    clickAttr = ` onclick="_openDatatypePopup('${escapeHtml(col)}')" style="cursor:pointer"`;
  }
  if (key === "REGEX_CLEAN" && isCol && state.regexRules?.[col]) {
    const cfg = state.regexRules[col];
    if (cfg.mapping) {
      const n = Object.keys(cfg.mapping).length;
      hints = n ? `${n} value${n !== 1 ? "s" : ""} mapped` : "no changes";
    } else {
      hints = cfg.auto ? "auto-cluster" : (`/${cfg.pattern || ""}/ → ${cfg.replacement || "(empty)"}`);
    }
  }
  return `<div class="rule-bubble${isCol ? " col-bubble" : ""}" draggable="true" data-color="${rule.color}" data-rule="${key}"${ca}${isCol ? "" : " data-mode='palette'"}${clickAttr}><div class="rule-bubble-inner"><span class="rule-bubble-label">${escapeHtml(rule.label)}</span><span class="rule-bubble-hints">${escapeHtml(hints)}</span></div>${xBtn}</div>`;
}
function removeRule(e, col, k) {
  e.stopPropagation();
  state.columnRules[col] = (state.columnRules[col] || []).filter(x => x !== k);
  if (k === "REGEX_CLEAN" && state.regexRules) delete state.regexRules[col];
  if (k === "TITLE_CASED" && state.caseStyles) delete state.caseStyles[col];
  if (k === "DATATYPE_CHECK" && state.dtypeRules) delete state.dtypeRules[col];
  renderRecoColumns();
  if (typeof saveFiltersToProject === "function") saveFiltersToProject(true);
}

// ── Global rule toggles ───────────────────────────────────────────────────────
function toggleGlobalRule(chip, key) {
  state.globalRules[key] = !state.globalRules[key];
  chip.classList.toggle("active",   state.globalRules[key]);
  chip.classList.toggle("inactive", !state.globalRules[key]);
  _debouncedSaveColumnRules();
}
function _initGlobalsDropZone() {
  const bar = document.getElementById("recoGlobalsBar");
  if (!bar || bar._dropBound) return;
  bar._dropBound = true;
  bar.addEventListener("dragover",  e => { if (!_dragState || _dragState.fromCol) return; e.preventDefault(); bar.classList.add("drop-over"); });
  bar.addEventListener("dragleave", e => { if (!bar.contains(e.relatedTarget)) bar.classList.remove("drop-over"); });
  bar.addEventListener("drop", e => {
    e.preventDefault(); bar.classList.remove("drop-over");
    if (!_dragState || _dragState.fromCol) return;
    const { ruleKey } = _dragState; _dragState = null;
    _addGlobalCustomRule(ruleKey);
  });
}
function _addGlobalCustomRule(ruleKey) {
  const rule = RULE_CATALOG[ruleKey]; if (!rule) return;
  if (rule.isDatatype) { _applyDatatypeAllColumns(); return; }
  const bar  = document.getElementById("recoGlobalsBar");
  if (bar.querySelector(`[data-global-custom="${ruleKey}"]`)) { showToast(`"${rule.label}" is already applied to all columns.`, "info"); return; }
  if (!state.globalRules.custom) state.globalRules.custom = {};
  state.globalRules.custom[ruleKey] = true;
  const chip = document.createElement("span");
  chip.className = "rule-chip-global-custom";
  chip.dataset.globalCustom = ruleKey;
  chip.innerHTML = `${escapeHtml(rule.label)} <span class="chip-x" title="Remove" onclick="_removeGlobalCustomRule(event,'${ruleKey}')">×</span>`;
  const hint = bar.querySelector(".reco-globals-hint");
  bar.insertBefore(chip, hint);
  showToast(`"${rule.label}" will be applied to all columns during pipeline.`, "success");
  _debouncedSaveColumnRules();
}
// ── Datatype filter ────────────────────────────────────────────────────────────
//
// Two drop targets, per the "Datatype" bubble in the palette:
//   • Dropped on "All columns" bar   → auto-detect every column's type from
//     the backend's pre-computed cache (see /api/upload/{file_id}/dtypes —
//     already scanned at upload time, so this is normally an instant read).
//   • Dropped on a single column card → open a popup asking which datatype
//     to check that column against (see _openDatatypePopup below).
async function _applyDatatypeAllColumns() {
  if (!state.fileId) { showToast("Upload a file first.", "error"); return; }
  let dtypes = state.autoDtypes;
  if (!dtypes || Object.keys(dtypes).length === 0) {
    try {
      const res = await apiDetectDtypes(state.fileId);
      dtypes = res.dtypes || {};
      state.autoDtypes = dtypes;
    } catch (e) { showToast("Datatype detection failed: " + e.message, "error"); return; }
  }
  let applied = 0;
  for (const col of state.columns) {
    const det = dtypes[col];
    if (!det || det.type === "unknown") continue;
    state.dtypeRules[col] = det.type;
    if (!(state.columnRules[col] || []).includes("DATATYPE_CHECK")) {
      state.columnRules[col] = [...(state.columnRules[col] || []), "DATATYPE_CHECK"];
    }
    applied++;
  }
  renderRecoColumns();
  showToast(`Auto-detected datatypes for ${applied} column${applied !== 1 ? "s" : ""}.`, "success");
  if (typeof saveFiltersToProject === "function") saveFiltersToProject(true);
}

const DTYPE_OPTIONS = ["integer", "decimal", "boolean", "email", "phone", "cnic", "iban", "date", "text"];

function _openDatatypePopup(col) {
  let overlay = document.getElementById("dtypePopupOverlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "dtypePopupOverlay";
    overlay.className = "modal-overlay";
    overlay.style.zIndex = "700";
    overlay.innerHTML = `
      <div class="modal" style="max-width:360px">
        <div class="modal-header">
          <h3 class="modal-title">Check datatype — <span id="dtypePopupColName" style="color:var(--accent)"></span></h3>
          <button class="modal-close" onclick="document.getElementById('dtypePopupOverlay').style.display='none'">
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 5l10 10M15 5L5 15" stroke-linecap="round"/></svg>
          </button>
        </div>
        <div class="modal-body">
          <p class="modal-desc" style="margin-bottom:10px">What datatype should this column be checked for?</p>
          <select class="select-input" id="dtypePopupSelect"></select>
          <p id="dtypePopupHint" style="font-size:12px;color:var(--text-4);margin-top:8px"></p>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" onclick="document.getElementById('dtypePopupOverlay').style.display='none'">Cancel</button>
          <button class="btn-primary" onclick="_confirmDatatypePopup()">Apply</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("mousedown", e => { if (e.target === overlay) overlay.style.display = "none"; });
  }
  document.getElementById("dtypePopupColName").textContent = col;
  const sel = document.getElementById("dtypePopupSelect");
  const suggested = state.autoDtypes?.[col]?.type;
  sel.innerHTML = DTYPE_OPTIONS.map(t =>
    `<option value="${t}"${t === (state.dtypeRules[col] || suggested) ? " selected" : ""}>${DTYPE_LABELS[t]}</option>`
  ).join("");
  const hint = document.getElementById("dtypePopupHint");
  hint.textContent = suggested
    ? `Auto-detect suggests "${DTYPE_LABELS[suggested] || suggested}" for this column.`
    : "";
  overlay.dataset.col = col;
  overlay.style.display = "flex";
}

function _confirmDatatypePopup() {
  const overlay = document.getElementById("dtypePopupOverlay");
  const col     = overlay.dataset.col;
  const type    = document.getElementById("dtypePopupSelect").value;
  state.dtypeRules[col] = type;
  if (!(state.columnRules[col] || []).includes("DATATYPE_CHECK")) {
    state.columnRules[col] = [...(state.columnRules[col] || []), "DATATYPE_CHECK"];
  }
  overlay.style.display = "none";
  renderRecoColumns();
  showToast(`"${col}" will be checked as ${DTYPE_LABELS[type]}.`, "success");
  if (typeof saveFiltersToProject === "function") saveFiltersToProject(true);
}
window._openDatatypePopup   = _openDatatypePopup;
window._confirmDatatypePopup = _confirmDatatypePopup;

function _removeGlobalCustomRule(e, ruleKey) {
  e.stopPropagation();
  const bar  = document.getElementById("recoGlobalsBar");
  const chip = bar.querySelector(`[data-global-custom="${ruleKey}"]`);
  if (chip) chip.remove();
  if (state.globalRules.custom) delete state.globalRules.custom[ruleKey];
  _debouncedSaveColumnRules();
}

// ── Regex rule prompt ─────────────────────────────────────────────────────────
if (!state.regexRules) state.regexRules = {};

async function _promptRegexRule(col) {
  let overlay = document.getElementById("regexRuleOverlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id        = "regexRuleOverlay";
    overlay.className = "modal-overlay";
    overlay.style.zIndex = "700";
    overlay.innerHTML = `
      <div class="modal modal--wide rrc-modal">
        <div class="modal-header">
          <h3 class="modal-title">Regex Clean — <span id="regexRuleColName" style="color:var(--accent)"></span></h3>
          <button class="modal-close" onclick="document.getElementById('regexRuleOverlay').style.display='none'">
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 5l10 10M15 5L5 15" stroke-linecap="round"/></svg>
          </button>
        </div>
        <div class="modal-body">
          <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--accent-soft);border-radius:var(--r-md);margin-bottom:12px">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="var(--accent)" stroke-width="2" style="flex-shrink:0"><circle cx="10" cy="10" r="8"/><path d="M10 6v4l3 3" stroke-linecap="round"/></svg>
            <span style="font-size:12px;color:var(--accent)">Runs when you click <strong>Run Pipeline</strong> — not right now.</span>
          </div>
          <p class="modal-desc" style="margin-bottom:12px">
            Drag a value into a different group to fix a wrong grouping, edit a target name, remove a value entirely,
            or start a brand-new group from any unique value below.
          </p>
          <div id="regexRulePreviewWrap"><div style="color:var(--text-4);font-size:12px;text-align:center;padding:20px">Analysing column…</div></div>
        </div>
        <div class="modal-footer" style="justify-content:space-between">
          <div style="font-size:11.5px;color:var(--text-3)" id="regexRuleStats"></div>
          <div style="display:flex;gap:8px">
            <button class="btn-ghost" onclick="document.getElementById('regexRuleOverlay').style.display='none'">Cancel</button>
            <button class="btn-primary" id="regexRuleConfirmBtn" onclick="_saveRegexRule()" disabled>Queue for Pipeline</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
  }
  overlay.style.display = "flex";
  overlay.dataset.col   = col;
  document.getElementById("regexRuleColName").textContent   = col;
  document.getElementById("regexRuleStats").textContent     = "";
  document.getElementById("regexRuleConfirmBtn").disabled   = true;
  document.getElementById("regexRulePreviewWrap").innerHTML =
    `<div style="color:var(--text-4);font-size:12px;text-align:center;padding:20px">Analysing column…</div>`;

  _regexRuleClusters = [];

  try {
    const fileId = state.fileId;
    if (!fileId) {
      document.getElementById("regexRulePreviewWrap").innerHTML =
        `<p style="color:var(--text-3);font-size:12px;text-align:center;padding:16px">No uploaded file found. The rule will still be queued.</p>`;
      document.getElementById("regexRuleConfirmBtn").disabled = false;
      return;
    }
    const res = await fetch(`/api/upload/${fileId}/clusters/${encodeURIComponent(col)}`).then(r => r.json());
    const allClusters = res.clusters || [];

    // Keep only clusters worth showing a decision for (>1 member); singletons
    // ("unique" values, nothing to merge) are listed separately below, lower
    // priority, per spec — shown but not requiring action.
    _regexRuleClusters = allClusters.filter(c => c.members.length > 1)
      .map(c => ({ canonical: c.canonical, members: [...c.members] }));
    _regexRuleSingles  = allClusters.filter(c => c.members.length === 1).map(c => c.members[0]);

    document.getElementById("regexRuleStats").textContent =
      `${_regexRuleClusters.length} group${_regexRuleClusters.length !== 1 ? "s" : ""} · ${res.value_count || 0} unique value${(res.value_count||0) !== 1 ? "s" : ""}`;

    // Always render — even with zero auto-detected groups, the user still
    // needs to see every unique value so they can build a cluster manually.
    // (Previously this returned early with a static "nothing detected"
    // message and never rendered the singles tray at all.)
    _renderRegexRuleClusters();
    document.getElementById("regexRuleConfirmBtn").disabled = false;
  } catch (e) {
    document.getElementById("regexRulePreviewWrap").innerHTML =
      `<p style="color:var(--red);font-size:12px;">${escapeHtml(e.message)}</p>`;
  }
}

// Editable cluster state for the upload-time Regex Clean popup. Mutated
// directly by drag/edit/remove interactions; "Queue for Pipeline" sends
// exactly this, not a re-derived auto-clustering.
let _regexRuleClusters = [];
let _regexRuleSingles  = [];

function _renderRegexRuleClusters() {
  const wrap = document.getElementById("regexRulePreviewWrap");
  if (!wrap) return;

  const groupsHtml = _regexRuleClusters.map((c, ci) => `
    <div class="rrc-cluster" data-cluster-idx="${ci}">
      <div class="rrc-cluster-head">
        <input type="text" class="rrc-target" value="${escapeAttr(c.canonical)}" data-cluster-idx="${ci}"
               placeholder="Target value" />
        <button type="button" class="rrc-discard" data-cluster-idx="${ci}" title="Discard this group">✕</button>
      </div>
      <div class="rrc-dropzone" data-cluster-idx="${ci}">
        ${c.members.map(v => `
          <span class="rrc-bubble" draggable="true" data-cluster-idx="${ci}" data-value="${escapeAttr(v)}">
            ${escapeHtml(v)}
            <button type="button" class="rrc-bubble-remove" data-cluster-idx="${ci}" data-value="${escapeAttr(v)}" title="Remove">✕</button>
          </span>
        `).join("")}
        ${c.members.length === 0 ? `<span class="rrc-dropzone-hint">Drag values here</span>` : ""}
      </div>
    </div>`).join("");

  // "+ New Group" — lets the user start an empty cluster and drag any
  // unique value (or a value from another group) into it. Always shown,
  // independent of whether any auto-detected groups exist.
  const newGroupHtml = `
    <button type="button" class="rrc-new-group" id="rrcNewGroupBtn">+ New Group</button>`;

  // Groups and singles each get their own independent scroll area instead
  // of one long page — the groups list can get tall with many clusters, and
  // the unique-values tray can have hundreds of bubbles; scrolling them
  // separately keeps both usable without the whole modal scrolling as one
  // unwieldy column.
  wrap.innerHTML = `
    <div class="rrc-groups-scroll">${groupsHtml}</div>
    ${newGroupHtml}
    <div class="rrc-singles-label">Unique values (no grouping suggested) — drag any of these into a group above, or "+ New Group" to start one</div>
    <div class="rrc-dropzone rrc-dropzone--singles rrc-singles-scroll" data-cluster-idx="singles">
      ${_regexRuleSingles.map(v => `
        <span class="rrc-bubble rrc-bubble--single" draggable="true" data-cluster-idx="singles" data-value="${escapeAttr(v)}">
          ${escapeHtml(v)}
          <button type="button" class="rrc-bubble-remove" data-cluster-idx="singles" data-value="${escapeAttr(v)}" title="Remove">✕</button>
        </span>
      `).join("")}
      ${_regexRuleSingles.length === 0 ? `<span class="rrc-dropzone-hint">No unique values</span>` : ""}
    </div>`;

  _bindRegexRuleDragDrop();

  const newGroupBtn = document.getElementById("rrcNewGroupBtn");
  if (newGroupBtn) {
    newGroupBtn.addEventListener("click", () => {
      _regexRuleClusters.push({ canonical: "", members: [] });
      _renderRegexRuleClusters();
      // Focus the new (empty) group's target input so the user can name it immediately.
      const inputs = document.querySelectorAll(".rrc-target");
      const last = inputs[inputs.length - 1];
      if (last) last.focus();
    });
  }
}

// Drag a bubble from any cluster (or the singles tray) into any other
// cluster's dropzone — full manual control, no re-running any algorithm.
function _bindRegexRuleDragDrop() {
  const wrap = document.getElementById("regexRulePreviewWrap");
  if (!wrap) return;

  wrap.querySelectorAll(".rrc-bubble").forEach(el => {
    el.addEventListener("dragstart", e => {
      // Don't start a drag if the gesture began on the bubble's own remove
      // button — that's a click action, not a drag.
      if (e.target.closest(".rrc-bubble-remove")) { e.preventDefault(); return; }
      _regexBubbleDrag = { fromCluster: el.dataset.clusterIdx, value: el.dataset.value };
      el.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    el.addEventListener("dragend", () => { el.classList.remove("is-dragging"); _regexBubbleDrag = null; });
  });

  wrap.querySelectorAll(".rrc-dropzone").forEach(zone => {
    zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drop-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drop-over"));
    zone.addEventListener("drop", e => {
      e.preventDefault();
      zone.classList.remove("drop-over");
      if (!_regexBubbleDrag) return;
      const { fromCluster, value } = _regexBubbleDrag;
      const toCluster = zone.dataset.clusterIdx;
      if (fromCluster === toCluster) return;

      // Remove from source
      if (fromCluster === "singles") {
        _regexRuleSingles = _regexRuleSingles.filter(v => v !== value);
      } else {
        const src = _regexRuleClusters[parseInt(fromCluster)];
        if (src) src.members = src.members.filter(v => v !== value);
      }

      // Add to destination
      if (toCluster === "singles") {
        _regexRuleSingles.push(value);
      } else {
        const dst = _regexRuleClusters[parseInt(toCluster)];
        if (dst) dst.members.push(value);
      }

      // Drop now-empty clusters (but never drop below zero groups silently —
      // just remove the empty one, the user can always discard manually too)
      _regexRuleClusters = _regexRuleClusters.filter(c => c.members.length > 0);

      _renderRegexRuleClusters();
    });
  });
}
let _regexBubbleDrag = null;

document.addEventListener("input", (e) => {
  if (!e.target.classList?.contains("rrc-target")) return;
  const idx = parseInt(e.target.dataset.clusterIdx);
  if (_regexRuleClusters[idx]) _regexRuleClusters[idx].canonical = e.target.value;
});

document.addEventListener("click", (e) => {
  const discardBtn = e.target.closest(".rrc-discard");
  if (discardBtn) {
    const idx = parseInt(discardBtn.dataset.clusterIdx);
    // Discarding a group returns its members to "unique" rather than deleting
    // them outright — nothing the user can see should vanish without a trace.
    const c = _regexRuleClusters[idx];
    if (c) _regexRuleSingles.push(...c.members);
    _regexRuleClusters.splice(idx, 1);
    _renderRegexRuleClusters();
    return;
  }

  // Per-bubble × — removes that single value entirely (it won't be remapped
  // to anything and won't show up anywhere else), distinct from dragging it
  // to another group. Confirms nothing since it's a no-op on the actual
  // dataset until "Queue for Pipeline" is clicked, and easily reversible by
  // just re-opening this popup (it re-reads the column's unique values fresh).
  const removeBtn = e.target.closest(".rrc-bubble-remove");
  if (removeBtn) {
    const clusterIdx = removeBtn.dataset.clusterIdx;
    const value       = removeBtn.dataset.value;
    if (clusterIdx === "singles") {
      _regexRuleSingles = _regexRuleSingles.filter(v => v !== value);
    } else {
      const c = _regexRuleClusters[parseInt(clusterIdx)];
      if (c) c.members = c.members.filter(v => v !== value);
    }
    _renderRegexRuleClusters();
  }
});

function _saveRegexRule() {
  const overlay = document.getElementById("regexRuleOverlay");
  const col     = overlay.dataset.col;
  if (!state.regexRules) state.regexRules = {};

  // Build the final {value: canonical} mapping from exactly what's on
  // screen — every member of every group maps to that group's (possibly
  // user-renamed) canonical. Singletons and the canonical's own bubble
  // (when present in members) are naturally no-ops since value === canonical.
  const mapping = {};
  _regexRuleClusters.forEach(c => {
    const target = (c.canonical || "").trim();
    if (!target) return;
    c.members.forEach(v => { if (v !== target) mapping[v] = target; });
  });

  state.regexRules[col]  = { auto: false, mapping };
  state.columnRules[col] = [...(state.columnRules[col] || []), "REGEX_CLEAN"];
  overlay.style.display  = "none";
  renderRecoColumns();
  showToast(`Auto Regex rule queued for "${col}". Runs during pipeline.`, "success");
}

// ── Case-style rule (upper / lower / title / camel) ────────────────────────────
const CASE_STYLE_LABELS = { title: "Title Case", upper: "UPPER CASE", lower: "lower case", camel: "camelCase" };
const CASE_STYLE_EXAMPLES = { title: "John Doe", upper: "JOHN DOE", lower: "john doe", camel: "johnDoe" };

function _promptCaseStyleRule(col) {
  let overlay = document.getElementById("caseStyleOverlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id        = "caseStyleOverlay";
    overlay.className = "modal-overlay";
    overlay.style.zIndex = "700";
    overlay.innerHTML = `
      <div class="modal">
        <div class="modal-header">
          <h3 class="modal-title">Text Case — <span id="caseStyleColName" style="color:var(--accent)"></span></h3>
          <button class="modal-close" onclick="document.getElementById('caseStyleOverlay').style.display='none'">
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 5l10 10M15 5L5 15" stroke-linecap="round"/></svg>
          </button>
        </div>
        <div class="modal-body">
          <p class="modal-desc" style="margin-bottom:12px">Choose how text in this column should be cased. Runs when you click <strong>Run Pipeline</strong>.</p>
          <div id="caseStyleOptions" style="display:flex;flex-direction:column;gap:8px"></div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
  }
  overlay.dataset.col = col;
  document.getElementById("caseStyleColName").textContent = col;
  const current = state.caseStyles[col] || "title";
  document.getElementById("caseStyleOptions").innerHTML = Object.entries(CASE_STYLE_LABELS).map(([key, label]) => `
    <button type="button" class="btn-ghost${key === current ? " active" : ""}"
      style="justify-content:space-between;display:flex;text-align:left;width:100%"
      onclick="_saveCaseStyleRule('${key}')">
      <span>${label}</span>
      <span style="color:var(--text-4);font-size:12px">${CASE_STYLE_EXAMPLES[key]}</span>
    </button>`).join("");
  overlay.style.display = "flex";
}

function _saveCaseStyleRule(style) {
  const overlay = document.getElementById("caseStyleOverlay");
  const col     = overlay.dataset.col;
  state.caseStyles[col]  = style;
  state.columnRules[col] = [...(state.columnRules[col] || []).filter(k => k !== "TITLE_CASED"), "TITLE_CASED"];
  overlay.style.display  = "none";
  renderRecoColumns();
  showToast(`${CASE_STYLE_LABELS[style]} queued for "${col}". Runs during pipeline.`, "success");
}

// ── Run Pipeline ──────────────────────────────────────────────────────────────
// Master catalog of every step the backend *can* perform, with the config
// check that decides whether it actually ran for THIS run, and a relative
// "weight" used only to split up the real measured time proportionally
// across the steps that did run (never shown for steps that didn't).
//
// "always" steps run every time (file read + write-out). Everything else is
// gated on the same state the user actually configured — global toggle
// chips, per-column rules, the UUID/ID column picker, and validation filters
// built by loadGeneratedFilters().
const PIPELINE_STEP_CATALOG = [
  // NOTE: there used to be a "Validating file" step here, running (and
  // shown as a step) at the start of every pipeline call. It's removed:
  // the file is already parsed and cached at upload time (that's how the
  // upload response can tell you the row/column counts immediately), so by
  // the time you click "Run Pipeline" there's nothing left to validate —
  // the backend just reuses the DataFrame it already has. Showing it as a
  // step implied real re-validation work was happening on every run, which
  // it isn't.
  // Regex/mapping rules run FIRST inside the pipeline now — they operate
  // on raw values before anything else touches them, and are genuinely
  // part of the same tracked step hierarchy as everything below (used to
  // be a separate pre-flight phase with its own client-side timing hack).
  //
  // This order is not a guess — it matches cleaning_engine.py's actual
  // execution sequence step for step: regex → trim → null → special →
  // [cnic → cell → gender → geo → catdate → dtype → unique] → case →
  // dtype_check → date → bank → (validation filters) → write. Keep this in
  // sync with _STEP_GROUP_ORDER and the calls around it in
  // clean_dataframe_fast if that ever changes — a mismatch here is exactly
  // what makes the popup look like steps run "out of order" when really
  // the list just doesn't reflect reality anymore.
  { key: "regex",    label: "Applying regex clean rules",        weight: 5,  test: () => !!(state.regexRules && Object.keys(state.regexRules).length) },
  { key: "trim",     label: "Trimming whitespace",              weight: 3,  test: () => state.globalRules?.trim !== false },
  { key: "null",     label: "Standardising nulls",               weight: 4,  test: () => state.globalRules?.null !== false },
  // Special-char cleaning is now a single dataset-wide pass (same shape as
  // trim/null above), not nested inside geo/catdate per column anymore —
  // it genuinely runs once, right here, for every column in the file.
  { key: "special",  label: "Cleaning special chars",            weight: 4,  test: () => state.globalRules?.special !== false },
  // NOTE: CNIC format-checking and UUID/duplicate-detection are two
  // separate, independently-configured things — kept as separate steps so
  // one doesn't imply the other ran when it didn't.
  { key: "cnic",     label: "Validating CNIC format",            weight: 5,  test: () => _anyColumnRule(["CNIC_FORMAT"]) },
  { key: "cell",     label: "Normalising cell numbers",          weight: 6,  test: () => _anyColumnRule(["CELL_NO_NORMALIZED"]) },
  { key: "gender",   label: "Standardising booleans & gender",   weight: 4,  test: () => _anyColumnRule(["GENDER_STANDARDIZED", "BOOL_STANDARDIZED"]) },
  { key: "geo",      label: "Fuzzy geo-matching",                weight: 12, test: () => _anyColumnRule(["GEO_STANDARDIZED"]) },
  { key: "catdate",  label: "Standardising categories",          weight: 8,  test: () => _anyColumnRule(["CATEGORY_STANDARDIZED"]) },
  { key: "dtype",    label: "Checking numeric/coordinate formats", weight: 4, test: () => Object.values(state.columnRules || {}).some(list => Array.isArray(list) && list.length > 0) },
  { key: "unique",   label: "Checking unique / not-null columns",weight: 5,  test: () => _anyColumnRule(["UNIQUE_VALUES", "NOT_NULL"]) },
  { key: "case",     label: "Applying text casing",              weight: 3,  test: () => _anyColumnRule(["TITLE_CASED"]) },
  { key: "dtype_check", label: "Checking column datatypes",      weight: 4,  test: () => Object.keys(state.dtypeRules || {}).length > 0 },
  { key: "date",     label: "Detecting & standardising dates",   weight: 4,  test: () => _anyColumnRule(["DATE_STANDARDIZED"]) },
  { key: "bank",     label: "Bank canonicalisation",             weight: 7,  test: () => _anyColumnRule(["BANK_STANDARDIZED", "BANK_ACCOUNT_NORMALISED"]) },
  // Duplicate detection is driven purely by whether a UUID/ID column is
  // picked in Settings — independent of CNIC format-checking above.
  // NOTE: there used to be a "Detecting duplicate UUID/ID rows" row here,
  // shown whenever a UUID/ID column was selected (for row-identification
  // purposes) — completely independent of whether the user configured any
  // actual duplicate-check validation filter. Picking an ID column just
  // means "use this to identify rows"; it doesn't mean "check it for
  // duplicates" was requested. The row also never actually completed,
  // since row-level duplicate flagging isn't a separate reportable step —
  // it's a byproduct of resolving the UUID column, computed instantly at
  // pipeline start with nothing to report. Removed: if you want duplicates
  // checked, add a "dup" condition in the Validation Filter builder — that
  // genuinely runs and reports through the filter rows below.
  { key: "write",    label: "Writing parquet output",            weight: 6,  always: true },
];

// Human labels for each validation-filter "cond" type, so the popup names
// the actual filter you configured (e.g. "coords") instead of a generic
// catch-all line. Falls back to a generic label for any cond not listed.
const _VALFILTER_COND_LABELS = {
  coords:       "Validating coordinates",
  date:         "Validating date rules",
  date_eq: "Validating date (exact)", date_neq: "Validating date (not-equal)",
  date_after_eq: "Validating date (on/after)", date_before_eq: "Validating date (on/before)",
  date_nbtwn: "Validating date (out-of-range)", date_weekday: "Validating date (weekday)",
  date_day_eq: "Validating date (day)", date_month_eq: "Validating date (month)",
  date_year_eq: "Validating date (year)", date_year_gt: "Validating date (after year)",
  date_year_lt: "Validating date (before year)",
  compare:      "Comparing columns",
  cross:        "Cross-checking against dataset 2",
  doublecross:  "Cross-checking (double) against dataset 2",
  and:          "Running combined (AND) filter",
  or:           "Running combined (OR) filter",
  dup:          "Checking column-level duplicates",
  eq: "Validating exact-match rule", neq: "Validating not-equal rule",
  gt: "Validating greater-than rule", lt: "Validating less-than rule",
  gte: "Validating minimum-value rule", lte: "Validating maximum-value rule",
  btwn: "Validating in-range rule", nbtwn: "Validating out-of-range rule",
  empty: "Validating required (not-empty) rule", notempty: "Validating not-empty rule",
  regex: "Validating regex pattern", bad_pattern: "Checking for bad patterns",
  contains: "Checking text contains", ncontains: "Checking text doesn't contain",
};
// Grouped by COND (not label text) so live progress events — which report
// their real cond, not a label — can be matched back to the right row
// unambiguously. Multiple filters sharing the same cond still collapse into
// one row with a "(N)" count, exactly as before.
function _validationFilterSteps() {
  const configs = window.allConfigs || [];
  const seen = new Map(); // cond -> count
  configs.forEach(c => {
    const cond = c.cond || "unknown";
    seen.set(cond, (seen.get(cond) || 0) + 1);
  });
  return Array.from(seen.entries()).map(([cond, count]) => {
    const label = _VALFILTER_COND_LABELS[cond] || "Running validation filter";
    return {
      key:   `valfil_${cond}`,
      cond,
      label: count > 1 ? `${label} (${count})` : label,
      weight: 8,
    };
  });
}

// True if any column has been assigned at least one of the given rule keys.
function _anyColumnRule(keys) {
  const cr = state.columnRules || {};
  return Object.values(cr).some(list => Array.isArray(list) && list.some(k => keys.includes(k)));
}

// Steps that actually apply to the current configuration — computed fresh
// right before showing the popup, so it always reflects what's really queued.
let ACTIVE_STEPS = [];
let _progStart = null;

function _computeActiveSteps() {
  const fixed = PIPELINE_STEP_CATALOG.filter(s => s.always || (typeof s.test === "function" && s.test()));
  // Insert the validation-filter steps (one per distinct filter type actually
  // configured) right before the final "Writing parquet output" step.
  const writeIdx = fixed.findIndex(s => s.key === "write");
  const valSteps = _validationFilterSteps();
  const merged = writeIdx === -1 ? [...fixed, ...valSteps] : [...fixed.slice(0, writeIdx), ...valSteps, ...fixed.slice(writeIdx)];
  return merged;
}

function _showProgress() {
  ACTIVE_STEPS = _computeActiveSteps();
  const stepsEl = document.getElementById("cpSteps");
  document.getElementById("cpTitle").textContent = "Running pipeline…";
  stepsEl.innerHTML = ACTIVE_STEPS.map((s, i) =>
    `<div class="cp-step" id="cpStep_${i}">
       <div class="cp-step-icon" id="cpStepIcon_${i}"></div>
       <span class="cp-step-label">${s.label}</span>
       <span class="cp-step-time" id="cpStepTime_${i}"></span>
     </div>`
  ).join("");
  document.getElementById("cpOverlay").style.display = "flex";
  const doneBtn0 = document.getElementById("cpDoneBtn");
  if (doneBtn0) doneBtn0.style.display = "none";
  _progStart = performance.now();
}
function _hideProgress() {
  if (_progEventSource) { _progEventSource.close(); _progEventSource = null; }
  clearInterval(_progTickTimer); _progTickTimer = null;
  document.getElementById("cpOverlay").style.display = "none";
}
// Matches a catalog row to a live progress-event identity from the backend.
function _matchStep(step, identity) {
  if (!identity) return false;
  if (step.key === "write") return identity.type === "stage" && identity.key === "write";
  if (step.key.startsWith("valfil_")) return identity.type === "filter" && identity.cond === step.cond;
  return identity.type === "clean_step" && identity.key === step.key;
}

// Real live progress via GET /api/clean/progress/{file_id} — replaces the
// old weight-based fake ticker entirely. Every number shown while the
// pipeline is running is either a completed real backend timer
// (time.perf_counter() around an actual step) or the live elapsed time on
// the step that is genuinely executing right now — nothing here is
// simulated or estimated.
//
// One honest caveat, by design rather than fabrication: some catalog rows
// (e.g. "geo", "gender") correspond to a cleaning step that runs once PER
// COLUMN, not once overall — the backend processes columns in schema order,
// which interleaves different step types rather than running them
// strictly one-row-at-a-time. So a row can go done → active → done again
// as later columns matching that step type get processed; each transition
// reflects a real backend event, not an animation glitch. The reported
// seconds for a row are always the true cumulative time spent on it so far.
let _progEventSource = null;
let _progTickTimer    = null;

// Real live progress via Server-Sent Events (GET /api/clean/progress-stream)
// — ONE connection for the whole run. The server pushes a message only when
// a step genuinely starts or ends; nothing here repeatedly asks "are you
// done yet?". Between pushes, a step that's currently active just ticks its
// displayed time forward locally from the timestamp of the last push (pure
// visual smoothness — the moment a new push arrives, it's the authority and
// overwrites whatever the local clock guessed). This is exactly "start the
// timer, don't recheck the backend every tick, and let the server tell the
// frontend when to stop" — no more polling loop.
function _pollProgress(fileId) {
  const doneSecondsByRow = new Array(ACTIVE_STEPS.length).fill(0);
  const everSeenByRow    = new Array(ACTIVE_STEPS.length).fill(false);
  const fmt = (s) => s >= 1 ? `${s.toFixed(1)}s` : `${Math.round(s * 1000)}ms`;
  let activeIdx = -1;
  let activeBaseSeconds = 0;   // real seconds already accumulated for the active row
  let activeSince = null;      // performance.now() when the active row last became active

  const render = () => {
    ACTIVE_STEPS.forEach((step, i) => {
      const row  = document.getElementById(`cpStep_${i}`);
      const icon = document.getElementById(`cpStepIcon_${i}`);
      const time = document.getElementById(`cpStepTime_${i}`);
      if (!row) return;
      const isActive = i === activeIdx;
      const isDone   = !isActive && everSeenByRow[i];
      row.className = "cp-step" + (isDone ? " done" : isActive ? " active" : "");
      if (icon) icon.textContent = isDone ? "✓" : "";
      if (!time) return;
      if (isActive) {
        const live = activeSince != null ? (performance.now() - activeSince) / 1000 : 0;
        time.textContent = fmt(activeBaseSeconds + live);
      } else if (isDone) {
        time.textContent = fmt(doneSecondsByRow[i]);
      } else {
        time.textContent = "";
      }
    });
  };

  const applySnapshot = (snap) => {
    if (!snap || !snap.known) return;

    // Recomputed fresh from the full done-list every push (idempotent sum,
    // not incremental client state) — so a missed or out-of-order message
    // can never drift from the real numbers.
    doneSecondsByRow.fill(0);
    for (const entry of (snap.done || [])) {
      ACTIVE_STEPS.forEach((step, i) => {
        if (_matchStep(step, entry)) {
          doneSecondsByRow[i] += entry.seconds || 0;
          everSeenByRow[i] = true;
        }
      });
    }
    activeIdx = ACTIVE_STEPS.findIndex(step => _matchStep(step, snap.current));
    activeBaseSeconds = activeIdx >= 0 ? (snap.elapsed_current || 0) : 0;
    activeSince = activeIdx >= 0 ? performance.now() : null;
    render();

    if (snap.finished) {
      if (_progEventSource) { _progEventSource.close(); _progEventSource = null; }
      if (_progTickTimer)   { clearInterval(_progTickTimer); _progTickTimer = null; }
    }
  };

  // Local visual tick only — never asks the backend anything. Just
  // re-renders the currently-active row's elapsed time smoothly between
  // real push messages; harmless no-op when nothing is active.
  _progTickTimer = setInterval(() => { if (activeIdx >= 0) render(); }, 200);

  if (typeof EventSource !== "undefined") {
    const es = new EventSource(`/api/clean/progress-stream/${encodeURIComponent(fileId)}`);
    _progEventSource = es;
    es.onmessage = (ev) => {
      try { applySnapshot(JSON.parse(ev.data)); } catch (e) { /* ignore malformed frame */ }
    };
    es.onerror = () => {
      // Connection dropped (e.g. proxy hiccup) — EventSource auto-reconnects
      // on its own; nothing to do here except avoid leaving stale UI if it
      // never comes back (the pipeline's own response is still authoritative
      // via _finishProgress() regardless of what this stream did).
    };
  } else {
    // Extremely old browser without EventSource support — fall back to a
    // single one-shot fetch so the popup isn't permanently blank; no
    // repeated polling loop even in this fallback path.
    fetch(`/api/clean/progress/${encodeURIComponent(fileId)}`)
      .then(r => r.json()).then(applySnapshot).catch(() => {});
  }
}
// Called once the real API call resolves. If the backend returned real
// measured per-step timings (result.step_timings — added alongside the
// backend timing instrumentation), each step row shows its ACTUAL wall-clock
// duration: cleaning steps matched by catalog key, validation filter steps
// matched by cond, and file/write from the coarse stage timings. Steps the
// backend didn't measure individually show "—" rather than an invented
// number. Falls back to the old proportional weight split only when the
// response carries no timing data (e.g. an older backend).
function _finishProgress(stepTimings) {
  const totalMs = _progStart ? (performance.now() - _progStart) : 0;

  const fmt = (sec) => sec >= 1 ? `${sec.toFixed(1)}s` : `${Math.round(sec * 1000)}ms`;

  if (stepTimings && (stepTimings.steps || stepTimings.stages || stepTimings.filters)) {
    const steps   = stepTimings.steps   || {};
    const stages  = stepTimings.stages  || {};
    const filters = stepTimings.filters || [];

    // Aggregate real per-filter seconds by cond, mirroring how
    // _validationFilterSteps() collapsed repeated filter types into one row.
    const condSeconds = {};
    filters.forEach(f => {
      const label = _VALFILTER_COND_LABELS[f.cond] || "Running validation filter";
      condSeconds[label] = (condSeconds[label] || 0) + (f.seconds || 0);
    });

    ACTIVE_STEPS.forEach((s, i) => {
      const row  = document.getElementById(`cpStep_${i}`);
      const icon = document.getElementById(`cpStepIcon_${i}`);
      const time = document.getElementById(`cpStepTime_${i}`);
      if (!row) return;

      let sec = null;
      if (s.key === "write") {
        sec = stages.write ?? null;
      } else if (s.key.startsWith("valfil_")) {
        const baseLabel = s.label.replace(/ \(\d+\)$/, "");
        sec = condSeconds[baseLabel] ?? null;
      } else if (steps[s.key] != null) {
        sec = steps[s.key];
      }
      // A checkmark claims "this step ran" — only show one when there's
      // real backend data proving it did. A step with no recorded time
      // (e.g. "special" when nothing in this run needed special-char
      // cleaning at all) genuinely never happened, and showing "✓ —" for
      // it claims a false completion. Previously every row got marked
      // done unconditionally here regardless of whether sec was null.
      const ran = sec != null;
      row.className    = "cp-step" + (ran ? " done" : "");
      if (icon) icon.textContent = ran ? "✓" : "";
      if (time) time.textContent = ran ? fmt(sec) : "—";
    });
    return;
  }

  // Legacy fallback: proportional split of the real total by step weight.
  const totalWeight = ACTIVE_STEPS.reduce((s, x) => s + x.weight, 0) || 1;
  ACTIVE_STEPS.forEach((s, i) => {
    const row  = document.getElementById(`cpStep_${i}`);
    const icon = document.getElementById(`cpStepIcon_${i}`);
    const time = document.getElementById(`cpStepTime_${i}`);
    if (row)  row.className    = "cp-step done";
    if (icon) icon.textContent = "✓";
    if (time) {
      const share = (s.weight / totalWeight) * totalMs;
      time.textContent = share >= 1000 ? `${(share / 1000).toFixed(1)}s` : `${Math.round(share)}ms`;
    }
  });
}

// Manual "Download duplicates" button — same file, on demand.
async function downloadDuplicates() {
  const result = window._lastPipelineResult;
  const url    = result?.download_urls?.duplicates;
  if (!url) { showToast("No download available. Run the pipeline first.", "error"); return; }
  const dupRows = result.output_files?.duplicate_rows || 0;
  if (dupRows === 0) { showToast("No duplicate UUID/CNIC rows found in the last run.", "success"); return; }
  try {
    await downloadNamedFile(url, result.output_files?.duplicates || "duplicates.parquet");
  } catch (e) { showToast(e.message, "error"); }
}
window.downloadDuplicates = downloadDuplicates;

async function runPipeline() {
  if (!state.fileId)   { showToast("Upload a file first.",       "error"); return; }
  if (!state.dataType) { showToast("Select a dataset type.",     "error"); return; }
  const needsIp = REQUIRES_IP[state.dataType];
  if (needsIp && !state.ipName) { showToast("Enter an organisation / IP name.", "error"); return; }

  if (typeof syncColumns === "function") syncColumns();
  loadGeneratedFilters();
  const filters = (window.allConfigs || []).map(({ sheetName, ...rest }) => rest);
  _showProgress(); _pollProgress(state.fileId);

  let result = null, err = null;

  // Regex/mapping "Regex Clean" rules are sent as part of the SAME pipeline
  // request now (see cleanFileWithValidation below) — applied as the first
  // real step inside the backend pipeline, tracked through the same live
  // progress stream as everything else. This used to be a separate
  // sequence of API calls run BEFORE the pipeline even started, invisible
  // to progress tracking and outside the actual execution hierarchy.

  try {
    result = await cleanFileWithValidation(
      state.dataType, needsIp ? state.ipName : null,
      state.fileId, state.uuidColumn || null,
      filters, state.globalRules || {}, state.caseStyles || {},
      state.columnRules || {}, state.regexRules || {}, state.dtypeRules || {},
      state.dataset2FileId || null
    );
  } catch (e) { err = e; }

  if (_progEventSource) { _progEventSource.close(); _progEventSource = null; }
  clearInterval(_progTickTimer); _progTickTimer = null;
  if (err) { _hideProgress(); showToast(err.message, "error"); setStatus("error", "Pipeline failed"); return; }

  _finishProgress(result?.step_timings || null);
  document.getElementById("cpTitle").textContent = "Pipeline complete ✓";

  state.cleanDataType = state.dataType;  state.cleanIpName = state.ipName;
  state.valDataType   = state.dataType;  state.valIpName   = state.ipName;
  state.fileId = null;
  document.getElementById("uploadZone").style.display = "block";
  document.getElementById("fileCard").style.display   = "none";
  document.getElementById("configGrid").style.display = "none";
  document.getElementById("runBar").style.display     = "none";
  document.getElementById("topbarFile").style.display = "none";
  document.getElementById("clearBtn").style.display   = "none";
  setStatus("active", "Pipeline complete");
  // Store predefined validation results for Validate tab
  const pv = result.predefined_validation || {};
  state.predefinedValidation = pv;

  // Build toast: show cleaning count + predefined failures + user filter failures
  const vf       = result.validation_summary;
  const pvFailed = pv.failed || 0;
  const ufFailed = vf && vf.failed != null ? vf.failed : 0;
  // If user filters ran, their summary already includes predefined — avoid double count
  const failMsg  = vf && vf.filter_results?.length
    ? (ufFailed > 0 ? `, ${ufFailed.toLocaleString()} validation failures` : "")
    : (pvFailed > 0 ? `, ${pvFailed.toLocaleString()} predefined rule failures` : "");

  showToast(
    `Done — ${result.summary.cells_auto_cleaned.toLocaleString()} cells cleaned${failMsg}.`,
    pvFailed > 0 || ufFailed > 0 ? "error" : "success", 6000
  );

  result._downloadUrl  = result.download_urls?.cleaned_dataset || null;
  result._downloadName = result.output_files?.cleaned || "cleaned_output.parquet";
  window._lastPipelineResult = result;

  // Cleaned dataset, report, and duplicates all already live in their
  // project folder on the server (write_outputs → resolve_dir). No browser
  // download is triggered automatically — use the Report / Duplicates
  // buttons in the Report tab if you need a local copy.

  // Pre-populate validate tab stats so Load Results isn't needed immediately
  _populateValStatsFromPipeline(result);

  if (typeof renderReport === "function") renderReport(result);
  if (typeof _invalidateCleanCache === "function") _invalidateCleanCache(state.cleanDataType, state.cleanIpName);
  _syncCleanProjectDropdown();

  // Quietly warm the Clean page's first page RIGHT NOW, while the popup is
  // still up and you're reading the summary — not a redirect, just a
  // background fetch. By the time you click "Done" below, page 1 is
  // usually already cached, so Clean opens instantly instead of waiting on
  // the backend's full-file read. (Pure frontend timing trick — it doesn't
  // make the backend read faster, it just does that read while you'd be
  // looking at this popup anyway instead of after you click through.)
  if (typeof getCleanedDataset === "function" && typeof _cleanPageCache !== "undefined") {
    getCleanedDataset(state.cleanDataType, state.cleanIpName, 1, 100)
      .then(data => { if (!data.error) _cleanPageCache.set(_cleanCacheKey(state.cleanDataType, state.cleanIpName, 1), data); })
      .catch(() => {});
  }

  // Popup now stays open — you decide when to move on.
  const doneBtn = document.getElementById("cpDoneBtn");
  if (doneBtn) {
    doneBtn.style.display = "inline-flex";
    doneBtn.onclick = () => {
      _hideProgress();
      doneBtn.style.display = "none";
      switchView("clean"); loadCleanDataset();
    };
  }
}

// ── Clean View ────────────────────────────────────────────────────────────────
function _syncCleanProjectDropdown() {
  const sel = document.getElementById("cleanProjectSelect"); if (!sel) return;
  sel.innerHTML = `<option value="">— select project —</option>` +
    state.allProjects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
  if (state.cleanDataType) {
    // Prefer an EXACT match on both dataType and ipName — this runs right
    // after a pipeline completes (state.cleanIpName is already correctly
    // set to the project just cleaned), so matching by dataType alone was
    // a real bug: with more than one project of the same data type saved,
    // .find() silently grabbed whichever one happened to come first in the
    // list and overwrote the just-completed run's ipName with it — the
    // Clean tab would then load and display a COMPLETELY DIFFERENT
    // project's data right after you finished cleaning a different one.
    // Falls back to a dataType-only match only when there's no exact hit
    // (e.g. a fresh run for a project that was never explicitly selected).
    const exact = state.allProjects.find(p =>
      p.dataType === state.cleanDataType &&
      (p.ipName || null) === (state.cleanIpName || null)
    );
    const match = exact || state.allProjects.find(p => p.dataType === state.cleanDataType);
    if (match) sel.value = match.id;
  }
  _syncCleanIpSelect();
}
function onCleanProjectChange(projId) {
  const proj = state.allProjects.find(p => String(p.id) === String(projId));
  if (proj) { state.cleanDataType = proj.dataType; state.cleanIpName = proj.ipName; }
  _syncCleanIpSelect();
  if (proj && !REQUIRES_IP[proj.dataType]) loadCleanDataset();
}
function _syncCleanIpSelect() {
  const proj  = state.allProjects.find(p => String(p.id) === String(document.getElementById("cleanProjectSelect")?.value));
  const ipSel = document.getElementById("cleanIpSelect");
  if (!proj || !proj.dataType) { if (ipSel) ipSel.style.display = "none"; return; }
  state.cleanDataType = proj.dataType;
  if (REQUIRES_IP[proj.dataType]) {
    if (ipSel) {
      ipSel.style.display = "none";
      if (proj.ipName) {
        ipSel.innerHTML = `<option value="${escapeHtml(proj.ipName)}">${escapeHtml(proj.ipName)}</option>`;
        ipSel.value     = proj.ipName;
        state.cleanIpName = proj.ipName;
      }
    }
    if (proj.ipName) loadCleanDataset();
  } else {
    if (ipSel) ipSel.style.display = "none";
    state.cleanIpName = null;
  }
}

// Client-side page cache for the clean dataset view. Keyed per
// dataType/ipName so switching projects never serves stale rows.
// This is a pure frontend speed-up: once a page has been fetched it's
// reused instantly (no re-fetch) instead of hitting the backend again
// every time you flip back to a page you've already seen.
const _cleanPageCache = new Map();
function _cleanCacheKey(dt, ip, page) { return `${dt}::${ip || ""}::${page}`; }

let _cleanLoadToken = 0;

async function loadCleanDataset(page = 1) {
  state.cleanPage = page;
  const dt = state.cleanDataType, ip = state.cleanIpName || null;
  if (!dt) { showToast("Select a project first.", "error"); return; }

  // Every navigation — including one served instantly from cache — bumps
  // the token, so an older still-in-flight request (e.g. you clicked page
  // 3, then immediately page 5 which happened to already be cached) can
  // never land late and clobber whatever's now on screen.
  const myToken = ++_cleanLoadToken;

  const cacheKey = _cleanCacheKey(dt, ip, page);
  const cached   = _cleanPageCache.get(cacheKey);

  // If we already have this page, render it instantly — no loader, no wait.
  if (cached) {
    _applyCleanDatasetResult(cached);
    _prefetchAdjacentCleanPages(dt, ip, page);
    return;
  }

  // Out-of-order guard: only the request matching the CURRENT token when it
  // resolves is allowed to render — see comment on _cleanLoadToken above.
  // First time seeing this page: show a lightweight table skeleton
  // immediately (rather than a blank freeze) while the real request runs.
  _showCleanTableSkeleton();
  showLoader("Loading cleaned dataset…");
  try {
    const data = await getCleanedDataset(dt, ip, page, 100);
    if (myToken !== _cleanLoadToken) return;   // superseded by a newer navigation
    if (data.error) {
      document.getElementById("cleanEmpty").style.display       = "flex";
      document.getElementById("cleanDatasetWrap").style.display = "none";
      showToast(data.error, "error"); return;
    }
    _cleanPageCache.set(cacheKey, data);
    _applyCleanDatasetResult(data);
    _prefetchAdjacentCleanPages(dt, ip, page);
  } catch (e) {
    if (myToken === _cleanLoadToken) showToast(e.message, "error");
  }
  finally { if (myToken === _cleanLoadToken) hideLoader(); }
}

// Shared render path for both cache-hit (instant) and freshly-fetched data.
function _applyCleanDatasetResult(data) {
  state.cleanTotalPages = data.total_pages || 1;
  renderCleanTable(data);
  if (data.columns && data.columns.length) state.columns = data.columns;
  document.getElementById("cleanDatasetWrap").style.display = "flex";
  document.getElementById("cleanEmpty").style.display       = "none";
  document.getElementById("cleanFileBadge").textContent     = data.file || "";
  _updateCleanPagination();
  if (state._lastRegexResult) {
    const { column, preview, review_rows } = state._lastRegexResult;
    if (typeof _highlightRegexResults === "function") _highlightRegexResults(column, preview, review_rows);
  }
}

// Quietly warms the cache for the next page (and, once you've moved past
// page 1, the previous page too) in the background, so clicking Next/Prev
// usually just hits the cache instead of waiting on a fresh request.
let _prefetchInFlight = new Set();
async function _prefetchAdjacentCleanPages(dt, ip, page) {
  const targets = [page + 1, page - 1].filter(p => p >= 1 && p <= (state.cleanTotalPages || 1));
  for (const p of targets) {
    const key = _cleanCacheKey(dt, ip, p);
    if (_cleanPageCache.has(key) || _prefetchInFlight.has(key)) continue;
    _prefetchInFlight.add(key);
    getCleanedDataset(dt, ip, p, 100)
      .then(data => { if (!data.error) _cleanPageCache.set(key, data); })
      .catch(() => {})
      .finally(() => _prefetchInFlight.delete(key));
  }
}

// Invalidate the cache — call this whenever the underlying cleaned dataset
// could have changed (e.g. after a new pipeline run, or a clean-tool action
// that edits cells), so we never show stale cached rows.
function _invalidateCleanCache(dt = null, ip = null) {
  if (!dt) { _cleanPageCache.clear(); return; }
  const prefix = `${dt}::${ip || ""}::`;
  for (const k of Array.from(_cleanPageCache.keys())) if (k.startsWith(prefix)) _cleanPageCache.delete(k);
}
window._invalidateCleanCache = _invalidateCleanCache;

// Minimal instant placeholder so switching pages never looks frozen while
// a (non-cached) page is in flight.
function _showCleanTableSkeleton() {
  const head = document.getElementById("cleanTableHead");
  const body = document.getElementById("cleanTableBody");
  if (!body) return;
  const colCount = (state.columns && state.columns.length) || 6;
  if (head && head.children.length === 0) {
    head.innerHTML = `<tr><th>#</th>${Array.from({ length: colCount }).map(() => `<th></th>`).join("")}</tr>`;
  }
  body.innerHTML = Array.from({ length: 12 }).map(() =>
    `<tr class="clean-skeleton-row">${Array.from({ length: colCount + 1 }).map(() =>
      `<td><span class="clean-skeleton-cell"></span></td>`).join("")}</tr>`
  ).join("");
  document.getElementById("cleanDatasetWrap").style.display = "flex";
  document.getElementById("cleanEmpty").style.display       = "none";
}

function cleanChangePage(dir) { loadCleanDataset(state.cleanPage + dir); }
function _updateCleanPagination() {
  const p = state.cleanPage, tp = state.cleanTotalPages;
  document.getElementById("cleanPrevBtn").disabled   = p <= 1;
  document.getElementById("cleanNextBtn").disabled   = p >= tp;
  document.getElementById("cleanPageLabel").textContent = `Page ${p} of ${tp}`;
}

function renderCleanTable(data) {
  const { columns, rows, total_rows } = data;
  document.getElementById("cleanTableHead").innerHTML =
    `<tr><th>#</th>${columns.map(c =>
      `<th title="Click to view flagged values" class="clean-th-clickable" data-col="${escapeHtml(c)}">${escapeHtml(c)}</th>`
    ).join("")}</tr>`;
  const startRow = (state.cleanPage - 1) * 100 + 2;
  document.getElementById("cleanTableBody").innerHTML = rows.map((cells, ri) =>
    `<tr><td>${startRow + ri}</td>${cells.map(cell => {
      const flags     = cell.flags || [];
      const cls       = _cellFlagClass(flags);
      const isNone    = cell.value === "" || cell.value === "None" || cell.value === "nan";
      const isCleaned = flags.includes("cell-auto-cleaned");
      const isReview  = flags.includes("cell-needs-review");
      let title       = escapeHtml(cell.value);
      if (isCleaned) title = "Auto-cleaned: "  + title;
      if (isReview)  title = "Needs review: "  + title;
      const badge = isCleaned
        ? `<span class="cell-clean-badge">✓</span>`
        : isReview ? `<span class="cell-review-badge">?</span>` : "";
      return `<td class="${cls}${isNone ? " cell-none" : ""}" title="${title}">${isNone ? "—" : escapeHtml(cell.value)}${badge}</td>`;
    }).join("")}</tr>`
  ).join("");
  document.getElementById("cleanDatasetInfo").textContent =
    `${total_rows.toLocaleString()} total rows · ${columns.length} columns`;
  const counts = {};
  rows.forEach(cells => cells.forEach(c => (c.flags || []).forEach(f => { counts[f] = (counts[f] || 0) + 1; })));
  // Count predefined rule failures across all cells on this page
  // __validation_status__ column carries FAIL for predefined-flagged rows
  let predefinedFailCount = 0;
  rows.forEach(cells => {
    const vsCell = cells.find(c => c.flags && (c.flags.includes("flag-val-fail") || c.flags.includes("flag-val-pass")));
    if (vsCell && vsCell.flags.includes("flag-val-fail")) predefinedFailCount++;
  });

  const chips = [
    { key: "cell-auto-cleaned", label: "Auto-cleaned",     cls: "flag-count-chip--green"   },
    { key: "cell-needs-review", label: "Needs review",     cls: "flag-count-chip--yellow"  },
    { key: "null_value",        label: "Null",             cls: "flag-count-chip--null"     },
    { key: "flag-val-fail",     label: "Validation fail",  cls: "flag-count-chip--uuid"     },
    { key: "special_at",        label: "Special char",     cls: "flag-count-chip--special"  },
  ].filter(x => counts[x.key] > 0)
   .map(x => `<span class="flag-count-chip ${x.cls} flag-count-chip--clickable" data-flag="${x.key}" title="Click to view all flagged values">${counts[x.key].toLocaleString()} ${x.label}</span>`)
   .join("");
  document.getElementById("cleanFlagSummary").innerHTML = chips;
}

// Predefined rule prefix → CSS class mapping (mirrors PREDEFINED_RULE_COLORS in backend)
const _PREDEFINED_COLOR_CLASS = {
  R01: "flag-val-fail",      // UUID format     → red
  R02: "flag-val-fail",      // UUID duplicate  → red
  R03: "flag-val-fail",      // CNIC invalid    → red
  R04: "flag-predefined-orange", // UUID-CNIC linkage → orange
  R05: "flag-predefined-orange", // CNIC shared      → orange
  R06: "flag-val-fail",      // mandatory empty → red
  R07: "flag-predefined-purple", // date format  → purple
  R08: "flag-predefined-purple", // future date  → purple
  R09: "flag-predefined-yellow", // date order   → yellow
  R10: "flag-predefined-yellow", // stage dep    → yellow
  R11: "flag-predefined-orange", // ITVC missing → orange
  R12: "flag-predefined-yellow", // status/date mismatch → yellow
};

function _cellFlagClass(flags) {
  if (!flags.length) return "";
  // Predefined rule flags — pick colour by rule prefix (highest priority)
  const preFlag = flags.find(f => f.startsWith("RULE_R") || f.startsWith("predefined_R"));
  if (preFlag) {
    const prefix = (preFlag.replace(/^(RULE_|predefined_)/, "")).slice(0, 3);
    return _PREDEFINED_COLOR_CLASS[prefix] || "flag-val-fail";
  }
  if (flags.includes("flag-val-fail"))        return "flag-val-fail";
  if (flags.includes("flag-val-pass"))        return "flag-val-pass";
  if (flags.includes("flag-val-cell-fail"))   return "flag-val-fail";
  if (flags.includes("cell-auto-cleaned"))    return "cell-auto-cleaned";
  if (flags.includes("cell-needs-review"))    return "cell-needs-review";
  if (flags.some(f => f.startsWith("special_"))) return "flag-special-at";
  if (flags.includes("null_value"))           return "flag-null";
  return "";
}

async function downloadCleanView() {
  const dt = state.cleanDataType, ip = state.cleanIpName || null;
  if (!dt) { showToast("Load a dataset first.", "error"); return; }
  const base  = ip ? `/api/clean/${encodeURIComponent(dt)}/${encodeURIComponent(ip)}` : `/api/clean/${encodeURIComponent(dt)}`;
  const badge = document.getElementById("cleanFileBadge").textContent;
  const stem  = badge.replace("_cleaned.parquet", "");
  window.open(`${base}/${stem}/download/cleaned`, "_blank");
}

async function downloadIssueRows() {
  if (!state.fileId) {
    const rows     = state.dataset?.rows || [];
    if (!rows.length) { showToast("Load data first.", "error"); return; }
    const cols      = state.dataset.columns || [];
    const issueRows = rows.filter(cells => cells.some(c => (c.flags || []).length > 0));
    if (!issueRows.length) { showToast("No flagged rows found in the current view.", "info"); return; }
    _downloadRowsAsCsv(cols, issueRows, state.cleanDataType || "issues");
    return;
  }
  showLoader("Preparing issue rows export…");
  try {
    const uuid = (state.uuidColumnClean || state.uuidColumn) || null;
    await downloadIssueRowsFile(state.fileId, state.fileName || "issue_rows", uuid);
    showToast("Issue rows export started.", "success");
  } catch (err) { showToast(err.message, "error"); }
  finally { hideLoader(); }
}

function _downloadRowsAsCsv(columns, rows, baseName) {
  const escape = v => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
  const header = ["#", ...columns].map(escape).join(",");
  const body   = rows.map((cells, i) => [i + 1, ...cells.map(c => c.value)].map(escape).join(",")).join("\n");
  const csv    = header + "\n" + body;
  const blob   = new Blob([csv], { type: "text/csv" });
  const url    = URL.createObjectURL(blob);
  const a      = document.createElement("a");
  a.href = url; a.download = `${baseName}_issues.csv`;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// ── Validate View ─────────────────────────────────────────────────────────────
// The validate view now does two things:
//   1. "Load Results" — loads the server-side __validation_status__ already
//      written to the parquet by the last pipeline run (same as before).
//   2. "Run Filters" — runs the currently configured filter rules client-side
//      against the cleaned dataset fetched from /api/dataset, and renders a
//      full highlighted table identical to the Clean section.

function _syncValProjectDropdown() {
  const sel = document.getElementById("valProjectSelect"); if (!sel) return;
  sel.innerHTML = `<option value="">— select project —</option>` +
    state.allProjects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
  if (state.valDataType) {
    const match = state.allProjects.find(p => p.dataType === state.valDataType);
    if (match) sel.value = match.id;
  }
  _syncValIpSelect();
}

function onValProjectChange(projId) {
  const proj = state.allProjects.find(p => String(p.id) === String(projId));
  if (proj) { state.valDataType = proj.dataType; state.valIpName = proj.ipName; }
  _syncValIpSelect();
}

function _syncValIpSelect() {
  const proj  = state.allProjects.find(p => String(p.id) === String(document.getElementById("valProjectSelect")?.value));
  const ipSel = document.getElementById("valIpSelect"); if (!ipSel) return;
  if (proj && REQUIRES_IP[proj.dataType] && proj.ipName) {
    ipSel.style.display = "inline-block";
    ipSel.innerHTML     = `<option value="${escapeHtml(proj.ipName)}">${escapeHtml(proj.ipName)}</option>`;
    ipSel.value         = proj.ipName;
    state.valIpName     = proj.ipName;
  } else {
    ipSel.style.display = "none";
    state.valIpName     = null;
  }
}

// ── Load Results — server-side validation status from parquet ─────────────────
async function loadValidationResults() {
  const dt = state.valDataType, ip = state.valIpName || null;
  if (!dt) { showToast("Select a project first.", "error"); return; }
  showLoader("Loading validation results…");
  try {
    const data = await getValidationResults(dt, ip);
    if (data.error) {
      document.getElementById("valEmpty").style.display = "flex";
      showToast(data.error, "error"); return;
    }
    document.getElementById("valEmpty").style.display = "none";

    // Stats
    document.getElementById("valStatTotal").textContent  = (data.total_rows || 0).toLocaleString();
    document.getElementById("valStatPassed").textContent = (data.passed     || 0).toLocaleString();
    document.getElementById("valStatFailed").textContent = (data.failed     || 0).toLocaleString();
    document.getElementById("valStatRow").style.display  = "grid";

    state.valData = data;

    // Filter breakdown
    if (data.filter_results && data.filter_results.length > 0) {
      document.getElementById("valBreakdownCard").style.display = "flex";
      document.getElementById("valFilterBreakdown").innerHTML = _renderFilterBreakdownRows(data.filter_results, data.total_rows);
    } else {
      document.getElementById("valBreakdownCard").style.display = "none";
    }

    // Failures list
    state.valFailures = data.failures || [];
    state.valPage     = 1;
    state.valSearch   = "";
    document.getElementById("valSearchInput").value = "";

    const hasFail = data.failures && data.failures.length > 0;
    const hasData = data.total_rows > 0;

    const toggle = document.getElementById("valViewToggle");
    if (toggle) {
      toggle.style.display = hasData ? "flex" : "none";
      const badge = document.getElementById("valFailedCount");
      if (badge) { badge.textContent = data.failed > 0 ? data.failed : ""; badge.style.display = data.failed > 0 ? "inline-flex" : "none"; }
    }

    if (hasFail) {
      // Switch to Failed Rows tab
      document.querySelectorAll(".val-view-btn").forEach(b => b.classList.remove("active"));
      const failBtn = document.getElementById("valViewBtnFailed");
      if (failBtn) failBtn.classList.add("active");
      document.getElementById("valTableCard").style.display = "flex";
      renderValTable();
      document.getElementById("valDatasetCard").style.display = "none";
    } else if (hasData) {
      // No failures — auto-switch to Full Dataset tab and render server-side status table
      document.querySelectorAll(".val-view-btn").forEach(b => b.classList.remove("active"));
      const allBtn = document.getElementById("valViewBtnAll");
      if (allBtn) allBtn.classList.add("active");
      document.getElementById("valTableCard").style.display = "none";
      document.getElementById("valDatasetCard").style.display = "flex";
      _renderValDataset();
    } else {
      document.getElementById("valTableCard").style.display = "none";
      document.getElementById("valDatasetCard").style.display = "none";
    }

    state.valDatasetPage   = 1;
    state.valDatasetSearch = "";

  } catch (e) { showToast(e.message, "error"); }
  finally { hideLoader(); }
}

// ── Run Filters — client-side validation with highlighted table ───────────────
// Called by the "Run Filters" button in the Validate view header.
// Fetches ALL pages of the cleaned dataset, runs window.runClientValidation()
// against all active filter rules, then renders the result as a full
// highlighted table using the same renderCleanTable() engine the Clean view uses.

async function runValidationFilters() {
  const dt = state.valDataType, ip = state.valIpName || null;
  if (!dt) { showToast("Select a project first.", "error"); return; }

  if (typeof syncColumns          === "function") syncColumns();
  if (typeof loadGeneratedFilters === "function") loadGeneratedFilters();
  const configs = window.allConfigs || [];
  // Allow running with 0 user filters — predefined rules (R01–R12) are always
  // present in the server-side __validation_status__. In this case, fall back
  // to loading the server-side results which already contain predefined results.
  if (configs.length === 0) {
    showToast("No user filters configured — loading predefined rule results.", "info");
    loadValidationResults();
    return;
  }

  showLoader("Running filters on dataset…");
  try {
    // Fetch page 1 to get total_rows / total_pages
    const first = await getCleanedDataset(dt, ip, 1, 200);
    if (first.error) { showToast(first.error, "error"); return; }

    const totalPages = first.total_pages || 1;
    let allRows      = [...(first.rows || [])];
    const columns    = first.columns || [];

    // Fetch remaining pages
    for (let pg = 2; pg <= totalPages; pg++) {
      loaderMsg(`Loading dataset page ${pg} of ${totalPages}…`);
      const page = await getCleanedDataset(dt, ip, pg, 200);
      if (page.rows) allRows = allRows.concat(page.rows);
    }

    loaderMsg("Running validation filters…");
    const dataset = { columns, rows: allRows, total_rows: first.total_rows };

    // runClientValidation is exported by validation_engine.js
    if (typeof window.runClientValidation !== "function") {
      showToast("Validation engine not loaded.", "error"); return;
    }
    const result = window.runClientValidation(dataset);
    if (!result) { showToast("Validation returned no result.", "error"); return; }

    // Store for pagination
    state.valHlData        = result;
    state.valHlPage        = 1;
    state.valHlTotalPages  = Math.max(1, Math.ceil((result.rows || []).length / 100));

    // Update stats
    document.getElementById("valStatTotal").textContent  = result.total_rows.toLocaleString();
    document.getElementById("valStatPassed").textContent = result.passed.toLocaleString();
    document.getElementById("valStatFailed").textContent = result.failed.toLocaleString();
    document.getElementById("valStatRow").style.display  = "grid";
    document.getElementById("valEmpty").style.display    = "none";

    // Filter breakdown from client result
    if (result.filter_results && result.filter_results.length > 0) {
      document.getElementById("valBreakdownCard").style.display = "flex";
      document.getElementById("valFilterBreakdown").innerHTML = _renderFilterBreakdownRows(result.filter_results, result.total_rows);
    }

    // Switch to the highlighted dataset view
    _showValHighlightTable();

    const toggle = document.getElementById("valViewToggle");
    if (toggle) {
      toggle.style.display = "flex";
      const badge = document.getElementById("valFailedCount");
      if (badge) {
        badge.textContent  = result.failed > 0 ? result.failed : "";
        badge.style.display = result.failed > 0 ? "inline-flex" : "none";
      }
    }
    // Update view toggle: show "Failed Rows" and "Full Dataset" buttons
    document.querySelectorAll(".val-view-btn").forEach(b => b.classList.remove("active"));
    const fullBtn = document.getElementById("valViewBtnAll");
    if (fullBtn) fullBtn.classList.add("active");

    document.getElementById("valTableCard").style.display = "none";

    showToast(`Filters run — ${result.failed.toLocaleString()} row${result.failed !== 1 ? "s" : ""} flagged.`,
      result.failed > 0 ? "error" : "success");

  } catch (e) { showToast(e.message, "error"); }
  finally { hideLoader(); }
}

// Render the highlighted dataset table using the Clean section's renderCleanTable engine
function _showValHighlightTable() {
  const result = state.valHlData; if (!result) return;

  const ps       = 100;
  const page     = state.valHlPage;
  const allRows  = result.rows || [];
  const start    = (page - 1) * ps;
  const slice    = allRows.slice(start, start + ps);
  const totalPgs = Math.max(1, Math.ceil(allRows.length / ps));
  state.valHlTotalPages = totalPgs;

  // Render into the valDatasetCard (reusing its DOM slot)
  const card = document.getElementById("valDatasetCard");
  if (!card) return;
  card.style.display = "flex";

  // Populate the inline filter bar with current columns
  if (typeof vifInit === "function") vifInit();

  // Build header
  const headEl = document.getElementById("valDatasetHead");
  if (headEl) {
    headEl.innerHTML = `<tr><th>#</th>${result.columns.map(c =>
      `<th title="${escapeHtml(c)}">${escapeHtml(c)}</th>`).join("")}</tr>`;
  }

  // Build body — reuse exact same cell rendering logic as renderCleanTable
  const startRow = start + 2;
  const bodyEl   = document.getElementById("valDatasetBody");
  if (bodyEl) {
    bodyEl.innerHTML = slice.map((cells, ri) => {
      // Row-level tooltip: list failing filters
      const filterDetails = cells[0]?._filterDetails || [];
      const rowTitle = filterDetails.length
        ? filterDetails.map(f => `${f.label}: ${f.actual || ""}`).join(" | ")
        : "";
      const rowFailed = cells.some(c => (c.flags || []).includes("flag-val-fail"));
      return `<tr${rowTitle ? ` title="${escapeHtml(rowTitle)}"` : ""}${rowFailed ? ' class="val-row-failed"' : ""}>
        <td class="row-num">${startRow + ri}</td>
        ${cells.map(cell => {
          const flags     = cell.flags || [];
          const cls       = _cellFlagClass(flags);
          const isNone    = cell.value === "" || cell.value === "None" || cell.value === "nan";
          const isCleaned = flags.includes("cell-auto-cleaned");
          const isReview  = flags.includes("cell-needs-review");
          const isValFail = flags.includes("flag-val-fail") || flags.includes("flag-val-cell-fail");
          let title = escapeHtml(cell.value || "");
          if (isCleaned) title = "Auto-cleaned: "  + title;
          if (isReview)  title = "Needs review: "  + title;
          if (isValFail) title = "Validation fail: " + title;
          const badge = isCleaned
            ? `<span class="cell-clean-badge">✓</span>`
            : isReview ? `<span class="cell-review-badge">?</span>`
            : isValFail ? `<span class="cell-val-fail-badge">✗</span>` : "";
          return `<td class="${cls}${isNone ? " cell-none" : ""}" title="${title}">${isNone ? "—" : escapeHtml(cell.value)}${badge}</td>`;
        }).join("")}
      </tr>`;
    }).join("") ||
    `<tr><td colspan="${result.columns.length + 1}" style="text-align:center;padding:28px;color:var(--text-4)">No rows on this page.</td></tr>`;
  }

  // Info line
  const infoEl = document.getElementById("valDatasetPageInfo");
  if (infoEl) {
    infoEl.textContent = `Page ${page} of ${totalPgs} · ${allRows.length.toLocaleString()} total rows · ${result.failed.toLocaleString()} flagged`;
  }

  // Flag summary chips (reuse same chip system as Clean view)
  const counts = {};
  slice.forEach(cells => cells.forEach(c => (c.flags || []).forEach(f => { counts[f] = (counts[f] || 0) + 1; })));
  const chipsHTML = [
    { key: "cell-auto-cleaned",  label: "Auto-cleaned",    cls: "flag-count-chip--green"   },
    { key: "cell-needs-review",  label: "Needs review",    cls: "flag-count-chip--yellow"  },
    { key: "null_value",         label: "Null",            cls: "flag-count-chip--null"     },
    { key: "flag-val-fail",      label: "Validation fail", cls: "flag-count-chip--uuid"     },
    { key: "flag-val-cell-fail", label: "Cell flagged",    cls: "flag-count-chip--uuid"     },
    { key: "special_at",         label: "Special char",    cls: "flag-count-chip--special"  },
  ].filter(x => counts[x.key] > 0)
   .map(x => `<span class="flag-count-chip ${x.cls}">${counts[x.key].toLocaleString()} ${x.label}</span>`)
   .join("");

  // Inject chips — look for the valDatasetCard header span
  const captionEl = card.querySelector(".val-table-title");
  if (captionEl) {
    captionEl.innerHTML = `Full Dataset — Validation Filters Applied
      <span style="margin-left:12px;font-weight:400;font-size:12px">${chipsHTML}</span>`;
  }

  // Pagination
  const pagEl = document.getElementById("valDatasetPagination");
  if (pagEl) {
    pagEl.style.display = totalPgs > 1 ? "flex" : "none";
    if (infoEl) infoEl.textContent =
      `Page ${page} of ${totalPgs} · ${allRows.length.toLocaleString()} rows · ${result.failed.toLocaleString()} flagged`;
    const prevBtn = pagEl.querySelector("button:first-child");
    const nextBtn = pagEl.querySelector("button:last-child");
    if (prevBtn) prevBtn.disabled = page <= 1;
    if (nextBtn) nextBtn.disabled = page >= totalPgs;
  }
}

function valHlChangePage(dir) {
  if (!state.valHlData) return;
  const newPage = (state.valHlPage || 1) + dir;
  const maxPage = state.valHlTotalPages || 1;
  state.valHlPage = Math.max(1, Math.min(newPage, maxPage));
  _showValHighlightTable();
}

// Expose to HTML onclick
window.valHlChangePage = valHlChangePage;

// ── switchValView: toggle between failed-rows list and highlighted dataset ─────
function switchValView(view) {
  document.querySelectorAll(".val-view-btn").forEach(b =>
    b.classList.toggle("active", b.id === `valViewBtn${capitalize(view)}`));

  if (view === "failed") {
    document.getElementById("valTableCard").style.display =
      state.valFailures.length ? "flex" : "none";
    document.getElementById("valDatasetCard").style.display = "none";
  } else {
    document.getElementById("valTableCard").style.display   = "none";
    document.getElementById("valDatasetCard").style.display = "flex";
    // Show the highlighted table if client validation was run, else the old status table
    if (state.valHlData) {
      _showValHighlightTable();
    } else {
      _renderValDataset();
    }
  }
}

// ── Old "Full Dataset" table (server-side status only, shown before client run)
function _renderValDataset() {
  const data = state.valData; if (!data) return;
  const all = state.valFailures || [];

  // All rows passed — show a clear success state instead of an empty table
  if (all.length === 0 && !state.valDatasetSearch) {
    const headEl = document.getElementById("valDatasetHead");
    if (headEl) headEl.innerHTML = `<tr><th>Status</th></tr>`;
    document.getElementById("valDatasetBody").innerHTML =
      `<tr><td style="text-align:center;padding:40px;color:var(--text-3)">
         <div style="font-size:32px;margin-bottom:10px">✓</div>
         <strong style="color:#16a34a;font-size:15px">All ${(data.total_rows||0).toLocaleString()} rows passed</strong>
         <div style="font-size:12px;margin-top:8px;color:var(--text-4)">
           No validation failures found.<br>Click <strong>Run Filters</strong> to see per-cell highlights across the full dataset.
         </div>
       </td></tr>`;
    const pag = document.getElementById("valDatasetPagination");
    if (pag) pag.style.display = "none";
    const titleEl = document.getElementById("valDatasetTitle");
    if (titleEl) titleEl.textContent = "Full Dataset — All Rows Passed";
    return;
  }

  const q        = (state.valDatasetSearch || "").toLowerCase();
  const filtered = q
    ? all.filter(f => String(f.row).includes(q) || (f.filters_failed || []).some(l => l.toLowerCase().includes(q)))
    : all;
  const ps = 50, page = state.valDatasetPage || 1;
  const total = filtered.length, pages = Math.max(1, Math.ceil(total / ps));
  state.valDatasetPage = Math.max(1, Math.min(page, pages));
  const slice = filtered.slice((state.valDatasetPage - 1) * ps, state.valDatasetPage * ps);

  // Update head to the status-only schema
  const headEl = document.getElementById("valDatasetHead");
  if (headEl) headEl.innerHTML = `<tr><th style="width:52px">Row</th><th>UUID</th><th>Status</th><th>Rules Failed</th></tr>`;

  document.getElementById("valDatasetBody").innerHTML = slice.map(f => `
    <tr>
      <td class="row-num">${f.row}</td>
      <td style="font-family:var(--font-mono);font-size:11px;color:var(--text-3)">—</td>
      <td><span class="val-status-fail">FAIL</span></td>
      <td>${(f.filters_failed || []).map(lbl => `<span class="val-rule-chip">${escapeHtml(lbl)}</span>`).join("")}</td>
    </tr>`).join("") ||
    `<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--text-4)">No failed rows${q ? " match your search" : ""}.</td></tr>`;

  const pag = document.getElementById("valDatasetPagination");
  if (pag) {
    pag.style.display = total > ps ? "flex" : "none";
    document.getElementById("valDatasetPageInfo").textContent =
      `Page ${state.valDatasetPage} of ${pages} · ${total.toLocaleString()} failed rows`;
  }
}

function filterValDataset(q) { state.valDatasetSearch = q; state.valDatasetPage = 1; _renderValDataset(); }
function valDatasetChangePage(dir) { state.valDatasetPage = (state.valDatasetPage || 1) + dir; _renderValDataset(); }

// ── Failures table (left tab — failed rows only) ──────────────────────────────
function filterValTable(q) { state.valSearch = q.toLowerCase(); state.valPage = 1; renderValTable(); }
function valChangePage(dir) { state.valPage += dir; renderValTable(); }

function renderValTable() {
  const all      = state.valFailures;
  const filtered = state.valSearch
    ? all.filter(f => f.filters_failed.some(lbl => lbl.toLowerCase().includes(state.valSearch)) || String(f.row).includes(state.valSearch))
    : all;
  const total = filtered.length, ps = state.valPageSize, pages = Math.max(1, Math.ceil(total / ps));
  state.valPage = Math.max(1, Math.min(state.valPage, pages));
  const start = (state.valPage - 1) * ps, slice = filtered.slice(start, start + ps);

  document.getElementById("valTableCaption").textContent = `${total.toLocaleString()} row${total !== 1 ? "s" : ""} failed`;
  document.getElementById("valTableBody").innerHTML = slice.map(f => {
    const details = f.filter_details || [];
    let filterCells = "";
    if (details.length) {
      filterCells = details.filter(d => !d.pass).map(d => `
        <div style="margin-bottom:6px;padding:6px 8px;border-radius:4px;background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.2)">
          <span class="val-filter-chip">${escapeHtml(d.label || d.cond || "")}</span>
          ${d.col ? `<span style="font-size:11px;color:var(--text-3);margin-left:6px">col: <code>${escapeHtml(d.col)}</code></span>` : ""}
          <div style="margin-top:4px;font-size:11px;display:flex;gap:12px;flex-wrap:wrap">
            ${d.actual   != null ? `<span style="color:var(--text-2)">actual: <strong>${escapeHtml(String(d.actual))}</strong></span>`   : ""}
            ${d.expected != null && d.expected !== "" ? `<span style="color:var(--text-3)">expected: ${escapeHtml(String(d.expected))}</span>` : ""}
          </div>
        </div>`).join("");
    } else {
      filterCells = (f.filters_failed || []).map(lbl => `<span class="val-filter-chip">${escapeHtml(lbl)}</span>`).join(" ");
    }
    return `<tr><td class="val-row-num">${f.row}</td><td>${filterCells || "—"}</td><td><span class="val-status-fail">FAIL</span></td></tr>`;
  }).join("") || `<tr><td colspan="3" style="padding:28px;text-align:center;color:var(--text-4)">No failures match</td></tr>`;

  const pag = document.getElementById("valPagination");
  if (pages > 1) {
    pag.style.display = "flex";
    document.getElementById("valPrevBtn").disabled = state.valPage <= 1;
    document.getElementById("valNextBtn").disabled = state.valPage >= pages;
    document.getElementById("valPageInfo").textContent = `Page ${state.valPage} of ${pages}`;
  } else {
    pag.style.display = "none";
  }
}

// ── Download failed rows from validation highlight table ──────────────────────
function downloadValFailedRows() {
  const result = state.valHlData;
  if (!result) { showToast("Run filters first.", "error"); return; }
  const failedRows = result.rows.filter(cells =>
    cells.some(c => (c.flags || []).includes("flag-val-fail")));
  if (!failedRows.length) { showToast("No flagged rows to export.", "info"); return; }
  _downloadRowsAsCsv(result.columns, failedRows, `${state.valDataType || "validation"}_failed`);
  showToast(`${failedRows.length.toLocaleString()} failed rows exported.`, "success");
}
window.downloadValFailedRows = downloadValFailedRows;

// ── Predefined validation helpers ────────────────────────────────────────────

// Colour token → CSS variable map for predefined rule colour dots
const _RULE_COLOR_DOT = {
  red:    "var(--red,#ef4444)",
  orange: "#f97316",
  yellow: "var(--yellow,#f59e0b)",
  purple: "#8b5cf6",
};

/**
 * Render filter breakdown rows — shared by loadValidationResults and
 * runValidationFilters. Adds a coloured dot for predefined rules and
 * a "Predefined" vs "User Filter" badge for visual distinction.
 */
function _renderFilterBreakdownRows(filterResults, totalRows) {
  return filterResults.map(f => {
    const isOk        = f.flagged_count === 0;
    const isPredefined = f.cond === "predefined";
    const dotColor    = isPredefined ? (_RULE_COLOR_DOT[f.color] || _RULE_COLOR_DOT.red) : null;
    const typeBadge   = isPredefined
      ? `<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:10px;background:rgba(139,92,246,0.12);color:#8b5cf6;border:1px solid rgba(139,92,246,0.25);white-space:nowrap">Predefined</span>`
      : `<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:10px;background:var(--accent-soft);color:var(--accent);border:1px solid var(--accent);white-space:nowrap">User Filter</span>`;
    return `<div class="val-filter-row" style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border);gap:8px">
      <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">
        ${dotColor ? `<span style="width:8px;height:8px;border-radius:50%;background:${dotColor};flex-shrink:0"></span>` : ""}
        <span class="val-filter-label" style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(f.label)}">${escapeHtml(f.label)}</span>
        ${typeBadge}
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
        <span style="font-size:12px;color:var(--text-3)">${((totalRows || 0) - f.flagged_count).toLocaleString()} pass</span>
        <span class="${isOk ? "val-badge-pass" : "val-badge-fail"}">${f.flagged_count} flagged</span>
        <span style="display:inline-flex;padding:2px 8px;border-radius:20px;font-size:11px;
          background:${isOk ? "var(--green-dim)" : "var(--red-dim)"};
          color:${isOk ? "var(--green)" : "var(--red)"}">${isOk ? "✓ OK" : "✗ Fail"}</span>
      </div>
    </div>`;
  }).join("");
}

/**
 * Pre-populate the Validate tab stats and breakdown immediately after
 * a pipeline run, using the predefined_validation block in the response.
 * This means the user can switch to Validate without clicking Load Results.
 */
function _populateValStatsFromPipeline(pipelineResult) {
  const pv = pipelineResult.predefined_validation;
  const vs = pipelineResult.validation_summary;

  // Use user-filter summary if available (it includes predefined), else predefined alone
  const summary = (vs && vs.total_rows) ? vs : pv;
  if (!summary || !summary.total_rows) return;

  // Set state so Load Results / Run Filters also work correctly
  state.valDataType = pipelineResult.data_type;
  state.valIpName   = pipelineResult.ip_name || null;

  // Update stats row
  document.getElementById("valStatTotal").textContent  = (summary.total_rows || 0).toLocaleString();
  document.getElementById("valStatPassed").textContent = (summary.passed     || 0).toLocaleString();
  document.getElementById("valStatFailed").textContent = (summary.failed     || 0).toLocaleString();
  document.getElementById("valStatRow").style.display  = "grid";
  document.getElementById("valEmpty").style.display    = "none";

  // Filter breakdown
  const filterResults = summary.filter_results || [];
  if (filterResults.length > 0) {
    document.getElementById("valBreakdownCard").style.display = "flex";
    document.getElementById("valFilterBreakdown").innerHTML =
      _renderFilterBreakdownRows(filterResults, summary.total_rows);
  } else {
    document.getElementById("valBreakdownCard").style.display = "none";
  }

  // Show view toggle
  const toggle = document.getElementById("valViewToggle");
  if (toggle) {
    toggle.style.display = "flex";
    const badge = document.getElementById("valFailedCount");
    if (badge) {
      badge.textContent   = summary.failed > 0 ? summary.failed : "";
      badge.style.display = summary.failed > 0 ? "inline-flex" : "none";
    }
  }

  // Show predefined summary panel below stats if predefined rules fired
  if (pv && pv.has_failures) {
    _showPredefinedSummaryPanel(pv);
  }
}

/**
 * Show a compact predefined rules summary panel above the filter breakdown.
 * Lists all failed rules with their colour, count, and a short description.
 */
function _showPredefinedSummaryPanel(pv) {
  let panel = document.getElementById("predefinedSummaryPanel");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "predefinedSummaryPanel";
    panel.style.cssText = `
      background:var(--bg-card);border:1px solid var(--border);border-radius:var(--r-lg);
      padding:14px 18px;display:flex;flex-direction:column;gap:10px;
    `;
    // Insert before valBreakdownCard
    const breakdownCard = document.getElementById("valBreakdownCard");
    if (breakdownCard && breakdownCard.parentNode) {
      breakdownCard.parentNode.insertBefore(panel, breakdownCard);
    } else {
      document.getElementById("valStatRow")?.after(panel);
    }
  }

  const failedRules = (pv.filter_results || []).filter(r => r.flagged_count > 0);
  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div>
        <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text-3)">Predefined Rule Failures</span>
        <span style="margin-left:10px;font-size:12px;color:var(--text-4)">${pv.failed.toLocaleString()} of ${pv.total_rows.toLocaleString()} rows affected</span>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${failedRules.map(r => {
          const dotColor = _RULE_COLOR_DOT[r.color] || _RULE_COLOR_DOT.red;
          return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;background:var(--bg-raised);border:1px solid var(--border)" title="${escapeHtml(r.label)}">
            <span style="width:6px;height:6px;border-radius:50%;background:${dotColor}"></span>
            ${escapeHtml(r.label.replace(/^R\d+_/, "").replace(/_/g, " "))}
            <span style="font-weight:400;color:var(--text-3)">· ${r.flagged_count}</span>
          </span>`;
        }).join("")}
      </div>
    </div>`;
  panel.style.display = "flex";
}

// ── Clear session ─────────────────────────────────────────────────────────────
function clearSession() {
  state.fileId = null; state.fileName = null; state.rowCount = null; state.columns = [];
  state.dataType = null; state.ipName = null; state.uuidColumn = null;
  state.columnRules = {}; state._lastRegexResult = null;
  state.valHlData = null; state.valHlPage = 1;
  state.predefinedValidation = null;
  const pvPanel = document.getElementById("predefinedSummaryPanel");
  if (pvPanel) pvPanel.style.display = "none";
  window.allColumns = []; window.allColumns2 = [];
  state.dataset2FileId = null; state.dataset2FileName = null;
  if (typeof _renderDataset2Slot === "function") _renderDataset2Slot();
  if (typeof syncColumns === "function") syncColumns();
  document.getElementById("uploadZone").style.display  = "block";
  document.getElementById("fileCard").style.display    = "none";
  document.getElementById("configGrid").style.display  = "none";
  document.getElementById("runBar").style.display      = "none";
  document.getElementById("recoTriggerBar").style.display = "none";
  closeRecoModal();
  document.getElementById("fileInput").value      = "";
  document.getElementById("paletteList").innerHTML = "";
  document.getElementById("recoColList").innerHTML = "";
  document.getElementById("topbarFile").style.display   = "none";
  document.getElementById("clearBtn").style.display     = "none";
  document.getElementById("sidebarFileChip").style.display = "none";
  setStatus("idle", "Awaiting file");
  showToast("Session cleared.", "info");
}

// ── UI helpers ────────────────────────────────────────────────────────────────
function showLoader(msg = "Processing…") {
  document.getElementById("loaderOverlay").style.display = "flex";
  document.getElementById("loaderMsg").textContent       = msg;
}
function loaderMsg(msg) { const el = document.getElementById("loaderMsg"); if (el) el.textContent = msg; }
function hideLoader()   { document.getElementById("loaderOverlay").style.display = "none"; }
function showToast(msg, type = "info", dur = 3500) {
  const t = document.getElementById("toast"); if (!t) return;
  t.textContent = msg; t.className = `toast ${type} show`;
  clearTimeout(t._timer); t._timer = setTimeout(() => t.classList.remove("show"), dur);
}
function setStatus(st, text) {
  document.getElementById("statusDot").className    = `status-dot ${st}`;
  document.getElementById("statusText").textContent = text;
}
function capitalize(s)   { return s.charAt(0).toUpperCase() + s.slice(1); }
function escapeHtml(s)   {
  if (s == null) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
                  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
function truncate(s, max) { return s.length > max ? s.slice(0, max - 1) + "…" : s; }
// ══════════════════════════════════════════════════════════════════════════════
// INLINE DATASET FILTER BAR  (Validate → Full Dataset tab)
// ══════════════════════════════════════════════════════════════════════════════
//
// Conditions that need a text value input:
const VIF_NEEDS_VALUE = new Set(["eq","neq","contains","ncontains","gt","lt","gte","lte","btwn",
  "date_before","date_after","date_year_eq"]);
// Conditions that need a 2nd column picker:
const VIF_NEEDS_COL2  = new Set(["cross"]);
// Conditions that need no extra input (just apply on selection):
const VIF_NO_INPUT    = new Set(["empty","notempty","dup","date_empty","date_invalid","date_future","date_past"]);

// State for the active inline filter
const _vif = { colIdx: null, cond: null, value: "", col2Idx: null, active: false };

// Called when the dataset card is shown — populate column dropdowns & show bar
function vifInit() {
  const result = state.valHlData;
  if (!result || !result.columns) return;
  const cols = result.columns;

  const colSel  = document.getElementById("vifColSelect");
  const col2Sel = document.getElementById("vifCol2Select");
  if (!colSel || !col2Sel) return;

  // Populate both column selects
  const opts = cols.map((c, i) => `<option value="${i}">${escapeHtml(c)}</option>`).join("");
  colSel.innerHTML  = `<option value="">Column…</option>${opts}`;
  col2Sel.innerHTML = `<option value="">Reference column…</option>${opts}`;

  // Reset state
  colSel.value  = "";
  col2Sel.value = "";
  document.getElementById("vifCondSelect").value = "";
  document.getElementById("vifValInput").value   = "";
  document.getElementById("vifCount").textContent = "";
  _vif.active = false;

  // Show the bar
  const bar = document.getElementById("valInlineFilter");
  if (bar) bar.style.display = "block";
}

// Called when column or condition changes — show/hide the right 3rd input
function vifOnCondChange() {
  const cond    = document.getElementById("vifCondSelect").value;
  const valInp  = document.getElementById("vifValInput");
  const col2Sel = document.getElementById("vifCol2Select");

  valInp.style.display  = VIF_NEEDS_VALUE.has(cond) ? "" : "none";
  col2Sel.style.display = VIF_NEEDS_COL2.has(cond)  ? "" : "none";

  // Set placeholder hint
  const hints = {
    btwn: "lo,hi  e.g. 10,50", gt:"number", lt:"number",
    gte:"number", lte:"number",
    date_before:"YYYY-MM-DD", date_after:"YYYY-MM-DD", date_year_eq:"e.g. 2023",
  };
  valInp.placeholder = hints[cond] || "Value…";

  // Auto-apply no-input conditions immediately
  if (VIF_NO_INPUT.has(cond)) vifApply();
}

// Apply the current filter to valHlData rows and re-render
function vifApply() {
  const result = state.valHlData;
  if (!result) return;

  const colIdxStr = document.getElementById("vifColSelect").value;
  const cond      = document.getElementById("vifCondSelect").value;
  const rawVal    = document.getElementById("vifValInput").value.trim();
  const col2Str   = document.getElementById("vifCol2Select").value;

  if (colIdxStr === "" || !cond) {
    // No filter selected — show all
    _vif.active = false;
    state.valHlPage = 1;
    _showValHighlightTable();
    document.getElementById("vifCount").textContent = "";
    return;
  }

  const colIdx = parseInt(colIdxStr);
  const col2   = col2Str !== "" ? parseInt(col2Str) : null;

  // Build reference pool for cross check
  let refPool = null;
  if (cond === "cross" && col2 !== null) {
    refPool = new Set(result.rows.map(cells => {
      const v = (cells[col2]?.value ?? "").toString().trim().toLowerCase();
      return v;
    }).filter(v => v && !["nan","none","null","n/a","","—"].includes(v)));
  }

  // Filter rows
  const NULL_SET = new Set(["","nan","none","null","n/a","na","nil","-","--","—"]);

  const filtered = result.rows.filter(cells => {
    const rawCell = (cells[colIdx]?.value ?? "").toString().trim();
    const low     = rawCell.toLowerCase();
    const isNull  = NULL_SET.has(low);
    const num     = parseFloat(rawCell);
    const matchLow = rawVal.toLowerCase();

    switch (cond) {
      case "empty":    return isNull;
      case "notempty": return !isNull;
      case "dup": {
        // Flag if this value appears more than once
        if (isNull) return false;
        return result.rows.filter(r =>
          (r[colIdx]?.value ?? "").toString().trim().toLowerCase() === low
        ).length > 1;
      }
      case "eq":       return !isNull && low === matchLow;
      case "neq":      return !isNull && low !== matchLow;
      case "contains": return !isNull && low.includes(matchLow);
      case "ncontains":return !isNull && !low.includes(matchLow);
      case "gt":       return !isNaN(num) && num >  parseFloat(rawVal);
      case "lt":       return !isNaN(num) && num <  parseFloat(rawVal);
      case "gte":      return !isNaN(num) && num >= parseFloat(rawVal);
      case "lte":      return !isNaN(num) && num <= parseFloat(rawVal);
      case "btwn": {
        const parts = rawVal.split(",");
        if (parts.length < 2) return false;
        const lo = parseFloat(parts[0]), hi = parseFloat(parts[1]);
        return !isNaN(num) && num >= lo && num <= hi;
      }
      case "cross":
        return !isNull && refPool && !refPool.has(low);
      case "date_empty":   return isNull;
      case "date_invalid": {
        if (isNull) return false;
        return isNaN(Date.parse(rawCell));
      }
      case "date_future": {
        const d = Date.parse(rawCell);
        return !isNaN(d) && d > Date.now();
      }
      case "date_past": {
        const d = Date.parse(rawCell);
        return !isNaN(d) && d < Date.now();
      }
      case "date_before": {
        const ref = Date.parse(rawVal);
        const d   = Date.parse(rawCell);
        return !isNaN(d) && !isNaN(ref) && d < ref;
      }
      case "date_after": {
        const ref = Date.parse(rawVal);
        const d   = Date.parse(rawCell);
        return !isNaN(d) && !isNaN(ref) && d > ref;
      }
      case "date_year_eq": {
        const d = new Date(rawCell);
        return !isNaN(d) && d.getFullYear() === parseInt(rawVal);
      }
      default: return true;
    }
  });

  _vif.active = true;

  // Temporarily override valHlData.rows for pagination
  const saved = result.rows;
  result._filteredRows = filtered;

  // Show count
  const countEl = document.getElementById("vifCount");
  if (countEl) countEl.textContent = `${filtered.length.toLocaleString()} of ${result.total_rows.toLocaleString()} rows`;

  // Re-render using filtered rows
  state.valHlPage = 1;
  _showValHighlightTableFiltered(filtered);
}

// Clear the inline filter and restore full dataset view
function vifClear() {
  document.getElementById("vifColSelect").value  = "";
  document.getElementById("vifCondSelect").value = "";
  document.getElementById("vifValInput").value   = "";
  document.getElementById("vifCol2Select").value = "";
  document.getElementById("vifValInput").style.display  = "none";
  document.getElementById("vifCol2Select").style.display = "none";
  document.getElementById("vifCount").textContent = "";
  _vif.active = false;
  state.valHlPage = 1;
  _showValHighlightTable();
}

// Variant of _showValHighlightTable that uses an explicit rows array
function _showValHighlightTableFiltered(rows) {
  const result = state.valHlData;
  if (!result) return;
  const card = document.getElementById("valDatasetCard");
  if (!card) return;
  card.style.display = "flex";

  const ps       = 100;
  const page     = state.valHlPage;
  const start    = (page - 1) * ps;
  const slice    = rows.slice(start, start + ps);
  const totalPgs = Math.max(1, Math.ceil(rows.length / ps));
  state.valHlTotalPages = totalPgs;

  // Reuse exact same head/body rendering as _showValHighlightTable
  const headEl = document.getElementById("valDatasetHead");
  if (headEl) {
    headEl.innerHTML = `<tr><th>#</th>${result.columns.map(c =>
      `<th title="${escapeHtml(c)}">${escapeHtml(c)}</th>`).join("")}</tr>`;
  }

  const startRow = start + 2;
  const bodyEl   = document.getElementById("valDatasetBody");
  if (bodyEl) {
    bodyEl.innerHTML = slice.map((cells, ri) => {
      const rowFailed = cells.some(c => (c.flags || []).includes("flag-val-fail"));
      return `<tr${rowFailed ? ' class="val-row-failed"' : ""}>
        <td class="row-num">${startRow + ri}</td>
        ${cells.map(cell => {
          const flags     = cell.flags || [];
          const cls       = _cellFlagClass(flags);
          const isNone    = cell.value === "" || cell.value === "None" || cell.value === "nan";
          const isCleaned = flags.includes("cell-auto-cleaned");
          const isReview  = flags.includes("cell-needs-review");
          const isValFail = flags.includes("flag-val-fail") || flags.includes("flag-val-cell-fail");
          const badge = isCleaned ? `<span class="cell-clean-badge">✓</span>`
                      : isReview  ? `<span class="cell-review-badge">?</span>`
                      : isValFail ? `<span class="cell-val-fail-badge">✗</span>` : "";
          return `<td class="${cls}${isNone ? " cell-none" : ""}">${isNone ? "—" : escapeHtml(cell.value)}${badge}</td>`;
        }).join("")}
      </tr>`;
    }).join("") ||
    `<tr><td colspan="${result.columns.length + 1}" style="text-align:center;padding:28px;color:var(--text-4)">No rows match this filter.</td></tr>`;
  }

  const infoEl = document.getElementById("valDatasetPageInfo");
  const pagEl  = document.getElementById("valDatasetPagination");
  if (pagEl) {
    pagEl.style.display = totalPgs > 1 ? "flex" : "none";
    if (infoEl) infoEl.textContent = `Page ${page} of ${totalPgs} · ${rows.length.toLocaleString()} rows (filtered)`;
    const prevBtn = pagEl.querySelector("button:first-child");
    const nextBtn = pagEl.querySelector("button:last-child");
    if (prevBtn) prevBtn.disabled = page <= 1;
    if (nextBtn) nextBtn.disabled = page >= totalPgs;
  }
}

// Override valHlChangePage to respect active filter
const _origValHlChangePage = valHlChangePage;
window.valHlChangePage = function(dir) {
  if (_vif.active && state.valHlData?._filteredRows) {
    const rows   = state.valHlData._filteredRows;
    const newPg  = (state.valHlPage || 1) + dir;
    const maxPg  = Math.max(1, Math.ceil(rows.length / 100));
    state.valHlPage = Math.max(1, Math.min(newPg, maxPg));
    _showValHighlightTableFiltered(rows);
  } else {
    _origValHlChangePage(dir);
  }
};

window.vifInit       = vifInit;
window.vifOnCondChange = vifOnCondChange;
window.vifApply      = vifApply;
window.vifClear      = vifClear;