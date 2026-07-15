// filter-palette.js
// Category tabs → draggable filter bubbles → column cards with drop zones.
// Feeds into the existing addSingleFilter / handleConditionUI pipeline.
// Includes full modal config for ALL filter types:
//   presence: empty, dup
//   value:    eq, neq, gt, lt, gte, lte, btwn, nbtwn
//   date:     date  (with full sub-condition suite)
//   cross:    cross, doublecross
//   logic:    and, or
//   compare:  compare
//   geo:      coords

// ── Filter catalog ────────────────────────────────────────────────────────────
const FP_CATALOG = {
  presence: [
    { key: "empty",       label: "Missing / Null",      color: "red",    hint: "Flags blank, null, or empty cells" },
    { key: "dup",         label: "Duplicate",            color: "orange", hint: "Flags duplicate values in a column" },
  ],
  value: [
    { key: "eq",          label: "Equal To",             color: "blue",   hint: "Value = X" },
    { key: "neq",         label: "Not Equal To",         color: "blue",   hint: "Value ≠ X" },
    { key: "gt",          label: "Greater Than",         color: "teal",   hint: "Value > X" },
    { key: "lt",          label: "Less Than",            color: "teal",   hint: "Value < X" },
    { key: "gte",         label: "≥ Greater or Equal",   color: "teal",   hint: "Value ≥ X" },
    { key: "lte",         label: "≤ Less or Equal",      color: "teal",   hint: "Value ≤ X" },
    { key: "btwn",        label: "Between",              color: "purple", hint: "min ≤ value ≤ max" },
    { key: "nbtwn",       label: "Not Between",          color: "purple", hint: "Value outside min–max range" },
  ],
  date: [
    { key: "date",        label: "Date Filter",          color: "green",  hint: "Full date condition suite — equal, before, after, range, year/month/day, weekday, etc." },
  ],
  cross: [
    { key: "cross",       label: "Cross Check",          color: "amber",  hint: "Match a column against Dataset 2" },
    { key: "doublecross", label: "Double Cross Check",   color: "amber",  hint: "Match two column pairs across both datasets" },
  ],
  logic: [
    { key: "and",         label: "AND",                  color: "indigo", hint: "All sub-conditions must pass" },
    { key: "or",          label: "OR",                   color: "indigo", hint: "Any sub-condition must pass" },
  ],
  compare: [
    { key: "compare",     label: "Compare",              color: "slate",  hint: "Column-to-column comparisons within Dataset 1" },
    { key: "compare2",    label: "Compare with Dataset 2", color: "slate", hint: "Same comparisons as Compare (equal, greater, between, date, AND/OR), against Dataset 2 instead of Dataset 1" },
  ],
  geo: [
    { key: "coords",      label: "Coordinate Check",     color: "geo",    hint: "Resolve lat/lng → district, tehsil, or UC and verify against a column value" },
  ],
};

const FP_COLOR_MAP = {
  red:    { bg: "#fee2e2", border: "#fca5a5", text: "#b91c1c" },
  orange: { bg: "#ffedd5", border: "#fdba74", text: "#c2410c" },
  blue:   { bg: "#dbeafe", border: "#93c5fd", text: "#1d4ed8" },
  teal:   { bg: "#ccfbf1", border: "#5eead4", text: "#0f766e" },
  purple: { bg: "#ede9fe", border: "#c4b5fd", text: "#6d28d9" },
  green:  { bg: "#dcfce7", border: "#86efac", text: "#15803d" },
  amber:  { bg: "#fef3c7", border: "#fcd34d", text: "#92400e" },
  indigo: { bg: "#e0e7ff", border: "#a5b4fc", text: "#3730a3" },
  slate:  { bg: "#f1f5f9", border: "#cbd5e1", text: "#334155" },
  geo:    { bg: "#ecfdf5", border: "#34d399", text: "#065f46" },
};

// Flat lookup: condition key → { label, color }
// Used to label chips and derive modal titles for any condition key.
const FP_KEY_LOOKUP = {};
Object.values(FP_CATALOG).forEach(list =>
  list.forEach(f => { FP_KEY_LOOKUP[f.key] = { label: f.label, color: f.color }; })
);

// ── Module state ──────────────────────────────────────────────────────────────
let _fpActiveCat  = "presence";
let _fpDragFilter = null;   // { key, label, color } while dragging
let _fpModal      = null;   // { ruleId, isNew, meta } while popup is open

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  _fpInitCategoryTabs();
  _fpRenderBubbles("presence");
  _fpInitModal();
});

// Called by uploader.js once columns are known
window.fpOnColumnsLoaded = function () {
  _fpRenderColGrid();
  // Reflect chips for any rules already restored from a saved profile.
  // restoreFilters uses nested setTimeouts (~320 ms total).
  setTimeout(() => {
    if (typeof window.fpReflectAllRules === "function") window.fpReflectAllRules();
  }, 380);
};

// ── Modal bootstrap ───────────────────────────────────────────────────────────
function _fpInitModal() {
  const overlay   = document.getElementById("fpModalOverlay");
  const closeBtn  = document.getElementById("fpModalClose");
  const cancelBtn = document.getElementById("fpModalCancelBtn");
  const saveBtn   = document.getElementById("fpModalSaveBtn");
  if (!overlay) return;

  overlay.addEventListener("mousedown", e => {
    if (e.target === overlay) window.fpModalCancel();
  });
  if (closeBtn)  closeBtn.addEventListener("click",  () => window.fpModalCancel());
  if (cancelBtn) cancelBtn.addEventListener("click", () => window.fpModalCancel());
  if (saveBtn)   saveBtn.addEventListener("click",   () => window.fpModalSave());

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && overlay.classList.contains("open")) window.fpModalCancel();
  });
}

// ── Category tabs ─────────────────────────────────────────────────────────────
function _fpInitCategoryTabs() {
  const tabs = document.getElementById("fpCategoryTabs");
  if (!tabs) return;
  tabs.querySelectorAll(".fp-cat-tab").forEach(btn => {
    btn.addEventListener("click", () => {
      tabs.querySelectorAll(".fp-cat-tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      _fpActiveCat = btn.dataset.cat;
      _fpRenderBubbles(_fpActiveCat);
    });
  });
}

// ── Bubble strip ──────────────────────────────────────────────────────────────
function _fpRenderBubbles(cat) {
  const strip = document.getElementById("fpBubbleStrip");
  if (!strip) return;
  strip.innerHTML = "";

  (FP_CATALOG[cat] || []).forEach(f => {
    const c    = FP_COLOR_MAP[f.color] || FP_COLOR_MAP.slate;
    const chip = document.createElement("div");
    chip.className  = "fp-bubble";
    chip.draggable  = true;
    chip.dataset.key = f.key;
    chip.title      = f.hint;
    chip.style.cssText = `background:${c.bg};border-color:${c.border};color:${c.text};`;
    chip.innerHTML  = `<span class="fp-bubble-label">${f.label}</span>`;

    chip.addEventListener("dragstart", e => {
      _fpDragFilter = f;
      chip.classList.add("dragging");
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("text/plain", f.key);
    });
    chip.addEventListener("dragend", () => {
      _fpDragFilter = null;
      chip.classList.remove("dragging");
    });

    // Click → add rule without a pre-bound column, open config popup
    chip.addEventListener("click", () => {
      const ruleId = _fpCreateRule(f.key, null);
      if (ruleId != null) _fpOpenModal(ruleId, { key: f.key, label: f.label, color: f.color }, true);
    });

    strip.appendChild(chip);
  });
}

// ── Column cards grid ─────────────────────────────────────────────────────────
function _fpRenderColGrid() {
  const grid = document.getElementById("fpColGrid");
  if (!grid) return;

  const cols = Array.isArray(window.allColumns) ? window.allColumns : [];
  if (cols.length === 0) { grid.style.display = "none"; return; }

  grid.innerHTML    = "";
  grid.style.display = "grid";

  // Clear the catch-all strip when a fresh dataset is loaded
  const strip = document.getElementById("fpGeneralStrip");
  if (strip) { strip.innerHTML = ""; strip.style.display = "none"; }

  cols.forEach((col, idx) => {
    const name = col.col1 || `Column ${idx + 1}`;
    const card = document.createElement("div");
    card.className      = "fp-col-card";
    card.dataset.colIdx = idx;

    card.innerHTML = `
      <div class="fp-col-head">
        <span class="fp-col-name" title="${name}">${name}</span>
        <span class="fp-col-badge" id="fpColBadge_${idx}"></span>
      </div>
      <div class="fp-col-zone" id="fpColZone_${idx}">
        <span class="fp-col-drop-hint">+ drop filter here</span>
      </div>
    `;

    card.addEventListener("dragover",  e => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; card.classList.add("fp-drop-over"); });
    card.addEventListener("dragleave", ()  => card.classList.remove("fp-drop-over"));
    card.addEventListener("drop", e => {
      e.preventDefault();
      card.classList.remove("fp-drop-over");
      if (_fpDragFilter) _fpAddFilterToColumn(_fpDragFilter, idx, name);
    });

    grid.appendChild(card);
  });
}

// ── Drop a bubble onto a column card ─────────────────────────────────────────
function _fpAddFilterToColumn(filter, colIdx, colName) {
  const ruleId = _fpCreateRule(filter.key, colIdx);
  if (ruleId == null) return;
  _fpOpenModal(ruleId, { key: filter.key, label: filter.label, color: filter.color, colName }, true);
}

// ── Public: "＋ Add Filter" button (no column pre-wired) ──────────────────────
window.fpAddBlankFilter = function () {
  if (typeof syncColumns === "function") syncColumns();
  const cols = Array.isArray(window.allColumns) ? window.allColumns : [];
  if (cols.length === 0) {
    if (typeof showToast === "function") showToast("Upload Dataset 1 first.", "error");
    else alert("Please upload Dataset 1 first.");
    return;
  }
  const container = document.getElementById("tablesContainer");
  if (!container) return;
  totalTables++;
  const i = totalTables;
  addSingleFilter(container, i);
  _fpOpenModal(i, { key: null, label: "New Filter", color: "slate" }, true);
};

// ── Create a hidden rule card (optionally pre-wiring condition + column) ──────
function _fpCreateRule(condKey, colIdx) {
  if (typeof syncColumns === "function") syncColumns();
  const container = document.getElementById("tablesContainer");
  if (!container) return null;

  totalTables++;
  const i = totalTables;
  addSingleFilter(container, i);

  // Pre-set condition
  const condSel = document.getElementById(`condSelect_${i}`);
  if (condSel && condKey) {
    condSel.value = condKey;
    handleConditionUI(i);
  }

  // Pre-select column when dropped onto a column card
  if (colIdx !== undefined && colIdx !== null) {
    const colSel = document.getElementById(`colSelect_${i}`);
    if (colSel) {
      colSel.value = String(colIdx);
      colSel.dispatchEvent(new Event("change"));
      const combo = colSel.closest(".col-combo-wrap");
      if (combo) {
        const inp = combo.querySelector(".col-combo-input");
        const opt = colSel.options[colSel.selectedIndex];
        if (inp && opt) { inp.value = opt.text; inp.classList.add("has-value"); }
      }
    }
    // Stamp the column name onto the card so fpReflectAllRules() can find it
    // by name (its primary resolution path) rather than relying solely on
    // the dropdown's current value — keeps chip placement correct even if
    // the dropdown gets rebuilt/re-synced between now and the popup reopening.
    const cardEl = document.getElementById(`tableBox_${i}`);
    const cols   = Array.isArray(window.allColumns) ? window.allColumns : [];
    const cName  = cols[colIdx]?.col1;
    if (cardEl && cName) cardEl.dataset.colName = cName;
  }

  return i;
}

/* ════════════════════════════════════════════════════════════════════════════
   CONFIG POPUP
   The live rule card (its actual DOM node) is moved into the popup body so
   all existing event handlers, IDs and wiring stay intact.
   On save / cancel the node is moved back to #tablesContainer.
════════════════════════════════════════════════════════════════════════════ */
function _fpOpenModal(ruleId, meta, isNew) {
  const overlay = document.getElementById("fpModalOverlay");
  const body    = document.getElementById("fpModalBody");
  const titleEl = document.getElementById("fpModalTitle");
  const subEl   = document.getElementById("fpModalSub");
  const box     = document.getElementById(`tableBox_${ruleId}`);
  if (!overlay || !body || !box) return;

  _fpModal = { ruleId, isNew: !!isNew, meta: meta || {} };

  // ── Modal title
  if (titleEl) titleEl.textContent = meta?.label || "Configure Filter";

  // ── Subtitle — show bound column name if known
  if (subEl) {
    const colSel  = document.getElementById(`colSelect_${ruleId}`);
    const colName = meta?.colName
      || (colSel && colSel.value !== "" ? (colSel.options[colSel.selectedIndex]?.text || "") : "");
    subEl.textContent = colName ? `Column: ${colName}` : _fpCondDescription(meta?.key);
  }

  // Move the live rule card into the popup
  body.innerHTML = "";
  body.appendChild(box);

  // ── Inject the inline helper panel for the current condition
  _fpBuildInlineHelper(ruleId, meta?.key);

  overlay.classList.add("open");
  document.body.classList.add("fp-modal-lock");

  // Fire column change so unique-value selects populate
  setTimeout(() => {
    const colSel = document.getElementById(`colSelect_${ruleId}`);
    if (colSel && colSel.value !== "") colSel.dispatchEvent(new Event("change", { bubbles: true }));
  }, 30);
}

/* Build a small human-readable description of a condition key */
function _fpCondDescription(key) {
  const descs = {
    empty:       "Flags rows where this column is blank or null",
    dup:         "Flags rows where this column has a duplicate value",
    eq:          "Flags rows where the value equals a threshold",
    neq:         "Flags rows where the value does not equal a threshold",
    gt:          "Flags rows where the value is greater than a threshold",
    lt:          "Flags rows where the value is less than a threshold",
    gte:         "Flags rows where the value is ≥ a threshold",
    lte:         "Flags rows where the value is ≤ a threshold",
    btwn:        "Flags rows where the value falls within a min–max range",
    nbtwn:       "Flags rows where the value falls outside a min–max range",
    date:        "Applies a date-based condition (before, after, between, year, month, etc.)",
    cross:       "Cross-checks this column against a matching column in Dataset 2",
    doublecross: "Cross-checks two column pairs simultaneously across both datasets",
    and:         "Flags rows that fail ALL of the sub-conditions",
    or:          "Flags rows that fail ANY of the sub-conditions",
    compare:     "Compares two columns within Dataset 1",
    compare2:    "Compares a Dataset 1 column against a Dataset 2 column, row by row",
    coords:      "Resolves GPS coordinates to an admin area and checks against a column",
  };
  return descs[key] || "Set the condition for this rule";
}

/* ── Inline helper panel rendered inside the popup body ──────────────────────
   Each condition type gets a small summary / reminder block that sits ABOVE
   the rule card so the user knows what they are configuring.              */
function _fpBuildInlineHelper(ruleId, condKey) {
  const body = document.getElementById("fpModalBody");
  if (!body) return;

  // Remove any previous helper
  body.querySelectorAll(".fp-inline-helper").forEach(el => el.remove());

  if (!condKey) return;

  const c   = FP_COLOR_MAP[FP_KEY_LOOKUP[condKey]?.color] || FP_COLOR_MAP.slate;
  const tip = _fpCondHelperHTML(condKey);
  if (!tip) return;

  const helper       = document.createElement("div");
  helper.className   = "fp-inline-helper";
  helper.style.cssText = `
    margin-bottom: 12px;
    padding: 10px 14px;
    border-radius: 8px;
    border-left: 3px solid ${c.border};
    background: ${c.bg};
    color: ${c.text};
    font-size: 12px;
    line-height: 1.6;
  `;
  helper.innerHTML   = tip;

  // Insert before the rule card node (which is always the last child after body.appendChild)
  const ruleCard = document.getElementById(`tableBox_${ruleId}`);
  if (ruleCard && body.contains(ruleCard)) body.insertBefore(helper, ruleCard);
  else body.prepend(helper);
}

/* Returns an HTML string describing what each condition expects */
function _fpCondHelperHTML(key) {
  const tips = {
    empty:
      "<strong>Missing / Null</strong> — select a column. Any row where that cell is blank, <code>null</code>, <code>nan</code>, or <code>N/A</code> will be flagged.",
    dup:
      "<strong>Duplicate</strong> — select a column. Every row whose value appears more than once in that column will be flagged.",
    eq:
      "<strong>Equal To</strong> — select a column, then enter a value (or pick from Unique Values). Flags every row whose cell matches that value exactly.",
    neq:
      "<strong>Not Equal To</strong> — flags every row whose cell does <em>not</em> match the value you enter.",
    gt:
      "<strong>Greater Than</strong> — enter a numeric threshold. Flags rows where the cell value is greater than that number.",
    lt:
      "<strong>Less Than</strong> — enter a numeric threshold. Flags rows where the cell value is less than that number.",
    gte:
      "<strong>Greater Than or Equal To</strong> — flags rows where the value is ≥ the threshold.",
    lte:
      "<strong>Less Than or Equal To</strong> — flags rows where the value is ≤ the threshold.",
    btwn:
      "<strong>Between</strong> — enter <code>min,max</code> (e.g. <code>10,50</code>). Flags rows where the value falls within that inclusive range.",
    nbtwn:
      "<strong>Not Between</strong> — same format as Between. Flags rows whose value falls <em>outside</em> that range.",
    date:
      "<strong>Date Filter</strong> — pick a column and a date condition (equal, before, after, between, year, month, weekday, etc.). The column must contain parseable date strings.",
    cross:
      "<strong>Cross Check</strong> — picks one column from Dataset 1 and one from Dataset 2. Flags rows where the Dataset 1 value is not found in Dataset 2.",
    doublecross:
      "<strong>Double Cross Check</strong> — simultaneously checks two column pairs across both datasets. Both pairs must mismatch for a row to be flagged.",
    and:
      "<strong>AND</strong> — add two or more sub-conditions. A row is flagged only when <em>all</em> sub-conditions fail.",
    or:
      "<strong>OR</strong> — add two or more sub-conditions. A row is flagged when <em>any</em> sub-condition fails.",
    compare:
      "<strong>Compare</strong> — compares two columns within Dataset 1 using a condition (equal, greater, between, date, AND/OR). Flags mismatches.",
    compare2:
      "<strong>Compare with Dataset 2</strong> — same conditions as Compare (equal, greater, between, date, AND/OR), but Column 2 comes from Dataset 2 instead of Dataset 1. Rows are matched by position (row 1 of Dataset 1 vs row 1 of Dataset 2, and so on) — the two files should be in the same row order for this to make sense.",
    coords:
      "<strong>Coordinate Check</strong> — select Longitude and Latitude columns, choose an admin level (District / Tehsil / UC), then pick a column whose value should match the resolved area name.",
  };
  return tips[key] || null;
}

/* ── Close the popup shell (does NOT discard the rule) ───────────────────── */
function _fpCloseModalShell() {
  const overlay   = document.getElementById("fpModalOverlay");
  const body      = document.getElementById("fpModalBody");
  const container = document.getElementById("tablesContainer");

  if (_fpModal) {
    // Return the rule card to the hidden container
    const box = document.getElementById(`tableBox_${_fpModal.ruleId}`);
    if (box && container) container.appendChild(box);
  }
  if (body)    body.innerHTML = "";
  if (overlay) overlay.classList.remove("open");
  document.body.classList.remove("fp-modal-lock");
}

/* ── Save ────────────────────────────────────────────────────────────────── */
window.fpModalSave = function () {
  if (!_fpModal) { _fpCloseModalShell(); return; }
  const { ruleId, meta } = _fpModal;

  const condSel = document.getElementById(`condSelect_${ruleId}`);
  if (!condSel || condSel.value === "") {
    if (typeof showToast === "function") showToast("Pick a condition first.", "error");
    return; // keep popup open
  }

  // Validate required fields per condition
  const validErr = _fpValidateRule(ruleId, condSel.value);
  if (validErr) {
    if (typeof showToast === "function") showToast(validErr, "error");
    return;
  }

  const condKey  = condSel.value;
  const looked   = FP_KEY_LOOKUP[condKey] || {};
  const chipMeta = {
    key:   condKey,
    label: looked.label || meta?.label || condKey,
    color: looked.color || meta?.color || "slate",
  };

  _fpModal.isNew = false;
  _fpCloseModalShell();

  // Keep the card's saved-column-name stamp in sync with whatever column is
  // actually selected right now — otherwise editing a filter's column here
  // would leave dataset.colName pointing at the OLD column, and the next
  // time the popup reopens (which wipes and rebuilds all chips from scratch)
  // the chip would reappear on the wrong column.
  {
    const cardEl = document.getElementById(`tableBox_${ruleId}`);
    const colSel = document.getElementById(`colSelect_${ruleId}`);
    const cols   = Array.isArray(window.allColumns) ? window.allColumns : [];
    if (cardEl && colSel && colSel.value !== "") {
      const cName = cols[parseInt(colSel.value)]?.col1;
      if (cName) cardEl.dataset.colName = cName;
    }
  }

  // Place chip(s) on column card(s)
  if (condKey === "coords") {
    const lngSel = document.getElementById(`coordLngCol_${ruleId}`);
    const lngIdx = lngSel && lngSel.value !== "" ? parseInt(lngSel.value) : null;
    _fpRefreshChip(ruleId, chipMeta, lngIdx);
    const latSel = document.getElementById(`coordLatCol_${ruleId}`);
    const latIdx = latSel && latSel.value !== "" ? parseInt(latSel.value) : null;
    if (latIdx !== null) _fpPlaceCoordsLatChip(ruleId, latIdx);
  } else {
    _fpRefreshChip(ruleId, chipMeta);
  }

  if (typeof showToast === "function") showToast("Filter saved", "success");
  _fpAutoSave();
};

/* ── Cancel ──────────────────────────────────────────────────────────────── */
window.fpModalCancel = function () {
  if (!_fpModal) { _fpCloseModalShell(); return; }
  const { ruleId, isNew } = _fpModal;
  const wasNew = isNew;
  _fpCloseModalShell();
  if (wasNew) {
    if (typeof removeTable === "function") removeTable(ruleId);
    _fpRemoveChip(ruleId);
  }
};

/* ── Validation on save — return an error string or null ─────────────────── */
function _fpValidateRule(ruleId, condKey) {
  // Conditions that need at least a column selected
  const needsCol = ["empty","dup","eq","neq","gt","lt","gte","lte","btwn","nbtwn","date","cross","doublecross","compare","compare2"];
  if (needsCol.includes(condKey)) {
    // For compare/compare2, colSelect may be absent when sub-cond is AND/OR (each sub-row has its own cols)
    if (condKey !== "compare" && condKey !== "compare2") {
      const colSel = document.getElementById(`colSelect_${ruleId}`);
      if (!colSel || colSel.value === "") return "Please select a column.";
    }
  }

  // Value-threshold conditions need a matchVal
  if (["eq","neq","gt","lt","gte","lte"].includes(condKey)) {
    const mi = document.getElementById(`matchInput_${ruleId}`);
    if (!mi || mi.value.trim() === "") return "Please enter a value to match.";
  }

  // Between / Not Between need min,max format
  if (["btwn","nbtwn"].includes(condKey)) {
    const mi = document.getElementById(`matchInput_${ruleId}`);
    if (!mi || !mi.value.includes(",")) return "Enter range as  min,max  e.g.  10,50";
  }

  // Date needs a sub-condition
  if (condKey === "date") {
    const dc = document.getElementById(`dateCond_${ruleId}`);
    if (!dc || dc.value === "") return "Please select a date condition.";
  }

  // Coords needs lng, lat, level, verify columns
  if (condKey === "coords") {
    const lng = document.getElementById(`coordLngCol_${ruleId}`);
    const lat = document.getElementById(`coordLatCol_${ruleId}`);
    const lvl = document.getElementById(`coordLevel_${ruleId}`);
    const ver = document.getElementById(`coordVerifyCol_${ruleId}`);
    if (!lng?.value) return "Select the Longitude column.";
    if (!lat?.value) return "Select the Latitude column.";
    if (!lvl?.value) return "Choose an admin level (District / Tehsil / UC).";
    if (!ver?.value) return "Select the Verification column.";
  }

  // AND / OR need at least 2 sub-filters with conditions set
  if (condKey === "and" || condKey === "or") {
    const rows = document.querySelectorAll(`#subRows_${ruleId} .subfilter`);
    const valid = [...rows].filter(r => {
      const sc = r.querySelector("select[id^='sub_cond_']");
      return sc && sc.value !== "";
    });
    if (valid.length < 2) return "Add at least 2 sub-conditions.";
  }

  return null; // no error
}

/* ════════════════════════════════════════════════════════════════════════════
   CHIPS — visual tokens on column cards representing saved rules
════════════════════════════════════════════════════════════════════════════ */

function _fpRefreshChip(ruleId, meta, explicitColIdx) {
  _fpRemoveChip(ruleId);

  // Resolve column index: explicit (from by-name lookup) → hidden select → lng col (coords)
  let colIdx = (explicitColIdx !== undefined && explicitColIdx !== null)
    ? String(explicitColIdx) : null;
  if (colIdx === null) {
    const colSel = document.getElementById(`colSelect_${ruleId}`);
    if (colSel && colSel.value !== "") colIdx = colSel.value;
  }
  if (colIdx === null) {
    const lngSel = document.getElementById(`coordLngCol_${ruleId}`);
    if (lngSel && lngSel.value !== "") colIdx = lngSel.value;
  }

  const c    = FP_COLOR_MAP[meta.color] || FP_COLOR_MAP.slate;
  const chip = _fpMakeChip(ruleId, meta, c, "Click to edit");

  if (colIdx !== null) {
    const zone = document.getElementById(`fpColZone_${colIdx}`);
    if (zone) {
      const hint = zone.querySelector(".fp-col-drop-hint");
      if (hint) hint.style.display = "none";
      zone.appendChild(chip);
      _fpUpdateColBadge(colIdx);
      return;
    }
  }

  // No bound column (AND/OR, multi-column rules) → catch-all strip
  const strip = document.getElementById("fpGeneralStrip");
  if (strip) {
    chip.title = "Click to edit (multi-column rule)";
    strip.appendChild(chip);
    strip.style.display = "flex";
  }
}

/* Secondary lat chip for coords rules */
function _fpPlaceCoordsLatChip(ruleId, latColIdx) {
  document.querySelectorAll(`.fp-col-bubble[data-rule-id="${ruleId}"][data-coords-lat="1"]`).forEach(el => {
    const zone = el.closest(".fp-col-zone");
    el.remove();
    if (zone) {
      const idx = zone.id.replace("fpColZone_", "");
      if (!zone.querySelector(".fp-col-bubble")) {
        const hint = zone.querySelector(".fp-col-drop-hint");
        if (hint) hint.style.display = "";
      }
      _fpUpdateColBadge(idx);
    }
  });

  const c    = FP_COLOR_MAP["geo"] || FP_COLOR_MAP.slate;
  const chip = _fpMakeChip(ruleId, { key: "coords", label: "Coordinate Check (Lat)", color: "geo" }, c, "Click to edit (Lat column)");
  chip.dataset.coordsLat = "1";

  const zone = document.getElementById(`fpColZone_${latColIdx}`);
  if (zone) {
    const hint = zone.querySelector(".fp-col-drop-hint");
    if (hint) hint.style.display = "none";
    zone.appendChild(chip);
    _fpUpdateColBadge(latColIdx);
  }
}

/* Build a chip DOM element */
function _fpMakeChip(ruleId, meta, c, titleText) {
  const chip             = document.createElement("div");
  chip.className         = "fp-col-bubble";
  chip.dataset.ruleId    = ruleId;
  chip.dataset.filterKey = meta.key || "";
  chip.style.cssText     = `background:${c.bg};border-color:${c.border};color:${c.text};cursor:pointer;`;
  chip.title             = titleText || "Click to edit";
  chip.innerHTML         = `<span>${meta.label}</span><button class="fp-col-bubble-x" title="Remove">×</button>`;

  chip.querySelector("span").addEventListener("click", () => _fpOpenModal(ruleId, meta, false));
  chip.querySelector(".fp-col-bubble-x").addEventListener("click", e => {
    e.stopPropagation();
    if (typeof removeTable === "function") removeTable(ruleId);
    _fpRemoveChip(ruleId);
    setTimeout(_fpAutoSave, 50);
  });

  return chip;
}

function _fpRemoveChip(ruleId) {
  document.querySelectorAll(`.fp-col-bubble[data-rule-id="${ruleId}"]`).forEach(el => {
    const zone  = el.closest(".fp-col-zone");
    const strip = el.closest("#fpGeneralStrip");
    el.remove();
    if (zone) {
      const idx = zone.id.replace("fpColZone_", "");
      if (!zone.querySelector(".fp-col-bubble")) {
        const hint = zone.querySelector(".fp-col-drop-hint");
        if (hint) hint.style.display = "";
      }
      _fpUpdateColBadge(idx);
    }
    if (strip && !strip.querySelector(".fp-col-bubble")) strip.style.display = "none";
  });
}

function _fpUpdateColBadge(colIdx) {
  const zone  = document.getElementById(`fpColZone_${colIdx}`);
  const badge = document.getElementById(`fpColBadge_${colIdx}`);
  if (!zone || !badge) return;
  const count = zone.querySelectorAll(".fp-col-bubble").length;
  badge.textContent   = count ? `${count} rule${count > 1 ? "s" : ""}` : "";
  badge.style.display = count ? "inline-flex" : "none";
}

/* Clear all chips (e.g. when switching profiles) */
window.fpClearAllChips = function () {
  document.querySelectorAll(".fp-col-zone .fp-col-bubble").forEach(el => el.remove());
  document.querySelectorAll(".fp-col-zone .fp-col-drop-hint").forEach(h => h.style.display = "");
  document.querySelectorAll(".fp-col-badge, [id^='fpColBadge_']").forEach(b => { b.textContent = ""; b.style.display = "none"; });
  const strip = document.getElementById("fpGeneralStrip");
  if (strip) { strip.innerHTML = ""; strip.style.display = "none"; }
};

/* Rebuild chips for every existing (hidden) rule — called after restoreFilters */
window.fpReflectAllRules = function () {
  const cols = Array.isArray(window.allColumns) ? window.allColumns : [];

  document.querySelectorAll("#tablesContainer .rule-card").forEach(card => {
    const m = (card.id || "").match(/tableBox_(\d+)/);
    if (!m) return;
    const ruleId  = parseInt(m[1], 10);
    const condSel = document.getElementById(`condSelect_${ruleId}`);
    if (!condSel || condSel.value === "") return;
    const looked  = FP_KEY_LOOKUP[condSel.value] || {};

    // Resolve column index by saved name (stamped during restoreFilters)
    const savedName = (card.dataset.colName || "").trim();
    let resolvedColIdx = null;
    if (savedName && cols.length > 0) {
      const idx = cols.findIndex(c => (c.col1 || "").trim().toLowerCase() === savedName.toLowerCase());
      if (idx !== -1) resolvedColIdx = idx;
    }
    if (resolvedColIdx === null) {
      const colSel = document.getElementById(`colSelect_${ruleId}`);
      if (colSel && colSel.value !== "") resolvedColIdx = parseInt(colSel.value);
    }

    _fpRefreshChip(ruleId, {
      key:   condSel.value,
      label: looked.label || condSel.value,
      color: looked.color || "slate",
    }, resolvedColIdx);

    // For coords also place secondary lat chip
    if (condSel.value === "coords") {
      const latSel = document.getElementById(`coordLatCol_${ruleId}`);
      const latIdx = latSel && latSel.value !== "" ? parseInt(latSel.value) : null;
      if (latIdx !== null) _fpPlaceCoordsLatChip(ruleId, latIdx);
    }
  });
};

// ── Auto-save to active project on any chip change ────────────────────────────
function _fpAutoSave() {
  if (typeof saveFiltersToProject === "function") saveFiltersToProject(true); // silent
}

/* ════════════════════════════════════════════════════════════════════════════
   FILTER SUMMARY PANEL
   Renders a compact human-readable summary of all active filters below the
   column card grid, so the user can review the full rule set at a glance.
════════════════════════════════════════════════════════════════════════════ */
window.fpRenderFilterSummary = function () {
  const panel = document.getElementById("fpFilterSummary");
  if (!panel) return;

  if (typeof loadGeneratedFilters === "function") loadGeneratedFilters();
  const configs = window.allConfigs || [];
  if (configs.length === 0) { panel.style.display = "none"; return; }

  const cols = Array.isArray(window.allColumns) ? window.allColumns : [];
  const colName = idx => (cols[idx]?.col1) || `Col ${idx}`;

  const rows = configs.map((f, n) => {
    let desc = "";
    if (f.cond === "empty")  desc = `<b>${colName(f.colIdx)}</b> is blank / null`;
    else if (f.cond === "dup")   desc = `<b>${colName(f.colIdx)}</b> is duplicate`;
    else if (["eq","neq","gt","lt","gte","lte"].includes(f.cond)) {
      const labels = { eq:"=", neq:"≠", gt:">", lt:"<", gte:"≥", lte:"≤" };
      desc = `<b>${colName(f.colIdx)}</b> ${labels[f.cond]} <code>${f.matchVal}</code>`;
    } else if (f.cond === "btwn")  desc = `<b>${colName(f.colIdx)}</b> between <code>${f.matchVal}</code>`;
    else if (f.cond === "nbtwn") desc = `<b>${colName(f.colIdx)}</b> not between <code>${f.matchVal}</code>`;
    else if (f.cond === "date")  desc = `<b>${colName(f.colIdx)}</b> date: <code>${f.dateCond || "?"}</code> ${f.dateVal1||""} ${f.dateVal2?"→ "+f.dateVal2:""}`;
    else if (f.cond === "cross")       desc = `<b>${colName(f.colIdx)}</b> cross-check vs Dataset 2`;
    else if (f.cond === "doublecross") desc = `Double cross-check — <b>${colName(f.colIdx)}</b> & <b>${colName(f.col2Idx||0)}</b>`;
    else if (f.cond === "and" || f.cond === "or") {
      const subs = (f.subfilters||[]).map(sf => `${colName(sf.colIdx)} ${sf.cond} ${sf.matchVal||""}`).join(` <b>${f.cond.toUpperCase()}</b> `);
      desc = subs || `${f.cond.toUpperCase()} (${(f.subfilters||[]).length} sub-conditions)`;
    } else if (f.cond === "compare") {
      desc = `Compare columns using <code>${f.subCond||"?"}</code>`;
    } else if (f.cond === "compare2") {
      desc = `Compare with Dataset 2 using <code>${f.subCond||"?"}</code>`;
    } else if (f.cond === "coords") {
      desc = `Coords: <b>${colName(f.lngColIdx)}</b> / <b>${colName(f.latColIdx)}</b> → ${f.coordLevel||"?"} vs <b>${colName(f.verifyColIdx)}</b>`;
    } else {
      desc = `<code>${f.cond}</code> on <b>${colName(f.colIdx)}</b>`;
    }

    const meta  = FP_KEY_LOOKUP[f.cond] || { label: f.cond, color: "slate" };
    const c     = FP_COLOR_MAP[meta.color] || FP_COLOR_MAP.slate;
    return `
      <div class="fp-summary-row" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
        <span class="fp-summary-num" style="min-width:20px;font-size:11px;color:var(--text-4);font-weight:600">${n+1}</span>
        <span class="fp-summary-badge" style="padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600;background:${c.bg};color:${c.text};border:1px solid ${c.border};white-space:nowrap">${meta.label}</span>
        <span class="fp-summary-desc" style="font-size:12px;color:var(--text-2);flex:1">${desc}</span>
      </div>`;
  }).join("");

  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <span style="font-weight:600;font-size:13px;color:var(--text-1)">${configs.length} active filter${configs.length !== 1 ? "s" : ""}</span>
      <button onclick="fpClearAllFilters()" style="font-size:11px;color:var(--danger);background:none;border:none;cursor:pointer;padding:0">Clear all</button>
    </div>
    ${rows}
  `;
  panel.style.display = "block";
};

/* Clear all rules and chips */
window.fpClearAllFilters = function () {
  if (typeof window.fpClearAllChips === "function") window.fpClearAllChips();
  document.querySelectorAll("#tablesContainer .rule-card").forEach(card => {
    const m = (card.id || "").match(/tableBox_(\d+)/);
    if (m && typeof removeTable === "function") removeTable(parseInt(m[1], 10));
  });
  const panel = document.getElementById("fpFilterSummary");
  if (panel) panel.style.display = "none";
  _fpAutoSave();
};

/* ════════════════════════════════════════════════════════════════════════════
   CONDITION-SPECIFIC QUICK ACTIONS
   Tiny helper buttons that appear inside the modal for specific conditions,
   giving the user one-click shortcuts to common configurations.
════════════════════════════════════════════════════════════════════════════ */

/* Called when condSelect changes inside the modal — rebuilds helper + subtitle */
window.fpOnCondChange = function (ruleId) {
  const condSel = document.getElementById(`condSelect_${ruleId}`);
  if (!condSel) return;
  const condKey = condSel.value;

  // Update modal title/sub
  const titleEl = document.getElementById("fpModalTitle");
  const subEl   = document.getElementById("fpModalSub");
  if (titleEl && condKey) titleEl.textContent = FP_KEY_LOOKUP[condKey]?.label || condKey;
  if (subEl)  subEl.textContent = _fpCondDescription(condKey);

  // Rebuild inline helper
  if (_fpModal && _fpModal.ruleId === ruleId) _fpBuildInlineHelper(ruleId, condKey);

  // Delegate to tables.js condition UI builder
  if (typeof handleConditionUI === "function") handleConditionUI(ruleId);
};

/* Wire the condSelect to fpOnCondChange (called from addSingleFilter in tables.js) */
window.fpWireCondSelect = function (ruleId) {
  const condSel = document.getElementById(`condSelect_${ruleId}`);
  if (!condSel) return;
  condSel.addEventListener("change", () => window.fpOnCondChange(ruleId));
};

/* ════════════════════════════════════════════════════════════════════════════
   KEYBOARD NAVIGATION INSIDE MODAL
   Tab cycles through visible interactive fields.
   Enter on the Save button submits.
════════════════════════════════════════════════════════════════════════════ */
document.addEventListener("keydown", e => {
  const overlay = document.getElementById("fpModalOverlay");
  if (!overlay || !overlay.classList.contains("open")) return;
  if (e.key === "Enter" && e.target.tagName !== "SELECT" && e.target.tagName !== "TEXTAREA") {
    e.preventDefault();
    window.fpModalSave();
  }
});

/* ════════════════════════════════════════════════════════════════════════════
   PUBLIC: CHIP COUNT BADGE REFRESH
   Called externally when a rule is removed programmatically (e.g. toolbar tools).
════════════════════════════════════════════════════════════════════════════ */
window.fpRefreshAllBadges = function () {
  const cols = Array.isArray(window.allColumns) ? window.allColumns : [];
  cols.forEach((_, idx) => _fpUpdateColBadge(idx));
};