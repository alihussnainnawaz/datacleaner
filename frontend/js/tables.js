// tables.js

let allColumns = [];
let allColumns2 = [];

try {
    allColumns = Array.isArray(window.allColumns) ? window.allColumns : (typeof window.allColumns === "string" ? JSON.parse(window.allColumns) : []);
} catch(e) { allColumns = []; }
try {
    allColumns2 = Array.isArray(window.allColumns2) ? window.allColumns2 : (typeof window.allColumns2 === "string" ? JSON.parse(window.allColumns2) : []);
} catch(e) { allColumns2 = []; }

function syncColumns() {
    try {
        allColumns  = Array.isArray(window.allColumns)  ? window.allColumns  : (typeof window.allColumns  === "string" ? JSON.parse(window.allColumns)  : []);
        allColumns2 = Array.isArray(window.allColumns2) ? window.allColumns2 : (typeof window.allColumns2 === "string" ? JSON.parse(window.allColumns2) : []);
    } catch(e) {}
}

/* =============================
   GENERATE TABLES
============================= */
let totalTables = 0;

window.generateTables = async function(){
    // Wait for the initial dataset load to finish before syncing columns
    // so column names are never replaced by "Column 1, 2, ..." fallbacks
    if (window._datasetInfoReady) await window._datasetInfoReady;
    syncColumns();
    const container = document.getElementById("tablesContainer");
    const selectAll = document.getElementById("selectAllCols");

    if(!Array.isArray(allColumns) || allColumns.length === 0){
        alert("Please upload Dataset 1 first");
        return;
    }

    if(selectAll.checked){
        container.innerHTML = "";
        totalTables = 0;
        for(let i = 0; i < allColumns.length; i++){
            totalTables++;
            addSingleFilter(container, totalTables);
        }
    } else {
        totalTables++;
        addSingleFilter(container, totalTables);
    }
};

function addSingleFilter(container, i){
    const div = document.createElement("div");
    div.innerHTML = `
        <div class="rule-card" id="tableBox_${i}">
            <button class="rule-remove" onclick="removeTable(${i})">✕</button>
            <div class="rule-grid">
                <div class="rule-field">
                    <select id="condSelect_${i}" onchange="handleConditionUI(${i})">
                        <option value="" disabled selected>Condition</option>
                        <option value="empty">Missing / Null</option>
                        <option value="dup">Duplicate</option>
                        <option value="eq">Equal to</option>
                        <option value="neq">Not Equal to</option>
                        <option value="gt">Greater Than</option>
                        <option value="lt">Less Than</option>
                        <option value="gte">Greater than or Equal to</option>
                        <option value="lte">Less than or Equal to</option>
                        <option value="btwn">Between</option>
                        <option value="nbtwn">Not Between</option>
                        <option value="cross">Cross Check</option>
                        <option value="doublecross">Double Cross Check</option>
                        <option value="and">AND</option>
                        <option value="or">OR</option>
                        <option value="date">Date</option>
                        <option value="compare">Compare</option>
                        <option value="compare2">Compare with Dataset 2</option>
                        <option value="coords">Coordinate Check</option>
                        <option value="bad_pattern">Bad Pattern (placeholder values)</option>
                    </select>
                </div>
            </div>
        </div>
    `;
    container.appendChild(div.firstElementChild);
}

/* =============================
   BAD PATTERN — multi-value chip input
   Lets the user add one or more literal/regex values to flag
   (e.g. "11111", "0000", "111111111") for the bad_pattern filter.
============================= */
window._badPatternValues = window._badPatternValues || {};

function _addBadPatternValue(id) {
    const input = document.getElementById(`badPatternInput_${id}`);
    if (!input) return;
    const v = input.value.trim();
    if (!v) return;
    window._badPatternValues[id] = window._badPatternValues[id] || [];
    if (!window._badPatternValues[id].includes(v)) {
        window._badPatternValues[id].push(v);
        _renderBadPatternChips(id);
    }
    input.value = "";
    input.focus();
}

function _removeBadPatternValue(id, value) {
    const list = window._badPatternValues[id] || [];
    window._badPatternValues[id] = list.filter(v => v !== value);
    _renderBadPatternChips(id);
}

function _renderBadPatternChips(id) {
    const container = document.getElementById(`badPatternChips_${id}`);
    const hidden     = document.getElementById(`matchInput_${id}`);
    const values     = window._badPatternValues[id] || [];
    const esc = s => (s + "").replace(/[<>&"]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;"}[c]));
    if (container) {
        container.innerHTML = values.map(v => `
            <span class="bad-pattern-chip" data-filter-id="${id}" data-value="${esc(v)}">
                ${esc(v)}
                <button type="button" class="bad-pattern-chip-remove" title="Remove">✕</button>
            </span>`).join("");
    }
    // Keep the hidden matchInput in sync as a comma-separated string — this is
    // what loadGeneratedFilters() reads via matchInput_${id}, same as every
    // other single-value filter, so no extra plumbing is needed downstream.
    if (hidden) hidden.value = values.join(",");
}

// Delegated click handler for chip-remove buttons (avoids unsafe inline
// onclick attributes built from arbitrary user-typed pattern values).
document.addEventListener("click", (e) => {
    const btn = e.target.closest(".bad-pattern-chip-remove");
    if (!btn) return;
    const chip = btn.closest(".bad-pattern-chip");
    if (!chip) return;
    const id    = parseInt(chip.dataset.filterId);
    const value = chip.dataset.value;
    _removeBadPatternValue(id, value);
});

/* =============================
   REMOVE RULE
============================= */
function removeTable(id){
    const box = document.getElementById(`tableBox_${id}`);
    if(box) box.remove();
    // NOTE: totalTables is a monotonic id generator — never reuse ids, even after
    // removals, otherwise a freshly created rule could collide with an existing
    // tableBox_<n>. collectFilters() iterates the actual rule-card ids, so gaps
    // in the sequence are fine.
}

/* =============================
   SEARCHABLE COLUMN COMBO
============================= */
/**
 * Creates a searchable column-picker combo that mimics a <select> but adds
 * a wildcard text filter on top of the list.
 *
 * Returns a wrapper <div> that contains:
 *   - a hidden <select id="{fieldId}"> (used by all existing logic)
 *   - a visible <input> search bar
 *   - a <ul> dropdown list
 *
 * After the user picks an item the hidden select's value is updated and a
 * native "change" event is dispatched so all existing onchange handlers fire
 * as before.
 */
function colComboSearch(fieldId, label, columns) {
    const wrap = document.createElement("div");
    wrap.className = "rule-field dynamic-field col-combo-wrap";
    wrap.setAttribute("data-combo-id", fieldId);

    // Hidden select kept for compatibility with all existing code
    const hiddenSel = document.createElement("select");
    hiddenSel.id = fieldId;
    hiddenSel.style.display = "none";
    hiddenSel.innerHTML = `<option value="" disabled selected>${label}</option>`;
    columns.forEach((c, idx) => hiddenSel.add(new Option(c.col1 || `Column ${idx + 1}`, idx)));
    wrap.appendChild(hiddenSel);

    // Visible search input
    const input = document.createElement("input");
    input.type = "text";
    input.className = "col-combo-input";
    input.placeholder = label;
    input.autocomplete = "off";
    wrap.appendChild(input);

    // Attach list to body so no ancestor overflow clips it
    const list = document.createElement("ul");
    list.className = "col-combo-list";
    list.style.display = "none";
    document.body.appendChild(list);

    function renderList(query) {
        const q = (query || "").toLowerCase().trim();
        list.innerHTML = "";

        // position: fixed uses viewport coords — do NOT add scrollY
        const rect = input.getBoundingClientRect();
        list.style.top   = (rect.bottom + 3) + "px";
        list.style.left  = rect.left + "px";
        list.style.width = rect.width + "px";

        const filtered = columns
            .map((c, idx) => ({ name: c.col1 || `Column ${idx + 1}`, idx }))
            .filter(item => !q || item.name.toLowerCase().includes(q));

        if (filtered.length === 0) {
            const li = document.createElement("li");
            li.className = "col-combo-empty";
            li.textContent = "No columns found";
            list.appendChild(li);
        } else {
            filtered.forEach(item => {
                const li = document.createElement("li");
                // Highlight matching portion
                if (q) {
                    const start = item.name.toLowerCase().indexOf(q);
                    if (start !== -1) {
                        li.innerHTML =
                            escapeHtml(item.name.slice(0, start)) +
                            `<mark>${escapeHtml(item.name.slice(start, start + q.length))}</mark>` +
                            escapeHtml(item.name.slice(start + q.length));
                    } else {
                        li.textContent = item.name;
                    }
                } else {
                    li.textContent = item.name;
                }
                // Mark selected
                if (String(item.idx) === hiddenSel.value) li.classList.add("selected");
                li.addEventListener("mousedown", e => {
                    e.preventDefault();
                    selectItem(item);
                });
                list.appendChild(li);
            });
        }
        list.style.display = "block";
    }

    function selectItem(item) {
        hiddenSel.value = item.idx;
        input.value = item.name;
        input.classList.add("has-value");
        list.style.display = "none";
        // Fire change on the hidden select so all existing onchange handlers work
        hiddenSel.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function escapeHtml(str) {
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    input.addEventListener("focus", () => renderList(input.value));
    input.addEventListener("input", () => renderList(input.value));
    input.addEventListener("blur", () => {
        setTimeout(() => { list.style.display = "none"; }, 150);
    });

    // Reposition on scroll or resize so the fixed dropdown tracks the input
    function reposition() {
        if (list.style.display !== "none") {
            const r = input.getBoundingClientRect();
            list.style.top   = (r.bottom + 3) + "px";
            list.style.left  = r.left + "px";
            list.style.width = r.width + "px";
        }
    }
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);

    // If the hidden select's value changes programmatically (e.g. restoreFilters),
    // sync the visible input text accordingly.
    const observer = new MutationObserver(() => {});
    hiddenSel.addEventListener("change", () => {
        const opt = hiddenSel.options[hiddenSel.selectedIndex];
        if (opt && hiddenSel.value !== "") {
            input.value = opt.text;
            input.classList.add("has-value");
        }
    });

    return wrap;
}

/* =============================
   CONDITION UI CONTROL
============================= */
function handleConditionUI(id) {
    const cond = document.getElementById(`condSelect_${id}`).value;
    const grid = document.querySelector(`#tableBox_${id} .rule-grid`);
    grid.querySelectorAll(".dynamic-field").forEach(el => el.remove());
    const oldSub = document.getElementById(`subContainer_${id}`);
    if (oldSub) oldSub.remove();
    const hasDataset2 = Array.isArray(window.allColumns2) && window.allColumns2.length > 0;

    function ds1Dropdown(fieldId, label) {
        if (allColumns.length === 0) {
            const div = document.createElement("div");
            div.className = "rule-field dynamic-field";
            const sel = document.createElement("select");
            sel.id = fieldId;
            sel.add(new Option("Upload Dataset First", ""));
            div.appendChild(sel);
            return div;
        }
        return colComboSearch(fieldId, label, allColumns);
    }

    function ds2Dropdown(fieldId, label) {
        return colComboSearch(fieldId, label, allColumns2);
    }

    // Shown in place of a Dataset 2 column dropdown until Dataset 2 has been
    // uploaded. Not a second upload entry point — Dataset 2 is uploaded
    // exactly once, from the "+ Dataset 2" slot in the file card on the
    // main dataset page (next to the "Ready" badge). This is a passive
    // pointer to that control, not a duplicate of it.
    function ds2LoadPill() {
        const div = document.createElement("div");
        div.className = "rule-field dynamic-field";
        div.innerHTML =
            `<div class="ds2-hint" title="Upload it once from the Dataset 2 slot in the file card on the main dataset page — every Cross Check / Double Cross Check filter reuses it automatically.">` +
            `Dataset 2 not loaded — upload it from the file card above</div>`;
        return div;
    }

    function uniqueAndMatch(uid, mid, colSelectId) {
        const uDiv = document.createElement("div");
        uDiv.className = "rule-field dynamic-field";
        uDiv.innerHTML = `<select id="${uid}"><option value="" disabled selected>Loading…</option></select>`;

        const mDiv = document.createElement("div");
        mDiv.className = "rule-field dynamic-field";
        mDiv.innerHTML = `<input type="text" id="${mid}" placeholder="Value to Match">`;

        async function _loadUniques(colIdx) {
            const u = document.getElementById(uid);
            if (!u) return;
            u.innerHTML = `<option value="" disabled selected>Loading…</option>`;
            try {
                const cols = window.allColumns || [];
                const colName = cols[parseInt(colIdx)]?.col1 || "";
                let values = [];

                // Try uploaded file first (pre-pipeline)
                const fileId = window._state?.fileId || (typeof state !== "undefined" && state.fileId);
                if (fileId && colName) {
                    const res = await fetch(`/api/upload/${fileId}/unique/${encodeURIComponent(colName)}`);
                    if (res.ok) { const d = await res.json(); values = d.values || []; }
                }

                // Fallback: try cleaned parquet (post-pipeline)
                if (!values.length && colName) {
                    const dt = (typeof state !== "undefined") && (state.cleanDataType || state.dataType);
                    const ip = (typeof state !== "undefined") && (state.cleanIpName || state.ipName || null);
                    if (dt) {
                        const res = await fetch("/api/clean/tools/unique", {
                            method: "POST", headers: {"Content-Type":"application/json"},
                            body: JSON.stringify({data_type: dt, ip_name: ip, column: colName})
                        });
                        if (res.ok) { const d = await res.json(); values = d.values || []; }
                    }
                }

                u.innerHTML = values.length
                    ? `<option value="" disabled selected>Unique Values (${values.length})</option>` +
                      values.map(v => `<option value="${v.replace(/"/g,"&quot;")}">${v}</option>`).join("")
                    : `<option value="" disabled selected>No values found</option>`;
            } catch(e) {
                u.innerHTML = `<option value="" disabled selected>Could not load values</option>`;
            }
        }

        setTimeout(() => {
            const colSel = document.getElementById(colSelectId);
            if (!colSel) return;
            // Load immediately if column already selected
            if (colSel.value !== "") _loadUniques(colSel.value);
            colSel.onchange = () => {
                if (colSel.value !== "") _loadUniques(colSel.value);
            };
        }, 0);
        setTimeout(() => {
            const u = document.getElementById(uid);
            if (u) u.addEventListener("change", () => {
                const m = document.getElementById(mid);
                if (m) m.value = u.value;
            });
        }, 0);
        return [uDiv, mDiv];
    }

    // MISSING / DUPLICATE
    if (cond === "empty" || cond === "dup") {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Dataset 1 Column"));
        return;
    }

    // NUMERIC / VALUE CONDITIONS
    if (["eq","neq","gt","lt","gte","lte","btwn","nbtwn"].includes(cond)) {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Dataset 1 Column"));
        const [uDiv, mDiv] = uniqueAndMatch(`uniqueSelect_${id}`, `matchInput_${id}`, `colSelect_${id}`);
        grid.appendChild(uDiv);
        grid.appendChild(mDiv);
        if (cond === "btwn" || cond === "nbtwn") {
            document.getElementById(`matchInput_${id}`).placeholder = "min,max e.g. 10,50";
        }
        return;
    }

    // CROSS CHECK
    if (cond === "cross") {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Dataset 1 Column"));
        if (hasDataset2) {
            grid.appendChild(ds2Dropdown(`crossCol_${id}`, "Dataset 2 Column"));
        } else {
            grid.appendChild(ds2LoadPill());
        }
        return;
    }

    // DOUBLE CROSS
    if (cond === "doublecross") {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Dataset 1 Column 1"));
        if (hasDataset2) {
            grid.appendChild(ds2Dropdown(`d2col1_${id}`, "Dataset 2 Column 1"));
            grid.appendChild(ds1Dropdown(`col2_${id}`, "Dataset 1 Column 2"));
            grid.appendChild(ds2Dropdown(`d2col2_${id}`, "Dataset 2 Column 2"));
        } else {
            grid.appendChild(ds1Dropdown(`col2_${id}`, "Dataset 1 Column 2"));
            grid.appendChild(ds2LoadPill());
        }
        return;
    }

    // DATE FILTER
    if (cond === "date") {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Date Column"));

        const condDiv = document.createElement("div");
        condDiv.className = "rule-field dynamic-field";
        condDiv.innerHTML = `
            <select id="dateCond_${id}" onchange="handleDateCondUI(${id})">
                <option value="" disabled selected>Date Condition</option>
                <option value="date_eq">Equal To (exact date)</option>
                <option value="date_neq">Not Equal To</option>
                <option value="date_before">Before</option>
                <option value="date_after">After</option>
                <option value="date_before_eq">Before or Equal To</option>
                <option value="date_after_eq">After or Equal To</option>
                <option value="date_btwn">Between (date range)</option>
                <option value="date_nbtwn">Not Between (date range)</option>
                <option value="date_empty">Missing / Null Date</option>
                <option value="date_invalid">Invalid Date Format</option>
                <option value="date_year_eq">Year Equals</option>
                <option value="date_year_gt">Year Greater Than</option>
                <option value="date_year_lt">Year Less Than</option>
                <option value="date_month_eq">Month Equals (1-12)</option>
                <option value="date_day_eq">Day Equals (1-31)</option>
                <option value="date_weekday">Weekday (Mon=1 … Sun=7)</option>
                <option value="date_future">In the Future</option>
                <option value="date_past">In the Past</option>
            </select>
        `;
        grid.appendChild(condDiv);

        // Placeholder for dynamic date input(s) — filled by handleDateCondUI
        const inputDiv = document.createElement("div");
        inputDiv.id = `dateInputWrap_${id}`;
        inputDiv.className = "dynamic-field";
        grid.appendChild(inputDiv);

        // Wire the column picker so it fires handleDateCondUI after the cond is picked
        setTimeout(() => {
            const colSel = document.getElementById(`colSelect_${id}`);
            if (colSel) colSel.addEventListener("change", () => handleDateCondUI(id));
        }, 0);
        return;
    }

    // COMPARE / COMPARE WITH DATASET 2
    if (cond === "compare" || cond === "compare2") {
        const isD2 = cond === "compare2";

        // Column 1 (always Dataset 1) is still bound to colSelect_${id} —
        // for plain Compare it's shown and pickable as before. For
        // Compare with Dataset 2, it's implicit: the column you dragged
        // the bubble onto (or, for a blank filter, still pickable — the
        // field exists, just hidden once a column is already set). No
        // separate configuration needed on the Dataset 1 side beyond
        // that, since Dataset 1's join key is the project's existing
        // UUID/ID column (Settings → UUID/ID column), reused automatically.
        const col1El = ds1Dropdown(`colSelect_${id}`, "Dataset 1 Column 1");
        col1El.id = `compareCol1Wrap_${id}`;
        if (isD2) col1El.style.display = "none";
        grid.appendChild(col1El);

        const subCondDiv = document.createElement("div");
        subCondDiv.className = "rule-field dynamic-field";
        subCondDiv.innerHTML = `
            <select id="compareSubCond_${id}" onchange="handleCompareSubCondUI(${id})">
                <option value="" disabled selected>Sub-Condition</option>
                <option value="empty">Missing / Null</option>
                <option value="dup">Duplicate</option>
                <option value="eq">Equal To</option>
                <option value="neq">Not Equal To</option>
                <option value="gt">Greater Than</option>
                <option value="lt">Less Than</option>
                <option value="gte">Greater Than or Equal To</option>
                <option value="lte">Less Than or Equal To</option>
                <option value="btwn">Between</option>
                <option value="nbtwn">Not Between</option>
                <option value="and">AND</option>
                <option value="or">OR</option>
                <option value="date">Date</option>
            </select>
        `;
        grid.appendChild(subCondDiv);

        // Dataset 2's join key — the actual row-matching column, NOT a
        // positional row-index guess. Every compare2 mode (simple, AND/OR,
        // Date) needs this, so it's always shown once compare2 is picked,
        // independent of which sub-condition is chosen below.
        if (isD2) {
            const d2UuidWrap = document.createElement("div");
            d2UuidWrap.id = `compareD2UuidWrap_${id}`;
            d2UuidWrap.className = "rule-field dynamic-field";
            if (!allColumns2 || allColumns2.length === 0) {
                d2UuidWrap.appendChild(ds2LoadPill());
            } else {
                d2UuidWrap.appendChild(colComboSearch(`compareD2Uuid_${id}`, "Dataset 2 UUID Column", allColumns2));
            }
            grid.appendChild(d2UuidWrap);
        }

        // Col 2 stays in the grid row (shown for simple sub-conditions).
        // For compare2, this comes from Dataset 2 — show the load hint in
        // place of the dropdown until Dataset 2 has been uploaded.
        const col2Wrap = document.createElement("div");
        col2Wrap.id = `compareCol2Wrap_${id}`;
        col2Wrap.className = "rule-field dynamic-field";
        col2Wrap.style.display = "block";
        if (isD2 && (!allColumns2 || allColumns2.length === 0)) {
            col2Wrap.appendChild(ds2LoadPill());
        } else {
            const col2Combo = colComboSearch(`compareCol2_${id}`, isD2 ? "Dataset 2 Column" : "Dataset 1 Column 2", isD2 ? allColumns2 : allColumns);
            col2Wrap.appendChild(col2Combo);
        }
        grid.appendChild(col2Wrap);

        // Row 2: AND/OR subfilters render BELOW the card grid (not inside it)
        const box = document.getElementById(`tableBox_${id}`);
        const compareExtra = document.createElement("div");
        compareExtra.id = `compareExtra_${id}`;
        compareExtra.style.marginTop = "10px";
        box.appendChild(compareExtra);

        return;
    }

    // COORDINATE CHECK
    if (cond === "coords") {
        // Longitude column
        grid.appendChild(ds1Dropdown(`coordLngCol_${id}`, "Longitude Column"));
        // Latitude column
        grid.appendChild(ds1Dropdown(`coordLatCol_${id}`, "Latitude Column"));

        // Admin level picker
        const levelDiv = document.createElement("div");
        levelDiv.className = "rule-field dynamic-field";
        levelDiv.innerHTML = `
            <select id="coordLevel_${id}">
                <option value="" disabled selected>Resolve to…</option>
                <option value="district">District</option>
                <option value="tehsil">Tehsil</option>
                <option value="uc">Union Council (UC)</option>
            </select>
        `;
        grid.appendChild(levelDiv);

        // Verification column (column whose value is compared against resolved name)
        grid.appendChild(ds1Dropdown(`coordVerifyCol_${id}`, "Verification Column"));
        return;
    }

    // BAD PATTERN (placeholder/junk-value detection — one or more values)
    if (cond === "bad_pattern") {
        grid.appendChild(ds1Dropdown(`colSelect_${id}`, "Column to Check"));

        const wrap = document.createElement("div");
        wrap.className = "rule-field dynamic-field bad-pattern-field";
        wrap.innerHTML = `
            <div class="bad-pattern-chips" id="badPatternChips_${id}"></div>
            <div class="bad-pattern-input-row">
                <input type="text" id="badPatternInput_${id}"
                       placeholder="Type a value (e.g. 11111, 0000) and press Enter"
                       onkeydown="if(event.key==='Enter'){event.preventDefault();_addBadPatternValue(${id});}">
                <button type="button" class="btn-tool" onclick="_addBadPatternValue(${id})">Add</button>
            </div>
            <!-- Hidden field that loadGeneratedFilters() reads — kept in sync as chips change -->
            <input type="hidden" id="matchInput_${id}" value="">
        `;
        grid.appendChild(wrap);
        window._badPatternValues = window._badPatternValues || {};
        window._badPatternValues[id] = [];
        return;
    }

    // AND / OR
    if (cond === "and" || cond === "or") {
        const box = document.getElementById(`tableBox_${id}`);
        box.insertAdjacentHTML("beforeend", `
            <div id="subContainer_${id}" class="subfilters-wrap">
                <div id="subRows_${id}"></div>
                <button onclick="addSubfilter(${id})" class="btn sub-add-btn">+ Add Sub-Filter</button>
            </div>
        `);
        // Start with 2 subfilters by default
        addSubfilter(id);
        addSubfilter(id);
    }
}

// Counter per parent filter to give each subfilter a unique incrementing index
window._subCounters = window._subCounters || {};

function addSubfilter(id) {
    if (!window._subCounters[id]) window._subCounters[id] = 0;
    window._subCounters[id]++;
    const i = window._subCounters[id];

    const rowsDiv = document.getElementById(`subRows_${id}`);
    if (!rowsDiv) return;

    const row = document.createElement("div");
    row.className = "subfilter";
    row.id = `subRow_${id}_${i}`;

    // Build the combo search for the column picker
    const colComboDiv = colComboSearch(`sub_col_${id}_${i}`, "Dataset 1 Column", allColumns);
    colComboDiv.classList.add("rule-field");
    // Wire onchange to updateSubUnique via the hidden select
    setTimeout(() => {
        const hiddenSel = document.getElementById(`sub_col_${id}_${i}`);
        if (hiddenSel) hiddenSel.addEventListener("change", () => updateSubUnique(id, i));
    }, 0);

    // Build the rest of the row via innerHTML in a temp element
    const restDiv = document.createElement("div");
    restDiv.innerHTML = `
        <div class="rule-field">
            <select id="sub_cond_${id}_${i}" onchange="handleSubCondUI(${id}, ${i})">
                <option disabled selected>Condition</option>
                <option value="empty">Missing / Null</option>
                <option value="dup">Duplicate</option>
                <option value="eq">Equal To</option>
                <option value="neq">Not Equal To</option>
                <option value="gt">Greater Than</option>
                <option value="lt">Less Than</option>
                <option value="gte">Greater Than or Equal To</option>
                <option value="lte">Less Than or Equal To</option>
                <option value="btwn">Between</option>
                <option value="nbtwn">Not Between</option>
                <option value="date">Date</option>
                <option value="compare">Compare</option>
            </select>
        </div>
        <div class="rule-field" id="sub_unique_wrap_${id}_${i}" style="display:none">
            <select id="sub_unique_${id}_${i}" onchange="setSubMatch(${id}, ${i})">
                <option disabled selected>Unique Values</option>
            </select>
        </div>
        <div class="rule-field" id="sub_val_wrap_${id}_${i}" style="display:none">
            <input type="text" id="sub_val_${id}_${i}" placeholder="Value to Match">
        </div>
        <button class="sub-remove-btn" title="Remove this sub-filter" onclick="removeSubfilter(${id}, ${i})">✕</button>
    `;

    row.appendChild(colComboDiv);
    Array.from(restDiv.childNodes).forEach(n => row.appendChild(n));
    rowsDiv.appendChild(row);
}

function removeSubfilter(id, i) {
    const row = document.getElementById(`subRow_${id}_${i}`);
    if (row) row.remove();
}

function updateSubUnique(id, i) {
    const colIdx = document.getElementById(`sub_col_${id}_${i}`).value;
    if (colIdx === "") return;
    const uniques = [...new Set(allColumns[colIdx].values.filter(v => v !== ""))];
    const u = document.getElementById(`sub_unique_${id}_${i}`);
    u.innerHTML = "<option disabled selected>Unique Values</option>";
    uniques.forEach(v => u.add(new Option(v, v)));
    const cond = document.getElementById(`sub_cond_${id}_${i}`)?.value;
    if (cond) handleSubCondUI(id, i);
}

function handleSubCondUI(id, i) {
    const cond = document.getElementById(`sub_cond_${id}_${i}`).value;
    const uniqueWrap = document.getElementById(`sub_unique_wrap_${id}_${i}`);
    const valWrap    = document.getElementById(`sub_val_wrap_${id}_${i}`);
    const valInput   = document.getElementById(`sub_val_${id}_${i}`);
    const needsFields = ["eq","neq","gt","lt","gte","lte","btwn","nbtwn"].includes(cond);
    const noFields = ["empty","dup","date","compare"].includes(cond);
    uniqueWrap.style.display = needsFields ? "block" : "none";
    valWrap.style.display    = needsFields ? "block" : "none";
    if (needsFields) valInput.placeholder = (cond === "btwn" || cond === "nbtwn") ? "min,max  e.g. 10,50" : "Value to Match";

    // Remove any existing date/compare sub-UI for this sub-row
    const row = document.getElementById(`subRow_${id}_${i}`);
    if (row) {
        row.querySelectorAll(".sub-extra-ui").forEach(el => el.remove());
    }

    // Date sub-condition: add a date condition selector
    if (cond === "date" && row) {
        const dateWrap = document.createElement("div");
        dateWrap.className = "rule-field sub-extra-ui";
        dateWrap.innerHTML = `
            <select id="sub_date_cond_${id}_${i}" onchange="handleSubDateCondUI(${id},${i})">
                <option value="" disabled selected>Date Condition</option>
                <option value="date_eq">Equal To (exact date)</option>
                <option value="date_neq">Not Equal To</option>
                <option value="date_before">Before</option>
                <option value="date_after">After</option>
                <option value="date_before_eq">Before or Equal To</option>
                <option value="date_after_eq">After or Equal To</option>
                <option value="date_btwn">Between (date range)</option>
                <option value="date_nbtwn">Not Between (date range)</option>
                <option value="date_empty">Missing / Null Date</option>
                <option value="date_invalid">Invalid Date Format</option>
                <option value="date_year_eq">Year Equals</option>
                <option value="date_year_gt">Year Greater Than</option>
                <option value="date_year_lt">Year Less Than</option>
                <option value="date_month_eq">Month Equals (1-12)</option>
                <option value="date_day_eq">Day Equals (1-31)</option>
                <option value="date_weekday">Weekday (Mon=1 … Sun=7)</option>
                <option value="date_future">In the Future</option>
                <option value="date_past">In the Past</option>
            </select>`;
        const dateInputWrap = document.createElement("div");
        dateInputWrap.className = "sub-extra-ui";
        dateInputWrap.id = `sub_date_inputs_${id}_${i}`;
        const removeBtn = row.querySelector(".sub-remove-btn");
        if (removeBtn) {
            row.insertBefore(dateWrap, removeBtn);
            row.insertBefore(dateInputWrap, removeBtn);
        } else {
            row.appendChild(dateWrap);
            row.appendChild(dateInputWrap);
        }
    }

    // Compare sub-condition: add col2 picker
    if (cond === "compare" && row) {
        const col2Combo = colComboSearch(`sub_cmp_col2_${id}_${i}`, "Compare Column", allColumns);
        col2Combo.classList.add("rule-field", "sub-extra-ui");
        const condDiv = document.createElement("div");
        condDiv.className = "rule-field sub-extra-ui";
        condDiv.innerHTML = `
            <select id="sub_cmp_cond_${id}_${i}">
                <option value="" disabled selected>Compare Condition</option>
                <option value="eq">Equal To</option>
                <option value="neq">Not Equal To</option>
                <option value="gt">Greater Than</option>
                <option value="lt">Less Than</option>
                <option value="gte">Greater Than or Equal To</option>
                <option value="lte">Less Than or Equal To</option>
            </select>`;
        const col3Combo = colComboSearch(`sub_cmp_col3_${id}_${i}`, "Compare Column 2", allColumns);
        col3Combo.classList.add("rule-field", "sub-extra-ui");
        const removeBtn = row.querySelector(".sub-remove-btn");
        if (removeBtn) {
            row.insertBefore(col2Combo, removeBtn);
            row.insertBefore(condDiv, removeBtn);
            row.insertBefore(col3Combo, removeBtn);
        } else {
            row.appendChild(col2Combo);
            row.appendChild(condDiv);
            row.appendChild(col3Combo);
        }
    }
}

function handleSubDateCondUI(id, i) {
    const dateCond = document.getElementById(`sub_date_cond_${id}_${i}`)?.value;
    const wrap = document.getElementById(`sub_date_inputs_${id}_${i}`);
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!dateCond || ["date_empty","date_invalid","date_future","date_past"].includes(dateCond)) return;

    const needsSingle = ["date_eq","date_neq","date_before","date_after","date_before_eq","date_after_eq",
                         "date_year_eq","date_year_gt","date_year_lt","date_month_eq","date_day_eq","date_weekday"];
    const needsRange  = ["date_btwn","date_nbtwn"];
    const isDatePicker = ["date_eq","date_neq","date_before","date_after","date_before_eq","date_after_eq"].includes(dateCond);
    const placeholders = { "date_year_eq":"e.g. 2023","date_year_gt":"e.g. 2020","date_year_lt":"e.g. 2025",
                           "date_month_eq":"1 – 12","date_day_eq":"1 – 31","date_weekday":"1=Mon … 7=Sun" };

    if (needsSingle.includes(dateCond)) {
        const d = document.createElement("div");
        d.className = "rule-field";
        const inp = document.createElement("input");
        inp.id = `sub_date_val1_${id}_${i}`;
        inp.type = isDatePicker ? "date" : "text";
        inp.placeholder = placeholders[dateCond] || "Value";
        inp.className = "date-input";
        d.appendChild(inp);
        wrap.appendChild(d);
    }
    if (needsRange.includes(dateCond)) {
        ["Start Date","End Date"].forEach((ph, k) => {
            const d = document.createElement("div");
            d.className = "rule-field";
            const inp = document.createElement("input");
            inp.id = k === 0 ? `sub_date_val1_${id}_${i}` : `sub_date_val2_${id}_${i}`;
            inp.type = "date";
            inp.placeholder = ph;
            inp.className = "date-input";
            d.appendChild(inp);
            wrap.appendChild(d);
        });
    }
}

function setSubMatch(id, i) {
    const unique = document.getElementById(`sub_unique_${id}_${i}`);
    const match  = document.getElementById(`sub_val_${id}_${i}`);
    if (unique && match) match.value = unique.value;
}

/* =============================
   DATE CONDITION UI
============================= */
function handleDateCondUI(id) {
    const dateCondSel = document.getElementById(`dateCond_${id}`);
    if (!dateCondSel) return;
    const cond = dateCondSel.value;
    const wrap = document.getElementById(`dateInputWrap_${id}`);
    if (!wrap) return;

    wrap.innerHTML = "";

    // Conditions that need no input fields
    if (["date_empty", "date_invalid", "date_future", "date_past", ""].includes(cond)) return;

    const needsSingle = ["date_eq","date_neq","date_before","date_after","date_before_eq","date_after_eq",
                         "date_year_eq","date_year_gt","date_year_lt","date_month_eq","date_day_eq","date_weekday"];
    const needsRange  = ["date_btwn","date_nbtwn"];

    if (needsSingle.includes(cond)) {
        const isDatePicker = ["date_eq","date_neq","date_before","date_after","date_before_eq","date_after_eq"].includes(cond);
        const isNumeric    = ["date_year_eq","date_year_gt","date_year_lt","date_month_eq","date_day_eq","date_weekday"].includes(cond);

        const placeholders = {
            "date_year_eq": "e.g. 2023",
            "date_year_gt": "e.g. 2020",
            "date_year_lt": "e.g. 2025",
            "date_month_eq": "1 – 12",
            "date_day_eq": "1 – 31",
            "date_weekday": "1=Mon … 7=Sun"
        };

        const d1 = document.createElement("div");
        d1.className = "rule-field";
        const inp = document.createElement("input");
        inp.id = `dateVal1_${id}`;
        inp.type = isDatePicker ? "date" : "text";
        inp.placeholder = placeholders[cond] || "Value";
        inp.className = "date-input";
        d1.appendChild(inp);
        wrap.appendChild(d1);
    }

    if (needsRange.includes(cond)) {
        const d1 = document.createElement("div");
        d1.className = "rule-field";
        const inp1 = document.createElement("input");
        inp1.id = `dateVal1_${id}`;
        inp1.type = "date";
        inp1.placeholder = "Start Date";
        inp1.className = "date-input";
        d1.appendChild(inp1);

        const d2 = document.createElement("div");
        d2.className = "rule-field";
        const inp2 = document.createElement("input");
        inp2.id = `dateVal2_${id}`;
        inp2.type = "date";
        inp2.placeholder = "End Date";
        inp2.className = "date-input";
        d2.appendChild(inp2);

        wrap.appendChild(d1);
        wrap.appendChild(d2);
    }
}


function handleCompareDateCondUI(id) {
    // Compare Date compares two date columns. No fixed date input is needed here.
    // The main Date filter still uses dateVal1/dateVal2 for fixed date values.
    const wrap = document.getElementById(`compareDateInputWrap_${id}`);
    if (wrap) wrap.innerHTML = "";
}

/* =============================
   COMPARE SUB-CONDITION UI
============================= */
function handleCompareSubCondUI(id) {
    const subCond   = document.getElementById(`compareSubCond_${id}`)?.value;
    const extraWrap = document.getElementById(`compareExtra_${id}`);
    const col1Wrap  = document.getElementById(`compareCol1Wrap_${id}`);
    const col2Wrap  = document.getElementById(`compareCol2Wrap_${id}`);
    const grid      = document.querySelector(`#tableBox_${id} .rule-grid`);
    if (!extraWrap || !grid) return;
    const isD2 = document.getElementById(`condSelect_${id}`)?.value === "compare2";

    extraWrap.innerHTML = "";

    if (subCond === "and" || subCond === "or") {
        // AND/OR: no Col 1 needed — each subfilter has its own Col A & Col B
        if (col1Wrap) col1Wrap.style.display = "none";
        if (col2Wrap) col2Wrap.style.display = "none";

        extraWrap.innerHTML = `
            <div id="compareSubContainer_${id}" class="subfilters-wrap" style="margin-top:4px;">
                <div id="compareSubRows_${id}"></div>
                <button onclick="addCompareSubfilter(${id})" class="btn sub-add-btn">+ Add Sub-Filter</button>
            </div>
        `;
        addCompareSubfilter(id);
        addCompareSubfilter(id);
    } else if (subCond === "date") {
        // Compare Date: compare Date Column 1 against Date Column 2.
        // For "compare" (same dataset), Date Column 1 gets its own picker
        // since it may differ from the top Column 1 field. For compare2,
        // Column 1 is already fixed (the dragged column + the project's
        // UUID column) — no separate picker, just a read-only reflection
        // of what's already chosen.
        if (col1Wrap) col1Wrap.style.display = "none";
        if (col2Wrap) col2Wrap.style.display = "none";

        extraWrap.innerHTML = `
            <div class="rule-grid" style="margin-top:6px;">
                <div class="rule-field" id="compareDateCol1Wrap_${id}"></div>
                <div class="rule-field">
                    <select id="compareDateCond_${id}" onchange="handleCompareDateCondUI(${id})">
                        <option value="" disabled selected>Date Compare Condition</option>
                        <option value="date_eq">Equal To</option>
                        <option value="date_neq">Not Equal To</option>
                        <option value="date_before">Before</option>
                        <option value="date_after">After</option>
                        <option value="date_before_eq">Before or Equal To</option>
                        <option value="date_after_eq">After or Equal To</option>
                        <option value="date_empty">Missing / Null mismatch</option>
                        <option value="date_invalid">Invalid Date mismatch</option>
                        <option value="date_year_eq">Year Equals</option>
                        <option value="date_month_eq">Month Equals</option>
                        <option value="date_day_eq">Day Equals</option>
                        <option value="date_weekday">Weekday Equals</option>
                    </select>
                </div>
                <div class="rule-field" id="compareDateCol2Wrap_${id}"></div>
                <div id="compareDateInputWrap_${id}" class="dynamic-field"></div>
            </div>
        `;

        const col1DateWrap = document.getElementById(`compareDateCol1Wrap_${id}`);
        if (isD2) {
            const colSel  = document.getElementById(`colSelect_${id}`);
            const colName = colSel && colSel.value !== "" ? colSel.options[colSel.selectedIndex]?.text : null;
            col1DateWrap.innerHTML = `<div class="ds2-hint" style="min-height:36px;">Date Column 1: <b style="margin-left:4px;color:var(--text-1)">${colName || "(no column selected)"}</b></div>`;
        } else {
            const col1Combo = colComboSearch(`cmpDateCol1_${id}`, "Date Column 1", allColumns);
            col1DateWrap.appendChild(col1Combo);
        }
        const col2Wrapper = document.getElementById(`compareDateCol2Wrap_${id}`);
        if (isD2 && (!allColumns2 || allColumns2.length === 0)) {
            col2Wrapper?.appendChild(ds2LoadPill());
        } else {
            const col2Combo = colComboSearch(`cmpDateCol2_${id}`, isD2 ? "Dataset 2 Date Column" : "Date Column 2", isD2 ? allColumns2 : allColumns);
            col2Wrapper?.appendChild(col2Combo);
        }

    } else {
        // Simple sub-condition (eq, neq, gt, lt, gte, lte, btwn, nbtwn, empty, dup):
        // Column 2 is visible; Column 1 too, EXCEPT for compare2 where it
        // stays hidden (implicit from the drag — no re-picking needed).
        if (col1Wrap) col1Wrap.style.display = isD2 ? "none" : "block";
        if (col2Wrap) col2Wrap.style.display = "block";
    }
}

window._compareSubCounters = window._compareSubCounters || {};

function addCompareSubfilter(id) {
    if (!window._compareSubCounters[id]) window._compareSubCounters[id] = 0;
    window._compareSubCounters[id]++;
    const i = window._compareSubCounters[id];
    const isD2 = document.getElementById(`condSelect_${id}`)?.value === "compare2";

    const rowsDiv = document.getElementById(`compareSubRows_${id}`);
    if (!rowsDiv) return;

    const row = document.createElement("div");
    row.className = "subfilter";
    row.id = `compareSubRow_${id}_${i}`;

    // Col A combo — always Dataset 1
    const colACombo = colComboSearch(`cmp_colA_${id}_${i}`, "Column A (D1)", allColumns);
    colACombo.classList.add("rule-field");

    // Sub-cond select
    const condDiv = document.createElement("div");
    condDiv.className = "rule-field";
    condDiv.innerHTML = `
        <select id="cmp_cond_${id}_${i}">
            <option disabled selected>Condition</option>
            <option value="empty">Missing / Null</option>
            <option value="dup">Duplicate</option>
            <option value="eq">Equal To</option>
            <option value="neq">Not Equal To</option>
            <option value="gt">Greater Than</option>
            <option value="lt">Less Than</option>
            <option value="gte">Greater Than or Equal To</option>
            <option value="lte">Less Than or Equal To</option>
            <option value="btwn">Between</option>
            <option value="nbtwn">Not Between</option>
            <option value="date">Date</option>
        </select>
    `;

    // Col B combo — Dataset 2 for compare2, Dataset 1 for compare
    let colBCombo;
    if (isD2 && (!allColumns2 || allColumns2.length === 0)) {
        colBCombo = ds2LoadPill();
    } else {
        colBCombo = colComboSearch(`cmp_colB_${id}_${i}`, isD2 ? "Column B (D2)" : "Column B (D1)", isD2 ? allColumns2 : allColumns);
        colBCombo.classList.add("rule-field");
    }

    // Remove btn
    const removeBtn = document.createElement("button");
    removeBtn.className = "sub-remove-btn";
    removeBtn.title = "Remove";
    removeBtn.textContent = "✕";
    removeBtn.onclick = () => row.remove();

    row.appendChild(colACombo);
    row.appendChild(condDiv);
    row.appendChild(colBCombo);
    row.appendChild(removeBtn);
    rowsDiv.appendChild(row);
}