// report.js  —  Pipeline Report View
// Reads summary data stored by runPipeline() on window._lastPipelineResult
// and renders the Dashboard / Issues / Executive tabs.

// ── Tab switching ─────────────────────────────────────────────────────────────

function switchReportTab(name) {
  document.querySelectorAll('.report-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.reportTab === name));
  document.querySelectorAll('.report-panel').forEach(p =>
    p.classList.toggle('active', p.id === `reportPanel${capitalize(name)}`));
}

// ── Render report from pipeline result ───────────────────────────────────────

function renderReport(result) {
  if (!result) return;

  const s   = result.summary || {};
  const vs  = result.validation_summary || {};
  const total = s.total_rows || 0;

  // Show/hide report content
  document.getElementById('reportEmpty').style.display   = 'none';
  document.getElementById('reportContent').style.display = 'block';
  document.getElementById('reportDownloadBtn').style.display = result.download_urls?.report ? 'inline-flex' : 'none';
  window._reportDownloadUrl  = result.download_urls?.report || null;
  window._reportDownloadName = result.output_files?.report || 'report.parquet';

  // ── Quality score ─────────────────────────────────────────────────────────
  const flaggedCells  = s.cells_flagged  || 0;
  const cleanedCells  = s.cells_auto_cleaned || 0;
  const totalCellsEst = total * 20; // rough estimate
  const score = totalCellsEst > 0
    ? Math.max(0, Math.min(100, Math.round(100 - (flaggedCells / totalCellsEst) * 100)))
    : 100;
  const scoreLabel = score >= 90 ? 'Excellent' : score >= 75 ? 'Good' : score >= 60 ? 'Fair' : 'Needs Work';

  document.getElementById('rQuality').textContent      = scoreLabel;
  document.getElementById('rDashTotalRows').textContent = total.toLocaleString();
  document.getElementById('rDashCleaned').textContent   = cleanedCells.toLocaleString();
  document.getElementById('rDashFlagged').textContent   = flaggedCells.toLocaleString();

  // Score ring
  const ring = document.getElementById('rScoreRing');
  ring.style.setProperty('--score', score + '%');
  document.getElementById('rScoreRingValue').textContent = score + '%';

  // ── Cleaning summary mini-grid ────────────────────────────────────────────
  document.getElementById('rTotalRows').textContent    = total.toLocaleString();
  document.getElementById('rRowsCleaned').textContent  = (s.rows_auto_cleaned || 0).toLocaleString();
  document.getElementById('rCellsCleaned').textContent = cleanedCells.toLocaleString();
  document.getElementById('rCellsFlagged').textContent = flaggedCells.toLocaleString();

  // ── Cleaning step breakdown ───────────────────────────────────────────────
  const steps = s.step_breakdown || {};
  const stepEntries = Object.entries(steps).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const maxStep = stepEntries[0]?.[1] || 1;
  document.getElementById('rStepBreakdown').innerHTML = stepEntries.length
    ? stepEntries.map(([name, count]) => `
        <div class="dist-row">
          <span>${escapeHtml(name)}</span>
          <div class="dist-bar"><i style="width:${Math.round(count/maxStep*100)}%"></i></div>
          <strong>${count.toLocaleString()}</strong>
        </div>`).join('')
    : '<p style="color:var(--text-4);font-size:12px">No steps recorded.</p>';

  // ── Validation summary ────────────────────────────────────────────────────
  const valTotal  = vs.total_rows || total || 0;
  const valPassed = vs.passed     || 0;
  const valFailed = vs.failed     || 0;
  document.getElementById('rValTotal').textContent  = valTotal.toLocaleString();
  document.getElementById('rValPassed').textContent = valPassed.toLocaleString();
  document.getElementById('rValFailed').textContent = valFailed.toLocaleString();

  // ── Review by column (Issues tab) ─────────────────────────────────────────
  const revCols  = s.review_by_column || {};
  const revEntries = Object.entries(revCols).sort((a, b) => b[1] - a[1]);
  const totalFlaggedCols = revEntries.reduce((sum, [,v]) => sum + v, 0) || 1;
  const maxRev = revEntries[0]?.[1] || 1;

  document.getElementById('rReviewByCol').innerHTML = revEntries.length
    ? revEntries.slice(0, 8).map(([col, count]) => `
        <div class="quality-row">
          <span title="${escapeHtml(col)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(col)}</span>
          <div class="quality-bar"><i style="width:${Math.round(count/maxRev*100)}%;background:var(--yellow)"></i></div>
          <strong>${count}</strong>
        </div>`).join('')
    : '<p style="color:var(--text-4);font-size:12px">No columns flagged.</p>';

  // ── Pipeline status indicators ─────────────────────────────────────────────
  const dupes = s.duplicate_uuid_rows || 0;
  const statuses = [
    { ok: cleanedCells > 0,   label: `Auto-cleaned ${cleanedCells.toLocaleString()} cells` },
    { ok: flaggedCells === 0, label: flaggedCells === 0 ? 'No cells flagged for review' : `${flaggedCells.toLocaleString()} cells need review` },
    { ok: dupes === 0,        label: dupes === 0 ? 'No duplicate UUID rows' : `${dupes} duplicate UUID rows detected` },
    { ok: valFailed === 0,    label: valFailed === 0 ? 'All rows passed validation' : `${valFailed.toLocaleString()} rows failed validation` },
  ];
  document.getElementById('rStatusList').innerHTML = statuses.map(st => `
    <div class="status-row ${st.ok ? 'ok' : 'warn'}">
      <strong>${st.ok ? '✓' : '!'}</strong>
      ${escapeHtml(st.label)}
    </div>`).join('');

  // ── Issues tab ────────────────────────────────────────────────────────────
  document.getElementById('rIssuesNeedReview').textContent = (s.rows_need_review || 0).toLocaleString();
  document.getElementById('rIssuesDupes').textContent      = dupes.toLocaleString();
  document.getElementById('rIssuesCells').textContent      = flaggedCells.toLocaleString();
  document.getElementById('rIssuesCaption').textContent    = `${revEntries.length} column${revEntries.length !== 1 ? 's' : ''} flagged`;

  document.getElementById('rIssuesTableBody').innerHTML = revEntries.length
    ? revEntries.map(([col, count]) => {
        const pct = Math.round(count / totalFlaggedCols * 100);
        return `<tr>
          <td class="mono">${escapeHtml(col)}</td>
          <td style="font-family:var(--font-mono)">${count.toLocaleString()}</td>
          <td>
            <div style="display:flex;align-items:center;gap:8px">
              <div class="dist-bar" style="width:80px"><i style="width:${pct}%"></i></div>
              <span style="font-size:11px;color:var(--text-3);font-family:var(--font-mono)">${pct}%</span>
            </div>
          </td>
        </tr>`;
      }).join('')
    : `<tr><td colspan="3" class="issues-empty-cell">No columns flagged for review.</td></tr>`;


  // ── Validation rules section (Issues tab) ──────────────────────────────────
  const filterResults = vs.filter_results || [];
  const valRulesCard  = document.getElementById("rValRulesCard");
  const valRulesBody  = document.getElementById("rValRulesBody");
  const valRulesCap   = document.getElementById("rValRulesCaption");

  if (filterResults.length > 0 && valRulesCard && valRulesBody) {
    valRulesCard.style.display = "block";
    valRulesCap.textContent = `${filterResults.length} rule${filterResults.length !== 1 ? "s" : ""} configured`;
    valRulesBody.innerHTML = filterResults.map(fr => {
      const flagged  = fr.flagged_count || 0;
      const total_fr = vs.total_rows || total || 1;
      const passed   = total_fr - flagged;
      const pct      = Math.round(flagged / total_fr * 100);
      const isOk     = flagged === 0;
      return `<tr>
        <td style="font-weight:600">${escapeHtml(fr.label || "—")}</td>
        <td><code style="font-size:11px;background:var(--bg);padding:1px 6px;border-radius:3px">${escapeHtml(fr.cond || "—")}</code></td>
        <td>
          <span class="${isOk ? "rval-pass" : "rval-fail"}">${flagged.toLocaleString()}</span>
          <span style="color:var(--text-4);font-size:11px"> / ${total_fr.toLocaleString()} rows</span>
          ${pct > 0 ? `<div style="margin-top:3px;height:3px;background:var(--border);border-radius:2px;width:100px;display:inline-block;vertical-align:middle;margin-left:8px"><div style="height:100%;width:${pct}%;background:var(--red);border-radius:2px"></div></div>` : ""}
        </td>
        <td>
          <span class="status-row ${isOk ? "ok" : "warn"}" style="display:inline-flex;padding:2px 8px;border-radius:20px;font-size:11px">
            ${isOk ? "✓ All Pass" : `✗ ${flagged} Fail`}
          </span>
        </td>
      </tr>`;
    }).join("");
  } else if (valRulesCard) {
    valRulesCard.style.display = "none";
  }


  // ── Executive tab ─────────────────────────────────────────────────────────
  const pctClean   = total > 0 ? Math.round((s.rows_auto_cleaned || 0) / total * 100) : 0;
  const valPassPct = valTotal > 0 ? Math.round(valPassed / valTotal * 100) : 100;
  document.getElementById('rExecutiveText').textContent =
    `This pipeline run processed ${total.toLocaleString()} rows and automatically cleaned ` +
    `${cleanedCells.toLocaleString()} cells across ${(s.rows_auto_cleaned||0).toLocaleString()} rows ` +
    `(${pctClean}% of total). ` +
    (flaggedCells > 0
      ? `${flaggedCells.toLocaleString()} cells across ${(s.rows_need_review||0).toLocaleString()} rows require manual review by a data steward. `
      : `No cells were flagged for manual review. `) +
    (dupes > 0 ? `${dupes} duplicate UUID rows were detected. ` : '') +
    `Validation filters passed ${valPassed.toLocaleString()} of ${valTotal.toLocaleString()} rows (${valPassPct}%). ` +
    `Overall data quality score: ${score}% (${scoreLabel}).`;

  document.getElementById('rManualCount').textContent = (s.rows_need_review || 0).toLocaleString();

  const ready = score >= 90 && flaggedCells === 0 && valFailed === 0;
  document.getElementById('rReadiness').textContent    = ready ? '✓ Ready' : 'Review Required';
  document.getElementById('rReadinessNote').textContent = ready
    ? 'This dataset is clean and ready for export.'
    : `${flaggedCells.toLocaleString()} cells and ${valFailed.toLocaleString()} validation failures need attention before export.`;

  // Initialise per-record view
  initReportRecords(result);

  // Switch to dashboard tab
  switchReportTab('dashboard');
}

// ── Download from report view ─────────────────────────────────────────────────

function downloadReportCleanedFile() {
  if (window._reportDownloadUrl) {
    window.open(window._reportDownloadUrl, '_blank');
  } else {
    showToast('No download available. Run the pipeline first.', 'error');
  }
}

// ── Hook into runPipeline result ──────────────────────────────────────────────
// Monkey-patch: after runPipeline succeeds and the result is stored, render the report.
// We hook on window._lastPipelineResult being set by main.js.

(function patchRunPipeline() {
  const _orig = window.renderReport || function(){};
  window.renderReport = renderReport;

  // Also watch for the report nav link to render the latest result
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-item').forEach(item => {
      if (item.dataset.view === 'report') {
        item.addEventListener('click', () => {
          if (window._lastPipelineResult) {
            renderReport(window._lastPipelineResult);
          }
        });
      }
    });
  });
})();

// ══════════════════════════════════════════════════════════
// RECORDS TAB
// Per-row audit: shows cleaned values, review flags, and
// per-filter pass/fail with actual vs expected values.
// ══════════════════════════════════════════════════════════

let _rState = {
  cursor:     null,
  prevCursors:[],   // stack for Prev navigation
  page:       1,
  hasMore:    false,
  dataType:   null,
  ipName:     null,
  stem:       null,
  filters:    [],   // from validation_summary.filter_results
  search:     "",
  rows:       [],   // current page rows (full, unfiltered)
};

// Called from renderReport() with the pipeline result
function initReportRecords(result) {
  const meta   = result.output_files || {};
  const vs     = result.validation_summary || {};
  const dtype  = result.data_type;
  const ip     = result.ip_name || null;

  // Derive stem from report filename e.g. "trdp_report.parquet" → "trdp"
  const rFile  = meta.report || "";
  const stem   = rFile.replace(/_report\.parquet$/, "");

  _rState.cursor             = null;
  _rState.currentStartCursor = null;
  _rState.prevCursors        = [];
  _rState.page               = 1;
  _rState.dataType    = dtype;
  _rState.ipName      = ip;
  _rState.stem        = stem;
  _rState.filters     = vs.filter_results || [];
  _rState.rows        = [];
  _rState.hasMore     = false;

  // Render filter legend
  _renderRecordsFilterLegend();

  // Load first page
  if (stem && dtype) {
    _loadReportPage();
  } else {
    document.getElementById("rRecordsEmpty").style.display = "block";
    document.getElementById("rRecordsList").innerHTML      = "";
  }
}

function _renderRecordsFilterLegend() {
  const legend = document.getElementById("rRecordsFilterLegend");
  const list   = document.getElementById("rRecordsFilterList");
  if (!legend || !list) return;
  const filters = _rState.filters;
  if (!filters.length) { legend.style.display = "none"; return; }
  legend.style.display = "block";
  list.innerHTML = filters.map((f, i) => {
    const ok = f.flagged_count === 0;
    return `<span style="padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;
      background:${ok?"rgba(16,185,129,0.12)":"rgba(239,68,68,0.12)"};
      color:${ok?"var(--green)":"var(--red)"};
      border:1px solid ${ok?"rgba(16,185,129,0.3)":"rgba(239,68,68,0.3)"}">
      ${ok?"✓":"✗"} ${escapeHtml(f.label)}
      <span style="opacity:.7;font-weight:400">(${f.flagged_count} flagged)</span>
    </span>`;
  }).join("");
}

async function _loadReportPage(cursor=null) {
  document.getElementById("rRecordsEmpty").style.display  = "none";
  document.getElementById("rRecordsList").innerHTML       =
    `<div style="text-align:center;padding:32px;color:var(--text-4);font-size:13px">Loading records…</div>`;

  try {
    const data = await fetchReportPage(_rState.dataType, _rState.ipName, _rState.stem, cursor, 20);
    _rState.rows    = data.rows || [];
    _rState.hasMore = data.pagination?.has_more || false;
    _rState.cursor  = data.pagination?.next_cursor || null;

    const count = document.getElementById("rRecordsCount");
    if (count) count.textContent =
      `${data.pagination?.total_rows?.toLocaleString() || "?"} total records · Page ${_rState.page}`;

    _renderRecordsPage();
    _updateRecordsPager();
  } catch (err) {
    document.getElementById("rRecordsList").innerHTML =
      `<div style="color:var(--red);font-size:13px;padding:20px">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function _renderRecordsPage() {
  const q    = _rState.search.toLowerCase();
  const rows = q
    ? _rState.rows.filter(r => String(r.uuid || "").toLowerCase().includes(q))
    : _rState.rows;

  if (!rows.length) {
    document.getElementById("rRecordsList").innerHTML =
      `<div style="text-align:center;padding:32px;color:var(--text-4)">No records${q?" match your search":""} on this page.</div>`;
    return;
  }

  // Build a lookup: row_uuid → set of filter indices that flagged it
  // filter_results no longer carries a row-index list (removed — redundant
  // with UUIDs, which already uniquely identify each row). Failures are
  // read straight off the per-row validation_status field instead.

  const pageOffset = (_rState.page - 1) * 20; // 20 per page
  document.getElementById("rRecordsList").innerHTML = rows.map((r, rowIdx) =>
    _buildRecordCard(r, pageOffset + rowIdx + 2)
  ).join("");
}

function _buildRecordCard(r, rowNum) {
  const uuid     = r.uuid || `Row ${rowNum}`;
  const cleaned  = r.cleaned_values  || {};  // { col: [new_val, step] }
  const reviews  = r.manual_reviews  || {};  // { col: orig_val }
  const valStatus= String(r.validation_status || "PASS");
  const isDup    = r.is_dup     || false;
  const isDupCnic= r.is_dup_cnic|| false;

  // Parse validation failures: "FAIL: Label1 | Label2"
  const failLabels = valStatus.startsWith("FAIL:")
    ? valStatus.replace("FAIL:", "").split("|").map(s => s.trim()).filter(Boolean)
    : [];
  const hasFail   = failLabels.length > 0;
  const hasReview = Object.values(reviews).some(v => v !== null && v !== undefined);
  const hasCleaned= Object.keys(cleaned).length > 0;

  const cardClass = hasFail ? "has-fail" : hasReview ? "has-review" : "all-pass";
  const statusLabel = hasFail ? "FAIL" : hasReview ? "Review" : "PASS";
  const statusClass = hasFail ? "fail" : hasReview ? "review" : "pass";

  // ── Cleaned columns section ──────────────────────────────────────────────
  let cleanedHtml = "";
  const cleanedEntries = Object.entries(cleaned);
  if (cleanedEntries.length) {
    cleanedHtml = `
      <div class="record-section">
        <div class="record-section-title">✓ Auto-Cleaned (${cleanedEntries.length} column${cleanedEntries.length!==1?"s":""})</div>
        <div class="record-col-grid">
          ${cleanedEntries.map(([col, val]) => {
            const newVal  = Array.isArray(val) ? val[0] : val;
            const step    = Array.isArray(val) ? val[1] : "";
            const origVal = (r.original_values || {})[col];
            return `<div class="record-col-item cleaned">
              <div class="record-col-name">${escapeHtml(col)}</div>
              <div class="record-col-val">${escapeHtml(String(newVal ?? "—"))}</div>
              ${origVal != null ? `<div class="record-col-old">was: ${escapeHtml(String(origVal))}</div>` : ""}
              ${step ? `<div class="record-col-step">${escapeHtml(step)}</div>` : ""}
            </div>`;
          }).join("")}
        </div>
      </div>`;
  }

  // ── Review columns section ───────────────────────────────────────────────
  let reviewHtml = "";
  const reviewEntries = Object.entries(reviews).filter(([,v]) => v !== null);
  if (reviewEntries.length) {
    reviewHtml = `
      <div class="record-section">
        <div class="record-section-title">⚠ Needs Manual Review (${reviewEntries.length} column${reviewEntries.length!==1?"s":""})</div>
        <div class="record-col-grid">
          ${reviewEntries.map(([col, val]) => `
            <div class="record-col-item review">
              <div class="record-col-name">${escapeHtml(col)}</div>
              <div class="record-col-val">${escapeHtml(String(val ?? "—"))}</div>
            </div>`).join("")}
        </div>
      </div>`;
  }

  // ── Validation section — use per-row filter details from validation_status JSON
  let validationHtml = "";
  // r.validation_status is a JSON string: {"result":"PASS","filters":[{label,cond,col,expected,actual,pass},...]}
  let filterDetails = [];
  try {
    const vs = typeof r.validation_status === "string" && r.validation_status.startsWith("{")
      ? JSON.parse(r.validation_status)
      : null;
    if (vs) filterDetails = vs.filters || [];
  } catch(e) {}

  if (filterDetails.length) {
    const filterRows = filterDetails.map(fd => {
      const isPassing  = fd.pass !== false;
      const passClass  = isPassing ? "pass" : "fail";
      const passIcon   = isPassing ? "✓" : "✗";
      const borderClr  = isPassing ? "rgba(16,185,129,0.3)" : "rgba(239,68,68,0.3)";

      // Build expected vs actual display
      let compareHtml = "";
      if (fd.col && fd.actual !== null && fd.actual !== undefined) {
        compareHtml = `<div style="margin-top:5px;font-size:11px;display:grid;grid-template-columns:auto 1fr;gap:2px 8px">
          <span style="color:var(--text-4)">column:</span>
          <span style="font-family:var(--font-mono);color:var(--text-2)">${escapeHtml(String(fd.col))}</span>
          <span style="color:var(--text-4)">actual:</span>
          <span style="font-weight:600;color:var(--text-1)">${escapeHtml(String(fd.actual))}</span>
          ${fd.expected != null && fd.expected !== "" ? `
          <span style="color:var(--text-4)">expected:</span>
          <span style="color:var(--text-3)">${escapeHtml(String(fd.expected))}</span>` : ""}
        </div>`;
      }

      return `<div class="record-col-item" style="border-color:${borderClr};background:${isPassing?"rgba(16,185,129,0.05)":"rgba(239,68,68,0.05)"}">
        <div class="record-col-name">${escapeHtml(fd.label || fd.cond || "Filter")}</div>
        ${compareHtml}
        <div class="record-val-check ${passClass}" style="margin-top:6px">
          <strong>${passIcon} ${isPassing ? "PASS" : "FAIL"}</strong>
        </div>
      </div>`;
    });

    validationHtml = `
      <div class="record-section">
        <div class="record-section-title">Validation Checks (${filterDetails.length})</div>
        <div class="record-col-grid">${filterRows.join("")}</div>
      </div>`;
  } else if (_rState.filters.length) {
    // Fallback: no per-row details — show filter legend only
    validationHtml = `
      <div class="record-section">
        <div class="record-section-title">Validation: ${escapeHtml(String(r.validation_status || "PASS"))}</div>
      </div>`;
  }

  // ── Duplicate flags ──────────────────────────────────────────────────────
  let dupHtml = "";
  if (isDup || isDupCnic) {
    dupHtml = `<div class="record-section">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${isDup    ? `<span class="val-rule-chip">Duplicate UUID</span>` : ""}
        ${isDupCnic? `<span class="val-rule-chip">Duplicate CNIC</span>` : ""}
      </div>
    </div>`;
  }

  return `<div class="record-card ${cardClass}">
    <div class="record-head">
      <span class="record-uuid">${escapeHtml(String(uuid))}</span>
      <span class="record-status-badge ${statusClass}">${statusLabel}</span>
      ${hasCleaned ? `<span style="font-size:11px;color:var(--green)">✓ ${Object.keys(cleaned).length} cleaned</span>` : ""}
      ${hasReview  ? `<span style="font-size:11px;color:var(--yellow,#f59e0b)">⚠ ${reviewEntries.length} review</span>` : ""}
      ${isDup || isDupCnic ? `<span style="font-size:11px;color:var(--red)">⚠ Duplicate</span>` : ""}
    </div>
    ${dupHtml}
    ${validationHtml}
    ${cleanedHtml}
    ${reviewHtml}
  </div>`;
}

function _updateRecordsPager() {
  const pager = document.getElementById("rRecordsPager");
  const prev  = document.getElementById("rRecordsPrev");
  const next  = document.getElementById("rRecordsNext");
  const info  = document.getElementById("rRecordsPageInfo");
  if (!pager) return;
  pager.style.display = "flex";
  if (prev) prev.disabled = _rState.page <= 1;
  if (next) next.disabled = !_rState.hasMore;
  if (info) info.textContent = `Page ${_rState.page}`;
}

function reportRecordsPage(dir) {
  if (dir === 1 && _rState.hasMore) {
    // Save the cursor that leads to the NEXT page (current page's next_cursor)
    // so we can go back to current page later by re-loading from its start cursor
    _rState.prevCursors.push(_rState.currentStartCursor || null);
    _rState.page++;
    _rState.currentStartCursor = _rState.cursor;
    _loadReportPage(_rState.cursor);
  } else if (dir === -1 && _rState.page > 1) {
    _rState.page--;
    const prevCursor = _rState.prevCursors.pop() || null;
    _rState.currentStartCursor = prevCursor;
    _loadReportPage(prevCursor);
  }
}

function filterReportRecords(q) {
  _rState.search = q;
  _renderRecordsPage();
}

