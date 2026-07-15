// clean_review.js
// Adds two interactions to the Clean view, additive only — does not touch
// any existing validate/clean workflow:
//   1. Click a column header  -> popup listing every flagged value in that
//      column (auto-cleaned / needs-review / null / validation-fail),
//      dataset-wide (not just the current page), with inline "Replace".
//   2. Click a footer flag bubble (e.g. "30 Needs review") -> popup listing
//      every value carrying that flag across all columns, dataset-wide.
//
// Both popups reuse the existing /api/clean/tools/standardize endpoint to
// persist replacements, so "Apply" behaves exactly like the Standardize
// Values tool already does (column-wide value -> value mapping).

const _reviewState = { mode: null, column: null, flag: null, values: [], pendingEdits: {} };

// ── Clean Tools dropdown (collapses Trim/Date/Standardize/Title/Regex) ────
function toggleCleanToolsMenu(e) {
  e.stopPropagation();
  document.getElementById("cleanToolsDropdown").classList.toggle("open");
}
function closeCleanToolsMenu() {
  document.getElementById("cleanToolsDropdown")?.classList.remove("open");
}
document.addEventListener("click", (e) => {
  const dd = document.getElementById("cleanToolsDropdown");
  if (dd && dd.classList.contains("open") && !dd.contains(e.target)) closeCleanToolsMenu();
});


// ── Delegated click wiring (column headers + flag bubbles) ────────────────
document.addEventListener("click", (e) => {
  const th = e.target.closest(".clean-th-clickable");
  if (th && th.dataset.col) {
    openColumnReviewPopup(th.dataset.col);
    return;
  }
  const chip = e.target.closest(".flag-count-chip--clickable");
  if (chip && chip.dataset.flag) {
    openFlagReviewPopup(chip.dataset.flag);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeReviewPopup(); closeCleanToolsMenu(); }
});

// ── Open / close ────────────────────────────────────────────────────────
async function openColumnReviewPopup(column) {
  if (!state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  _reviewState.mode = "column";
  _reviewState.column = column;
  _reviewState.flag = null;
  _reviewState.pendingEdits = {};
  _showReviewModal(`Flagged values — ${column}`,
    "Every flagged cell in this column, across the whole dataset. Edit a value and click Replace to apply it everywhere it occurs in this column.");
  await _loadReviewValues();
}

const _FLAG_LABELS = {
  "cell-auto-cleaned": "Auto-cleaned values",
  "cell-needs-review":  "Values needing review",
  "null_value":         "Null / empty values",
  "flag-val-fail":      "Validation failures",
  "special_at":         "Values with special characters",
};

async function openFlagReviewPopup(flagKey) {
  if (!state.cleanDataType) { showToast("Load a dataset first.", "error"); return; }
  _reviewState.mode = "flag";
  _reviewState.column = null;
  _reviewState.flag = flagKey;
  _reviewState.pendingEdits = {};
  _showReviewModal(_FLAG_LABELS[flagKey] || "Flagged values",
    "Every value across all columns carrying this flag, dataset-wide. Edit a value and click Replace to apply it everywhere it occurs in that column.");
  await _loadReviewValues();
}

function closeReviewPopup() {
  const m = document.getElementById("reviewPopupModal");
  if (m) { m.style.display = "none"; document.body.style.overflow = ""; }
}

function _showReviewModal(title, desc) {
  document.getElementById("reviewPopupTitle").textContent = title;
  document.getElementById("reviewPopupDesc").textContent  = desc;
  document.getElementById("reviewPopupBody").innerHTML =
    `<div class="review-popup-loading">Loading flagged values…</div>`;
  const m = document.getElementById("reviewPopupModal");
  m.style.display = "flex";
  document.body.style.overflow = "hidden";
}

// ── Data load + render ─────────────────────────────────────────────────────
async function _loadReviewValues() {
  try {
    const res = await getFlaggedValues({
      dataType: state.cleanDataType,
      ipName:   state.cleanIpName,
      mode:     _reviewState.mode,
      column:   _reviewState.column,
      flag:     _reviewState.flag,
      limit:    500,
    });
    _reviewState.values = res.values || [];
    _renderReviewList();
  } catch (e) {
    document.getElementById("reviewPopupBody").innerHTML =
      `<div class="review-popup-empty">Couldn't load flagged values: ${escapeHtml(e.message)}</div>`;
  }
}

const _STATUS_LABEL = {
  "cleaned":          { text: "Auto-cleaned",     cls: "review-tag--green"  },
  "review":           { text: "Needs review",     cls: "review-tag--yellow" },
  "validation-fail":  { text: "Validation fail",  cls: "review-tag--red"    },
  "null":             { text: "Null",             cls: "review-tag--grey"   },
  "flagged":          { text: "Flagged",          cls: "review-tag--grey"   },
};

function _renderReviewList() {
  const body = document.getElementById("reviewPopupBody");
  const items = _reviewState.values;

  if (!items.length) {
    body.innerHTML = `<div class="review-popup-empty">No flagged values found here. Nice and clean.</div>`;
    return;
  }

  body.innerHTML = `
    <div class="review-popup-count">${items.length.toLocaleString()} flagged value${items.length !== 1 ? "s" : ""}</div>
    <div class="review-popup-list">
      ${items.map((it, i) => {
        const tag = _STATUS_LABEL[it.status] || _STATUS_LABEL.flagged;
        const colLabel = _reviewState.mode === "flag" ? `<span class="review-row-col">${escapeHtml(it.column)}</span>` : "";
        const rowsPreview = (it.rows || []).slice(0, 5).join(", ") + (it.count > 5 ? `, +${it.count - 5} more` : "");
        return `
        <div class="review-row" data-idx="${i}">
          <div class="review-row-main">
            <span class="review-tag ${tag.cls}">${tag.text}</span>
            ${colLabel}
            <span class="review-row-value" title="${escapeHtml(it.value) || "(empty)"}">${it.value ? escapeHtml(it.value) : "<em>(empty)</em>"}</span>
            <span class="review-row-count">${it.count}× · rows ${escapeHtml(rowsPreview)}</span>
          </div>
          <div class="review-row-edit">
            <input type="text" class="review-row-input" id="reviewInput${i}" placeholder="Replace with…" value="${escapeHtml(it.value)}" />
            <button class="btn-tool btn-tool--accent review-row-apply" onclick="_applyReviewEdit(${i})">Replace</button>
          </div>
        </div>`;
      }).join("")}
    </div>
  `;
}

async function _applyReviewEdit(idx) {
  const item = _reviewState.values[idx];
  if (!item) return;
  const input = document.getElementById(`reviewInput${idx}`);
  const newVal = input.value;
  if (newVal === item.value) { showToast("No change to apply.", "error"); return; }

  const column = _reviewState.mode === "column" ? _reviewState.column : item.column;
  const row = document.querySelector(`.review-row[data-idx="${idx}"]`);
  const btn = row ? row.querySelector(".review-row-apply") : null;
  if (btn) { btn.disabled = true; btn.textContent = "Applying…"; }

  try {
    const res = await _toolFetch("/standardize", {
      data_type: state.cleanDataType,
      ip_name:   state.cleanIpName || null,
      column,
      mapping:   { [item.value]: newVal },
    });
    showToast(`Replaced ${res.changes} cell${res.changes === 1 ? "" : "s"} in "${column}".`, "success");
    if (row) row.remove();
    _reviewState.values.splice(idx, 1);
    if (typeof _refreshCleanDataset === "function") await _refreshCleanDataset();
    if (!_reviewState.values.length) {
      document.getElementById("reviewPopupBody").innerHTML =
        `<div class="review-popup-empty">All flagged values here have been resolved.</div>`;
    } else {
      document.querySelector(".review-popup-count").textContent =
        `${_reviewState.values.length.toLocaleString()} flagged value${_reviewState.values.length !== 1 ? "s" : ""}`;
    }
  } catch (e) {
    showToast(e.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "Replace"; }
  }
}
